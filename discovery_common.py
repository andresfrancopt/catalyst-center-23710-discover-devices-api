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

"""Shared helpers: .env settings, credential profiles, IP validation, payloads.

This module deliberately does not import the Catalyst Center SDK, so it can be
tested and reused without it.
"""

from __future__ import annotations

import argparse
import csv
import difflib
import ipaddress
import json
import os
import re
import sys
import time
from collections import OrderedDict
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

import urllib3
from dotenv import load_dotenv

# Reading guide: [main task] marks where the real work happens, [credentials]
# where credentials or secrets are read, looked up or kept out of output, and
# [error handling] where input is checked or errors are caught and explained.

DEFAULT_ENV_PATH = Path(__file__).resolve().parent / ".env"
REQUEST_TIMEOUT_SECONDS = 30

# SDK 3.1.6.0.0 has no 2.3.7.10 model; 2.3.7.9 is the nearest earlier one.
SDK_API_VERSION = "2.3.7.9"

# Discovery settings taken from the supplied request body. The discovery type
# and IP list are chosen per job by address_list() below.
PROTOCOL_ORDER = "ssh"
TIMEOUT_SECONDS = 5
RETRY_COUNT = 3

_PROFILE_NAME = re.compile(r"^[A-Za-z0-9_]+$")


class ConfigError(RuntimeError):
    """Invalid or missing configuration or input data."""


class JobSubmissionError(RuntimeError):
    """A discovery job failed; earlier jobs may already have been submitted."""


# [credentials] A profile is one named set of credentials (IDs or descriptions) for
# devices that share the same logins. One discovery job is submitted per profile.
@dataclass(frozen=True)
class CredentialProfile:
    name: str
    cli_ids: tuple[str, ...]
    snmpv2_ro_ids: tuple[str, ...]

    @property
    def credential_ids(self) -> list[str]:
        return list(self.cli_ids + self.snmpv2_ro_ids)


# [credentials] The API login and profiles come from .env or the environment, never hard-coded.
@dataclass(frozen=True)
class Settings:
    base_url: str
    username: str
    password: str = field(repr=False)
    ca_bundle: str | None
    profiles: Mapping[str, CredentialProfile]
    default_profile: str
    verify_ssl: bool = True

    @property
    def verify(self) -> bool | str:
        if not self.verify_ssl:
            return False
        return self.ca_bundle or True


def apply_tls_policy(settings: Settings) -> None:
    """Warn once, loudly, when certificate verification is switched off."""
    if settings.verify_ssl:
        return
    print(
        "WARNING: TLS certificate verification is DISABLED "
        "(CATALYST_CENTER_VERIFY_SSL=false). Use this only in a lab.",
        file=sys.stderr,
    )
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


def _parse_bool(name: str, raw: str, default: bool) -> bool:
    text = raw.strip().lower()
    if not text:
        return default
    if text in ("true", "1", "yes", "on"):
        return True
    if text in ("false", "0", "no", "off"):
        return False
    raise ConfigError("{} must be true or false, not {!r}".format(name, raw))


def _split_ids(raw: str) -> tuple[str, ...]:
    ids: list[str] = []
    for item in raw.split(","):
        item = item.strip()
        if item and item not in ids:
            ids.append(item)
    return tuple(ids)


# [credentials] Reads each profile named in CREDENTIAL_PROFILES from the variables
# CRED_<NAME>_CLI_IDS and CRED_<NAME>_SNMPV2_RO_IDS.
# [error handling] A bad or incomplete profile stops the script before any API call.
def _load_profiles(get: Callable[[str], str]) -> dict[str, CredentialProfile]:
    profiles: dict[str, CredentialProfile] = {}
    for raw_name in get("CREDENTIAL_PROFILES").split(","):
        name = raw_name.strip()
        if not name:
            continue
        if not _PROFILE_NAME.match(name):
            raise ConfigError(
                "Invalid profile name {!r}: use letters, digits and underscores".format(
                    name
                )
            )
        key = name.lower()
        if key in profiles:
            raise ConfigError("Profile {!r} is listed more than once".format(name))
        prefix = "CRED_{}_".format(name.upper())
        cli_ids = _split_ids(get(prefix + "CLI_IDS"))
        snmp_ids = _split_ids(get(prefix + "SNMPV2_RO_IDS"))
        if not cli_ids or not snmp_ids:
            raise ConfigError(
                "Profile {!r} needs at least one value in both {}CLI_IDS and "
                "{}SNMPV2_RO_IDS".format(name, prefix, prefix)
            )
        profiles[key] = CredentialProfile(key, cli_ids, snmp_ids)

    if not profiles:
        raise ConfigError("CREDENTIAL_PROFILES does not list any profile")
    return profiles


def load_settings(
    environ: Mapping[str, str] | None = None,
    dotenv_path: Path | str = DEFAULT_ENV_PATH,
    require_profiles: bool = True,
) -> Settings:
    """Read settings from the environment, loading the .env file first.

    Variables already present in the real environment win over .env values.
    Scripts that only talk to the API (not submit jobs) can skip the profiles.
    """
    # [credentials] Load .env into the environment. Variables that are already set in
    # the real environment win over the file.
    if environ is None:
        if not Path(dotenv_path).is_file():
            raise ConfigError(
                "No .env file at {}. Copy .env.example to .env and fill it in.".format(
                    dotenv_path
                )
            )
        load_dotenv(dotenv_path, override=False)
        environ = os.environ

    def get(name: str) -> str:
        return environ.get(name, "").strip()

    # [error handling] Check everything up front, so a mistake in .env is reported
    # before any API call is made.
    missing = [
        name
        for name in (
            "CATALYST_CENTER_BASE_URL",
            "CATALYST_CENTER_USERNAME",
            "CATALYST_CENTER_PASSWORD",
            *(("CREDENTIAL_PROFILES",) if require_profiles else ()),
        )
        if not environ.get(name, "").strip()
    ]
    if missing:
        raise ConfigError("Missing required setting(s): " + ", ".join(missing))

    base_url = get("CATALYST_CENTER_BASE_URL").rstrip("/")
    if not base_url.lower().startswith("https://") or "/" in base_url[8:]:
        raise ConfigError(
            "CATALYST_CENTER_BASE_URL must be an HTTPS origin such as "
            "https://catalyst-center.example.com"
        )

    ca_bundle = get("CATALYST_CENTER_CA_BUNDLE") or None
    if ca_bundle is not None and not Path(ca_bundle).is_file():
        raise ConfigError("CATALYST_CENTER_CA_BUNDLE is not a readable file")

    # Lab only: certificate checking can be switched off, but never together with a CA bundle.
    verify_ssl = _parse_bool(
        "CATALYST_CENTER_VERIFY_SSL", get("CATALYST_CENTER_VERIFY_SSL"), True
    )
    if not verify_ssl and ca_bundle is not None:
        raise ConfigError(
            "Set either CATALYST_CENTER_CA_BUNDLE or "
            "CATALYST_CENTER_VERIFY_SSL=false, not both"
        )

    profiles = _load_profiles(get) if require_profiles else {}

    return Settings(
        base_url=base_url,
        username=get("CATALYST_CENTER_USERNAME"),
        password=environ["CATALYST_CENTER_PASSWORD"],
        ca_bundle=ca_bundle,
        profiles=profiles,
        default_profile=next(iter(profiles), ""),
        verify_ssl=verify_ssl,
    )


def normalize_ip(value: str) -> str:
    text = value.strip()
    try:
        address = ipaddress.ip_address(text)
    except ValueError:
        raise ConfigError("Invalid IP address: {!r}".format(value)) from None
    if address.version != 4:
        raise ConfigError("Only IPv4 addresses are supported: {!r}".format(value))
    return str(address)


def group_by_profile(
    devices: Iterable[tuple[str, str | None]], settings: Settings
) -> "OrderedDict[str, list[str]]":
    """Group (ip, profile) pairs by profile, preserving input order.

    An empty or missing profile means the default (first) profile.
    """
    groups: OrderedDict[str, list[str]] = OrderedDict()
    seen: set[str] = set()
    for raw_ip, raw_profile in devices:
        # [error handling] Reject bad IPs, repeated IPs and unknown profiles now, not
        # halfway through a run.
        ip = normalize_ip(raw_ip)
        if ip in seen:
            raise ConfigError("IP address listed more than once: {}".format(ip))
        seen.add(ip)

        profile = (raw_profile or "").strip().lower() or settings.default_profile
        if profile not in settings.profiles:
            raise ConfigError(
                "Unknown credential profile {!r} for {} (defined: {})".format(
                    raw_profile, ip, ", ".join(settings.profiles)
                )
            )
        groups.setdefault(profile, []).append(ip)

    if not groups:
        raise ConfigError("No devices to discover")
    return groups


def collapse_ranges(ip_addresses: Sequence[str]) -> list[tuple[str, str]]:
    """Merge IPv4 addresses into contiguous (start, end) pairs.

    Only addresses that were listed are covered, never the gaps between them.
    """
    numbers = sorted({int(ipaddress.IPv4Address(ip)) for ip in ip_addresses})
    pairs: list[list[int]] = []
    for number in numbers:
        if pairs and number == pairs[-1][1] + 1:
            pairs[-1][1] = number
        else:
            pairs.append([number, number])
    return [(str(ipaddress.IPv4Address(a)), str(ipaddress.IPv4Address(b))) for a, b in pairs]


def address_list(ip_addresses: Sequence[str]) -> tuple[str, str]:
    """Return (discoveryType, ipAddressList) in the format Catalyst Center expects.

    One IP is 'Single' ("10.0.0.1"); one contiguous block is 'Range' with a
    single start-end pair ("10.0.0.1-10.0.0.4"); anything else is 'Multi Range'
    with comma-separated start-end pairs. A comma-separated list of bare IPs
    is rejected by 'Range' ("IPAddress for Range Discovery is invalid").
    """
    pairs = collapse_ranges(ip_addresses)
    if len(pairs) == 1:
        start, end = pairs[0]
        if start == end:
            return "Single", start
        return "Range", "{}-{}".format(start, end)
    return "Multi Range", ",".join("{}-{}".format(start, end) for start, end in pairs)


def build_payload(
    name: str,
    ip_addresses: Sequence[str],
    profile: CredentialProfile,
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
        "globalCredentialIdList": profile.credential_ids,
    }
    # Optional NETCONF: the controller also tries this port. The SDK documents the field as
    # a string and says it needs valid SSH credentials, which every profile already has.
    if netconf_port is not None:
        payload["netconfPort"] = str(netconf_port)
    return payload


def build_jobs(
    groups: Mapping[str, Sequence[str]],
    settings: Settings,
    name_prefix: str,
    netconf_port: int | None = None,
) -> list[tuple[str, dict[str, Any]]]:
    prefix = name_prefix.strip()
    if not prefix:
        raise ConfigError("The discovery name prefix must not be blank")
    # One job per profile, so each group of devices is only tried with its own credentials.
    return [
        (
            profile,
            build_payload(
                "{}-{}".format(prefix, profile), ips, settings.profiles[profile], netconf_port
            ),
        )
        for profile, ips in groups.items()
    ]


def run_jobs(
    jobs: Sequence[tuple[str, dict[str, Any]]],
    submit: Callable[[dict[str, Any]], Any],
) -> list[tuple[str, Any]]:
    """Submit one job per profile; stop at the first failure."""
    results: list[tuple[str, Any]] = []
    # [main task] Submit the jobs one by one. [error handling] On the first failure,
    # stop and say which jobs were already submitted, so nothing is silently half done.
    for profile, payload in jobs:
        try:
            results.append((profile, submit(payload)))
        except Exception as exc:
            done = ", ".join(p for p, _ in results) or "none"
            raise JobSubmissionError(
                "Discovery for profile {!r} failed ({}: {}). "
                "Already submitted: {}.".format(profile, type(exc).__name__, exc, done)
            ) from exc
    return results


# ---------------------------------------------------------------------------
# Credential lookup: .env values may be a credential ID or its description.
# ---------------------------------------------------------------------------

CLI_SUBTYPE = "CLI"
SNMPV2_RO_SUBTYPE = "SNMPV2_READ_COMMUNITY"
_KIND_LABEL = {CLI_SUBTYPE: "CLI", SNMPV2_RO_SUBTYPE: "SNMPv2 read-only"}
_MAX_LISTED = 25


def response_items(response: Any) -> list[Mapping[str, Any]]:
    """Return the list stored under a Catalyst Center response's 'response' key."""
    body = response.get("response") if isinstance(response, Mapping) else None
    if isinstance(body, list):
        return [item for item in body if isinstance(item, Mapping)]
    return []


def _credential_id(item: Mapping[str, Any]) -> str:
    return str(item.get("id") or item.get("instanceUuid") or "").strip()


def _describe(item: Mapping[str, Any]) -> str:
    # Only identifying fields are ever shown; secret fields are never printed.
    name = item.get("description") or item.get("username") or "(no description)"
    return "{} ({})".format(name, _credential_id(item) or "no id")


def match_credential(ref: str, items: Sequence[Mapping[str, Any]], kind: str) -> str:
    """Resolve an ID or description (CLI: also username) to a credential ID."""
    wanted = ref.strip()
    label = _KIND_LABEL.get(kind, kind)

    def by_id(item: Mapping[str, Any]) -> bool:
        return wanted in (item.get("id"), item.get("instanceUuid"))

    def by_text(name: str) -> Callable[[Mapping[str, Any]], bool]:
        return lambda item: str(item.get(name) or "").strip().lower() == wanted.lower()

    # [credentials] Try an exact ID first, then the description, then (CLI only) the username.
    stages = [("ID", by_id), ("description", by_text("description"))]
    if kind == CLI_SUBTYPE:
        stages.append(("username", by_text("username")))

    # [error handling] Several matches, or none, raise an error that lists what exists
    # (names and IDs only, never secrets).
    for stage, test in stages:
        found = [item for item in items if test(item)]
        if len(found) > 1:
            raise ConfigError(
                "{} credential {!r} is ambiguous ({} credentials share that {}): {}. "
                "Use the credential ID instead.".format(
                    label, ref, len(found), stage, ", ".join(_describe(i) for i in found)
                )
            )
        if found:
            credential_id = _credential_id(found[0])
            if not credential_id:
                raise ConfigError(
                    "{} credential {!r} was found but has no ID".format(label, ref)
                )
            return credential_id

    if items:
        shown = ", ".join(_describe(i) for i in items[:_MAX_LISTED])
        more = len(items) - _MAX_LISTED
        available = "Available: {}{}".format(shown, " and {} more".format(more) if more > 0 else "")
    else:
        available = "The controller has no {} credentials.".format(label)
    raise ConfigError(
        "No {} credential matches {!r}. Use a credential ID or description. {}".format(
            label, ref, available
        )
    )


def resolve_settings(
    settings: Settings,
    profile_names: Iterable[str],
    list_credentials: Callable[[str], Any],
) -> Settings:
    """Replace names/descriptions in the used profiles with real credential IDs.

    list_credentials(subtype) must return the controller's
    GET /dna/intent/api/v1/global-credential response for that subtype.
    """
    # [credentials] Ask the controller for its credentials (once per type) and swap each
    # value from .env for the real credential ID, which is what a discovery job needs.
    cache: dict[str, list[Mapping[str, Any]]] = {}

    def items(subtype: str) -> list[Mapping[str, Any]]:
        if subtype not in cache:
            cache[subtype] = response_items(list_credentials(subtype))
        return cache[subtype]

    resolved = dict(settings.profiles)
    for name in profile_names:
        profile = settings.profiles[name]
        try:
            cli = tuple(
                dict.fromkeys(
                    match_credential(ref, items(CLI_SUBTYPE), CLI_SUBTYPE)
                    for ref in profile.cli_ids
                )
            )
            snmp = tuple(
                dict.fromkeys(
                    match_credential(ref, items(SNMPV2_RO_SUBTYPE), SNMPV2_RO_SUBTYPE)
                    for ref in profile.snmpv2_ro_ids
                )
            )
        except ConfigError as exc:
            # [error handling] Name the profile, so the bad .env entry is easy to find.
            raise ConfigError("Profile {!r}: {}".format(name, exc)) from exc
        resolved[name] = CredentialProfile(name, cli, snmp)
    return replace(settings, profiles=resolved)


# ---------------------------------------------------------------------------
# Verification: discovery is asynchronous, so follow the task and the discovery.
# ---------------------------------------------------------------------------

POLL_INTERVAL_SECONDS = 5
DEFAULT_WAIT_TIMEOUT_SECONDS = 300
# Discovery states that mean "not finished yet". These values were not confirmed on a
# live controller; run with --raw to see what yours returns.
_RUNNING_CONDITIONS = {"in progress", "inprogress", "in-progress", "queued", "pending", "starting", "running"}
_SENSITIVE_KEY_PARTS = ("password", "passphrase", "community", "secret", "token")


@dataclass(frozen=True)
class ApiGetters:
    """The read calls verification needs; SDK and requests both provide them."""

    get_task: Callable[[str], Any]
    get_discovery: Callable[[str], Any]
    get_jobs: Callable[[str], Any]


@dataclass
class JobReport:
    label: str
    ok: bool
    lines: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)


NETCONF_DEFAULT_PORT = 830


def netconf_port_number(text: str) -> int:
    """argparse type for --netconf-port: a TCP port from 1 to 65535."""
    try:
        port = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError("{!r} is not a port number".format(text)) from None
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("port must be between 1 and 65535, not {}".format(port))
    return port


def add_netconf_arguments(parser: Any) -> None:
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


def netconf_port_from_args(args: Any) -> int | None:
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


def add_wait_arguments(parser: Any) -> None:
    parser.add_argument(
        "--no-wait",
        action="store_true",
        help="submit only; do not wait for or check the discovery result",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=DEFAULT_WAIT_TIMEOUT_SECONDS,
        help="seconds to wait per job for results (default: %(default)s)",
    )
    parser.add_argument(
        "--raw",
        action="store_true",
        help="also print the controller's raw task/discovery responses (secrets redacted)",
    )


# [credentials] Replace secret-looking fields, so raw responses can be printed safely.
def redact(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            key: "***"
            if any(part in str(key).lower() for part in _SENSITIVE_KEY_PARTS)
            else redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value


# Catalyst Center wraps results in {"response": ...}. These helpers tolerate missing or
# unexpected fields (names not confirmed live), so a surprise never crashes a check.
def task_id_from_response(response: Any) -> str | None:
    body = response.get("response") if isinstance(response, Mapping) else None
    task_id = body.get("taskId") if isinstance(body, Mapping) else None
    return str(task_id) if task_id else None


def _body(response: Any) -> Mapping[str, Any]:
    body = response.get("response") if isinstance(response, Mapping) else None
    return body if isinstance(body, Mapping) else {}


def _task_done(response: Any) -> bool:
    body = _body(response)
    return bool(body.get("isError")) or bool(body.get("endTime"))


def _discovery_running(response: Any) -> bool:
    condition = str(_body(response).get("discoveryCondition") or "").strip().lower()
    return condition in _RUNNING_CONDITIONS


def _poll(
    fetch: Callable[[], Any],
    done: Callable[[Any], bool],
    deadline: float,
    interval: float,
    sleep: Callable[[float], None],
    clock: Callable[[], float],
) -> tuple[Any, bool]:
    # Check; if not finished, wait and check again until the deadline passes.
    while True:
        value = fetch()
        if done(value):
            return value, True
        remaining = deadline - clock()
        if remaining <= 0:
            return value, False
        sleep(min(interval, remaining))


def parse_task_result(body: Mapping[str, Any]) -> tuple[str, str]:
    """(discovery ID, job task ID) from a finished discovery task; either may be ''."""
    # The task's 'data' is a JSON string such as {"discoveryId": "581", "jobTaskId": "..."};
    # older results only carry the discovery ID in 'progress'.
    data = body.get("data")
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except ValueError:
            data = None
    if not isinstance(data, Mapping):
        data = {}
    discovery_id = str(data.get("discoveryId") or body.get("progress") or "").strip()
    return discovery_id, str(data.get("jobTaskId") or "").strip()


def _not_found(exc: Exception) -> bool:
    status = getattr(exc, "status_code", None) or getattr(getattr(exc, "response", None), "status_code", None)
    return status == 404


_IP_FIELDS = ("ipAddress", "managementIpAddress", "deviceIp")
_STATUS_FIELDS = ("pingStatus", "snmpStatus", "cliStatus", "netconfStatus")


def summarize_jobs(jobs: Sequence[Mapping[str, Any]], num_devices: int | None = None) -> tuple[list[str], bool]:
    """Report what the discovery returned: per-IP statuses if present, else the job status.

    A per-IP line is ok only when ping, SNMP and CLI all succeeded (and NETCONF, if it was
    tried). Catalyst Center 2.3.7.10 returns only a job record (jobStatus, times) from the
    /job call, with no per-device or per-credential results, so those are left to the user.
    """
    lines: list[str] = []
    all_ok = True
    if num_devices == 0:
        lines.append("- no devices were discovered")
        all_ok = False
    if not jobs:
        return lines + ["- no job result was returned"], False

    detailed = False
    for job in jobs:
        ip = next((str(job[name]) for name in _IP_FIELDS if job.get(name)), "")
        if not ip and not any(job.get(name) for name in _STATUS_FIELDS):
            # A job-level record: all it says is whether the discovery job succeeded.
            job_status = str(job.get("jobStatus") or "").strip()
            if job_status:
                lines.append("- discovery job status: {}".format(job_status))
                all_ok = all_ok and job_status.lower() == "success"
            else:
                names = ", ".join(sorted(str(key) for key in job)) or "none"
                lines.append("- unrecognised result (fields: {}); run with --raw".format(names))
                all_ok = False
            continue
        detailed = True
        statuses = {
            key: str(job.get(field_name) or "-")
            for key, field_name in (("ping", "pingStatus"), ("snmp", "snmpStatus"), ("cli", "cliStatus"))
        }
        ok = all(value.strip().lower() == "success" for value in statuses.values())
        # NETCONF is shown only when it was tried (it is NOT-PROVIDED otherwise); a failure needs attention.
        netconf = str(job.get("netconfStatus") or "").strip()
        if netconf.lower() not in ("", "not-provided", "not-validated"):
            statuses["netconf"] = netconf
            ok = ok and netconf.lower() == "success"
        all_ok = all_ok and ok
        detail = next(
            (
                str(job[name])
                for name in ("errorDescription", "errDescription", "errorCode")
                if job.get(name)
            ),
            "",
        )
        lines.append(
            "- {ip:<15} {state}  {protocols}{detail}".format(
                ip=ip or "?",
                state="OK       " if ok else "ATTENTION",
                protocols=" ".join("{}={}".format(key, value) for key, value in statuses.items()),
                detail=": " + detail if detail else "",
            )
        )
    if not detailed:
        lines.append(
            "- note: per-device and per-credential results are not available from Catalyst Center here; "
            "to confirm which protocols (SSH, SNMP, NETCONF) worked, check the discovery in the "
            "Catalyst Center GUI or the inventory (standalone/list_devices.py)"
        )
    return lines, all_ok


def verify_job(
    label: str,
    getters: ApiGetters,
    *,
    task_id: str | None = None,
    discovery_id: str | None = None,
    timeout: float = DEFAULT_WAIT_TIMEOUT_SECONDS,
    interval: float = POLL_INTERVAL_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> JobReport:
    """Follow a job: task result, then discovery completion, then per-IP results."""
    # [main task] Follow a submitted job in three steps: the task, then the discovery,
    # then each device.
    report = JobReport(label, False)
    deadline = clock() + timeout
    try:
        # Step 1: wait for the task. [error handling] A failed task is reported with its
        # error code and reason.
        if discovery_id is None:
            if not task_id:
                raise ValueError("a task ID or a discovery ID is required")
            task, finished = _poll(
                lambda: getters.get_task(task_id), _task_done, deadline, interval, sleep, clock
            )
            report.raw["task"] = task
            body = _body(task)
            if not finished:
                report.lines.append(
                    "Task {} is still running after {}s. Check later with: "
                    "python check_discovery.py {}".format(task_id, int(timeout), task_id)
                )
                return report
            if body.get("isError"):
                reason = body.get("failureReason") or body.get("progress") or "no reason given"
                code = body.get("errorCode")
                report.lines.append(
                    "Task {} failed: {}{}".format(
                        task_id, "[{}] ".format(code) if code else "", reason
                    )
                )
                return report
            discovery_id = parse_task_result(body)[0] or None
            if not discovery_id:
                report.lines.append(
                    "Task {} completed but its result has no discovery ID, so the devices "
                    "cannot be checked (use --raw to inspect the response).".format(task_id)
                )
                return report
            report.lines.append("Task {} completed".format(task_id))

        # Step 2: the finished task's result is the discovery ID; wait until that
        # discovery stops running. [error handling] A 404 means it no longer exists.
        try:
            discovery, finished = _poll(
                lambda: getters.get_discovery(discovery_id),
                lambda response: not _discovery_running(response),
                deadline,
                interval,
                sleep,
                clock,
            )
        except Exception as exc:
            if not _not_found(exc):
                raise
            report.lines.append(
                "Discovery {} was not found (it may have been deleted in Catalyst Center)".format(discovery_id)
            )
            return report
        report.raw["discovery"] = discovery
        info = _body(discovery)
        header = 'Discovery {} "{}": {}, {} device(s)'.format(
            discovery_id,
            info.get("name") or "?",
            info.get("discoveryCondition") or "unknown state",
            info.get("numDevices") if info.get("numDevices") is not None else "?",
        )
        if not finished:
            report.lines.append(
                header + ". Still running after {}s; check later with: "
                "python check_discovery.py --discovery-id {}".format(int(timeout), discovery_id)
            )
            return report

        # Step 3: the discovery job result (GET /discovery/{id}/job). The job is OK when it
        # succeeded and devices were found; per-IP statuses are shown only if the controller
        # returns them.
        jobs = getters.get_jobs(discovery_id)
        report.raw["jobs"] = jobs
        try:
            num_devices = int(info.get("numDevices"))
        except (TypeError, ValueError):
            num_devices = None
        job_lines, report.ok = summarize_jobs(response_items(jobs), num_devices)
        report.lines.append(header)
        report.lines.extend(job_lines)
    # [error handling] Turn any failure into a PROBLEM line, so the other jobs are still
    # reported.
    except Exception as exc:  # keep reporting the other jobs
        report.ok = False
        report.lines.append("Verification failed: {}: {}".format(type(exc).__name__, exc))
    return report


def verify_submitted(
    results: Sequence[tuple[str, Any]],
    getters: ApiGetters,
    timeout: float = DEFAULT_WAIT_TIMEOUT_SECONDS,
) -> list[JobReport]:
    reports: list[JobReport] = []
    # [main task] Check every submitted job. [error handling] A response without a task
    # ID is reported as a problem, not skipped.
    for profile, response in results:
        task_id = task_id_from_response(response)
        if task_id is None:
            reports.append(
                JobReport(
                    profile,
                    False,
                    ["The submission response has no task ID: {}".format(json.dumps(redact(response), default=str))],
                )
            )
            continue
        print(
            "Checking {} (task {}, waiting up to {}s)...".format(profile, task_id, int(timeout)),
            file=sys.stderr,
        )
        reports.append(verify_job(profile, getters, task_id=task_id, timeout=timeout))
    return reports


def print_reports(reports: Sequence[JobReport], raw: bool = False) -> int:
    """Print every report; return 0 when all are OK, otherwise 1."""
    for report in reports:
        print("[{}] {}".format(report.label, "OK" if report.ok else "PROBLEM"))
        for line in report.lines:
            print("  " + line)
        if raw:
            print(json.dumps(redact(report.raw), indent=2, default=str))
    return 0 if all(report.ok for report in reports) else 1


# ---------------------------------------------------------------------------
# Paging, the local site cache, and assigning devices to a site.
# ---------------------------------------------------------------------------

DEFAULT_PAGE_SIZE = 500  # the controller's limit per request
MAX_PAGES = 1000  # safety net against a loop that never ends
ASSIGN_BATCH_SIZE = 100  # the most devices one assign request accepts
INVENTORY_LOOKUP_CHUNK = 50  # IP addresses per inventory lookup
SITE_CACHE_VERSION = 1
SITE_CACHE_MAX_AGE_DAYS = 7
DEVICE_SITE_TYPES = ("building", "floor")  # devices can only go to these
ASSIGN_WAIT_INTERVAL_SECONDS = 10

# Where each device ends up. TO ASSIGN is the plan (dry run); the rest are results.
STATUS_TO_ASSIGN = "TO ASSIGN"
STATUS_TASK_DONE = "TASK DONE"
STATUS_ASSIGNED = "ASSIGNED"
STATUS_ALREADY = "ALREADY ASSIGNED"
STATUS_MISSING = "NOT IN INVENTORY"
STATUS_SUBMITTED = "SUBMITTED"
STATUS_PENDING = "PENDING"
STATUS_FAILED = "FAILED"
_OK_STATUSES = {STATUS_TO_ASSIGN, STATUS_ASSIGNED, STATUS_ALREADY, STATUS_SUBMITTED}

SITE_REFRESH_REMINDER = (
    "Reminder: site names are read from a local copy of the site list. If sites were created "
    "or renamed in Catalyst Center, run this script again with --refresh-sites."
)


# [error handling] The controller returns at most 500 records per request and the SDK does
# not page for you, so ask for page after page. Offsets start at 1.
def fetch_pages(
    fetch_page: Callable[[int, int], list[Mapping[str, Any]]],
    page_size: int = DEFAULT_PAGE_SIZE,
    max_pages: int = MAX_PAGES,
    on_page: Callable[[int, int], None] | None = None,
) -> list[Mapping[str, Any]]:
    """Collect every record: fetch_page(offset, limit) is called for offset 1, 1 + size, ...

    A page shorter than page_size is the last one. Stops with an error if the controller
    returns the same page twice (it ignored the offset) or there are too many pages.
    """
    items: list[Mapping[str, Any]] = []
    previous: list[Mapping[str, Any]] | None = None
    for page_number in range(1, max_pages + 1):
        offset = 1 + (page_number - 1) * page_size
        page = fetch_page(offset, page_size)
        if not page:
            return items
        if previous is not None and page == previous:
            raise ConfigError(
                "The controller returned the same page twice (offset {}), so paging does not "
                "work as expected. Stopping with {} records read.".format(offset, len(items))
            )
        items.extend(page)
        if on_page is not None:
            on_page(page_number, len(items))
        if len(page) < page_size:
            return items
        previous = page
    raise ConfigError(
        "More than {} pages of {} records. Narrow the search with a filter, or raise "
        "the page size.".format(max_pages, page_size)
    )


@dataclass(frozen=True)
class SiteApi:
    """The calls the assignment needs. The SDK scripts fill these in with SDK methods."""

    list_sites: Callable[[int, int], list[Mapping[str, Any]]]  # (offset, limit)
    list_unassigned: Callable[[int, int], list[Mapping[str, Any]]]  # (offset, limit)
    list_devices_by_ip: Callable[[Sequence[str], int, int], list[Mapping[str, Any]]]
    assign: Callable[[str, Sequence[str]], Any]  # (site_id, device_ids) -> task response
    get_task: Callable[[str], Any]


# --- Site names and the local site cache -----------------------------------


def normalize_site_name(text: str) -> str:
    """'Global / London/Building1/ ' -> 'global/london/building1' (for matching only)."""
    parts = [part.strip() for part in text.strip().split("/")]
    return "/".join(part for part in parts if part).lower()


def parse_sites(items: Iterable[Mapping[str, Any]]) -> list[dict[str, str]]:
    """Keep what the assignment needs from the controller's site records."""
    sites: list[dict[str, str]] = []
    for item in items:
        site_id = str(item.get("id") or "").strip()
        hierarchy = str(item.get("nameHierarchy") or item.get("siteNameHierarchy") or "").strip()
        if not site_id or not hierarchy:
            continue
        sites.append({
            "id": site_id,
            "name": str(item.get("name") or hierarchy.rsplit("/", 1)[-1]),
            "nameHierarchy": hierarchy,
            "type": str(item.get("type") or "").strip().lower(),
        })
    return sorted(sites, key=lambda site: site["nameHierarchy"].lower())


def site_type_counts(sites: Iterable[Mapping[str, str]]) -> str:
    counts: dict[str, int] = {}
    for site in sites:
        key = site.get("type") or "unknown"
        counts[key] = counts.get(key, 0) + 1
    return ", ".join("{} {}".format(counts[key], key) for key in sorted(counts)) or "none"


@dataclass(frozen=True)
class SiteCache:
    controller: str
    fetched_at: datetime
    sites: tuple[Mapping[str, str], ...]


# [main task] Read every site from the controller (paged) and keep it in a local file, so
# later runs do not need to ask for all sites again.
def refresh_site_cache(
    api: SiteApi,
    controller: str,
    path: Path,
    page_size: int = DEFAULT_PAGE_SIZE,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> SiteCache:
    sites = parse_sites(fetch_pages(api.list_sites, page_size))
    if not sites:
        raise ConfigError("The controller returned no sites, so the site list was not saved.")
    cache = SiteCache(controller, now(), tuple(sites))
    save_site_cache(path, cache)
    return cache


def save_site_cache(path: Path, cache: SiteCache) -> None:
    document = {
        "version": SITE_CACHE_VERSION,
        "controller": cache.controller,
        "fetched_at": cache.fetched_at.isoformat(),
        "sites": list(cache.sites),
    }
    # Write to a temporary file first, so a crash never leaves a half-written cache.
    temporary = path.with_name(path.name + ".tmp")
    try:
        temporary.write_text(json.dumps(document, indent=2), encoding="utf-8")
        os.replace(temporary, path)
    except OSError as exc:
        raise ConfigError("Could not save the site list to {}: {}".format(path, exc)) from exc


def load_site_cache(path: Path, controller: str) -> tuple[SiteCache | None, str]:
    """Return (cache, "") or (None, why-it-cannot-be-used). A missing file is not an error."""
    if not path.is_file():
        return None, "no site list has been saved yet"
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
        if document["version"] != SITE_CACHE_VERSION:
            return None, "the saved site list has an unknown format"
        if document["controller"] != controller:
            return None, "the saved site list is for another controller ({})".format(document["controller"])
        cache = SiteCache(
            controller, datetime.fromisoformat(document["fetched_at"]), tuple(document["sites"])
        )
    except (OSError, ValueError, KeyError, TypeError):
        return None, "the saved site list in {} cannot be read".format(path)
    return cache, ""


def cache_note(cache: SiteCache, now: datetime) -> str:
    age = (now - cache.fetched_at).days
    note = "Sites: using the list saved on {:%Y-%m-%d %H:%M} UTC ({} sites: {}).".format(
        cache.fetched_at.astimezone(timezone.utc), len(cache.sites), site_type_counts(cache.sites))
    if age >= SITE_CACHE_MAX_AGE_DAYS:
        note += " It is {} days old; consider --refresh-sites.".format(age)
    return note


# [error handling] A site name must match exactly one building or floor. When it does not,
# say why and suggest the closest names, and remind the user how to refresh the list.
def resolve_site(cache: SiteCache, name: str) -> Mapping[str, str]:
    wanted = normalize_site_name(name)
    matches = [site for site in cache.sites if normalize_site_name(site["nameHierarchy"]) == wanted]
    if not matches:
        usable = [s["nameHierarchy"] for s in cache.sites if s.get("type") in DEVICE_SITE_TYPES]
        close = difflib.get_close_matches(name.strip(), usable, n=3, cutoff=0.6)
        raise ConfigError(
            "Site {!r} is not in the saved site list ({:%Y-%m-%d %H:%M} UTC).{} If the site was "
            "created or renamed recently, run again with --refresh-sites.".format(
                name, cache.fetched_at.astimezone(timezone.utc),
                " Closest names: {}.".format("; ".join(close)) if close else "")
        )
    if len(matches) > 1:
        raise ConfigError(
            "Site {!r} matches {} sites ({}). Use the full name hierarchy.".format(
                name, len(matches), "; ".join(s["nameHierarchy"] for s in matches))
        )
    site = matches[0]
    # Only reject when the type is known: if the controller sent none, let it decide.
    if site.get("type") and site["type"] not in DEVICE_SITE_TYPES:
        raise ConfigError(
            "Site {!r} is {} {}; devices can only be assigned to a building or a floor.".format(
                name, "an" if site["type"][0] in "aeiou" else "a", site["type"])
        )
    return site


def resolve_sites(cache: SiteCache, names: Iterable[str]) -> dict[str, Mapping[str, str]]:
    """Resolve every distinct site name, reporting all the problems at once."""
    resolved: dict[str, Mapping[str, str]] = {}
    problems: list[str] = []
    for name in dict.fromkeys(names):
        try:
            resolved[name] = resolve_site(cache, name)
        except ConfigError as exc:
            problems.append("- {}".format(exc))
    if problems:
        raise ConfigError("Some sites could not be resolved:\n" + "\n".join(problems))
    return resolved


# --- The CSV with the devices --------------------------------------------------


def read_device_sites(csv_path: Path, default_site: str | None = None) -> list[tuple[str, str]]:
    """(ip_address, site) pairs from the CSV. Rows with no site use default_site."""
    rows: list[tuple[str, str | None]] = []
    try:
        with open(csv_path, encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            columns = {(name or "").strip(): name for name in reader.fieldnames or []}
            if "ip_address" not in columns:
                raise ConfigError("CSV needs an 'ip_address' column in its header row")
            ip_column, site_column = columns["ip_address"], columns.get("site")
            for row in reader:
                ip = row.get(ip_column)
                if ip is None or not ip.strip():
                    raise ConfigError("CSV line {}: empty ip_address".format(reader.line_num))
                site = (row.get(site_column) or "").strip() if site_column else ""
                rows.append((normalize_ip(ip), site or default_site))
    except csv.Error as exc:
        raise ConfigError("Could not parse CSV: {}".format(exc)) from exc

    if not rows:
        raise ConfigError("CSV contains no devices")
    seen: set[str] = set()
    for ip, _ in rows:
        if ip in seen:
            raise ConfigError("IP address listed more than once: {}".format(ip))
        seen.add(ip)
    without_site = [ip for ip, site in rows if not site]
    if without_site:
        raise ConfigError(
            "No site for {} device(s): {}. Add a 'site' column to the CSV, or give --site.".format(
                len(without_site), ", ".join(without_site[:10]) + (" ..." if len(without_site) > 10 else ""))
        )
    return [(ip, str(site)) for ip, site in rows]


# --- Finding the devices in the inventory --------------------------------------


@dataclass
class AssignItem:
    ip: str
    site_name: str
    site_id: str = ""
    site_path: str = ""
    device_id: str = ""
    hostname: str = ""
    status: str = ""
    detail: str = ""


def _by_ip(devices: Iterable[Mapping[str, Any]]) -> dict[str, Mapping[str, Any]]:
    found: dict[str, Mapping[str, Any]] = {}
    for device in devices:
        try:
            found[normalize_ip(str(device.get("managementIpAddress") or ""))] = device
        except ConfigError:
            continue  # a record without a usable management IP cannot be matched
    return found


def lookup_inventory(api: SiteApi, ips: Sequence[str]) -> dict[str, Mapping[str, Any]]:
    """Inventory devices for these IPs, a few IPs per request (any device, assigned or not)."""
    found: dict[str, Mapping[str, Any]] = {}
    for start in range(0, len(ips), INVENTORY_LOOKUP_CHUNK):
        chunk = list(ips[start:start + INVENTORY_LOOKUP_CHUNK])
        found.update(_by_ip(fetch_pages(lambda offset, limit: api.list_devices_by_ip(chunk, offset, limit))))
    return found


# [main task] Find each CSV device cheaply: read the 'not assigned to a site' list once and
# match the IPs here, instead of asking about every IP. IPs missing from that list are looked
# up in the inventory, to tell "already has a site" from "not in the inventory (yet)".
def locate_devices(
    api: SiteApi,
    ips: Sequence[str],
    wait_seconds: float = 0,
    interval: float = ASSIGN_WAIT_INTERVAL_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> dict[str, tuple[str, Mapping[str, Any]]]:
    deadline = clock() + wait_seconds
    while True:
        unassigned = _by_ip(fetch_pages(api.list_unassigned))
        others = [ip for ip in ips if ip not in unassigned]
        inventory = lookup_inventory(api, others) if others else {}
        located: dict[str, tuple[str, Mapping[str, Any]]] = {}
        for ip in ips:
            if ip in unassigned:
                located[ip] = (STATUS_TO_ASSIGN, unassigned[ip])
            elif ip in inventory:
                located[ip] = (STATUS_ALREADY, inventory[ip])
            else:
                located[ip] = (STATUS_MISSING, {})
        # --wait-for-devices: right after a discovery, devices can take a while to appear.
        if all(status != STATUS_MISSING for status, _ in located.values()) or clock() >= deadline:
            return located
        sleep(min(interval, max(deadline - clock(), 0)))


def plan_items(
    rows: Sequence[tuple[str, str]],
    sites: Mapping[str, Mapping[str, str]],
    located: Mapping[str, tuple[str, Mapping[str, Any]]],
) -> list[AssignItem]:
    items: list[AssignItem] = []
    for ip, site_name in rows:
        status, device = located[ip]
        site = sites[site_name]
        item = AssignItem(
            ip=ip, site_name=site_name, site_id=site["id"], site_path=site["nameHierarchy"],
            device_id=str(device.get("id") or ""), hostname=str(device.get("hostname") or ""),
            status=status,
        )
        if status == STATUS_ALREADY:
            item.detail = "already has a site; devices are never moved"
        elif status == STATUS_MISSING:
            item.detail = "not in the inventory (is the discovery finished? try --wait-for-devices)"
        elif not item.device_id:
            item.status, item.detail = STATUS_FAILED, "the controller record has no device ID"
        items.append(item)
    return items


# --- Assigning ---------------------------------------------------------------


def chunked(values: Sequence[Any], size: int) -> list[list[Any]]:
    return [list(values[start:start + size]) for start in range(0, len(values), size)]


def wait_for_task(
    api: SiteApi,
    task_id: str,
    timeout: float,
    interval: float = POLL_INTERVAL_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> tuple[str, str]:
    """Wait for a task. Returns ("done" | "failed" | "pending", message)."""
    task, finished = _poll(lambda: api.get_task(task_id), _task_done, clock() + timeout, interval, sleep, clock)
    body = _body(task)
    if not finished:
        return "pending", "task {} is still running after {}s".format(task_id, int(timeout))
    if body.get("isError"):
        code = body.get("errorCode")
        reason = body.get("failureReason") or body.get("progress") or "no reason given"
        return "failed", "{}{}".format("[{}] ".format(code) if code else "", reason)
    return "done", ""


# [main task] Assign the devices: one request per site, at most 100 devices each, then wait
# for each request's task. A failed request does not stop the others.
def run_assignments(
    api: SiteApi,
    items: Sequence[AssignItem],
    timeout: float,
    no_wait: bool = False,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> None:
    by_site: dict[str, list[AssignItem]] = {}
    for item in items:
        if item.status == STATUS_TO_ASSIGN:
            by_site.setdefault(item.site_id, []).append(item)
    for site_id, group in by_site.items():
        for chunk in chunked(group, ASSIGN_BATCH_SIZE):
            # [error handling] Whatever goes wrong with this request is shown on its devices.
            try:
                response = api.assign(site_id, [item.device_id for item in chunk])
                task_id = task_id_from_response(response)
                if not task_id:
                    raise ConfigError("the response has no task ID: {}".format(
                        json.dumps(redact(response), default=str)))
                if no_wait:
                    state, message = "submitted", "task {}".format(task_id)
                else:
                    state, message = wait_for_task(api, task_id, timeout, sleep=sleep, clock=clock)
            except Exception as exc:
                state, message = "failed", "{}: {}".format(type(exc).__name__, exc)
            status = {"done": STATUS_TASK_DONE, "submitted": STATUS_SUBMITTED,
                      "pending": STATUS_PENDING, "failed": STATUS_FAILED}[state]
            for item in chunk:
                item.status, item.detail = status, message


# Success is only reported if the devices really left the 'not assigned to a site' list.
def verify_assignments(api: SiteApi, items: Sequence[AssignItem]) -> None:
    waiting = [item for item in items if item.status == STATUS_TASK_DONE]
    if not waiting:
        return
    still_unassigned = {str(device.get("id")) for device in fetch_pages(api.list_unassigned)}
    for item in waiting:
        if item.device_id in still_unassigned:
            item.status = STATUS_FAILED
            item.detail = "the task finished, but the device is still not assigned to a site"
        else:
            item.status, item.detail = STATUS_ASSIGNED, ""


def assignment_report(items: Sequence[AssignItem]) -> tuple[list[str], bool]:
    """One line per device, a summary line, and whether everything is fine."""
    lines = []
    for item in items:
        lines.append("- {ip:<15} {host:<20} {site}  {status}{detail}".format(
            ip=item.ip, host=item.hostname or "-", site=item.site_path or item.site_name,
            status=item.status, detail=": " + item.detail if item.detail else ""))
    counts: dict[str, int] = {}
    for item in items:
        counts[item.status] = counts.get(item.status, 0) + 1
    summary = ", ".join("{}: {}".format(status, counts[status]) for status in sorted(counts))
    return lines + ["{} device(s). {}".format(len(items), summary)], all(
        item.status in _OK_STATUSES for item in items)
