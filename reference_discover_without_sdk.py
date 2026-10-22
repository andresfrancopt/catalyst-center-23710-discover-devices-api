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

"""Same as discover_ips_in_code.py, but WITHOUT the Catalyst Center SDK.

This reference script shows what the SDK does for you: it requests the auth
token by hand, builds every request and sends it with `requests`, and has to
turn HTTP errors into messages itself. Compare it with discover_ips_in_code.py.

    python reference_discover_without_sdk.py [--dry-run] [--no-wait] [--timeout N] [--raw]
"""

from __future__ import annotations

import argparse
import json
import sys

import requests

from discovery_common import (
    REQUEST_TIMEOUT_SECONDS,
    ApiGetters,
    ConfigError,
    JobSubmissionError,
    Settings,
    add_wait_arguments,
    apply_tls_policy,
    build_jobs,
    group_by_profile,
    load_settings,
    print_reports,
    resolve_settings,
    run_jobs,
    task_id_from_response,
    verify_submitted,
)

# ---------------------------------------------------------------------------
# EDIT HERE: (IP address, credential profile defined in .env).
# Use None for the default profile. Each profile becomes one discovery job.
# ---------------------------------------------------------------------------
DEVICES = [
    ("10.48.90.173", "default"),
    ("10.48.90.174", "default"),
    ("10.48.91.10", "branch"),
]
JOB_NAME_PREFIX = "discover"
# ---------------------------------------------------------------------------

AUTH_PATH = "/dna/system/api/v1/auth/token"
DISCOVERY_PATH = "/dna/intent/api/v1/discovery"
DISCOVERY_ID_PATH = "/dna/intent/api/v1/discovery/{id}"
DISCOVERY_JOBS_PATH = "/dna/intent/api/v1/discovery/{id}/job"
CREDENTIAL_PATH = "/dna/intent/api/v1/global-credential"
TASK_PATH = "/dna/intent/api/v1/task/{id}"


def get_token(settings: Settings) -> str:
    # With the SDK this is automatic: CatalystCenterAPI(username=..., password=...)
    response = requests.post(
        settings.base_url + AUTH_PATH,
        auth=(settings.username, settings.password),  # HTTP Basic auth
        verify=settings.verify,
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    token = response.json().get("Token")
    if not token:
        raise ConfigError("The authentication response did not contain a token")
    return token


def call(settings: Settings, token: str, method: str, path: str, **kwargs) -> dict:
    # With the SDK each of these is one method call, e.g. api.task.get_task_by_id(id).
    response = requests.request(
        method,
        settings.base_url + path,
        headers={"X-Auth-Token": token},
        verify=settings.verify,
        timeout=REQUEST_TIMEOUT_SECONDS,
        **kwargs,
    )
    response.raise_for_status()
    return response.json()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--dry-run", action="store_true", help="print the payloads without calling the API"
    )
    add_wait_arguments(parser)
    args = parser.parse_args()

    try:
        settings = load_settings()
        groups = group_by_profile(DEVICES, settings)

        if args.dry_run:
            print(
                "# Dry run: credential values are shown as written in .env "
                "(not looked up yet).",
                file=sys.stderr,
            )
            for _profile, payload in build_jobs(groups, settings, JOB_NAME_PREFIX):
                print(json.dumps(payload, indent=2))
            return 0

        apply_tls_policy(settings)
        token = get_token(settings)

        def api(method: str, path: str, **kwargs) -> dict:
            return call(settings, token, method, path, **kwargs)

        # GET /dna/intent/api/v1/global-credential: turn the descriptions or IDs
        # in .env into real credential IDs, and stop early if one is missing.
        settings = resolve_settings(
            settings,
            groups,
            lambda subtype: api(
                "GET", CREDENTIAL_PATH, params={"credentialSubType": subtype}
            ),
        )
        jobs = build_jobs(groups, settings, JOB_NAME_PREFIX)

        # With the SDK this is api.discovery.start_discovery(payload=...)
        results = run_jobs(
            jobs, lambda payload: api("POST", DISCOVERY_PATH, json=payload)
        )
        for profile, response in results:
            print(
                "{}: submitted, task {}".format(
                    profile, task_id_from_response(response) or "?"
                )
            )
        if args.no_wait:
            print(
                "Not waiting for results (--no-wait). "
                "Check later with: python check_discovery.py <task-id>"
            )
            return 0

        # Discovery is asynchronous: follow each task, then the discovery itself.
        getters = ApiGetters(
            get_task=lambda task_id: api("GET", TASK_PATH.format(id=task_id)),
            get_discovery=lambda discovery_id: api(
                "GET", DISCOVERY_ID_PATH.format(id=discovery_id)
            ),
            get_jobs=lambda discovery_id: api(
                "GET", DISCOVERY_JOBS_PATH.format(id=discovery_id)
            ),
        )
        reports = verify_submitted(results, getters, args.timeout)
    except (ConfigError, JobSubmissionError, requests.RequestException, OSError) as exc:
        print("Error: {}".format(exc), file=sys.stderr)
        return 1

    return print_reports(reports, args.raw)


if __name__ == "__main__":
    raise SystemExit(main())
