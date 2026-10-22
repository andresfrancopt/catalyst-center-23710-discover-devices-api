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

import discover_from_csv
import discover_ips_in_code
import reference_discover_without_sdk
from discovery_common import ConfigError, SDK_API_VERSION
from test_discovery_common import BASE_ENV


def run_main(module, argv):
    out, err = io.StringIO(), io.StringIO()
    with patch("sys.argv", ["prog", *argv]), redirect_stdout(out), redirect_stderr(err):
        code = module.main()
    return code, out.getvalue(), err.getvalue()


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


class SdkScriptTests(unittest.TestCase):
    def setUp(self) -> None:
        patcher = patch("discovery_common.load_dotenv")
        patcher.start()
        self.addCleanup(patcher.stop)
        env_patch = patch.dict("os.environ", BASE_ENV, clear=True)
        env_patch.start()
        self.addCleanup(env_patch.stop)
        file_patch = patch("discovery_common.Path.is_file", return_value=True)
        file_patch.start()
        self.addCleanup(file_patch.stop)
        devices_patch = patch.object(
            discover_ips_in_code,
            "DEVICES",
            [("10.48.90.173", "default"), ("10.48.90.174", None), ("10.48.91.10", "branch")],
        )
        devices_patch.start()
        self.addCleanup(devices_patch.stop)

    def test_verify_ssl_false_reaches_the_sdk_with_a_warning(self) -> None:
        with patch.dict("os.environ", {"CATALYST_CENTER_VERIFY_SSL": "false"}):
            with patch.object(discover_ips_in_code, "CatalystCenterAPI") as sdk:
                sdk.return_value.discovery.start_discovery.return_value = {"taskId": "t"}
                code, _, err = run_main(discover_ips_in_code, [])
        self.assertEqual(code, 0)
        self.assertIs(sdk.call_args.kwargs["verify"], False)
        self.assertIn("certificate verification is DISABLED", err)

    def test_in_code_script_uses_sdk_once_per_profile(self) -> None:
        with patch.object(discover_ips_in_code, "CatalystCenterAPI") as sdk:
            sdk.return_value.discovery.start_discovery.return_value = {"taskId": "t"}
            code, out, err = run_main(discover_ips_in_code, [])

        self.assertEqual((code, err), (0, ""))
        sdk.assert_called_once_with(
            username="api-user",
            password="  s3cret  ",
            base_url="https://catalyst.example.test",
            version=SDK_API_VERSION,
            verify=True,
            single_request_timeout=30,
        )
        calls = sdk.return_value.discovery.start_discovery.call_args_list
        payloads = [c.kwargs["payload"] for c in calls]
        self.assertEqual([p["name"] for p in payloads], ["discover-default", "discover-branch"])
        self.assertTrue(all(c.kwargs["active_validation"] is False for c in calls))
        self.assertEqual(payloads[0]["ipAddressList"], "10.48.90.173,10.48.90.174")
        self.assertEqual(payloads[1]["globalCredentialIdList"], ["cli-2", "cli-3", "snmp-2"])

    def test_csv_script_groups_rows_and_reports_failures(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            csv_path = Path(tmp) / "d.csv"
            csv_path.write_text(
                "ip_address,credential_profile\n10.0.0.1,branch\n10.0.0.2,\n", encoding="utf-8"
            )
            with patch.object(discover_from_csv, "CatalystCenterAPI") as sdk:
                start = sdk.return_value.discovery.start_discovery
                start.side_effect = [{"taskId": "1"}, RuntimeError("boom")]
                code, out, err = run_main(discover_from_csv, [str(csv_path), "--name-prefix", "x"])

        self.assertEqual(code, 1)
        self.assertIn("boom", err)
        self.assertIn("Already submitted: branch", err)
        names = [c.kwargs["payload"]["name"] for c in start.call_args_list]
        self.assertEqual(names, ["x-branch", "x-default"])

    def test_dry_run_never_touches_the_api(self) -> None:
        with patch.object(discover_ips_in_code, "CatalystCenterAPI") as sdk:
            code, out, _ = run_main(discover_ips_in_code, ["--dry-run"])
        self.assertEqual(code, 0)
        sdk.assert_not_called()
        self.assertIn('"name": "discover-branch"', out)

    def test_configuration_error_is_reported(self) -> None:
        with patch.dict("os.environ", {"CATALYST_CENTER_USERNAME": ""}):
            code, _, err = run_main(discover_ips_in_code, [])
        self.assertEqual(code, 1)
        self.assertIn("CATALYST_CENTER_USERNAME", err)


class ReferenceScriptTests(unittest.TestCase):
    def test_authenticates_then_posts_each_profile(self) -> None:
        patches = [
            patch("discovery_common.load_dotenv"),
            patch.dict("os.environ", BASE_ENV, clear=True),
            patch("discovery_common.Path.is_file", return_value=True),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

        token_response = MagicMock()
        token_response.json.return_value = {"Token": "tok"}
        job_response = MagicMock()
        job_response.json.return_value = {"taskId": "t"}
        with patch.object(
            reference_discover_without_sdk.requests,
            "post",
            side_effect=[token_response, job_response, job_response],
        ) as post:
            code, out, err = run_main(reference_discover_without_sdk, [])

        self.assertEqual((code, err), (0, ""))
        auth_call, first_job, _second_job = post.call_args_list
        self.assertEqual(
            auth_call.args[0], "https://catalyst.example.test/dna/system/api/v1/auth/token"
        )
        self.assertEqual(auth_call.kwargs["auth"], ("api-user", "  s3cret  "))
        self.assertEqual(
            first_job.args[0], "https://catalyst.example.test/dna/intent/api/v1/discovery"
        )
        self.assertEqual(first_job.kwargs["headers"], {"X-Auth-Token": "tok"})
        self.assertEqual(first_job.kwargs["json"]["name"], "discover-default")
        self.assertIs(first_job.kwargs["verify"], True)


if __name__ == "__main__":
    unittest.main()
