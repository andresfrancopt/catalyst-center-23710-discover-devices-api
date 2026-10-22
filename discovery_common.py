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

This module deliberately does not import the Catalyst Center SDK, so the
non-SDK reference script can reuse it.
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import sys
import time
from collections import OrderedDict
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

import urllib3
from dotenv import load_dotenv

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


@dataclass(frozen=True)
class CredentialProfile:
    name: str
    cli_ids: tuple[str, ...]
    snmpv2_ro_ids: tuple[str, ...]

    @property
    def credential_ids(self) -> list[str]:
        return list(self.cli_ids + self.snmpv2_ro_ids)


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
    name: str, ip_addresses: Sequence[str], profile: CredentialProfile
) -> dict[str, Any]:
    discovery_type, ip_list = address_list(ip_addresses)
    return {
        "name": name,
        "discoveryType": discovery_type,
        "ipAddressList": ip_list,
        "protocolOrder": PROTOCOL_ORDER,
        "timeOut": TIMEOUT_SECONDS,
        "retryCount": RETRY_COUNT,
        "isAutoCdp": False,
        "globalCredentialIdList": profile.credential_ids,
    }


def build_jobs(
    groups: Mapping[str, Sequence[str]], settings: Settings, name_prefix: str
) -> list[tuple[str, dict[str, Any]]]:
    prefix = name_prefix.strip()
    if not prefix:
        raise ConfigError("The discovery name prefix must not be blank")
    return [
        (profile, build_payload("{}-{}".format(prefix, profile), ips, settings.profiles[profile]))
        for profile, ips in groups.items()
    ]


def run_jobs(
    jobs: Sequence[tuple[str, dict[str, Any]]],
    submit: Callable[[dict[str, Any]], Any],
) -> list[tuple[str, Any]]:
    """Submit one job per profile; stop at the first failure."""
    results: list[tuple[str, Any]] = []
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

    stages = [("ID", by_id), ("description", by_text("description"))]
    if kind == CLI_SUBTYPE:
        stages.append(("username", by_text("username")))

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
            raise ConfigError("Profile {!r}: {}".format(name, exc)) from exc
        resolved[name] = CredentialProfile(name, cli, snmp)
    return replace(settings, profiles=resolved)


# ---------------------------------------------------------------------------
# Verification: discovery is asynchronous, so follow the task and the discovery.
# ---------------------------------------------------------------------------

POLL_INTERVAL_SECONDS = 5
DEFAULT_WAIT_TIMEOUT_SECONDS = 300
_RUNNING_CONDITIONS = {"in progress", "inprogress", "in-progress", "queued", "pending", "starting", "running"}
_SENSITIVE_KEY_PARTS = ("password", "passphrase", "community", "secret", "token")


@dataclass(frozen=True)
class ApiGetters:
    """The three read calls verification needs; SDK and requests both provide them."""

    get_task: Callable[[str], Any]
    get_discovery: Callable[[str], Any]
    get_jobs: Callable[[str], Any]


@dataclass
class JobReport:
    label: str
    ok: bool
    lines: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)


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
    while True:
        value = fetch()
        if done(value):
            return value, True
        remaining = deadline - clock()
        if remaining <= 0:
            return value, False
        sleep(min(interval, remaining))


def summarize_jobs(jobs: Sequence[Mapping[str, Any]]) -> tuple[list[str], bool]:
    """One line per IP; ok only when ping, SNMP and CLI all succeeded."""
    if not jobs:
        return ["- no per-IP results were returned"], False

    lines: list[str] = []
    all_ok = True
    for job in jobs:
        statuses = {
            key: str(job.get(field_name) or "-")
            for key, field_name in (("ping", "pingStatus"), ("snmp", "snmpStatus"), ("cli", "cliStatus"))
        }
        ok = all(value.strip().lower() == "success" for value in statuses.values())
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
            "- {ip:<15} {state}  ping={ping} snmp={snmp} cli={cli}{detail}".format(
                ip=str(job.get("ipAddress") or "?"),
                state="OK       " if ok else "ATTENTION",
                detail=": " + detail if detail else "",
                **statuses,
            )
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
    report = JobReport(label, False)
    deadline = clock() + timeout
    try:
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
            discovery_id = str(body.get("progress") or "").strip() or None
            if not discovery_id:
                report.lines.append(
                    "Task {} completed but its result has no discovery ID, so the devices "
                    "cannot be checked (use --raw to inspect the response).".format(task_id)
                )
                return report
            report.lines.append("Task {} completed".format(task_id))

        discovery, finished = _poll(
            lambda: getters.get_discovery(discovery_id),
            lambda response: not _discovery_running(response),
            deadline,
            interval,
            sleep,
            clock,
        )
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

        jobs = getters.get_jobs(discovery_id)
        report.raw["jobs"] = jobs
        job_lines, report.ok = summarize_jobs(response_items(jobs))
        report.lines.append(header)
        report.lines.extend(job_lines)
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
