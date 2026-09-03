# BACnet read-only collector and LAN console

This project is a Raspberry Pi OS service for an approved, single-building
BACnet/IP pilot. It trends an explicit device/point allowlist, stores readings
and errors in SQLite, and presents a local LAN dashboard with collector health,
approved point health, discovery results, and scan history.

It has no BACnet write calls, control logic, command priorities, schedules, or
controller-configuration features. Ordinary BACnet reads need no credentials.

> Connect this only with written BAS-owner approval. Discovery and reads still
> create network/controller load. This source and its tests were developed
> offline; no Raspberry Pi, BAS, or networked device was contacted.

## Safety model

- The normal collector receives a capability-limited client with `read` and
  `read_multiple` only.
  It cannot initiate discovery.
- Discovery exists in a separate scan component and runs only after an operator
  submits the dashboard's **Run operator scan now** form.
- Service startup never schedules or runs a scan. There is no scan timer.
- A scan sends Who-Is plus read-only object/property requests. It never approves,
  modifies, or configures a BAS device.
- Scan results are observational. Devices and points become approved only by an
  administrator editing the local YAML allowlist and restarting the service.
- The scan form uses an unguessable per-process token to block cross-site form
  submissions. This is not user authentication; put the BAS interface and Pi on
  an access-controlled LAN/VLAN and use host firewall policy as required.
- The web server binds to the RFC1918 address selected by `network.bind`; `auto`
  resolves the active default-route IPv4 interface each time the service starts.
  `0.0.0.0`, public addresses, loopback, and a mismatched dashboard host are
  rejected before startup.

Any future BACnet control feature must be a separate project, process, service
account, configuration, and authorization review. Do not extend these facades
with mutation methods.

## Operation

### Initial discovery-only commissioning

`devices: []` is valid. With an empty allowlist the collector heartbeat and web
console run, but no trend reads occur. No scan runs automatically. An authorized
operator can press **Run operator scan now** to perform one bounded scan. While
a scan is active, that same in-page button changes to **Stop scan**. It cancels
the operator task without leaving the dashboard and preserves partial results
as an interrupted scan. Operator stops are labeled **stopped** in the live panel,
with the reason saved as `scan stopped by operator`. For compatibility with
existing databases, the stored status remains `failed`; the error distinguishes
operator stops, shutdowns, and actual failures. Counts come from committed rows,
including points collected partway through a device.

The latest result shows discovered devices, their object-list points, read
health, values when readable, and whether each item matches the approved YAML.
Saved scans remain available until you explicitly delete them. There is no
automatic scan-count retention limit. Export/back up important scans; deletions
cannot be undone. Previously purged scans cannot be recovered by this update.

The scanner defaults to at most 50 devices and 200 objects per device, with a
250 ms minimum gap between read requests and a 30-minute total scan deadline.
These limits are configurable under `scan` and `network`, but should be
increased only with BAS-owner approval. If the deadline expires, the scan is
marked failed with the partial counts preserved. A scan left running by a
service restart is also marked failed automatically, so the dashboard cannot
wait forever on an abandoned run. Devices that return no usable object list
are recorded with an explicit error instead of an indefinite point wait.

### Approved collection

Add approved devices and points under `devices`. A device can have no points,
which displays device health without trending. An address can be configured
manually; otherwise a successful operator scan can resolve it locally. The
collector never performs discovery to resolve an address on its own.

The default trend interval is **300 seconds (five minutes)**. This is a sensible,
deliberately conservative starting point for general building temperatures and
status telemetry. Faster operational needs should be reviewed with the BAS owner;
configuration rejects intervals below 60 seconds. Requests run sequentially with
a configurable 250 ms minimum gap, including individual-read fallback.

### Adaptive read batching

Operator scans and approved collection share one read scheduler. It starts with
two property references per ReadPropertyMultiple request and doubles the batch
after two complete responses, up to `network.read_multiple_batch_size` (default
20). This counts properties, not objects: a scanned object's name, value, and
units consume three references. Set the limit to 1 to use individual reads only.

There is only one read request in flight. Slow responses reduce the batch size
and increase the gap. A failed batch falls back to paced individual reads and
puts batching for that address on a 30-second cooldown, increasing to at most
five minutes after repeated failures. Missing response entries are individually
retried; explicit property errors are saved as errors, not successful values.
Completed scan points are saved as they arrive, including during fallback.

Request timing and error counters are kept in memory, without additional network
requests; they are not yet displayed in the dashboard. These safeguards do not
establish a controller's safe throughput. Actual ECY performance still requires
an approved on-site check; no fixed objects-per-second rate is guaranteed.

## Configuration

Copy `config.example.yaml`. Use `auto` for the default-route interface, or specify
the address/prefix of the approved BAS interface explicitly. `dashboard.host`
must resolve to the same host address. On a dual-connected Wi-Fi/Ethernet Pi,
the default route is not necessarily the BAS-facing interface:

```yaml
network:
  bind: auto
dashboard:
  host: auto
  port: 8080
devices: []
```

Validate without opening a BACnet socket:

```sh
bacnet-console --config ./config.yaml --check-config
```

Point identifiers use `type,instance`, such as `analog-input,1`; properties use
hyphenated BACnet names such as `present-value`. Do not place passwords or BAS
credentials in this file.

## Raspberry Pi resources

The software is model-neutral across Raspberry Pi OS Bookworm devices with
Python 3.11 and a supported wired Ethernet interface (built in or approved USB):

- Minimum small pilot: 512 MB RAM, one CPU core, and 1 GB free persistent space.
- Recommended: 1 GB+ RAM, two cores, high-endurance SD/SSD, reliable power/UPS.
- Budget about 200 MB for the environment plus SQLite growth. At 100 points and
  five-minute polling, there are 28,800 readings/day. Measure actual growth and
  plan storage accordingly. The `retention_days` setting and pruning helper
  currently exist, but automatic pruning is not scheduled; do not rely on them
  to cap disk usage. Saved scans are never automatically pruned.

## Installation staging

The included systemd unit runs as an unprivileged `bacnetconsole` account,
restarts after failure/reboot, and allows writes only to `/var/lib/bacnet-console`.
Stage and validate the source before any approved deployment window:

```sh
python3 -m venv .venv
.venv/bin/pip install .
.venv/bin/bacnet-console --config ./config.yaml --check-config
```

The service file expects the application at `/opt/bacnet-console`, configuration
at `/etc/bacnet-console/config.yaml`, and data at `/var/lib/bacnet-console`.
Installation, interface configuration, firewall changes, and service startup are
site deployment actions and are intentionally not automated by this repository.

## Dashboard and health

With `auto`, DHCP address changes are picked up whenever the service starts. The
configured URL is `http://LAN_ADDRESS:8080/`. Changing networks while the process
is running still requires a restart; `auto` is not a live interface watcher. It shows:

- collector start, heartbeat, completed cycles, and fatal error state;
- approved device addresses, last-seen timestamps, point values and failures;
- latest discovered device/object results and read errors;
- scan requester address, timestamps, status, counts, and failures.

The page markup, CSS, and scan-progress JavaScript are separate files under
`templates/` and `static/`. The dashboard reads them on each request with
browser caching disabled, so presentation edits take effect after a refresh
without restarting the Python service. Timestamps are shown in compact UTC
form with the original ISO value retained on hover.
Installed wheels also include fallback assets under the environment's
`share/bacnet-console` directory, so launching from a different working directory
does not lose the dashboard. A source directory containing `templates/dashboard.html`
takes precedence for live editing.

The live panel uses `GET /api/scan/status`, returning only the latest scan rather
than every saved scan's points. It polls once per second during scans and every
five seconds while idle, so scans started in another tab become visible. Requests
have a ten-second browser timeout, buttons reject duplicate pending submissions,
and a failed status request leaves previously shown data on screen. Completed or
stopped scans refresh the saved-results sections without page navigation. Long
tables scroll within their containers; on narrow screens live status appears first.

`GET /api/status` returns the same data as JSON. The dashboard displays every
saved discovered scan point in a scrollable device/point view. `POST
/api/points.csv` downloads the saved scan-point list, and `GET /healthz` proves
the HTTP process is responding. Use the collector heartbeat and point timestamps
for end-to-end health. `POST /api/rescan` starts a read-only scan and `POST /api/scan/rename`
changes only a local scan-history label. The dashboard also offers local
deletion of saved scans, removal of saved scan points, and disabling of approved
points. These actions require the token served in the dashboard form and are
recorded in the audit log. Other mutation methods are rejected; none of these
local console actions can issue a BACnet write.
Removal and deletion buttons request confirmation and handle errors in-page.
Active scans and their points cannot be deleted. Disabling an approved point
stops subsequent trend reads and persists across restarts (an already-started
read may finish). The YAML alone does not re-enable a disabled point; a dedicated
restore control is not implemented yet. Saved-scan point removal does not disable
an independently configured trend point. CSV text starting with spreadsheet
formula markers is prefixed with an apostrophe for safe spreadsheet opening.

## Persistence and offline verification

SQLite uses WAL mode and `synchronous=FULL`; systemd handles automatic restart.
The database retains last-known point/device health through power loss. Stable
power and high-endurance storage are still recommended.

Tests use fake BACnet clients, the installed BACpypes request parser with a mocked
transport, temporary databases, and loopback HTTP only. They never open a BACnet
socket:

```sh
python3 -m venv .venv
.venv/bin/pip install -e '.[test]'
.venv/bin/pytest
```
