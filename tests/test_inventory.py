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

"""Tests for standalone/list_devices.py (inventory to screen and CSV, with paging)."""

from __future__ import annotations

import csv
import io
import os
import tempfile
import unittest
from contextlib import contextmanager, redirect_stderr
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

from catalystcentersdk.exceptions import catalystcentersdkException
from test_discovery_common import BASE_ENV
from test_standalone import API_ENV, load, patched_env, run_main

inv = load("list_devices.py")


def device(i: int, **extra) -> dict:
    base = {
        "id": "dev-{}".format(i),
        "hostname": "sw-{}".format(i),
        "managementIpAddress": "10.0.{}.{}".format(i // 250, i % 250 + 1),
        "platformId": "C9300-24P",
        "family": "Switches and Hubs",
        "role": "ACCESS",
        "softwareVersion": "17.9.4",
        "serialNumber": "SN{:05d}".format(i),
        "macAddress": "aa:bb:cc:00:{:02x}:{:02x}".format(i // 256, i % 256),
        "reachabilityStatus": "Reachable" if i % 5 else "Unreachable",
        "collectionStatus": "Managed",
        "errorCode": None,
        "errorDescription": None,
        "upTime": "10 days, 3:20:31.00",
        "lastUpdated": "2026-10-22 16:30:00",
    }
    base.update(extra)
    return base


def pager(records):
    """A fake API call that returns the requested page (1-based offset) of `records`."""
    calls = []

    def call(offset=None, limit=None, **filters):
        calls.append({"offset": offset, "limit": limit, **filters})
        return {"response": records[offset - 1: offset - 1 + limit]}

    call.calls = calls
    return call


def read_rows(path: Path) -> list[dict]:
    with open(path, encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


@contextmanager
def working_folder():
    with tempfile.TemporaryDirectory() as tmp:
        before = os.getcwd()
        os.chdir(tmp)
        try:
            yield Path(tmp)
        finally:
            os.chdir(before)


class PagingTests(unittest.TestCase):
    def test_reads_every_page_and_stops_on_a_short_one(self) -> None:
        records = [{"n": i} for i in range(5)]
        calls = []

        def fetch(offset, limit):
            calls.append((offset, limit))
            return records[offset - 1: offset - 1 + limit]

        seen = []
        result = inv.fetch_pages(fetch, 2, on_page=lambda page, total: seen.append((page, total)))
        self.assertEqual(result, records)
        self.assertEqual(calls, [(1, 2), (3, 2), (5, 2)])  # offsets start at 1
        self.assertEqual(seen, [(1, 2), (2, 4), (3, 5)])

    def test_an_exact_multiple_needs_one_more_empty_request(self) -> None:
        records = [{"n": i} for i in range(4)]
        calls = []

        def fetch(offset, limit):
            calls.append(offset)
            return records[offset - 1: offset - 1 + limit]

        self.assertEqual(inv.fetch_pages(fetch, 2), records)
        self.assertEqual(calls, [1, 3, 5])

    def test_nothing_found(self) -> None:
        self.assertEqual(inv.fetch_pages(lambda offset, limit: [], 500), [])

    def test_a_controller_that_ignores_offset_is_detected(self) -> None:
        page = [{"n": 1}, {"n": 2}]
        with self.assertRaisesRegex(inv.ScriptError, "same page twice"):
            inv.fetch_pages(lambda offset, limit: list(page), 2)

    def test_too_many_pages_stops_with_advice(self) -> None:
        counter = iter(range(10 ** 6))
        with self.assertRaisesRegex(inv.ScriptError, "More than 3 pages"):
            inv.fetch_pages(lambda offset, limit: [{"n": next(counter)}, {"n": next(counter)}], 2, max_pages=3)

    def test_a_large_inventory_takes_the_expected_number_of_requests(self) -> None:
        records = [device(i) for i in range(1203)]
        api = MagicMock()
        api.devices.get_device_list.side_effect = pager(records)
        result = inv.fetch_devices(api, {}, 500)
        self.assertEqual(len(result), 1203)
        self.assertEqual([c.kwargs["offset"] for c in api.devices.get_device_list.call_args_list], [1, 501, 1001])
        self.assertEqual({c.kwargs["limit"] for c in api.devices.get_device_list.call_args_list}, {500})

    def test_filters_are_sent_on_every_page(self) -> None:
        api = MagicMock()
        fake = pager([device(i) for i in range(3)])
        api.devices.get_device_list.side_effect = fake
        inv.fetch_devices(api, {"role": ["ACCESS"], "hostname": ["sw-.*"]}, 2)
        self.assertTrue(all(c["role"] == ["ACCESS"] and c["hostname"] == ["sw-.*"] for c in fake.calls))
        self.assertEqual(len(fake.calls), 2)

    def test_unassigned_ids_are_collected_across_pages(self) -> None:
        api = MagicMock()
        api.site_design.get_site_not_assigned_network_devices.side_effect = pager(
            [{"id": "dev-{}".format(i)} for i in range(5)])
        self.assertEqual(inv.fetch_unassigned_ids(api, 2), {"dev-{}".format(i) for i in range(5)})

    def test_unassigned_records_without_ids_are_reported(self) -> None:
        api = MagicMock()
        api.site_design.get_site_not_assigned_network_devices.side_effect = pager([{"hostname": "x"}])
        with self.assertRaisesRegex(inv.ScriptError, "no device IDs"):
            inv.fetch_unassigned_ids(api, 500)


class RowAndOutputTests(unittest.TestCase):
    def test_site_text_reads_the_common_shapes(self) -> None:
        for response, expected in (
            ({"response": {"siteNameHierarchy": "Global/A/B"}}, "Global/A/B"),
            ({"response": [{"nameHierarchy": "Global/X"}]}, "Global/X"),
            ({"response": {"siteName": "B"}}, "B"),
            ({"response": []}, ""), ({"response": None}, ""), ("junk", ""), ({}, ""),
        ):
            self.assertEqual(inv.site_text(response), expected)

    def test_look_up_sites_tolerates_a_failing_device(self) -> None:
        api = MagicMock()

        def lookup(device_id):
            if device_id == "dev-1":
                raise catalystcentersdkException("404")
            return {"response": {"siteNameHierarchy": "Global/" + device_id}}

        api.site_design.get_site_assigned_network_device.side_effect = lookup
        err = io.StringIO()
        with redirect_stderr(err):
            sites = inv.look_up_sites(api, [device(0), device(1), device(2), {"hostname": "no-id"}])
        self.assertEqual(sites, {"dev-0": "Global/dev-0", "dev-1": "", "dev-2": "Global/dev-2"})
        self.assertIn("1 site lookup(s) failed", err.getvalue())

    def test_make_row_fills_gaps_and_adds_site_only_when_asked(self) -> None:
        row = inv.make_row({"id": "x", "hostname": None}, None)
        self.assertEqual(row["id"], "x")
        self.assertEqual(row["hostname"], "")
        self.assertEqual(row["serial_number"], "")
        self.assertNotIn("site", row)
        self.assertEqual(inv.make_row(device(1), "Global/A")["site"], "Global/A")

    def test_reachability_summary(self) -> None:
        rows = [inv.make_row(device(i), None) for i in range(1, 11)]
        self.assertEqual(inv.reachability_summary(rows), "Reachable: 8, Unreachable: 2")
        self.assertEqual(inv.reachability_summary([{"reachability": ""}]), "unknown: 1")

    def test_table_is_aligned_and_long_values_are_shortened(self) -> None:
        rows = [{"hostname": "a", "role": "x" * 100}, {"hostname": "longer-name", "role": "y"}]
        lines = inv.format_table(rows, ["hostname", "role"]).splitlines()
        self.assertEqual(lines[0].split(), ["hostname", "role"])
        self.assertTrue(lines[1].startswith("-----------"))
        self.assertTrue(lines[2].rstrip().endswith("..."))
        self.assertLessEqual(max(len(line) for line in lines), 11 + 2 + inv.SCREEN_MAX_WIDTH)

    def test_safe_cell_neutralises_formulas_only(self) -> None:
        for dangerous in ("=1+1", "+cmd", "-cmd", "@SUM(A1)", "\tx", "\rx"):
            self.assertEqual(inv.safe_cell(dangerous), "'" + dangerous)
        for harmless in ("", "10.0.0.1", "-5", "-12.5", "Reachable", "a=b", "1+1"):
            self.assertEqual(inv.safe_cell(harmless), harmless)

    def test_default_file_name(self) -> None:
        self.assertEqual(inv.default_output_name(datetime(2026, 10, 22, 16, 5, 9)), "inventory-20261022-160509.csv")

    def test_write_csv_round_trips_and_guards_cells(self) -> None:
        with working_folder() as tmp:
            rows = [{"a": "=evil()", "b": "naïve, \"quoted\""}, {"a": "x", "b": ""}]
            inv.write_csv(tmp / "o.csv", rows, ["a", "b"], overwrite=False)
            with open(tmp / "o.csv", encoding="utf-8", newline="") as handle:
                self.assertEqual(list(csv.reader(handle)),
                                 [["a", "b"], ["'=evil()", 'naïve, "quoted"'], ["x", ""]])

    def test_an_existing_file_is_kept_unless_overwrite_is_given(self) -> None:
        with working_folder() as tmp:
            path = tmp / "o.csv"
            path.write_text("precious", encoding="utf-8")
            with self.assertRaisesRegex(inv.ScriptError, "already exists"):
                inv.write_csv(path, [{"a": "1"}], ["a"], overwrite=False)
            self.assertEqual(path.read_text(encoding="utf-8"), "precious")
            inv.write_csv(path, [{"a": "1"}], ["a"], overwrite=True)
            self.assertEqual(path.read_text(encoding="utf-8").split(), ["a", "1"])

    def test_a_missing_folder_is_explained(self) -> None:
        with working_folder() as tmp:
            with self.assertRaisesRegex(inv.ScriptError, "folder .* does not exist"):
                inv.write_csv(tmp / "nope" / "o.csv", [], ["a"], overwrite=False)

    def test_redact_hides_secrets(self) -> None:
        self.assertEqual(inv.redact({"password": "x", "nested": [{"snmpCommunity": "y", "ok": 1}]}),
                         {"password": "***", "nested": [{"snmpCommunity": "***", "ok": 1}]})

    def test_argument_helpers(self) -> None:
        self.assertEqual(inv.split_list("a, b,,c "), ["a", "b", "c"])
        self.assertEqual(inv.page_size_number("500"), 500)
        for bad in ("0", "501", "x"):
            with self.assertRaises(Exception):
                inv.page_size_number(bad)


class MainTests(unittest.TestCase):
    def setUp(self) -> None:
        patched_env(self, inv, API_ENV)

    def run_inventory(self, argv, records=None, unassigned=None, sites=None):
        records = [device(i) for i in range(1, 8)] if records is None else records
        with patch.object(inv, "CatalystCenterAPI") as sdk:
            api = sdk.return_value
            fake = pager(records)
            api.devices.get_device_list.side_effect = fake
            api.site_design.get_site_not_assigned_network_devices.side_effect = pager(
                [{"id": i} for i in (unassigned or [])])
            api.site_design.get_site_assigned_network_device.side_effect = (
                lambda device_id: {"response": {"siteNameHierarchy": (sites or {}).get(device_id, "")}})
            result = run_main(inv, argv)
        return api, fake, result

    def test_shows_a_table_and_writes_every_column_to_the_csv(self) -> None:
        with working_folder() as tmp:
            api, fake, (code, out, err) = self.run_inventory(["--output", "devices-out.csv"])
            self.assertEqual(code, 0, err)
            rows = read_rows(tmp / "devices-out.csv")
        self.assertEqual(len(rows), 7)
        self.assertEqual(list(rows[0]), [name for name, _ in inv.COLUMNS])
        self.assertEqual(rows[0]["management_ip"], "10.0.0.2")
        self.assertIn("sw-1", out)
        self.assertIn("7 device(s). Reachable: 6, Unreachable: 1", out)
        self.assertIn("Wrote 7 row(s) with 15 columns to devices-out.csv", out)
        self.assertNotIn("mac_address", out.splitlines()[0])  # the screen is narrower than the CSV

    def test_default_file_name_is_timestamped_in_the_current_folder(self) -> None:
        with working_folder() as tmp:
            _, _, (code, out, err) = self.run_inventory([])
            files = sorted(p.name for p in tmp.iterdir())
        self.assertEqual(code, 0, err)
        self.assertEqual(len(files), 1)
        self.assertRegex(files[0], r"^inventory-\d{8}-\d{6}\.csv$")

    def test_no_csv_writes_nothing(self) -> None:
        with working_folder() as tmp:
            _, _, (code, out, _) = self.run_inventory(["--no-csv"])
            self.assertEqual(list(tmp.iterdir()), [])
        self.assertEqual(code, 0)
        self.assertNotIn("Wrote", out)

    def test_options_that_contradict_each_other_are_rejected(self) -> None:
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as ctx:
            self.run_inventory(["--no-csv", "--output", "x.csv"])
        self.assertEqual(ctx.exception.code, 2)

    def test_existing_output_file_is_not_replaced(self) -> None:
        with working_folder() as tmp:
            (tmp / "x.csv").write_text("keep", encoding="utf-8")
            _, _, (code, _, err) = self.run_inventory(["--output", "x.csv"])
            self.assertEqual((tmp / "x.csv").read_text(encoding="utf-8"), "keep")
        self.assertEqual(code, 1)
        self.assertIn("--overwrite", err)

    def test_filters_reach_the_controller_as_lists(self) -> None:
        with working_folder():
            _, fake, (code, _, err) = self.run_inventory(
                ["--no-csv", "--ip", "10.0.0..*, 10.1.0.1", "--role", "ACCESS", "--reachability", "Reachable",
                 "--family", "Switches and Hubs", "--hostname", "sw-.*"])
        self.assertEqual(code, 0, err)
        self.assertEqual(fake.calls[0]["management_ip_address"], ["10.0.0..*", "10.1.0.1"])
        self.assertEqual(fake.calls[0]["role"], ["ACCESS"])
        self.assertEqual(fake.calls[0]["reachability_status"], ["Reachable"])
        self.assertEqual(fake.calls[0]["family"], ["Switches and Hubs"])
        self.assertEqual(fake.calls[0]["hostname"], ["sw-.*"])

    def test_no_filters_means_none_are_sent(self) -> None:
        with working_folder():
            _, fake, _ = self.run_inventory(["--no-csv"])
        self.assertEqual(set(fake.calls[0]), {"offset", "limit"})

    def test_pages_are_requested_with_the_chosen_size(self) -> None:
        with working_folder():
            _, fake, (code, _, err) = self.run_inventory(["--no-csv", "--page-size", "3"])
        self.assertEqual(code, 0)
        self.assertEqual([c["offset"] for c in fake.calls], [1, 4, 7])
        self.assertIn("page 3: 7 devices read so far", err)

    def test_unassigned_keeps_only_devices_without_a_site(self) -> None:
        with working_folder():
            _, _, (code, out, _) = self.run_inventory(["--no-csv", "--unassigned"], unassigned=["dev-2", "dev-5"])
        self.assertEqual(code, 0)
        self.assertIn("2 device(s)", out)
        self.assertIn("sw-2", out)
        self.assertNotIn("sw-3", out)

    def test_with_site_adds_the_site_column_to_screen_and_csv(self) -> None:
        with working_folder() as tmp:
            api, _, (code, out, err) = self.run_inventory(
                ["--with-site", "--output", "s.csv"], sites={"dev-1": "Global/London/B1/F1"})
            rows = read_rows(tmp / "s.csv")
        self.assertEqual(code, 0, err)
        self.assertEqual(api.site_design.get_site_assigned_network_device.call_count, 7)
        self.assertEqual(rows[0]["site"], "Global/London/B1/F1")
        self.assertEqual(rows[1]["site"], "")
        self.assertIn("site", out.splitlines()[0])

    def test_with_site_and_unassigned_makes_no_per_device_calls(self) -> None:
        with working_folder():
            api, _, (code, out, _) = self.run_inventory(
                ["--no-csv", "--with-site", "--unassigned"], unassigned=["dev-2"])
        self.assertEqual(code, 0)
        api.site_design.get_site_assigned_network_device.assert_not_called()
        self.assertIn("(not assigned)", out)

    def test_no_matching_devices(self) -> None:
        with working_folder() as tmp:
            _, _, (code, out, _) = self.run_inventory(["--output", "e.csv"], records=[])
            header = (tmp / "e.csv").read_text(encoding="utf-8").splitlines()
        self.assertEqual(code, 0)
        self.assertIn("No devices match.", out)
        self.assertEqual(len(header), 1)  # header only

    def test_raw_prints_one_redacted_record(self) -> None:
        with working_folder():
            _, _, (code, out, _) = self.run_inventory(
                ["--no-csv", "--raw"], records=[device(1, snmpCommunity="topsecret", password="p")])
        self.assertEqual(code, 0)
        self.assertIn('"snmpCommunity": "***"', out)
        self.assertNotIn("topsecret", out)

    def test_api_errors_exit_with_one_line_and_code_one(self) -> None:
        with working_folder() as tmp, patch.object(inv, "CatalystCenterAPI") as sdk:
            sdk.return_value.devices.get_device_list.side_effect = catalystcentersdkException("boom")
            code, _, err = run_main(inv, ["--output", "o.csv"])
            self.assertFalse((tmp / "o.csv").exists())
        self.assertEqual(code, 1)
        self.assertEqual(err.count("Error:"), 1)
        self.assertIn("boom", err)

    def test_missing_settings_are_reported_before_any_request(self) -> None:
        with patch.dict("os.environ", {"CATALYST_CENTER_USERNAME": ""}), \
                patch.object(inv, "CatalystCenterAPI") as sdk:
            code, _, err = run_main(inv, ["--no-csv"])
        self.assertEqual(code, 1)
        self.assertIn("CATALYST_CENTER_USERNAME", err)
        sdk.assert_not_called()

    def test_certificate_warning_when_verification_is_off(self) -> None:
        with working_folder():
            with patch.dict("os.environ", {"CATALYST_CENTER_VERIFY_SSL": "false"}):
                api, _, (code, _, err) = self.run_inventory(["--no-csv"])
        self.assertEqual(code, 0)
        self.assertIn("certificate verification is DISABLED", err)


if __name__ == "__main__":
    unittest.main()
