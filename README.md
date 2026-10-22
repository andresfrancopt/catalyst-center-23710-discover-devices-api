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

There is also a set of **fully standalone** scripts in [standalone/](standalone) that do the same jobs (plus credential management) without sharing any code; see [Standalone scripts](#standalone-scripts).

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

## Standalone scripts

The scripts in [standalone/](standalone) each contain everything they need in one file, in labelled sections (settings, devices, credentials, verification, main flow). They import only the Python standard library and the packages in [requirements.txt](requirements.txt) (`catalystcentersdk`, `python-dotenv`, `requests`), never `discovery_common.py` or each other. That makes them easy to read top to bottom, and any one of them can be copied somewhere else together with a `.env` file.

| Script | What it does |
| --- | --- |
| [standalone/discover_ips.py](standalone/discover_ips.py) | Discovery for the IPs in its `DEVICES` list |
| [standalone/discover_csv.py](standalone/discover_csv.py) | Discovery for the IPs in a CSV file |
| [standalone/check_task.py](standalone/check_task.py) | Check a task by ID (and optionally the devices it discovered) |
| [standalone/manage_credentials.py](standalone/manage_credentials.py) | List, create and check global CLI and SNMPv2 read-only credentials |

They read the **same `.env` variables** as the other scripts. The `.env` file is looked for next to the script first, then in the folder you run it from, so from the project root you can run `python standalone/discover_ips.py`. Unlike the other scripts, a `.env` file is **optional** here: if none is found, they use the settings already present in the environment (handy in a container or CI), and only complain, naming the missing settings, if required ones are absent. Variables set in the environment always win over `.env`. `check_task.py` and `manage_credentials.py` only need the API login variables.

The discovery scripts behave exactly like `discover_ips_in_code.py` and `discover_from_csv.py`: credential profiles, ID-or-description lookup, `Single`/`Range`/`Multi Range` formatting, `--dry-run`, `--no-wait`, `--timeout`, `--raw`, and the same exit codes. Tests compare their results with `discovery_common.py` so the two sets cannot quietly drift apart.

```sh
python standalone/discover_ips.py --dry-run
python standalone/discover_csv.py devices.csv --name-prefix office
```

### Checking a task by ID

```sh
python standalone/check_task.py <task-id>               # running, failed (code and reason) or completed
python standalone/check_task.py <task-id> --discovery   # also report each device (ping, SNMP, CLI)
python standalone/check_task.py --discovery-id <id>     # check a discovery directly
python standalone/check_task.py <task-id> --timeout 0   # check once, do not wait
```

It works for any task, not only discovery. The exit code is `0` only when the task completed (and, with `--discovery`, every device is `OK`).

### Creating and checking global credentials

```sh
python standalone/manage_credentials.py list
python standalone/manage_credentials.py check CSS            # by description, ID or (CLI) username
python standalone/manage_credentials.py create-cli --description lab-cli --username admin
python standalone/manage_credentials.py create-snmp-ro --description lab-ro
```

- **Secrets:** the CLI password, the optional enable password and the SNMP read community are typed at a hidden prompt (twice). They are never accepted as command-line arguments, so they do not end up in your shell history or the process list, and they are never printed, including in error messages and `--raw` output.
- **Unattended runs:** add `--from-env` and set `NEW_CLI_PASSWORD`, `NEW_CLI_ENABLE_PASSWORD` (optional) or `NEW_SNMP_RO_COMMUNITY` in the environment instead of prompting. Prefer exporting them in the shell for one run rather than saving them in `.env`.
- **No duplicates:** if a credential of that type with the same description (ignoring case) already exists, the script reports its ID and changes nothing. It checks first, so you are not even asked for the secrets in that case.
- **Confirmation:** after creating, the script lists the credentials again (waiting up to `--timeout` seconds, default 30) and only reports success when the new credential is really there. If the create call returns a task, the script follows it and reports the controller's error code and reason on failure.
- **`--dry-run`** shows the request with the secrets masked and calls nothing; it needs neither a `.env` file nor a connection.
- **Use the result:** put the description (for example `lab-cli`) or the reported ID into `CRED_<PROFILE>_CLI_IDS` / `CRED_<PROFILE>_SNMPV2_RO_IDS` in `.env`.

The request fields (`description`, `username`, `password`, `enablePassword`, `comments` and `credentialType: GLOBAL` for CLI; `description`, `readCommunity`, `comments` and `credentialType` for SNMPv2) come from the SDK's own request schemas. What the controller returns from the create call was not confirmed against a live controller, which is why the script confirms by listing. Creating a credential changes the controller, so run `--dry-run` first.

### The trade-off

Because each file carries its own copy of the shared code (settings, IP and range handling, credential lookup, polling, redaction), a fix has to be applied in each file that has it. Use the standalone scripts when you want something simple to read or hand over; use the `discovery_common.py`-based scripts when you would rather maintain that code once.

## Version compatibility: please verify

- SDK `catalystcentersdk` 3.1.6.0.0 has no 2.3.7.10 model. The scripts use the nearest earlier one (`2.3.7.9`) and turn off the SDK's request validation so the payload is sent exactly as supplied. The supplied body uses `timeOut` and `retryCount`, while the SDK's own model names these `timeout` and `retry`.
- **IP list format:** your original body used `discoveryType: "Range"` with a comma-separated list (`10.48.90.173,10.48.90.174`). The controller rejects that with `[BadRequest] IPAddress for Range Discovery is invalid`, because `Range` takes a single `start-end` pair. The scripts now pick the format from the IPs of each job: one IP is `Single` (`10.48.90.173`); one contiguous block is `Range` (`10.48.90.173-10.48.90.174`); anything else is `Multi Range` (`10.0.0.1-10.0.0.2,10.0.0.9-10.0.0.9`). Only the IPs you list are covered, never the addresses in the gaps. The `Range`, `Single` and `Multi Range` formats come from the SDK's documentation; none of them has been confirmed against your 2.3.7.10 controller yet (the error above only shows that the comma-separated list is rejected).
- Run with `--dry-run` first and confirm the payload is accepted by your 2.3.7.10 controller.
- The verification step relies on response fields I could not check against a live 2.3.7.10 controller while building this: `isError`/`endTime`/`failureReason`/`errorCode` on the task, the discovery ID being in the finished task's `progress` field, `discoveryCondition`/`numDevices` on the discovery, and `ipAddress`/`pingStatus`/`snmpStatus`/`cliStatus`/`errorDescription` on each job entry. The parsers tolerate missing fields (the job is then reported as not verified rather than crashing). Run once with `--raw` on your lab to confirm the names. It was tested against a local simulated controller only.

## Reading the code

Every script starts with a short "reading guide" comment, and the code is commented in short, plain-English notes. A few of them carry a tag at the start, so you can scan or search for them (for example, search for `[credentials]`):

- `[main task]`: where the script does its real work (submitting a discovery, creating a credential, waiting for and reporting the results).
- `[credentials]`: where credentials or secrets are read from `.env` or the environment, looked up on the controller, typed at a prompt, or kept out of printed output.
- `[error handling]`: where input is validated up front, or an error is caught and turned into a clear message.

In the main scripts the steps of `main()` are numbered (Step 1 to Step 5), so you can follow the flow from top to bottom. Wherever the same code appears in several files, the comment text is the same too.

## Tests

```sh
python -m unittest discover -s tests -v
```

The suite includes a test that every file in `standalone/` runs on its own from an otherwise empty folder, and tests that compare the standalone code with `discovery_common.py`.
