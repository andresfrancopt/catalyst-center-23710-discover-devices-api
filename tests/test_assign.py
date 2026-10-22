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

"""Tests for assigning devices to a site: shared logic, the root script and the standalone script."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from collections import Counter
from contextlib import redirect_stderr
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import assign_devices_to_site as root_assign
import discovery_common as common
from catalystcentersdk.exceptions import catalystcentersdkException
from test_discovery_common import FakeTime
from test_standalone import API_ENV, load, patched_env, run_main

sa = load("assign_site.py")

SITES = [
    {"id": "s-global", "name": "Global", "nameHierarchy": "Global", "type": "area"},
    {"id": "s-uk", "name": "London", "nameHierarchy": "Global/London", "type": "area"},
    {"id": "s-b1", "name": "Building1", "nameHierarchy": "Global/London/Building1", "type": "building"},
    {"id": "s-f1", "name": "Floor1", "nameHierarchy": "Global/London/Building1/Floor1", "type": "floor"},
    {"id": "s-b2", "name": "Building2", "nameHierarchy": "Global/London/Building2", "type": "building"},
]


def make_devices(count: int = 6, assigned: tuple[int, ...] = (3,)) -> list[dict]:
    return [
        {"id": "dev-{}".format(i), "hostname": "sw-{}".format(i),
         "managementIpAddress": "10.0.0.{}".format(i), "site": "s-b2" if i in assigned else None}
        for i in range(1, count + 1)
    ]


class FakeController:
    """A small in-memory Catalyst Center: sites, devices, an assignment task."""

    def __init__(self, sites=None, devices=None) -> None:
        self.sites = [dict(s) for s in (SITES if sites is None else sites)]
        self.devices = make_devices() if devices is None else devices
        self.calls: Counter = Counter()
        self.assign_calls: list[tuple[str, list[str]]] = []
        self.pending: dict[str, tuple[str, list[str]]] = {}
        self.task_mode = "ok"  # ok | error | running
        self.apply_assignment = True  # False: the task "succeeds" but nothing changes
        self.assign_error: Exception | None = None
        self.no_task_id = False
        self.first_unassigned_call_seen = False

    @staticmethod
    def page(records, offset, limit):
        return records[offset - 1: offset - 1 + limit]

    def list_sites(self, offset, limit):
        self.calls["sites"] += 1
        return [dict(s) for s in self.page(self.sites, offset, limit)]

    def _unassigned(self):
        return [d for d in self.devices if not d["site"]]

    def list_unassigned(self, offset, limit):
        self.calls["unassigned"] += 1
        return [{"id": d["id"], "hostname": d["hostname"], "managementIpAddress": d["managementIpAddress"]}
                for d in self.page(self._unassigned(), offset, limit)]

    def list_devices_by_ip(self, ips, offset, limit):
        self.calls["inventory"] += 1
        wanted = set(ips)
        matches = [d for d in self.devices if d["managementIpAddress"] in wanted]
        return [dict(d) for d in self.page(matches, offset, limit)]

    def assign(self, site_id, device_ids):
        self.calls["assign"] += 1
        self.assign_calls.append((site_id, list(device_ids)))
        if self.assign_error:
            raise self.assign_error
        if self.no_task_id:
            return {"response": {}}
        task_id = "task-{}".format(len(self.assign_calls))
        self.pending[task_id] = (site_id, list(device_ids))
        return {"response": {"taskId": task_id, "url": "/x"}}

    def get_task(self, task_id):
        self.calls["task"] += 1
        if self.task_mode == "error":
            return {"response": {"isError": True, "errorCode": "E42", "failureReason": "not allowed", "endTime": 2}}
        if self.task_mode == "running":
            return {"response": {"isError": False, "progress": "Working"}}
        site_id, ids = self.pending[task_id]
        if self.apply_assignment:
            for device in self.devices:
                if device["id"] in ids:
                    device["site"] = site_id
        return {"response": {"isError": False, "endTime": 2, "progress": "done"}}

    def site_api(self) -> common.SiteApi:
        return common.SiteApi(self.list_sites, self.list_unassigned, self.list_devices_by_ip,
                              self.assign, self.get_task)

    def sdk_mock(self) -> MagicMock:
        """A stand-in for CatalystCenterAPI whose methods are routed to this fake."""
        api = MagicMock()
        api.site_design.get_sites.side_effect = lambda offset, limit: {"response": self.list_sites(offset, limit)}
        api.site_design.get_site_not_assigned_network_devices.side_effect = (
            lambda offset, limit: {"response": self.list_unassigned(offset, limit)})
        api.devices.get_device_list.side_effect = (
            lambda management_ip_address, offset, limit: {
                "response": self.list_devices_by_ip(management_ip_address, offset, limit)})
        api.site_design.assign_network_devices_to_a_site.side_effect = (
            lambda deviceIds, siteId: self.assign(siteId, deviceIds))
        api.task.get_task_by_id.side_effect = lambda task_id: self.get_task(task_id)
        return api


def cache_from(sites=SITES, fetched_at=None) -> common.SiteCache:
    return common.SiteCache(
        "https://catalyst.example.test",
        fetched_at or datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc),
        tuple(common.parse_sites(sites)))


class SiteNameTests(unittest.TestCase):
    modules = (common, sa)

    def test_normalising(self) -> None:
        for mod in self.modules:
            for raw, expected in (("Global/London/Building1", "global/london/building1"),
                                  ("  Global / London/ Building1 /", "global/london/building1"),
                                  ("GLOBAL//london", "global/london"), ("", ""), ("/", "")):
                self.assertEqual(mod.normalize_site_name(raw), expected)

    def test_parse_sites_keeps_what_matters_and_skips_junk(self) -> None:
        raw = [{"id": "b", "nameHierarchy": "Global/B", "type": "BUILDING"},
               {"id": "a", "name": "A", "nameHierarchy": "Global/A", "type": "area", "extra": 1},
               {"nameHierarchy": "Global/no-id"}, {"id": "no-name"}, {"id": "c", "siteNameHierarchy": "Global/C"}]
        for mod in self.modules:
            sites = mod.parse_sites(raw)
            self.assertEqual([s["nameHierarchy"] for s in sites], ["Global/A", "Global/B", "Global/C"])
            self.assertEqual(sites[1], {"id": "b", "name": "B", "nameHierarchy": "Global/B", "type": "building"})
            self.assertEqual(sites[2]["type"], "")
            self.assertEqual(mod.site_type_counts(sites), "1 area, 1 building, 1 unknown")

    def test_resolving_ignores_spacing_case_and_trailing_slash(self) -> None:
        for mod, error in ((common, common.ConfigError), (sa, sa.ScriptError)):
            cache = mod.SiteCache("c", datetime(2026, 10, 1, tzinfo=timezone.utc), tuple(mod.parse_sites(SITES)))
            for name in ("Global/London/Building1/Floor1", " global / london/BUILDING1 /floor1/"):
                self.assertEqual(mod.resolve_site(cache, name)["id"], "s-f1")
            self.assertTrue(issubclass(error, RuntimeError))

    def test_unknown_site_message_helps(self) -> None:
        for mod in self.modules:
            cache = mod.SiteCache("c", datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc), tuple(mod.parse_sites(SITES)))
            with self.assertRaises(RuntimeError) as ctx:
                mod.resolve_site(cache, "Global/London/Buildin1")
            message = str(ctx.exception)
            self.assertIn("2026-10-01 12:00 UTC", message)
            self.assertIn("Closest names: Global/London/Building1", message)
            self.assertIn("--refresh-sites", message)
            with self.assertRaises(RuntimeError) as ctx:
                mod.resolve_site(cache, "Nowhere/Near")
            self.assertNotIn("Closest", str(ctx.exception))

    def test_areas_are_rejected_but_unknown_types_are_left_to_the_controller(self) -> None:
        for mod in self.modules:
            cache = mod.SiteCache("c", datetime(2026, 10, 1, tzinfo=timezone.utc), tuple(
                mod.parse_sites(SITES + [{"id": "s-x", "nameHierarchy": "Global/Mystery"}])))
            with self.assertRaisesRegex(RuntimeError, "is an area.*building or a floor"):
                mod.resolve_site(cache, "Global/London")
            self.assertEqual(mod.resolve_site(cache, "Global/Mystery")["id"], "s-x")

    def test_ambiguous_names_are_rejected_and_problems_are_reported_together(self) -> None:
        for mod in self.modules:
            sites = SITES + [{"id": "s-dup", "nameHierarchy": "global/london/BUILDING1", "type": "building"}]
            cache = mod.SiteCache("c", datetime(2026, 10, 1, tzinfo=timezone.utc), tuple(mod.parse_sites(sites)))
            with self.assertRaisesRegex(RuntimeError, "matches 2 sites"):
                mod.resolve_site(cache, "Global/London/Building1")
            with self.assertRaises(RuntimeError) as ctx:
                mod.resolve_sites(cache, ["Global/London/Building2", "Nope/1", "Global/London", "Nope/1"])
            text = str(ctx.exception)
            self.assertIn("Nope/1", text)
            self.assertIn("is an area", text)
            self.assertEqual(text.count("Nope/1"), 1)  # a repeated name is reported once
            self.assertEqual(mod.resolve_sites(cache, ["Global/London/Building2"])["Global/London/Building2"]["id"], "s-b2")


class SiteCacheTests(unittest.TestCase):
    def test_round_trip_and_compatibility_between_the_two_scripts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sites_cache.json"
            cache = cache_from()
            common.save_site_cache(path, cache)
            self.assertFalse((Path(tmp) / "sites_cache.json.tmp").exists())
            for mod in (common, sa):
                loaded, why = mod.load_site_cache(path, "https://catalyst.example.test")
                self.assertEqual(why, "")
                self.assertEqual(loaded.fetched_at, cache.fetched_at)
                self.assertEqual([dict(s) for s in loaded.sites], [dict(s) for s in cache.sites])
            sa.save_site_cache(path, sa.SiteCache(cache.controller, cache.fetched_at, cache.sites))
            self.assertIsNotNone(common.load_site_cache(path, cache.controller)[0])

    def test_unusable_caches_explain_why(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "c.json"
            for mod in (common, sa):
                path.unlink(missing_ok=True)
                self.assertIn("no site list", mod.load_site_cache(path, "x")[1])
                path.write_text("{not json", encoding="utf-8")
                self.assertIn("cannot be read", mod.load_site_cache(path, "x")[1])
                path.write_text(json.dumps({"version": 99}), encoding="utf-8")
                self.assertIn("unknown format", mod.load_site_cache(path, "x")[1])
                path.write_text(json.dumps({"version": 1, "controller": "https://other", "fetched_at": "x",
                                            "sites": []}), encoding="utf-8")
                self.assertIn("another controller (https://other)", mod.load_site_cache(path, "x")[1])
                path.write_text(json.dumps({"version": 1, "controller": "x", "fetched_at": "garbage",
                                            "sites": []}), encoding="utf-8")
                self.assertIn("cannot be read", mod.load_site_cache(path, "x")[1])

    def test_refresh_pages_through_every_site_and_saves(self) -> None:
        many = [{"id": "s{}".format(i), "name": "n{}".format(i), "nameHierarchy": "Global/n{}".format(i),
                 "type": "building"} for i in range(1203)]
        for mod in (common, sa):
            fake = FakeController(sites=many)
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "c.json"
                fixed = datetime(2026, 10, 22, 9, 0, tzinfo=timezone.utc)
                cache = mod.refresh_site_cache(fake.site_api() if mod is common else
                                               sa.SiteApi(fake.list_sites, fake.list_unassigned,
                                                          fake.list_devices_by_ip, fake.assign, fake.get_task),
                                               "https://c", path, now=lambda: fixed)
                self.assertEqual(len(cache.sites), 1203)
                self.assertEqual(fake.calls["sites"], 3)  # 500 + 500 + 203
                self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["fetched_at"], fixed.isoformat())

    def test_an_empty_site_list_is_not_saved(self) -> None:
        fake = FakeController(sites=[])
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "c.json"
            with self.assertRaisesRegex(common.ConfigError, "no sites"):
                common.refresh_site_cache(fake.site_api(), "https://c", path)
            self.assertFalse(path.exists())

    def test_a_cache_that_cannot_be_written_is_explained(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(common.ConfigError, "Could not save"):
                common.save_site_cache(Path(tmp) / "no-such-folder" / "c.json", cache_from())

    def test_old_caches_get_a_nudge(self) -> None:
        now = datetime(2026, 10, 22, 12, 0, tzinfo=timezone.utc)
        for mod in (common, sa):
            fresh = mod.SiteCache("c", now - timedelta(days=1), tuple(mod.parse_sites(SITES)))
            old = mod.SiteCache("c", now - timedelta(days=9), tuple(mod.parse_sites(SITES)))
            self.assertNotIn("consider --refresh-sites", mod.cache_note(fresh, now))
            self.assertIn("It is 9 days old; consider --refresh-sites.", mod.cache_note(old, now))
            self.assertIn("5 sites: 2 area, 2 building, 1 floor", mod.cache_note(fresh, now))


class CsvTests(unittest.TestCase):
    def read(self, text: str, mod, default=None):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "d.csv"
            path.write_text(text, encoding="utf-8")
            return mod.read_device_sites(path, default)

    def test_reads_the_discovery_csv_and_ignores_other_columns(self) -> None:
        text = ("\ufeffip_address,credential_profile, site \n"
                "10.0.0.1,default, Global/London/Building1/Floor1 \n10.0.0.2,branch,Global/London/Building2\n")
        for mod in (common, sa):
            self.assertEqual(self.read(text, mod), [("10.0.0.1", "Global/London/Building1/Floor1"),
                                                    ("10.0.0.2", "Global/London/Building2")])

    def test_default_site_fills_in_the_gaps(self) -> None:
        text = "ip_address,site\n10.0.0.1,\n10.0.0.2,Global/London/Building2\n"
        for mod in (common, sa):
            self.assertEqual(self.read(text, mod, "Global/London/Building1"),
                             [("10.0.0.1", "Global/London/Building1"), ("10.0.0.2", "Global/London/Building2")])
            self.assertEqual(self.read("ip_address\n10.0.0.1\n", mod, "Global/X"), [("10.0.0.1", "Global/X")])

    def test_problems_are_reported_before_anything_runs(self) -> None:
        cases = (("ip_address,site\n10.0.0.1,\n10.0.0.2,\n", "No site for 2 device\\(s\\): 10.0.0.1, 10.0.0.2"),
                 ("ip_address\n10.0.0.1\n", "No site for 1"), ("address,site\n1,x\n", "ip_address"),
                 ("ip_address,site\n10.0.0.1,x\n10.0.0.1,y\n", "more than once"),
                 ("ip_address,site\nnot-an-ip,x\n", "Invalid IP address"), ("ip_address,site\n", "no devices"),
                 ("ip_address,site\n ,x\n", "empty ip_address"), ("ip_address,site\n2001:db8::1,x\n", "IPv4"))
        for mod in (common, sa):
            for text, pattern in cases:
                with self.subTest(module=mod.__name__, text=text), self.assertRaisesRegex(RuntimeError, pattern):
                    self.read(text, mod)


class PagingParityTests(unittest.TestCase):
    def test_same_behavior_as_the_shared_helper(self) -> None:
        records = [{"n": i} for i in range(5)]
        for mod in (common, sa):
            calls = []

            def fetch(offset, limit):
                calls.append(offset)
                return records[offset - 1: offset - 1 + limit]

            self.assertEqual(mod.fetch_pages(fetch, 2), records)
            self.assertEqual(calls, [1, 3, 5])
            with self.assertRaisesRegex(RuntimeError, "same page twice"):
                mod.fetch_pages(lambda offset, limit: [{"n": 1}, {"n": 2}], 2)
            self.assertEqual(mod.fetch_pages(lambda offset, limit: [], 2), [])


def modules_with_apis():
    """(module, build-a-SiteApi-from-a-FakeController) for the shared code and the standalone copy."""
    return (
        (common, lambda fake: fake.site_api()),
        (sa, lambda fake: sa.SiteApi(fake.list_sites, fake.list_unassigned, fake.list_devices_by_ip,
                                     fake.assign, fake.get_task)),
    )


class LocateAndPlanTests(unittest.TestCase):
    def test_finds_unassigned_devices_with_one_list_and_separates_the_rest(self) -> None:
        for mod, make_api in modules_with_apis():
            fake = FakeController()
            located = mod.locate_devices(make_api(fake), ["10.0.0.1", "10.0.0.3", "10.0.0.99", "10.0.0.2"])
            self.assertEqual({ip: status for ip, (status, _) in located.items()},
                             {"10.0.0.1": "TO ASSIGN", "10.0.0.3": "ALREADY ASSIGNED",
                              "10.0.0.99": "NOT IN INVENTORY", "10.0.0.2": "TO ASSIGN"})
            self.assertEqual(located["10.0.0.1"][1]["id"], "dev-1")
            self.assertEqual(fake.calls["unassigned"], 1)  # one read of the list, not one per IP
            self.assertEqual(fake.calls["inventory"], 1)

    def test_nothing_is_looked_up_when_every_device_is_unassigned(self) -> None:
        for mod, make_api in modules_with_apis():
            fake = FakeController()
            mod.locate_devices(make_api(fake), ["10.0.0.1", "10.0.0.2"])
            self.assertEqual(fake.calls["inventory"], 0)

    def test_inventory_lookups_are_chunked(self) -> None:
        devices = [{"id": "d{}".format(i), "hostname": "h", "managementIpAddress": "10.1.{}.{}".format(i // 200, i % 200 + 1),
                    "site": "s-b1"} for i in range(120)]
        ips = [d["managementIpAddress"] for d in devices]
        for mod, make_api in modules_with_apis():
            fake = FakeController(devices=devices)
            located = mod.locate_devices(make_api(fake), ips)
            self.assertEqual({status for status, _ in located.values()}, {"ALREADY ASSIGNED"})
            self.assertEqual(fake.calls["inventory"], 3)  # 50 + 50 + 20 IPs

    def test_waiting_for_devices_that_appear_later(self) -> None:
        for mod, make_api in modules_with_apis():
            fake = FakeController()
            clock = FakeTime()
            original = fake.list_unassigned
            state = {"polls": 0}

            def delayed(offset, limit, original=original, state=state, fake=fake):
                state["polls"] += 1
                if state["polls"] == 2:  # the device shows up between the first and second look
                    fake.devices.append({"id": "dev-new", "hostname": "new", "managementIpAddress": "10.0.0.50", "site": None})
                return original(offset, limit)

            api = make_api(fake)
            api = type(api)(**{**api.__dict__, "list_unassigned": delayed})
            located = mod.locate_devices(api, ["10.0.0.50"], 60, 10, clock.sleep, clock.clock)
            self.assertEqual(located["10.0.0.50"][0], "TO ASSIGN")
            self.assertEqual(clock.sleeps, [10])

    def test_waiting_gives_up_at_the_deadline(self) -> None:
        for mod, make_api in modules_with_apis():
            clock = FakeTime()
            located = mod.locate_devices(make_api(FakeController()), ["10.0.0.99"], 25, 10, clock.sleep, clock.clock)
            self.assertEqual(located["10.0.0.99"][0], "NOT IN INVENTORY")
            self.assertEqual(clock.sleeps, [10, 10, 5])

    def test_no_waiting_by_default(self) -> None:
        for mod, make_api in modules_with_apis():
            clock = FakeTime()
            mod.locate_devices(make_api(FakeController()), ["10.0.0.99"], sleep=clock.sleep, clock=clock.clock)
            self.assertEqual(clock.sleeps, [])

    def test_the_plan_explains_each_outcome(self) -> None:
        for mod, make_api in modules_with_apis():
            fake = FakeController()
            cache = mod.SiteCache("c", datetime(2026, 10, 1, tzinfo=timezone.utc), tuple(mod.parse_sites(SITES)))
            rows = [("10.0.0.1", "Global/London/Building1"), ("10.0.0.3", "Global/London/Building1"),
                    ("10.0.0.99", "Global/London/Building2")]
            located = mod.locate_devices(make_api(fake), [ip for ip, _ in rows])
            items = mod.plan_items(rows, mod.resolve_sites(cache, [s for _, s in rows]), located)
            self.assertEqual([i.status for i in items], ["TO ASSIGN", "ALREADY ASSIGNED", "NOT IN INVENTORY"])
            self.assertEqual((items[0].device_id, items[0].hostname, items[0].site_id, items[0].site_path),
                             ("dev-1", "sw-1", "s-b1", "Global/London/Building1"))
            self.assertIn("never moved", items[1].detail)
            self.assertIn("--wait-for-devices", items[2].detail)

    def test_a_record_without_an_id_cannot_be_assigned(self) -> None:
        for mod, _ in modules_with_apis():
            cache = mod.SiteCache("c", datetime(2026, 10, 1, tzinfo=timezone.utc), tuple(mod.parse_sites(SITES)))
            located = {"10.0.0.1": ("TO ASSIGN", {"hostname": "x"})}
            item = mod.plan_items([("10.0.0.1", "Global/London/Building1")],
                                  mod.resolve_sites(cache, ["Global/London/Building1"]), located)[0]
            self.assertEqual(item.status, "FAILED")


def items_for(mod, count: int, site_id: str = "s-b1", status: str = "TO ASSIGN"):
    return [mod.AssignItem(ip="10.9.0.{}".format(i), site_name="x", site_id=site_id, site_path="Global/x",
                           device_id="dev-{}-{}".format(site_id, i), hostname="h{}".format(i), status=status)
            for i in range(count)]


class AssignmentTests(unittest.TestCase):
    def test_devices_go_in_batches_of_at_most_100_per_site(self) -> None:
        for mod, make_api in modules_with_apis():
            fake = FakeController(devices=[])
            items = items_for(mod, 250, "s-b1") + items_for(mod, 3, "s-b2")
            # no-wait: only count the requests (the fake has no such devices to apply)
            mod.run_assignments(make_api(fake), items, 60, no_wait=True)
            self.assertEqual([(site, len(ids)) for site, ids in fake.assign_calls],
                             [("s-b1", 100), ("s-b1", 100), ("s-b1", 50), ("s-b2", 3)])
            self.assertTrue(all(i.status == "SUBMITTED" and i.detail.startswith("task task-") for i in items))

    def test_a_failed_task_marks_only_its_own_devices(self) -> None:
        for mod, make_api in modules_with_apis():
            fake = FakeController()
            fake.task_mode = "error"
            items = items_for(mod, 2, "s-b1")
            mod.run_assignments(make_api(fake), items, 60)
            self.assertEqual({i.status for i in items}, {"FAILED"})
            self.assertEqual({i.detail for i in items}, {"[E42] not allowed"})

    def test_one_bad_request_does_not_stop_the_other_sites(self) -> None:
        for mod, make_api in modules_with_apis():
            fake = FakeController()
            real_assign = fake.assign

            def flaky(site_id, ids, real_assign=real_assign):
                if site_id == "s-b1":
                    raise catalystcentersdkException("boom")
                return real_assign(site_id, ids)

            api = make_api(fake)
            api = type(api)(**{**api.__dict__, "assign": flaky})
            fake.devices = make_devices()
            ok_items = [mod.AssignItem(ip="10.0.0.2", site_name="b2", site_id="s-b2", device_id="dev-2",
                                       status="TO ASSIGN")]
            bad_items = [mod.AssignItem(ip="10.0.0.1", site_name="b1", site_id="s-b1", device_id="dev-1",
                                        status="TO ASSIGN")]
            mod.run_assignments(api, bad_items + ok_items, 60)
            self.assertEqual(bad_items[0].status, "FAILED")
            self.assertIn("catalystcentersdkException: boom", bad_items[0].detail)
            self.assertEqual(ok_items[0].status, "TASK DONE")

    def test_a_response_without_a_task_id_is_a_failure(self) -> None:
        for mod, make_api in modules_with_apis():
            fake = FakeController()
            fake.no_task_id = True
            items = items_for(mod, 1)
            mod.run_assignments(make_api(fake), items, 60)
            self.assertEqual(items[0].status, "FAILED")
            self.assertIn("no task ID", items[0].detail)

    def test_a_task_that_never_finishes_is_pending(self) -> None:
        for mod, make_api in modules_with_apis():
            fake = FakeController()
            fake.task_mode = "running"
            clock = FakeTime()
            items = items_for(mod, 1)
            mod.run_assignments(make_api(fake), items, 12, sleep=clock.sleep, clock=clock.clock)
            self.assertEqual(items[0].status, "PENDING")
            self.assertIn("still running after 12s", items[0].detail)

    def test_only_devices_in_the_plan_are_sent(self) -> None:
        for mod, make_api in modules_with_apis():
            fake = FakeController()
            items = items_for(mod, 2, status="ALREADY ASSIGNED") + items_for(mod, 1, "s-b2", "NOT IN INVENTORY")
            mod.run_assignments(make_api(fake), items, 60)
            self.assertEqual(fake.assign_calls, [])

    def test_success_needs_the_device_to_leave_the_unassigned_list(self) -> None:
        for mod, make_api in modules_with_apis():
            for applies, expected in ((True, "ASSIGNED"), (False, "FAILED")):
                fake = FakeController()
                fake.apply_assignment = applies
                items = [mod.AssignItem(ip="10.0.0.1", site_name="b1", site_id="s-b1", device_id="dev-1",
                                        status="TO ASSIGN")]
                mod.run_assignments(make_api(fake), items, 60)
                mod.verify_assignments(make_api(fake), items)
                self.assertEqual(items[0].status, expected)
                if not applies:
                    self.assertIn("still not assigned", items[0].detail)

    def test_nothing_to_verify_means_no_calls(self) -> None:
        for mod, make_api in modules_with_apis():
            fake = FakeController()
            mod.verify_assignments(make_api(fake), items_for(mod, 2, status="FAILED"))
            self.assertEqual(fake.calls["unassigned"], 0)

    def test_report_lines_summary_and_overall_result(self) -> None:
        for mod, _ in modules_with_apis():
            items = items_for(mod, 2, status="ASSIGNED") + items_for(mod, 1, status="ALREADY ASSIGNED")
            lines, ok = mod.assignment_report(items)
            self.assertTrue(ok)
            self.assertIn("3 device(s). ALREADY ASSIGNED: 1, ASSIGNED: 2", lines[-1])
            items[0].status, items[0].detail = "FAILED", "[E1] nope"
            lines, ok = mod.assignment_report(items)
            self.assertFalse(ok)
            self.assertIn("FAILED: [E1] nope", lines[0])
            self.assertFalse(mod.assignment_report(items_for(mod, 1, status="NOT IN INVENTORY"))[1])
            self.assertFalse(mod.assignment_report(items_for(mod, 1, status="PENDING"))[1])
            self.assertTrue(mod.assignment_report(items_for(mod, 1, status="TO ASSIGN"))[1])
            self.assertTrue(mod.assignment_report(items_for(mod, 1, status="SUBMITTED"))[1])


class ScriptCase(unittest.TestCase):
    """Runs the root script and the standalone script against the same fake controller."""

    def setUp(self) -> None:
        patched_env(self, sa, API_ENV)
        loader = patch.object(root_assign, "load_settings",
                              side_effect=lambda require_profiles=False: common.load_settings(
                                  API_ENV, require_profiles=False))
        loader.start()
        self.addCleanup(loader.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name)

    def csv(self, text: str) -> str:
        path = self.folder / "devices.csv"
        path.write_text(text, encoding="utf-8")
        return str(path)

    @property
    def cache_path(self) -> Path:
        return self.folder / "sites_cache.json"

    def run_script(self, mod, fake: FakeController, argv):
        with patch.object(mod, "CatalystCenterAPI") as sdk:
            sdk.return_value = fake.sdk_mock()
            code, out, err = run_main(mod, ["--sites-cache", str(self.cache_path), *argv])
        return code, out, err

    scripts = (root_assign, sa)


GOOD_CSV = ("ip_address,credential_profile,site\n"
            "10.0.0.1,default,Global/London/Building1/Floor1\n10.0.0.2,default,Global/London/Building1\n")


class AssignScriptTests(ScriptCase):
    def test_refresh_sites_alone_saves_the_list_and_stops(self) -> None:
        for mod in self.scripts:
            self.cache_path.unlink(missing_ok=True)
            fake = FakeController()
            code, out, err = self.run_script(mod, fake, ["--refresh-sites"])
            with self.subTest(module=mod.__name__):
                self.assertEqual(code, 0, err)
                self.assertIn("Saved 5 sites (2 area, 2 building, 1 floor)", out)
                self.assertIn("--refresh-sites", out)  # the reminder
                self.assertEqual(fake.calls["unassigned"], 0)
                self.assertTrue(self.cache_path.is_file())

    def test_the_cache_is_used_so_sites_are_not_read_again(self) -> None:
        for mod in self.scripts:
            self.cache_path.unlink(missing_ok=True)
            fake = FakeController()
            self.run_script(mod, fake, [self.csv(GOOD_CSV), "--dry-run"])
            first = fake.calls["sites"]
            code, out, err = self.run_script(mod, fake, [self.csv(GOOD_CSV), "--dry-run"])
            with self.subTest(module=mod.__name__):
                self.assertEqual(first, 1)
                self.assertEqual(fake.calls["sites"], 1)  # the second run did not ask for sites
                self.assertEqual(code, 0, err)
                self.assertIn("Sites: using the list saved on", out)

    def test_refresh_sites_reads_them_again_even_with_a_cache(self) -> None:
        for mod in self.scripts:
            self.cache_path.unlink(missing_ok=True)
            fake = FakeController()
            self.run_script(mod, fake, ["--refresh-sites"])
            fake.sites.append({"id": "s-f2", "name": "Floor2", "nameHierarchy": "Global/London/Building2/Floor2",
                               "type": "floor"})
            code, out, _ = self.run_script(mod, fake, [self.csv("ip_address,site\n10.0.0.1,Global/London/Building2/Floor2\n"),
                                                       "--refresh-sites", "--dry-run"])
            with self.subTest(module=mod.__name__):
                self.assertEqual(fake.calls["sites"], 2)
                self.assertEqual(code, 0, out)
                self.assertIn("Global/London/Building2/Floor2  TO ASSIGN", out)

    def test_a_new_site_is_not_found_until_the_list_is_refreshed(self) -> None:
        for mod in self.scripts:
            self.cache_path.unlink(missing_ok=True)
            fake = FakeController()
            self.run_script(mod, fake, ["--refresh-sites"])
            fake.sites.append({"id": "s-f2", "name": "Floor2", "nameHierarchy": "Global/London/Building2/Floor2",
                               "type": "floor"})
            code, out, err = self.run_script(mod, fake, [self.csv(
                "ip_address,site\n10.0.0.1,Global/London/Building2/Floor2\n"), "--dry-run"])
            with self.subTest(module=mod.__name__):
                self.assertEqual(code, 1)
                self.assertIn("not in the saved site list", err)
                self.assertIn("--refresh-sites", err)
                self.assertEqual(fake.calls["sites"], 1)  # no silent refresh
                self.assertEqual(fake.calls["unassigned"], 0)  # stopped before looking at devices

    def test_dry_run_changes_nothing_and_shows_the_plan(self) -> None:
        text = GOOD_CSV + "10.0.0.3,default,Global/London/Building1\n10.0.0.4,default,Global/London/Building2\n"
        for mod in self.scripts:
            self.cache_path.unlink(missing_ok=True)
            fake = FakeController()
            code, out, err = self.run_script(mod, fake, [self.csv(text), "--dry-run"])
            with self.subTest(module=mod.__name__):
                self.assertEqual(code, 0, err)
                self.assertEqual(fake.assign_calls, [])
                self.assertIn("Dry run: nothing is assigned", out)
                self.assertIn("- {:<15} {:<20} {}  TO ASSIGN".format(
                    "10.0.0.1", "sw-1", "Global/London/Building1/Floor1"), out)
                self.assertIn("ALREADY ASSIGNED: already has a site; devices are never moved", out)
                self.assertIn("4 device(s). ALREADY ASSIGNED: 1, TO ASSIGN: 3", out)
                self.assertTrue(out.rstrip().endswith("--refresh-sites."))  # the reminder is always last

    def test_a_device_that_is_not_in_the_inventory_makes_the_run_fail(self) -> None:
        for mod in self.scripts:
            self.cache_path.unlink(missing_ok=True)
            fake = FakeController()
            code, out, _ = self.run_script(mod, fake, [self.csv(GOOD_CSV + "10.0.0.99,default,Global/London/Building1\n"),
                                                       "--dry-run"])
            with self.subTest(module=mod.__name__):
                self.assertEqual(code, 1)
                self.assertIn("NOT IN INVENTORY: not in the inventory", out)

    def test_assigns_follows_the_task_and_verifies(self) -> None:
        for mod in self.scripts:
            self.cache_path.unlink(missing_ok=True)
            fake = FakeController()
            code, out, err = self.run_script(mod, fake, [self.csv(GOOD_CSV)])
            with self.subTest(module=mod.__name__):
                self.assertEqual(code, 0, out + err)
                self.assertEqual(fake.assign_calls, [("s-f1", ["dev-1"]), ("s-b1", ["dev-2"])])
                self.assertEqual({d["id"]: d["site"] for d in fake.devices if d["id"] in ("dev-1", "dev-2")},
                                 {"dev-1": "s-f1", "dev-2": "s-b1"})
                self.assertIn("ASSIGNED", out)
                self.assertNotIn("TASK DONE", out)
                self.assertIn("2 device(s). ASSIGNED: 2", out)
                self.assertTrue(out.rstrip().endswith("--refresh-sites."))
            for device in fake.devices:  # reset for the next module
                device["site"] = "s-b2" if device["id"] == "dev-3" else None

    def test_a_second_run_skips_what_is_already_assigned(self) -> None:
        for mod in self.scripts:
            self.cache_path.unlink(missing_ok=True)
            fake = FakeController()
            self.run_script(mod, fake, [self.csv(GOOD_CSV)])
            fake.assign_calls.clear()
            code, out, _ = self.run_script(mod, fake, [self.csv(GOOD_CSV)])
            with self.subTest(module=mod.__name__):
                self.assertEqual(code, 0)
                self.assertEqual(fake.assign_calls, [])
                self.assertIn("ALREADY ASSIGNED: 2", out)

    def test_default_site_is_used_for_rows_without_one(self) -> None:
        for mod in self.scripts:
            self.cache_path.unlink(missing_ok=True)
            fake = FakeController()
            code, out, err = self.run_script(mod, fake, [
                self.csv("ip_address\n10.0.0.1\n10.0.0.2\n"), "--site", "Global/London/Building1"])
            with self.subTest(module=mod.__name__):
                self.assertEqual(code, 0, out + err)
                self.assertEqual(fake.assign_calls, [("s-b1", ["dev-1", "dev-2"])])  # one request, one site
            for device in fake.devices:
                device["site"] = "s-b2" if device["id"] == "dev-3" else None

    def test_an_area_is_rejected_before_anything_is_sent(self) -> None:
        for mod in self.scripts:
            self.cache_path.unlink(missing_ok=True)
            fake = FakeController()
            code, _, err = self.run_script(mod, fake, [self.csv("ip_address,site\n10.0.0.1,Global/London\n")])
            with self.subTest(module=mod.__name__):
                self.assertEqual(code, 1)
                self.assertIn("is an area", err)
                self.assertEqual(fake.assign_calls, [])

    def test_a_failed_task_is_reported_with_its_reason(self) -> None:
        for mod in self.scripts:
            self.cache_path.unlink(missing_ok=True)
            fake = FakeController()
            fake.task_mode = "error"
            code, out, _ = self.run_script(mod, fake, [self.csv(GOOD_CSV)])
            with self.subTest(module=mod.__name__):
                self.assertEqual(code, 1)
                self.assertIn("FAILED: [E42] not allowed", out)

    def test_a_task_that_succeeds_without_changing_anything_is_caught(self) -> None:
        for mod in self.scripts:
            self.cache_path.unlink(missing_ok=True)
            fake = FakeController()
            fake.apply_assignment = False
            code, out, _ = self.run_script(mod, fake, [self.csv(GOOD_CSV)])
            with self.subTest(module=mod.__name__):
                self.assertEqual(code, 1)
                self.assertIn("the task finished, but the device is still not assigned to a site", out)

    def test_no_wait_submits_and_prints_the_task_ids(self) -> None:
        for mod in self.scripts:
            self.cache_path.unlink(missing_ok=True)
            fake = FakeController()
            code, out, _ = self.run_script(mod, fake, [self.csv(GOOD_CSV), "--no-wait"])
            with self.subTest(module=mod.__name__):
                self.assertEqual(code, 0)
                self.assertEqual(fake.calls["task"], 0)
                self.assertIn("SUBMITTED: task task-1", out)
                self.assertIn("check_task.py <task-id>", out)
            for device in fake.devices:
                device["site"] = "s-b2" if device["id"] == "dev-3" else None

    def test_raw_shows_sample_records_without_secrets(self) -> None:
        for mod in self.scripts:
            self.cache_path.unlink(missing_ok=True)
            fake = FakeController()
            fake.devices[0]["password"] = "shh"
            real = fake.list_unassigned

            def with_secret(offset, limit, real=real, fake=fake):
                records = real(offset, limit)
                for record in records:
                    record["password"] = "shh"
                return records

            fake.list_unassigned = with_secret
            code, out, _ = self.run_script(mod, fake, [self.csv(GOOD_CSV), "--dry-run", "--raw"])
            with self.subTest(module=mod.__name__):
                self.assertEqual(code, 0)
                self.assertIn('"site": {', out)
                self.assertIn('"password": "***"', out)
                self.assertNotIn("shh", out)

    def test_a_csv_or_refresh_is_required(self) -> None:
        for mod in self.scripts:
            with self.subTest(module=mod.__name__), redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as ctx:
                    self.run_script(mod, FakeController(), [])
                self.assertEqual(ctx.exception.code, 2)

    def test_api_errors_are_one_line_with_exit_code_one(self) -> None:
        for mod in self.scripts:
            self.cache_path.unlink(missing_ok=True)
            fake = FakeController()
            fake.assign_error = None
            with patch.object(mod, "CatalystCenterAPI") as sdk:
                api = fake.sdk_mock()
                api.site_design.get_sites.side_effect = catalystcentersdkException("sites down")
                sdk.return_value = api
                code, out, err = run_main(mod, ["--sites-cache", str(self.cache_path), self.csv(GOOD_CSV)])
            with self.subTest(module=mod.__name__):
                self.assertEqual(code, 1)
                self.assertEqual(err.count("Error:"), 1)
                self.assertIn("sites down", err)

    def test_bad_csv_stops_before_logging_in(self) -> None:
        for mod in self.scripts:
            with patch.object(mod, "CatalystCenterAPI") as sdk:
                code, _, err = run_main(mod, ["--sites-cache", str(self.cache_path),
                                              self.csv("ip_address,site\n10.0.0.1,\n")])
            with self.subTest(module=mod.__name__):
                self.assertEqual(code, 1)
                self.assertIn("No site for 1 device(s): 10.0.0.1", err)
                sdk.assert_not_called()

    def test_a_cache_for_another_controller_is_replaced(self) -> None:
        for mod in self.scripts:
            self.cache_path.write_text(json.dumps({
                "version": 1, "controller": "https://elsewhere", "fetched_at": "2026-01-01T00:00:00+00:00",
                "sites": []}), encoding="utf-8")
            fake = FakeController()
            code, out, _ = self.run_script(mod, fake, [self.csv(GOOD_CSV), "--dry-run"])
            with self.subTest(module=mod.__name__):
                self.assertEqual(code, 0)
                self.assertIn("another controller (https://elsewhere)", out)
                self.assertEqual(fake.calls["sites"], 1)


if __name__ == "__main__":
    unittest.main()
