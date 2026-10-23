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

"""Tests for the flat scripts in basic/."""

from __future__ import annotations

import ast
import io
import runpy
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

BASIC = Path(__file__).resolve().parent.parent / "basic"
NAMES = ["get_credential_ids.py", "discover.py", "check_task.py", "discover_without_sdk.py"]
ALLOWED_IMPORTS = {"os", "sys", "dotenv", "catalystcentersdk", "requests"}

ENV = {
    "CATALYST_CENTER_BASE_URL": "https://cc.example.com",
    "CATALYST_CENTER_USERNAME": "api-user",
    "CATALYST_CENTER_PASSWORD": "api-secret-value",
    "CATALYST_CENTER_VERIFY_SSL": "false",
    "CLI_CREDENTIAL_ID": "cli-id-1",
    "SNMP_CREDENTIAL_ID": "snmp-id-1",
}


def run(name, api, argv=("prog",), env=None):
    out = io.StringIO()
    with patch.dict("os.environ", ENV if env is None else env, clear=True), \
            patch("dotenv.load_dotenv"), \
            patch("catalystcentersdk.CatalystCenterAPI", return_value=api) as factory, \
            patch("sys.argv", list(argv)), redirect_stdout(out):
        runpy.run_path(str(BASIC / name), run_name="__main__")
    return out.getvalue(), factory


class BasicStructureTests(unittest.TestCase):
    def test_only_basic_imports_and_no_functions(self):
        for name in NAMES:
            tree = ast.parse((BASIC / name).read_text())
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    modules = {a.name.split(".")[0] for a in node.names}
                elif isinstance(node, ast.ImportFrom):
                    modules = {node.module.split(".")[0]}
                else:
                    self.assertNotIsInstance(node, (ast.FunctionDef, ast.ClassDef), name)
                    continue
                self.assertLessEqual(modules, ALLOWED_IMPORTS, name)

    def test_license_header_and_env_example(self):
        for name in NAMES:
            text = (BASIC / name).read_text()
            self.assertIn("Cisco Sample", text)
            self.assertIn("DEMONSTRATION CODE", text)
        example = (BASIC / ".env.example").read_text()
        for key in ENV:
            self.assertIn(key + "=", example)
        self.assertIn("CATALYST_CENTER_VERIFY_SSL=true", example)


class GetCredentialIdsTests(unittest.TestCase):
    def test_lists_ids_and_never_secrets(self):
        api = MagicMock()
        api.discovery.get_global_credentials.side_effect = [
            {"response": [{"id": "c1", "description": "CSS", "username": "admin", "password": "TOPSECRET"}]},
            {"response": [{"id": "s1", "description": "RO", "readCommunity": "TOPSECRET"}]},
        ]
        out, factory = run("get_credential_ids.py", api)
        self.assertIn("CSS  id=c1  username=admin", out)
        self.assertIn("RO  id=s1", out)
        self.assertNotIn("TOPSECRET", out)
        self.assertNotIn("api-secret-value", out)
        self.assertEqual([c.args for c in api.discovery.get_global_credentials.call_args_list],
                         [("CLI",), ("SNMPV2_READ_COMMUNITY",)])
        kwargs = factory.call_args.kwargs
        self.assertIs(kwargs["verify"], False)
        self.assertEqual(kwargs["version"], "2.3.7.9")

    def test_verify_defaults_to_true(self):
        api = MagicMock()
        api.discovery.get_global_credentials.return_value = {"response": []}
        env = {k: v for k, v in ENV.items() if k != "CATALYST_CENTER_VERIFY_SSL"}
        _, factory = run("get_credential_ids.py", api, env=env)
        self.assertIs(factory.call_args.kwargs["verify"], True)


class DiscoverTests(unittest.TestCase):
    def test_sends_expected_body(self):
        api = MagicMock()
        api.discovery.start_discovery.return_value = {"response": {"taskId": "task-9"}}
        out, _ = run("discover.py", api)
        api.discovery.start_discovery.assert_called_once_with(
            payload={
                "name": "basic-discovery",
                "discoveryType": "Multi Range",
                "ipAddressList": "10.48.90.173-10.48.90.173,10.48.90.174-10.48.90.174",
                "protocolOrder": "ssh",
                "timeOut": 5,
                "retryCount": 3,
                "isAutoCdp": False,
                "globalCredentialIdList": ["cli-id-1", "snmp-id-1"],
            },
            active_validation=False,
        )
        self.assertIn("Task ID: task-9", out)
        self.assertNotIn("api-secret-value", out)

    def test_empty_credential_id_stops_before_any_call(self):
        api = MagicMock()
        with self.assertRaises(SystemExit) as caught:
            run("discover.py", api, env={**ENV, "SNMP_CREDENTIAL_ID": ""})
        self.assertIn("CLI_CREDENTIAL_ID", str(caught.exception))
        api.discovery.start_discovery.assert_not_called()


class DiscoverWithoutSdkTests(unittest.TestCase):
    def run_script(self, env=None, post=None):
        out = io.StringIO()
        post = post or MagicMock(side_effect=[
            MagicMock(json=lambda: {"Token": "tok-1"}),
            MagicMock(json=lambda: {"response": {"taskId": "task-7"}}),
        ])
        with patch.dict("os.environ", ENV if env is None else env, clear=True), \
                patch("dotenv.load_dotenv"), patch("requests.post", post), redirect_stdout(out):
            runpy.run_path(str(BASIC / "discover_without_sdk.py"), run_name="__main__")
        return out.getvalue(), post

    def test_does_not_use_the_sdk(self):
        tree = ast.parse((BASIC / "discover_without_sdk.py").read_text())
        names = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
        names |= {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
        self.assertNotIn("catalystcentersdk", names)
        self.assertIn("requests", names)

    def test_logs_in_then_sends_the_same_body_as_the_sdk_script(self):
        out, post = self.run_script()
        login, discovery = post.call_args_list
        self.assertEqual(login.args[0], "https://cc.example.com/dna/system/api/v1/auth/token")
        self.assertEqual(login.kwargs["auth"], ("api-user", "api-secret-value"))
        self.assertIs(login.kwargs["verify"], False)
        self.assertEqual(discovery.args[0], "https://cc.example.com/dna/intent/api/v1/discovery")
        self.assertEqual(discovery.kwargs["headers"], {"X-Auth-Token": "tok-1"})
        self.assertEqual(discovery.kwargs["json"], {
            "name": "basic-discovery",
            "discoveryType": "Multi Range",
            "ipAddressList": "10.48.90.173-10.48.90.173,10.48.90.174-10.48.90.174",
            "protocolOrder": "ssh",
            "timeOut": 5,
            "retryCount": 3,
            "isAutoCdp": False,
            "globalCredentialIdList": ["cli-id-1", "snmp-id-1"],
        })
        self.assertIn("Task ID: task-7", out)
        for secret in ("api-secret-value", "tok-1"):
            self.assertNotIn(secret, out)

    def test_empty_credential_id_stops_before_any_call(self):
        post = MagicMock()
        with self.assertRaises(SystemExit):
            self.run_script(env={**ENV, "CLI_CREDENTIAL_ID": ""}, post=post)
        post.assert_not_called()


class CheckTaskTests(unittest.TestCase):
    def check(self, task):
        api = MagicMock()
        api.task.get_task_by_id.return_value = {"response": task}
        out, _ = run("check_task.py", api, argv=("prog", "t-1"))
        api.task.get_task_by_id.assert_called_once_with("t-1")
        return out

    def test_failed(self):
        out = self.check({"isError": True, "errorCode": "NCIM90257", "failureReason": "creds missing"})
        self.assertIn("FAILED: NCIM90257 creds missing", out)

    def test_finished(self):
        self.assertIn("Task finished: done", self.check({"endTime": 1, "progress": "done"}))

    def test_running(self):
        self.assertIn("Still running: working", self.check({"progress": "working"}))

    def test_needs_task_id(self):
        with self.assertRaises(SystemExit):
            run("check_task.py", MagicMock(), argv=("prog",))


if __name__ == "__main__":
    unittest.main()
