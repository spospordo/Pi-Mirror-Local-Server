# Pi-Mirror-Local-Server

Daemon and tooling to run on the Raspberry Pi that powers the Smart Mirror,
enabling secure remote management from another host (e.g., the
[Local-Server-Site-Pusher](https://github.com/spospordo/Local-Server-Site-Pusher)).

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

## Quick Start

### 1. Clone on the Raspberry Pi

```bash
git clone https://github.com/spospordo/Pi-Mirror-Local-Server.git
cd Pi-Mirror-Local-Server
```

### 2. Install

```bash
sudo bash scripts/install.sh
```

The install script:
- Creates the `pi-mirror` system user with `gpio` / `video` group membership
- Copies Python files to `/opt/pi-mirror/`
- Writes the default config to `/etc/pi-mirror/mirror.conf` (not overwritten on updates)
- Installs and enables the systemd service `pi-mirror-daemon`
- Adds a sudoers rule so the daemon can run `reboot` / `shutdown`
- Prints instructions for setting up SSH authorized keys

### 3. Configure

Edit `/etc/pi-mirror/mirror.conf`:

```ini
[mirror]
pir_pin     = 22      # BCM GPIO pin for PIR sensor
timeout     = 120     # Seconds of inactivity before display turns off
log_file    = /var/log/pi-mirror-daemon.log
socket_path = /run/pi-mirror-daemon.sock
admin_users = mirror-admin   # Comma-separated list; may issue reboot/shutdown/update_config
# browser_cmd = chromium-browser --noerrdialogs --disable-infobars --kiosk http://localhost
```

Then restart: `sudo systemctl restart pi-mirror-daemon`

### 4. Set up SSH access for a remote controller

Add a line to the **pi-mirror** user's (or any allowed user's)
`~/.ssh/authorized_keys`:

```
command="python3 /opt/pi-mirror/mirror_cmd.py --user mirror-admin",no-pty,no-agent-forwarding,no-X11-forwarding ssh-ed25519 AAAA… controller@local-server
```

- Replace `mirror-admin` with the logical name of the remote caller.
- The name must also appear in `admin_users` in `mirror.conf` if the caller needs
  admin commands (`reboot`, `shutdown`, `update_config`).

---

## Remote Command API

The remote host issues commands via plain SSH:

```bash
ssh mirror-admin@pi display_on
ssh mirror-admin@pi display_off
ssh mirror-admin@pi display_auto
ssh mirror-admin@pi restart_browser
ssh mirror-admin@pi get_status
ssh mirror-admin@pi get_logs --lines 100
ssh mirror-admin@pi update_config timeout=300
ssh mirror-admin@pi update_config pir_pin=17 timeout=60
ssh mirror-admin@pi reboot --delay 10
ssh mirror-admin@pi shutdown --delay 5
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
| `update_config` | **Yes** | `key=value …` | Update config keys at runtime and persist |
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

## Running Tests

No Raspberry Pi hardware is required.

```bash
python3 -m pytest tests/test_mirror_daemon.py -v
```

---

## Updating

```bash
git pull
sudo bash scripts/install.sh   # Restarts the service; preserves your mirror.conf
```

---

## Logs

```bash
# Live log tail
sudo journalctl -u pi-mirror-daemon -f

# Or directly from the log file
sudo tail -f /var/log/pi-mirror-daemon.log
```
