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

"""Basic: list the global CLI and SNMPv2 read credentials and their IDs.

Run:  python get_credential_ids.py
Then copy the two IDs you want into .env (CLI_CREDENTIAL_ID, SNMP_CREDENTIAL_ID).
Only descriptions, IDs and the CLI username are printed, never passwords.
"""

import os

from catalystcentersdk import CatalystCenterAPI
from dotenv import load_dotenv

# Read the settings from the .env file in this folder.
load_dotenv()

# Connect. The SDK gets the login token for us.
api = CatalystCenterAPI(
    base_url=os.environ["CATALYST_CENTER_BASE_URL"],
    username=os.environ["CATALYST_CENTER_USERNAME"],
    password=os.environ["CATALYST_CENTER_PASSWORD"],
    version="2.3.7.9",  # nearest model in the SDK for Catalyst Center 2.3.7.10
    verify=os.getenv("CATALYST_CENTER_VERIFY_SSL", "true").lower() != "false",
)

# GET /dna/intent/api/v1/global-credential?credentialSubType=CLI
print("CLI credentials")
for item in api.discovery.get_global_credentials("CLI")["response"]:
    print("  {}  id={}  username={}".format(item.get("description"), item.get("id"), item.get("username")))

# GET /dna/intent/api/v1/global-credential?credentialSubType=SNMPV2_READ_COMMUNITY
print("SNMPv2 read credentials")
for item in api.discovery.get_global_credentials("SNMPV2_READ_COMMUNITY")["response"]:
    print("  {}  id={}".format(item.get("description"), item.get("id")))
