# Catalyst Center discovery scripts

> **Demonstration code only.** These scripts are sample code provided for demonstration and example purposes. They are not supported and have not been tested for production use. The customer is solely responsible for reviewing, testing, and validating them, and for any use or execution in their own environments, including production. Use is subject to the [Cisco Sample Code License, Version 1.1](LICENSE).

Start Catalyst Center discovery jobs (`POST /dna/intent/api/v1/discovery`) from Python.

| Script | IPs come from | Uses |
| --- | --- | --- |
| [discover_ips_in_code.py](discover_ips_in_code.py) | the `DEVICES` list at the top of the script | Catalyst Center SDK |
| [discover_from_csv.py](discover_from_csv.py) | a CSV file | Catalyst Center SDK |
| [reference_discover_without_sdk.py](reference_discover_without_sdk.py) | the `DEVICES` list (same as the first script) | plain `requests`, for comparison |

[discovery_common.py](discovery_common.py) holds the shared, SDK-free logic: `.env` loading, credential profiles, IP validation, and the discovery payload.

## Setup (Python 3.14.3, see `.python-version`)

```sh
python -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env      # then edit .env; it is git-ignored
```

## `.env`

```ini
CATALYST_CENTER_BASE_URL=https://catalyst-center.example.com
CATALYST_CENTER_USERNAME=api-user
CATALYST_CENTER_PASSWORD=...
CATALYST_CENTER_CA_BUNDLE=          # optional PEM for an internal CA
CATALYST_CENTER_VERIFY_SSL=true     # lab only: false skips certificate checks

CREDENTIAL_PROFILES=default,branch  # first one is the default
CRED_DEFAULT_CLI_IDS=<cli-id-1>
CRED_DEFAULT_SNMPV2_RO_IDS=<snmp-id-1>
CRED_BRANCH_CLI_IDS=<cli-id-2>,<cli-id-3>
CRED_BRANCH_SNMPV2_RO_IDS=<snmp-id-2>
```

Values are Catalyst Center **credential IDs**, not passwords: the actual device credentials stay in Catalyst Center. Variables already set in the real environment take precedence over `.env`. TLS certificate verification is **on by default**. For a lab controller with a self-signed certificate, either point `CATALYST_CENTER_CA_BUNDLE` at its certificate (PEM), or set `CATALYST_CENTER_VERIFY_SSL=false` to skip verification. The two cannot be combined, and when verification is off the scripts print a warning on every run. Do not disable verification outside a lab.

## Different credentials for different devices (profiles)

Catalyst Center tries every credential ID in a job against every device in that job. If devices use different credentials, putting all IDs in one job is slow and can cause failed logins (and lockouts). Instead, define one **profile** per credential set in `.env` and say which profile each device uses. The scripts group devices by profile and submit **one discovery job per profile**, containing only that profile's IDs (CLI IDs first, then SNMP IDs, in the order written). A profile may list several IDs if a group of devices could use any of a few credentials. Both a CLI and an SNMPv2 read-only ID are required per profile.

Job names are `<prefix>-<profile>` (for example `discover-branch`). If Catalyst Center rejects a repeated name, use a different prefix.

## Script 1: IPs in the code

Edit `DEVICES` (IP, profile; `None` means the default profile), then:

```sh
python discover_ips_in_code.py --dry-run   # show the payloads, call nothing
python discover_ips_in_code.py
```

## Script 2: IPs from a CSV

[devices.example.csv](devices.example.csv):

```csv
ip_address,credential_profile
10.48.90.173,default
10.48.91.10,branch
10.48.91.11,
```

`credential_profile` is optional; an empty or missing value uses the default profile. Other columns are ignored.

```sh
python discover_from_csv.py devices.csv --name-prefix office --dry-run
python discover_from_csv.py devices.csv --name-prefix office
```

## With vs. without the SDK

`reference_discover_without_sdk.py` does the same as script 1 with `requests`. The difference:

- **Authentication:** the SDK requests the token from `/dna/system/api/v1/auth/token` and attaches it to every call. Without it you do the Basic-auth request and set the `X-Auth-Token` header yourself.
- **Calling the API:** `api.discovery.start_discovery(payload=...)` versus building the URL, headers, and JSON body by hand.
- **Errors:** the SDK raises typed exceptions (`ApiError`, `AccessTokenError`, ...); with plain `requests` you check status codes yourself.

## Output and failures

Each script prints the API response per profile (it normally contains a task ID; discovery runs asynchronously and the scripts do not wait for it). Any configuration problem (unknown profile, invalid or duplicate IP, missing `.env` values) is reported before any API call. If a job fails, the scripts stop and list the profiles already submitted. Exit code is non-zero on any error.

## Version compatibility: please verify

- SDK `catalystcentersdk` 3.1.6.0.0 has no 2.3.7.10 model. The scripts use the nearest earlier one (`2.3.7.9`) and turn off the SDK's request validation so the payload is sent exactly as supplied. The supplied body uses `timeOut` and `retryCount`, while the SDK's own model names these `timeout` and `retry`.
- The supplied body uses `discoveryType: "Range"` with a comma-separated IP list; the SDK documents comma-separated lists under `Multi Range` and a single `start-end` pair for `Range`. The values live in constants at the top of `discovery_common.py`.
- Run with `--dry-run` first and confirm the payload is accepted by your 2.3.7.10 controller. No live discovery was run while building this project.

## Tests

```sh
python -m unittest discover -s tests -v
```
