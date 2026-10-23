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

"""Standalone: check a Catalyst Center task by its ID.

This file needs only the Python packages catalystcentersdk, python-dotenv and
requests (see requirements.txt). It does not import any other script. It only
needs the API login (CATALYST_CENTER_BASE_URL, _USERNAME, _PASSWORD, and
optionally _VERIFY_SSL / _CA_BUNDLE) in a .env file next to this script or in
the folder you run it from.

    python check_task.py <task-id>                 # any task: running, failed or completed
    python check_task.py <task-id> --discovery     # also report the discovered devices
    python check_task.py --discovery-id <id>       # check a discovery directly
    python check_task.py <task-id> --timeout 0     # check once, do not wait
"""

from __future__ import annotations

import argparse
import json
import os
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
POLL_INTERVAL_SECONDS = 5
DEFAULT_WAIT_TIMEOUT_SECONDS = 300
# Discovery states that mean "not finished yet". These values were not confirmed on a
# live controller; run with --raw to see what yours returns.
_RUNNING_CONDITIONS = {"in progress", "inprogress", "in-progress", "queued", "pending", "starting", "running"}
_SECRET_KEY_PARTS = ("password", "passphrase", "community", "secret", "token")
_MAX_TEXT = 300


class ScriptError(RuntimeError):
    """Invalid configuration or input."""


NO_ENV_HINT = (
    ". No .env file was found next to this script or in the current folder: "
    "copy .env.example to .env and fill it in, or set these in the environment."
)


# ===========================================================================
# 1. Settings from .env (API login only)
# ===========================================================================


# [credentials] Only the API login is needed here (no credential profiles). It comes from
# .env or the environment, never hard-coded.
@dataclass(frozen=True)
class Settings:
    base_url: str
    username: str
    password: str = field(repr=False)
    verify: bool | str  # True, False (lab only) or a CA bundle path


def find_env_file() -> Path | None:
    """A .env next to this script, else in the current folder. Optional."""
    for folder in (Path(__file__).resolve().parent, Path.cwd()):
        candidate = folder / ".env"
        if candidate.is_file():
            return candidate
    return None


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
    required = ["CATALYST_CENTER_BASE_URL", "CATALYST_CENTER_USERNAME", "CATALYST_CENTER_PASSWORD"]
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

    return Settings(
        base_url=base_url,
        username=get("CATALYST_CENTER_USERNAME"),
        password=environ["CATALYST_CENTER_PASSWORD"],
        verify=False if not verify_ssl else (ca_bundle or True),
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
# 2. Checking a task (and optionally the discovery it started)
# ===========================================================================


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


def _items(response: Any) -> list[Mapping[str, Any]]:
    body = response.get("response") if isinstance(response, Mapping) else None
    return [item for item in body if isinstance(item, Mapping)] if isinstance(body, list) else []


# Long task messages are shortened, so one odd response cannot flood the screen.
def _short(value: Any) -> str:
    text = str(value)
    return text if len(text) <= _MAX_TEXT else text[:_MAX_TEXT] + "..."


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
    """One line per IP; ok only when ping, SNMP and CLI all succeeded (and NETCONF, if it was tried)."""
    if not jobs:
        return ["- no per-IP results were returned"], False
    lines, all_ok = [], True
    for job in jobs:
        status = {key: str(job.get(name) or "-")
                  for key, name in (("ping", "pingStatus"), ("snmp", "snmpStatus"), ("cli", "cliStatus"))}
        ok = all(value.strip().lower() == "success" for value in status.values())
        # NETCONF is shown only when it was tried (it is NOT-PROVIDED otherwise); a failure needs attention.
        netconf = str(job.get("netconfStatus") or "").strip()
        if netconf.lower() not in ("", "not-provided", "not-validated"):
            status["netconf"] = netconf
            ok = ok and netconf.lower() == "success"
        all_ok = all_ok and ok
        detail = next((str(job[n]) for n in ("errorDescription", "errDescription", "errorCode") if job.get(n)), "")
        lines.append("- {ip:<15} {state}  {protocols}{detail}".format(
            ip=str(job.get("ipAddress") or "?"), state="OK       " if ok else "ATTENTION",
            protocols=" ".join("{}={}".format(key, value) for key, value in status.items()),
            detail=": " + detail if detail else ""))
    return lines, all_ok


def follow_discovery(api: CatalystCenterAPI, report: Report, discovery_id: str, deadline: float,
                     interval: float, timeout: float, sleep: Callable[[float], None],
                     clock: Callable[[], float], hint: str) -> None:
    """Wait for a discovery to finish, then add its per-IP results to the report."""
    # Wait for the discovery to stop running (GET /dna/intent/api/v1/discovery/{id}).
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
        report.lines.append(header + ". Still running after {}s; check later with: {}".format(int(timeout), hint))
        return
    # One result per device (ping, SNMP and CLI). The job is OK only if all of them
    # succeeded (GET /dna/intent/api/v1/discovery/{id}/job).
    jobs = api.discovery.get_list_of_discoveries_by_discovery_id(discovery_id)
    report.raw["jobs"] = jobs
    device_lines, report.ok = summarize_devices(_items(jobs))
    report.lines.append(header)
    report.lines.extend(device_lines)


def check(api: CatalystCenterAPI, task_id: str | None, discovery_id: str | None, follow: bool,
          timeout: float, interval: float = POLL_INTERVAL_SECONDS,
          sleep: Callable[[float], None] = time.sleep,
          clock: Callable[[], float] = time.monotonic) -> Report:
    # [main task] Check a task by ID: still running, failed or completed. With --discovery,
    # also follow it into the devices it discovered.
    report = Report("task {}".format(task_id) if task_id else "discovery {}".format(discovery_id))
    deadline = clock() + timeout
    try:
        # --discovery-id: check a discovery directly, without looking at a task.
        if discovery_id:
            follow_discovery(api, report, discovery_id, deadline, interval, timeout, sleep, clock,
                             "python check_task.py --discovery-id {}".format(discovery_id))
            return report

        # Wait for the task (GET /dna/intent/api/v1/task/{taskId}).
        # [error handling] A failed task is reported with its error code and reason.
        task, finished = _poll(
            lambda: api.task.get_task_by_id(task_id),
            lambda t: bool(_body(t).get("isError")) or bool(_body(t).get("endTime")),
            deadline, interval, sleep, clock)
        report.raw["task"] = task
        body = _body(task)
        if not finished:
            report.lines.append("Task {} is still running after {}s: {}".format(
                task_id, int(timeout), _short(body.get("progress") or "no progress text")))
            return report
        if body.get("isError"):
            code = body.get("errorCode")
            reason = body.get("failureReason") or body.get("progress") or "no reason given"
            report.lines.append("Task {} failed: {}{}".format(
                task_id, "[{}] ".format(code) if code else "", _short(reason)))
            return report

        report.ok = True
        report.lines.append("Task {} completed: {}".format(task_id, _short(body.get("progress") or "no result text")))
        # For a discovery task, the result of a finished task is the discovery ID.
        if follow:
            discovery_id = str(body.get("progress") or "").strip()
            if not discovery_id:
                report.ok = False
                report.lines.append("The task result has no discovery ID, so the devices cannot be checked "
                                    "(use --raw to inspect the response).")
                return report
            report.ok = False  # now decided by the devices
            follow_discovery(api, report, discovery_id, deadline, interval, timeout, sleep, clock,
                             "python check_task.py {} --discovery".format(task_id))
        else:
            report.lines.append("If this task started a discovery, add --discovery to see its devices.")
    # [error handling] A failed check becomes a PROBLEM line instead of a crash.
    except (catalystcentersdkException, requests.RequestException, OSError) as exc:
        report.ok = False
        report.lines.append("Check failed: {}: {}".format(type(exc).__name__, exc))
    return report


def print_report(report: Report, raw: bool) -> int:
    print("[{}] {}".format(report.label, "OK" if report.ok else "PROBLEM"))
    for line in report.lines:
        print("  " + line)
    if raw:
        print(json.dumps(redact(report.raw), indent=2, default=str))
    return 0 if report.ok else 1


# ===========================================================================
# 3. Main flow
# ===========================================================================


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("task_id", nargs="?", help="task ID, e.g. from a 'submitted, task ...' line")
    parser.add_argument("--discovery-id", help="check this discovery directly (skips the task step)")
    parser.add_argument("--discovery", action="store_true",
                        help="the task started a discovery: also report its devices")
    parser.add_argument("--timeout", type=int, default=DEFAULT_WAIT_TIMEOUT_SECONDS,
                        help="seconds to wait for results; 0 checks once (default: %(default)s)")
    parser.add_argument("--raw", action="store_true",
                        help="also print the controller's raw responses (secrets redacted)")
    args = parser.parse_args()
    # [error handling] Exactly one of a task ID or --discovery-id is required.
    if bool(args.task_id) == bool(args.discovery_id):
        parser.error("give exactly one of: a task ID, or --discovery-id")
    if args.discovery and args.discovery_id:
        parser.error("--discovery is for task IDs; --discovery-id already checks the discovery")

    try:
        # [credentials] Only the API login is read from .env here. Lab only: warn if
        # certificate checking is off.
        settings = load_settings()
        warn_if_insecure(settings)
        # [main task] Log in and check the task.
        report = check(connect(settings), args.task_id, args.discovery_id, args.discovery, args.timeout)
    # [error handling] Expected problems (bad settings, an SDK or network error) print one line
    # and exit 1. Anything unexpected still shows a full traceback.
    except (ScriptError, catalystcentersdkException, requests.RequestException, OSError) as exc:
        print("Error: {}".format(exc), file=sys.stderr)
        return 1
    # Exit code 0 only if the task completed (and, with --discovery, every device is OK).
    return print_report(report, args.raw)


if __name__ == "__main__":
    raise SystemExit(main())
