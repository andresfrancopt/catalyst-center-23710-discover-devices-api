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

"""Start Catalyst Center discovery jobs for the devices listed below (SDK version).

Edit DEVICES, make sure .env is filled in, then run:

    python discover_ips_in_code.py            # submit the discovery jobs
    python discover_ips_in_code.py --dry-run  # only show what would be sent
"""

from __future__ import annotations

import argparse
import json
import sys

import requests
from catalystcentersdk import CatalystCenterAPI
from catalystcentersdk.exceptions import catalystcentersdkException

from discovery_common import (
    REQUEST_TIMEOUT_SECONDS,
    SDK_API_VERSION,
    ConfigError,
    JobSubmissionError,
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
#    ("10.48.91.10", "branch"),
]
JOB_NAME_PREFIX = "discover"
# ---------------------------------------------------------------------------


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

        # The SDK requests the auth token (POST /dna/system/api/v1/auth/token)
        # and sends it with every later call.
        api = CatalystCenterAPI(
            username=settings.username,
            password=settings.password,
            base_url=settings.base_url,
            version=SDK_API_VERSION,
            verify=settings.verify,
            single_request_timeout=REQUEST_TIMEOUT_SECONDS,
        )

        # POST /dna/intent/api/v1/discovery, once per credential profile.
        results = run_jobs(
            jobs,
            lambda payload: api.discovery.start_discovery(
                payload=payload, active_validation=False
            ),
        )
    except (
        ConfigError,
        JobSubmissionError,
        catalystcentersdkException,
        requests.RequestException,
        OSError,
    ) as exc:
        print("Error: {}".format(exc), file=sys.stderr)
        return 1

    for profile, response in results:
        print("{}: {}".format(profile, json.dumps(response)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
