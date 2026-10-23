# Catalyst Center discovery scripts

> **Demonstration code only.** These scripts are sample code provided for demonstration and example purposes. They are not supported and have not been tested for production use. The customer is solely responsible for reviewing, testing, and validating them, and for any use or execution in their own environments, including production. Use is subject to the [Cisco Sample Code License, Version 1.1](LICENSE).

Python scripts for Cisco Catalyst Center 2.3.7.10 that start a discovery (`POST /dna/intent/api/v1/discovery`), check the result, and manage the devices and credentials around it. They are meant to be read as much as run: the same job is done at three levels of detail, so you can start with the smallest one and move up when you need more.

## Which folder should I use?

| Folder | What it is | Start here if |
| --- | --- | --- |
| [1-basic](1-basic) | The smallest working scripts: about 50 to 90 lines each, flat, no helpers. One CLI and one SNMP credential. | You want to see how little code a discovery needs. |
| [2-standalone](2-standalone) | Complete scripts, each in a single file: credential profiles, input checks, dry run, NETCONF, result tracking, site assignment, inventory export, credential management. | You want something you can actually use, and copy one file at a time. |
| [3-shared-module](3-shared-module) | Four of the same scripts, built on one shared helper module (`discovery_common.py`). | You would rather maintain the common code once than copy it into each script. |

Each folder has its own `.env.example`, `devices.example.csv` and README.

## Setup (Python 3.14.3, see `.python-version`)

```sh
python -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt

cd 1-basic                              # or 2-standalone, or 3-shared-module
cp .env.example .env                    # then edit it
cp devices.example.csv devices.csv      # then edit it (only for the scripts that read a CSV)
```

Run the scripts from inside their folder. `.env` and `devices.csv` hold your own settings and addresses; they are git-ignored, while the `.example` files are tracked. TLS certificate verification is on by default; for a lab controller with a self-signed certificate set `CATALYST_CENTER_VERIFY_SSL=false` (the scripts then print a warning on every run). Do not disable it outside a lab.

## 1. Basic ([1-basic](1-basic))

| Script | What it does |
| --- | --- |
| [get_credential_ids.py](1-basic/get_credential_ids.py) | Lists your global CLI and SNMPv2 read credentials with their IDs |
| [discover.py](1-basic/discover.py) | Discovers the IPs in its `IPS` list |
| [discover_from_csv.py](1-basic/discover_from_csv.py) | Discovers the IPs in the `ip_address` column of `devices.csv` |
| [check_task.py](1-basic/check_task.py) | Checks a task once by its ID |
| `*_without_sdk.py` | The same discovery and credential listing with plain `requests`, to compare with the SDK versions |

See [1-basic/README.md](1-basic/README.md).

## 2. Standalone ([2-standalone](2-standalone))

| Script | What it does |
| --- | --- |
| [discover_ips.py](2-standalone/discover_ips.py) | Discovery for the IPs in its `DEVICES` list |
| [discover_csv.py](2-standalone/discover_csv.py) | Discovery for the IPs in a CSV file |
| [check_task.py](2-standalone/check_task.py) | Checks a task by ID, and optionally the devices it discovered |
| [manage_credentials.py](2-standalone/manage_credentials.py) | Lists, creates and checks global CLI and SNMPv2 read-only credentials |
| [list_devices.py](2-standalone/list_devices.py) | Lists the inventory on screen and in a CSV file, paging through large inventories |
| [assign_site.py](2-standalone/assign_site.py) | Assigns devices to a site |

Each file contains everything it needs and imports only the standard library and the packages in [requirements.txt](requirements.txt). See [2-standalone/README.md](2-standalone/README.md) for credential profiles, NETCONF, how results are verified, and every option.

## 3. Shared module ([3-shared-module](3-shared-module))

[discover_ips_in_code.py](3-shared-module/discover_ips_in_code.py), [discover_from_csv.py](3-shared-module/discover_from_csv.py), [check_discovery.py](3-shared-module/check_discovery.py) and [assign_devices_to_site.py](3-shared-module/assign_devices_to_site.py) behave like their standalone twins, but share [discovery_common.py](3-shared-module/discovery_common.py) (settings, credential matching, IP ranges, result parsing). That module does not import the SDK, so it is easy to test; the scripts call the SDK. See [3-shared-module/README.md](3-shared-module/README.md).

## What the longer script adds

[1-basic/discover_from_csv.py](1-basic/discover_from_csv.py) (84 lines, 4 imports) and [2-standalone/discover_csv.py](2-standalone/discover_csv.py) (about 800 lines, 32 functions and classes, 17 imports) read the same CSV and start a discovery. The point of the comparison: once the script is Python and not a one-off request, ordinary programming logic lets it make decisions, check its input and react to what happens.

| | Basic | Standalone |
| --- | --- | --- |
| Credentials | One CLI and one SNMP ID from `.env`, tried on every device | Named profiles; a `credential_profile` column picks the credentials per device, one discovery job per profile |
| Credential values | Raw IDs only | An ID, a description or a CLI username, looked up on the controller; unknown or ambiguous values stop the run with the list of what exists |
| Input checks | None; the controller reports a bad value later | Invalid or repeated IPs, unknown profiles, a missing column or empty file are reported before anything is sent |
| IP list | Every IP as its own range | The most compact valid form (single, range or multi-range) that covers only the listed IPs |
| Before sending | Nothing | `--dry-run` shows the exact request; `--name-prefix` sets job names |
| Options | None | `--netconf` and `--netconf-port`, `--no-wait`, `--timeout`, `--raw` |
| After sending | Prints a task ID; you check it yourself | Follows the task, then the discovery, then each device (ping, SNMP, CLI, NETCONF), with timeouts and a clear message for a failed task or a deleted discovery |
| Automation | Always exits 0 unless it crashes | Exit code 0 only when every job succeeded |
| Safety | Whatever the SDK prints on error | TLS options (CA bundle, lab-only switch with a warning), secrets redacted from raw output, short error messages instead of tracebacks |
| Setup | `.env` required | `.env` optional; real environment variables win |

The price is size and effort: about ten times the code, argparse, dataclasses, and roughly thirty functions to read, test and keep correct, plus the same shared logic copied into several files (see the trade-off in [2-standalone/README.md](2-standalone/README.md)). Start basic; add only the pieces you need.

## With or without the SDK

`1-basic` has plain-`requests` versions of two scripts next to the SDK ones, so you can compare them. The difference:

- **Authentication:** the SDK requests the token from `/dna/system/api/v1/auth/token` and attaches it to every call. Without it you make the Basic-auth request and set the `X-Auth-Token` header yourself.
- **Calling the API:** `api.discovery.start_discovery(payload=...)` versus building the URL, headers and JSON body by hand.
- **Errors:** the SDK raises typed exceptions (`ApiError`, `AccessTokenError`, ...); with plain `requests` you check status codes yourself.

## Version compatibility: please verify

- SDK `catalystcentersdk` 3.1.6.0.0 has no 2.3.7.10 model. The scripts use the nearest earlier one (`2.3.7.9`) and turn off the SDK's request validation so the payload is sent exactly as supplied. The discovery body uses `timeOut` and `retryCount`, while the SDK's own model names these `timeout` and `retry`.
- **IP list format:** a discovery with `discoveryType: "Range"` and a comma-separated list (`10.48.90.173,10.48.90.174`) is rejected with `[BadRequest] IPAddress for Range Discovery is invalid`, because `Range` takes a single `start-end` pair. The scripts pick the format from the IPs of each job: one IP is `Single`; one contiguous block is `Range` (`10.48.90.173-10.48.90.174`); anything else is `Multi Range` (`10.0.0.1-10.0.0.2,10.0.0.9-10.0.0.9`). Only the IPs you list are covered, never the addresses in the gaps. The `Single` and `Multi Range` formats come from the SDK's documentation and have not been confirmed against a 2.3.7.10 controller.
- `netconfPort` was accepted by the lab controller and echoed back in the discovery (`"netconfPort": "830"`).
- The verification step was checked against a real 2.3.7.10 lab for: the task (`isError`, `endTime`, `data` with `discoveryId`/`jobTaskId`, `progress`), the discovery (`discoveryCondition`, `numDevices`, `deviceIds`, `netconfPort`, `globalCredentialIdList`) and the `/job` record (`jobStatus`, `startTime`, `endTime`, `taskId`, `name`). The task failure fields (`errorCode`, `failureReason`) and the per-device fields of `/discovery/{id}/network-device` (`managementIpAddress`, `hostname`, `pingStatus`, `snmpStatus`, `cliStatus`, `netconfStatus`, `errorDescription`, taken from the 2.3.7.9 API documentation) have not been seen live; if that call returns nothing usable, the job-status summary is shown instead. The parsers tolerate missing fields instead of crashing; run with `--raw` to see the controller's own responses (secrets redacted).
- Run with `--dry-run` first and confirm the request is accepted by your controller.

## Tests

```sh
python -m unittest discover -s tests -v
```

The suite includes a test that every script in `2-standalone` runs on its own from an otherwise empty folder, and tests that compare the standalone code with `3-shared-module/discovery_common.py`, so the two cannot quietly drift apart.
