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

"""Assign devices that are in Catalyst Center but not yet assigned to a site (SDK version).

REMINDER: site names such as "Global/London/Building1/Floor1" are translated to site IDs
using a LOCAL COPY of the site list (sites_cache.json), so Catalyst Center is not asked for
all sites on every run. If sites are created or renamed in Catalyst Center, update the copy
by running this script with --refresh-sites.

The devices come from the same CSV that the discovery uses (devices.csv). It needs an
ip_address column and, for the site, a site column (other columns are ignored):

    ip_address,credential_profile,site
    10.48.90.173,default,Global/London/Building1/Floor1
    10.48.90.174,default,Global/London/Building1

    python assign_devices_to_site.py --refresh-sites          # (re)read the sites and save them
    python assign_devices_to_site.py devices.csv --dry-run    # preview only: nothing is changed
    python assign_devices_to_site.py devices.csv              # assign, then verify
    python assign_devices_to_site.py devices.csv --site "Global/London/Building1"   # default site

Only devices that are not assigned to any site are assigned; devices that already have a
site are skipped and reported, never moved. A site must be a building or a floor (access
points and sensors go to a floor, other devices to a building; the controller enforces it).
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests
from catalystcentersdk import CatalystCenterAPI
from catalystcentersdk.exceptions import catalystcentersdkException

from discovery_common import (
    ASSIGN_WAIT_INTERVAL_SECONDS,
    DEFAULT_PAGE_SIZE,
    DEFAULT_WAIT_TIMEOUT_SECONDS,
    REQUEST_TIMEOUT_SECONDS,
    SDK_API_VERSION,
    SITE_REFRESH_REMINDER,
    ConfigError,
    SiteApi,
    apply_tls_policy,
    assignment_report,
    cache_note,
    load_settings,
    load_site_cache,
    locate_devices,
    plan_items,
    read_device_sites,
    redact,
    refresh_site_cache,
    resolve_sites,
    response_items,
    run_assignments,
    site_type_counts,
    verify_assignments,
)

# Reading guide: [main task] marks where the real work happens, [credentials]
# where credentials or secrets are read, looked up or kept out of output, and
# [error handling] where input is checked or errors are caught and explained.

DEFAULT_SITES_CACHE = "sites_cache.json"


def build_site_api(api: CatalystCenterAPI, samples: dict | None = None) -> SiteApi:
    """Connect the shared logic to the SDK. `samples` remembers a first raw record (for --raw)."""

    def remember(name: str, items: list) -> list:
        if samples is not None and items and name not in samples:
            samples[name] = items[0]
        return items

    return SiteApi(
        # GET /dna/intent/api/v1/sites (all site types; the cache keeps what it needs)
        list_sites=lambda offset, limit: remember(
            "site", response_items(api.site_design.get_sites(offset=offset, limit=limit))),
        # GET /dna/intent/api/v1/networkDevices/notAssignedToSite
        list_unassigned=lambda offset, limit: remember(
            "device not assigned to a site",
            response_items(api.site_design.get_site_not_assigned_network_devices(offset=offset, limit=limit))),
        # GET /dna/intent/api/v1/network-device?managementIpAddress=...
        list_devices_by_ip=lambda ips, offset, limit: response_items(
            api.devices.get_device_list(management_ip_address=list(ips), offset=offset, limit=limit)),
        # POST /dna/intent/api/v1/networkDevices/assignToSite/apply (starts a task)
        assign=lambda site_id, device_ids: api.site_design.assign_network_devices_to_a_site(
            deviceIds=list(device_ids), siteId=site_id),
        # GET /dna/intent/api/v1/task/{taskId}
        get_task=lambda task_id: api.task.get_task_by_id(task_id),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("csv_file", nargs="?", type=Path,
                        help="CSV with ip_address and (optionally) site columns")
    parser.add_argument("--site", help="site for CSV rows that have no site, e.g. Global/London/Building1")
    parser.add_argument("--sites-cache", type=Path, default=Path(DEFAULT_SITES_CACHE),
                        help="local copy of the site list (default: %(default)s)")
    parser.add_argument("--refresh-sites", action="store_true",
                        help="read the sites from Catalyst Center again and save them "
                             "(use after creating or renaming sites)")
    parser.add_argument("--wait-for-devices", type=int, default=0, metavar="SECONDS",
                        help="keep looking for devices that are not in the inventory yet "
                             "(right after a discovery); default: do not wait")
    parser.add_argument("--dry-run", action="store_true",
                        help="read-only preview: show what would be assigned, change nothing")
    parser.add_argument("--no-wait", action="store_true",
                        help="submit the assignment only; do not wait for or verify the result")
    parser.add_argument("--timeout", type=int, default=DEFAULT_WAIT_TIMEOUT_SECONDS,
                        help="seconds to wait for each assignment task (default: %(default)s)")
    parser.add_argument("--raw", action="store_true",
                        help="also print the first raw site and device records (secrets redacted)")
    args = parser.parse_args()
    # [error handling] Something to do is required: a CSV file, or just refreshing the sites.
    if args.csv_file is None and not args.refresh_sites:
        parser.error("give a CSV file, or use --refresh-sites on its own")

    try:
        # Step 1: read the settings ([credentials]: only the API login is needed) and the CSV
        # ([error handling]: bad or repeated IPs and rows with no site stop the script here).
        settings = load_settings(require_profiles=False)
        rows = read_device_sites(args.csv_file, args.site) if args.csv_file else None

        # Lab only: prints a warning when certificate checking is switched off in .env.
        apply_tls_policy(settings)
        # Step 2 [credentials]: log in. The SDK requests the auth token and sends it with every call.
        api = CatalystCenterAPI(
            username=settings.username,
            password=settings.password,
            base_url=settings.base_url,
            version=SDK_API_VERSION,
            verify=settings.verify,
            single_request_timeout=REQUEST_TIMEOUT_SECONDS,
        )
        samples: dict = {}
        site_api = build_site_api(api, samples if args.raw else None)

        # Step 3: the site list. Use the local copy if there is one; read it from Catalyst
        # Center (paged) the first time, when it is for another controller, or when asked to.
        now = datetime.now(timezone.utc)
        cache, why = (None, "") if args.refresh_sites else load_site_cache(args.sites_cache, settings.base_url)
        if cache is None:
            if why:
                print("Reading the site list from Catalyst Center: {}.".format(why))
            cache = refresh_site_cache(site_api, settings.base_url, args.sites_cache, DEFAULT_PAGE_SIZE)
            print("Saved {} sites ({}) to {}.".format(len(cache.sites), site_type_counts(cache.sites), args.sites_cache))
        else:
            print(cache_note(cache, now))
        if rows is None:
            print(SITE_REFRESH_REMINDER)
            return 0

        # [error handling] Every site name must match one building or floor; all problems are
        # shown together. A name that is not in the list stops the script (no silent refresh).
        sites = resolve_sites(cache, [site for _, site in rows])

        # Step 4 [main task]: find the devices. The 'not assigned to a site' list is read once
        # and matched locally; the rest are looked up in the inventory.
        located = locate_devices(
            site_api, [ip for ip, _ in rows], args.wait_for_devices, ASSIGN_WAIT_INTERVAL_SECONDS)
        items = plan_items(rows, sites, located)

        if args.dry_run:
            print("Dry run: nothing is assigned. This is what would happen:")
        else:
            # Step 5 [main task]: assign (one request per site, at most 100 devices each) ...
            run_assignments(site_api, items, args.timeout, args.no_wait)
            # ... Step 6: confirm the devices really left the 'not assigned' list.
            if not args.no_wait:
                verify_assignments(site_api, items)
    # [error handling] Expected problems (bad input, an unknown site, an SDK or network error)
    # print one line and exit 1. Anything unexpected still shows a full traceback.
    except (ConfigError, catalystcentersdkException, requests.RequestException, OSError) as exc:
        print("Error: {}".format(exc), file=sys.stderr)
        return 1

    lines, ok = assignment_report(items)
    print("\n".join(lines))
    if args.no_wait and not args.dry_run:
        print("Not waiting for results (--no-wait). Check each task with: python ../2-standalone/check_task.py <task-id>")
    if args.raw:
        print(json.dumps(redact(samples), indent=2, default=str))
    print(SITE_REFRESH_REMINDER)
    # Exit code 0 only if every device is assigned, already assigned (or would be, in a dry run).
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
