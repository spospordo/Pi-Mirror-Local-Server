"""
mirror_daemon.py – Pi Mirror Remote Management Daemon.

This is the main entry point for the systemd service.  It:

  1. Loads configuration from mirror.conf (path configurable via
     MIRROR_CONF env-var or --config CLI flag).
  2. Starts the PIR motion-detection / display-control loop
     (via pir_display.PIRController).
  3. Listens on a Unix-domain socket for JSON command messages from
     mirror_cmd.py (the SSH-ForceCommand shim) and authenticated remote
     callers.
  4. Handles all defined commands, logs every operation, and returns
     JSON responses.

Socket protocol
---------------
Request  (newline-terminated JSON):
    {"command": "<cmd>", "user": "<ssh_user>", "args": {…}}

Response (newline-terminated JSON):
    {"status": "ok"|"error", "data": {…}, "message": "…"}

Commands
--------
  display_on      – Force display ON  (overrides PIR)
  display_off     – Force display OFF (overrides PIR)
  display_auto    – Return display control to PIR sensor
  restart_browser – Kill + relaunch the kiosk browser
  reboot          – Reboot the Pi  (admin only)
  shutdown        – Shut the Pi down  (admin only)
  get_status      – Return current state as JSON
  get_logs        – Return the last N lines of the log file
  update_config   – Update one or more config keys at runtime
"""

from __future__ import annotations

import argparse
import json
import logging
import logging.handlers
import os
import select
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

# Project-local module
import pir_display
from pir_display import MirrorConfig, PIRController, hdmi_is_on, restart_browser

# ---------------------------------------------------------------------------
# Logging setup (file + syslog-style stderr)
# ---------------------------------------------------------------------------

LOG_FORMAT = "%(asctime)s %(name)s %(levelname)s %(message)s"
log = logging.getLogger("mirror_daemon")


def _configure_logging(cfg: MirrorConfig) -> None:
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    # Rotating file handler
    try:
        fh = logging.handlers.RotatingFileHandler(
            cfg.log_file, maxBytes=5 * 1024 * 1024, backupCount=3
        )
        fh.setFormatter(logging.Formatter(LOG_FORMAT))
        root.addHandler(fh)
    except OSError as exc:
        # Fall back to stderr if we cannot write the log file
        print(f"WARNING: cannot open log file {cfg.log_file}: {exc}", file=sys.stderr)

    # Always also log to stderr (captured by systemd/journald)
    sh = logging.StreamHandler(sys.stderr)
    sh.setFormatter(logging.Formatter(LOG_FORMAT))
    root.addHandler(sh)


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

DEFAULT_CONFIG_PATH = "/etc/pi-mirror/mirror.conf"


def load_config(path: str) -> MirrorConfig:
    """Parse the INI-style config file and return a :class:`MirrorConfig`."""
    import configparser

    cfg = MirrorConfig()
    parser = configparser.ConfigParser()

    if not parser.read(path):
        log.warning("Config file not found at %s; using defaults.", path)
        return cfg

    section = "mirror"
    if section not in parser:
        log.warning("Config file has no [mirror] section; using defaults.")
        return cfg

    def get(key: str, default: Any) -> Any:
        return parser.get(section, key, fallback=default)

    cfg.pir_pin = int(get("pir_pin", cfg.pir_pin))
    cfg.timeout = int(get("timeout", cfg.timeout))
    cfg.log_file = get("log_file", cfg.log_file)
    cfg.socket_path = get("socket_path", cfg.socket_path)

    browser_cmd_str = get("browser_cmd", "")
    if browser_cmd_str:
        cfg.browser_cmd = browser_cmd_str.split()

    admin_users_str = get("admin_users", "")
    if admin_users_str:
        cfg.admin_users = [u.strip() for u in admin_users_str.split(",") if u.strip()]

    log.info(
        "Config loaded from %s: pir_pin=%d, timeout=%d, admins=%s",
        path,
        cfg.pir_pin,
        cfg.timeout,
        cfg.admin_users,
    )
    return cfg


def _update_config_file(path: str, updates: dict[str, str]) -> None:
    """Persist *updates* back into the config file under [mirror]."""
    import configparser

    parser = configparser.ConfigParser()
    parser.read(path)
    if "mirror" not in parser:
        parser["mirror"] = {}
    for key, value in updates.items():
        parser["mirror"][key] = str(value)
    with open(path, "w", encoding="utf-8") as fh:
        parser.write(fh)


# ---------------------------------------------------------------------------
# Daemon
# ---------------------------------------------------------------------------

# Writable config keys (subset safe to update remotely)
_MUTABLE_CONFIG_KEYS = {"timeout", "pir_pin", "browser_cmd"}
# Config keys restricted to admin users
_ADMIN_ONLY_COMMANDS = {"reboot", "shutdown", "update_config"}


class MirrorDaemon:
    """Core daemon: owns the PIR controller and the Unix-socket server."""

    def __init__(self, config_path: str) -> None:
        self._config_path = config_path
        self._cfg = load_config(config_path)
        _configure_logging(self._cfg)
        self._pir = PIRController(self._cfg)
        self._pir.set_state_change_callback(self._on_display_change)
        self._display_on: bool = True
        self._start_time = time.time()
        self._running = False
        self._sock: socket.socket | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def run(self) -> None:
        """Start daemon; blocks until a stop signal is received."""
        log.info("Pi Mirror Daemon starting (config=%s)", self._config_path)
        self._running = True

        # Register graceful shutdown on SIGTERM / SIGINT
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, self._handle_signal)

        self._pir.start()

        try:
            self._serve_socket()
        finally:
            self._cleanup()

    def _handle_signal(self, signum: int, _frame: Any) -> None:
        log.info("Received signal %d; shutting down.", signum)
        self._running = False

    def _cleanup(self) -> None:
        self._pir.stop()
        if self._sock:
            self._sock.close()
        sock_path = Path(self._cfg.socket_path)
        if sock_path.exists():
            sock_path.unlink(missing_ok=True)
        log.info("Pi Mirror Daemon stopped.")

    def _on_display_change(self, is_on: bool) -> None:
        self._display_on = is_on
        log.debug("Display state changed by PIR: %s", "ON" if is_on else "OFF")

    # ------------------------------------------------------------------
    # Unix socket server
    # ------------------------------------------------------------------

    def _serve_socket(self) -> None:
        sock_path = self._cfg.socket_path
        # Remove stale socket
        Path(sock_path).unlink(missing_ok=True)

        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.bind(sock_path)
        # Permissions: owner read/write only (daemon runs as dedicated user)
        os.chmod(sock_path, 0o660)
        self._sock.listen(5)
        self._sock.setblocking(False)
        log.info("Listening on Unix socket: %s", sock_path)

        while self._running:
            readable, _, _ = select.select([self._sock], [], [], 1.0)
            if not readable:
                continue
            try:
                conn, _ = self._sock.accept()
            except OSError:
                break
            t = threading.Thread(
                target=self._handle_connection, args=(conn,), daemon=True
            )
            t.start()

    def _handle_connection(self, conn: socket.socket) -> None:
        try:
            data = b""
            conn.settimeout(5)
            while b"\n" not in data:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                data += chunk

            if not data:
                return

            try:
                request = json.loads(data.decode("utf-8").strip())
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                self._send_response(conn, "error", message=f"Invalid JSON: {exc}")
                return

            log.info(
                "Received command '%s' from user '%s'",
                request.get("command"),
                request.get("user", "<unknown>"),
            )

            response = self._dispatch(request)
            self._send_response(conn, **response)
        except OSError as exc:
            log.warning("Socket error handling connection: %s", exc)
        finally:
            conn.close()

    @staticmethod
    def _send_response(
        conn: socket.socket,
        status: str = "ok",
        data: dict | None = None,
        message: str = "",
    ) -> None:
        payload = json.dumps(
            {"status": status, "data": data or {}, "message": message}
        )
        try:
            conn.sendall((payload + "\n").encode("utf-8"))
        except OSError:
            pass

    # ------------------------------------------------------------------
    # Command dispatch
    # ------------------------------------------------------------------

    def _dispatch(self, request: dict) -> dict:
        command = request.get("command", "")
        user = request.get("user", "")
        args = request.get("args", {})

        # Admin-only gate
        if command in _ADMIN_ONLY_COMMANDS:
            if user not in self._cfg.admin_users:
                log.warning(
                    "User '%s' attempted admin command '%s' – denied.", user, command
                )
                return {
                    "status": "error",
                    "message": f"Command '{command}' requires admin privileges.",
                }

        handlers = {
            "display_on": self._cmd_display_on,
            "display_off": self._cmd_display_off,
            "display_auto": self._cmd_display_auto,
            "restart_browser": self._cmd_restart_browser,
            "reboot": self._cmd_reboot,
            "shutdown": self._cmd_shutdown,
            "get_status": self._cmd_get_status,
            "get_logs": self._cmd_get_logs,
            "update_config": self._cmd_update_config,
        }

        handler = handlers.get(command)
        if handler is None:
            return {"status": "error", "message": f"Unknown command: '{command}'"}

        try:
            return handler(user=user, args=args)
        except Exception as exc:  # noqa: BLE001
            log.exception("Unhandled exception executing command '%s'", command)
            return {"status": "error", "message": str(exc)}

    # ------------------------------------------------------------------
    # Command handlers
    # ------------------------------------------------------------------

    def _cmd_display_on(self, *, user: str, args: dict) -> dict:
        self._pir.force_display(True)
        self._display_on = True
        return {"status": "ok", "message": "Display forced ON."}

    def _cmd_display_off(self, *, user: str, args: dict) -> dict:
        self._pir.force_display(False)
        self._display_on = False
        return {"status": "ok", "message": "Display forced OFF."}

    def _cmd_display_auto(self, *, user: str, args: dict) -> dict:
        self._pir.clear_override()
        return {"status": "ok", "message": "PIR motion control resumed."}

    def _cmd_restart_browser(self, *, user: str, args: dict) -> dict:
        ok = restart_browser(self._cfg.browser_cmd)
        if ok:
            return {"status": "ok", "message": "Browser restarted."}
        return {"status": "error", "message": "Failed to restart browser."}

    def _cmd_reboot(self, *, user: str, args: dict) -> dict:
        delay = int(args.get("delay", 5))
        log.info("REBOOT requested by user '%s' (delay=%ds)", user, delay)

        def _do_reboot() -> None:
            time.sleep(delay)
            result = subprocess.run(["sudo", "reboot"], check=False)  # noqa: S603,S607
            if result.returncode != 0:
                log.error("reboot command failed with exit code %d", result.returncode)

        threading.Thread(target=_do_reboot, daemon=True).start()
        return {"status": "ok", "message": f"Rebooting in {delay} seconds."}

    def _cmd_shutdown(self, *, user: str, args: dict) -> dict:
        delay = int(args.get("delay", 5))
        log.info("SHUTDOWN requested by user '%s' (delay=%ds)", user, delay)

        def _do_shutdown() -> None:
            time.sleep(delay)
            result = subprocess.run(["sudo", "shutdown", "-h", "now"], check=False)  # noqa: S603,S607
            if result.returncode != 0:
                log.error("shutdown command failed with exit code %d", result.returncode)

        threading.Thread(target=_do_shutdown, daemon=True).start()
        return {"status": "ok", "message": f"Shutting down in {delay} seconds."}

    def _cmd_get_status(self, *, user: str, args: dict) -> dict:
        current_display = hdmi_is_on()
        override = self._pir.override_state()
        uptime_secs = int(time.time() - self._start_time)
        status_data = {
            "display_on": current_display,
            "pir_override": override,
            "pir_override_label": (
                "none"
                if override is None
                else ("forced_on" if override else "forced_off")
            ),
            "pir_pin": self._cfg.pir_pin,
            "timeout": self._cfg.timeout,
            "uptime_seconds": uptime_secs,
            "log_file": self._cfg.log_file,
        }
        return {"status": "ok", "data": status_data}

    def _cmd_get_logs(self, *, user: str, args: dict) -> dict:
        lines = int(args.get("lines", 50))
        try:
            with open(self._cfg.log_file, "r", encoding="utf-8", errors="replace") as fh:
                all_lines = fh.readlines()
            tail = all_lines[-lines:]
            return {"status": "ok", "data": {"lines": tail}}
        except OSError as exc:
            return {"status": "error", "message": f"Cannot read log: {exc}"}

    def _cmd_update_config(self, *, user: str, args: dict) -> dict:
        updates = args.get("updates", {})
        unknown = set(updates.keys()) - _MUTABLE_CONFIG_KEYS
        if unknown:
            return {
                "status": "error",
                "message": f"Unknown or immutable config keys: {unknown}",
            }

        # Apply to in-memory config
        for key, value in updates.items():
            if key == "timeout":
                self._cfg.timeout = int(value)
            elif key == "pir_pin":
                self._cfg.pir_pin = int(value)
            elif key == "browser_cmd":
                self._cfg.browser_cmd = str(value).split()

        # Persist
        try:
            _update_config_file(
                self._config_path, {k: str(v) for k, v in updates.items()}
            )
        except OSError as exc:
            log.warning("Could not persist config update: %s", exc)
            return {
                "status": "ok",
                "message": f"Config updated in memory only (file write failed: {exc}).",
            }

        log.info("Config updated by user '%s': %s", user, updates)
        return {"status": "ok", "message": "Config updated and persisted."}


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Pi Mirror Remote Management Daemon",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--config",
        default=os.environ.get("MIRROR_CONF", DEFAULT_CONFIG_PATH),
        help="Path to mirror.conf",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)
    daemon = MirrorDaemon(config_path=args.config)
    daemon.run()


if __name__ == "__main__":
    main()
