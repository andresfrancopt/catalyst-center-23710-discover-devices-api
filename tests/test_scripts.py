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

from __future__ import annotations

import io
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

import _paths  # noqa: F401  (adds 3-shared-module to sys.path)
import check_discovery
import discover_from_csv
import discover_ips_in_code
from discovery_common import ConfigError, SDK_API_VERSION
from test_discovery_common import BASE_ENV, GOOD_JOBS, NCIM_FAILED_TASK, OK_TASK, discovery

CLI_CREDS = {
    "response": [
        {"id": "cli-1", "description": "CSS"},
        {"id": "cli-2", "description": "B1"},
        {"id": "cli-3", "description": "B2"},
    ]
}
SNMP_CREDS = {"response": [{"id": "snmp-1", "description": "RO"}, {"id": "snmp-2", "description": "RO2"}]}
DEVICES = [("10.48.90.173", "default"), ("10.48.90.174", None), ("10.48.91.10", "branch")]


def run_main(module, argv):
    out, err = io.StringIO(), io.StringIO()
    with patch("sys.argv", ["prog", *argv]), redirect_stdout(out), redirect_stderr(err):
        code = module.main()
    return code, out.getvalue(), err.getvalue()


def configure(api, task=OK_TASK) -> None:
    """Make a mocked SDK client behave like a healthy controller."""
    api.discovery.get_global_credentials.side_effect = lambda subtype: (
        CLI_CREDS if subtype == "CLI" else SNMP_CREDS
    )
    api.discovery.start_discovery.return_value = {"response": {"taskId": "t1", "url": "/x"}}
    api.task.get_task_by_id.return_value = task
    api.discovery.get_discovery_by_id.return_value = discovery("Complete")
    api.discovery.get_list_of_discoveries_by_discovery_id.return_value = GOOD_JOBS


class EnvironmentCase(unittest.TestCase):
    env = BASE_ENV

    def setUp(self) -> None:
        for p in (
            patch("discovery_common.load_dotenv"),
            patch.dict("os.environ", self.env, clear=True),
            patch("discovery_common.Path.is_file", return_value=True),
        ):
            p.start()
            self.addCleanup(p.stop)


class CsvReaderTests(unittest.TestCase):
    def write(self, text: str) -> Path:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "devices.csv"
        path.write_text(text, encoding="utf-8")
        return path

    def test_reads_bom_whitespace_and_optional_profile(self) -> None:
        path = self.write(
            "\ufeffname, ip_address ,credential_profile\n"
            "a, 10.0.0.1 ,branch\nb,10.0.0.2,\n"
        )
        self.assertEqual(
            discover_from_csv.read_devices(path),
            [(" 10.0.0.1 ", "branch"), ("10.0.0.2", "")],
        )

    def test_profile_column_is_optional(self) -> None:
        path = self.write("ip_address\n10.0.0.1\n")
        self.assertEqual(discover_from_csv.read_devices(path), [("10.0.0.1", None)])

    def test_rejects_missing_column_empty_ip_and_no_rows(self) -> None:
        with self.assertRaisesRegex(ConfigError, "ip_address"):
            discover_from_csv.read_devices(self.write("address\n10.0.0.1\n"))
        with self.assertRaisesRegex(ConfigError, "line 3"):
            discover_from_csv.read_devices(self.write("ip_address\n10.0.0.1\n ,x\n"))
        with self.assertRaisesRegex(ConfigError, "no devices"):
            discover_from_csv.read_devices(self.write("ip_address\n"))


class InCodeScriptTests(EnvironmentCase):
    def setUp(self) -> None:
        super().setUp()
        patcher = patch.object(discover_ips_in_code, "DEVICES", DEVICES)
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_script(self, argv=(), env=None):
        with patch.dict("os.environ", env or {}):
            with patch.object(discover_ips_in_code, "CatalystCenterAPI") as sdk:
                configure(sdk.return_value)
                result = run_main(discover_ips_in_code, list(argv))
        return sdk, result

    def test_submits_once_per_profile_then_verifies_each_job(self) -> None:
        sdk, (code, out, err) = self.run_script()

        self.assertEqual(code, 0, out + err)
        sdk.assert_called_once_with(
            username="api-user",
            password="  s3cret  ",
            base_url="https://catalyst.example.test",
            version=SDK_API_VERSION,
            verify=True,
            single_request_timeout=30,
        )
        api = sdk.return_value
        calls = api.discovery.start_discovery.call_args_list
        payloads = [c.kwargs["payload"] for c in calls]
        self.assertEqual([p["name"] for p in payloads], ["discover-default", "discover-branch"])
        self.assertTrue(all(c.kwargs["active_validation"] is False for c in calls))
        self.assertEqual(payloads[0]["discoveryType"], "Range")
        self.assertEqual(payloads[0]["ipAddressList"], "10.48.90.173-10.48.90.174")
        self.assertEqual(payloads[0]["globalCredentialIdList"], ["cli-1", "snmp-1"])
        self.assertEqual(payloads[1]["globalCredentialIdList"], ["cli-2", "cli-3", "snmp-2"])
        self.assertEqual(api.task.get_task_by_id.call_count, 2)
        self.assertIn("default: submitted, task t1", out)
        self.assertIn("[default] OK", out)
        self.assertIn("[branch] OK", out)

    def test_descriptions_in_env_are_resolved_to_credential_ids(self) -> None:
        sdk, (code, out, err) = self.run_script(
            env={"CRED_DEFAULT_CLI_IDS": "css", "CRED_DEFAULT_SNMPV2_RO_IDS": "RO"}
        )
        self.assertEqual(code, 0, out + err)
        first = sdk.return_value.discovery.start_discovery.call_args_list[0]
        self.assertEqual(first.kwargs["payload"]["globalCredentialIdList"], ["cli-1", "snmp-1"])

    def test_unknown_credential_stops_before_anything_is_submitted(self) -> None:
        sdk, (code, out, err) = self.run_script(env={"CRED_DEFAULT_CLI_IDS": "CSS-typo"})
        self.assertEqual(code, 1)
        self.assertIn("Profile 'default'", err)
        self.assertIn("CSS (cli-1)", err)
        sdk.return_value.discovery.start_discovery.assert_not_called()

    def test_failed_task_is_reported_with_a_nonzero_exit_code(self) -> None:
        with patch.object(discover_ips_in_code, "CatalystCenterAPI") as sdk:
            configure(sdk.return_value, task=NCIM_FAILED_TASK)
            code, out, _ = run_main(discover_ips_in_code, [])
        self.assertEqual(code, 1)
        self.assertIn("[default] PROBLEM", out)
        self.assertIn("NCIM90257", out)

    def test_no_wait_submits_without_any_verification(self) -> None:
        sdk, (code, out, _) = self.run_script(["--no-wait"])
        self.assertEqual(code, 0)
        api = sdk.return_value
        self.assertEqual(api.discovery.start_discovery.call_count, 2)
        api.task.get_task_by_id.assert_not_called()
        self.assertIn("check_discovery.py", out)

    def test_raw_prints_redacted_responses(self) -> None:
        _, (code, out, _) = self.run_script(["--raw"])
        self.assertEqual(code, 0)
        self.assertIn('"discoveryCondition": "Complete"', out)
        self.assertIn('"password": "***"', out)
        self.assertNotIn("do-not-print", out)

    def test_dry_run_never_touches_the_api(self) -> None:
        with patch.object(discover_ips_in_code, "CatalystCenterAPI") as sdk:
            code, out, _ = run_main(discover_ips_in_code, ["--dry-run"])
        self.assertEqual(code, 0)
        sdk.assert_not_called()
        self.assertIn('"name": "discover-branch"', out)

    def test_verify_ssl_false_reaches_the_sdk_with_a_warning(self) -> None:
        sdk, (code, _, err) = self.run_script(env={"CATALYST_CENTER_VERIFY_SSL": "false"})
        self.assertEqual(code, 0)
        self.assertIs(sdk.call_args.kwargs["verify"], False)
        self.assertIn("certificate verification is DISABLED", err)

    def test_configuration_error_is_reported(self) -> None:
        _, (code, _, err) = self.run_script(env={"CATALYST_CENTER_USERNAME": ""})
        self.assertEqual(code, 1)
        self.assertIn("CATALYST_CENTER_USERNAME", err)


class CsvScriptTests(EnvironmentCase):
    def run_csv(self, rows: str, argv=(), configure_api=configure):
        with tempfile.TemporaryDirectory() as tmp:
            csv_path = Path(tmp) / "d.csv"
            csv_path.write_text(rows, encoding="utf-8")
            with patch.object(discover_from_csv, "CatalystCenterAPI") as sdk:
                configure_api(sdk.return_value)
                result = run_main(discover_from_csv, [str(csv_path), *argv])
        return sdk, result

    def test_groups_rows_by_profile_and_verifies(self) -> None:
        sdk, (code, out, err) = self.run_csv(
            "ip_address,credential_profile\n10.0.0.1,branch\n10.0.0.2,\n",
            ["--name-prefix", "x"],
        )
        self.assertEqual(code, 0, out + err)
        names = [
            c.kwargs["payload"]["name"]
            for c in sdk.return_value.discovery.start_discovery.call_args_list
        ]
        self.assertEqual(names, ["x-branch", "x-default"])
        self.assertIn("[branch] OK", out)

    def test_submission_failure_lists_what_was_already_submitted(self) -> None:
        def broken(api):
            configure(api)
            api.discovery.start_discovery.side_effect = [
                {"response": {"taskId": "1"}},
                RuntimeError("boom"),
            ]

        sdk, (code, _, err) = self.run_csv(
            "ip_address,credential_profile\n10.0.0.1,branch\n10.0.0.2,\n", configure_api=broken
        )
        self.assertEqual(code, 1)
        self.assertIn("boom", err)
        self.assertIn("Already submitted: branch", err)

    def test_no_wait(self) -> None:
        sdk, (code, out, _) = self.run_csv("ip_address\n10.0.0.1\n", ["--no-wait"])
        self.assertEqual(code, 0)
        sdk.return_value.task.get_task_by_id.assert_not_called()
        self.assertIn("default: submitted, task t1", out)


class CheckDiscoveryScriptTests(EnvironmentCase):
    # The check script only needs the API login, not credential profiles.
    env = {k: v for k, v in BASE_ENV.items() if not k.startswith("CRED")}

    def run_check(self, argv, task=OK_TASK):
        with patch.object(check_discovery, "CatalystCenterAPI") as sdk:
            configure(sdk.return_value, task=task)
            result = run_main(check_discovery, argv)
        return sdk, result

    def test_checks_a_task_id(self) -> None:
        sdk, (code, out, err) = self.run_check(["t1"])
        self.assertEqual(code, 0, out + err)
        sdk.return_value.task.get_task_by_id.assert_called_once_with("t1")
        self.assertIn("[task t1] OK", out)

    def test_checks_a_discovery_id_directly(self) -> None:
        sdk, (code, out, _) = self.run_check(["--discovery-id", "1234"])
        self.assertEqual(code, 0)
        sdk.return_value.task.get_task_by_id.assert_not_called()
        sdk.return_value.discovery.get_discovery_by_id.assert_called_once_with("1234")
        self.assertIn("[discovery 1234] OK", out)

    def test_failed_task_gives_a_nonzero_exit_code(self) -> None:
        _, (code, out, _) = self.run_check(["t1"], task=NCIM_FAILED_TASK)
        self.assertEqual(code, 1)
        self.assertIn("NCIM90257", out)

    def test_needs_exactly_one_identifier(self) -> None:
        for argv in ([], ["t1", "--discovery-id", "1"]):
            with self.subTest(argv=argv), redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as ctx:
                    self.run_check(argv)
                self.assertEqual(ctx.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
