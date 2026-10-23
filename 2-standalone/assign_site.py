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

"""Standalone: assign devices that are in Catalyst Center but not yet assigned to a site.

This file needs only the Python packages catalystcentersdk, python-dotenv and requests (see
requirements.txt). It does not import any other script. It needs the API login in a .env file
next to this script or in the folder you run it from (see .env.example in the project root).

REMINDER: site names such as "Global/London/Building1/Floor1" are translated to site IDs
using a LOCAL COPY of the site list (sites_cache.json), so Catalyst Center is not asked for
all sites on every run. If sites are created or renamed in Catalyst Center, update the copy
by running this script with --refresh-sites.

The devices come from the same CSV that the discovery uses (devices.csv). It needs an
ip_address column and, for the site, a site column (other columns are ignored):

    ip_address,credential_profile,site
    10.48.90.173,default,Global/London/Building1/Floor1
    10.48.90.174,default,Global/London/Building1

    python assign_site.py --refresh-sites          # (re)read the sites and save them
    python assign_site.py devices.csv --dry-run    # preview only: nothing is changed
    python assign_site.py devices.csv              # assign, then verify
    python assign_site.py devices.csv --site "Global/London/Building1"   # default site

Only devices that are not assigned to any site are assigned; devices that already have a
site are skipped and reported, never moved. A site must be a building or a floor (access
points and sensors go to a floor, other devices to a building; the controller enforces it).
"""

from __future__ import annotations

import argparse
import csv
import difflib
import ipaddress
import json
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

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
DEFAULT_SITES_CACHE = "sites_cache.json"

DEFAULT_PAGE_SIZE = 500  # the controller's limit per request
MAX_PAGES = 1000  # safety net against a loop that never ends
ASSIGN_BATCH_SIZE = 100  # the most devices one assign request accepts
INVENTORY_LOOKUP_CHUNK = 50  # IP addresses per inventory lookup
SITE_CACHE_VERSION = 1
SITE_CACHE_MAX_AGE_DAYS = 7
DEVICE_SITE_TYPES = ("building", "floor")  # devices can only go to these
ASSIGN_WAIT_INTERVAL_SECONDS = 10
_SECRET_KEY_PARTS = ("password", "passphrase", "community", "secret", "token")

# Where each device ends up. TO ASSIGN is the plan (dry run); the rest are results.
STATUS_TO_ASSIGN = "TO ASSIGN"
STATUS_TASK_DONE = "TASK DONE"
STATUS_ASSIGNED = "ASSIGNED"
STATUS_ALREADY = "ALREADY ASSIGNED"
STATUS_MISSING = "NOT IN INVENTORY"
STATUS_SUBMITTED = "SUBMITTED"
STATUS_PENDING = "PENDING"
STATUS_FAILED = "FAILED"
_OK_STATUSES = {STATUS_TO_ASSIGN, STATUS_ASSIGNED, STATUS_ALREADY, STATUS_SUBMITTED}

SITE_REFRESH_REMINDER = (
    "Reminder: site names are read from a local copy of the site list. If sites were created "
    "or renamed in Catalyst Center, run this script again with --refresh-sites."
)


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
# 2. Responses, paging and the calls to Catalyst Center
# ===========================================================================


# Catalyst Center wraps results in {"response": ...}. These helpers tolerate missing or
# unexpected fields (names not confirmed live), so a surprise never crashes a check.
def _body(response: Any) -> Mapping[str, Any]:
    body = response.get("response") if isinstance(response, Mapping) else None
    return body if isinstance(body, Mapping) else {}


def response_items(response: Any) -> list[Mapping[str, Any]]:
    body = response.get("response") if isinstance(response, Mapping) else None
    return [item for item in body if isinstance(item, Mapping)] if isinstance(body, list) else []


def task_id_from_response(response: Any) -> str | None:
    task_id = _body(response).get("taskId")
    return str(task_id) if task_id else None


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


# [error handling] The controller returns at most 500 records per request and the SDK does
# not page for you, so ask for page after page. Offsets start at 1.
def fetch_pages(
    fetch_page: Callable[[int, int], list[Mapping[str, Any]]],
    page_size: int = DEFAULT_PAGE_SIZE,
    max_pages: int = MAX_PAGES,
    on_page: Callable[[int, int], None] | None = None,
) -> list[Mapping[str, Any]]:
    """Collect every record: fetch_page(offset, limit) is called for offset 1, 1 + size, ...

    A page shorter than page_size is the last one. Stops with an error if the controller
    returns the same page twice (it ignored the offset) or there are too many pages.
    """
    items: list[Mapping[str, Any]] = []
    previous: list[Mapping[str, Any]] | None = None
    for page_number in range(1, max_pages + 1):
        offset = 1 + (page_number - 1) * page_size
        page = fetch_page(offset, page_size)
        if not page:
            return items
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


@dataclass(frozen=True)
class SiteApi:
    """The calls the assignment needs, filled in with SDK methods by build_site_api()."""

    list_sites: Callable[[int, int], list[Mapping[str, Any]]]  # (offset, limit)
    list_unassigned: Callable[[int, int], list[Mapping[str, Any]]]  # (offset, limit)
    list_devices_by_ip: Callable[[Sequence[str], int, int], list[Mapping[str, Any]]]
    assign: Callable[[str, Sequence[str]], Any]  # (site_id, device_ids) -> task response
    get_task: Callable[[str], Any]


def build_site_api(api: CatalystCenterAPI, samples: dict | None = None) -> SiteApi:
    """Connect the logic below to the SDK. `samples` remembers a first raw record (for --raw)."""

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


# ===========================================================================
# 3. Site names and the local site cache
# ===========================================================================


def normalize_site_name(text: str) -> str:
    """'Global / London/Building1/ ' -> 'global/london/building1' (for matching only)."""
    parts = [part.strip() for part in text.strip().split("/")]
    return "/".join(part for part in parts if part).lower()


def parse_sites(items: Iterable[Mapping[str, Any]]) -> list[dict[str, str]]:
    """Keep what the assignment needs from the controller's site records."""
    sites: list[dict[str, str]] = []
    for item in items:
        site_id = str(item.get("id") or "").strip()
        hierarchy = str(item.get("nameHierarchy") or item.get("siteNameHierarchy") or "").strip()
        if not site_id or not hierarchy:
            continue
        sites.append({
            "id": site_id,
            "name": str(item.get("name") or hierarchy.rsplit("/", 1)[-1]),
            "nameHierarchy": hierarchy,
            "type": str(item.get("type") or "").strip().lower(),
        })
    return sorted(sites, key=lambda site: site["nameHierarchy"].lower())


def site_type_counts(sites: Iterable[Mapping[str, str]]) -> str:
    counts: dict[str, int] = {}
    for site in sites:
        key = site.get("type") or "unknown"
        counts[key] = counts.get(key, 0) + 1
    return ", ".join("{} {}".format(counts[key], key) for key in sorted(counts)) or "none"


@dataclass(frozen=True)
class SiteCache:
    controller: str
    fetched_at: datetime
    sites: tuple[Mapping[str, str], ...]


# [main task] Read every site from the controller (paged) and keep it in a local file, so
# later runs do not need to ask for all sites again.
def refresh_site_cache(
    api: SiteApi,
    controller: str,
    path: Path,
    page_size: int = DEFAULT_PAGE_SIZE,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> SiteCache:
    sites = parse_sites(fetch_pages(api.list_sites, page_size))
    if not sites:
        raise ScriptError("The controller returned no sites, so the site list was not saved.")
    cache = SiteCache(controller, now(), tuple(sites))
    save_site_cache(path, cache)
    return cache


def save_site_cache(path: Path, cache: SiteCache) -> None:
    document = {
        "version": SITE_CACHE_VERSION,
        "controller": cache.controller,
        "fetched_at": cache.fetched_at.isoformat(),
        "sites": list(cache.sites),
    }
    # Write to a temporary file first, so a crash never leaves a half-written cache.
    temporary = path.with_name(path.name + ".tmp")
    try:
        temporary.write_text(json.dumps(document, indent=2), encoding="utf-8")
        os.replace(temporary, path)
    except OSError as exc:
        raise ScriptError("Could not save the site list to {}: {}".format(path, exc)) from exc


def load_site_cache(path: Path, controller: str) -> tuple[SiteCache | None, str]:
    """Return (cache, "") or (None, why-it-cannot-be-used). A missing file is not an error."""
    if not path.is_file():
        return None, "no site list has been saved yet"
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
        if document["version"] != SITE_CACHE_VERSION:
            return None, "the saved site list has an unknown format"
        if document["controller"] != controller:
            return None, "the saved site list is for another controller ({})".format(document["controller"])
        cache = SiteCache(
            controller, datetime.fromisoformat(document["fetched_at"]), tuple(document["sites"])
        )
    except (OSError, ValueError, KeyError, TypeError):
        return None, "the saved site list in {} cannot be read".format(path)
    return cache, ""


def cache_note(cache: SiteCache, now: datetime) -> str:
    age = (now - cache.fetched_at).days
    note = "Sites: using the list saved on {:%Y-%m-%d %H:%M} UTC ({} sites: {}).".format(
        cache.fetched_at.astimezone(timezone.utc), len(cache.sites), site_type_counts(cache.sites))
    if age >= SITE_CACHE_MAX_AGE_DAYS:
        note += " It is {} days old; consider --refresh-sites.".format(age)
    return note


# [error handling] A site name must match exactly one building or floor. When it does not,
# say why and suggest the closest names, and remind the user how to refresh the list.
def resolve_site(cache: SiteCache, name: str) -> Mapping[str, str]:
    wanted = normalize_site_name(name)
    matches = [site for site in cache.sites if normalize_site_name(site["nameHierarchy"]) == wanted]
    if not matches:
        usable = [s["nameHierarchy"] for s in cache.sites if s.get("type") in DEVICE_SITE_TYPES]
        close = difflib.get_close_matches(name.strip(), usable, n=3, cutoff=0.6)
        raise ScriptError(
            "Site {!r} is not in the saved site list ({:%Y-%m-%d %H:%M} UTC).{} If the site was "
            "created or renamed recently, run again with --refresh-sites.".format(
                name, cache.fetched_at.astimezone(timezone.utc),
                " Closest names: {}.".format("; ".join(close)) if close else "")
        )
    if len(matches) > 1:
        raise ScriptError(
            "Site {!r} matches {} sites ({}). Use the full name hierarchy.".format(
                name, len(matches), "; ".join(s["nameHierarchy"] for s in matches))
        )
    site = matches[0]
    # Only reject when the type is known: if the controller sent none, let it decide.
    if site.get("type") and site["type"] not in DEVICE_SITE_TYPES:
        raise ScriptError(
            "Site {!r} is {} {}; devices can only be assigned to a building or a floor.".format(
                name, "an" if site["type"][0] in "aeiou" else "a", site["type"])
        )
    return site


def resolve_sites(cache: SiteCache, names: Iterable[str]) -> dict[str, Mapping[str, str]]:
    """Resolve every distinct site name, reporting all the problems at once."""
    resolved: dict[str, Mapping[str, str]] = {}
    problems: list[str] = []
    for name in dict.fromkeys(names):
        try:
            resolved[name] = resolve_site(cache, name)
        except ScriptError as exc:
            problems.append("- {}".format(exc))
    if problems:
        raise ScriptError("Some sites could not be resolved:\n" + "\n".join(problems))
    return resolved


# ===========================================================================
# 4. The CSV with the devices
# ===========================================================================


def normalize_ip(value: str) -> str:
    text = value.strip()
    try:
        address = ipaddress.ip_address(text)
    except ValueError:
        raise ScriptError("Invalid IP address: {!r}".format(value)) from None
    if address.version != 4:
        raise ScriptError("Only IPv4 addresses are supported: {!r}".format(value))
    return str(address)


def read_device_sites(csv_path: Path, default_site: str | None = None) -> list[tuple[str, str]]:
    """(ip_address, site) pairs from the CSV. Rows with no site use default_site."""
    # [error handling] A missing column, an empty IP, a repeated IP, a row with no site or an
    # empty file stops the script with a clear message. "utf-8-sig" accepts Excel's byte-order mark.
    rows: list[tuple[str, str | None]] = []
    try:
        with open(csv_path, encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            columns = {(name or "").strip(): name for name in reader.fieldnames or []}
            if "ip_address" not in columns:
                raise ScriptError("CSV needs an 'ip_address' column in its header row")
            ip_column, site_column = columns["ip_address"], columns.get("site")
            for row in reader:
                ip = row.get(ip_column)
                if ip is None or not ip.strip():
                    raise ScriptError("CSV line {}: empty ip_address".format(reader.line_num))
                site = (row.get(site_column) or "").strip() if site_column else ""
                rows.append((normalize_ip(ip), site or default_site))
    except csv.Error as exc:
        raise ScriptError("Could not parse CSV: {}".format(exc)) from exc

    if not rows:
        raise ScriptError("CSV contains no devices")
    seen: set[str] = set()
    for ip, _ in rows:
        if ip in seen:
            raise ScriptError("IP address listed more than once: {}".format(ip))
        seen.add(ip)
    without_site = [ip for ip, site in rows if not site]
    if without_site:
        raise ScriptError(
            "No site for {} device(s): {}. Add a 'site' column to the CSV, or give --site.".format(
                len(without_site), ", ".join(without_site[:10]) + (" ..." if len(without_site) > 10 else ""))
        )
    return [(ip, str(site)) for ip, site in rows]


# ===========================================================================
# 5. Finding the devices, assigning them, and checking the result
# ===========================================================================


@dataclass
class AssignItem:
    ip: str
    site_name: str
    site_id: str = ""
    site_path: str = ""
    device_id: str = ""
    hostname: str = ""
    status: str = ""
    detail: str = ""


def _by_ip(devices: Iterable[Mapping[str, Any]]) -> dict[str, Mapping[str, Any]]:
    found: dict[str, Mapping[str, Any]] = {}
    for device in devices:
        try:
            found[normalize_ip(str(device.get("managementIpAddress") or ""))] = device
        except ScriptError:
            continue  # a record without a usable management IP cannot be matched
    return found


def lookup_inventory(api: SiteApi, ips: Sequence[str]) -> dict[str, Mapping[str, Any]]:
    """Inventory devices for these IPs, a few IPs per request (any device, assigned or not)."""
    found: dict[str, Mapping[str, Any]] = {}
    for start in range(0, len(ips), INVENTORY_LOOKUP_CHUNK):
        chunk = list(ips[start:start + INVENTORY_LOOKUP_CHUNK])
        found.update(_by_ip(fetch_pages(lambda offset, limit: api.list_devices_by_ip(chunk, offset, limit))))
    return found


# [main task] Find each CSV device cheaply: read the 'not assigned to a site' list once and
# match the IPs here, instead of asking about every IP. IPs missing from that list are looked
# up in the inventory, to tell "already has a site" from "not in the inventory (yet)".
def locate_devices(
    api: SiteApi,
    ips: Sequence[str],
    wait_seconds: float = 0,
    interval: float = ASSIGN_WAIT_INTERVAL_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> dict[str, tuple[str, Mapping[str, Any]]]:
    deadline = clock() + wait_seconds
    while True:
        unassigned = _by_ip(fetch_pages(api.list_unassigned))
        others = [ip for ip in ips if ip not in unassigned]
        inventory = lookup_inventory(api, others) if others else {}
        located: dict[str, tuple[str, Mapping[str, Any]]] = {}
        for ip in ips:
            if ip in unassigned:
                located[ip] = (STATUS_TO_ASSIGN, unassigned[ip])
            elif ip in inventory:
                located[ip] = (STATUS_ALREADY, inventory[ip])
            else:
                located[ip] = (STATUS_MISSING, {})
        # --wait-for-devices: right after a discovery, devices can take a while to appear.
        if all(status != STATUS_MISSING for status, _ in located.values()) or clock() >= deadline:
            return located
        sleep(min(interval, max(deadline - clock(), 0)))


def plan_items(
    rows: Sequence[tuple[str, str]],
    sites: Mapping[str, Mapping[str, str]],
    located: Mapping[str, tuple[str, Mapping[str, Any]]],
) -> list[AssignItem]:
    items: list[AssignItem] = []
    for ip, site_name in rows:
        status, device = located[ip]
        site = sites[site_name]
        item = AssignItem(
            ip=ip, site_name=site_name, site_id=site["id"], site_path=site["nameHierarchy"],
            device_id=str(device.get("id") or ""), hostname=str(device.get("hostname") or ""),
            status=status,
        )
        if status == STATUS_ALREADY:
            item.detail = "already has a site; devices are never moved"
        elif status == STATUS_MISSING:
            item.detail = "not in the inventory (is the discovery finished? try --wait-for-devices)"
        elif not item.device_id:
            item.status, item.detail = STATUS_FAILED, "the controller record has no device ID"
        items.append(item)
    return items


def chunked(values: Sequence[Any], size: int) -> list[list[Any]]:
    return [list(values[start:start + size]) for start in range(0, len(values), size)]


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


def wait_for_task(
    api: SiteApi,
    task_id: str,
    timeout: float,
    interval: float = POLL_INTERVAL_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> tuple[str, str]:
    """Wait for a task. Returns ("done" | "failed" | "pending", message)."""
    task, finished = _poll(
        lambda: api.get_task(task_id),
        lambda t: bool(_body(t).get("isError")) or bool(_body(t).get("endTime")),
        clock() + timeout, interval, sleep, clock)
    body = _body(task)
    if not finished:
        return "pending", "task {} is still running after {}s".format(task_id, int(timeout))
    if body.get("isError"):
        code = body.get("errorCode")
        reason = body.get("failureReason") or body.get("progress") or "no reason given"
        return "failed", "{}{}".format("[{}] ".format(code) if code else "", reason)
    return "done", ""


# [main task] Assign the devices: one request per site, at most 100 devices each, then wait
# for each request's task. A failed request does not stop the others.
def run_assignments(
    api: SiteApi,
    items: Sequence[AssignItem],
    timeout: float,
    no_wait: bool = False,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> None:
    by_site: dict[str, list[AssignItem]] = {}
    for item in items:
        if item.status == STATUS_TO_ASSIGN:
            by_site.setdefault(item.site_id, []).append(item)
    for site_id, group in by_site.items():
        for chunk in chunked(group, ASSIGN_BATCH_SIZE):
            # [error handling] Whatever goes wrong with this request is shown on its devices.
            try:
                response = api.assign(site_id, [item.device_id for item in chunk])
                task_id = task_id_from_response(response)
                if not task_id:
                    raise ScriptError("the response has no task ID: {}".format(
                        json.dumps(redact(response), default=str)))
                if no_wait:
                    state, message = "submitted", "task {}".format(task_id)
                else:
                    state, message = wait_for_task(api, task_id, timeout, sleep=sleep, clock=clock)
            except Exception as exc:
                state, message = "failed", "{}: {}".format(type(exc).__name__, exc)
            status = {"done": STATUS_TASK_DONE, "submitted": STATUS_SUBMITTED,
                      "pending": STATUS_PENDING, "failed": STATUS_FAILED}[state]
            for item in chunk:
                item.status, item.detail = status, message


# Success is only reported if the devices really left the 'not assigned to a site' list.
def verify_assignments(api: SiteApi, items: Sequence[AssignItem]) -> None:
    waiting = [item for item in items if item.status == STATUS_TASK_DONE]
    if not waiting:
        return
    still_unassigned = {str(device.get("id")) for device in fetch_pages(api.list_unassigned)}
    for item in waiting:
        if item.device_id in still_unassigned:
            item.status = STATUS_FAILED
            item.detail = "the task finished, but the device is still not assigned to a site"
        else:
            item.status, item.detail = STATUS_ASSIGNED, ""


def assignment_report(items: Sequence[AssignItem]) -> tuple[list[str], bool]:
    """One line per device, a summary line, and whether everything is fine."""
    lines = []
    for item in items:
        lines.append("- {ip:<15} {host:<20} {site}  {status}{detail}".format(
            ip=item.ip, host=item.hostname or "-", site=item.site_path or item.site_name,
            status=item.status, detail=": " + item.detail if item.detail else ""))
    counts: dict[str, int] = {}
    for item in items:
        counts[item.status] = counts.get(item.status, 0) + 1
    summary = ", ".join("{}: {}".format(status, counts[status]) for status in sorted(counts))
    return lines + ["{} device(s). {}".format(len(items), summary)], all(
        item.status in _OK_STATUSES for item in items)


# ===========================================================================
# 6. Main flow
# ===========================================================================


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
        settings = load_settings()
        rows = read_device_sites(args.csv_file, args.site) if args.csv_file else None

        # Lab only: prints a warning when certificate checking is switched off in .env.
        warn_if_insecure(settings)
        # Step 2 [credentials]: log in (see connect).
        api = connect(settings)
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
    except (ScriptError, catalystcentersdkException, requests.RequestException, OSError) as exc:
        print("Error: {}".format(exc), file=sys.stderr)
        return 1

    lines, ok = assignment_report(items)
    print("\n".join(lines))
    if args.no_wait and not args.dry_run:
        print("Not waiting for results (--no-wait). Check each task with: python check_task.py <task-id>")
    if args.raw:
        print(json.dumps(redact(samples), indent=2, default=str))
    print(SITE_REFRESH_REMINDER)
    # Exit code 0 only if every device is assigned, already assigned (or would be, in a dry run).
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
