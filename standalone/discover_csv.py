# Copyright (c) 2026 Cisco and/or its affiliates.
#
# This software is licensed to you under the terms of the Cisco Sample
# Code License, Version 1.1 (the "License"). You may obtain a copy of the
# License at
#
#                https://developer.cisco.com/docs/licenses
#
# All use of the material herein must be in accordance with the terms of
# the License. All rights not expressly granted by the License are
# reserved. Unless required by applicable law or agreed to separately in
# writing, software distributed under the License is distributed on an "AS
# IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express
# or implied.
#
# DEMONSTRATION CODE: This sample is provided for demonstration and example
# purposes only. It is not supported and has not been tested for production
# use. The customer is solely responsible for reviewing, testing and
# validating it, and for any use or execution in their own environments,
# including production.

"""Standalone: start Catalyst Center discovery jobs for devices listed in a CSV file.

This file needs only the Python packages catalystcentersdk, python-dotenv and
requests (see requirements.txt). It does not import any other script.

CSV columns: ip_address (required), credential_profile (optional). An empty or
missing credential_profile uses the default (first) profile in .env. Put your
settings in a .env file next to this script or in the folder you run it from
(see .env.example in the project root).

    python discover_csv.py devices.csv                 # submit, then wait and report
    python discover_csv.py devices.csv --dry-run       # only show what would be sent
    python discover_csv.py devices.csv --name-prefix office --no-wait
    python discover_csv.py devices.csv --netconf       # also use NETCONF on port 830 (or --netconf-port N)
"""

from __future__ import annotations

import argparse
import csv
import ipaddress
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

import requests
import urllib3
from catalystcentersdk import CatalystCenterAPI
from catalystcentersdk.exceptions import catalystcentersdkException
from dotenv import load_dotenv

# Reading guide: [main task] marks where the real work happens, [credentials]
# where credentials or secrets are read, looked up or kept out of output, and
# [error handling] where input is checked or errors are caught and explained.

# SDK 3.1.6.0.0 has no 2.3.7.10 model; 2.3.7.9 is the nearest earlier one.
SDK_API_VERSION = "2.3.7.9"
REQUEST_TIMEOUT_SECONDS = 30
PROTOCOL_ORDER = "ssh"
TIMEOUT_SECONDS = 5
RETRY_COUNT = 3
POLL_INTERVAL_SECONDS = 5
DEFAULT_WAIT_TIMEOUT_SECONDS = 300
NETCONF_DEFAULT_PORT = 830


class ScriptError(RuntimeError):
    """Invalid configuration or input, or a discovery job that failed."""


NO_ENV_HINT = (
    ". No .env file was found next to this script or in the current folder: "
    "copy .env.example to .env and fill it in, or set these in the environment."
)


# ===========================================================================
# 1. Settings from .env
# ===========================================================================


# [credentials] A profile is one named set of credentials (IDs or descriptions) for
# devices that share the same logins. One discovery job is submitted per profile.
@dataclass(frozen=True)
class Profile:
    name: str
    cli: tuple[str, ...]  # credential IDs or descriptions
    snmp: tuple[str, ...]


# [credentials] The API login and profiles come from .env or the environment, never hard-coded.
@dataclass(frozen=True)
class Settings:
    base_url: str
    username: str
    password: str = field(repr=False)
    verify: bool | str  # True, False (lab only) or a CA bundle path
    profiles: Mapping[str, Profile]
    default_profile: str


def find_env_file() -> Path | None:
    """A .env next to this script, else in the current folder. Optional."""
    for folder in (Path(__file__).resolve().parent, Path.cwd()):
        candidate = folder / ".env"
        if candidate.is_file():
            return candidate
    return None


def _split(raw: str) -> tuple[str, ...]:
    items: list[str] = []
    for item in raw.split(","):
        item = item.strip()
        if item and item not in items:
            items.append(item)
    return tuple(items)


def load_settings(environ: Mapping[str, str] | None = None) -> Settings:
    """Read settings from the environment (after loading .env, which never overrides it)."""
    # [credentials] Load .env into the environment (the file is optional). Variables that
    # are already set in the real environment win over the file.
    no_env_file = False
    if environ is None:
        env_file = find_env_file()
        if env_file:
            load_dotenv(env_file, override=False)
        no_env_file = env_file is None
        environ = os.environ

    def get(name: str) -> str:
        return environ.get(name, "").strip()

    # [error handling] Check everything up front, so a mistake in .env is reported
    # before any API call is made.
    required = ["CATALYST_CENTER_BASE_URL", "CATALYST_CENTER_USERNAME",
                "CATALYST_CENTER_PASSWORD", "CREDENTIAL_PROFILES"]
    missing = [name for name in required if not get(name)]
    if missing:
        raise ScriptError("Missing required setting(s): " + ", ".join(missing) + NO_ENV_HINT * no_env_file)

    base_url = get("CATALYST_CENTER_BASE_URL").rstrip("/")
    if not base_url.lower().startswith("https://") or "/" in base_url[8:]:
        raise ScriptError(
            "CATALYST_CENTER_BASE_URL must be an HTTPS origin such as "
            "https://catalyst-center.example.com"
        )

    ca_bundle = get("CATALYST_CENTER_CA_BUNDLE")
    if ca_bundle and not Path(ca_bundle).is_file():
        raise ScriptError("CATALYST_CENTER_CA_BUNDLE is not a readable file")

    # Lab only: certificate checking can be switched off, but never together with a CA bundle.
    verify_text = get("CATALYST_CENTER_VERIFY_SSL").lower()
    if verify_text in ("", "true", "1", "yes", "on"):
        verify_ssl = True
    elif verify_text in ("false", "0", "no", "off"):
        verify_ssl = False
    else:
        raise ScriptError(
            "CATALYST_CENTER_VERIFY_SSL must be true or false, not {!r}".format(
                environ.get("CATALYST_CENTER_VERIFY_SSL")
            )
        )
    if not verify_ssl and ca_bundle:
        raise ScriptError(
            "Set either CATALYST_CENTER_CA_BUNDLE or CATALYST_CENTER_VERIFY_SSL=false, not both"
        )

    # [credentials] Reads each profile named in CREDENTIAL_PROFILES from the variables
    # CRED_<NAME>_CLI_IDS and CRED_<NAME>_SNMPV2_RO_IDS.
    # [error handling] A bad or incomplete profile stops the script before any API call.
    profiles: dict[str, Profile] = {}
    for raw_name in get("CREDENTIAL_PROFILES").split(","):
        name = raw_name.strip()
        if not name:
            continue
        if not re.fullmatch(r"[A-Za-z0-9_]+", name):
            raise ScriptError(
                "Invalid profile name {!r}: use letters, digits and underscores".format(name)
            )
        key = name.lower()
        if key in profiles:
            raise ScriptError("Profile {!r} is listed more than once".format(name))
        prefix = "CRED_{}_".format(name.upper())
        cli, snmp = _split(get(prefix + "CLI_IDS")), _split(get(prefix + "SNMPV2_RO_IDS"))
        if not cli or not snmp:
            raise ScriptError(
                "Profile {!r} needs at least one value in both {}CLI_IDS and {}SNMPV2_RO_IDS".format(
                    name, prefix, prefix
                )
            )
        profiles[key] = Profile(key, cli, snmp)
    if not profiles:
        raise ScriptError("CREDENTIAL_PROFILES does not list any profile")

    return Settings(
        base_url=base_url,
        username=get("CATALYST_CENTER_USERNAME"),
        password=environ["CATALYST_CENTER_PASSWORD"],
        verify=False if not verify_ssl else (ca_bundle or True),
        profiles=profiles,
        default_profile=next(iter(profiles)),
    )


def warn_if_insecure(settings: Settings) -> None:
    if settings.verify is False:
        print(
            "WARNING: TLS certificate verification is DISABLED "
            "(CATALYST_CENTER_VERIFY_SSL=false). Use this only in a lab.",
            file=sys.stderr,
        )
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


def connect(settings: Settings) -> CatalystCenterAPI:
    # [credentials] Log in. The SDK requests the auth token (POST /dna/system/api/v1/auth/token)
    # and sends it with every later call.
    return CatalystCenterAPI(
        username=settings.username,
        password=settings.password,
        base_url=settings.base_url,
        version=SDK_API_VERSION,
        verify=settings.verify,
        single_request_timeout=REQUEST_TIMEOUT_SECONDS,
    )


# ===========================================================================
# 2. Devices (from the CSV), profiles and the discovery payload
# ===========================================================================


def read_devices(csv_path: Path) -> list[tuple[str, str | None]]:
    """Return (ip_address, credential_profile) pairs from the CSV."""
    # [error handling] A missing column, an empty IP or an empty file stops the script with
    # a clear message. "utf-8-sig" accepts the byte-order mark that Excel adds.
    try:
        with open(csv_path, encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            columns = {(name or "").strip(): name for name in reader.fieldnames or []}
            if "ip_address" not in columns:
                raise ScriptError("CSV needs an 'ip_address' column in its header row")
            ip_column = columns["ip_address"]
            profile_column = columns.get("credential_profile")

            devices: list[tuple[str, str | None]] = []
            for row in reader:
                ip = row.get(ip_column)
                if ip is None or not ip.strip():
                    raise ScriptError("CSV line {}: empty ip_address".format(reader.line_num))
                devices.append((ip, row.get(profile_column) if profile_column else None))
    except csv.Error as exc:
        raise ScriptError("Could not parse CSV: {}".format(exc)) from exc
    if not devices:
        raise ScriptError("CSV contains no devices")
    return devices


def group_by_profile(
    devices: Sequence[tuple[str, str | None]], settings: Settings
) -> dict[str, list[str]]:
    """Group (ip, profile) pairs by profile, in input order. No profile = the default."""
    groups: dict[str, list[str]] = {}
    seen: set[str] = set()
    for raw_ip, raw_profile in devices:
        # [error handling] Reject bad IPs, repeated IPs and unknown profiles now, not
        # halfway through a run.
        try:
            address = ipaddress.ip_address(raw_ip.strip())
        except ValueError:
            raise ScriptError("Invalid IP address: {!r}".format(raw_ip)) from None
        if address.version != 4:
            raise ScriptError("Only IPv4 addresses are supported: {!r}".format(raw_ip))
        ip = str(address)
        if ip in seen:
            raise ScriptError("IP address listed more than once: {}".format(ip))
        seen.add(ip)

        profile = (raw_profile or "").strip().lower() or settings.default_profile
        if profile not in settings.profiles:
            raise ScriptError(
                "Unknown credential profile {!r} for {} (defined: {})".format(
                    raw_profile, ip, ", ".join(settings.profiles)
                )
            )
        groups.setdefault(profile, []).append(ip)
    if not groups:
        raise ScriptError("No devices to discover")
    return groups


def collapse_ranges(ip_addresses: Sequence[str]) -> list[tuple[str, str]]:
    """Merge IPv4 addresses into contiguous (start, end) pairs; gaps are never covered."""
    pairs: list[list[int]] = []
    for number in sorted({int(ipaddress.IPv4Address(ip)) for ip in ip_addresses}):
        if pairs and number == pairs[-1][1] + 1:
            pairs[-1][1] = number
        else:
            pairs.append([number, number])
    return [(str(ipaddress.IPv4Address(a)), str(ipaddress.IPv4Address(b))) for a, b in pairs]


def address_list(ip_addresses: Sequence[str]) -> tuple[str, str]:
    """Return (discoveryType, ipAddressList) in the format Catalyst Center expects.

    One IP is 'Single' ("10.0.0.1"); one contiguous block is 'Range' with ONE
    start-end pair ("10.0.0.1-10.0.0.4"); anything else is 'Multi Range' with
    comma-separated start-end pairs. A comma-separated list of bare IPs is
    rejected ("IPAddress for Range Discovery is invalid").
    """
    pairs = collapse_ranges(ip_addresses)
    if len(pairs) == 1:
        start, end = pairs[0]
        return ("Single", start) if start == end else ("Range", "{}-{}".format(start, end))
    return "Multi Range", ",".join("{}-{}".format(start, end) for start, end in pairs)


def build_payload(
    name: str,
    ip_addresses: Sequence[str],
    credential_ids: Sequence[str],
    netconf_port: int | None = None,
) -> dict[str, Any]:
    # The discovery type and IP list depend on the IPs (see address_list); the other
    # fields are fixed defaults.
    discovery_type, ip_list = address_list(ip_addresses)
    payload: dict[str, Any] = {
        "name": name,
        "discoveryType": discovery_type,
        "ipAddressList": ip_list,
        "protocolOrder": PROTOCOL_ORDER,
        "timeOut": TIMEOUT_SECONDS,
        "retryCount": RETRY_COUNT,
        "isAutoCdp": False,
        "globalCredentialIdList": list(credential_ids),
    }
    # Optional NETCONF: the controller also tries this port. The SDK documents the field as
    # a string and says it needs valid SSH credentials, which every profile already has.
    if netconf_port is not None:
        payload["netconfPort"] = str(netconf_port)
    return payload


def netconf_port_number(text: str) -> int:
    """argparse type for --netconf-port: a TCP port from 1 to 65535."""
    try:
        port = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError("{!r} is not a port number".format(text)) from None
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("port must be between 1 and 65535, not {}".format(port))
    return port


def add_netconf_arguments(parser: argparse.ArgumentParser) -> None:
    """Add the optional NETCONF flags. Without them the discovery is exactly as before."""
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--netconf",
        action="store_true",
        help="also use NETCONF on port {} (needs the SSH CLI credential of the profile)".format(
            NETCONF_DEFAULT_PORT
        ),
    )
    group.add_argument(
        "--netconf-port",
        type=netconf_port_number,
        metavar="PORT",
        help="also use NETCONF on this port instead of {}".format(NETCONF_DEFAULT_PORT),
    )


def netconf_port_from_args(args: argparse.Namespace) -> int | None:
    """The NETCONF port chosen on the command line, or None when NETCONF is not requested."""
    if args.netconf_port is not None:
        return args.netconf_port
    return NETCONF_DEFAULT_PORT if args.netconf else None


def announce_netconf(port: int | None) -> None:
    if port is not None:
        print(
            "NETCONF is on: port {} is added to each discovery (it needs the SSH CLI "
            "credential in the profile).".format(port),
            file=sys.stderr,
        )


# ===========================================================================
# 3. Credentials: .env values may be an ID or a description (like CSS / RO)
# ===========================================================================

CLI_SUBTYPE = "CLI"
SNMPV2_RO_SUBTYPE = "SNMPV2_READ_COMMUNITY"
_KIND_LABEL = {CLI_SUBTYPE: "CLI", SNMPV2_RO_SUBTYPE: "SNMPv2 read-only"}


def response_items(response: Any) -> list[Mapping[str, Any]]:
    body = response.get("response") if isinstance(response, Mapping) else None
    return [item for item in body if isinstance(item, Mapping)] if isinstance(body, list) else []


def _id_of(item: Mapping[str, Any]) -> str:
    return str(item.get("id") or item.get("instanceUuid") or "").strip()


def _describe(item: Mapping[str, Any]) -> str:
    # Only identifying fields are ever shown; secret fields are never printed.
    return "{} ({})".format(item.get("description") or item.get("username") or "(no description)",
                            _id_of(item) or "no id")


def match_credential(ref: str, items: Sequence[Mapping[str, Any]], kind: str) -> str:
    """Resolve an ID or description (CLI: also username) to a credential ID."""
    wanted, label = ref.strip(), _KIND_LABEL.get(kind, kind)

    def text_is(name: str) -> Callable[[Mapping[str, Any]], bool]:
        return lambda item: str(item.get(name) or "").strip().lower() == wanted.lower()

    # [credentials] Try an exact ID first, then the description, then (CLI only) the username.
    stages: list[tuple[str, Callable[[Mapping[str, Any]], bool]]] = [
        ("ID", lambda item: wanted in (item.get("id"), item.get("instanceUuid"))),
        ("description", text_is("description")),
    ]
    if kind == CLI_SUBTYPE:
        stages.append(("username", text_is("username")))

    # [error handling] Several matches, or none, raise an error that lists what exists
    # (names and IDs only, never secrets).
    for stage, test in stages:
        found = [item for item in items if test(item)]
        if len(found) > 1:
            raise ScriptError(
                "{} credential {!r} is ambiguous ({} credentials share that {}): {}. "
                "Use the credential ID instead.".format(
                    label, ref, len(found), stage, ", ".join(_describe(i) for i in found)
                )
            )
        if found:
            if not _id_of(found[0]):
                raise ScriptError("{} credential {!r} was found but has no ID".format(label, ref))
            return _id_of(found[0])

    if items:
        shown = ", ".join(_describe(i) for i in items[:25])
        more = len(items) - 25
        available = "Available: {}{}".format(shown, " and {} more".format(more) if more > 0 else "")
    else:
        available = "The controller has no {} credentials.".format(label)
    raise ScriptError(
        "No {} credential matches {!r}. Use a credential ID or description. {}".format(label, ref, available)
    )


def resolve_credential_ids(api: CatalystCenterAPI, profile: Profile) -> list[str]:
    """Turn a profile's values into real credential IDs (GET /dna/intent/api/v1/global-credential)."""
    # [credentials] Ask the controller for its credentials and swap each value from .env for
    # the real credential ID, which is what a discovery job needs.
    try:
        cli_items = response_items(api.discovery.get_global_credentials(CLI_SUBTYPE))
        snmp_items = response_items(api.discovery.get_global_credentials(SNMPV2_RO_SUBTYPE))
        cli = [match_credential(ref, cli_items, CLI_SUBTYPE) for ref in profile.cli]
        snmp = [match_credential(ref, snmp_items, SNMPV2_RO_SUBTYPE) for ref in profile.snmp]
    except ScriptError as exc:
        # [error handling] Name the profile, so the bad .env entry is easy to find.
        raise ScriptError("Profile {!r}: {}".format(profile.name, exc)) from exc
    return list(dict.fromkeys(cli + snmp))


# ===========================================================================
# 4. Verification: discovery is asynchronous, so follow the task and the discovery
# ===========================================================================

# Discovery states that mean "not finished yet". These values were not confirmed on a
# live controller; run with --raw to see what yours returns.
_RUNNING_CONDITIONS = {"in progress", "inprogress", "in-progress", "queued", "pending", "starting", "running"}
_SECRET_KEY_PARTS = ("password", "passphrase", "community", "secret", "token")


@dataclass
class Report:
    label: str
    ok: bool = False
    lines: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)


# [credentials] Replace secret-looking fields, so raw responses can be printed safely.
def redact(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            key: "***" if any(part in str(key).lower() for part in _SECRET_KEY_PARTS) else redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value


# Catalyst Center wraps results in {"response": ...}. These helpers tolerate missing or
# unexpected fields (names not confirmed live), so a surprise never crashes a check.
def _body(response: Any) -> Mapping[str, Any]:
    body = response.get("response") if isinstance(response, Mapping) else None
    return body if isinstance(body, Mapping) else {}


def _poll(fetch: Callable[[], Any], done: Callable[[Any], bool], deadline: float, interval: float,
          sleep: Callable[[float], None], clock: Callable[[], float]) -> tuple[Any, bool]:
    # Check; if not finished, wait and check again until the deadline passes.
    while True:
        value = fetch()
        if done(value):
            return value, True
        remaining = deadline - clock()
        if remaining <= 0:
            return value, False
        sleep(min(interval, remaining))


def summarize_devices(jobs: Sequence[Mapping[str, Any]]) -> tuple[list[str], bool]:
    """One line per IP; ok only when ping, SNMP and CLI all succeeded."""
    if not jobs:
        return ["- no per-IP results were returned"], False
    lines, all_ok = [], True
    for job in jobs:
        status = {key: str(job.get(name) or "-")
                  for key, name in (("ping", "pingStatus"), ("snmp", "snmpStatus"), ("cli", "cliStatus"))}
        ok = all(value.strip().lower() == "success" for value in status.values())
        all_ok = all_ok and ok
        detail = next((str(job[n]) for n in ("errorDescription", "errDescription", "errorCode") if job.get(n)), "")
        lines.append("- {ip:<15} {state}  ping={ping} snmp={snmp} cli={cli}{detail}".format(
            ip=str(job.get("ipAddress") or "?"), state="OK       " if ok else "ATTENTION",
            detail=": " + detail if detail else "", **status))
    return lines, all_ok


def follow_job(api: CatalystCenterAPI, label: str, task_id: str, timeout: float,
               interval: float = POLL_INTERVAL_SECONDS,
               sleep: Callable[[float], None] = time.sleep,
               clock: Callable[[], float] = time.monotonic) -> Report:
    """Follow a submitted job: task result, then discovery completion, then per-IP results."""
    # [main task] Follow a submitted job in three steps: the task, then the discovery,
    # then each device.
    report = Report(label)
    deadline = clock() + timeout
    try:
        # Step 1: wait for the task (GET /dna/intent/api/v1/task/{taskId}).
        # [error handling] A failed task is reported with its error code and reason.
        task, finished = _poll(
            lambda: api.task.get_task_by_id(task_id),
            lambda t: bool(_body(t).get("isError")) or bool(_body(t).get("endTime")),
            deadline, interval, sleep, clock)
        report.raw["task"] = task
        body = _body(task)
        if not finished:
            report.lines.append("Task {} is still running after {}s. Check later with: "
                                "python check_task.py {}".format(task_id, int(timeout), task_id))
            return report
        if body.get("isError"):
            code = body.get("errorCode")
            reason = body.get("failureReason") or body.get("progress") or "no reason given"
            report.lines.append("Task {} failed: {}{}".format(task_id, "[{}] ".format(code) if code else "", reason))
            return report
        discovery_id = str(body.get("progress") or "").strip()
        if not discovery_id:
            report.lines.append("Task {} completed but its result has no discovery ID, so the devices "
                                "cannot be checked (use --raw to inspect the response).".format(task_id))
            return report
        report.lines.append("Task {} completed".format(task_id))

        # Step 2: the finished task's result is the discovery ID; wait until that discovery
        # stops running (GET /dna/intent/api/v1/discovery/{id}).
        discovery, finished = _poll(
            lambda: api.discovery.get_discovery_by_id(discovery_id),
            lambda d: str(_body(d).get("discoveryCondition") or "").strip().lower() not in _RUNNING_CONDITIONS,
            deadline, interval, sleep, clock)
        report.raw["discovery"] = discovery
        info = _body(discovery)
        header = 'Discovery {} "{}": {}, {} device(s)'.format(
            discovery_id, info.get("name") or "?", info.get("discoveryCondition") or "unknown state",
            info.get("numDevices") if info.get("numDevices") is not None else "?")
        if not finished:
            report.lines.append(header + ". Still running after {}s; check later with: "
                                "python check_task.py {} --discovery".format(int(timeout), task_id))
            return report

        # Step 3: one result per device (ping, SNMP and CLI). The job is OK only if all of them
        # succeeded (GET /dna/intent/api/v1/discovery/{id}/job).
        jobs = api.discovery.get_list_of_discoveries_by_discovery_id(discovery_id)
        report.raw["jobs"] = jobs
        device_lines, report.ok = summarize_devices(response_items(jobs))
        report.lines.append(header)
        report.lines.extend(device_lines)
    # [error handling] Turn a failure into a PROBLEM line, so the other jobs are still reported.
    except (catalystcentersdkException, requests.RequestException, OSError) as exc:
        report.ok = False
        report.lines.append("Verification failed: {}: {}".format(type(exc).__name__, exc))
    return report


def print_reports(reports: Sequence[Report], raw: bool) -> int:
    """Print every report; return 0 only when all are OK."""
    for report in reports:
        print("[{}] {}".format(report.label, "OK" if report.ok else "PROBLEM"))
        for line in report.lines:
            print("  " + line)
        if raw:
            print(json.dumps(redact(report.raw), indent=2, default=str))
    return 0 if all(report.ok for report in reports) else 1


# ===========================================================================
# 5. Main flow
# ===========================================================================


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("csv_file", type=Path, help="CSV with ip_address[,credential_profile]")
    parser.add_argument("--name-prefix", default="discover",
                        help="job names are <prefix>-<profile> (default: %(default)s)")
    parser.add_argument("--dry-run", action="store_true", help="print the payloads without calling the API")
    parser.add_argument("--no-wait", action="store_true",
                        help="submit only; do not wait for or check the discovery result")
    parser.add_argument("--timeout", type=int, default=DEFAULT_WAIT_TIMEOUT_SECONDS,
                        help="seconds to wait per job for results (default: %(default)s)")
    parser.add_argument("--raw", action="store_true",
                        help="also print the controller's raw responses (secrets redacted)")
    add_netconf_arguments(parser)
    args = parser.parse_args()
    # Optional NETCONF (off unless --netconf or --netconf-port is given).
    netconf_port = netconf_port_from_args(args)
    announce_netconf(netconf_port)

    try:
        prefix = args.name_prefix.strip()
        if not prefix:
            raise ScriptError("The discovery name prefix must not be blank")
        # Step 1: read the settings ([credentials]: API login and profiles from .env) and the CSV
        # ([error handling]: bad IPs or unknown profiles stop here, before any API call).
        settings = load_settings()
        groups = group_by_profile(read_devices(args.csv_file), settings)

        if args.dry_run:
            print("# Dry run: credential values are shown as written in .env (not looked up yet).",
                  file=sys.stderr)
            for profile, ips in groups.items():
                refs = settings.profiles[profile].cli + settings.profiles[profile].snmp
                print(json.dumps(build_payload("{}-{}".format(prefix, profile), ips, refs, netconf_port), indent=2))
            return 0

        # Lab only: prints a warning when certificate checking is switched off in .env.
        warn_if_insecure(settings)
        # Step 2 [credentials]: log in (see connect).
        api = connect(settings)

        # Step 3 [credentials]: look up ALL credentials first (GET /dna/intent/api/v1/global-credential),
        # so a typo ([error handling]) stops everything before anything is submitted.
        jobs = []
        for profile, ips in groups.items():
            ids = resolve_credential_ids(api, settings.profiles[profile])
            jobs.append((profile, build_payload("{}-{}".format(prefix, profile), ips, ids, netconf_port)))

        # Step 4 [main task]: submit the discovery (POST /dna/intent/api/v1/discovery), once per
        # credential profile. This only starts it; the discovery keeps running on the controller.
        submitted: list[tuple[str, str]] = []
        for profile, payload in jobs:
            try:
                response = api.discovery.start_discovery(payload=payload, active_validation=False)
                task_id = _body(response).get("taskId")
                if not task_id:
                    raise ScriptError("the response has no task ID: {}".format(json.dumps(redact(response), default=str)))
            # [error handling] On a failure, stop and say which profiles were already submitted.
            except (ScriptError, catalystcentersdkException, requests.RequestException) as exc:
                raise ScriptError("Discovery for profile {!r} failed ({}: {}). Already submitted: {}.".format(
                    profile, type(exc).__name__, exc, ", ".join(p for p, _ in submitted) or "none")) from exc
            submitted.append((profile, str(task_id)))
            print("{}: submitted, task {}".format(profile, task_id))

        if args.no_wait:
            print("Not waiting for results (--no-wait). Check later with: python check_task.py <task-id> --discovery")
            return 0

        # Step 5 [main task]: discovery is asynchronous, so follow each task, then the
        # discovery itself, then each device.
        reports = []
        for profile, task_id in submitted:
            print("Checking {} (task {}, waiting up to {}s)...".format(profile, task_id, args.timeout),
                  file=sys.stderr)
            reports.append(follow_job(api, profile, task_id, args.timeout))
    # [error handling] Expected problems (bad settings, a failed submission, an SDK or network
    # error) print one line and exit 1. Anything unexpected still shows a full traceback.
    except (ScriptError, catalystcentersdkException, requests.RequestException, OSError) as exc:
        print("Error: {}".format(exc), file=sys.stderr)
        return 1

    # Exit code 0 only if every job succeeded.
    return print_reports(reports, args.raw)


if __name__ == "__main__":
    raise SystemExit(main())
