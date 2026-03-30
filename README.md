# Pi-Mirror-Local-Server

Daemon and tooling to run on the Raspberry Pi that powers the Smart Mirror,
enabling secure remote management from another host (e.g., the
[Local-Server-Site-Pusher](https://github.com/spospordo/Local-Server-Site-Pusher)).

---

## Table of Contents

1. [Prerequisites](#prerequisites)
2. [Repository Layout](#repository-layout)
3. [End-to-End Setup Walkthrough](#end-to-end-setup-walkthrough)
   - [Step 1 – Prepare the Pi OS](#step-1--prepare-the-pi-os)
   - [Step 2 – Clone the repository](#step-2--clone-the-repository)
   - [Step 3 – Run the install script](#step-3--run-the-install-script)
   - [Step 4 – Edit the configuration](#step-4--edit-the-configuration)
   - [Step 5 – Enable the systemd service](#step-5--enable-the-systemd-service)
   - [Step 6 – Set up SSH keys for remote control](#step-6--set-up-ssh-keys-for-remote-control)
   - [Step 7 – Verify everything works](#step-7--verify-everything-works)
4. [Configuration Reference](#configuration-reference)
5. [Remote Command API](#remote-command-api)
6. [Architecture](#architecture)
7. [Persistence and Reboots](#persistence-and-reboots)
8. [Updating the Daemon](#updating-the-daemon)
9. [Running Tests](#running-tests)
10. [Logs](#logs)
11. [Troubleshooting / FAQ](#troubleshooting--faq)

---

## Prerequisites

| Requirement | Details |
|---|---|
| **Hardware** | Raspberry Pi 3B, 3B+, 4B, 5, or Zero 2 W (any model with GPIO header) |
| **OS** | Raspberry Pi OS Bookworm or Bullseye (32-bit or 64-bit); headless or with HDMI display |
| **Python** | Python 3.9 or newer (pre-installed on all Raspberry Pi OS releases) |
| **pip3** | Usually pre-installed; if missing run `sudo apt install python3-pip` |
| **git** | `sudo apt install git` if not already present |
| **SSH server** | Enabled via `sudo raspi-config` → Interface Options → SSH, or ship headless with `ssh` file in `/boot` |
| **Internet access** | Required only during install to download `RPi.GPIO` if not already present |

---

## Repository Layout

```
.
├── pir_display.py                  # Core PIR motion-detection / HDMI control module
├── mirror_daemon.py                # Main daemon (systemd service entry point)
├── mirror_cmd.py                   # SSH ForceCommand shim (called by sshd)
├── config/
│   └── mirror.conf                 # Default configuration (INI format)
├── systemd/
│   └── pi-mirror-daemon.service    # systemd unit file
├── scripts/
│   └── install.sh                  # Install / update script (run as root)
└── tests/
    └── test_mirror_daemon.py       # Unit test suite (57 tests, no hardware needed)
```

---

## End-to-End Setup Walkthrough

The steps below take you from a freshly imaged Raspberry Pi OS to a fully
running, auto-starting daemon that survives reboots.

### Step 1 – Prepare the Pi OS

```bash
# Update package lists and upgrade existing packages
sudo apt update && sudo apt upgrade -y

# Install git and pip3 if not already present
sudo apt install -y git python3-pip

# (Optional) Enable SSH if you haven't already
sudo systemctl enable ssh
sudo systemctl start ssh
```

> **Headless tip:** Place an empty file named `ssh` in the `/boot` partition
> before first boot to enable SSH automatically.

---

### Step 2 – Clone the repository

```bash
# Run from the home directory of the pi user (or any working directory)
git clone https://github.com/spospordo/Pi-Mirror-Local-Server.git
cd Pi-Mirror-Local-Server
```

---

### Step 3 – Run the install script

```bash
sudo bash scripts/install.sh
```

The install script is **idempotent** – it is safe to re-run for updates.
It performs the following steps automatically:

| Step | What happens |
|---|---|
| Creates service user | Adds `pi-mirror` system account with `gpio` and `video` group membership |
| Installs Python deps | Runs `pip3 install RPi.GPIO`; warns (non-fatal) if it fails |
| Creates directories | `/opt/pi-mirror/` (Python files) and `/etc/pi-mirror/` (config) |
| Copies Python files | `mirror_daemon.py`, `mirror_cmd.py`, `pir_display.py` → `/opt/pi-mirror/` |
| Writes default config | `/etc/pi-mirror/mirror.conf` – **only on first install**; never overwritten |
| Prepares log file | Creates `/var/log/pi-mirror-daemon.log` owned by `pi-mirror` |
| Installs systemd unit | Copies to `/etc/systemd/system/` and runs `systemctl enable` |
| Adds sudoers rule | Allows the `pi-mirror` user to run `sudo reboot` / `sudo shutdown` without a password |
| Prints SSH key guide | Shows the exact `authorized_keys` line format for remote control |

**Expected output (abbreviated):**

```
[INFO]  Created system user 'pi-mirror'.
[INFO]  Installing Python dependencies …
[INFO]  Directories ready: /opt/pi-mirror  /etc/pi-mirror
[INFO]  Python files installed to /opt/pi-mirror
[INFO]  Installed default config to /etc/pi-mirror/mirror.conf
[INFO]  Log file prepared: /var/log/pi-mirror-daemon.log
[INFO]  Systemd unit installed and enabled.
[INFO]  Sudoers rule written to /etc/sudoers.d/pi-mirror

---- SSH Authorized-Keys Setup (manual step) --------------------------------
…
[INFO]  Service started.
[INFO]  Pi Mirror Daemon installation complete.
```

---

### Step 4 – Edit the configuration

The config file lives at `/etc/pi-mirror/mirror.conf` and is **never
overwritten** by `install.sh` on subsequent runs, so your customisations
persist across updates.

```bash
sudo nano /etc/pi-mirror/mirror.conf
```

See the [Configuration Reference](#configuration-reference) section for a
full explanation of every setting.

After editing, apply the changes:

```bash
sudo systemctl restart pi-mirror-daemon
```

---

### Step 5 – Enable the systemd service

The install script already calls `systemctl enable`, but here are the key
service management commands for reference:

```bash
# Check current status
sudo systemctl status pi-mirror-daemon

# Start (if not running)
sudo systemctl start pi-mirror-daemon

# Stop
sudo systemctl stop pi-mirror-daemon

# Restart (e.g. after a config change)
sudo systemctl restart pi-mirror-daemon

# Enable auto-start on every boot (already done by install.sh)
sudo systemctl enable pi-mirror-daemon

# Disable auto-start
sudo systemctl disable pi-mirror-daemon
```

**Verify the service is active:**

```
● pi-mirror-daemon.service - Pi Mirror Remote Management Daemon
     Loaded: loaded (/etc/systemd/system/pi-mirror-daemon.service; enabled; …)
     Active: active (running) since …
```

The `enabled` keyword confirms it will start automatically on every reboot.

---

### Step 6 – Set up SSH keys for remote control

The remote controller host authenticates with an SSH public key and is
restricted to the `mirror_cmd.py` shim via `ForceCommand`.

**On the remote controller host**, generate a dedicated key pair (skip if you
already have one):

```bash
ssh-keygen -t ed25519 -C "pi-mirror-controller" -f ~/.ssh/pi_mirror_key
# Press Enter twice to use no passphrase (for unattended automation)
```

**On the Raspberry Pi**, create the SSH directory for the `pi-mirror` user
and add the public key with the required `ForceCommand` prefix:

```bash
# Create .ssh directory for the pi-mirror service user
sudo mkdir -p /home/pi-mirror/.ssh
sudo chmod 700 /home/pi-mirror/.ssh
sudo chown pi-mirror:pi-mirror /home/pi-mirror/.ssh

# Open (or create) authorized_keys
sudo nano /home/pi-mirror/.ssh/authorized_keys
```

Add **one line** per remote controller, in this exact format:

```
command="python3 /opt/pi-mirror/mirror_cmd.py --user mirror-admin",no-pty,no-agent-forwarding,no-X11-forwarding ssh-ed25519 AAAA…your-public-key… controller@remote-host
```

- Replace `mirror-admin` with the logical username of the remote caller.
  This name is used for admin-privilege checks.
- Replace `ssh-ed25519 AAAA…` with the **full** contents of
  `~/.ssh/pi_mirror_key.pub` from the remote controller.
- The `command=…` prefix means the remote host can **only** run
  `mirror_cmd.py` regardless of what SSH command it sends.

Set correct permissions:

```bash
sudo chmod 600 /home/pi-mirror/.ssh/authorized_keys
sudo chown pi-mirror:pi-mirror /home/pi-mirror/.ssh/authorized_keys
```

**Enable admin access** for the remote caller (needed for `reboot`,
`shutdown`, `update_config`): make sure the username you used in
`--user mirror-admin` appears in `/etc/pi-mirror/mirror.conf`:

```ini
admin_users = mirror-admin
```

Then restart the service to pick up the change:

```bash
sudo systemctl restart pi-mirror-daemon
```

---

### Step 7 – Verify everything works

**From the remote controller host:**

```bash
# Test connectivity and get daemon status
ssh -i ~/.ssh/pi_mirror_key pi-mirror@<pi-ip-address> get_status
```

Expected response:

```json
{
  "status": "ok",
  "data": {
    "display_on": true,
    "override": null,
    "uptime_seconds": 42,
    "config": { "pir_pin": 22, "timeout": 120, … }
  },
  "message": "status ok"
}
```

**On the Pi itself:**

```bash
# Live service log
sudo journalctl -u pi-mirror-daemon -f

# Service status
sudo systemctl status pi-mirror-daemon
```

---

## Configuration Reference

File: `/etc/pi-mirror/mirror.conf`

```ini
[mirror]

# BCM GPIO pin number for the PIR sensor signal wire.
# Use the BCM (Broadcom) numbering scheme, not the physical pin number.
# Default: 22  (physical pin 15 on a standard 40-pin header)
pir_pin = 22

# Seconds of no motion detected before the display is automatically turned off.
# Set to 0 to disable the PIR timeout entirely (display stays on permanently).
# Default: 120
timeout = 120

# Full command + arguments for the kiosk browser, space-separated.
# Uncomment and adjust to match the browser installed on your Pi.
# Leave commented out if you do not use browser management.
# browser_cmd = chromium-browser --noerrdialogs --disable-infobars --kiosk http://localhost

# Absolute path for the rotating daemon log file.
# The pi-mirror user must be able to write to this path.
# Default: /var/log/pi-mirror-daemon.log
log_file = /var/log/pi-mirror-daemon.log

# Absolute path for the Unix-domain management socket.
# Must be on a tmpfs (e.g. /run) so it is re-created on every boot.
# Default: /run/pi-mirror-daemon.sock
socket_path = /run/pi-mirror-daemon.sock

# Comma-separated list of SSH usernames allowed to issue admin commands
# (reboot, shutdown, update_config).
# Must match the --user value used in authorized_keys ForceCommand lines.
# Leave empty to disallow all admin operations.
# Default: mirror-admin
admin_users = mirror-admin
```

After editing, always restart the service:

```bash
sudo systemctl restart pi-mirror-daemon
```

You can also update `timeout`, `pir_pin`, and `browser_cmd` **at runtime**
(no restart needed) via the `update_config` remote command – see
[Remote Command API](#remote-command-api).

---

## Remote Command API

The remote host issues commands via plain SSH:

```bash
ssh -i ~/.ssh/pi_mirror_key pi-mirror@<pi-ip> display_on
ssh -i ~/.ssh/pi_mirror_key pi-mirror@<pi-ip> display_off
ssh -i ~/.ssh/pi_mirror_key pi-mirror@<pi-ip> display_auto
ssh -i ~/.ssh/pi_mirror_key pi-mirror@<pi-ip> restart_browser
ssh -i ~/.ssh/pi_mirror_key pi-mirror@<pi-ip> get_status
ssh -i ~/.ssh/pi_mirror_key pi-mirror@<pi-ip> get_logs --lines 100
ssh -i ~/.ssh/pi_mirror_key pi-mirror@<pi-ip> update_config timeout=300
ssh -i ~/.ssh/pi_mirror_key pi-mirror@<pi-ip> update_config pir_pin=17 timeout=60
ssh -i ~/.ssh/pi_mirror_key pi-mirror@<pi-ip> reboot --delay 10
ssh -i ~/.ssh/pi_mirror_key pi-mirror@<pi-ip> shutdown --delay 5
```

Every command returns a JSON object:

```json
{
  "status": "ok",
  "data":   { … },
  "message": "human-readable description"
}
```

### Command Reference

| Command | Admin only | Arguments | Description |
|---|---|---|---|
| `display_on` | No | — | Force display ON (overrides PIR) |
| `display_off` | No | — | Force display OFF (overrides PIR) |
| `display_auto` | No | — | Return display control to the PIR sensor |
| `restart_browser` | No | — | Kill and relaunch the kiosk browser |
| `get_status` | No | — | Return current state (display, overrides, uptime, config) |
| `get_logs` | No | `--lines N` (default 50) | Tail the daemon log file |
| `update_config` | **Yes** | `key=value …` | Update config keys at runtime and persist to disk |
| `reboot` | **Yes** | `--delay N` (default 5 s) | Reboot the Pi after N seconds |
| `shutdown` | **Yes** | `--delay N` (default 5 s) | Shut the Pi down after N seconds |

**Mutable config keys** (for `update_config`): `timeout`, `pir_pin`, `browser_cmd`

---

## Architecture

```
┌──────────────────────────────────────────────────────┐
│  Raspberry Pi                                        │
│                                                      │
│  ┌───────────────────────────────────────────────┐   │
│  │  mirror_daemon.py  (systemd service)          │   │
│  │  ┌─────────────────────────────────────────┐  │   │
│  │  │  PIRController thread                   │  │   │
│  │  │  - polls GPIO pin every 500 ms          │  │   │
│  │  │  - turns display off after timeout      │  │   │
│  │  │  - respects remote_override flag        │  │   │
│  │  └─────────────────────────────────────────┘  │   │
│  │  ┌─────────────────────────────────────────┐  │   │
│  │  │  Unix socket server (/run/pi-mirror-…)  │  │   │
│  │  │  - handles one JSON command per conn    │  │   │
│  │  │  - returns JSON response                │  │   │
│  │  └─────────────────────────────────────────┘  │   │
│  └────────────────────▲──────────────────────────┘   │
│                       │ Unix socket                  │
│  ┌────────────────────┴──────────────────────────┐   │
│  │  mirror_cmd.py  (SSH ForceCommand shim)       │   │
│  │  - reads SSH_ORIGINAL_COMMAND                 │   │
│  │  - validates + forwards to daemon socket      │   │
│  │  - prints JSON response to SSH caller         │   │
│  └───────────────────────────────────────────────┘   │
│                       ▲                              │
│                 SSH connection                       │
└───────────────────────┼──────────────────────────────┘
                        │
              Remote controller host
              (Local-Server-Site-Pusher)
```

**Security model:**
- Only public-key SSH authentication reaches the Pi.
- `ForceCommand` in `authorized_keys` means the remote caller can only invoke
  `mirror_cmd.py` regardless of what command they type.
- `mirror_cmd.py` validates the command against an allowlist before forwarding.
- Admin commands (`reboot`, `shutdown`, `update_config`) are rejected unless the
  SSH username is listed in `admin_users` in `mirror.conf`.
- The daemon Unix socket is `chmod 660` and owned by the `pi-mirror` group,
  so only the service and the SSH shim (run as `pi-mirror`) can reach it.

---

## Persistence and Reboots

The following table summarises what persists across reboots and updates:

| Item | Location | Persists across reboot | Persists across `install.sh` update |
|---|---|---|---|
| Configuration | `/etc/pi-mirror/mirror.conf` | ✅ Yes | ✅ Yes (never overwritten) |
| Python source files | `/opt/pi-mirror/` | ✅ Yes | 🔄 Updated to latest |
| Log file | `/var/log/pi-mirror-daemon.log` | ✅ Yes | ✅ Yes |
| Daemon socket | `/run/pi-mirror-daemon.sock` | ❌ Recreated on start | ❌ Recreated on start |
| SSH authorized keys | `/home/pi-mirror/.ssh/authorized_keys` | ✅ Yes | ✅ Yes (not touched by script) |
| systemd unit file | `/etc/systemd/system/pi-mirror-daemon.service` | ✅ Yes | 🔄 Updated to latest |
| Service enabled state | systemd | ✅ Yes (survives reboot) | ✅ Yes |

**To confirm the service starts automatically after a reboot:**

```bash
sudo reboot
# After the Pi comes back up:
sudo systemctl status pi-mirror-daemon
# Should show: Active: active (running)
```

---

## Updating the Daemon

```bash
# On the Raspberry Pi, from inside the cloned repo:
git pull
sudo bash scripts/install.sh
```

The script:
1. Copies the latest Python files to `/opt/pi-mirror/`
2. Updates the systemd unit file
3. Restarts the service automatically
4. **Does not** overwrite `/etc/pi-mirror/mirror.conf` – your config is safe

---

## Running Tests

No Raspberry Pi hardware is required. Tests mock GPIO and subprocess calls.

```bash
# Install pytest if needed
pip3 install pytest

# Run the full test suite
python3 -m pytest tests/test_mirror_daemon.py -v
```

All tests should pass with no failures.

---

## Logs

```bash
# Live log tail via journald
sudo journalctl -u pi-mirror-daemon -f

# Show last 100 lines from journald
sudo journalctl -u pi-mirror-daemon -n 100 --no-pager

# Or tail the log file directly
sudo tail -f /var/log/pi-mirror-daemon.log

# Retrieve logs remotely (returns last 50 lines by default)
ssh -i ~/.ssh/pi_mirror_key pi-mirror@<pi-ip> get_logs
ssh -i ~/.ssh/pi_mirror_key pi-mirror@<pi-ip> get_logs --lines 200
```

---

## Troubleshooting / FAQ

### Service fails to start

**Check the logs first:**

```bash
sudo journalctl -u pi-mirror-daemon -n 50 --no-pager
```

| Symptom | Likely cause | Fix |
|---|---|---|
| `gpio` group does not exist | Minimal OS image without GPIO packages | `sudo apt install raspi-gpio` then re-run `install.sh` |
| `ModuleNotFoundError: RPi.GPIO` | Pip install failed at setup time | `sudo pip3 install RPi.GPIO` |
| `Permission denied` on socket | Wrong ownership on `/run` socket | Re-run `sudo bash scripts/install.sh` |
| Service crashes in a loop | Config file syntax error | `sudo nano /etc/pi-mirror/mirror.conf` and fix; restart |
| `Unit not found` | `install.sh` not run yet, or run without sudo | `sudo bash scripts/install.sh` |

### SSH connection is refused or times out

- Confirm SSH is enabled: `sudo systemctl status ssh`
- Confirm the Pi's IP address: `hostname -I`
- Confirm the `pi-mirror` user exists: `id pi-mirror`
- Test with verbose mode: `ssh -vvv -i ~/.ssh/pi_mirror_key pi-mirror@<pi-ip> get_status`

### SSH connects but returns `Permission denied (publickey)`

- Check the public key was copied correctly into
  `/home/pi-mirror/.ssh/authorized_keys`
- Confirm permissions: `sudo ls -la /home/pi-mirror/.ssh/`
  - `.ssh/` must be `700`, `authorized_keys` must be `600`, both owned by `pi-mirror`
- Check the `ForceCommand` line in `authorized_keys` uses the exact path
  `/opt/pi-mirror/mirror_cmd.py`

### Admin commands are rejected (`"error": "permission denied"`)

- Confirm the `--user <name>` value in the `authorized_keys` `command=` line
  matches an entry in `admin_users` in `/etc/pi-mirror/mirror.conf`
- Restart the service after editing the config:
  `sudo systemctl restart pi-mirror-daemon`

### Display does not turn off (PIR has no effect)

- Verify the PIR sensor is wired to the BCM pin number specified in `pir_pin`
- Check the daemon log for GPIO errors: `sudo tail -50 /var/log/pi-mirror-daemon.log`
- If `RPi.GPIO` is unavailable the daemon runs in software-only mode (display
  stays on) – install it with `sudo pip3 install RPi.GPIO` and restart the service

### `update_config` changes don't survive a service restart

`update_config` writes changes to `/etc/pi-mirror/mirror.conf` on disk,
so they **do** survive a restart. If values are reverting, check that the
service user has write permission to `/etc/pi-mirror/mirror.conf`:

```bash
ls -l /etc/pi-mirror/mirror.conf
# Should show: -rw-r----- root pi-mirror
```

### How do I reset to the default config?

```bash
sudo rm /etc/pi-mirror/mirror.conf
sudo bash scripts/install.sh   # Re-installs the default config
```

### Recovery after unexpected reboot or power loss

The systemd unit is configured with `Restart=on-failure` and
`WantedBy=multi-user.target`, so the daemon:
- **Restarts automatically** if it crashes (up to 5 times in 10 minutes)
- **Starts automatically** on every boot

If the daemon is stuck in a restart loop:

```bash
sudo systemctl status pi-mirror-daemon
sudo journalctl -u pi-mirror-daemon -n 30 --no-pager
# Fix the root cause, then:
sudo systemctl start pi-mirror-daemon
```
