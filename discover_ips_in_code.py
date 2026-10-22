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

    python discover_ips_in_code.py            # submit, then wait and report the results
    python discover_ips_in_code.py --dry-run  # only show what would be sent
    python discover_ips_in_code.py --no-wait  # submit only (check later with check_discovery.py)
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
    ApiGetters,
    ConfigError,
    JobSubmissionError,
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
#    ("10.48.91.10", "branch"),
]
JOB_NAME_PREFIX = "discover"
# ---------------------------------------------------------------------------


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

        # .env may hold credential descriptions (like "CSS") or IDs. Look them up
        # (GET /dna/intent/api/v1/global-credential) and stop early if one is missing.
        settings = resolve_settings(
            settings,
            groups,
            lambda subtype: api.discovery.get_global_credentials(subtype),
        )
        jobs = build_jobs(groups, settings, JOB_NAME_PREFIX)

        # POST /dna/intent/api/v1/discovery, once per credential profile.
        results = run_jobs(
            jobs,
            lambda payload: api.discovery.start_discovery(
                payload=payload, active_validation=False
            ),
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
            get_task=lambda task_id: api.task.get_task_by_id(task_id),
            get_discovery=lambda discovery_id: api.discovery.get_discovery_by_id(
                discovery_id
            ),
            get_jobs=lambda discovery_id: (
                api.discovery.get_list_of_discoveries_by_discovery_id(discovery_id)
            ),
        )
        reports = verify_submitted(results, getters, args.timeout)
    except (
        ConfigError,
        JobSubmissionError,
        catalystcentersdkException,
        requests.RequestException,
        OSError,
    ) as exc:
        print("Error: {}".format(exc), file=sys.stderr)
        return 1

    return print_reports(reports, args.raw)


if __name__ == "__main__":
    raise SystemExit(main())
