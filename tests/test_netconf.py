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

"""Tests for the optional NETCONF port in the discovery scripts."""

from __future__ import annotations

import argparse
import io
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch

import discover_from_csv
import discover_ips_in_code
import discovery_common as common
from test_discovery_common import BASE_ENV
from test_scripts import DEVICES, configure, run_main
from test_standalone import csv_mod, ips_mod, patched_env


def parser_with_netconf(module) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    module.add_netconf_arguments(parser)
    return parser


class NetconfFlagTests(unittest.TestCase):
    modules = (common, ips_mod, csv_mod)

    def test_off_by_default_and_the_two_ways_to_turn_it_on(self) -> None:
        for mod in self.modules:
            parser = parser_with_netconf(mod)
            with self.subTest(module=mod.__name__):
                self.assertIsNone(mod.netconf_port_from_args(parser.parse_args([])))
                self.assertEqual(mod.netconf_port_from_args(parser.parse_args(["--netconf"])), 830)
                self.assertEqual(
                    mod.netconf_port_from_args(parser.parse_args(["--netconf-port", "8830"])), 8830
                )

    def test_flags_exclude_each_other_and_ports_are_validated(self) -> None:
        for mod in self.modules:
            parser = parser_with_netconf(mod)
            for argv in (["--netconf", "--netconf-port", "830"], ["--netconf-port", "0"],
                         ["--netconf-port", "65536"], ["--netconf-port", "abc"]):
                with self.subTest(module=mod.__name__, argv=argv), redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit) as ctx:
                        parser.parse_args(argv)
                    self.assertEqual(ctx.exception.code, 2)

    def test_boundary_ports_are_accepted(self) -> None:
        for mod in self.modules:
            self.assertEqual(mod.netconf_port_number("1"), 1)
            self.assertEqual(mod.netconf_port_number("65535"), 65535)

    def test_notice_only_when_enabled(self) -> None:
        for mod in self.modules:
            quiet, loud = io.StringIO(), io.StringIO()
            with redirect_stderr(quiet):
                mod.announce_netconf(None)
            with redirect_stderr(loud):
                mod.announce_netconf(830)
            self.assertEqual(quiet.getvalue(), "")
            self.assertIn("port 830", loud.getvalue())
            self.assertIn("SSH CLI credential", loud.getvalue())


class NetconfPayloadTests(unittest.TestCase):
    def test_field_is_absent_unless_requested_and_is_a_string(self) -> None:
        settings = common.load_settings(BASE_ENV)
        profile = settings.profiles["default"]
        self.assertNotIn("netconfPort", common.build_payload("n", ["10.0.0.1"], profile))
        self.assertEqual(common.build_payload("n", ["10.0.0.1"], profile, 830)["netconfPort"], "830")
        for mod in (ips_mod, csv_mod):
            self.assertNotIn("netconfPort", mod.build_payload("n", ["10.0.0.1"], profile.credential_ids))
            self.assertEqual(
                mod.build_payload("n", ["10.0.0.1"], profile.credential_ids, 8830),
                common.build_payload("n", ["10.0.0.1"], profile, 8830),
            )

    def test_the_rest_of_the_payload_is_untouched(self) -> None:
        profile = common.load_settings(BASE_ENV).profiles["default"]
        plain = common.build_payload("n", ["10.0.0.1", "10.0.0.2"], profile)
        with_netconf = common.build_payload("n", ["10.0.0.1", "10.0.0.2"], profile, 830)
        self.assertEqual({k: v for k, v in with_netconf.items() if k != "netconfPort"}, plain)
        self.assertEqual(with_netconf["protocolOrder"], "ssh")  # NETCONF needs the SSH credential

    def test_build_jobs_passes_the_port_to_every_profile(self) -> None:
        settings = common.load_settings(BASE_ENV)
        groups = common.group_by_profile([("10.0.0.1", "default"), ("10.0.0.2", "branch")], settings)
        jobs = common.build_jobs(groups, settings, "x", 830)
        self.assertEqual([p["netconfPort"] for _, p in jobs], ["830", "830"])
        self.assertTrue(all("netconfPort" not in p for _, p in common.build_jobs(groups, settings, "x")))


class NetconfScriptTests(unittest.TestCase):
    def setUp(self) -> None:
        for p in (
            patch("discovery_common.load_dotenv"),
            patch.dict("os.environ", BASE_ENV, clear=True),
            patch("discovery_common.Path.is_file", return_value=True),
            patch.object(discover_ips_in_code, "DEVICES", DEVICES),
        ):
            p.start()
            self.addCleanup(p.stop)
        patched_env(self, ips_mod, BASE_ENV)
        patched_env(self, csv_mod, BASE_ENV)
        devices = patch.object(ips_mod, "DEVICES", DEVICES)
        devices.start()
        self.addCleanup(devices.stop)

    def submitted_payloads(self, module, argv):
        with patch.object(module, "CatalystCenterAPI") as sdk:
            configure(sdk.return_value)
            code, out, err = run_main(module, argv)
            calls = sdk.return_value.discovery.start_discovery.call_args_list
        self.assertEqual(code, 0, out + err)
        return [c.kwargs["payload"] for c in calls], err

    def test_root_script_sends_netconf_only_when_asked(self) -> None:
        plain, plain_err = self.submitted_payloads(discover_ips_in_code, ["--no-wait"])
        self.assertTrue(all("netconfPort" not in p for p in plain))
        self.assertNotIn("NETCONF", plain_err)

        asked, err = self.submitted_payloads(discover_ips_in_code, ["--no-wait", "--netconf"])
        self.assertEqual([p["netconfPort"] for p in asked], ["830", "830"])
        self.assertIn("NETCONF is on: port 830", err)

        other, _ = self.submitted_payloads(discover_ips_in_code, ["--no-wait", "--netconf-port", "8830"])
        self.assertEqual({p["netconfPort"] for p in other}, {"8830"})

    def test_root_csv_script_dry_run_shows_the_field(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "d.csv"
            path.write_text("ip_address\n10.0.0.1\n", encoding="utf-8")
            code, out, err = run_main(discover_from_csv, [str(path), "--dry-run", "--netconf"])
        self.assertEqual(code, 0, err)
        self.assertIn('"netconfPort": "830"', out)

    def test_standalone_scripts_send_netconf_only_when_asked(self) -> None:
        for mod in (ips_mod, csv_mod):
            argv_base = ["--no-wait"]
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "d.csv"
                path.write_text("ip_address,credential_profile\n10.0.0.1,branch\n", encoding="utf-8")
                extra = [str(path)] if mod is csv_mod else []
                results = {}
                for label, flags in (("plain", []), ("netconf", ["--netconf"]), ("port", ["--netconf-port", "9"])):
                    with patch.object(mod, "CatalystCenterAPI") as sdk:
                        configure(sdk.return_value)
                        code, out, err = run_main(mod, extra + argv_base + flags)
                        payloads = [c.kwargs["payload"] for c in
                                    sdk.return_value.discovery.start_discovery.call_args_list]
                    self.assertEqual(code, 0, out + err)
                    results[label] = payloads
            with self.subTest(module=mod.__name__):
                self.assertTrue(all("netconfPort" not in p for p in results["plain"]))
                self.assertTrue(all(p["netconfPort"] == "830" for p in results["netconf"]))
                self.assertTrue(all(p["netconfPort"] == "9" for p in results["port"]))

    def test_invalid_port_is_rejected_before_anything_runs(self) -> None:
        with patch.object(ips_mod, "CatalystCenterAPI") as sdk, redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                run_main(ips_mod, ["--netconf-port", "99999"])
        sdk.assert_not_called()


if __name__ == "__main__":
    unittest.main()
