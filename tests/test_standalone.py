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

"""Tests for the self-contained scripts in standalone/."""

from __future__ import annotations

import ast
import importlib.util
import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

import discovery_common as common
from catalystcentersdk.exceptions import MalformedRequest, catalystcentersdkException
from test_discovery_common import (
    BASE_ENV,
    GOOD_JOBS,
    NCIM_FAILED_TASK,
    OK_TASK,
    RUNNING_TASK,
    FakeTime,
    discovery,
)
from test_scripts import CLI_CREDS, DEVICES, SNMP_CREDS, configure

ROOT = Path(__file__).resolve().parent.parent
STANDALONE = ROOT / "standalone"
NAMES = ["discover_ips.py", "discover_csv.py", "check_task.py", "manage_credentials.py",
         "list_devices.py", "assign_site.py"]


def load(name: str):
    spec = importlib.util.spec_from_file_location("sa_" + name.removesuffix(".py"), STANDALONE / name)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses look the module up while it executes
    spec.loader.exec_module(module)
    return module


ips_mod = load("discover_ips.py")
csv_mod = load("discover_csv.py")
task_mod = load("check_task.py")
cred_mod = load("manage_credentials.py")

API_ENV = {k: v for k, v in BASE_ENV.items() if not k.startswith("CRED")}


def run_main(module, argv):
    out, err = io.StringIO(), io.StringIO()
    with patch("sys.argv", ["prog", *argv]), redirect_stdout(out), redirect_stderr(err):
        code = module.main()
    return code, out.getvalue(), err.getvalue()


def patched_env(case: unittest.TestCase, module, env: dict[str, str]) -> None:
    """Make a standalone module read `env` instead of a real .env file."""
    for p in (
        patch.dict("os.environ", env, clear=True),
        patch.object(module, "find_env_file", return_value=Path("unused.env")),
        patch.object(module, "load_dotenv"),
    ):
        p.start()
        case.addCleanup(p.stop)


# ---------------------------------------------------------------------------
# The files really are standalone
# ---------------------------------------------------------------------------

ALLOWED_IMPORTS = {
    "__future__", "argparse", "csv", "dataclasses", "datetime", "difflib", "getpass", "ipaddress",
    "json", "os", "pathlib", "re", "sys", "time", "typing",  # standard library
    "catalystcentersdk", "dotenv", "requests", "urllib3",  # declared in requirements.txt
}


class StandaloneProofTests(unittest.TestCase):
    def test_imports_only_the_standard_library_and_declared_packages(self) -> None:
        for name in NAMES:
            tree = ast.parse((STANDALONE / name).read_text(encoding="utf-8"))
            imported = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imported.update(alias.name.split(".")[0] for alias in node.names)
                elif isinstance(node, ast.ImportFrom):
                    self.assertEqual(node.level, 0, name + " uses a relative import")
                    imported.add(node.module.split(".")[0])
            with self.subTest(file=name):
                self.assertLessEqual(imported, ALLOWED_IMPORTS, imported - ALLOWED_IMPORTS)
                self.assertNotIn("discovery_common", (STANDALONE / name).read_text(encoding="utf-8"))

    def test_every_file_carries_the_license_and_demo_disclaimer(self) -> None:
        for name in NAMES:
            head = (STANDALONE / name).read_text(encoding="utf-8")[:1500]
            with self.subTest(file=name):
                self.assertIn("Cisco Sample", head)
                self.assertIn("DEMONSTRATION CODE", head)

    def run_alone(self, name: str, argv: list[str], files: dict[str, str] | None = None,
                  work_files: dict[str, str] | None = None):
        """Run one script copied, on its own, into an otherwise empty folder."""
        with tempfile.TemporaryDirectory() as tmp:
            script_dir, work_dir = Path(tmp) / "script", Path(tmp) / "work"
            script_dir.mkdir()
            work_dir.mkdir()
            shutil.copy(STANDALONE / name, script_dir / name)
            for folder, content in ((script_dir, files), (work_dir, work_files)):
                for fname, text in (content or {}).items():
                    (folder / fname).write_text(text, encoding="utf-8")
            self.assertEqual(sorted(p.name for p in script_dir.glob("*.py")), [name])
            return subprocess.run(
                [sys.executable, str(script_dir / name), *argv],
                cwd=work_dir, capture_output=True, text=True, timeout=120,
                env={"PATH": os.environ.get("PATH", ""), "PYTHONDONTWRITEBYTECODE": "1"},
            )

    ENV_TEXT = "\n".join("{}={}".format(k, v.strip()) for k, v in BASE_ENV.items())

    def test_each_script_runs_alone(self) -> None:
        for name in NAMES:
            with self.subTest(file=name):
                result = self.run_alone(name, ["--help"])
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("usage:", result.stdout)

    def test_discovery_scripts_dry_run_alone_with_env_next_to_script(self) -> None:
        result = self.run_alone("discover_ips.py", ["--dry-run"], files={".env": self.ENV_TEXT})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('"ipAddressList": "10.48.90.173-10.48.90.174"', result.stdout)

        result = self.run_alone(
            "discover_csv.py", ["devices.csv", "--dry-run"],
            files={".env": self.ENV_TEXT},
            work_files={"devices.csv": "ip_address,credential_profile\n10.0.0.1,branch\n10.0.0.2,\n"},
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('"name": "discover-branch"', result.stdout)
        self.assertIn('"discoveryType": "Single"', result.stdout)

    def test_env_file_is_also_found_in_the_current_folder(self) -> None:
        result = self.run_alone("discover_ips.py", ["--dry-run"], work_files={".env": self.ENV_TEXT})
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_missing_env_file_and_settings_is_explained(self) -> None:
        result = self.run_alone("check_task.py", ["some-task"])
        self.assertEqual(result.returncode, 1)
        self.assertIn("Missing required setting(s): CATALYST_CENTER_BASE_URL", result.stderr)
        self.assertIn("No .env file was found", result.stderr)

    def test_env_file_is_optional_when_settings_come_from_the_environment(self) -> None:
        env = {k: v.strip() for k, v in BASE_ENV.items()}
        for name, argv in (("discover_ips.py", ["--dry-run"]),):
            with tempfile.TemporaryDirectory() as tmp:
                shutil.copy(STANDALONE / name, Path(tmp) / name)
                result = subprocess.run(
                    [sys.executable, str(Path(tmp) / name), *argv], cwd=tmp, capture_output=True,
                    text=True, timeout=120,
                    env={**env, "PATH": os.environ.get("PATH", ""), "PYTHONDONTWRITEBYTECODE": "1"})
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('"ipAddressList": "10.48.90.173-10.48.90.174"', result.stdout)
        self.assertNotIn("No .env file", result.stderr)

    def test_credential_dry_run_needs_neither_env_nor_connection(self) -> None:
        result = self.run_alone("manage_credentials.py",
                                ["create-cli", "--description", "lab-cli", "--username", "admin", "--dry-run"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('"password": "***"', result.stdout)


# ---------------------------------------------------------------------------
# Parity: the copies behave exactly like discovery_common (guards against drift)
# ---------------------------------------------------------------------------

IP_SETS = [
    ["10.0.0.5"],
    ["10.0.0.3", "10.0.0.1", "10.0.0.2"],
    ["10.0.0.1", "10.0.0.2", "10.0.0.9", "10.0.1.0"],
    ["10.0.0.255", "10.0.1.0"],
    ["10.0.0.1", "10.0.0.3"],
]
CRED_ITEMS = [
    {"id": "cli-uuid-1", "description": "CSS", "username": "admin", "password": "x"},
    {"id": "cli-uuid-2", "description": "Other", "username": "ops"},
    {"id": "dup-a", "description": "Dup"}, {"id": "dup-b", "description": "dup"},
]


class ParityTests(unittest.TestCase):
    modules = [ips_mod, csv_mod, task_mod]  # modules that contain each helper

    def test_address_formatting(self) -> None:
        for mod in (ips_mod, csv_mod):
            for ips in IP_SETS:
                with self.subTest(module=mod.__name__, ips=ips):
                    self.assertEqual(mod.collapse_ranges(ips), common.collapse_ranges(ips))
                    self.assertEqual(mod.address_list(ips), common.address_list(ips))

    def test_payload(self) -> None:
        settings = common.load_settings(BASE_ENV)
        profile = settings.profiles["branch"]
        for mod in (ips_mod, csv_mod):
            for ips in IP_SETS:
                self.assertEqual(
                    mod.build_payload("n", ips, profile.credential_ids),
                    common.build_payload("n", ips, profile),
                )

    def test_settings(self) -> None:
        ref = common.load_settings(BASE_ENV)
        for mod in (ips_mod, csv_mod):
            got = mod.load_settings(BASE_ENV)
            self.assertEqual((got.base_url, got.username, got.password, got.verify, got.default_profile),
                             (ref.base_url, ref.username, ref.password, ref.verify, ref.default_profile))
            self.assertEqual({k: (p.cli, p.snmp) for k, p in got.profiles.items()},
                             {k: (p.cli_ids, p.snmpv2_ro_ids) for k, p in ref.profiles.items()})
        insecure = {**BASE_ENV, "CATALYST_CENTER_VERIFY_SSL": "false"}
        self.assertIs(ips_mod.load_settings(insecure).verify, common.load_settings(insecure).verify)

    def test_settings_errors_match(self) -> None:
        bad_envs = [
            {k: v for k, v in BASE_ENV.items() if "PASSWORD" not in k},
            {**BASE_ENV, "CATALYST_CENTER_BASE_URL": "http://host"},
            {**BASE_ENV, "CATALYST_CENTER_VERIFY_SSL": "maybe"},
            {**BASE_ENV, "CREDENTIAL_PROFILES": "bad-name"},
            {**BASE_ENV, "CREDENTIAL_PROFILES": "default,DEFAULT"},
            {**BASE_ENV, "CRED_BRANCH_SNMPV2_RO_IDS": ""},
        ]
        for env in bad_envs:
            with self.subTest(env=sorted(set(BASE_ENV) ^ set(env))):
                with self.assertRaises(common.ConfigError) as expected:
                    common.load_settings(env)
                with self.assertRaises(ips_mod.ScriptError) as got:
                    ips_mod.load_settings(env)
                self.assertEqual(str(got.exception), str(expected.exception))

    def test_grouping(self) -> None:
        settings = common.load_settings(BASE_ENV)
        devices = [("10.0.0.1", "branch"), ("10.0.0.2", None), ("10.0.0.3", ""), ("10.0.0.4", "BRANCH")]
        for mod in (ips_mod, csv_mod):
            self.assertEqual(dict(mod.group_by_profile(devices, mod.load_settings(BASE_ENV))),
                             dict(common.group_by_profile(devices, settings)))
            for bad in ([("10.0.0.1", "nope")], [("10.0.0.1", None), ("10.0.0.1", None)], [], [("x", None)],
                        [("2001:db8::1", None)]):
                with self.assertRaises(common.ConfigError) as expected:
                    common.group_by_profile(bad, settings)
                with self.assertRaises(mod.ScriptError) as got:
                    mod.group_by_profile(bad, mod.load_settings(BASE_ENV))
                self.assertEqual(str(got.exception), str(expected.exception))

    def test_credential_matching(self) -> None:
        for mod in (ips_mod, csv_mod):
            for ref in ("cli-uuid-2", " css ", "OPS", "dup-a", "Dup", "nope", "x"):
                for kind in (common.CLI_SUBTYPE, common.SNMPV2_RO_SUBTYPE):
                    with self.subTest(module=mod.__name__, ref=ref, kind=kind):
                        try:
                            expected = ("ok", common.match_credential(ref, CRED_ITEMS, kind))
                        except common.ConfigError as exc:
                            expected = ("error", str(exc))
                        try:
                            got = ("ok", mod.match_credential(ref, CRED_ITEMS, kind))
                        except mod.ScriptError as exc:
                            got = ("error", str(exc))
                        self.assertEqual(got, expected)

    def test_device_summary_and_redaction(self) -> None:
        failing = {"response": [
            {"ipAddress": "10.0.0.2", "pingStatus": "Success", "snmpStatus": "Success",
             "cliStatus": "Failed", "errorDescription": "CLI login failed"}]}
        netconf_ok = {"response": [
            {"ipAddress": "10.0.0.3", "pingStatus": "SUCCESS", "snmpStatus": "SUCCESS",
             "cliStatus": "SUCCESS", "netconfStatus": "SUCCESS"}]}
        netconf_failed = {"response": [
            {"ipAddress": "10.0.0.4", "pingStatus": "SUCCESS", "snmpStatus": "SUCCESS",
             "cliStatus": "SUCCESS", "netconfStatus": "FAILURE", "errorDescription": "NETCONF refused"}]}
        netconf_unused = {"response": [
            {"ipAddress": "10.0.0.5", "pingStatus": "SUCCESS", "snmpStatus": "SUCCESS",
             "cliStatus": "SUCCESS", "netconfStatus": "NOT-PROVIDED"}]}
        for jobs in (GOOD_JOBS, failing, {"response": []}, netconf_ok, netconf_failed, netconf_unused):
            expected = common.summarize_jobs(common.response_items(jobs))
            self.assertEqual(ips_mod.summarize_devices(ips_mod.response_items(jobs)), expected)
            self.assertEqual(csv_mod.summarize_devices(csv_mod.response_items(jobs)), expected)
            self.assertEqual(task_mod.summarize_devices(task_mod._items(jobs)), expected)
        sample = discovery("Complete")
        for mod in (ips_mod, csv_mod, task_mod, cred_mod):
            self.assertEqual(mod.redact(sample), common.redact(sample))

    def test_netconf_status_is_shown_and_failure_needs_attention(self) -> None:
        def line(**extra):
            job = {"ipAddress": "10.0.0.9", "pingStatus": "SUCCESS", "snmpStatus": "SUCCESS",
                   "cliStatus": "SUCCESS", **extra}
            lines, ok = common.summarize_jobs([job])
            return lines[0], ok

        self.assertEqual(line(netconfStatus="SUCCESS"),
                         ("- 10.0.0.9        OK         ping=SUCCESS snmp=SUCCESS cli=SUCCESS netconf=SUCCESS", True))
        text, ok = line(netconfStatus="FAILURE")
        self.assertIn("ATTENTION  ping=SUCCESS snmp=SUCCESS cli=SUCCESS netconf=FAILURE", text)
        self.assertFalse(ok)
        for unused in ("NOT-PROVIDED", "NOT-VALIDATED", ""):
            text, ok = line(netconfStatus=unused)
            self.assertNotIn("netconf", text)
            self.assertTrue(ok)
        text, ok = line()
        self.assertNotIn("netconf", text)
        self.assertTrue(ok)


# ---------------------------------------------------------------------------
# Standalone discovery scripts
# ---------------------------------------------------------------------------


class DiscoveryScriptTests(unittest.TestCase):
    def setUp(self) -> None:
        patched_env(self, ips_mod, BASE_ENV)
        patched_env(self, csv_mod, BASE_ENV)
        devices = patch.object(ips_mod, "DEVICES", DEVICES)
        devices.start()
        self.addCleanup(devices.stop)

    def run_ips(self, argv=(), env=None, task=OK_TASK):
        with patch.dict("os.environ", env or {}):
            with patch.object(ips_mod, "CatalystCenterAPI") as sdk:
                configure(sdk.return_value, task=task)
                result = run_main(ips_mod, list(argv))
        return sdk, result

    def test_submits_one_job_per_profile_then_verifies_each(self) -> None:
        sdk, (code, out, err) = self.run_ips()
        self.assertEqual(code, 0, out + err)
        sdk.assert_called_once_with(
            username="api-user", password="  s3cret  ", base_url="https://catalyst.example.test",
            version=ips_mod.SDK_API_VERSION, verify=True, single_request_timeout=30)
        api = sdk.return_value
        calls = api.discovery.start_discovery.call_args_list
        payloads = [c.kwargs["payload"] for c in calls]
        self.assertEqual([p["name"] for p in payloads], ["discover-default", "discover-branch"])
        self.assertTrue(all(c.kwargs["active_validation"] is False for c in calls))
        self.assertEqual((payloads[0]["discoveryType"], payloads[0]["ipAddressList"]),
                         ("Range", "10.48.90.173-10.48.90.174"))
        self.assertEqual(payloads[0]["globalCredentialIdList"], ["cli-1", "snmp-1"])
        self.assertEqual(payloads[1]["globalCredentialIdList"], ["cli-2", "cli-3", "snmp-2"])
        self.assertEqual(api.task.get_task_by_id.call_count, 2)
        self.assertIn("default: submitted, task t1", out)
        self.assertIn("[default] OK", out)
        self.assertIn("[branch] OK", out)

    def test_descriptions_are_resolved_before_submitting(self) -> None:
        sdk, (code, out, err) = self.run_ips(env={"CRED_DEFAULT_CLI_IDS": "css", "CRED_DEFAULT_SNMPV2_RO_IDS": "RO"})
        self.assertEqual(code, 0, out + err)
        first = sdk.return_value.discovery.start_discovery.call_args_list[0]
        self.assertEqual(first.kwargs["payload"]["globalCredentialIdList"], ["cli-1", "snmp-1"])

    def test_unknown_credential_stops_everything_before_any_submission(self) -> None:
        sdk, (code, _, err) = self.run_ips(env={"CRED_BRANCH_CLI_IDS": "typo"})
        self.assertEqual(code, 1)
        self.assertIn("Profile 'branch'", err)
        self.assertIn("CSS (cli-1)", err)
        sdk.return_value.discovery.start_discovery.assert_not_called()

    def test_failed_task_is_reported_with_code_and_reason(self) -> None:
        _, (code, out, _) = self.run_ips(task=NCIM_FAILED_TASK)
        self.assertEqual(code, 1)
        self.assertIn("[default] PROBLEM", out)
        self.assertIn("[NCIM90257] Some of the selected global credentials are not available", out)

    def test_no_wait_dry_run_raw_and_insecure_tls(self) -> None:
        sdk, (code, out, _) = self.run_ips(["--no-wait"])
        self.assertEqual(code, 0)
        sdk.return_value.task.get_task_by_id.assert_not_called()
        self.assertIn("check_task.py", out)

        with patch.object(ips_mod, "CatalystCenterAPI") as unused:
            code, out, _ = run_main(ips_mod, ["--dry-run"])
        self.assertEqual(code, 0)
        unused.assert_not_called()
        self.assertIn('"name": "discover-branch"', out)

        _, (code, out, _) = self.run_ips(["--raw"])
        self.assertIn('"password": "***"', out)
        self.assertNotIn("do-not-print", out)

        sdk, (code, _, err) = self.run_ips(env={"CATALYST_CENTER_VERIFY_SSL": "false"})
        self.assertIs(sdk.call_args.kwargs["verify"], False)
        self.assertIn("certificate verification is DISABLED", err)

    def test_submission_failure_lists_what_was_already_submitted(self) -> None:
        with patch.object(ips_mod, "CatalystCenterAPI") as sdk:
            configure(sdk.return_value)
            sdk.return_value.discovery.start_discovery.side_effect = [
                {"response": {"taskId": "1"}}, RuntimeError("boom")]
            with self.assertRaises(RuntimeError):  # unexpected errors are not swallowed
                run_main(ips_mod, [])
            sdk.return_value.discovery.start_discovery.side_effect = [
                {"response": {"taskId": "1"}}, catalystcentersdkException("boom")]
            code, _, err = run_main(ips_mod, [])
        self.assertEqual(code, 1)
        self.assertIn("boom", err)
        self.assertIn("Already submitted: default", err)

    def test_csv_script_groups_rows_and_supports_no_wait(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "d.csv"
            path.write_text("\ufeffname,ip_address,credential_profile\na, 10.0.0.1 ,branch\nb,10.0.0.2,\n", encoding="utf-8")
            with patch.object(csv_mod, "CatalystCenterAPI") as sdk:
                configure(sdk.return_value)
                code, out, err = run_main(csv_mod, [str(path), "--name-prefix", "x"])
                names = [c.kwargs["payload"]["name"] for c in sdk.return_value.discovery.start_discovery.call_args_list]
                code2, out2, _ = run_main(csv_mod, [str(path), "--no-wait"])
        self.assertEqual(code, 0, out + err)
        self.assertEqual(names[:2], ["x-branch", "x-default"])
        self.assertIn("[branch] OK", out)
        self.assertEqual(code2, 0)
        self.assertIn("Not waiting", out2)

    def test_csv_reader_errors(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            def write(text):
                path = Path(tmp) / "d.csv"
                path.write_text(text, encoding="utf-8")
                return path

            self.assertEqual(csv_mod.read_devices(write("ip_address\n10.0.0.1\n")), [("10.0.0.1", None)])
            for text, message in (("address\n1\n", "ip_address"), ("ip_address\n10.0.0.1\n ,x\n", "line 3"),
                                  ("ip_address\n", "no devices")):
                with self.subTest(text=text), self.assertRaisesRegex(csv_mod.ScriptError, message):
                    csv_mod.read_devices(write(text))

    def test_follow_job_waits_then_times_out_with_a_check_later_hint(self) -> None:
        for mod, hint in ((ips_mod, "check_task.py t1"), (csv_mod, "check_task.py t1")):
            api = MagicMock()
            api.task.get_task_by_id.return_value = RUNNING_TASK
            fake = FakeTime()
            report = mod.follow_job(api, "p", "t1", 12, sleep=fake.sleep, clock=fake.clock)
            self.assertFalse(report.ok)
            self.assertIn(hint, report.lines[0])
            self.assertEqual(fake.sleeps, [5, 5, 2])

            api = MagicMock()
            api.task.get_task_by_id.side_effect = [RUNNING_TASK, OK_TASK]
            api.discovery.get_discovery_by_id.side_effect = [discovery("In Progress"), discovery("Complete")]
            api.discovery.get_list_of_discoveries_by_discovery_id.return_value = GOOD_JOBS
            fake = FakeTime()
            report = mod.follow_job(api, "p", "t1", 60, sleep=fake.sleep, clock=fake.clock)
            self.assertTrue(report.ok, report.lines)
            self.assertEqual(fake.sleeps, [5, 5])


# ---------------------------------------------------------------------------
# Standalone task checker
# ---------------------------------------------------------------------------


class CheckTaskTests(unittest.TestCase):
    def setUp(self) -> None:
        patched_env(self, task_mod, API_ENV)

    def run_check(self, argv, task=OK_TASK):
        with patch.object(task_mod, "CatalystCenterAPI") as sdk:
            configure(sdk.return_value, task=task)
            result = run_main(task_mod, argv)
        return sdk, result

    def test_a_completed_task_is_ok_and_shows_its_result(self) -> None:
        sdk, (code, out, err) = self.run_check(["t1"])
        self.assertEqual(code, 0, out + err)
        sdk.return_value.task.get_task_by_id.assert_called_once_with("t1")
        sdk.return_value.discovery.get_discovery_by_id.assert_not_called()
        self.assertIn("[task t1] OK", out)
        self.assertIn("Task t1 completed: 1234", out)
        self.assertIn("--discovery", out)

    def test_a_failed_task_reports_code_and_reason(self) -> None:
        _, (code, out, _) = self.run_check(["t1"], task=NCIM_FAILED_TASK)
        self.assertEqual(code, 1)
        self.assertIn("[NCIM90257] Some of the selected global credentials are not available", out)

    def test_discovery_flag_follows_into_the_devices(self) -> None:
        sdk, (code, out, _) = self.run_check(["t1", "--discovery"])
        self.assertEqual(code, 0)
        sdk.return_value.discovery.get_discovery_by_id.assert_called_once_with("1234")
        self.assertIn('Discovery 1234 "discover-default": Complete, 2 device(s)', out)
        self.assertIn("10.0.0.2", out)

    def test_discovery_flag_reports_failing_devices(self) -> None:
        with patch.object(task_mod, "CatalystCenterAPI") as sdk:
            configure(sdk.return_value)
            sdk.return_value.discovery.get_list_of_discoveries_by_discovery_id.return_value = {"response": [
                {"ipAddress": "10.0.0.2", "pingStatus": "Success", "snmpStatus": "Success",
                 "cliStatus": "Failed", "errorDescription": "CLI login failed"}]}
            code, out, _ = run_main(task_mod, ["t1", "--discovery"])
        self.assertEqual(code, 1)
        self.assertIn("ATTENTION  ping=Success snmp=Success cli=Failed: CLI login failed", out)

    def test_discovery_id_checks_directly(self) -> None:
        sdk, (code, out, _) = self.run_check(["--discovery-id", "1234"])
        self.assertEqual(code, 0)
        sdk.return_value.task.get_task_by_id.assert_not_called()
        self.assertIn("[discovery 1234] OK", out)

    def test_running_task_times_out(self) -> None:
        api = MagicMock()
        api.task.get_task_by_id.return_value = RUNNING_TASK
        fake = FakeTime()
        report = task_mod.check(api, "t1", None, False, 7, sleep=fake.sleep, clock=fake.clock)
        self.assertFalse(report.ok)
        self.assertIn("still running after 7s: Starting", report.lines[0])
        self.assertEqual(fake.sleeps, [5, 2])

    def test_timeout_zero_checks_once(self) -> None:
        sdk, (code, out, _) = self.run_check(["t1", "--timeout", "0"], task=RUNNING_TASK)
        self.assertEqual(code, 1)
        self.assertEqual(sdk.return_value.task.get_task_by_id.call_count, 1)

    def test_a_long_task_message_is_shortened(self) -> None:
        long_task = {"response": {"isError": False, "endTime": 1, "progress": "x" * 1000}}
        _, (code, out, _) = self.run_check(["t1"], task=long_task)
        self.assertEqual(code, 0)
        self.assertLess(len(out), 700)

    def test_api_errors_become_a_problem_not_a_crash(self) -> None:
        api = MagicMock()
        api.task.get_task_by_id.side_effect = catalystcentersdkException("network down")
        report = task_mod.check(api, "t1", None, False, 0)
        self.assertFalse(report.ok)
        self.assertIn("network down", report.lines[0])

    def test_arguments_are_validated(self) -> None:
        for argv in ([], ["t1", "--discovery-id", "1"], ["--discovery-id", "1", "--discovery"]):
            with self.subTest(argv=argv), redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as ctx:
                    self.run_check(argv)
                self.assertEqual(ctx.exception.code, 2)

    def test_needs_only_the_api_login(self) -> None:
        self.assertEqual(task_mod.load_settings(API_ENV).username, "api-user")
        with self.assertRaisesRegex(task_mod.ScriptError, "CATALYST_CENTER_USERNAME"):
            task_mod.load_settings({k: v for k, v in API_ENV.items() if "USERNAME" not in k})


# ---------------------------------------------------------------------------
# Standalone credential manager
# ---------------------------------------------------------------------------

SECRETS = ("S3CR3T-CLI", "S3CR3T-EN", "S3CR3T-COMM", "NEWPW", "NEWEN", "NEWCOMM", "NEWPW1", "NEWPW2")
EXISTING_CLI = [{"id": "cli-uuid-1", "description": "CSS", "username": "admin",
                 "password": "S3CR3T-CLI", "enablePassword": "S3CR3T-EN"}]
EXISTING_SNMP = [{"id": "snmp-uuid-1", "description": "RO", "readCommunity": "S3CR3T-COMM"}]


def listing(cli=(EXISTING_CLI,), snmp=(EXISTING_SNMP,)):
    """get_global_credentials stub: each call returns the next response (the last one repeats)."""
    queues = {"CLI": list(cli), "SNMPV2_READ_COMMUNITY": list(snmp)}

    def stub(subtype, *args, **kwargs):
        queue = queues[subtype]
        return {"response": queue.pop(0) if len(queue) > 1 else queue[0]}

    return stub


class ManageCredentialsTests(unittest.TestCase):
    def setUp(self) -> None:
        patched_env(self, cred_mod, API_ENV)

    def run_cmd(self, argv, *, cli=(EXISTING_CLI,), snmp=(EXISTING_SNMP,), secrets=(), setup=None, env=None):
        with patch.dict("os.environ", env or {}):
            with patch.object(cred_mod, "CatalystCenterAPI") as sdk, \
                    patch.object(cred_mod.getpass, "getpass", side_effect=list(secrets)) as ask:
                api = sdk.return_value
                api.discovery.get_global_credentials.side_effect = listing(cli, snmp)
                api.discovery.create_cli_credentials.return_value = {"response": "ignored"}
                api.discovery.create_snmp_read_community.return_value = {"response": "ignored"}
                if setup:
                    setup(api)
                code, out, err = run_main(cred_mod, argv)
        for secret in SECRETS:
            self.assertNotIn(secret, out + err, "a secret was printed")
        return api, ask, code, out, err

    # --- list / check ---------------------------------------------------

    def test_list_shows_identifying_fields_only(self) -> None:
        _, _, code, out, _ = self.run_cmd(["list"])
        self.assertEqual(code, 0)
        self.assertIn("CLI credentials (1):", out)
        self.assertIn("CSS (id cli-uuid-1), username admin", out)
        self.assertIn("SNMPv2 read-only credentials (1):", out)
        self.assertIn("RO (id snmp-uuid-1)", out)

    def test_list_raw_redacts_secret_fields(self) -> None:
        _, _, code, out, _ = self.run_cmd(["list", "--raw"])
        self.assertEqual(code, 0)
        self.assertIn('"password": "***"', out)
        self.assertIn('"readCommunity": "***"', out)

    def test_check_finds_by_description_username_and_id(self) -> None:
        for ref in ("css", "ADMIN", "cli-uuid-1"):
            with self.subTest(ref=ref):
                _, _, code, out, _ = self.run_cmd(["check", ref])
                self.assertEqual(code, 0)
                self.assertIn("Found CLI: CSS (id cli-uuid-1), username admin", out)
        _, _, code, out, _ = self.run_cmd(["check", "RO"])
        self.assertIn("Found SNMPv2 read-only: RO (id snmp-uuid-1)", out)

    def test_check_not_found_and_type_filter(self) -> None:
        _, _, code, _, err = self.run_cmd(["check", "nope"])
        self.assertEqual(code, 1)
        self.assertIn("No CLI or SNMPv2 read-only credential matches 'nope'", err)
        _, _, code, _, err = self.run_cmd(["check", "CSS", "--type", "snmp-ro"])
        self.assertEqual(code, 1)

    # --- create-cli ---------------------------------------------------------

    def test_create_cli_skips_an_existing_description_without_asking_for_secrets(self) -> None:
        api, ask, code, out, _ = self.run_cmd(["create-cli", "--description", "css", "--username", "x"])
        self.assertEqual(code, 0)
        self.assertIn("already exists (CSS (id cli-uuid-1), username admin); nothing was created", out)
        ask.assert_not_called()
        api.discovery.create_cli_credentials.assert_not_called()

    def test_create_cli_prompts_creates_and_confirms_by_listing(self) -> None:
        created = [{"id": "new-1", "description": "lab-cli", "username": "admin", "password": "****"}]
        api, ask, code, out, err = self.run_cmd(
            ["create-cli", "--description", "lab-cli", "--username", "admin"],
            cli=([], created), secrets=["NEWPW", "NEWPW", ""])
        self.assertEqual(code, 0, out + err)
        api.discovery.create_cli_credentials.assert_called_once_with(payload=[{
            "credentialType": "GLOBAL", "description": "lab-cli", "username": "admin", "password": "NEWPW"}])
        self.assertEqual(ask.call_count, 3)
        self.assertIn("CLI credential created and confirmed: lab-cli (id new-1), username admin", out)

    def test_create_cli_with_enable_password_and_comments(self) -> None:
        created = [{"id": "new-1", "description": "lab-cli", "username": "admin"}]
        api, _, code, _, _ = self.run_cmd(
            ["create-cli", "--description", "lab-cli", "--username", "admin", "--comments", "lab"],
            cli=([], created), secrets=["NEWPW", "NEWPW", "NEWEN", "NEWEN"])
        self.assertEqual(code, 0)
        payload = api.discovery.create_cli_credentials.call_args.kwargs["payload"][0]
        self.assertEqual((payload["enablePassword"], payload["comments"]), ("NEWEN", "lab"))

    def test_mismatched_or_empty_secrets_stop_before_creating(self) -> None:
        api, _, code, _, err = self.run_cmd(["create-cli", "--description", "n", "--username", "u"],
                                           cli=([],), secrets=["NEWPW1", "NEWPW2"])
        self.assertEqual(code, 1)
        self.assertIn("do not match", err)
        api.discovery.create_cli_credentials.assert_not_called()

        api, _, code, _, err = self.run_cmd(["create-cli", "--description", "n", "--username", "u"],
                                           cli=([],), secrets=[""])
        self.assertEqual(code, 1)
        self.assertIn("must not be empty", err)
        api.discovery.create_cli_credentials.assert_not_called()

    def test_blank_description_or_username_is_rejected(self) -> None:
        for argv in (["create-cli", "--description", " ", "--username", "u"],
                     ["create-cli", "--description", "d", "--username", " "]):
            _, ask, code, _, err = self.run_cmd(argv)
            self.assertEqual(code, 1)
            self.assertIn("must not be empty", err)
            ask.assert_not_called()

    def test_from_env_reads_secrets_without_prompting(self) -> None:
        created = [{"id": "new-1", "description": "lab-cli", "username": "admin"}]
        api, ask, code, _, _ = self.run_cmd(
            ["create-cli", "--description", "lab-cli", "--username", "admin", "--from-env"],
            cli=([], created), env={"NEW_CLI_PASSWORD": "NEWPW", "NEW_CLI_ENABLE_PASSWORD": "NEWEN"})
        self.assertEqual(code, 0)
        ask.assert_not_called()
        payload = api.discovery.create_cli_credentials.call_args.kwargs["payload"][0]
        self.assertEqual((payload["password"], payload["enablePassword"]), ("NEWPW", "NEWEN"))

        api, _, code, _, err = self.run_cmd(
            ["create-cli", "--description", "lab-cli", "--username", "admin", "--from-env"], cli=([],))
        self.assertEqual(code, 1)
        self.assertIn("NEW_CLI_PASSWORD", err)
        api.discovery.create_cli_credentials.assert_not_called()

    def test_sdk_and_api_errors_never_echo_secrets(self) -> None:
        for error in (MalformedRequest("[{'password': 'NEWPW'}] is invalid. Reason: bad field"),
                      catalystcentersdkException("rejected NEWPW")):
            def setup(api, error=error):
                api.discovery.create_cli_credentials.side_effect = error
            _, _, code, _, err = self.run_cmd(["create-cli", "--description", "n", "--username", "u"],
                                             cli=([],), secrets=["NEWPW", "NEWPW", ""], setup=setup)
            self.assertEqual(code, 1)
            self.assertTrue("bad field" in err or "rejected ***" in err, err)

    def test_a_task_error_from_the_controller_is_reported(self) -> None:
        def setup(api):
            api.discovery.create_cli_credentials.return_value = {"response": {"taskId": "t9"}}
            api.task.get_task_by_id.return_value = {"response": {
                "isError": True, "errorCode": "X1", "failureReason": "not allowed", "endTime": 2}}

        api, _, code, _, err = self.run_cmd(["create-cli", "--description", "n", "--username", "u"],
                                           cli=([],), secrets=["NEWPW", "NEWPW", ""], setup=setup)
        self.assertEqual(code, 1)
        self.assertIn("[X1] not allowed", err)
        self.assertEqual(api.discovery.get_global_credentials.call_count, 1)  # no confirmation attempted

    def test_not_confirmed_is_a_failure_with_a_hint(self) -> None:
        _, _, code, _, err = self.run_cmd(
            ["create-cli", "--description", "n", "--username", "u", "--timeout", "0"],
            cli=([],), secrets=["NEWPW", "NEWPW", ""])
        self.assertEqual(code, 1)
        self.assertIn("is not in the list after 0s", err)
        self.assertIn("manage_credentials.py check n", err)

    # --- create-snmp-ro --------------------------------------------------------

    def test_create_snmp_ro_prompts_creates_and_confirms(self) -> None:
        created = [{"id": "snmp-new", "description": "lab-ro", "readCommunity": "****"}]
        api, ask, code, out, err = self.run_cmd(
            ["create-snmp-ro", "--description", "lab-ro"], snmp=([], created), secrets=["NEWCOMM", "NEWCOMM"])
        self.assertEqual(code, 0, out + err)
        api.discovery.create_snmp_read_community.assert_called_once_with(payload=[{
            "credentialType": "GLOBAL", "description": "lab-ro", "readCommunity": "NEWCOMM"}])
        self.assertIn("SNMPv2 read-only credential created and confirmed: lab-ro (id snmp-new)", out)

    def test_create_snmp_ro_skips_existing_and_supports_from_env(self) -> None:
        api, ask, code, out, _ = self.run_cmd(["create-snmp-ro", "--description", "ro"])
        self.assertEqual(code, 0)
        self.assertIn("already exists", out)
        api.discovery.create_snmp_read_community.assert_not_called()

        created = [{"id": "snmp-new", "description": "lab-ro"}]
        api, ask, code, _, _ = self.run_cmd(["create-snmp-ro", "--description", "lab-ro", "--from-env"],
                                           snmp=([], created), env={"NEW_SNMP_RO_COMMUNITY": "NEWCOMM"})
        self.assertEqual(code, 0)
        ask.assert_not_called()

    # --- dry run ------------------------------------------------------------------

    def test_dry_run_masks_secrets_and_touches_nothing(self) -> None:
        with patch.object(cred_mod, "find_env_file", side_effect=cred_mod.ScriptError("no .env needed")), \
                patch.object(cred_mod, "CatalystCenterAPI") as sdk, \
                patch.object(cred_mod.getpass, "getpass") as ask:
            for argv in (["create-cli", "--description", "d", "--username", "u", "--dry-run"],
                         ["create-snmp-ro", "--description", "d", "--dry-run"]):
                code, out, err = run_main(cred_mod, argv)
                self.assertEqual(code, 0, err)
                self.assertIn("Dry run", out)
                self.assertIn('"***"', out)
            sdk.assert_not_called()
            ask.assert_not_called()

    # --- unit helpers ------------------------------------------------------------------

    def test_helpers(self) -> None:
        self.assertEqual(cred_mod.scrub("a SECRET b SECRET", ["SECRET", ""]), "a *** b ***")
        answers = iter(["pw", "pw"])
        self.assertEqual(cred_mod.prompt_secret("X", required=True, ask=lambda _p: next(answers)), "pw")
        self.assertEqual(cred_mod.prompt_secret("X", required=False, ask=lambda _p: ""), "")
        items = [{"id": "Alpha", "description": "beta", "username": "u1"},
                 {"id": "beta", "description": "other", "username": "u2"}]
        self.assertEqual(cred_mod.find_by_reference(items, "beta", "CLI")[0]["id"], "beta")  # ID wins
        self.assertEqual(cred_mod.find_by_reference(items, "U2", "CLI")[0]["id"], "beta")
        self.assertEqual(cred_mod.find_by_reference(items, "u2", "SNMPV2_READ_COMMUNITY"), [])


if __name__ == "__main__":
    unittest.main()
