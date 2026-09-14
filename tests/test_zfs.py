from __future__ import annotations

import unittest

from zfs_mount_wizard.zfs import (
    Dataset,
    determine_mountpoint,
    parse_importable_pools,
    recommendation,
)
from zfs_mount_wizard.tui import TUIApp
from zfs_mount_wizard.__main__ import command_from_args


class ZFSParsingTests(unittest.TestCase):
    def test_parse_importable_pools(self) -> None:
        output = """\
   pool: backup
     id: 928374
  state: ONLINE
 status: The pool can be imported.
   pool: old-system
     id: 102938
  state: UNAVAIL
"""
        pools = parse_importable_pools(output)

        self.assertEqual([pool.name for pool in pools], ["backup", "old-system"])
        self.assertEqual(pools[0].import_id, "928374")
        self.assertEqual(pools[0].kind, "available")

    def test_altroot_mountpoint_is_determined(self) -> None:
        self.assertEqual(determine_mountpoint("/mnt/zfs/tank", "/"), "/mnt/zfs/tank")
        self.assertEqual(determine_mountpoint("/mnt/zfs/tank", "/home"), "/mnt/zfs/tank/home")
        self.assertEqual(determine_mountpoint("/mnt/zfs/tank", "legacy"), "legacy")

    def test_recommendation_excludes_locked_and_system_datasets(self) -> None:
        locked = Dataset("tank/secret", "no", "/secret", "/secret", "tank/secret", "aes-256-gcm", "unavailable", "on", False, "")
        system = Dataset("tank/root", "no", "/", "/", "-", "off", "-", "on", False, "")

        self.assertEqual(recommendation(locked, "/mnt/zfs/tank"), (False, "locked"))
        self.assertEqual(recommendation(system, "-"), (False, "system path"))

    def test_tui_deduplicates_encryption_roots(self) -> None:
        first = Dataset("tank/a", "no", "/a", "/a", "tank/key", "aes-256-gcm", "unavailable", "on", False, "")
        second = Dataset("tank/b", "no", "/b", "/b", "tank/key", "aes-256-gcm", "unavailable", "on", False, "")

        self.assertEqual(TUIApp.unique_roots([first, second], loaded=False), ["tank/key"])

    def test_capital_u_unlocks_and_mounts(self) -> None:
        app = TUIApp.__new__(TUIApp)
        called = []
        app.mount_selected = lambda: called.append("unlock+mount")

        app.handle_dataset_key(ord("U"))

        self.assertEqual(called, ["unlock+mount"])

    def test_lowercase_u_uses_optional_mount_flow(self) -> None:
        app = TUIApp.__new__(TUIApp)
        called = []
        app.unlock_selected = lambda: called.append("unlock")

        app.handle_dataset_key(ord("u"))

        self.assertEqual(called, ["unlock"])

    def test_command_dispatch_defaults_to_tui(self) -> None:
        self.assertEqual(command_from_args([]), ("tui", []))
        self.assertEqual(command_from_args(["gui"]), ("gui", []))
        self.assertEqual(command_from_args(["--version"]), ("version", []))
        with self.assertRaises(ValueError):
            command_from_args(["unknown"])


if __name__ == "__main__":
    unittest.main()
