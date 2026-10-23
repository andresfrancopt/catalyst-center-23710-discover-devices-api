# 2-standalone: complete scripts, one file each

> **Demonstration code only.** Provided for demonstration and example purposes, not supported and not tested for production use. The customer is solely responsible for reviewing, testing and validating it, and for any use or execution in their own environments, including production. Use is subject to the [Cisco Sample Code License, Version 1.1](../LICENSE).

Each script contains everything it needs in one file, in labelled sections (settings, devices, credentials, verification, main flow). It imports only the Python standard library and the packages in [requirements.txt](../requirements.txt) (`catalystcentersdk`, `python-dotenv`, `requests`), never another script. That makes each one easy to read top to bottom, and any one of them can be copied somewhere else together with a `.env` file.

| Script | What it does |
| --- | --- |
| [discover_ips.py](discover_ips.py) | Discovery for the IPs in its `DEVICES` list |
| [discover_csv.py](discover_csv.py) | Discovery for the IPs in a CSV file |
| [check_task.py](check_task.py) | Check a task by ID (and optionally the devices it discovered) |
| [manage_credentials.py](manage_credentials.py) | List, create and check global CLI and SNMPv2 read-only credentials |
| [list_devices.py](list_devices.py) | List the inventory on screen and in a CSV file (pages through large inventories) |
| [assign_site.py](assign_site.py) | Assign devices to a site |

Run everything from this folder. See also the [project README](../README.md) for how this folder compares with `1-basic` and `3-shared-module`.

## Setup

```sh
cp .env.example .env              # then edit it; it is git-ignored
cp devices.example.csv devices.csv
```

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

The `.env` file is looked for next to the script first, then in the folder you run it from. It is **optional**: if none is found, the scripts use the settings already present in the environment (handy in a container or CI), and only complain, naming the missing settings, if required ones are absent. Variables set in the environment always win over `.env`. `check_task.py`, `list_devices.py`, `assign_site.py` and `manage_credentials.py` only need the API login variables (the discovery scripts also need the credential profiles).

Credential values are **IDs or descriptions, not passwords**: the actual device credentials stay in Catalyst Center. TLS certificate verification is **on by default**. For a lab controller with a self-signed certificate, either point `CATALYST_CENTER_CA_BUNDLE` at its certificate (PEM), or set `CATALYST_CENTER_VERIFY_SSL=false`. The two cannot be combined, and when verification is off the scripts print a warning on every run.

### Credential values (ID or description)

Catalyst Center only accepts credential **IDs** (UUIDs) in a discovery job. If you put something else in `.env` (for example the description `CSS`), the job is accepted but then fails with `NCIM90257: Some of the selected global credentials are not available`. To avoid that, each value can be either an ID or the credential's description, and the scripts resolve it before submitting:

1. They list the controller's global credentials of the right type (`GET /dna/intent/api/v1/global-credential`, type `CLI` or `SNMPV2_READ_COMMUNITY`).
2. A value is matched to an `id` first, then to a `description` (ignoring case); a CLI value may also match a CLI `username`. A CLI credential cannot be used where an SNMP one is expected, and vice versa.
3. If nothing matches, or a description matches more than one credential, the scripts stop before submitting anything and list the available credentials as `description (id)`. Secrets are never printed.

`--dry-run` does not call the API, so it shows the values as written in `.env`.

### Different credentials for different devices (profiles)

Catalyst Center tries every credential in a job against every device in that job. If devices use different credentials, putting all of them in one job is slow and can cause failed logins (and lockouts). Instead, define one **profile** per credential set in `.env` and say which profile each device uses. The scripts group devices by profile and submit **one discovery job per profile**, containing only that profile's credentials (CLI first, then SNMP, in the order written). A profile may list several credentials if a group of devices could use any of a few. Both a CLI and an SNMPv2 read-only credential are required per profile.

Job names are `<prefix>-<profile>` (for example `discover-branch`). If Catalyst Center rejects a repeated name, use a different prefix.

## Discovery: `discover_ips.py` and `discover_csv.py`

`discover_ips.py`: edit `DEVICES` (IP, profile; `None` means the default profile), then:

```sh
python discover_ips.py --dry-run     # show the requests, call nothing
python discover_ips.py
```

`discover_csv.py`: a CSV with an `ip_address` column and an optional `credential_profile` column (an empty or missing value uses the default profile; other columns are ignored):

```csv
ip_address,credential_profile,site
10.48.90.173,default,Global/London/Building1/Floor1
10.48.91.10,branch,Global/Paris/Building2
10.48.91.11,,
```

```sh
python discover_csv.py devices.csv --name-prefix office --dry-run
python discover_csv.py devices.csv --name-prefix office
```

### Optional NETCONF

NETCONF is **off by default**, so nothing changes unless you ask for it:

```sh
python discover_csv.py devices.csv --netconf --dry-run          # NETCONF on port 830
python discover_csv.py devices.csv --netconf-port 8830          # another port
```

`--netconf` and `--netconf-port N` cannot be used together. When enabled, the discovery request gets a `netconfPort` field (a string, as the SDK documents it) and the script prints one line saying so; `--dry-run` shows the field. NETCONF needs valid SSH credentials, so the SSH CLI credential in the profile is still required (the discovery keeps `protocolOrder` set to `ssh`). The switch applies to every profile in that run. The lab controller accepted the field and echoed `"netconfPort": "830"` in the discovery.

### Output, verification and failures

`POST /dna/intent/api/v1/discovery` is asynchronous: it only returns a task ID, and problems (like unavailable credentials) show up later in the task or in the per-device results. So after submitting, the scripts follow each job:

1. **Task:** poll `GET /dna/intent/api/v1/task/{taskId}` until it finishes. If it failed, the error code and reason are printed (for example `NCIM90257`).
2. **Discovery:** read the discovery ID from the finished task (`data.discoveryId`, falling back to `progress`) and poll `GET /dna/intent/api/v1/discovery/{id}` until it is no longer running. A discovery that no longer exists (deleted in Catalyst Center) is reported as not found.
3. **Devices:** read `GET /dna/intent/api/v1/discovery/{id}/network-device` (SDK `get_discovered_network_devices_by_discovery_id`), which the API documents with one entry per discovered device and its ping, SNMP, CLI and NETCONF status at discovery time. One line is printed per device, for example `10.48.90.174  ATTENTION  ping=SUCCESS snmp=SUCCESS cli=SUCCESS netconf=FAILURE (sw2): NETCONF connection refused`. A device is `OK` only when ping, SNMP and CLI succeeded and, if NETCONF was tried (`--netconf`), NETCONF too; `netconf=` is hidden when NETCONF was not used, and the HTTP status is ignored.

   If that call fails, is empty or has no usable fields, the scripts fall back to `GET /dna/intent/api/v1/discovery/{id}/job`. On Catalyst Center 2.3.7.10 that is a single job record (`jobStatus`, start and end time), so the output shows `discovery job status: SUCCESS` (or the failing status) and a note that per-device results are not available. In both cases a discovery that found 0 devices is a problem. A record with none of the expected fields is shown as `unrecognised result (fields: ...)`; run with `--raw` and share the output.

Catalyst Center does **not** report which of several credentials worked, only the status per protocol. For anything finer, use the Catalyst Center GUI, or `list_devices.py` for the inventory.

```text
[branch] OK
  Task 01a1... completed
  Discovery 1234 "discover-branch": Complete, 1 device(s)
  - 10.48.91.10     OK         ping=SUCCESS snmp=SUCCESS cli=SUCCESS (sw1)
```

Options:

- `--no-wait` submits only and prints the task IDs; check them later with `check_task.py`.
- `--timeout N` is how long to wait per job (default 300 seconds, polling every 5). On timeout the job is reported as a problem with the command to check it later.
- `--raw` also prints the controller's raw task, discovery and device responses, with fields that look secret (password, passphrase, community, token) replaced by `***`. Use it to see the exact fields your controller returns.

The exit code is `0` only when every job succeeded. It is non-zero for a configuration error, a failed task, a device that is not `OK`, or a timeout. Configuration problems (unknown profile, invalid or duplicate IP, missing `.env` values, credentials that cannot be found) are reported before anything is submitted. If a submission fails, the script stops and lists the profiles already submitted.

## Checking a task by ID: `check_task.py`

```sh
python check_task.py <task-id>               # running, failed (code and reason) or completed
python check_task.py <task-id> --discovery   # also report each device
python check_task.py --discovery-id <id>     # check a discovery directly
python check_task.py <task-id> --timeout 0   # check once, do not wait
```

It works for any task, not only discovery. The exit code is `0` only when the task completed (and, with `--discovery`, every device is `OK`).

## Creating and checking global credentials: `manage_credentials.py`

```sh
python manage_credentials.py list
python manage_credentials.py check CSS            # by description, ID or (CLI) username
python manage_credentials.py create-cli --description lab-cli --username admin
python manage_credentials.py create-snmp-ro --description lab-ro
```

- **Secrets:** the CLI password, the optional enable password and the SNMP read community are typed at a hidden prompt (twice). They are never accepted as command-line arguments, so they do not end up in your shell history or the process list, and they are never printed, including in error messages and `--raw` output.
- **Unattended runs:** add `--from-env` and set `NEW_CLI_PASSWORD`, `NEW_CLI_ENABLE_PASSWORD` (optional) or `NEW_SNMP_RO_COMMUNITY` in the environment instead of prompting. Prefer exporting them in the shell for one run rather than saving them in `.env`.
- **No duplicates:** if a credential of that type with the same description (ignoring case) already exists, the script reports its ID and changes nothing. It checks first, so you are not even asked for the secrets in that case.
- **Confirmation:** after creating, the script lists the credentials again (waiting up to `--timeout` seconds, default 30) and only reports success when the new credential is really there. If the create call returns a task, the script follows it and reports the controller's error code and reason on failure.
- **`--dry-run`** shows the request with the secrets masked and calls nothing; it needs neither a `.env` file nor a connection.
- **Use the result:** put the description (for example `lab-cli`) or the reported ID into `CRED_<PROFILE>_CLI_IDS` / `CRED_<PROFILE>_SNMPV2_RO_IDS` in `.env`.

The request fields (`description`, `username`, `password`, `enablePassword`, `comments` and `credentialType: GLOBAL` for CLI; `description`, `readCommunity`, `comments` and `credentialType` for SNMPv2) come from the SDK's own request schemas. What the controller returns from the create call was not confirmed against a live controller, which is why the script confirms by listing. Creating a credential changes the controller, so run `--dry-run` first.

## Listing the inventory to screen and CSV: `list_devices.py`

```sh
python list_devices.py                      # every device: table on screen + inventory-<date>-<time>.csv
python list_devices.py --unassigned         # only devices that are not assigned to a site
python list_devices.py --ip "10.48.90..*" --role ACCESS --reachability Unreachable
python list_devices.py --with-site --output inventory.csv --overwrite
python list_devices.py --no-csv             # screen only
```

- **Paging:** the SDK does **not** page for you. One request returns at most 500 devices (`limit` up to 500, `offset` starting at 1), and the controller's own documentation tells you to make repeated calls for more. The script asks for page after page (`offset` 1, 501, 1001, ...) and stops at the first page that is shorter than the page size, so inventories of any size are read completely. It stops with a clear error if the controller returns the same page twice (it ignored `offset`) or after an absurd number of pages. `--page-size N` (1-500) changes the page size, and progress is shown on stderr. The count endpoint is deliberately not used, because it supports fewer filters than the list.
- **Columns:** the CSV has `id, hostname, management_ip, platform, family, role, software_version, serial_number, mac_address, reachability, collection_status, error_code, error_description, uptime, last_updated` (plus `site` with `--with-site`). The screen shows a narrower selection so lines stay readable.
- **Filters** (one value or several separated by commas; `.*` is a wildcard): `--ip`, `--hostname`, `--family`, `--role`, `--reachability`. They are applied by the controller. `--unassigned` reads the paged "not assigned to a site" list and keeps only those devices.
- **`--with-site`** adds each device's site, with one extra API call per device, so it is slower on large inventories (it is skipped for devices known to have no site). Devices whose site cannot be looked up are left blank.
- **Output file:** `--output` (default `inventory-YYYYmmdd-HHMMSS.csv` in the current folder). An existing file is never replaced unless you add `--overwrite`. Values that start with `=`, `+`, `-` (other than plain negative numbers) or `@` get a leading quote, so a spreadsheet cannot run inventory text as a formula. The inventory files are git-ignored. It only reads from Catalyst Center. `--raw` also prints one raw device record (secrets redacted), to check the field names on your controller.

## Assigning discovered devices to a site: `assign_site.py`

After a discovery, devices are in the inventory but **not assigned to a site**. `assign_site.py` assigns them. It uses the same `devices.csv` as the discovery, with one more column, `site`:

```csv
ip_address,credential_profile,site
10.48.90.173,default,Global/London/Building1/Floor1
10.48.90.174,default,Global/London/Building1
```

The discovery scripts ignore the `site` column, so one file serves both. Sites are given **by name** (the full hierarchy) and translated to site IDs for you.

```sh
python assign_site.py --refresh-sites           # read the sites and save a local copy
python assign_site.py devices.csv --dry-run     # read-only preview: nothing is changed
python assign_site.py devices.csv               # assign, then verify
python assign_site.py devices.csv --site "Global/London/Building1"   # for rows with no site
python assign_site.py devices.csv --wait-for-devices 300             # right after a discovery
```

> **Reminder: refresh the site list when sites change.** To avoid asking Catalyst Center for every site on each run, the script keeps a local copy of the site list in `sites_cache.json` (change the location with `--sites-cache`). It is created on the first run and is never refreshed by itself. If sites are **created or renamed in Catalyst Center, run the script with `--refresh-sites`**. A site name that is not in the copy stops the run and says so, with the closest names. The copy records which controller it came from (a copy for another controller is replaced) and its date; a note appears if it is older than 7 days. The reminder is also printed at the end of every run and at the top of the script.

**What it does:**

1. Reads the CSV and checks it before any API call (bad or repeated IPs, rows with no site; `--site` fills in rows that have none).
2. Loads the site list from the local copy (reading it from Catalyst Center, paged, only the first time or with `--refresh-sites`), and resolves each site name to a site ID. Spaces around `/` and capitals do not matter. A site must be a **building or a floor**; all name problems are shown together.
3. Finds the devices cheaply: it reads the paged "not assigned to a site" list **once** and matches the IPs itself, instead of asking about each IP. IPs not in that list are looked up in the inventory (a few per request), to tell **already assigned** from **not in the inventory**. `--wait-for-devices N` keeps looking for up to N seconds, for devices that have not appeared yet.
4. Assigns: one request per site, at most 100 devices per request (the limit of the API), and waits for each task. A failed request does not stop the others.
5. Verifies by reading the "not assigned" list again: a device only counts as `ASSIGNED` if it is really gone from it.

**Rules:**

- Only devices that are not assigned to any site are assigned. A device that **already has a site is skipped and reported (`ALREADY ASSIGNED`), never moved**. Provisioning and creating sites are not part of this script.
- Access points and sensors must go to a floor, other devices to a building. The controller enforces this, and its error message is shown against the device.
- `--dry-run` reads the sites and devices and prints what would happen (`TO ASSIGN`, `ALREADY ASSIGNED`, `NOT IN INVENTORY`), but sends nothing. Run it first. `--no-wait` submits only and prints the task IDs (check them with `check_task.py`). `--timeout`, `--raw` (prints one raw site and one raw device record, secrets redacted) work as in the other scripts.
- **Exit code:** 0 when every device is assigned, was already assigned, or (in a dry run) would be assigned; 1 otherwise (a device not in the inventory, a failed or unfinished task, a site problem, an API error).

Example output:

```text
Sites: using the list saved on 2026-10-22 20:04 UTC (814 sites: 12 area, 201 building, 601 floor).
- 10.48.90.173    sw-1                 Global/London/Building1/Floor1  ASSIGNED
- 10.48.90.174    sw-2                 Global/London/Building1  ALREADY ASSIGNED: already has a site; devices are never moved
2 device(s). ALREADY ASSIGNED: 1, ASSIGNED: 1
Reminder: site names are read from a local copy of the site list. ...
```

**To confirm on your controller:** this uses the newer assignment API (`POST /dna/intent/api/v1/networkDevices/assignToSite/apply`, which takes device IDs and a building or floor ID), the "not assigned to a site" list and the sites list. The scripts were tested against a simulated controller only. The field names assumed in the responses (sites: `id`, `nameHierarchy`, `type`; devices: `id`, `managementIpAddress`, `hostname`) could not be confirmed against a live 2.3.7.10 controller. A missing field never crashes the script (it shows up as a clear message or a blank), and `--raw` shows what your controller returns. `--refresh-sites` and `--dry-run` only read, so they are safe to try first.

## The trade-off

Because each file carries its own copy of the shared code (settings, IP and range handling, credential lookup, polling, redaction), a fix has to be applied in each file that has it. Use these scripts when you want something simple to read or hand over; use [3-shared-module](../3-shared-module) when you would rather maintain that code once. Tests compare the code in these files with `3-shared-module/discovery_common.py`, so the two sets cannot quietly drift apart.
