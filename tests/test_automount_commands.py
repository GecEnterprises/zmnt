import unittest
from unittest.mock import Mock, patch

from zfs_mount_wizard import __main__ as cli
from zfs_mount_wizard.automation import AutomountConfig
from zfs_mount_wizard.tui import TUIApp
from zfs_mount_wizard.zfs import Dataset


class AutomountCommandTests(unittest.TestCase):
    def test_automount_dispatch_applies_saved_entries(self):
        with patch.object(cli.sys, "argv", ["zmnt", "automount"]), \
             patch.object(cli, "ensure_elevated_from_start"), \
             patch("zfs_mount_wizard.automation.AutomationManager") as manager:
            manager.return_value.start.return_value = []
            cli.main()
        manager.return_value.start.assert_called_once_with()
        manager.return_value.stop.assert_not_called()

    def test_reload_refreshes_unit_then_uses_reload_action(self):
        with patch.object(cli, "ensure_elevated_from_start"), \
             patch.object(cli.subprocess, "run", return_value=Mock(returncode=0)) as run:
            with self.assertRaises(SystemExit) as exit:
                cli.run_service(["reload"])
        self.assertEqual(exit.exception.code, 0)
        self.assertEqual([call.args[0] for call in run.call_args_list], [
            ["systemctl", "daemon-reload"],
            ["systemctl", "reload-or-restart", "zmnt.service"],
        ])

    def test_reload_stops_when_unit_reload_fails(self):
        with patch.object(cli, "ensure_elevated_from_start"), \
             patch.object(cli.subprocess, "run", return_value=Mock(returncode=1)) as run:
            with self.assertRaises(SystemExit) as exit:
                cli.run_service(["reload"])
        self.assertEqual(exit.exception.code, 1)
        self.assertEqual(run.call_count, 1)

    def make_app(self):
        app = TUIApp(Mock(), Mock())
        app.automation = Mock()
        app.automation.load.return_value = AutomountConfig()
        app.datasets = [Dataset(
            name, "no", "/data", "/data", "tank/key", "aes-256-gcm",
            "unavailable", "on", False, "",
        ) for name in ["tank/key/a", "tank/key/b"]]
        app.selected = {d.name for d in app.datasets}
        return app

    def test_tui_groups_selected_datasets_under_one_credential(self):
        app = self.make_app()
        app.prompt = Mock(side_effect=["c", "tpm2", "secret"])
        app.confirm = Mock(return_value=True)
        app.handle_dataset_key(ord("a"))
        app.automation.configure.assert_called_once_with(
            "tank/key", ["tank/key/a", "tank/key/b"], "secret", "tpm2",
            automount=True, clear_on_stop=True,
        )

    def test_tui_cancel_does_not_save_credentials(self):
        app = self.make_app()
        app.prompt = Mock(side_effect=["c", "tpm2", None])
        app.configure_automount()
        app.automation.configure.assert_not_called()

    def test_tui_disables_selected_root_once(self):
        app = self.make_app()
        app.prompt = Mock(return_value="d")
        app.configure_automount()
        app.automation.set_automount.assert_called_once_with("tank/key", False)
