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

"""Start Catalyst Center discovery jobs for devices listed in a CSV file (SDK version).

CSV columns: ip_address (required), credential_profile (optional).
An empty or missing credential_profile uses the default (first) profile in .env.

    python discover_from_csv.py devices.csv
    python discover_from_csv.py devices.csv --name-prefix office --dry-run
    python discover_from_csv.py devices.csv --no-wait   # submit only; check later
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

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

# Reading guide: [main task] marks where the real work happens, [credentials]
# where credentials or secrets are read, looked up or kept out of output, and
# [error handling] where input is checked or errors are caught and explained.


def read_devices(csv_path: Path) -> list[tuple[str, str | None]]:
    """Return (ip_address, credential_profile) pairs from the CSV."""
    # [error handling] A missing column, an empty IP or an empty file stops the script with
    # a clear message. "utf-8-sig" accepts the byte-order mark that Excel adds.
    try:
        with open(csv_path, encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            columns = {(name or "").strip(): name for name in reader.fieldnames or []}
            if "ip_address" not in columns:
                raise ConfigError("CSV needs an 'ip_address' column in its header row")
            ip_column = columns["ip_address"]
            profile_column = columns.get("credential_profile")

            devices: list[tuple[str, str | None]] = []
            for row in reader:
                ip = row.get(ip_column)
                if ip is None or not ip.strip():
                    raise ConfigError("CSV line {}: empty ip_address".format(reader.line_num))
                profile = row.get(profile_column) if profile_column else None
                devices.append((ip, profile))
    except csv.Error as exc:
        raise ConfigError("Could not parse CSV: {}".format(exc)) from exc

    if not devices:
        raise ConfigError("CSV contains no devices")
    return devices


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("csv_file", type=Path, help="CSV with ip_address[,credential_profile]")
    parser.add_argument(
        "--name-prefix",
        default="discover",
        help="job names are <prefix>-<profile> (default: %(default)s)",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="print the payloads without calling the API"
    )
    add_wait_arguments(parser)
    args = parser.parse_args()

    try:
        # Step 1: read the settings ([credentials]: API login and profiles from .env) and the CSV
        # ([error handling]: bad IPs or unknown profiles stop here, before any API call).
        settings = load_settings()
        groups = group_by_profile(read_devices(args.csv_file), settings)

        if args.dry_run:
            print(
                "# Dry run: credential values are shown as written in .env "
                "(not looked up yet).",
                file=sys.stderr,
            )
            for _profile, payload in build_jobs(groups, settings, args.name_prefix):
                print(json.dumps(payload, indent=2))
            return 0

        # Lab only: prints a warning when certificate checking is switched off in .env.
        apply_tls_policy(settings)

        # Step 2 [credentials]: log in. The SDK requests the auth token
        # (POST /dna/system/api/v1/auth/token) and sends it with every later call.
        api = CatalystCenterAPI(
            username=settings.username,
            password=settings.password,
            base_url=settings.base_url,
            version=SDK_API_VERSION,
            verify=settings.verify,
            single_request_timeout=REQUEST_TIMEOUT_SECONDS,
        )

        # Step 3 [credentials]: .env may hold credential descriptions (like "CSS") or IDs.
        # Look them up (GET /dna/intent/api/v1/global-credential) and stop early if one is
        # missing ([error handling]), so a typo never reaches the controller.
        settings = resolve_settings(
            settings,
            groups,
            lambda subtype: api.discovery.get_global_credentials(subtype),
        )
        jobs = build_jobs(groups, settings, args.name_prefix)

        # Step 4 [main task]: submit the discovery (POST /dna/intent/api/v1/discovery), once per
        # credential profile. This only starts it; the discovery keeps running on the controller.
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

        # Step 5 [main task]: discovery is asynchronous, so follow each task, then the
        # discovery itself, then each device.
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
    # [error handling] Expected problems (bad settings, a failed submission, an SDK or network
    # error) print one line and exit 1. Anything unexpected still shows a full traceback.
    except (
        ConfigError,
        JobSubmissionError,
        catalystcentersdkException,
        requests.RequestException,
        OSError,
    ) as exc:
        print("Error: {}".format(exc), file=sys.stderr)
        return 1

    # Exit code 0 only if every job succeeded.
    return print_reports(reports, args.raw)


if __name__ == "__main__":
    raise SystemExit(main())
