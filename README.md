# Catalyst Center discovery scripts

> **Demonstration code only.** These scripts are sample code provided for demonstration and example purposes. They are not supported and have not been tested for production use. The customer is solely responsible for reviewing, testing, and validating them, and for any use or execution in their own environments, including production. Use is subject to the [Cisco Sample Code License, Version 1.1](LICENSE).

Start Catalyst Center discovery jobs (`POST /dna/intent/api/v1/discovery`) from Python.

| Script | IPs come from | Uses |
| --- | --- | --- |
| [discover_ips_in_code.py](discover_ips_in_code.py) | the `DEVICES` list at the top of the script | Catalyst Center SDK |
| [discover_from_csv.py](discover_from_csv.py) | a CSV file | Catalyst Center SDK |
| [reference_discover_without_sdk.py](reference_discover_without_sdk.py) | the `DEVICES` list (same as the first script) | plain `requests`, for comparison |
| [check_discovery.py](check_discovery.py) | a task ID or discovery ID from an earlier run | Catalyst Center SDK |

[discovery_common.py](discovery_common.py) holds the shared, SDK-free logic: `.env` loading, credential profiles and lookup, IP validation, the discovery payload, and result verification.

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
CRED_DEFAULT_CLI_IDS=CSS            # a credential ID or its description
CRED_DEFAULT_SNMPV2_RO_IDS=RO
CRED_BRANCH_CLI_IDS=<cli-id-2>,<cli-id-3>
CRED_BRANCH_SNMPV2_RO_IDS=<snmp-id-2>
```

Credential values are **IDs or descriptions, not passwords**: the actual device credentials stay in Catalyst Center. See [Credential values](#credential-values-id-or-description) below. Variables already set in the real environment take precedence over `.env`. TLS certificate verification is **on by default**. For a lab controller with a self-signed certificate, either point `CATALYST_CENTER_CA_BUNDLE` at its certificate (PEM), or set `CATALYST_CENTER_VERIFY_SSL=false` to skip verification. The two cannot be combined, and when verification is off the scripts print a warning on every run. Do not disable verification outside a lab.

## Credential values (ID or description)

Catalyst Center only accepts credential **IDs** (UUIDs) in a discovery job. If you put something else in `.env` (for example the description `CSS`), the job is accepted but then fails with `NCIM90257: Some of the selected global credentials are not available`. To avoid that, each value can be either an ID or the credential's description, and the scripts resolve it before submitting:

1. They list the controller's global credentials of the right type (`GET /dna/intent/api/v1/global-credential`, type `CLI` or `SNMPV2_READ_COMMUNITY`).
2. A value is matched to an `id` first, then to a `description` (ignoring case); a CLI value may also match a CLI `username`. A CLI credential cannot be used where an SNMP one is expected, and vice versa.
3. If nothing matches, or a description matches more than one credential, the scripts stop before submitting anything and list the available credentials as `description (id)`. Secrets are never printed.

`--dry-run` does not call the API, so it shows the values as written in `.env`.

## Different credentials for different devices (profiles)

Catalyst Center tries every credential in a job against every device in that job. If devices use different credentials, putting all of them in one job is slow and can cause failed logins (and lockouts). Instead, define one **profile** per credential set in `.env` and say which profile each device uses. The scripts group devices by profile and submit **one discovery job per profile**, containing only that profile's credentials (CLI first, then SNMP, in the order written). A profile may list several credentials if a group of devices could use any of a few. Both a CLI and an SNMPv2 read-only credential are required per profile.

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
- **Calling the API:** `api.discovery.start_discovery(payload=...)` versus building the URL, headers, and JSON body by hand. The same goes for the credential lookup and the task and discovery checks.
- **Errors:** the SDK raises typed exceptions (`ApiError`, `AccessTokenError`, ...); with plain `requests` you check status codes yourself.

## Output, verification and failures

`POST /dna/intent/api/v1/discovery` is asynchronous: it only returns a task ID, and problems (like unavailable credentials) show up later in the task or in the per-device results. So after submitting, the scripts follow each job:

1. **Task:** poll `GET /dna/intent/api/v1/task/{taskId}` until it finishes. If it failed, the error code and reason are printed (for example `NCIM90257`).
2. **Discovery:** read the discovery ID from the finished task and poll `GET /dna/intent/api/v1/discovery/{id}` until it is no longer running.
3. **Devices:** read `GET /dna/intent/api/v1/discovery/{id}/job` and print one line per IP with its ping, SNMP and CLI status and any error. An IP is `OK` only when all three succeeded.

```text
[branch] OK
  Task 01a1... completed
  Discovery 1234 "discover-branch": Complete, 1 device(s)
  - 10.48.91.10     OK         ping=Success snmp=Success cli=Success
```

Options for both main scripts (and the reference script):

- `--no-wait` submits only and prints the task IDs; check later with `check_discovery.py`.
- `--timeout N` is how long to wait per job (default 300 seconds, polling every 5). On timeout the job is reported as a problem with the command to check it later.
- `--raw` also prints the controller's raw task, discovery and job responses, with fields that look secret (password, passphrase, community, token) replaced by `***`. Use it to see the exact fields your controller returns.

The exit code is `0` only when every job succeeded. It is non-zero for a configuration error, a failed task, a device that is not `OK`, or a timeout. Configuration problems (unknown profile, invalid or duplicate IP, missing `.env` values, credentials that cannot be found) are reported before anything is submitted. If a submission fails, the scripts stop and list the profiles already submitted.

### Checking a job later

```sh
python check_discovery.py <task-id>                    # from the "submitted, task ..." line
python check_discovery.py --discovery-id <discovery-id>
python check_discovery.py <task-id> --timeout 0        # check once, do not wait
```

It prints the same report and uses the same exit codes. It only needs the API login in `.env`, not the credential profiles.

## Version compatibility: please verify

- SDK `catalystcentersdk` 3.1.6.0.0 has no 2.3.7.10 model. The scripts use the nearest earlier one (`2.3.7.9`) and turn off the SDK's request validation so the payload is sent exactly as supplied. The supplied body uses `timeOut` and `retryCount`, while the SDK's own model names these `timeout` and `retry`.
- The supplied body uses `discoveryType: "Range"` with a comma-separated IP list; the SDK documents comma-separated lists under `Multi Range` and a single `start-end` pair for `Range`. The values live in constants at the top of `discovery_common.py`.
- Run with `--dry-run` first and confirm the payload is accepted by your 2.3.7.10 controller.
- The verification step relies on response fields I could not check against a live 2.3.7.10 controller while building this: `isError`/`endTime`/`failureReason`/`errorCode` on the task, the discovery ID being in the finished task's `progress` field, `discoveryCondition`/`numDevices` on the discovery, and `ipAddress`/`pingStatus`/`snmpStatus`/`cliStatus`/`errorDescription` on each job entry. The parsers tolerate missing fields (the job is then reported as not verified rather than crashing). Run once with `--raw` on your lab to confirm the names. It was tested against a local simulated controller only.

## Tests

```sh
python -m unittest discover -s tests -v
```
