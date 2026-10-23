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

"""Basic: start a discovery of a few IP addresses.

Run:  python discover.py
The discovery runs in the background; use the task ID printed at the end
with check_task.py. This creates a discovery job in Catalyst Center.
"""

import os

from catalystcentersdk import CatalystCenterAPI
from dotenv import load_dotenv

# The devices to discover. Edit this list.
IPS = ["10.48.90.173", "10.48.90.174"]

# Read the settings and the two credential IDs from the .env file.
load_dotenv()
cli_ids = [i.strip() for i in os.environ["CLI_CREDENTIAL_ID"].split(",") if i.strip()]
snmp_ids = [i.strip() for i in os.environ["SNMP_CREDENTIAL_ID"].split(",") if i.strip()]

# Stop early with a clear message if an ID is still empty.
if not cli_ids or not snmp_ids:
    raise SystemExit("Set CLI_CREDENTIAL_ID and SNMP_CREDENTIAL_ID in .env (see get_credential_ids.py)")

# Connect. The SDK gets the login token for us.
api = CatalystCenterAPI(
    base_url=os.environ["CATALYST_CENTER_BASE_URL"],
    username=os.environ["CATALYST_CENTER_USERNAME"],
    password=os.environ["CATALYST_CENTER_PASSWORD"],
    version="2.3.7.9",  # nearest model in the SDK for Catalyst Center 2.3.7.10
    verify=os.getenv("CATALYST_CENTER_VERIFY_SSL", "true").lower() != "false",
)

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
    "globalCredentialIdList": cli_ids + snmp_ids,
}

# POST /dna/intent/api/v1/discovery
# active_validation=False because the SDK's model is for 2.3.7.9, not 2.3.7.10.
response = api.discovery.start_discovery(payload=body, active_validation=False)

# The call only starts the job and returns a task ID.
print("Discovery submitted. Task ID:", response["response"]["taskId"])
print("Check it with: python check_task.py <task-id>")
