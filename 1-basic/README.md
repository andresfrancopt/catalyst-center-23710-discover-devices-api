# Basic discovery scripts

> **Demonstration code only.** Provided for demonstration and example purposes, not supported and not tested for production use. The customer is solely responsible for reviewing, testing and validating it, and for any use or execution in their own environments, including production. Use is subject to the [Cisco Sample Code License, Version 1.1](../LICENSE).

The smallest working way to discover devices in Catalyst Center 2.3.7.10 with `catalystcentersdk`. Each script is one flat file you can read top to bottom. No helper modules.

| Script | What it does |
| --- | --- |
| [get_credential_ids.py](get_credential_ids.py) | Lists your global CLI and SNMPv2 read credentials with their IDs |
| [discover.py](discover.py) | Starts a discovery of the IPs in its `IPS` list |
| [discover_from_csv.py](discover_from_csv.py) | The same, but reads the IPs from the `ip_address` column of `devices.csv` (other columns are ignored) |
| [discover_without_sdk.py](discover_without_sdk.py) | The same discovery without `catalystcentersdk`: plain `requests`, same `.env`, same body. Shows the login and the two REST calls the SDK normally hides |
| [get_credential_ids_without_sdk.py](get_credential_ids_without_sdk.py) | The same credential listing without `catalystcentersdk`: plain `requests`, same `.env` |
| [check_task.py](check_task.py) | Checks a task once by its ID |

## Steps

1. Install the packages (from the project root): `python -m pip install -r requirements.txt`
2. Work from this folder: `cd 1-basic`. Then `cp .env.example .env` and fill in the URL, username and password. For a lab with a self-signed certificate set `CATALYST_CENTER_VERIFY_SSL=false`. (`.env` and `devices.csv` are git-ignored; the `.example` files are tracked.)
3. `python get_credential_ids.py`, then paste one CLI ID and one SNMP ID into `.env`.
4. Edit the `IPS` list in [discover.py](discover.py) and run `python discover.py`. Or `cp devices.example.csv devices.csv`, put your IPs in the `ip_address` column and run `python discover_from_csv.py`. It prints a task ID.
5. `python check_task.py <task-id>`. Run it again while it says "Still running".

## Limits (on purpose)

- One discovery job with one set of credentials for all devices. `CLI_CREDENTIAL_ID` and `SNMP_CREDENTIAL_ID` may each list several IDs separated by commas (for example `CLI_CREDENTIAL_ID=id1,id2`); Catalyst Center then tries them in order on every device. This works in all the basic discovery scripts. Trying many wrong credentials against a device can trigger account lockouts, so list only the ones you need and put the most likely first. It cannot say which device uses which credential; for that use the credential profiles in [2-standalone](../2-standalone).
- The task check only tells you whether the discovery job was accepted. Look at the discovery in the Catalyst Center GUI for per-device results (or use [2-standalone/check_task.py](../2-standalone/check_task.py) with `--discovery`).
- The IPs go as `Multi Range` with each IP as `ip-ip`. If your controller rejects that, use a single `Range` such as `"ipAddressList": "10.48.90.173-10.48.90.174"` with `"discoveryType": "Range"`.
- The SDK has no 2.3.7.10 model, so `version="2.3.7.9"` and `active_validation=False` are used. This works with a 2.3.7.10 controller: the model only selects the SDK's methods, and the calls go to the same API paths (see the version notes in the [project README](../README.md)).

For credential profiles, input checks, NETCONF, result checking, inventory and site assignment, see [2-standalone](../2-standalone/README.md) and the [project README](../README.md), which also compares the basic and the full script.
