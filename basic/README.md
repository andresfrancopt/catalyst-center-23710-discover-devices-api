# Basic discovery scripts

> **Demonstration code only.** Provided for demonstration and example purposes, not supported and not tested for production use. The customer is solely responsible for reviewing, testing and validating it, and for any use or execution in their own environments, including production. Use is subject to the [Cisco Sample Code License, Version 1.1](../LICENSE).

The smallest working way to discover devices in Catalyst Center 2.3.7.10 with `catalystcentersdk`. Each script is one flat file you can read top to bottom. No helper modules.

| Script | What it does |
| --- | --- |
| [get_credential_ids.py](get_credential_ids.py) | Lists your global CLI and SNMPv2 read credentials with their IDs |
| [discover.py](discover.py) | Starts a discovery of the IPs in its `IPS` list |
| [check_task.py](check_task.py) | Checks a task once by its ID |

## Steps

1. Install the packages (from the project root): `pip install -r requirements.txt`
2. `cp basic/.env.example basic/.env`, then fill in the URL, username and password. For a lab with a self-signed certificate set `CATALYST_CENTER_VERIFY_SSL=false`.
3. `python basic/get_credential_ids.py`, then paste one CLI ID and one SNMP ID into `basic/.env`.
4. Edit the `IPS` list in [discover.py](discover.py) and run `python basic/discover.py`. It prints a task ID.
5. `python basic/check_task.py <task-id>`. Run it again while it says "Still running".

## Limits (on purpose)

- One CLI and one SNMPv2 read credential for all devices.
- The task check only tells you whether the discovery job was accepted. Look at the discovery in the Catalyst Center GUI for per-device results.
- The IPs go as `Multi Range` with each IP as `ip-ip`. If your controller rejects that, use a single `Range` such as `"ipAddressList": "10.48.90.173-10.48.90.174"` with `"discoveryType": "Range"`.
- The SDK has no 2.3.7.10 model, so `version="2.3.7.9"` and `active_validation=False` are used.

For credential profiles, CSV input, result checking, NETCONF, inventory and site assignment, see the main [README](../README.md).
