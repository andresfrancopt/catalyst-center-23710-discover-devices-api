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

import _paths  # noqa: F401  (adds 3-shared-module to sys.path)
from discovery_common import (
    CLI_SUBTYPE,
    SNMPV2_RO_SUBTYPE,
    ApiGetters,
    ConfigError,
    JobReport,
    JobSubmissionError,
    address_list,
    apply_tls_policy,
    build_jobs,
    build_payload,
    collapse_ranges,
    group_by_profile,
    load_settings,
    match_credential,
    normalize_ip,
    parse_task_result,
    print_reports,
    resolve_settings,
    run_jobs,
    summarize_jobs,
    task_id_from_response,
    verify_job,
    verify_submitted,
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
    def test_payload_uses_a_start_end_range_for_contiguous_ips(self) -> None:
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
                "ipAddressList": "10.48.90.173-10.48.90.174",
                "protocolOrder": "ssh",
                "timeOut": 5,
                "retryCount": 3,
                "isAutoCdp": False,
                "globalCredentialIdList": ["cli-1", "snmp-1"],
            },
        )

    def test_address_list_formats(self) -> None:
        self.assertEqual(address_list(["10.0.0.5"]), ("Single", "10.0.0.5"))
        self.assertEqual(
            address_list(["10.0.0.3", "10.0.0.1", "10.0.0.2"]),
            ("Range", "10.0.0.1-10.0.0.3"),
        )
        self.assertEqual(
            address_list(["10.0.0.1", "10.0.0.2", "10.0.0.9", "10.0.1.0"]),
            ("Multi Range", "10.0.0.1-10.0.0.2,10.0.0.9-10.0.0.9,10.0.1.0-10.0.1.0"),
        )
        # 255 -> 256 crosses an octet boundary and is still contiguous.
        self.assertEqual(
            address_list(["10.0.0.255", "10.0.1.0"]), ("Range", "10.0.0.255-10.0.1.0")
        )

    def test_ranges_never_cover_addresses_that_were_not_listed(self) -> None:
        # .1 and .3 with a gap at .2: must stay two separate entries.
        self.assertEqual(
            collapse_ranges(["10.0.0.1", "10.0.0.3"]),
            [("10.0.0.1", "10.0.0.1"), ("10.0.0.3", "10.0.0.3")],
        )

    def test_jobs_are_one_per_profile_with_only_that_profiles_ids(self) -> None:
        s = settings()
        groups = group_by_profile([("10.0.0.1", "default"), ("10.0.0.2", "branch")], s)
        jobs = dict(build_jobs(groups, s, "office"))
        self.assertEqual(jobs["default"]["name"], "office-default")
        self.assertEqual(jobs["default"]["globalCredentialIdList"], ["cli-1", "snmp-1"])
        self.assertEqual(jobs["branch"]["name"], "office-branch")
        self.assertEqual(jobs["branch"]["discoveryType"], "Single")
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


class ProfilesOptionalTests(unittest.TestCase):
    def test_check_scripts_do_not_need_profiles(self) -> None:
        env = {k: v for k, v in BASE_ENV.items() if not k.startswith(("CRED", "CREDENTIAL"))}
        s = load_settings(env, require_profiles=False)
        self.assertEqual(dict(s.profiles), {})
        with self.assertRaisesRegex(ConfigError, "CREDENTIAL_PROFILES"):
            load_settings(env)


CLI_ITEMS = [
    {"id": "cli-uuid-1", "description": "CSS", "username": "admin", "password": "x"},
    {"id": "cli-uuid-2", "description": "Other", "username": "ops"},
]
SNMP_ITEMS = [{"id": "snmp-uuid-1", "description": "RO", "readCommunity": "secret-community"}]


class CredentialMatchTests(unittest.TestCase):
    def test_matches_id_then_description_then_cli_username(self) -> None:
        self.assertEqual(match_credential("cli-uuid-2", CLI_ITEMS, CLI_SUBTYPE), "cli-uuid-2")
        self.assertEqual(match_credential(" css ", CLI_ITEMS, CLI_SUBTYPE), "cli-uuid-1")
        self.assertEqual(match_credential("OPS", CLI_ITEMS, CLI_SUBTYPE), "cli-uuid-2")
        self.assertEqual(match_credential("RO", SNMP_ITEMS, SNMPV2_RO_SUBTYPE), "snmp-uuid-1")

    def test_snmp_never_matches_on_the_community_string(self) -> None:
        with self.assertRaises(ConfigError):
            match_credential("secret-community", SNMP_ITEMS, SNMPV2_RO_SUBTYPE)

    def test_no_match_lists_available_without_secrets(self) -> None:
        with self.assertRaises(ConfigError) as ctx:
            match_credential("nope", CLI_ITEMS, CLI_SUBTYPE)
        message = str(ctx.exception)
        self.assertIn("CSS (cli-uuid-1)", message)
        self.assertIn("Other (cli-uuid-2)", message)
        self.assertNotIn("password", message.lower().replace("passwords", ""))
        with self.assertRaisesRegex(ConfigError, "no SNMPv2 read-only credentials"):
            match_credential("RO", [], SNMPV2_RO_SUBTYPE)

    def test_ambiguous_description_is_rejected(self) -> None:
        items = [{"id": "a", "description": "Dup"}, {"id": "b", "description": "dup"}]
        with self.assertRaisesRegex(ConfigError, "ambiguous"):
            match_credential("Dup", items, CLI_SUBTYPE)

    def test_wrong_type_is_not_matched(self) -> None:
        # 'RO' exists only as an SNMP credential, so it cannot be used as CLI.
        with self.assertRaises(ConfigError):
            match_credential("RO", CLI_ITEMS, CLI_SUBTYPE)


class ResolveSettingsTests(unittest.TestCase):
    def test_resolves_only_used_profiles_and_queries_each_type_once(self) -> None:
        env = {**BASE_ENV, "CRED_DEFAULT_CLI_IDS": "CSS", "CRED_DEFAULT_SNMPV2_RO_IDS": "RO"}
        calls: list[str] = []

        def list_credentials(subtype: str):
            calls.append(subtype)
            return {"response": CLI_ITEMS if subtype == CLI_SUBTYPE else SNMP_ITEMS}

        resolved = resolve_settings(load_settings(env), ["default"], list_credentials)
        self.assertEqual(
            resolved.profiles["default"].credential_ids, ["cli-uuid-1", "snmp-uuid-1"]
        )
        self.assertEqual(resolved.profiles["branch"].cli_ids, ("cli-2", "cli-3"))  # untouched
        self.assertEqual(calls, [CLI_SUBTYPE, SNMPV2_RO_SUBTYPE])

    def test_error_names_the_profile(self) -> None:
        env = {**BASE_ENV, "CRED_DEFAULT_CLI_IDS": "missing"}
        with self.assertRaisesRegex(ConfigError, "Profile 'default'.*missing"):
            resolve_settings(load_settings(env), ["default"], lambda subtype: {"response": []})


NCIM_FAILED_TASK = {
    "response": {
        "progress": "Inventory service initiate discovery has failed.",
        "errorCode": "NCIM90257",
        "failureReason": "Some of the selected global credentials are not available.",
        "isError": True,
        "startTime": 1,
        "endTime": 2,
        "id": "task-1",
    },
    "version": "1.0",
}
OK_TASK = {"response": {"progress": "1234", "isError": False, "endTime": 2, "id": "task-1"}}
RUNNING_TASK = {"response": {"progress": "Starting", "isError": False, "id": "task-1"}}


def discovery(condition: str, devices: int = 2):
    return {
        "response": {
            "id": "1234",
            "name": "discover-default",
            "discoveryCondition": condition,
            "numDevices": devices,
            "password": "do-not-print",
        }
    }


GOOD_JOBS = {
    "response": [
        {"ipAddress": "10.0.0.1", "pingStatus": "Success", "snmpStatus": "Success", "cliStatus": "Success"},
        {"ipAddress": "10.0.0.2", "pingStatus": "Success", "snmpStatus": "Success", "cliStatus": "Success"},
    ]
}


class FakeTime:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def sequence(*values):
    items = list(values)
    return lambda _id: items.pop(0) if len(items) > 1 else items[0]


def verify(getters: ApiGetters, **kwargs):
    fake = FakeTime()
    report = verify_job("job", getters, sleep=fake.sleep, clock=fake.clock, **kwargs)
    return report, fake


class VerifyJobTests(unittest.TestCase):
    def test_reports_the_ncim90257_failure_from_the_lab(self) -> None:
        getters = ApiGetters(sequence(NCIM_FAILED_TASK), sequence(), sequence())
        report, _ = verify(getters, task_id="task-1")
        self.assertFalse(report.ok)
        self.assertIn("NCIM90257", report.lines[0])
        self.assertIn("global credentials are not available", report.lines[0])

    def test_follows_task_then_discovery_to_per_ip_success(self) -> None:
        getters = ApiGetters(
            sequence(RUNNING_TASK, OK_TASK),
            sequence(discovery("In Progress"), discovery("Complete")),
            sequence(GOOD_JOBS),
        )
        report, fake = verify(getters, task_id="task-1")
        self.assertTrue(report.ok, report.lines)
        self.assertEqual(fake.sleeps, [5, 5])
        self.assertIn("Task task-1 completed", report.lines[0])
        self.assertIn('Discovery 1234 "discover-default": Complete, 2 device(s)', report.lines[1])
        self.assertEqual(len(report.lines), 4)

    def test_per_ip_failure_marks_the_job_as_a_problem(self) -> None:
        jobs = {
            "response": [
                {"ipAddress": "10.0.0.1", "pingStatus": "Success", "snmpStatus": "Success", "cliStatus": "Success"},
                {
                    "ipAddress": "10.0.0.2",
                    "pingStatus": "Success",
                    "snmpStatus": "Success",
                    "cliStatus": "Failed",
                    "errorDescription": "CLI login failed",
                },
            ]
        }
        getters = ApiGetters(sequence(OK_TASK), sequence(discovery("Complete")), sequence(jobs))
        report, _ = verify(getters, task_id="task-1")
        self.assertFalse(report.ok)
        text = "\n".join(report.lines)
        self.assertIn("ATTENTION  ping=Success snmp=Success cli=Failed: CLI login failed", text)

    def test_times_out_while_the_task_is_running(self) -> None:
        getters = ApiGetters(sequence(RUNNING_TASK), sequence(), sequence())
        report, fake = verify(getters, task_id="task-1", timeout=12)
        self.assertFalse(report.ok)
        self.assertIn("still running after 12s", report.lines[0])
        self.assertIn("check_discovery.py task-1", report.lines[0])
        self.assertEqual(fake.sleeps, [5, 5, 2])

    def test_times_out_while_the_discovery_is_running(self) -> None:
        getters = ApiGetters(sequence(OK_TASK), sequence(discovery("In Progress")), sequence())
        report, _ = verify(getters, task_id="task-1", timeout=10)
        self.assertFalse(report.ok)
        self.assertIn("--discovery-id 1234", report.lines[-1])

    def test_task_without_discovery_id_cannot_be_verified(self) -> None:
        task = {"response": {"progress": "", "isError": False, "endTime": 2}}
        report, _ = verify(ApiGetters(sequence(task), sequence(), sequence()), task_id="t")
        self.assertFalse(report.ok)
        self.assertIn("no discovery ID", report.lines[0])

    def test_discovery_id_skips_the_task_step(self) -> None:
        getters = ApiGetters(sequence(), sequence(discovery("Complete")), sequence(GOOD_JOBS))
        report, _ = verify(getters, discovery_id="1234")
        self.assertTrue(report.ok, report.lines)
        self.assertNotIn("Task", report.lines[0])

    def test_api_errors_become_a_report_not_a_crash(self) -> None:
        def boom(_id):
            raise RuntimeError("network down")

        report, _ = verify(ApiGetters(boom, boom, boom), task_id="t")
        self.assertFalse(report.ok)
        self.assertIn("RuntimeError: network down", report.lines[0])

    def test_needs_a_task_or_discovery_id(self) -> None:
        report, _ = verify(ApiGetters(sequence(), sequence(), sequence()))
        self.assertFalse(report.ok)
        self.assertIn("required", report.lines[0])

    def test_no_per_ip_results_is_not_a_success(self) -> None:
        getters = ApiGetters(sequence(OK_TASK), sequence(discovery("Complete", 0)), sequence({"response": []}))
        report, _ = verify(getters, task_id="t")
        self.assertFalse(report.ok)


REAL_TASK_DATA = (
    '{"discoveryId":"581","note":"For further details on the discovery job use discoveryId and '
    'jobTaskId.","jobTaskId":"1792754668237","status":"Success"}'
)
REAL_TASK = {
    "response": {
        "data": REAL_TASK_DATA,
        "progress": "581",
        "isError": False,
        "endTime": 1792754671554,
        "id": "task-1",
    }
}
# The one record the real controller returns from /discovery/{id}/job: a job record with a
# status and times, but no IP and no per-protocol statuses.
REAL_JOB_ENTRY = {"response": [{"name": "demo-default", "taskId": "1792756860432",
                                "startTime": "1792756860435", "endTime": "1792756865591",
                                "jobStatus": "SUCCESS"}]}
UNRECOGNISED_ENTRY = {"response": [{"id": "job-1", "name": "x", "secret": "do-not-print"}]}
NETCONF_JOBS = {"response": [
    {"ipAddress": "10.0.0.1", "pingStatus": "SUCCESS", "snmpStatus": "SUCCESS",
     "cliStatus": "SUCCESS", "netconfStatus": "SUCCESS"},
    {"ipAddress": "10.0.0.2", "pingStatus": "SUCCESS", "snmpStatus": "SUCCESS",
     "cliStatus": "SUCCESS", "netconfStatus": "FAILURE"}]}
# Shaped like the 2.3.7.9 spec for GET /discovery/{id}/network-device.
SPEC_DEVICES = {"response": [
    {"managementIpAddress": "10.48.90.173", "hostname": "sw1", "pingStatus": "SUCCESS",
     "snmpStatus": "SUCCESS", "cliStatus": "SUCCESS", "httpStatus": "FAILURE",
     "netconfStatus": "SUCCESS", "inventoryCollectionStatus": "Managed"},
    {"managementIpAddress": "10.48.90.174", "hostname": "sw2", "pingStatus": "SUCCESS",
     "snmpStatus": "SUCCESS", "cliStatus": "SUCCESS", "httpStatus": "NOT-PROVIDED",
     "netconfStatus": "FAILURE", "errorDescription": "NETCONF connection refused"}]}


def _no_jobs(_id):
    raise AssertionError("the /job call must not be used when /network-device has device entries")


class NotFound(Exception):
    status_code = 404


class DeviceResultsTests(unittest.TestCase):
    def test_parse_task_result(self) -> None:
        self.assertEqual(parse_task_result(REAL_TASK["response"]), ("581", "1792754668237"))
        self.assertEqual(parse_task_result({"progress": "574"}), ("574", ""))
        self.assertEqual(parse_task_result({"data": "not json", "progress": "7"}), ("7", ""))
        self.assertEqual(parse_task_result({"data": {"discoveryId": 9, "jobTaskId": 3}}), ("9", "3"))
        self.assertEqual(parse_task_result({}), ("", ""))

    def test_discovery_id_comes_from_the_task_data(self) -> None:
        seen = []

        def get_discovery(discovery_id):
            seen.append(discovery_id)
            return discovery("Complete")

        getters = ApiGetters(sequence(REAL_TASK), get_discovery, sequence(GOOD_JOBS))
        report, _ = verify(getters, task_id="task-1")
        self.assertEqual(seen, ["581"])
        self.assertTrue(report.ok, report.lines)

    def test_the_real_job_record_is_a_success_without_misleading_errors(self) -> None:
        getters = ApiGetters(sequence(REAL_TASK), sequence(discovery("Complete")), sequence(REAL_JOB_ENTRY))
        report, _ = verify(getters, task_id="task-1")
        text = "\n".join(report.lines)
        self.assertTrue(report.ok, report.lines)
        self.assertIn("Complete, 2 device(s)", text)
        self.assertIn("- discovery job status: SUCCESS", text)
        self.assertIn("per-device and per-credential results are not available", text)
        for misleading in ("unrecognised", "ATTENTION", "ping=", "?"):
            self.assertNotIn(misleading, text)

    def test_a_failed_job_status_needs_attention(self) -> None:
        job = {"response": [{"jobStatus": "FAILURE"}]}
        report, _ = verify(ApiGetters(sequence(OK_TASK), sequence(discovery("Complete")), sequence(job)),
                           task_id="task-1")
        self.assertFalse(report.ok)
        self.assertIn("- discovery job status: FAILURE", report.lines)

    def test_a_discovery_with_no_devices_is_a_problem(self) -> None:
        getters = ApiGetters(sequence(OK_TASK), sequence(discovery("Complete", 0)), sequence(REAL_JOB_ENTRY))
        report, _ = verify(getters, task_id="task-1")
        self.assertFalse(report.ok)
        self.assertIn("- no devices were discovered", report.lines)

    def test_per_ip_statuses_are_shown_when_the_controller_returns_them(self) -> None:
        getters = ApiGetters(sequence(OK_TASK), sequence(discovery("Complete")), sequence(NETCONF_JOBS))
        report, _ = verify(getters, task_id="task-1")
        text = "\n".join(report.lines)
        self.assertFalse(report.ok)
        self.assertIn("OK         ping=SUCCESS snmp=SUCCESS cli=SUCCESS netconf=SUCCESS", text)
        self.assertIn("ATTENTION  ping=SUCCESS snmp=SUCCESS cli=SUCCESS netconf=FAILURE", text)
        self.assertNotIn("not available", text)

    def test_unknown_record_lists_field_names_and_never_values(self) -> None:
        getters = ApiGetters(sequence(OK_TASK), sequence(discovery("Complete")), sequence(UNRECOGNISED_ENTRY))
        report, _ = verify(getters, task_id="task-1")
        text = "\n".join(report.lines)
        self.assertFalse(report.ok)
        self.assertIn("unrecognised result (fields: id, name, secret); run with --raw", text)
        self.assertNotIn("do-not-print", text)

    def test_network_device_results_show_hostname_and_netconf_per_device(self) -> None:
        getters = ApiGetters(sequence(REAL_TASK), sequence(discovery("Complete")), _no_jobs, sequence(SPEC_DEVICES))
        report, _ = verify(getters, task_id="task-1")
        text = "\n".join(report.lines)
        self.assertFalse(report.ok)
        self.assertIn("10.48.90.173    OK         ping=SUCCESS snmp=SUCCESS cli=SUCCESS netconf=SUCCESS (sw1)", text)
        self.assertIn(
            "10.48.90.174    ATTENTION  ping=SUCCESS snmp=SUCCESS cli=SUCCESS netconf=FAILURE (sw2): "
            "NETCONF connection refused", text)
        self.assertNotIn("http", text.lower())
        self.assertNotIn("not available", text)
        self.assertEqual(report.raw["devices"], SPEC_DEVICES)
        self.assertNotIn("jobs", report.raw)

    def test_netconf_that_was_not_tried_is_not_shown(self) -> None:
        devices = {"response": [{"managementIpAddress": "10.0.0.1", "hostname": "r1", "pingStatus": "SUCCESS",
                                 "snmpStatus": "SUCCESS", "cliStatus": "SUCCESS", "netconfStatus": "NOT-PROVIDED"}]}
        getters = ApiGetters(sequence(OK_TASK), sequence(discovery("Complete", 1)), _no_jobs, sequence(devices))
        report, _ = verify(getters, task_id="task-1")
        self.assertTrue(report.ok, report.lines)
        self.assertNotIn("netconf", "\n".join(report.lines))

    def test_a_cli_failure_is_flagged_with_its_error(self) -> None:
        devices = {"response": [{"managementIpAddress": "10.0.0.1", "pingStatus": "SUCCESS", "snmpStatus": "SUCCESS",
                                 "cliStatus": "FAILURE", "errorDescription": "login failed"}]}
        getters = ApiGetters(sequence(OK_TASK), sequence(discovery("Complete", 1)), _no_jobs, sequence(devices))
        report, _ = verify(getters, task_id="task-1")
        self.assertFalse(report.ok)
        self.assertIn("ATTENTION  ping=SUCCESS snmp=SUCCESS cli=FAILURE: login failed", "\n".join(report.lines))

    def test_falls_back_to_the_job_record_when_network_device_is_unusable(self) -> None:
        def broken(_id):
            raise NotFound("no such call")

        for devices in (broken, sequence({"response": []}), sequence({"response": 2}),
                        sequence(UNRECOGNISED_ENTRY)):
            getters = ApiGetters(sequence(REAL_TASK), sequence(discovery("Complete")),
                                 sequence(REAL_JOB_ENTRY), devices)
            report, _ = verify(getters, task_id="task-1")
            text = "\n".join(report.lines)
            self.assertTrue(report.ok, report.lines)
            self.assertIn("- discovery job status: SUCCESS", text)
            self.assertIn("not available from Catalyst Center", text)
            self.assertEqual(report.raw["jobs"], REAL_JOB_ENTRY)

    def test_failed_network_device_call_is_kept_in_raw_output(self) -> None:
        def broken(_id):
            raise NotFound("no such call")

        getters = ApiGetters(sequence(REAL_TASK), sequence(discovery("Complete")), sequence(REAL_JOB_ENTRY), broken)
        report, _ = verify(getters, task_id="task-1")
        self.assertIn("NotFound: no such call", report.raw["devices_error"])

    def test_summarize_jobs_directly(self) -> None:
        self.assertEqual(summarize_jobs([], 2)[1], False)
        self.assertEqual(summarize_jobs(REAL_JOB_ENTRY["response"], None)[1], True)

    def test_deleted_discovery_gets_a_clear_message(self) -> None:
        def missing(_id):
            raise NotFound("Request data not found")

        report, _ = verify(ApiGetters(sequence(REAL_TASK), missing, sequence()), task_id="task-1")
        self.assertFalse(report.ok)
        self.assertEqual(report.lines[-1], "Discovery 581 was not found (it may have been deleted in Catalyst Center)")

    def test_other_discovery_errors_are_still_reported_as_verification_failures(self) -> None:
        def boom(_id):
            raise RuntimeError("network down")

        report, _ = verify(ApiGetters(sequence(REAL_TASK), boom, sequence()), task_id="task-1")
        self.assertIn("Verification failed: RuntimeError: network down", report.lines[-1])


class ReportingTests(unittest.TestCase):
    def test_redacts_secrets_in_raw_output_and_sets_exit_code(self) -> None:
        report = JobReport("p", True, ["fine"], {"discovery": discovery("Complete")})
        bad = JobReport("q", False, ["broken"])
        out = io.StringIO()
        with redirect_stdout(out):
            code_ok = print_reports([report], raw=True)
            code_bad = print_reports([report, bad])
        self.assertEqual((code_ok, code_bad), (0, 1))
        self.assertIn('"password": "***"', out.getvalue())
        self.assertNotIn("do-not-print", out.getvalue())
        self.assertIn("[q] PROBLEM", out.getvalue())

    def test_verify_submitted_handles_a_missing_task_id(self) -> None:
        reports = verify_submitted([("p", {"response": {}})], ApiGetters(sequence(), sequence(), sequence()))
        self.assertFalse(reports[0].ok)
        self.assertIn("no task ID", reports[0].lines[0])

    def test_task_id_extraction(self) -> None:
        self.assertEqual(task_id_from_response({"response": {"taskId": "abc"}}), "abc")
        self.assertIsNone(task_id_from_response({"response": {}}))
        self.assertIsNone(task_id_from_response("garbage"))


if __name__ == "__main__":
    unittest.main()
