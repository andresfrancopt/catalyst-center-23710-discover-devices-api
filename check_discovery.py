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

"""Check the result of a Catalyst Center discovery that was submitted earlier (SDK version).

Give either the task ID printed when the job was submitted, or a discovery ID:

    python check_discovery.py <task-id>
    python check_discovery.py --discovery-id <discovery-id>
    python check_discovery.py <task-id> --timeout 0     # check once, do not wait
"""

from __future__ import annotations

import argparse
import sys

import requests
from catalystcentersdk import CatalystCenterAPI
from catalystcentersdk.exceptions import catalystcentersdkException

from discovery_common import (
    DEFAULT_WAIT_TIMEOUT_SECONDS,
    REQUEST_TIMEOUT_SECONDS,
    SDK_API_VERSION,
    ApiGetters,
    ConfigError,
    apply_tls_policy,
    load_settings,
    print_reports,
    verify_job,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("task_id", nargs="?", help="task ID returned when the job was submitted")
    parser.add_argument("--discovery-id", help="check this discovery directly (skips the task step)")
    parser.add_argument(
        "--timeout",
        type=int,
        default=DEFAULT_WAIT_TIMEOUT_SECONDS,
        help="seconds to wait for results; 0 checks once (default: %(default)s)",
    )
    parser.add_argument(
        "--raw",
        action="store_true",
        help="also print the controller's raw responses (secrets redacted)",
    )
    args = parser.parse_args()
    if bool(args.task_id) == bool(args.discovery_id):
        parser.error("give exactly one of: a task ID, or --discovery-id")

    try:
        # Only the API login is needed here, not the credential profiles.
        settings = load_settings(require_profiles=False)
        apply_tls_policy(settings)

        api = CatalystCenterAPI(
            username=settings.username,
            password=settings.password,
            base_url=settings.base_url,
            version=SDK_API_VERSION,
            verify=settings.verify,
            single_request_timeout=REQUEST_TIMEOUT_SECONDS,
        )
        getters = ApiGetters(
            get_task=lambda task_id: api.task.get_task_by_id(task_id),
            get_discovery=lambda discovery_id: api.discovery.get_discovery_by_id(
                discovery_id
            ),
            get_jobs=lambda discovery_id: (
                api.discovery.get_list_of_discoveries_by_discovery_id(discovery_id)
            ),
        )
        label = "task {}".format(args.task_id) if args.task_id else "discovery {}".format(args.discovery_id)
        report = verify_job(
            label,
            getters,
            task_id=args.task_id,
            discovery_id=args.discovery_id,
            timeout=args.timeout,
        )
    except (ConfigError, catalystcentersdkException, requests.RequestException, OSError) as exc:
        print("Error: {}".format(exc), file=sys.stderr)
        return 1

    return print_reports([report], args.raw)


if __name__ == "__main__":
    raise SystemExit(main())
