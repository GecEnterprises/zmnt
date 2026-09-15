"""Application entry point and root-first launcher."""

from __future__ import annotations

import os
import argparse
import getpass
import json
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
  daemon    Run the systemd automount lifecycle action
  auto      Configure encrypted ZFS automount entries
  automount Unlock and mount enabled entries now
  reload    Reload the service unit and apply automount settings
  service   Enable, disable, or inspect zmnt.service
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
    if command in {"tui", "gui", "daemon", "auto", "service", "automount", "reload"}:
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


def _automation_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="zmnt auto", description="Manage TPM-backed automount settings")
    subparsers = parser.add_subparsers(dest="action", required=True)
    subparsers.add_parser("list", help="Show configured encryption roots")
    subparsers.add_parser("mount", help="Unlock and mount enabled entries now")
    configure = subparsers.add_parser("configure", help="Store a credential and configure automount")
    configure.add_argument("encryption_root")
    configure.add_argument("datasets", nargs="+")
    configure.add_argument("--backend", choices=("tpm2", "host+tpm2"), default="tpm2")
    configure.add_argument("--credential-dir")
    configure.add_argument("--no-automount", action="store_true")
    configure.add_argument("--keep-loaded-on-stop", action="store_true")
    clear = subparsers.add_parser("clear", help="Remove a stored credential")
    clear.add_argument("encryption_root")
    clear.add_argument("--unload", action="store_true", help="Also unmount and unload the active key")
    remove = subparsers.add_parser("remove", help="Remove an entire automation entry")
    remove.add_argument("encryption_root")
    toggle = subparsers.add_parser("enable", help="Enable automount for an entry")
    toggle.add_argument("encryption_root")
    toggle = subparsers.add_parser("disable", help="Disable automount for an entry")
    toggle.add_argument("encryption_root")
    unload = subparsers.add_parser("unload", help="Unmount configured datasets and unload the active key")
    unload.add_argument("encryption_root")
    return parser


def run_auto(arguments: list[str]) -> None:
    from .automation import AutomationManager
    from .zfs import ZFSError

    options = _automation_parser().parse_args(arguments)
    ensure_elevated_from_start(relaunch_args=["auto", *arguments])
    manager = AutomationManager()
    try:
        if options.action == "mount":
            failures = manager.start()
            if failures:
                raise ZFSError("\n".join(failures))
            print("Applied enabled automount entries.")
        elif options.action == "list":
            config = manager.load()
            print(json.dumps({
                "credential_dir": config.credential_dir,
                "entries": [entry.__dict__ for entry in config.entries],
            }, indent=2))
        elif options.action == "configure":
            secret = getpass.getpass(f"ZFS passphrase for {options.encryption_root}: ")
            manager.configure(
                options.encryption_root, options.datasets, secret, options.backend,
                not options.no_automount, not options.keep_loaded_on_stop,
                options.credential_dir,
            )
            print(f"Configured {options.encryption_root} ({options.backend}).")
        elif options.action == "clear":
            if options.unload:
                manager.clear_active_key(options.encryption_root)
            if not manager.remove_stored_credential(options.encryption_root):
                raise ZFSError(f"No automation entry for {options.encryption_root}")
            print(f"Removed stored credential for {options.encryption_root}.")
        elif options.action == "remove":
            if not manager.remove_entry(options.encryption_root):
                raise ZFSError(f"No automation entry for {options.encryption_root}")
            print(f"Removed automation entry for {options.encryption_root}.")
        elif options.action == "unload":
            manager.clear_active_key(options.encryption_root)
            print(f"Unmounted datasets and unloaded {options.encryption_root}.")
        else:
            manager.set_automount(options.encryption_root, options.action == "enable")
            print(f"Automount {'enabled' if options.action == 'enable' else 'disabled'} for {options.encryption_root}.")
    except ZFSError as error:
        print(f"{APP_NAME}: {error}", file=sys.stderr)
        raise SystemExit(1) from error


def run_daemon(arguments: list[str]) -> None:
    parser = argparse.ArgumentParser(prog="zmnt daemon")
    parser.add_argument("action", choices=("start", "stop"))
    options = parser.parse_args(arguments)
    if os.geteuid() != 0:
        print("zmnt: daemon actions must run as root", file=sys.stderr)
        raise SystemExit(1)
    from .automation import AutomationManager
    from .zfs import ZFSError
    try:
        failures = getattr(AutomationManager(), options.action)()
    except ZFSError as error:
        print(f"{APP_NAME}: {error}", file=sys.stderr)
        raise SystemExit(1) from error
    for failure in failures:
        print(f"{APP_NAME}: {failure}", file=sys.stderr)
    # A failed entry must not fail the start: systemd only runs ExecStop for units
    # that started, so the entries that did unlock would keep their keys loaded.
    if failures and options.action == "stop":
        raise SystemExit(1)


def run_service(arguments: list[str]) -> None:
    parser = argparse.ArgumentParser(prog="zmnt service")
    parser.add_argument("action", choices=("enable", "disable", "status", "reload"))
    options = parser.parse_args(arguments)
    if options.action != "status":
        ensure_elevated_from_start(relaunch_args=["service", *arguments])
    command = ["systemctl"]
    if options.action == "reload":
        result = subprocess.run(["systemctl", "daemon-reload"], check=False)
        if result.returncode:
            raise SystemExit(result.returncode)
        command.extend(["reload-or-restart", "zmnt.service"])
    elif options.action == "enable":
        command.extend(["enable", "--now", "zmnt.service"])
    elif options.action == "disable":
        command.extend(["disable", "--now", "zmnt.service"])
    else:
        command.extend(["status", "--no-pager", "zmnt.service"])
    raise SystemExit(subprocess.run(command, check=False).returncode)


def main() -> None:
    try:
        command, remaining = command_from_args(sys.argv[1:])
    except ValueError as error:
        print(f"{APP_NAME}: {error}\n\n{USAGE}", file=sys.stderr)
        raise SystemExit(2) from error
    if remaining and command not in {"daemon", "auto", "service"}:
        print(f"{APP_NAME}: command '{command}' does not accept arguments\n\n{USAGE}", file=sys.stderr)
        raise SystemExit(2)
    if command == "help":
        print(USAGE, end="")
        return
    if command == "version":
        print(VERSION)
        return
    if command == "auto":
        run_auto(remaining)
        return
    if command == "automount":
        run_auto(["mount"])
        return
    if command == "reload":
        run_service(["reload"])
        return
    if command == "daemon":
        run_daemon(remaining)
        return
    if command == "service":
        run_service(remaining)
        return
    if command == "tui":
        from .tui import main as tui_main

        tui_main()
        return
    run_gui()


if __name__ == "__main__":
    main()
