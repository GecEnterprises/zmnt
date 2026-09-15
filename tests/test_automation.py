from __future__ import annotations

import json
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from zfs_mount_wizard.automation import (
    AutomationManager, AutomountConfig, AutomountEntry, credential_name,
)
from zfs_mount_wizard.zfs import ZFSError


class FakeService:
    def __init__(
        self, loaded: bool = False, imported: set[str] | None = None,
        altroot: str | None = None, import_error: str = "",
    ) -> None:
        self.loaded = loaded
        self.imported = {"tank"} if imported is None else imported
        self.altroot = altroot
        self.import_error = import_error
        self.calls: list[tuple[str, object]] = []

    def pool_is_imported(self, name: str) -> bool:
        return name in self.imported

    def pool_altroot(self, name: str) -> str | None:
        return self.altroot if name in self.imported else None

    def import_pool(self, name: str, altroot: str, force: bool) -> None:
        self.calls.append(("import", (name, altroot, force)))
        if self.import_error:
            raise ZFSError(self.import_error)
        self.imported.add(name)

    def key_is_loaded(self, root: str) -> bool:
        self.calls.append(("key_is_loaded", root))
        return self.loaded

    def mount_datasets(self, datasets: list[str]) -> None:
        self.calls.append(("mount", datasets))

    def unmount_datasets(self, datasets: list[str]) -> None:
        self.calls.append(("unmount", datasets))

    def unload_keys(self, roots: list[str]) -> None:
        self.calls.append(("unload", roots))


class AutomationTests(unittest.TestCase):
    def test_credential_is_read_from_pipe_regardless_of_keylocation(self) -> None:
        decrypt = Mock(stdout=io.BytesIO(b"secret\n"), stderr=io.BytesIO())
        decrypt.wait.return_value = 0
        with patch("zfs_mount_wizard.automation.subprocess.Popen", return_value=decrypt), \
             patch("zfs_mount_wizard.automation.subprocess.run", return_value=Mock(returncode=0)) as run:
            AutomationManager()._load_credential(
                AutomountConfig(), AutomountEntry("tank/secure", credential="key.cred"),
            )
        self.assertEqual(run.call_args.args[0], ["zfs", "load-key", "-L", "prompt", "tank/secure"])
        self.assertIs(run.call_args.kwargs["stdin"], decrypt.stdout)
        self.assertTrue(decrypt.stdout.closed)

    def test_decrypt_failure_preserves_zfs_failure(self) -> None:
        decrypt = Mock(stdout=io.BytesIO(), stderr=io.BytesIO())
        decrypt.wait.return_value = -13
        with patch("zfs_mount_wizard.automation.subprocess.Popen", return_value=decrypt), \
             patch("zfs_mount_wizard.automation.subprocess.run", return_value=Mock(
                 returncode=1, stderr=b"dataset does not exist",
             )):
            with self.assertRaisesRegex(ZFSError, "status -13; zfs load-key: dataset does not exist"):
                AutomationManager()._load_credential(
                    AutomountConfig(), AutomountEntry("tank/missing", credential="key.cred"),
                )

    def test_credential_names_do_not_expose_dataset_names(self) -> None:
        name = credential_name("tank/private/home")
        self.assertTrue(name.startswith("zfs-"))
        self.assertNotIn("private", name)
        self.assertEqual(name, credential_name("tank/private/home"))

    def test_configuration_round_trip_and_clear(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            config_path = base / "config.json"
            credential_dir = base / "credentials"
            manager = AutomationManager(config_path, FakeService(altroot="/mnt/zfs/tank"))

            def fake_encrypt(command, **kwargs):
                Path(command[-1]).write_bytes(b"encrypted")
                return type("Result", (), {"returncode": 0, "stderr": b""})()

            with patch("zfs_mount_wizard.automation.subprocess.run", side_effect=fake_encrypt):
                entry = manager.configure(
                    "tank/secure", ["tank/secure/home", "tank/secure"], "secret",
                    credential_dir=str(credential_dir),
                )

            loaded = manager.load()
            self.assertEqual(loaded.entries, [entry])
            self.assertEqual(entry.altroot, "/mnt/zfs/tank")
            self.assertTrue((credential_dir / entry.credential).exists())
            self.assertEqual(config_path.stat().st_mode & 0o777, 0o600)

            self.assertTrue(manager.remove_stored_credential("tank/secure"))
            loaded = manager.load()
            self.assertFalse(loaded.entries[0].automount)
            self.assertEqual(loaded.entries[0].credential, "")
            self.assertFalse((credential_dir / entry.credential).exists())

    def test_start_skips_decryption_when_key_is_loaded(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config_path = Path(temporary) / "config.json"
            config_path.write_text(json.dumps({
                "credential_dir": temporary,
                "entries": [{
                    "encryption_root": "tank/secure", "datasets": ["tank/secure"],
                    "automount": True, "credential": "key.cred", "backend": "tpm2",
                    "clear_on_stop": True,
                }],
            }))
            service = FakeService(loaded=True)
            manager = AutomationManager(config_path, service)  # type: ignore[arg-type]
            with patch.object(manager, "_load_credential") as load:
                manager.start()
            load.assert_not_called()
            self.assertIn(("mount", ["tank/secure"]), service.calls)

    def make_manager(self, directory: str, service: FakeService, entries: list[AutomountEntry]) -> AutomationManager:
        manager = AutomationManager(Path(directory) / "config.json", service)  # type: ignore[arg-type]
        manager.save(AutomountConfig(directory, entries))
        return manager

    def test_start_imports_missing_pool_with_recorded_altroot(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            service = FakeService(imported=set())
            manager = self.make_manager(temporary, service, [AutomountEntry(
                "tank/secure", ["tank/secure"], credential="key.cred", altroot="/mnt/zfs/tank",
            )])
            with patch.object(manager, "_load_credential") as load:
                self.assertEqual(manager.start(), [])
            load.assert_called_once()
            self.assertEqual(service.calls[0], ("import", ("tank", "/mnt/zfs/tank", False)))
            self.assertIn(("mount", ["tank/secure"]), service.calls)

    def test_start_imports_legacy_entry_under_default_mount_base(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            service = FakeService(imported=set())
            manager = self.make_manager(temporary, service, [AutomountEntry(
                "tank/secure", ["tank/secure"], credential="key.cred",
            )])
            with patch.object(manager, "_load_credential"):
                manager.start()
            self.assertEqual(service.calls[0], ("import", ("tank", "/mnt/zfs/tank", False)))

    def test_start_continues_after_failures(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            service = FakeService(imported={"good"}, import_error="no such pool")
            manager = self.make_manager(temporary, service, [
                AutomountEntry("good/a", ["good/a"], credential="a.cred"),
                AutomountEntry("good/b", ["good/b"], credential="b.cred"),
                AutomountEntry("missing/c", ["missing/c"], credential="c.cred"),
                AutomountEntry("missing/d", ["missing/d"], credential="d.cred"),
            ])

            def load(config, entry):
                if entry.encryption_root == "good/a":
                    raise FileNotFoundError("systemd-creds")

            with patch.object(manager, "_load_credential", side_effect=load):
                failures = manager.start()
            self.assertEqual(len(failures), 3)
            self.assertIn("good/a: systemd-creds", failures)
            self.assertIn("missing/d: pool missing is not imported: no such pool", failures)
            self.assertIn(("mount", ["good/b"]), service.calls)
            # A pool that failed to import is only attempted once.
            self.assertEqual(sum(call[0] == "import" for call in service.calls), 1)

    def test_daemon_start_succeeds_with_entry_failures(self) -> None:
        from zfs_mount_wizard import __main__ as cli

        with patch.object(cli.os, "geteuid", return_value=0), \
             patch("zfs_mount_wizard.automation.AutomationManager") as manager, \
             patch.object(cli.sys, "stderr", io.StringIO()) as stderr:
            manager.return_value.start.return_value = ["tank/secure: locked"]
            cli.run_daemon(["start"])
            manager.return_value.stop.return_value = ["tank/secure: busy"]
            with self.assertRaises(SystemExit):
                cli.run_daemon(["stop"])
        self.assertIn("zmnt: tank/secure: locked", stderr.getvalue())

    def test_stop_unmounts_children_first_then_unloads(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            manager = AutomationManager(Path(temporary) / "config.json", FakeService())
            manager.save(AutomountConfig(temporary, [AutomountEntry(
                "tank/secure", ["tank/secure", "tank/secure/home"], True,
                "key.cred", "tpm2", True,
            )]))
            manager.stop()
            self.assertEqual(manager.service.calls, [
                ("unmount", ["tank/secure/home", "tank/secure"]),
                ("unload", ["tank/secure"]),
            ])


if __name__ == "__main__":
    unittest.main()
