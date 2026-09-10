# BACnet Collector

A **read-only BACnet/IP monitoring tool** designed to run on a Raspberry Pi.

It lets you:

- See BACnet devices and points on a building network
- Choose which points you want to monitor
- Save temperature, status, and other BACnet values over time
- View everything from a simple web dashboard.
- Export collected information when needed

## What does this actually do?

Think of this program as a **BACnet observer**.

It can:

**LOOK → READ → RECORD → DISPLAY**

It cannot:

**CHANGE → COMMAND → OVERRIDE → CONTROL**

The collector does not write BACnet values, change setpoints, modify schedules, or configure controllers.

---

# Basic workflow

```text
BACnet Controllers
        │
        ▼
   Raspberry Pi
        │
        ├── Reads approved points
        │
        ├── Saves history
        │
        └── Runs web dashboard
                 │
                 ▼
          Phone / Laptop
```

Normally you will:

1. Install the collector on a Raspberry Pi.
2. Connect the Pi to the approved BACnet/IP network.
3. Open the dashboard.
4. Run a scan to see what BACnet devices are available.
5. Choose the devices and points you want to monitor.
6. Add those points to `config.yaml`.
7. Let the Pi collect them automatically.

---

# Dashboard

The dashboard runs on your local network.

By default:

```text
http://PI_ADDRESS:8080
```

For example:

```text
http://192.168.1.50:8080
```

The dashboard can show:

- Collector status
- BACnet devices
- Point values
- Last successful readings
- Communication errors
- Scan results
- Previous scans
- Point health

The dashboard also allows you to manually start and stop BACnet scans.

A scan **does not run automatically when the Pi starts**.

---

# First-time setup

You can start with no configured BACnet devices:

```yaml
devices: []
```

The collector and dashboard will still run.

This is useful when first connecting to a building because you can open the dashboard and manually run a discovery scan.

The scan will show what it finds without automatically adding anything to your permanent monitoring configuration.

After reviewing the results, add only the devices and points you actually want to monitor.

---

# Example point

A configured device might look like this:

```yaml
devices:
  - name: Example Air Handler
    instance: 1001
    address: 192.168.50.41

    points:
      - name: Supply Air Temperature
        object: analog-input,1
        property: present-value
        units: °F
```

This tells the collector:

```text
Device:
Example Air Handler

BACnet Device Instance:
1001

IP:
192.168.50.41

Watch:
Analog Input 1

Read:
Present Value

Display as:
Supply Air Temperature
```

You can add additional points under the same device.

---

# How often are points collected?

The default interval is:

```text
300 seconds
```

or:

```text
5 minutes
```

This works well for things such as:

- Room temperature
- Supply air temperature
- Return air temperature
- Humidity
- Equipment status
- Building trends

The minimum allowed polling interval is **60 seconds**.

You can change the interval in:

```yaml
poll_interval_seconds: 300
```

---

# Installation

The software requires:

```text
Python 3.11+
```

From the project directory:

```sh
python3 -m venv .venv
.venv/bin/pip install .
```

Copy the example configuration:

```sh
cp config.example.yaml config.yaml
```

Check the configuration before connecting:

```sh
.venv/bin/bacnet-console --config ./config.yaml --check-config
```

If everything is correct, you should see:

```text
configuration is valid
```

Then run the collector:

```sh
.venv/bin/bacnet-console --config ./config.yaml
```

---

# Basic configuration

A simple starting configuration looks like this:

```yaml
network:
  bind: auto

poll_interval_seconds: 300

dashboard:
  host: auto
  port: 8080

devices: []
```

Using:

```yaml
bind: auto
```

tells the program to use the Pi's active private network interface.

For a permanent installation, you may prefer to explicitly configure the BACnet-facing interface.

---

# Where is the data stored?

Readings and scan information are stored locally in a SQLite database.

The normal service installation uses:

```text
/var/lib/bacnet-console/console.sqlite3
```

This allows the collector to keep historical information through restarts.

For long-term installations, use a reliable SD card or SSD and occasionally back up important data.

---

# Raspberry Pi requirements

The collector is lightweight.

For a small installation:

```text
512 MB RAM
1 CPU core
1 GB free storage
```

Recommended:

```text
1 GB+ RAM
2+ CPU cores
High-endurance SD card or SSD
Reliable power supply
```

A normal Raspberry Pi should have no trouble running it.

---

# Important: this project is read-only

The BACnet portion of this project is intentionally limited to reading information.

It does **not** contain BACnet commands for:

- Changing setpoints
- Overriding outputs
- Changing schedules
- Writing properties
- Configuring controllers
- Changing BACnet priorities

If control functionality is ever added, it should be treated as a separate project with separate authorization and safety review.

---

# Network safety

Only connect the collector to a BAS network where you have permission to do so.

BACnet reads are normally lightweight, but discovery and large numbers of requests still create network and controller traffic.

The program includes limits intended to avoid aggressive scanning.

The dashboard should remain on a trusted local network or VLAN rather than being exposed directly to the internet.

---

# Technical notes

The sections below describe how the collector behaves internally. Most users do not need to understand these details to operate it.

## BACnet scanning

Scans only happen when an operator requests one from the dashboard.

The default limits are:

```yaml
scan:
  max_duration_seconds: 1800
  max_devices: 50
  max_objects_per_device: 200
```

The default maximum scan duration is therefore:

```text
30 minutes
```

A scan can also be manually stopped from the dashboard.

Partial results are preserved when possible.

Devices discovered during a scan are **not automatically approved for monitoring**.

They must still be added to the YAML configuration.

---

## BACnet request pacing

The collector deliberately spaces BACnet requests.

Default:

```yaml
network:
  inter_request_delay_seconds: 0.25
```

That is a minimum gap of:

```text
250 ms
```

between requests.

Only one read request is active at a time.

This reduces the chance of overwhelming slower BACnet controllers.

---

## ReadPropertyMultiple

The collector supports BACnet `ReadPropertyMultiple` to reduce unnecessary network traffic.

It starts with small requests and gradually increases the number of properties requested together when the controller responds reliably.

Default maximum:

```yaml
read_multiple_batch_size: 20
```

If a controller has trouble with larger requests, the collector automatically reduces the batch size.

If batching fails, it falls back to individual property reads.

Setting:

```yaml
read_multiple_batch_size: 1
```

effectively disables batching.

---

## Timeouts and slow controllers

The default BACnet request timeout is:

```yaml
request_timeout_seconds: 5
```

Controllers that respond slowly or return errors cause the collector to reduce request batching and slow down requests.

Repeated failures create progressively longer cooldown periods.

These protections help the collector behave politely on slower BAS networks.

---

## Storage

SQLite runs using WAL mode and:

```text
synchronous=FULL
```

to improve resilience against unexpected shutdowns.

The configuration contains:

```yaml
retention_days: 90
```

but automatic database pruning is currently **not scheduled**.

Do not assume the database will automatically remain below a particular size.

Saved discovery scans are also not automatically deleted.

---

## Dashboard API

The local dashboard uses several HTTP endpoints internally.

Examples include:

```text
GET  /api/status
GET  /api/scan/status
GET  /healthz

POST /api/rescan
POST /api/points.csv
POST /api/scan/rename
```

These are primarily used by the dashboard itself.

The dashboard periodically requests scan status so a scan started from another browser tab can still appear live.

---

## Dashboard security

The dashboard uses a temporary token to help prevent unwanted form submissions.

This is **not a replacement for user authentication**.

The intended protection is running the Pi and BACnet interface on an access-controlled local network or VLAN.

The application also rejects public, loopback, wildcard, and mismatched network bindings where appropriate.

---

## systemd installation

The included systemd configuration expects:

```text
Application:
/opt/bacnet-console

Configuration:
/etc/bacnet-console/config.yaml

Database/data:
/var/lib/bacnet-console
```

The service runs as an unprivileged:

```text
bacnetconsole
```

user and can automatically restart after a failure or reboot.

Network configuration, firewall configuration, and deployment are intentionally left as site-specific installation tasks.

---

# Testing

Development tests do not require a real BAS.

They use simulated BACnet clients, temporary databases, and local HTTP connections.

Run the test suite with:

```sh
python3 -m venv .venv
.venv/bin/pip install -e '.[test]'
.venv/bin/pytest
```

---

# Summary

For normal use, the important part is:

```text
Install it
    ↓
Open dashboard
    ↓
Scan BACnet network
    ↓
Find the equipment you care about
    ↓
Add approved points to config.yaml
    ↓
Restart collector
    ↓
Pi trends the points automatically
```

Everything below that workflow is mostly implementation detail and safety protection.
