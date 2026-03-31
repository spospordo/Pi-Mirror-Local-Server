"""
tests/test_mirror_daemon.py – Unit tests for the Pi Mirror Daemon.

These tests run without Raspberry Pi hardware by mocking GPIO and subprocess
calls.  They validate:
  - Config loading and defaults
  - All daemon command handlers
  - Admin-privilege gating
  - SSH command parsing in mirror_cmd.py
  - End-to-end socket communication
"""

from __future__ import annotations

import configparser
import json
import os
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, call, mock_open, patch

# Ensure the repo root is on the path so imports work from the tests/ folder.
import sys

sys.path.insert(0, str(Path(__file__).parent.parent))


# ---------------------------------------------------------------------------
# pir_display tests
# ---------------------------------------------------------------------------


class TestHdmiHelpers(unittest.TestCase):
    @patch("pir_display.subprocess.run")
    def test_hdmi_on_success(self, mock_run: MagicMock) -> None:
        import pir_display

        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        result = pir_display.hdmi_on()
        self.assertTrue(result)
        mock_run.assert_called_once()
        args = mock_run.call_args[0][0]
        self.assertEqual(args[:3], ["sudo", "sh", "-c"])
        self.assertIn("echo on", args[3])
        self.assertIn(pir_display.HDMI_STATUS_FILE, args[3])

    @patch("pir_display.subprocess.run")
    def test_hdmi_off_success(self, mock_run: MagicMock) -> None:
        import pir_display

        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        result = pir_display.hdmi_off()
        self.assertTrue(result)
        mock_run.assert_called_once()
        args = mock_run.call_args[0][0]
        self.assertEqual(args[:3], ["sudo", "sh", "-c"])
        self.assertIn("echo off", args[3])
        self.assertIn(pir_display.HDMI_STATUS_FILE, args[3])

    @patch("pir_display.subprocess.run")
    def test_hdmi_on_failure(self, mock_run: MagicMock) -> None:
        import pir_display

        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="error")
        result = pir_display.hdmi_on()
        self.assertFalse(result)

    @patch("pir_display.subprocess.run")
    def test_hdmi_off_failure(self, mock_run: MagicMock) -> None:
        import pir_display

        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="error")
        result = pir_display.hdmi_off()
        self.assertFalse(result)

    def test_hdmi_is_on_from_file(self) -> None:
        import pir_display

        m = mock_open(read_data="connected\n")
        with patch("builtins.open", m):
            self.assertTrue(pir_display.hdmi_is_on())

    def test_hdmi_is_off_from_file(self) -> None:
        import pir_display

        m = mock_open(read_data="disconnected\n")
        with patch("builtins.open", m):
            self.assertFalse(pir_display.hdmi_is_on())

    @patch("pir_display.subprocess.run")
    def test_hdmi_is_on_fallback_vcgencmd(self, mock_run: MagicMock) -> None:
        import pir_display

        mock_run.return_value = MagicMock(
            returncode=0, stdout="display_power=1", stderr=""
        )
        with patch("builtins.open", side_effect=OSError("no file")):
            result = pir_display.hdmi_is_on()
        self.assertTrue(result)

    @patch("pir_display.subprocess.run")
    def test_hdmi_is_off_fallback_vcgencmd(self, mock_run: MagicMock) -> None:
        import pir_display

        mock_run.return_value = MagicMock(
            returncode=0, stdout="display_power=0", stderr=""
        )
        with patch("builtins.open", side_effect=OSError("no file")):
            result = pir_display.hdmi_is_on()
        self.assertFalse(result)

    @patch("pir_display.subprocess.run")
    def test_hdmi_is_on_returns_none_on_failure(self, mock_run: MagicMock) -> None:
        import pir_display

        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="err")
        with patch("builtins.open", side_effect=OSError("no file")):
            result = pir_display.hdmi_is_on()
        self.assertIsNone(result)


class TestRestartBrowser(unittest.TestCase):
    @patch("pir_display.subprocess.Popen")
    @patch("pir_display._run")
    @patch("pir_display.time.sleep")
    def test_restart_success(
        self,
        mock_sleep: MagicMock,
        mock_run: MagicMock,
        mock_popen: MagicMock,
    ) -> None:
        import pir_display

        mock_run.return_value = (0, "", "")
        result = pir_display.restart_browser()
        self.assertTrue(result)
        mock_popen.assert_called_once()

    @patch("pir_display.subprocess.Popen", side_effect=OSError("not found"))
    @patch("pir_display._run")
    @patch("pir_display.time.sleep")
    def test_restart_failure(
        self,
        mock_sleep: MagicMock,
        mock_run: MagicMock,
        mock_popen: MagicMock,
    ) -> None:
        import pir_display

        mock_run.return_value = (0, "", "")
        result = pir_display.restart_browser()
        self.assertFalse(result)

    @patch("pir_display.subprocess.Popen")
    @patch("pir_display._run")
    @patch("pir_display.time.sleep")
    def test_pkill_uses_sudo(
        self,
        mock_sleep: MagicMock,
        mock_run: MagicMock,
        mock_popen: MagicMock,
    ) -> None:
        """pkill must be invoked via sudo so it can kill browser processes
        owned by any user (the X session user differs from the daemon user)."""
        import pir_display

        mock_run.return_value = (0, "", "")
        pir_display.restart_browser()
        args, _ = mock_run.call_args
        cmd = args[0]
        self.assertEqual(cmd[0], "sudo")
        self.assertIn("pkill", cmd)

    @patch("pir_display.subprocess.Popen")
    @patch("pir_display._run")
    @patch("pir_display.time.sleep")
    def test_popen_sets_display_env(
        self,
        mock_sleep: MagicMock,
        mock_run: MagicMock,
        mock_popen: MagicMock,
    ) -> None:
        """Popen must receive DISPLAY=:0 so Chromium can connect to the X server."""
        import pir_display

        mock_run.return_value = (0, "", "")
        pir_display.restart_browser()
        _, kwargs = mock_popen.call_args
        env = kwargs.get("env", {})
        self.assertEqual(env.get("DISPLAY"), ":0")

    @patch("pir_display.subprocess.Popen")
    @patch("pir_display._run")
    @patch("pir_display.time.sleep")
    @patch("pir_display.os.path.exists")
    def test_popen_sets_xauthority_when_file_exists(
        self,
        mock_exists: MagicMock,
        mock_sleep: MagicMock,
        mock_run: MagicMock,
        mock_popen: MagicMock,
    ) -> None:
        """XAUTHORITY must be set in the Popen env when a valid file is found."""
        import pir_display

        mock_run.return_value = (0, "", "")
        # Simulate the first candidate existing
        mock_exists.side_effect = lambda p: True
        pir_display.restart_browser()
        _, kwargs = mock_popen.call_args
        env = kwargs.get("env", {})
        self.assertIn("XAUTHORITY", env)

    @patch("pir_display.subprocess.Popen")
    @patch("pir_display._run")
    @patch("pir_display.time.sleep")
    @patch("pir_display.os.path.exists", return_value=False)
    def test_popen_no_xauthority_when_no_file(
        self,
        mock_exists: MagicMock,
        mock_sleep: MagicMock,
        mock_run: MagicMock,
        mock_popen: MagicMock,
    ) -> None:
        """When no XAUTHORITY file is found, XAUTHORITY should not be set in env."""
        import pir_display

        mock_run.return_value = (0, "", "")
        pir_display.restart_browser()
        _, kwargs = mock_popen.call_args
        env = kwargs.get("env", {})
        self.assertNotIn("XAUTHORITY", env)


class TestMirrorConfig(unittest.TestCase):
    def test_default_values(self) -> None:
        from pir_display import MirrorConfig

        cfg = MirrorConfig()
        self.assertEqual(cfg.pir_pin, 22)
        self.assertEqual(cfg.timeout, 120)
        self.assertIn("chromium-browser", cfg.browser_cmd)

    def test_default_admin_users_empty(self) -> None:
        from pir_display import MirrorConfig

        cfg = MirrorConfig()
        self.assertEqual(cfg.admin_users, [])


# ---------------------------------------------------------------------------
# Config loading tests
# ---------------------------------------------------------------------------


class TestLoadConfig(unittest.TestCase):
    def _write_conf(self, tmp_path: str, content: str) -> str:
        conf_file = os.path.join(tmp_path, "mirror.conf")
        with open(conf_file, "w") as fh:
            fh.write(content)
        return conf_file

    def test_load_custom_values(self) -> None:
        from mirror_daemon import load_config

        with tempfile.TemporaryDirectory() as tmp:
            conf = self._write_conf(
                tmp,
                "[mirror]\npir_pin=17\ntimeout=60\nadmin_users=alice,bob\n",
            )
            cfg = load_config(conf)
        self.assertEqual(cfg.pir_pin, 17)
        self.assertEqual(cfg.timeout, 60)
        self.assertIn("alice", cfg.admin_users)
        self.assertIn("bob", cfg.admin_users)

    def test_load_missing_file_uses_defaults(self) -> None:
        from mirror_daemon import load_config
        from pir_display import MirrorConfig

        cfg = load_config("/nonexistent/mirror.conf")
        defaults = MirrorConfig()
        self.assertEqual(cfg.pir_pin, defaults.pir_pin)
        self.assertEqual(cfg.timeout, defaults.timeout)

    def test_load_no_section_uses_defaults(self) -> None:
        from mirror_daemon import load_config

        with tempfile.TemporaryDirectory() as tmp:
            conf = self._write_conf(tmp, "[other]\nfoo=bar\n")
            cfg = load_config(conf)
        from pir_display import MirrorConfig

        self.assertEqual(cfg.pir_pin, MirrorConfig().pir_pin)

    def test_load_browser_cmd(self) -> None:
        from mirror_daemon import load_config

        with tempfile.TemporaryDirectory() as tmp:
            conf = self._write_conf(
                tmp, "[mirror]\nbrowser_cmd=firefox --kiosk http://localhost\n"
            )
            cfg = load_config(conf)
        self.assertEqual(cfg.browser_cmd[0], "firefox")

    def test_load_socket_path(self) -> None:
        from mirror_daemon import load_config

        with tempfile.TemporaryDirectory() as tmp:
            conf = self._write_conf(tmp, "[mirror]\nsocket_path=/tmp/test.sock\n")
            cfg = load_config(conf)
        self.assertEqual(cfg.socket_path, "/tmp/test.sock")


# ---------------------------------------------------------------------------
# MirrorDaemon command handler tests
# ---------------------------------------------------------------------------


def _make_daemon(tmp_dir: str) -> "MirrorDaemon":
    """Create a MirrorDaemon pointed at a temp config with a temp socket."""
    import mirror_daemon
    from pir_display import MirrorConfig

    sock_path = os.path.join(tmp_dir, "mirror.sock")
    conf_path = os.path.join(tmp_dir, "mirror.conf")
    with open(conf_path, "w") as fh:
        fh.write(
            f"[mirror]\nsocket_path={sock_path}\n"
            f"log_file={tmp_dir}/mirror.log\n"
            "admin_users=admin\n"
        )

    with patch("mirror_daemon._configure_logging"):
        daemon = mirror_daemon.MirrorDaemon(config_path=conf_path)
    return daemon


class TestDaemonCommandHandlers(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._daemon = _make_daemon(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    # --- display_on / display_off / display_auto ---

    @patch("pir_display.hdmi_on")
    def test_display_on(self, mock_on: MagicMock) -> None:
        resp = self._daemon._dispatch({"command": "display_on", "user": "user1", "args": {}})
        self.assertEqual(resp["status"], "ok")
        mock_on.assert_called_once()
        self.assertTrue(self._daemon._pir.override_state())

    @patch("pir_display.hdmi_off")
    def test_display_off(self, mock_off: MagicMock) -> None:
        resp = self._daemon._dispatch({"command": "display_off", "user": "user1", "args": {}})
        self.assertEqual(resp["status"], "ok")
        mock_off.assert_called_once()
        self.assertFalse(self._daemon._pir.override_state())

    @patch("pir_display.hdmi_on")
    def test_display_auto_clears_override(self, mock_on: MagicMock) -> None:
        self._daemon._pir.force_display(True)
        resp = self._daemon._dispatch({"command": "display_auto", "user": "user1", "args": {}})
        self.assertEqual(resp["status"], "ok")
        self.assertIsNone(self._daemon._pir.override_state())

    # --- restart_browser ---

    @patch("mirror_daemon.restart_browser", return_value=True)
    def test_restart_browser_ok(self, mock_restart: MagicMock) -> None:
        resp = self._daemon._dispatch({"command": "restart_browser", "user": "u", "args": {}})
        self.assertEqual(resp["status"], "ok")

    @patch("mirror_daemon.restart_browser", return_value=False)
    def test_restart_browser_fail(self, mock_restart: MagicMock) -> None:
        resp = self._daemon._dispatch({"command": "restart_browser", "user": "u", "args": {}})
        self.assertEqual(resp["status"], "error")

    # --- get_status ---

    @patch("mirror_daemon.hdmi_is_on", return_value=True)
    def test_get_status(self, mock_status: MagicMock) -> None:
        resp = self._daemon._dispatch({"command": "get_status", "user": "u", "args": {}})
        self.assertEqual(resp["status"], "ok")
        data = resp["data"]
        self.assertIn("display_on", data)
        self.assertIn("uptime_seconds", data)
        self.assertIn("pir_pin", data)
        self.assertIn("timeout", data)

    # --- get_logs ---

    def test_get_logs_ok(self) -> None:
        log_path = os.path.join(self._tmp.name, "mirror.log")
        with open(log_path, "w") as fh:
            fh.write("line1\nline2\nline3\n")
        self._daemon._cfg.log_file = log_path
        resp = self._daemon._dispatch({"command": "get_logs", "user": "u", "args": {"lines": 2}})
        self.assertEqual(resp["status"], "ok")
        self.assertEqual(len(resp["data"]["lines"]), 2)

    def test_get_logs_missing_file(self) -> None:
        self._daemon._cfg.log_file = "/nonexistent/log.log"
        resp = self._daemon._dispatch({"command": "get_logs", "user": "u", "args": {}})
        self.assertEqual(resp["status"], "error")

    # --- update_config ---

    @patch("mirror_daemon._update_config_file")
    def test_update_config_ok(self, mock_write: MagicMock) -> None:
        resp = self._daemon._dispatch(
            {
                "command": "update_config",
                "user": "admin",
                "args": {"updates": {"timeout": "300"}},
            }
        )
        self.assertEqual(resp["status"], "ok")
        self.assertEqual(self._daemon._cfg.timeout, 300)
        mock_write.assert_called_once()

    @patch("mirror_daemon._update_config_file")
    def test_update_config_unknown_key(self, mock_write: MagicMock) -> None:
        resp = self._daemon._dispatch(
            {
                "command": "update_config",
                "user": "admin",
                "args": {"updates": {"nonexistent_key": "value"}},
            }
        )
        self.assertEqual(resp["status"], "error")
        mock_write.assert_not_called()

    @patch("mirror_daemon._update_config_file")
    def test_update_config_pir_pin(self, mock_write: MagicMock) -> None:
        resp = self._daemon._dispatch(
            {
                "command": "update_config",
                "user": "admin",
                "args": {"updates": {"pir_pin": "17"}},
            }
        )
        self.assertEqual(resp["status"], "ok")
        self.assertEqual(self._daemon._cfg.pir_pin, 17)

    # --- reboot / shutdown (admin only) ---

    @patch("mirror_daemon.threading.Thread")
    def test_reboot_admin_ok(self, mock_thread: MagicMock) -> None:
        mock_thread.return_value = MagicMock()
        resp = self._daemon._dispatch(
            {"command": "reboot", "user": "admin", "args": {"delay": 2}}
        )
        self.assertEqual(resp["status"], "ok")

    def test_reboot_non_admin_denied(self) -> None:
        resp = self._daemon._dispatch(
            {"command": "reboot", "user": "not_admin", "args": {}}
        )
        self.assertEqual(resp["status"], "error")
        self.assertIn("admin", resp["message"].lower())

    def test_shutdown_non_admin_denied(self) -> None:
        resp = self._daemon._dispatch(
            {"command": "shutdown", "user": "not_admin", "args": {}}
        )
        self.assertEqual(resp["status"], "error")

    def test_update_config_non_admin_denied(self) -> None:
        resp = self._daemon._dispatch(
            {
                "command": "update_config",
                "user": "not_admin",
                "args": {"updates": {"timeout": "60"}},
            }
        )
        self.assertEqual(resp["status"], "error")

    # --- unknown command ---

    def test_unknown_command(self) -> None:
        resp = self._daemon._dispatch({"command": "fly", "user": "u", "args": {}})
        self.assertEqual(resp["status"], "error")
        self.assertIn("Unknown command", resp["message"])


# ---------------------------------------------------------------------------
# Socket end-to-end test
# ---------------------------------------------------------------------------


class TestDaemonSocket(unittest.TestCase):
    """Start the daemon socket listener in a thread and send real commands."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._daemon = _make_daemon(self._tmp.name)

    def tearDown(self) -> None:
        self._daemon._running = False
        if self._daemon._sock:
            self._daemon._sock.close()
        self._tmp.cleanup()

    @patch("mirror_daemon.hdmi_is_on", return_value=True)
    def test_get_status_over_socket(self, _mock: MagicMock) -> None:
        """Send get_status via a real Unix socket and verify the response."""
        sock_path = self._daemon._cfg.socket_path
        ready = threading.Event()

        # Patch _serve_socket to signal readiness after bind
        original_serve = self._daemon._serve_socket

        def _serve_with_signal() -> None:
            # Bind manually so we can signal before accept loop
            from pathlib import Path

            Path(sock_path).unlink(missing_ok=True)
            srv_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            srv_sock.bind(sock_path)
            os.chmod(sock_path, 0o660)
            srv_sock.listen(5)
            srv_sock.setblocking(False)
            self._daemon._sock = srv_sock
            ready.set()  # signal that the socket is bound and listening

            import select as _sel

            while self._daemon._running:
                readable, _, _ = _sel.select([srv_sock], [], [], 1.0)
                if not readable:
                    continue
                try:
                    conn, _ = srv_sock.accept()
                except OSError:
                    break
                t2 = threading.Thread(
                    target=self._daemon._handle_connection,
                    args=(conn,),
                    daemon=True,
                )
                t2.start()

        self._daemon._running = True
        t = threading.Thread(target=_serve_with_signal, name="test-socket", daemon=True)
        t.start()
        self.assertTrue(ready.wait(timeout=5), "Daemon socket did not become ready")

        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.settimeout(5)
                client.connect(sock_path)
                payload = json.dumps(
                    {"command": "get_status", "user": "u", "args": {}}
                ) + "\n"
                client.sendall(payload.encode())
                data = b""
                while b"\n" not in data:
                    chunk = client.recv(4096)
                    if not chunk:
                        break
                    data += chunk
        finally:
            self._daemon._running = False
            t.join(timeout=3)

        response = json.loads(data.decode().strip())
        self.assertEqual(response["status"], "ok")
        self.assertIn("display_on", response["data"])


# ---------------------------------------------------------------------------
# mirror_cmd.py SSH command parsing tests
# ---------------------------------------------------------------------------


class TestMirrorCmdParsing(unittest.TestCase):
    def setUp(self) -> None:
        import mirror_cmd

        self._parse = mirror_cmd._parse_remote_command

    def test_display_on(self) -> None:
        req = self._parse("display_on")
        self.assertEqual(req["command"], "display_on")
        self.assertEqual(req["args"], {})

    def test_display_off(self) -> None:
        req = self._parse("display_off")
        self.assertEqual(req["command"], "display_off")

    def test_display_auto(self) -> None:
        req = self._parse("display_auto")
        self.assertEqual(req["command"], "display_auto")

    def test_restart_browser(self) -> None:
        req = self._parse("restart_browser")
        self.assertEqual(req["command"], "restart_browser")

    def test_get_status(self) -> None:
        req = self._parse("get_status")
        self.assertEqual(req["command"], "get_status")

    def test_get_logs_default_lines(self) -> None:
        req = self._parse("get_logs")
        self.assertEqual(req["command"], "get_logs")
        self.assertEqual(req["args"]["lines"], 50)

    def test_get_logs_custom_lines(self) -> None:
        req = self._parse("get_logs --lines 100")
        self.assertEqual(req["args"]["lines"], 100)

    def test_reboot_default_delay(self) -> None:
        req = self._parse("reboot")
        self.assertEqual(req["command"], "reboot")
        self.assertEqual(req["args"]["delay"], 5)

    def test_reboot_custom_delay(self) -> None:
        req = self._parse("reboot --delay 10")
        self.assertEqual(req["args"]["delay"], 10)

    def test_shutdown_default_delay(self) -> None:
        req = self._parse("shutdown")
        self.assertEqual(req["args"]["delay"], 5)

    def test_update_config_single_pair(self) -> None:
        req = self._parse("update_config timeout=300")
        self.assertEqual(req["command"], "update_config")
        self.assertEqual(req["args"]["updates"]["timeout"], "300")

    def test_update_config_multiple_pairs(self) -> None:
        req = self._parse("update_config timeout=300 pir_pin=17")
        self.assertEqual(req["args"]["updates"]["timeout"], "300")
        self.assertEqual(req["args"]["updates"]["pir_pin"], "17")

    def test_unknown_command_exits(self) -> None:
        with self.assertRaises(SystemExit) as ctx:
            self._parse("fly")
        self.assertEqual(ctx.exception.code, 2)

    def test_empty_command_exits(self) -> None:
        with self.assertRaises(SystemExit) as ctx:
            self._parse("")
        self.assertEqual(ctx.exception.code, 2)

    def test_update_config_missing_value_exits(self) -> None:
        with self.assertRaises(SystemExit) as ctx:
            self._parse("update_config notakeyvalue")
        self.assertEqual(ctx.exception.code, 2)

    def test_update_config_no_pairs_exits(self) -> None:
        with self.assertRaises(SystemExit) as ctx:
            self._parse("update_config")
        self.assertEqual(ctx.exception.code, 2)


class TestMirrorCmdMain(unittest.TestCase):
    """Test the mirror_cmd.main() entry point end-to-end with a mock daemon."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._sock_path = os.path.join(self._tmp.name, "test.sock")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _start_mock_daemon(self, response: dict) -> threading.Thread:
        """Spin up a minimal Unix socket server that returns *response*."""
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(self._sock_path)
        srv.listen(1)
        srv.settimeout(3)

        def _serve() -> None:
            try:
                conn, _ = srv.accept()
                conn.recv(4096)  # consume request
                conn.sendall((json.dumps(response) + "\n").encode())
                conn.close()
            except OSError:
                pass
            finally:
                srv.close()

        t = threading.Thread(target=_serve, daemon=True)
        t.start()
        time.sleep(0.05)
        return t

    def test_main_ok_exit_zero(self) -> None:
        import mirror_cmd

        self._start_mock_daemon({"status": "ok", "data": {}, "message": "done"})

        with patch.dict(
            os.environ, {"SSH_ORIGINAL_COMMAND": "display_on", "MIRROR_SOCKET": self._sock_path}
        ):
            with self.assertRaises(SystemExit) as ctx:
                mirror_cmd.main(["--user", "testuser"])
        self.assertEqual(ctx.exception.code, 0)

    def test_main_error_exit_one(self) -> None:
        import mirror_cmd

        self._start_mock_daemon({"status": "error", "data": {}, "message": "denied"})

        with patch.dict(
            os.environ,
            {"SSH_ORIGINAL_COMMAND": "display_on", "MIRROR_SOCKET": self._sock_path},
        ):
            with self.assertRaises(SystemExit) as ctx:
                mirror_cmd.main(["--user", "testuser"])
        self.assertEqual(ctx.exception.code, 1)

    def test_main_no_ssh_original_command_exits_two(self) -> None:
        import mirror_cmd

        env = {k: v for k, v in os.environ.items() if k != "SSH_ORIGINAL_COMMAND"}
        with patch.dict(os.environ, env, clear=True):
            with self.assertRaises(SystemExit) as ctx:
                mirror_cmd.main(["--user", "testuser"])
        self.assertEqual(ctx.exception.code, 2)


# ---------------------------------------------------------------------------
# PIRController tests (no GPIO hardware required)
# ---------------------------------------------------------------------------


class TestPIRControllerOverrides(unittest.TestCase):
    def setUp(self) -> None:
        from pir_display import MirrorConfig, PIRController

        self._cfg = MirrorConfig()
        self._ctrl = PIRController(self._cfg)

    @patch("pir_display.hdmi_on")
    def test_force_on(self, mock_on: MagicMock) -> None:
        self._ctrl.force_display(True)
        mock_on.assert_called_once()
        self.assertTrue(self._ctrl.override_state())
        self.assertTrue(self._ctrl.is_overridden())

    @patch("pir_display.hdmi_off")
    def test_force_off(self, mock_off: MagicMock) -> None:
        self._ctrl.force_display(False)
        mock_off.assert_called_once()
        self.assertFalse(self._ctrl.override_state())
        self.assertTrue(self._ctrl.is_overridden())

    @patch("pir_display.hdmi_on")
    def test_clear_override(self, mock_on: MagicMock) -> None:
        self._ctrl.force_display(True)
        self._ctrl.clear_override()
        self.assertIsNone(self._ctrl.override_state())
        self.assertFalse(self._ctrl.is_overridden())


if __name__ == "__main__":
    unittest.main()
