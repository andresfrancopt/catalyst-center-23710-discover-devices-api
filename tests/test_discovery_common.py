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
from contextlib import redirect_stderr
from pathlib import Path

from discovery_common import (
    ConfigError,
    JobSubmissionError,
    apply_tls_policy,
    build_jobs,
    build_payload,
    group_by_profile,
    load_settings,
    normalize_ip,
    run_jobs,
)

BASE_ENV = {
    "CATALYST_CENTER_BASE_URL": "https://catalyst.example.test/",
    "CATALYST_CENTER_USERNAME": "api-user",
    "CATALYST_CENTER_PASSWORD": "  s3cret  ",
    "CREDENTIAL_PROFILES": "default, Branch",
    "CRED_DEFAULT_CLI_IDS": "cli-1",
    "CRED_DEFAULT_SNMPV2_RO_IDS": "snmp-1",
    "CRED_BRANCH_CLI_IDS": "cli-2, cli-3,cli-2",
    "CRED_BRANCH_SNMPV2_RO_IDS": "snmp-2",
}


def settings():
    return load_settings(BASE_ENV)


class SettingsTests(unittest.TestCase):
    def test_loads_profiles_and_defaults(self) -> None:
        s = settings()
        self.assertEqual(s.base_url, "https://catalyst.example.test")
        self.assertEqual(s.password, "  s3cret  ")
        self.assertNotIn("s3cret", repr(s))
        self.assertEqual(s.default_profile, "default")
        self.assertEqual(list(s.profiles), ["default", "branch"])
        self.assertEqual(s.profiles["branch"].cli_ids, ("cli-2", "cli-3"))
        self.assertEqual(
            s.profiles["branch"].credential_ids, ["cli-2", "cli-3", "snmp-2"]
        )
        self.assertIs(s.verify, True)

    def test_missing_settings_are_reported(self) -> None:
        with self.assertRaisesRegex(ConfigError, "CATALYST_CENTER_PASSWORD"):
            load_settings({k: v for k, v in BASE_ENV.items() if "PASSWORD" not in k})

    def test_rejects_non_https_url_and_paths(self) -> None:
        for url in ("http://host", "https://host/dna", "host"):
            with self.subTest(url=url), self.assertRaisesRegex(ConfigError, "HTTPS"):
                load_settings({**BASE_ENV, "CATALYST_CENTER_BASE_URL": url})

    def test_profile_needs_cli_and_snmp_ids(self) -> None:
        env = {**BASE_ENV, "CRED_BRANCH_SNMPV2_RO_IDS": ""}
        with self.assertRaisesRegex(ConfigError, "'Branch'"):
            load_settings(env)

    def test_rejects_bad_or_duplicate_profile_names(self) -> None:
        with self.assertRaisesRegex(ConfigError, "Invalid profile name"):
            load_settings({**BASE_ENV, "CREDENTIAL_PROFILES": "bad-name"})
        with self.assertRaisesRegex(ConfigError, "more than once"):
            load_settings({**BASE_ENV, "CREDENTIAL_PROFILES": "default,DEFAULT"})

    def test_missing_ca_bundle_file(self) -> None:
        with self.assertRaisesRegex(ConfigError, "CA_BUNDLE"):
            load_settings({**BASE_ENV, "CATALYST_CENTER_CA_BUNDLE": "/no/such.pem"})

    def test_verify_ssl_is_on_by_default_and_can_be_disabled(self) -> None:
        self.assertTrue(settings().verify_ssl)
        for raw in ("false", "FALSE", "0", "no"):
            with self.subTest(raw=raw):
                s = load_settings({**BASE_ENV, "CATALYST_CENTER_VERIFY_SSL": raw})
                self.assertIs(s.verify, False)
        self.assertIs(
            load_settings({**BASE_ENV, "CATALYST_CENTER_VERIFY_SSL": "true"}).verify, True
        )

    def test_verify_ssl_rejects_garbage_and_ca_bundle_conflict(self) -> None:
        with self.assertRaisesRegex(ConfigError, "true or false"):
            load_settings({**BASE_ENV, "CATALYST_CENTER_VERIFY_SSL": "maybe"})
        with tempfile.TemporaryDirectory() as tmp:
            bundle = Path(tmp) / "ca.pem"
            bundle.write_text("x", encoding="utf-8")
            with self.assertRaisesRegex(ConfigError, "not both"):
                load_settings(
                    {
                        **BASE_ENV,
                        "CATALYST_CENTER_VERIFY_SSL": "false",
                        "CATALYST_CENTER_CA_BUNDLE": str(bundle),
                    }
                )

    def test_insecure_mode_prints_a_warning_only_when_disabled(self) -> None:
        buffer = io.StringIO()
        with redirect_stderr(buffer):
            apply_tls_policy(settings())
        self.assertEqual(buffer.getvalue(), "")
        insecure = load_settings({**BASE_ENV, "CATALYST_CENTER_VERIFY_SSL": "false"})
        with redirect_stderr(buffer):
            apply_tls_policy(insecure)
        self.assertIn("DISABLED", buffer.getvalue())

    def test_reads_dotenv_file_when_no_environ_given(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env_file = Path(tmp) / ".env"
            env_file.write_text(
                "\n".join(f"{k}={v.strip()}" for k, v in BASE_ENV.items()),
                encoding="utf-8",
            )
            keys = list(BASE_ENV)
            import os

            saved = {k: os.environ.pop(k, None) for k in keys}
            try:
                s = load_settings(dotenv_path=env_file)
            finally:
                for k in keys:
                    os.environ.pop(k, None)
                    if saved[k] is not None:
                        os.environ[k] = saved[k]
        self.assertEqual(s.username, "api-user")
        self.assertIn("branch", s.profiles)

    def test_missing_dotenv_file_is_explained(self) -> None:
        with self.assertRaisesRegex(ConfigError, r"\.env\.example"):
            load_settings(dotenv_path="/no/such/.env")


class GroupingTests(unittest.TestCase):
    def test_groups_by_profile_in_order_and_uses_default(self) -> None:
        groups = group_by_profile(
            [("10.0.0.1", "branch"), ("10.0.0.2", None), ("10.0.0.3", ""), ("10.0.0.4", "BRANCH")],
            settings(),
        )
        self.assertEqual(
            dict(groups),
            {"branch": ["10.0.0.1", "10.0.0.4"], "default": ["10.0.0.2", "10.0.0.3"]},
        )

    def test_unknown_profile_duplicate_ip_and_empty_input(self) -> None:
        with self.assertRaisesRegex(ConfigError, "Unknown credential profile"):
            group_by_profile([("10.0.0.1", "nope")], settings())
        with self.assertRaisesRegex(ConfigError, "more than once"):
            group_by_profile([("10.0.0.1", None), (" 10.0.0.1 ", "branch")], settings())
        with self.assertRaisesRegex(ConfigError, "No devices"):
            group_by_profile([], settings())

    def test_ip_validation(self) -> None:
        self.assertEqual(normalize_ip(" 10.0.0.1 "), "10.0.0.1")
        for bad in ("10.0.0", "not-an-ip", "", "2001:db8::1"):
            with self.subTest(bad=bad), self.assertRaises(ConfigError):
                normalize_ip(bad)


class PayloadTests(unittest.TestCase):
    def test_payload_matches_supplied_body(self) -> None:
        s = settings()
        payload = build_payload(
            "discover-10.48.90.173-174",
            ["10.48.90.173", "10.48.90.174"],
            s.profiles["default"],
        )
        self.assertEqual(
            payload,
            {
                "name": "discover-10.48.90.173-174",
                "discoveryType": "Range",
                "ipAddressList": "10.48.90.173,10.48.90.174",
                "protocolOrder": "ssh",
                "timeOut": 5,
                "retryCount": 3,
                "isAutoCdp": False,
                "globalCredentialIdList": ["cli-1", "snmp-1"],
            },
        )

    def test_jobs_are_one_per_profile_with_only_that_profiles_ids(self) -> None:
        s = settings()
        groups = group_by_profile([("10.0.0.1", "default"), ("10.0.0.2", "branch")], s)
        jobs = dict(build_jobs(groups, s, "office"))
        self.assertEqual(jobs["default"]["name"], "office-default")
        self.assertEqual(jobs["default"]["globalCredentialIdList"], ["cli-1", "snmp-1"])
        self.assertEqual(jobs["branch"]["name"], "office-branch")
        self.assertEqual(jobs["branch"]["ipAddressList"], "10.0.0.2")
        self.assertEqual(
            jobs["branch"]["globalCredentialIdList"], ["cli-2", "cli-3", "snmp-2"]
        )
        with self.assertRaisesRegex(ConfigError, "prefix"):
            build_jobs(groups, s, "  ")


class RunJobsTests(unittest.TestCase):
    def test_stops_at_first_failure_and_lists_submitted(self) -> None:
        calls = []

        def submit(payload):
            calls.append(payload["name"])
            if payload["name"] == "b":
                raise RuntimeError("boom")
            return {"ok": payload["name"]}

        jobs = [("p1", {"name": "a"}), ("p2", {"name": "b"}), ("p3", {"name": "c"})]
        with self.assertRaisesRegex(JobSubmissionError, r"'p2'.*boom.*Already submitted: p1"):
            run_jobs(jobs, submit)
        self.assertEqual(calls, ["a", "b"])

    def test_returns_results(self) -> None:
        self.assertEqual(
            run_jobs([("p", {"name": "a"})], lambda payload: {"ok": 1}),
            [("p", {"ok": 1})],
        )


if __name__ == "__main__":
    unittest.main()
