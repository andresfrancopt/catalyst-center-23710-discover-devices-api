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

"""Standalone: list, create and check global CLI and SNMPv2 read-only credentials.

This file needs only the Python packages catalystcentersdk, python-dotenv and
requests (see requirements.txt). It does not import any other script. It only
needs the API login (CATALYST_CENTER_BASE_URL, _USERNAME, _PASSWORD, and
optionally _VERIFY_SSL / _CA_BUNDLE) in a .env file next to this script or in
the folder you run it from.

    python manage_credentials.py list
    python manage_credentials.py check CSS
    python manage_credentials.py create-cli --description lab-cli --username admin
    python manage_credentials.py create-snmp-ro --description lab-ro

Secrets (CLI password, enable password, SNMP read community) are typed at a
hidden prompt. They are never accepted as command-line arguments and never
printed. For unattended runs, add --from-env and set NEW_CLI_PASSWORD,
NEW_CLI_ENABLE_PASSWORD (optional) and NEW_SNMP_RO_COMMUNITY in the environment.
Add --dry-run to see the request with secrets masked, without calling the API.

A credential with the same description is never created twice: if one exists,
its ID is reported and nothing is changed. After creating, the script lists the
credentials again to confirm the new one is there.
"""

from __future__ import annotations

import argparse
import getpass
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
from catalystcentersdk.exceptions import MalformedRequest, catalystcentersdkException
from dotenv import load_dotenv

# Reading guide: [main task] marks where the real work happens, [credentials]
# where credentials or secrets are read, looked up or kept out of output, and
# [error handling] where input is checked or errors are caught and explained.

# SDK 3.1.6.0.0 has no 2.3.7.10 model; 2.3.7.9 is the nearest earlier one.
SDK_API_VERSION = "2.3.7.9"
REQUEST_TIMEOUT_SECONDS = 30
DEFAULT_CONFIRM_TIMEOUT_SECONDS = 30
POLL_INTERVAL_SECONDS = 2
CLI_SUBTYPE = "CLI"
SNMPV2_RO_SUBTYPE = "SNMPV2_READ_COMMUNITY"
LABELS = {CLI_SUBTYPE: "CLI", SNMPV2_RO_SUBTYPE: "SNMPv2 read-only"}
_SECRET_KEY_PARTS = ("password", "passphrase", "community", "secret", "token")
# [credentials] Environment variables read by --from-env, as an alternative to the hidden prompt.
ENV_CLI_PASSWORD = "NEW_CLI_PASSWORD"
ENV_CLI_ENABLE_PASSWORD = "NEW_CLI_ENABLE_PASSWORD"
ENV_SNMP_COMMUNITY = "NEW_SNMP_RO_COMMUNITY"


class ScriptError(RuntimeError):
    """Invalid configuration or input, or a request that failed."""


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
# 2. Secrets: never printed, never taken from command-line arguments
# ===========================================================================


# [credentials] Replace secret-looking fields, so raw responses can be printed safely.
def redact(value: Any) -> Any:
    """Copy of a structure with secret-looking fields replaced by ***."""
    if isinstance(value, Mapping):
        return {
            key: "***" if any(part in str(key).lower() for part in _SECRET_KEY_PARTS) else redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value


# [credentials] Remove secret values from text. [error handling] SDK errors can echo the
# whole request, so error messages are scrubbed before they are printed.
def scrub(text: str, secrets: Sequence[str]) -> str:
    """Remove secret values from text, e.g. error messages that echo the request."""
    for secret in secrets:
        if secret:
            text = text.replace(secret, "***")
    return text


# [credentials] Secrets are typed at a hidden prompt, asked twice to catch typos, and never
# passed as command-line arguments (those end up in shell history and process lists).
def prompt_secret(label: str, *, required: bool, ask: Callable[[str], str] | None = None) -> str:
    """Ask for a secret twice. Optional secrets may be left blank."""
    ask = ask or getpass.getpass
    first = ask("{}{}: ".format(label, "" if required else " (leave blank for none)"))
    if not first:
        if required:
            raise ScriptError("{} must not be empty".format(label))
        return ""
    if ask("Repeat {}: ".format(label.lower())) != first:
        raise ScriptError("The two entries for {} do not match".format(label.lower()))
    return first


# [credentials] For unattended runs (--from-env): read a secret from an environment variable.
def env_secret(name: str, *, required: bool, environ: Mapping[str, str] | None = None) -> str:
    value = (os.environ if environ is None else environ).get(name, "")
    if required and not value:
        raise ScriptError("--from-env needs the environment variable {} to be set".format(name))
    return value


# ===========================================================================
# 3. Listing, finding and confirming credentials
# ===========================================================================


def list_credentials(api: CatalystCenterAPI, subtype: str) -> list[Mapping[str, Any]]:
    # GET /dna/intent/api/v1/global-credential?credentialSubType=...
    response = api.discovery.get_global_credentials(subtype)
    body = response.get("response") if isinstance(response, Mapping) else None
    return [item for item in body if isinstance(item, Mapping)] if isinstance(body, list) else []


def id_of(item: Mapping[str, Any]) -> str:
    return str(item.get("id") or item.get("instanceUuid") or "").strip()


def describe(item: Mapping[str, Any], subtype: str) -> str:
    """Identifying fields only; secret fields are never shown."""
    text = "{} (id {})".format(item.get("description") or "(no description)", id_of(item) or "?")
    if subtype == CLI_SUBTYPE and item.get("username"):
        text += ", username {}".format(item["username"])
    return text


def find_by_description(items: Sequence[Mapping[str, Any]], description: str) -> list[Mapping[str, Any]]:
    wanted = description.strip().lower()
    return [item for item in items if str(item.get("description") or "").strip().lower() == wanted]


def find_by_reference(items: Sequence[Mapping[str, Any]], ref: str, subtype: str) -> list[Mapping[str, Any]]:
    """Match an ID, then a description, then (CLI only) a username."""
    wanted = ref.strip()
    tests: list[Callable[[Mapping[str, Any]], bool]] = [
        lambda item: wanted in (item.get("id"), item.get("instanceUuid")),
        lambda item: str(item.get("description") or "").strip().lower() == wanted.lower(),
    ]
    if subtype == CLI_SUBTYPE:
        tests.append(lambda item: str(item.get("username") or "").strip().lower() == wanted.lower())
    for test in tests:
        found = [item for item in items if test(item)]
        if found:
            return found
    return []


def wait_for_credential(api: CatalystCenterAPI, subtype: str, description: str, timeout: float,
                        interval: float = POLL_INTERVAL_SECONDS,
                        sleep: Callable[[float], None] = time.sleep,
                        clock: Callable[[], float] = time.monotonic) -> Mapping[str, Any] | None:
    """List until the credential appears (creation can take a moment), or give up."""
    deadline = clock() + timeout
    while True:
        found = find_by_description(list_credentials(api, subtype), description)
        if found:
            return found[0]
        remaining = deadline - clock()
        if remaining <= 0:
            return None
        sleep(min(interval, remaining))


def wait_for_task(api: CatalystCenterAPI, response: Any, timeout: float,
                  interval: float = POLL_INTERVAL_SECONDS,
                  sleep: Callable[[float], None] = time.sleep,
                  clock: Callable[[], float] = time.monotonic) -> None:
    """If the create call returned a task, wait for it and fail on a task error."""
    body = response.get("response") if isinstance(response, Mapping) else None
    task_id = body.get("taskId") if isinstance(body, Mapping) else None
    if not task_id:
        return
    deadline = clock() + timeout
    while True:
        # GET /dna/intent/api/v1/task/{taskId}
        task = api.task.get_task_by_id(str(task_id))
        info = task.get("response") if isinstance(task, Mapping) else None
        info = info if isinstance(info, Mapping) else {}
        # [error handling] A task error becomes a clear message with the controller's error
        # code and reason.
        if info.get("isError"):
            code = info.get("errorCode")
            reason = info.get("failureReason") or info.get("progress") or "no reason given"
            raise ScriptError("The controller rejected the credential: {}{}".format(
                "[{}] ".format(code) if code else "", reason))
        if info.get("endTime"):
            return
        remaining = deadline - clock()
        if remaining <= 0:
            return  # the listing below is the final check
        sleep(min(interval, remaining))


# ===========================================================================
# 4. Commands
# ===========================================================================


def cmd_list(api: CatalystCenterAPI, args: argparse.Namespace) -> int:
    # [main task] Show every CLI and SNMPv2 read-only credential: name and ID, never secrets.
    for subtype in (CLI_SUBTYPE, SNMPV2_RO_SUBTYPE):
        items = list_credentials(api, subtype)
        print("{} credentials ({}):".format(LABELS[subtype], len(items)))
        for item in items:
            print("  - " + describe(item, subtype))
        if args.raw:
            print(json.dumps(redact(items), indent=2, default=str))
    return 0


def cmd_check(api: CatalystCenterAPI, args: argparse.Namespace) -> int:
    # [main task] Find a credential by ID, description or (CLI) username. Exit code 1 if none matches.
    subtypes = {"cli": [CLI_SUBTYPE], "snmp-ro": [SNMPV2_RO_SUBTYPE]}.get(args.type, [CLI_SUBTYPE, SNMPV2_RO_SUBTYPE])
    total = 0
    for subtype in subtypes:
        for item in find_by_reference(list_credentials(api, subtype), args.reference, subtype):
            total += 1
            print("Found {}: {}".format(LABELS[subtype], describe(item, subtype)))
    if total == 0:
        print("No {} credential matches {!r}. Run the 'list' command to see what exists.".format(
            " or ".join(LABELS[s] for s in subtypes), args.reference), file=sys.stderr)
        return 1
    return 0


def create_credential(api: CatalystCenterAPI, subtype: str, payload: dict[str, Any],
                      get_secrets: Callable[[], tuple[dict[str, Any], list[str]]], timeout: float) -> int:
    """Create a credential unless its description exists. Secrets are asked for only if needed."""
    description = payload["description"]
    label = LABELS[subtype]

    # Step 1: never create duplicates. If the description already exists, report it and stop.
    existing = find_by_description(list_credentials(api, subtype), description)
    if existing:
        print("{} credential {!r} already exists ({}); nothing was created or changed.".format(
            label, description, describe(existing[0], subtype)))
        return 0

    # Step 2 [credentials]: only now ask for the secrets, so nobody types a password just to
    # be told the credential already exists.
    secret_fields, secrets = get_secrets()
    payload = {**payload, **secret_fields}
    # Step 3 [main task]: create the credential. If the controller answers with a task, wait for it.
    try:
        # POST /dna/intent/api/v1/global-credential/cli  (or .../snmpv2-read-community)
        if subtype == CLI_SUBTYPE:
            response = api.discovery.create_cli_credentials(payload=[payload])
        else:
            response = api.discovery.create_snmp_read_community(payload=[payload])
        wait_for_task(api, response, timeout)
    except MalformedRequest as exc:
        # [error handling] The SDK's message echoes the whole request, secrets included, so
        # only its reason is shown.
        raise ScriptError("The SDK rejected the request: {}".format(
            scrub(str(exc).split("Reason:")[-1].strip(), secrets))) from None
    # [error handling] Any other SDK or network error is shown with secrets scrubbed out.
    except (catalystcentersdkException, requests.RequestException) as exc:
        raise ScriptError("Creating the {} credential failed ({}): {}".format(
            label, type(exc).__name__, scrub(str(exc), secrets))) from None

    # Step 4: confirm by listing again. Success is only reported if the credential is really there.
    created = wait_for_credential(api, subtype, description, timeout)
    if created is None:
        print("The controller accepted the request, but {} credential {!r} is not in the list after {}s. "
              "Check it in Catalyst Center or run: python manage_credentials.py check {}".format(
                  label, description, int(timeout), description), file=sys.stderr)
        return 1
    print("{} credential created and confirmed: {}".format(label, describe(created, subtype)))
    return 0


def cmd_create_cli(api: CatalystCenterAPI | None, args: argparse.Namespace) -> int:
    description, username = args.description.strip(), args.username.strip()
    # [error handling] Blank values are rejected before anything is sent.
    if not description or not username:
        raise ScriptError("--description and --username must not be empty")

    # The request body without secrets; the secrets are added later, only if needed.
    payload: dict[str, Any] = {"credentialType": "GLOBAL", "description": description, "username": username}
    if args.comments:
        payload["comments"] = args.comments
    # --dry-run: show the request with secrets masked. Needs no .env file and no connection.
    if args.dry_run:
        payload.update({"password": "<prompted>", "enablePassword": "<prompted, optional>"})
        print("# Dry run: nothing was sent. You would be asked for the password and an optional enable password.")
        print(json.dumps([redact(payload)], indent=2))
        return 0

    def get_secrets() -> tuple[dict[str, Any], list[str]]:
        # [credentials] Passwords come from the environment (--from-env) or hidden prompts.
        if args.from_env:
            password = env_secret(ENV_CLI_PASSWORD, required=True)
            enable = env_secret(ENV_CLI_ENABLE_PASSWORD, required=False)
        else:
            password = prompt_secret("CLI password", required=True)
            enable = prompt_secret("Enable password", required=False)
        fields = {"password": password}
        if enable:
            fields["enablePassword"] = enable
        return fields, [password, enable]

    assert api is not None
    return create_credential(api, CLI_SUBTYPE, payload, get_secrets, args.timeout)


def cmd_create_snmp_ro(api: CatalystCenterAPI | None, args: argparse.Namespace) -> int:
    description = args.description.strip()
    # [error handling] A blank description is rejected before anything is sent.
    if not description:
        raise ScriptError("--description must not be empty")

    # The request body without secrets; the secrets are added later, only if needed.
    payload: dict[str, Any] = {"credentialType": "GLOBAL", "description": description}
    if args.comments:
        payload["comments"] = args.comments
    # --dry-run: show the request with secrets masked. Needs no .env file and no connection.
    if args.dry_run:
        payload["readCommunity"] = "<prompted>"
        print("# Dry run: nothing was sent. You would be asked for the read community.")
        print(json.dumps([redact(payload)], indent=2))
        return 0

    def get_secrets() -> tuple[dict[str, Any], list[str]]:
        # [credentials] The read community comes from the environment (--from-env) or a hidden prompt.
        community = (env_secret(ENV_SNMP_COMMUNITY, required=True) if args.from_env
                     else prompt_secret("SNMP read community", required=True))
        return {"readCommunity": community}, [community]

    assert api is not None
    return create_credential(api, SNMPV2_RO_SUBTYPE, payload, get_secrets, args.timeout)


# ===========================================================================
# 5. Main flow
# ===========================================================================


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    # Four commands: list, check, create-cli and create-snmp-ro. Secrets are deliberately not
    # command-line options (see prompt_secret).
    commands = parser.add_subparsers(dest="command", required=True)

    listing = commands.add_parser("list", help="list CLI and SNMPv2 read-only credentials (no secrets shown)")
    listing.add_argument("--raw", action="store_true", help="also print the raw list (secrets redacted)")

    check = commands.add_parser("check", help="find a credential by ID, description or (CLI) username")
    check.add_argument("reference")
    check.add_argument("--type", choices=["cli", "snmp-ro", "any"], default="any")

    for name, help_text in (("create-cli", "create a global CLI credential"),
                            ("create-snmp-ro", "create a global SNMPv2 read-only credential")):
        sub = commands.add_parser(name, help=help_text)
        sub.add_argument("--description", required=True, help="the name shown in Catalyst Center (must be unique)")
        if name == "create-cli":
            sub.add_argument("--username", required=True)
        sub.add_argument("--comments", help="optional comment stored with the credential")
        sub.add_argument("--from-env", action="store_true",
                         help="read secrets from environment variables instead of prompting")
        sub.add_argument("--dry-run", action="store_true",
                         help="show the request with secrets masked; call nothing")
        sub.add_argument("--timeout", type=int, default=DEFAULT_CONFIRM_TIMEOUT_SECONDS,
                         help="seconds to wait for the credential to appear (default: %(default)s)")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        offline = args.command.startswith("create") and args.dry_run
        api = None
        # [credentials] Read the API login from .env and log in, unless this is a dry run.
        if not offline:  # a dry run needs no .env and no connection
            settings = load_settings()
            warn_if_insecure(settings)
            api = connect(settings)

        # [main task] Run the chosen command.
        if args.command == "list":
            return cmd_list(api, args)
        if args.command == "check":
            return cmd_check(api, args)
        if args.command == "create-cli":
            return cmd_create_cli(api, args)
        return cmd_create_snmp_ro(api, args)
    # [error handling] Expected problems (bad input, an SDK or network error) print one line and
    # exit 1. Anything unexpected still shows a full traceback.
    except (ScriptError, catalystcentersdkException, requests.RequestException, OSError) as exc:
        print("Error: {}".format(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
