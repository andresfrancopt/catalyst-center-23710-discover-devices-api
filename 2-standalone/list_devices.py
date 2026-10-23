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

"""Standalone: list the devices in the Catalyst Center inventory, on screen and in a CSV file.

This file needs only the Python packages catalystcentersdk, python-dotenv and
requests (see requirements.txt). It does not import any other script. It only
reads from Catalyst Center and only needs the API login (CATALYST_CENTER_BASE_URL,
_USERNAME, _PASSWORD, and optionally _VERIFY_SSL / _CA_BUNDLE) in a .env file next
to this script or in the folder you run it from.

    python list_devices.py                          # all devices: table on screen + CSV file
    python list_devices.py --unassigned             # only devices not assigned to a site
    python list_devices.py --ip "10.48.90..*" --role ACCESS
    python list_devices.py --with-site              # also look up each device's site
    python list_devices.py --output inventory.csv --overwrite
    python list_devices.py --no-csv                 # screen only

The inventory can be larger than one response, and the SDK does not page for you, so
this script asks for the devices page by page (up to 500 per page) until it has them all.
The screen shows the most useful columns; the CSV has all of them.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime
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
MAX_PAGE_SIZE = 500  # the controller's limit per request
DEFAULT_PAGE_SIZE = MAX_PAGE_SIZE
MAX_PAGES = 1000  # safety net against a loop that never ends
_SECRET_KEY_PARTS = ("password", "passphrase", "community", "secret", "token")
UNASSIGNED_TEXT = "(not assigned)"

# CSV column name -> field in the controller's device record. The "site" column is extra.
COLUMNS: list[tuple[str, str]] = [
    ("id", "id"),
    ("hostname", "hostname"),
    ("management_ip", "managementIpAddress"),
    ("platform", "platformId"),
    ("family", "family"),
    ("role", "role"),
    ("software_version", "softwareVersion"),
    ("serial_number", "serialNumber"),
    ("mac_address", "macAddress"),
    ("reachability", "reachabilityStatus"),
    ("collection_status", "collectionStatus"),
    ("error_code", "errorCode"),
    ("error_description", "errorDescription"),
    ("uptime", "upTime"),
    ("last_updated", "lastUpdated"),
]
# The screen is narrower than the CSV, so it shows only these columns.
SCREEN_COLUMNS = ["hostname", "management_ip", "platform", "role", "software_version",
                  "serial_number", "reachability", "collection_status"]
SCREEN_MAX_WIDTH = 32


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
# 2. Reading the inventory page by page
# ===========================================================================


# Catalyst Center wraps results in {"response": ...}. These helpers tolerate missing or
# unexpected fields (names not confirmed live), so a surprise never crashes a check.
def response_items(response: Any) -> list[Mapping[str, Any]]:
    body = response.get("response") if isinstance(response, Mapping) else None
    return [item for item in body if isinstance(item, Mapping)] if isinstance(body, list) else []


def fetch_pages(
    fetch_page: Callable[[int, int], list[Mapping[str, Any]]],
    page_size: int = DEFAULT_PAGE_SIZE,
    max_pages: int = MAX_PAGES,
    on_page: Callable[[int, int], None] | None = None,
) -> list[Mapping[str, Any]]:
    """Collect every record by asking for page after page.

    fetch_page(offset, limit) returns one page. The controller numbers records from 1, so
    the pages start at offset 1, 1 + page_size, 1 + 2 * page_size, and so on. A page shorter
    than page_size is the last one.
    """
    items: list[Mapping[str, Any]] = []
    previous: list[Mapping[str, Any]] | None = None
    for page_number in range(1, max_pages + 1):
        offset = 1 + (page_number - 1) * page_size
        page = fetch_page(offset, page_size)
        if not page:
            return items
        # [error handling] A controller that ignores "offset" would send the same page forever.
        if previous is not None and page == previous:
            raise ScriptError(
                "The controller returned the same page twice (offset {}), so paging does not "
                "work as expected. Stopping with {} records read.".format(offset, len(items))
            )
        items.extend(page)
        if on_page is not None:
            on_page(page_number, len(items))
        if len(page) < page_size:
            return items
        previous = page
    raise ScriptError(
        "More than {} pages of {} records. Narrow the search with a filter, or raise "
        "the page size.".format(max_pages, page_size)
    )


def split_list(text: str) -> list[str]:
    """'a, b,,c' -> ['a', 'b', 'c']. Filters accept several comma-separated values."""
    return [part.strip() for part in text.split(",") if part.strip()]


def fetch_devices(api: CatalystCenterAPI, filters: Mapping[str, list[str]], page_size: int,
                  on_page: Callable[[int, int], None] | None = None) -> list[Mapping[str, Any]]:
    """Every inventory device matching the filters (GET /dna/intent/api/v1/network-device)."""
    return fetch_pages(
        lambda offset, limit: response_items(
            api.devices.get_device_list(offset=offset, limit=limit, **filters)
        ),
        page_size,
        on_page=on_page,
    )


def fetch_unassigned_ids(api: CatalystCenterAPI, page_size: int) -> set[str]:
    """IDs of devices with no site (GET /dna/intent/api/v1/networkDevices/notAssignedToSite)."""
    items = fetch_pages(
        lambda offset, limit: response_items(
            api.site_design.get_site_not_assigned_network_devices(offset=offset, limit=limit)
        ),
        page_size,
    )
    ids = {str(item["id"]) for item in items if item.get("id")}
    # [error handling] If the records have no "id" we cannot match them to inventory devices.
    if items and not ids:
        raise ScriptError(
            "The 'not assigned to a site' list has no device IDs, so it cannot be matched to the "
            "inventory (run with --raw to see what the controller returns)."
        )
    return ids


def site_text(response: Any) -> str:
    """The site name hierarchy from a 'device -> site' response, or '' if there is none."""
    body = response.get("response") if isinstance(response, Mapping) else None
    if isinstance(body, list):
        body = body[0] if body else None
    if not isinstance(body, Mapping):
        return ""
    for key in ("siteNameHierarchy", "nameHierarchy", "siteName", "name"):
        if body.get(key):
            return str(body[key])
    return ""


def look_up_sites(api: CatalystCenterAPI, devices: Sequence[Mapping[str, Any]]) -> dict[str, str]:
    """Each device's site, one call per device (GET /dna/intent/api/v1/networkDevices/{id}/assignedToSite)."""
    sites: dict[str, str] = {}
    failures = 0
    for number, device in enumerate(devices, start=1):
        device_id = str(device.get("id") or "")
        if not device_id:
            continue
        try:
            sites[device_id] = site_text(api.site_design.get_site_assigned_network_device(device_id))
        # [error handling] One device that cannot be looked up must not stop the whole run.
        except (catalystcentersdkException, requests.RequestException):
            sites[device_id] = ""
            failures += 1
        if number % 25 == 0 or number == len(devices):
            print("  looked up the site of {} of {} devices".format(number, len(devices)), file=sys.stderr)
    if failures:
        print("  {} site lookup(s) failed and are left blank.".format(failures), file=sys.stderr)
    return sites


# ===========================================================================
# 3. Rows, table and CSV
# ===========================================================================


def make_row(device: Mapping[str, Any], site: str | None) -> dict[str, str]:
    """One output row. Missing fields become empty text; a site column is added if given."""
    row = {name: "" if device.get(field_name) is None else str(device[field_name])
           for name, field_name in COLUMNS}
    if site is not None:
        row["site"] = site
    return row


def reachability_summary(rows: Sequence[Mapping[str, str]]) -> str:
    counts: dict[str, int] = {}
    for row in rows:
        key = row.get("reachability") or "unknown"
        counts[key] = counts.get(key, 0) + 1
    return ", ".join("{}: {}".format(key, counts[key]) for key in sorted(counts))


def format_table(rows: Sequence[Mapping[str, str]], columns: Sequence[str]) -> str:
    """A plain-text table; long values are shortened so the lines stay readable."""
    def shorten(text: str) -> str:
        return text if len(text) <= SCREEN_MAX_WIDTH else text[: SCREEN_MAX_WIDTH - 3] + "..."

    cells = [[shorten(row.get(column, "")) for column in columns] for row in rows]
    widths = [max([len(column)] + [len(line[i]) for line in cells]) for i, column in enumerate(columns)]

    def line(values: Sequence[str]) -> str:
        return "  ".join(value.ljust(width) for value, width in zip(values, widths)).rstrip()

    return "\n".join([line(columns), line(["-" * width for width in widths])] + [line(c) for c in cells])


_NUMBER = re.compile(r"^-?\d+(\.\d+)?$")


# [credentials] [error handling] Inventory text comes from outside, and a spreadsheet runs
# cells that start with = + - @ as formulas. A leading quote makes them plain text.
def safe_cell(value: str) -> str:
    if value[:1] in ("=", "+", "@", "\t", "\r") or (value[:1] == "-" and not _NUMBER.match(value)):
        return "'" + value
    return value


def default_output_name(now: datetime) -> str:
    return "inventory-{}.csv".format(now.strftime("%Y%m%d-%H%M%S"))


def write_csv(path: Path, rows: Sequence[Mapping[str, str]], columns: Sequence[str], overwrite: bool) -> None:
    # [error handling] Never replace an existing file by accident, and explain a missing folder.
    if not path.parent.is_dir():
        raise ScriptError("The folder for the CSV file does not exist: {}".format(path.parent))
    try:
        with open(path, "w" if overwrite else "x", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(columns)
            for row in rows:
                writer.writerow([safe_cell(row.get(column, "")) for column in columns])
    except FileExistsError:
        raise ScriptError(
            "{} already exists. Use --overwrite to replace it, or choose another --output.".format(path)
        ) from None


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


# ===========================================================================
# 4. Main flow
# ===========================================================================


def page_size_number(text: str) -> int:
    """argparse type for --page-size: 1 to 500 (the controller's limit)."""
    try:
        size = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError("{!r} is not a number".format(text)) from None
    if not 1 <= size <= MAX_PAGE_SIZE:
        raise argparse.ArgumentTypeError("page size must be between 1 and {}".format(MAX_PAGE_SIZE))
    return size


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    filters = parser.add_argument_group(
        "filters", "each takes one value or several separated by commas; .* works as a wildcard")
    filters.add_argument("--ip", help="management IP address, e.g. 10.48.90.173 or '10.48.90..*'")
    filters.add_argument("--hostname", help="hostname, e.g. 'switch-.*'")
    filters.add_argument("--family", help="device family, e.g. 'Switches and Hubs'")
    filters.add_argument("--role", help="device role, e.g. ACCESS, DISTRIBUTION, CORE")
    filters.add_argument("--reachability", help="reachability status, e.g. Reachable or Unreachable")
    parser.add_argument("--unassigned", action="store_true",
                        help="only devices that are not assigned to a site")
    parser.add_argument("--with-site", action="store_true",
                        help="add a site column (one extra API call per device, so slower)")
    parser.add_argument("--output", type=Path,
                        help="CSV file to write (default: inventory-<date>-<time>.csv in this folder)")
    parser.add_argument("--overwrite", action="store_true", help="replace the CSV file if it exists")
    parser.add_argument("--no-csv", action="store_true", help="only show the table; write no file")
    parser.add_argument("--page-size", type=page_size_number, default=DEFAULT_PAGE_SIZE,
                        help="records per request, 1-%d (default: %%(default)s)" % MAX_PAGE_SIZE)
    parser.add_argument("--raw", action="store_true",
                        help="also print the first raw device record (secrets redacted), to check field names")
    return parser


def main(now: Callable[[], datetime] = datetime.now) -> int:
    parser = build_parser()
    args = parser.parse_args()
    # [error handling] A CSV file cannot be both skipped and named.
    if args.no_csv and (args.output or args.overwrite):
        parser.error("--no-csv cannot be combined with --output or --overwrite")

    try:
        # [credentials] Only the API login is read from .env. Lab only: warn if certificate
        # checking is off.
        settings = load_settings()
        warn_if_insecure(settings)
        api = connect(settings)

        # Only the filters that were given are sent to the controller.
        filters = {name: split_list(value) for name, value in (
            ("management_ip_address", args.ip), ("hostname", args.hostname), ("family", args.family),
            ("role", args.role), ("reachability_status", args.reachability)) if value}

        # [main task] Read the inventory page by page, because one request returns at most
        # 500 devices and the SDK does not page for you.
        def progress(page: int, total: int) -> None:
            print("  page {}: {} devices read so far".format(page, total), file=sys.stderr)

        print("Reading devices from {} ...".format(settings.base_url), file=sys.stderr)
        devices = fetch_devices(api, filters, args.page_size, progress)

        # --unassigned: keep only the devices that appear in the 'not assigned to a site' list.
        if args.unassigned:
            unassigned = fetch_unassigned_ids(api, args.page_size)
            devices = [device for device in devices if str(device.get("id")) in unassigned]

        # --with-site: one extra call per device (skipped for devices known to have no site).
        sites: dict[str, str] | None = None
        if args.with_site:
            sites = ({str(d.get("id")): UNASSIGNED_TEXT for d in devices} if args.unassigned
                     else look_up_sites(api, devices))
        rows = [make_row(d, None if sites is None else sites.get(str(d.get("id")), "")) for d in devices]

        columns = [name for name, _ in COLUMNS] + (["site"] if args.with_site else [])
        screen_columns = SCREEN_COLUMNS + (["site"] if args.with_site else [])

        # [main task] Show the result on screen ...
        if rows:
            print(format_table(rows, screen_columns))
        else:
            print("No devices match.")
        print("{} device(s). {}".format(len(rows), reachability_summary(rows) or "nothing to summarise"))

        # ... and write all columns to the CSV file.
        if not args.no_csv:
            path = args.output or Path(default_output_name(now()))
            write_csv(path, rows, columns, args.overwrite)
            print("Wrote {} row(s) with {} columns to {}".format(len(rows), len(columns), path))

        if args.raw and devices:
            print("First raw device record:")
            print(json.dumps(redact(dict(devices[0])), indent=2, default=str))
    # [error handling] Expected problems (bad settings, an SDK or network error, a file that
    # cannot be written) print one line and exit 1. Anything unexpected still shows a traceback.
    except (ScriptError, catalystcentersdkException, requests.RequestException, OSError) as exc:
        print("Error: {}".format(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
