# Basic discovery scripts

> **Demonstration code only.** Provided for demonstration and example purposes, not supported and not tested for production use. The customer is solely responsible for reviewing, testing and validating it, and for any use or execution in their own environments, including production. Use is subject to the [Cisco Sample Code License, Version 1.1](../LICENSE).

The smallest working way to discover devices in Catalyst Center 2.3.7.10 with `catalystcentersdk`. Each script is one flat file you can read top to bottom. No helper modules.

| Script | What it does |
| --- | --- |
| [get_credential_ids.py](get_credential_ids.py) | Lists your global CLI and SNMPv2 read credentials with their IDs |
| [discover.py](discover.py) | Starts a discovery of the IPs in its `IPS` list |
| [discover_from_csv.py](discover_from_csv.py) | The same, but reads the IPs from the `ip_address` column of `devices.csv` (other columns are ignored, so the main project's `devices.csv` works) |
| [discover_without_sdk.py](discover_without_sdk.py) | The same discovery without `catalystcentersdk`: plain `requests`, same `.env`, same body. Shows the login and the two REST calls the SDK normally hides |
| [get_credential_ids_without_sdk.py](get_credential_ids_without_sdk.py) | The same credential listing without `catalystcentersdk`: plain `requests`, same `.env` |
| [check_task.py](check_task.py) | Checks a task once by its ID |

## Steps

1. Install the packages (from the project root): `pip install -r requirements.txt`
2. `cp basic/.env.example basic/.env`, then fill in the URL, username and password. For a lab with a self-signed certificate set `CATALYST_CENTER_VERIFY_SSL=false`.
3. `python basic/get_credential_ids.py`, then paste one CLI ID and one SNMP ID into `basic/.env`.
4. Edit the `IPS` list in [discover.py](discover.py) and run `python basic/discover.py`. Or put the IPs in `devices.csv` (header `ip_address`) and run `python basic/discover_from_csv.py` from the project folder. It prints a task ID.
5. `python basic/check_task.py <task-id>`. Run it again while it says "Still running".

## Limits (on purpose)

- One discovery job with one set of credentials for all devices. `CLI_CREDENTIAL_ID` and `SNMP_CREDENTIAL_ID` may each list several IDs separated by commas (for example `CLI_CREDENTIAL_ID=id1,id2`); Catalyst Center then tries them in order on every device. This works in all the basic discovery scripts. Trying many wrong credentials against a device can trigger account lockouts, so list only the ones you need and put the most likely first. It cannot say which device uses which credential; for that use the credential profiles in the main scripts.
- The task check only tells you whether the discovery job was accepted. Look at the discovery in the Catalyst Center GUI for per-device results.
- The IPs go as `Multi Range` with each IP as `ip-ip`. If your controller rejects that, use a single `Range` such as `"ipAddressList": "10.48.90.173-10.48.90.174"` with `"discoveryType": "Range"`.
- The SDK has no 2.3.7.10 model, so `version="2.3.7.9"` and `active_validation=False` are used.

For credential profiles, CSV input, result checking, NETCONF, inventory and site assignment, see the main [README](../README.md).
