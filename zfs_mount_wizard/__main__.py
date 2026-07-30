"""Application entry point and root-first launcher."""

from __future__ import annotations

import os
import pwd
import shutil
import subprocess
import sys
from collections.abc import Sequence


APP_NAME = "zmnt"
VERSION = "0.1.0"
USAGE = """Usage: zmnt [COMMAND]

Commands:
  tui       Start the terminal interface (default)
  gui       Start the graphical interface
  help      Show this help message
  version   Show the installed version
"""


def ensure_elevated_from_start(
    module: str = "zfs_mount_wizard", relaunch_args: Sequence[str] | None = None,
) -> None:
    if os.geteuid() == 0:
        return
    run0 = shutil.which("run0")
    if not run0:
        raise RuntimeError("run0 is required to start this wizard as root from the beginning.")

    original_user = os.environ.get("USER") or pwd.getpwuid(os.getuid()).pw_name
    args = [
        "--description=zmnt",
        f"--setenv=ZFS_WIZARD_ORIG_UID={os.getuid()}",
        f"--setenv=ZFS_WIZARD_ORIG_USER={original_user}",
        f"--setenv=ZFS_WIZARD_ORIG_HOME={os.environ.get('HOME', '')}",
    ]
    for name in ("DISPLAY", "WAYLAND_DISPLAY", "XAUTHORITY", "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS", "DESKTOP_SESSION", "XDG_CURRENT_DESKTOP"):
        if value := os.environ.get(name):
            args.append(f"--setenv={name}={value}")
    child_args = list(sys.argv[1:] if relaunch_args is None else relaunch_args)
    result = subprocess.run([run0, *args, sys.executable, "-m", module, *child_args], check=False)
    raise SystemExit(result.returncode)


def command_from_args(args: Sequence[str]) -> tuple[str, list[str]]:
    if not args:
        return "tui", []
    command, *remaining = args
    if command in {"tui", "gui"}:
        return command, remaining
    if command in {"help", "--help", "-h"}:
        return "help", remaining
    if command in {"version", "--version", "-V"}:
        return "version", remaining
    raise ValueError(f"Unknown command: {command}")


def run_gui() -> None:
    try:
        ensure_elevated_from_start(relaunch_args=["gui"])
    except RuntimeError as error:
        print(f"{APP_NAME}: {error}", file=sys.stderr)
        raise SystemExit(1) from error

    from PySide6.QtWidgets import QApplication

    from .ui import WizardWindow

    application = QApplication(sys.argv)
    application.setApplicationName(APP_NAME)
    window = WizardWindow()
    window.show()
    raise SystemExit(application.exec())


def main() -> None:
    try:
        command, remaining = command_from_args(sys.argv[1:])
    except ValueError as error:
        print(f"{APP_NAME}: {error}\n\n{USAGE}", file=sys.stderr)
        raise SystemExit(2) from error
    if remaining:
        print(f"{APP_NAME}: command '{command}' does not accept arguments\n\n{USAGE}", file=sys.stderr)
        raise SystemExit(2)
    if command == "help":
        print(USAGE, end="")
        return
    if command == "version":
        print(VERSION)
        return
    if command == "tui":
        from .tui import main as tui_main

        tui_main()
        return
    run_gui()


if __name__ == "__main__":
    main()
