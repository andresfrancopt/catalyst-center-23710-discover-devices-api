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

"""Basic, without the SDK: start a discovery of a few IP addresses.

Same as discover.py, but it calls the REST API directly with "requests".
Uses the same .env file. Run:  python discover_without_sdk.py
"""

import os

import requests
from dotenv import load_dotenv

# The devices to discover. Edit this list.
IPS = ["10.48.90.173", "10.48.90.174"]

# Read the settings and the two credential IDs from the .env file.
load_dotenv()
base_url = os.environ["CATALYST_CENTER_BASE_URL"].rstrip("/")
cli_id = os.environ["CLI_CREDENTIAL_ID"]
snmp_id = os.environ["SNMP_CREDENTIAL_ID"]
verify = os.getenv("CATALYST_CENTER_VERIFY_SSL", "true").lower() != "false"

# Stop early with a clear message if an ID is still empty.
if not cli_id or not snmp_id:
    raise SystemExit("Set CLI_CREDENTIAL_ID and SNMP_CREDENTIAL_ID in .env (see get_credential_ids.py)")

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
token = login.json()["Token"]

# The discovery request. "Range" does not accept a comma-separated list of
# single IPs, so each IP is written as a one-address range ("ip-ip") under
# "Multi Range".
body = {
    "name": "basic-discovery",
    "discoveryType": "Multi Range",
    "ipAddressList": ",".join("{}-{}".format(ip, ip) for ip in IPS),
    "protocolOrder": "ssh",
    "timeOut": 5,
    "retryCount": 3,
    "isAutoCdp": False,
    "globalCredentialIdList": [cli_id, snmp_id],
}

# POST /dna/intent/api/v1/discovery
# The token goes in the X-Auth-Token header (the SDK adds it for us).
response = requests.post(
    base_url + "/dna/intent/api/v1/discovery",
    headers={"X-Auth-Token": token},
    json=body,
    verify=verify,
    timeout=30,
)
response.raise_for_status()

# The call only starts the job and returns a task ID.
print("Discovery submitted. Task ID:", response.json()["response"]["taskId"])
print("Check it with: python check_task.py <task-id>")
