# 3-shared-module: the same jobs, built on one shared helper

> **Demonstration code only.** Provided for demonstration and example purposes, not supported and not tested for production use. The customer is solely responsible for reviewing, testing and validating it, and for any use or execution in their own environments, including production. Use is subject to the [Cisco Sample Code License, Version 1.1](../LICENSE).

These scripts do the same jobs as their twins in [2-standalone](../2-standalone), but the logic they have in common lives once, in [discovery_common.py](discovery_common.py): settings and `.env` loading, credential profiles and lookup, IP validation and range formatting, the discovery request, and the parsing of results. The scripts themselves stay short and call the Catalyst Center SDK (`catalystcentersdk`).

| File | What it is |
| --- | --- |
| [discovery_common.py](discovery_common.py) | The shared helper. It does not import the SDK (the scripts pass the SDK calls in), so it can be tested without a controller |
| [discover_ips_in_code.py](discover_ips_in_code.py) | Discovery for the IPs in its `DEVICES` list (twin of `discover_ips.py`) |
| [discover_from_csv.py](discover_from_csv.py) | Discovery for the IPs in a CSV file (twin of `discover_csv.py`) |
| [check_discovery.py](check_discovery.py) | Checks a task or a discovery by ID (twin of `check_task.py --discovery`) |
| [assign_devices_to_site.py](assign_devices_to_site.py) | Assigns devices to a site (twin of `assign_site.py`) |

## Setup and use

```sh
cp .env.example .env              # then edit it; it is git-ignored
cp devices.example.csv devices.csv
```

Run the scripts from this folder. The `.env` file must exist and is read from this folder (next to `discovery_common.py`); variables already set in the environment win over it. The settings, credential profiles and every option work exactly as described in [2-standalone/README.md](../2-standalone/README.md), with the same exit codes:

```sh
python discover_ips_in_code.py --dry-run
python discover_from_csv.py devices.csv --name-prefix office --dry-run
python discover_from_csv.py devices.csv --name-prefix office --netconf
python check_discovery.py <task-id>
python check_discovery.py --discovery-id <discovery-id>
python assign_devices_to_site.py --refresh-sites
python assign_devices_to_site.py devices.csv --dry-run
```

The credential manager, the inventory export and `check_task.py` for non-discovery tasks exist only in `2-standalone`.

## Why two sets?

- **Maintain it once:** a fix to settings, credential matching, range handling or result parsing is made in `discovery_common.py` only, instead of in each standalone file that carries its own copy.
- **Testable:** the helper takes the API calls as small functions, so the tests run it against fake responses.
- **The cost:** a script cannot be copied on its own, and you read two files to follow one run. The standalone files trade the other way. The tests compare the standalone code with this module, so the two sets stay in step.
