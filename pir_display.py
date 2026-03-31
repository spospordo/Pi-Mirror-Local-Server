"""
pir_display.py – Core PIR motion-detection and HDMI display-control module.

This module encapsulates all Raspberry Pi GPIO and HDMI interactions.
It is designed to be imported by the main daemon (mirror_daemon.py) so the
PIR logic can be reused or evolved independently of the remote-management layer.

Hardware defaults (overridable via MirrorConfig):
  PIR_PIN : BCM GPIO pin connected to the PIR sensor output  (default 22)
  TIMEOUT : seconds of inactivity before the display is turned off (default 120)
"""

from __future__ import annotations

import logging
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# HDMI helpers
# ---------------------------------------------------------------------------

HDMI_STATUS_FILE = "/sys/class/drm/card0-HDMI-A-1/status"


def _run(cmd: list[str]) -> tuple[int, str, str]:
    """Run a subprocess and return (returncode, stdout, stderr)."""
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=10,
        )
        return result.returncode, result.stdout.strip(), result.stderr.strip()
    except FileNotFoundError:
        return 1, "", f"Command not found: {cmd[0]}"
    except subprocess.TimeoutExpired:
        return 1, "", f"Command timed out: {' '.join(cmd)}"


def hdmi_on() -> bool:
    """Turn the HDMI display on.  Returns True on success."""
    rc, _, err = _run(["sudo", "sh", "-c", f"echo on > {HDMI_STATUS_FILE}"])
    if rc == 0:
        log.info("HDMI display turned ON")
        return True
    log.error("Failed to turn HDMI display ON: %s", err)
    return False


def hdmi_off() -> bool:
    """Turn the HDMI display off.  Returns True on success."""
    rc, _, err = _run(["sudo", "sh", "-c", f"echo off > {HDMI_STATUS_FILE}"])
    if rc == 0:
        log.info("HDMI display turned OFF")
        return True
    log.error("Failed to turn HDMI display OFF: %s", err)
    return False


def hdmi_is_on() -> Optional[bool]:
    """
    Return True if the HDMI display is currently on, False if off, or None if
    the status cannot be determined.
    """
    try:
        with open(HDMI_STATUS_FILE, "r", encoding="utf-8") as fh:
            status = fh.read().strip().lower()
        return status == "connected"
    except OSError:
        pass

    # Fallback: ask vcgencmd
    rc, stdout, _ = _run(["vcgencmd", "display_power"])
    if rc == 0 and "=" in stdout:
        return stdout.split("=", 1)[1].strip() == "1"
    return None


# ---------------------------------------------------------------------------
# Browser / dashboard process
# ---------------------------------------------------------------------------

BROWSER_CMD_DEFAULT = [
    "chromium-browser",
    "--noerrdialogs",
    "--disable-infobars",
    "--kiosk",
    "http://localhost",
]


def restart_browser(cmd: Optional[list[str]] = None) -> bool:
    """
    Kill any running browser instance and start a fresh one.
    *cmd* overrides the default browser command list.
    Returns True on success.
    """
    browser_cmd = cmd or BROWSER_CMD_DEFAULT
    browser_bin = browser_cmd[0]

    # Kill existing instance (ignore errors if not running)
    _run(["pkill", "-f", browser_bin])
    time.sleep(1)

    try:
        subprocess.Popen(
            browser_cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        log.info("Browser restarted: %s", " ".join(browser_cmd))
        return True
    except OSError as exc:
        log.error("Failed to restart browser: %s", exc)
        return False


# ---------------------------------------------------------------------------
# PIR controller
# ---------------------------------------------------------------------------


@dataclass
class MirrorConfig:
    """All tuneable knobs for the daemon.  Loaded from mirror.conf."""

    pir_pin: int = 22
    timeout: int = 120
    poll_interval: float = 0.5  # seconds between PIR sensor reads
    browser_cmd: list[str] = field(default_factory=lambda: list(BROWSER_CMD_DEFAULT))
    log_file: str = "/var/log/pi-mirror-daemon.log"
    socket_path: str = "/run/pi-mirror/pi-mirror-daemon.sock"
    admin_users: list[str] = field(default_factory=list)


class PIRController:
    """
    Manages PIR-driven display control in a background thread.

    The controller exposes a *remote_override* flag.  When set by an external
    command (display_on / display_off), PIR motion events will NOT change the
    display state until :meth:`clear_override` is called (display_auto).
    """

    def __init__(self, config: MirrorConfig) -> None:
        self._cfg = config
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

        # None  = no override (PIR in control)
        # True  = remote forced ON
        # False = remote forced OFF
        self._override: Optional[bool] = None
        self._lock = threading.Lock()
        self._last_motion = time.monotonic()

        self._on_state_change: Optional[Callable[[bool], None]] = None

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def set_state_change_callback(self, cb: Callable[[bool], None]) -> None:
        """Register a callback invoked with the new display state whenever it changes."""
        self._on_state_change = cb

    def start(self) -> None:
        """Start the PIR monitoring thread."""
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run_loop, name="pir-controller", daemon=True
        )
        self._thread.start()
        log.info(
            "PIR controller started (pin=%d, timeout=%ds)",
            self._cfg.pir_pin,
            self._cfg.timeout,
        )

    def stop(self) -> None:
        """Signal the PIR thread to stop and wait for it to exit."""
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)
        log.info("PIR controller stopped")

    def force_display(self, state: bool) -> None:
        """
        Set a remote override: True = force ON, False = force OFF.
        PIR motion will not change the display while the override is active.
        """
        with self._lock:
            self._override = state
        if state:
            hdmi_on()
        else:
            hdmi_off()
        log.info("Remote display override: %s", "ON" if state else "OFF")

    def clear_override(self) -> None:
        """Remove remote override; PIR resumes control."""
        with self._lock:
            self._override = None
        log.info("Remote display override cleared; PIR control resumed")

    def is_overridden(self) -> bool:
        with self._lock:
            return self._override is not None

    def override_state(self) -> Optional[bool]:
        with self._lock:
            return self._override

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _run_loop(self) -> None:
        """Main PIR polling loop (runs in its own thread)."""
        try:
            import RPi.GPIO as GPIO  # type: ignore[import]

            GPIO.setmode(GPIO.BCM)
            GPIO.setup(self._cfg.pir_pin, GPIO.IN)
            gpio_available = True
        except (ImportError, RuntimeError) as exc:
            log.warning("RPi.GPIO not available (%s); PIR detection disabled.", exc)
            gpio_available = False

        display_currently_on = True
        self._last_motion = time.monotonic()

        try:
            while not self._stop_event.is_set():
                with self._lock:
                    override = self._override

                if override is not None:
                    # Remote has taken control – just sleep
                    time.sleep(self._cfg.poll_interval)
                    continue

                # --- PIR in control ---
                motion_detected = False
                if gpio_available:
                    try:
                        import RPi.GPIO as GPIO  # type: ignore[import]

                        motion_detected = GPIO.input(self._cfg.pir_pin) == GPIO.HIGH
                    except RuntimeError as exc:
                        log.warning("GPIO read error: %s", exc)

                now = time.monotonic()
                if motion_detected:
                    self._last_motion = now
                    if not display_currently_on:
                        hdmi_on()
                        display_currently_on = True
                        if self._on_state_change:
                            self._on_state_change(True)
                elif display_currently_on:
                    elapsed = now - self._last_motion
                    if elapsed >= self._cfg.timeout:
                        hdmi_off()
                        display_currently_on = False
                        if self._on_state_change:
                            self._on_state_change(False)

                time.sleep(self._cfg.poll_interval)
        finally:
            if gpio_available:
                try:
                    import RPi.GPIO as GPIO  # type: ignore[import]

                    GPIO.cleanup()
                except RuntimeError:
                    pass
