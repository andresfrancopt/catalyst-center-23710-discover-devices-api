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

"""Shared helpers: .env settings, credential profiles, IP validation, payloads.

This module deliberately does not import the Catalyst Center SDK, so the
non-SDK reference script can reuse it.
"""

from __future__ import annotations

import ipaddress
import os
import re
import sys
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

import urllib3
from dotenv import load_dotenv

DEFAULT_ENV_PATH = Path(__file__).resolve().parent / ".env"
REQUEST_TIMEOUT_SECONDS = 30

# SDK 3.1.6.0.0 has no 2.3.7.10 model; 2.3.7.9 is the nearest earlier one.
SDK_API_VERSION = "2.3.7.9"

# Discovery settings taken from the supplied request body.
DISCOVERY_TYPE = "Range"
PROTOCOL_ORDER = "ssh"
TIMEOUT_SECONDS = 5
RETRY_COUNT = 3

_PROFILE_NAME = re.compile(r"^[A-Za-z0-9_]+$")


class ConfigError(RuntimeError):
    """Invalid or missing configuration or input data."""


class JobSubmissionError(RuntimeError):
    """A discovery job failed; earlier jobs may already have been submitted."""


@dataclass(frozen=True)
class CredentialProfile:
    name: str
    cli_ids: tuple[str, ...]
    snmpv2_ro_ids: tuple[str, ...]

    @property
    def credential_ids(self) -> list[str]:
        return list(self.cli_ids + self.snmpv2_ro_ids)


@dataclass(frozen=True)
class Settings:
    base_url: str
    username: str
    password: str = field(repr=False)
    ca_bundle: str | None
    profiles: Mapping[str, CredentialProfile]
    default_profile: str
    verify_ssl: bool = True

    @property
    def verify(self) -> bool | str:
        if not self.verify_ssl:
            return False
        return self.ca_bundle or True


def apply_tls_policy(settings: Settings) -> None:
    """Warn once, loudly, when certificate verification is switched off."""
    if settings.verify_ssl:
        return
    print(
        "WARNING: TLS certificate verification is DISABLED "
        "(CATALYST_CENTER_VERIFY_SSL=false). Use this only in a lab.",
        file=sys.stderr,
    )
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


def _parse_bool(name: str, raw: str, default: bool) -> bool:
    text = raw.strip().lower()
    if not text:
        return default
    if text in ("true", "1", "yes", "on"):
        return True
    if text in ("false", "0", "no", "off"):
        return False
    raise ConfigError("{} must be true or false, not {!r}".format(name, raw))


def _split_ids(raw: str) -> tuple[str, ...]:
    ids: list[str] = []
    for item in raw.split(","):
        item = item.strip()
        if item and item not in ids:
            ids.append(item)
    return tuple(ids)


def load_settings(
    environ: Mapping[str, str] | None = None,
    dotenv_path: Path | str = DEFAULT_ENV_PATH,
) -> Settings:
    """Read settings from the environment, loading the .env file first.

    Variables already present in the real environment win over .env values.
    """
    if environ is None:
        if not Path(dotenv_path).is_file():
            raise ConfigError(
                "No .env file at {}. Copy .env.example to .env and fill it in.".format(
                    dotenv_path
                )
            )
        load_dotenv(dotenv_path, override=False)
        environ = os.environ

    def get(name: str) -> str:
        return environ.get(name, "").strip()

    missing = [
        name
        for name in (
            "CATALYST_CENTER_BASE_URL",
            "CATALYST_CENTER_USERNAME",
            "CATALYST_CENTER_PASSWORD",
            "CREDENTIAL_PROFILES",
        )
        if not environ.get(name, "").strip()
    ]
    if missing:
        raise ConfigError("Missing required setting(s): " + ", ".join(missing))

    base_url = get("CATALYST_CENTER_BASE_URL").rstrip("/")
    if not base_url.lower().startswith("https://") or "/" in base_url[8:]:
        raise ConfigError(
            "CATALYST_CENTER_BASE_URL must be an HTTPS origin such as "
            "https://catalyst-center.example.com"
        )

    ca_bundle = get("CATALYST_CENTER_CA_BUNDLE") or None
    if ca_bundle is not None and not Path(ca_bundle).is_file():
        raise ConfigError("CATALYST_CENTER_CA_BUNDLE is not a readable file")

    verify_ssl = _parse_bool(
        "CATALYST_CENTER_VERIFY_SSL", get("CATALYST_CENTER_VERIFY_SSL"), True
    )
    if not verify_ssl and ca_bundle is not None:
        raise ConfigError(
            "Set either CATALYST_CENTER_CA_BUNDLE or "
            "CATALYST_CENTER_VERIFY_SSL=false, not both"
        )

    profiles: dict[str, CredentialProfile] = {}
    for raw_name in get("CREDENTIAL_PROFILES").split(","):
        name = raw_name.strip()
        if not name:
            continue
        if not _PROFILE_NAME.match(name):
            raise ConfigError(
                "Invalid profile name {!r}: use letters, digits and underscores".format(
                    name
                )
            )
        key = name.lower()
        if key in profiles:
            raise ConfigError("Profile {!r} is listed more than once".format(name))
        prefix = "CRED_{}_".format(name.upper())
        cli_ids = _split_ids(get(prefix + "CLI_IDS"))
        snmp_ids = _split_ids(get(prefix + "SNMPV2_RO_IDS"))
        if not cli_ids or not snmp_ids:
            raise ConfigError(
                "Profile {!r} needs at least one ID in both {}CLI_IDS and "
                "{}SNMPV2_RO_IDS".format(name, prefix, prefix)
            )
        profiles[key] = CredentialProfile(key, cli_ids, snmp_ids)

    if not profiles:
        raise ConfigError("CREDENTIAL_PROFILES does not list any profile")

    return Settings(
        base_url=base_url,
        username=get("CATALYST_CENTER_USERNAME"),
        password=environ["CATALYST_CENTER_PASSWORD"],
        ca_bundle=ca_bundle,
        profiles=profiles,
        default_profile=next(iter(profiles)),
        verify_ssl=verify_ssl,
    )


def normalize_ip(value: str) -> str:
    text = value.strip()
    try:
        address = ipaddress.ip_address(text)
    except ValueError:
        raise ConfigError("Invalid IP address: {!r}".format(value)) from None
    if address.version != 4:
        raise ConfigError("Only IPv4 addresses are supported: {!r}".format(value))
    return str(address)


def group_by_profile(
    devices: Iterable[tuple[str, str | None]], settings: Settings
) -> "OrderedDict[str, list[str]]":
    """Group (ip, profile) pairs by profile, preserving input order.

    An empty or missing profile means the default (first) profile.
    """
    groups: OrderedDict[str, list[str]] = OrderedDict()
    seen: set[str] = set()
    for raw_ip, raw_profile in devices:
        ip = normalize_ip(raw_ip)
        if ip in seen:
            raise ConfigError("IP address listed more than once: {}".format(ip))
        seen.add(ip)

        profile = (raw_profile or "").strip().lower() or settings.default_profile
        if profile not in settings.profiles:
            raise ConfigError(
                "Unknown credential profile {!r} for {} (defined: {})".format(
                    raw_profile, ip, ", ".join(settings.profiles)
                )
            )
        groups.setdefault(profile, []).append(ip)

    if not groups:
        raise ConfigError("No devices to discover")
    return groups


def build_payload(
    name: str, ip_addresses: Sequence[str], profile: CredentialProfile
) -> dict[str, Any]:
    return {
        "name": name,
        "discoveryType": DISCOVERY_TYPE,
        "ipAddressList": ",".join(ip_addresses),
        "protocolOrder": PROTOCOL_ORDER,
        "timeOut": TIMEOUT_SECONDS,
        "retryCount": RETRY_COUNT,
        "isAutoCdp": False,
        "globalCredentialIdList": profile.credential_ids,
    }


def build_jobs(
    groups: Mapping[str, Sequence[str]], settings: Settings, name_prefix: str
) -> list[tuple[str, dict[str, Any]]]:
    prefix = name_prefix.strip()
    if not prefix:
        raise ConfigError("The discovery name prefix must not be blank")
    return [
        (profile, build_payload("{}-{}".format(prefix, profile), ips, settings.profiles[profile]))
        for profile, ips in groups.items()
    ]


def run_jobs(
    jobs: Sequence[tuple[str, dict[str, Any]]],
    submit: Callable[[dict[str, Any]], Any],
) -> list[tuple[str, Any]]:
    """Submit one job per profile; stop at the first failure."""
    results: list[tuple[str, Any]] = []
    for profile, payload in jobs:
        try:
            results.append((profile, submit(payload)))
        except Exception as exc:
            done = ", ".join(p for p, _ in results) or "none"
            raise JobSubmissionError(
                "Discovery for profile {!r} failed ({}: {}). "
                "Already submitted: {}.".format(profile, type(exc).__name__, exc, done)
            ) from exc
    return results
