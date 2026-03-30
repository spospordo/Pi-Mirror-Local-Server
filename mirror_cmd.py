"""
mirror_cmd.py – SSH ForceCommand shim for Pi Mirror Daemon.

This script is invoked by sshd via the ForceCommand directive in
~/.ssh/authorized_keys.  It reads the command the remote caller originally
requested (from SSH_ORIGINAL_COMMAND), validates and maps it to a structured
JSON request, then forwards that request to the daemon over the Unix-domain
socket, and prints the JSON response back to the caller.

Usage (set in authorized_keys):
    command="python3 /opt/pi-mirror/mirror_cmd.py --user <username>",no-pty <pubkey>

The remote caller then runs commands like:
    ssh pi@mirror display_on
    ssh pi@mirror display_off
    ssh pi@mirror display_auto
    ssh pi@mirror restart_browser
    ssh pi@mirror get_status
    ssh pi@mirror get_logs [--lines N]
    ssh pi@mirror update_config timeout=300
    ssh pi@mirror reboot [--delay N]
    ssh pi@mirror shutdown [--delay N]

Exit codes:
    0  – command succeeded
    1  – daemon returned an error
    2  – usage / parse error
    3  – could not connect to daemon socket
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import socket
import sys
from pathlib import Path

DEFAULT_SOCKET_PATH = "/run/pi-mirror/pi-mirror-daemon.sock"


# ---------------------------------------------------------------------------
# Command parsing
# ---------------------------------------------------------------------------

KNOWN_COMMANDS = {
    "display_on",
    "display_off",
    "display_auto",
    "restart_browser",
    "reboot",
    "shutdown",
    "get_status",
    "get_logs",
    "update_config",
}


def _parse_remote_command(raw: str) -> dict:
    """
    Parse a space-separated command string from SSH_ORIGINAL_COMMAND.
    Returns a dict suitable for JSON-encoding and sending to the daemon.

    Raises SystemExit(2) on usage errors.
    """
    try:
        tokens = shlex.split(raw.strip())
    except ValueError as exc:
        print(f"error: could not parse command: {exc}", file=sys.stderr)
        sys.exit(2)

    if not tokens:
        print("error: no command provided.", file=sys.stderr)
        _print_usage()
        sys.exit(2)

    command = tokens[0]
    if command not in KNOWN_COMMANDS:
        print(f"error: unknown command '{command}'.", file=sys.stderr)
        _print_usage()
        sys.exit(2)

    args: dict = {}

    # Command-specific argument parsing
    if command == "get_logs":
        p = argparse.ArgumentParser(prog="get_logs", add_help=False)
        p.add_argument("--lines", type=int, default=50)
        ns, _ = p.parse_known_args(tokens[1:])
        args["lines"] = ns.lines

    elif command in ("reboot", "shutdown"):
        p = argparse.ArgumentParser(prog=command, add_help=False)
        p.add_argument("--delay", type=int, default=5)
        ns, _ = p.parse_known_args(tokens[1:])
        args["delay"] = ns.delay

    elif command == "update_config":
        # Remaining tokens are key=value pairs
        updates: dict[str, str] = {}
        for token in tokens[1:]:
            if "=" not in token:
                print(
                    f"error: expected key=value, got '{token}'.", file=sys.stderr
                )
                sys.exit(2)
            k, v = token.split("=", 1)
            updates[k.strip()] = v.strip()
        if not updates:
            print("error: update_config requires at least one key=value.", file=sys.stderr)
            sys.exit(2)
        args["updates"] = updates

    return {"command": command, "args": args}


def _print_usage() -> None:
    print(
        "Available commands:\n"
        "  display_on\n"
        "  display_off\n"
        "  display_auto\n"
        "  restart_browser\n"
        "  get_status\n"
        "  get_logs [--lines N]\n"
        "  update_config key=value [key=value …]\n"
        "  reboot [--delay N]\n"
        "  shutdown [--delay N]\n",
        file=sys.stderr,
    )


# ---------------------------------------------------------------------------
# Daemon communication
# ---------------------------------------------------------------------------


def _send_to_daemon(
    request: dict, socket_path: str = DEFAULT_SOCKET_PATH
) -> dict:
    """Send *request* to the daemon and return the parsed response dict."""
    sock_file = Path(socket_path)
    if not sock_file.exists():
        print(
            f"error: daemon socket not found at {socket_path}. "
            "Is pi-mirror-daemon running?",
            file=sys.stderr,
        )
        sys.exit(3)

    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(10)
            sock.connect(socket_path)
            payload = json.dumps(request) + "\n"
            sock.sendall(payload.encode("utf-8"))

            data = b""
            while b"\n" not in data:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                data += chunk

        return json.loads(data.decode("utf-8").strip())
    except (OSError, json.JSONDecodeError) as exc:
        print(f"error: communication with daemon failed: {exc}", file=sys.stderr)
        sys.exit(3)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Mirror Daemon SSH command shim",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--user",
        required=True,
        help="Authenticated SSH username (injected by ForceCommand in authorized_keys).",
    )
    parser.add_argument(
        "--socket",
        default=os.environ.get("MIRROR_SOCKET", DEFAULT_SOCKET_PATH),
        dest="socket_path",
        help="Path to the daemon Unix socket.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    ns = _parse_args(argv)

    raw_cmd = os.environ.get("SSH_ORIGINAL_COMMAND", "").strip()
    if not raw_cmd:
        print("error: SSH_ORIGINAL_COMMAND is not set.", file=sys.stderr)
        _print_usage()
        sys.exit(2)

    request = _parse_remote_command(raw_cmd)
    request["user"] = ns.user

    response = _send_to_daemon(request, socket_path=ns.socket_path)

    # Pretty-print the response
    print(json.dumps(response, indent=2))

    sys.exit(0 if response.get("status") == "ok" else 1)


if __name__ == "__main__":
    main()
