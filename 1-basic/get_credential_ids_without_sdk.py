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

"""Basic, without the SDK: list the global CLI and SNMPv2 read credentials and their IDs.

Same as get_credential_ids.py, but it calls the REST API directly with "requests".
Uses the same .env file. Run:  python get_credential_ids_without_sdk.py
Then copy the two IDs you want into .env (CLI_CREDENTIAL_ID, SNMP_CREDENTIAL_ID).
Only descriptions, IDs and the CLI username are printed, never passwords.
"""

import os

import requests
from dotenv import load_dotenv

# Read the settings from the .env file in this folder.
load_dotenv()
base_url = os.environ["CATALYST_CENTER_BASE_URL"].rstrip("/")
verify = os.getenv("CATALYST_CENTER_VERIFY_SSL", "true").lower() != "false"

# Log in. The SDK does this step for us; here we ask for the token ourselves
# with the username and password (HTTP Basic authentication).
# POST /dna/system/api/v1/auth/token
login = requests.post(
    base_url + "/dna/system/api/v1/auth/token",
    auth=(os.environ["CATALYST_CENTER_USERNAME"], os.environ["CATALYST_CENTER_PASSWORD"]),
    verify=verify,
    timeout=30,
)
login.raise_for_status()
headers = {"X-Auth-Token": login.json()["Token"]}  # the SDK adds this header for us

# GET /dna/intent/api/v1/global-credential?credentialSubType=CLI
print("CLI credentials")
cli = requests.get(
    base_url + "/dna/intent/api/v1/global-credential",
    params={"credentialSubType": "CLI"},
    headers=headers,
    verify=verify,
    timeout=30,
)
cli.raise_for_status()
for item in cli.json()["response"]:
    print("  {}  id={}  username={}".format(item.get("description"), item.get("id"), item.get("username")))

# GET /dna/intent/api/v1/global-credential?credentialSubType=SNMPV2_READ_COMMUNITY
print("SNMPv2 read credentials")
snmp = requests.get(
    base_url + "/dna/intent/api/v1/global-credential",
    params={"credentialSubType": "SNMPV2_READ_COMMUNITY"},
    headers=headers,
    verify=verify,
    timeout=30,
)
snmp.raise_for_status()
for item in snmp.json()["response"]:
    print("  {}  id={}".format(item.get("description"), item.get("id")))
