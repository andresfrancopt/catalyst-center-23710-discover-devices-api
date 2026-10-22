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

    python reference_discover_without_sdk.py [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import sys

import requests

from discovery_common import (
    REQUEST_TIMEOUT_SECONDS,
    ConfigError,
    JobSubmissionError,
    Settings,
    apply_tls_policy,
    build_jobs,
    group_by_profile,
    load_settings,
    run_jobs,
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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--dry-run", action="store_true", help="print the payloads without calling the API"
    )
    args = parser.parse_args()

    try:
        settings = load_settings()
        groups = group_by_profile(DEVICES, settings)
        jobs = build_jobs(groups, settings, JOB_NAME_PREFIX)

        if args.dry_run:
            for _profile, payload in jobs:
                print(json.dumps(payload, indent=2))
            return 0

        apply_tls_policy(settings)
        token = get_token(settings)

        def submit(payload: dict) -> dict:
            # With the SDK this is api.discovery.start_discovery(payload=...)
            response = requests.post(
                settings.base_url + DISCOVERY_PATH,
                headers={"X-Auth-Token": token},
                json=payload,
                verify=settings.verify,
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
            response.raise_for_status()
            return response.json()

        results = run_jobs(jobs, submit)
    except (ConfigError, JobSubmissionError, requests.RequestException, OSError) as exc:
        print("Error: {}".format(exc), file=sys.stderr)
        return 1

    for profile, response in results:
        print("{}: {}".format(profile, json.dumps(response)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
