"""PySide6 graphical interface for ZFS Mount Wizard."""

from __future__ import annotations

from collections.abc import Callable
import subprocess

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QFormLayout, QGridLayout, QGroupBox, QHBoxLayout,
    QHeaderView, QInputDialog, QLabel, QLineEdit, QMainWindow, QMessageBox,
    QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from .zfs import DEFAULT_MOUNT_BASE, Dataset, Pool, ZFSError, ZFSService
from .automation import AutomationManager, DEFAULT_CREDENTIAL_DIR


class WizardWindow(QMainWindow):
    def __init__(self, service: ZFSService | None = None) -> None:
        super().__init__()
        self.service = service or ZFSService()
        self.automation = AutomationManager(service=self.service)
        self.pools: dict[str, Pool] = {}
        self.active_pool: Pool | None = None
        self.datasets: list[Dataset] = []
        self.selected: set[str] = set()
        self.setWindowTitle("zmnt")
        self.resize(1120, 720)
        self._build_ui()
        self._load_automation_settings()
        self.refresh_pools()

    def _build_ui(self) -> None:
        root = QWidget()
        layout = QVBoxLayout(root)
        title = QLabel("zmnt")
        title.setStyleSheet("font-size: 22px; font-weight: 600;")
        layout.addWidget(title)
        subtitle = QLabel("Import pools, unlock or relock selected encrypted roots, mount datasets, and open their mountpoints.")
        subtitle.setWordWrap(True)
        layout.addWidget(subtitle)

        pool_box = QGroupBox("Pool")
        form = QFormLayout(pool_box)
        self.pool_picker = QComboBox()
        self.pool_picker.currentIndexChanged.connect(self._on_pool_selected)
        self.altroot = QLineEdit()
        self.altroot.setPlaceholderText(f"{DEFAULT_MOUNT_BASE}/pool")
        self.force = QCheckBox("Force import")
        form.addRow("Pool", self.pool_picker)
        form.addRow("Import altroot", self.altroot)
        actions = QHBoxLayout()
        refresh = QPushButton("Refresh Pools")
        refresh.clicked.connect(self.refresh_pools)
        use_pool = QPushButton("Use Pool")
        use_pool.clicked.connect(self.use_selected_pool)
        actions.addWidget(refresh)
        actions.addWidget(use_pool)
        actions.addWidget(self.force)
        actions.addStretch()
        form.addRow(actions)
        layout.addWidget(pool_box)

        automation_box = QGroupBox("Boot automount and key storage")
        automation_form = QFormLayout(automation_box)
        self.credential_backend = QComboBox()
        self.credential_backend.addItems(["tpm2", "host+tpm2"])
        self.credential_directory = QLineEdit(str(DEFAULT_CREDENTIAL_DIR))
        self.automount_enabled = QCheckBox("Mount selected datasets at boot")
        self.automount_enabled.setChecked(True)
        self.clear_on_stop = QCheckBox("Unmount and unload keys when the service stops")
        self.clear_on_stop.setChecked(True)
        automation_form.addRow("Key protection", self.credential_backend)
        automation_form.addRow("Encrypted credential directory", self.credential_directory)
        automation_form.addRow(self.automount_enabled)
        automation_form.addRow(self.clear_on_stop)
        automation_actions = QHBoxLayout()
        for label, callback in (
            ("Store Keys for Selected", self.store_selected_keys),
            ("Clear Stored Keys", self.clear_selected_stored_keys),
            ("Unload Active Keys", self.unload_selected_active_keys),
            ("Enable Daemon", lambda: self.set_daemon_enabled(True)),
            ("Disable Daemon", lambda: self.set_daemon_enabled(False)),
        ):
            button = QPushButton(label)
            button.clicked.connect(callback)
            automation_actions.addWidget(button)
        automation_actions.addStretch()
        automation_form.addRow(automation_actions)
        layout.addWidget(automation_box)

        layout.addWidget(QLabel("Datasets"))
        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(["Mount", "Dataset", "Determined mountpoint", "Encryption", "State", "Actions"])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(5, QHeaderView.ResizeMode.ResizeToContents)
        layout.addWidget(self.table, 1)

        toolbar = QHBoxLayout()
        for label, callback in (
            ("Mount Selected", self.mount_selected), ("Unlock Selected", self.unlock_selected),
            ("Lock Selected", self.lock_selected), ("Lock All", self.lock_all),
            ("Refresh Datasets", self.refresh_datasets),
        ):
            button = QPushButton(label)
            button.clicked.connect(callback)
            toolbar.addWidget(button)
        toolbar.addStretch()
        self.status = QLabel("Starting as root.")
        self.status.setWordWrap(True)
        toolbar.addWidget(self.status)
        layout.addLayout(toolbar)
        self.setCentralWidget(root)

    def _load_automation_settings(self) -> None:
        try:
            config = self.automation.load()
        except ZFSError as error:
            self.status.setText(str(error))
            return
        self.credential_directory.setText(config.credential_dir)
        if config.entries:
            entry = config.entries[0]
            index = self.credential_backend.findText(entry.backend)
            if index >= 0:
                self.credential_backend.setCurrentIndex(index)
            self.automount_enabled.setChecked(entry.automount)
            self.clear_on_stop.setChecked(entry.clear_on_stop)

    def refresh_pools(self) -> None:
        try:
            pools = self.service.list_pools()
        except ZFSError as error:
            self._error("Could not list pools", error)
            return
        previous = self.pool_picker.currentData()
        self.pools = {pool.id: pool for pool in pools}
        self.pool_picker.blockSignals(True)
        self.pool_picker.clear()
        for pool in pools:
            self.pool_picker.addItem(pool.id, pool.id)
        self.pool_picker.blockSignals(False)
        self.status.setText(f"Found {len(pools)} pool entries.")
        if not pools:
            self.active_pool = None
            self.datasets = []
            self._render_datasets()
            return
        index = self.pool_picker.findData(previous)
        self.pool_picker.setCurrentIndex(index if index >= 0 else 0)
        self._on_pool_selected()

    def _on_pool_selected(self, _index: int | None = None) -> None:
        pool = self.pools.get(self.pool_picker.currentData())
        if pool is None:
            return
        if pool.kind == "available":
            default = f"{DEFAULT_MOUNT_BASE}/{pool.name}"
            if not self.altroot.text().strip() or self.altroot.text().startswith(f"{DEFAULT_MOUNT_BASE}/"):
                self.altroot.setText(default)
            self.altroot.setEnabled(True)
            self.force.setEnabled(True)
            self.status.setText("Pool is available to import. Choose an altroot, then use the pool.")
        else:
            self.altroot.setText(pool.altroot)
            self.altroot.setEnabled(False)
            self.force.setEnabled(False)
            self.status.setText("Pool is already imported. Use the pool to inspect datasets.")

    def use_selected_pool(self) -> None:
        pool = self.pools.get(self.pool_picker.currentData())
        if pool is None:
            self._message("Choose a pool first.")
            return
        try:
            if pool.kind == "available":
                altroot = self.altroot.text().strip()
                if not altroot.startswith("/"):
                    self._message("Import altroot must be an absolute path.")
                    return
                self.service.import_pool(pool.name, altroot, self.force.isChecked())
                try:
                    pool = self.service.find_imported_pool(pool.name)
                except ZFSError:
                    pool = Pool(**{**pool.__dict__, "kind": "imported", "altroot": altroot})
                self.status.setText(f"Imported {pool.name}.")
            self.active_pool = pool
            self.refresh_datasets()
        except ZFSError as error:
            self._error("Import failed", error)

    def refresh_datasets(self) -> None:
        if self.active_pool is None:
            self._message("Choose and use a pool first.")
            return
        try:
            self.datasets = self.service.list_datasets(self.active_pool)
        except ZFSError as error:
            self._error("Could not list datasets", error)
            return
        self.selected = {dataset.name for dataset in self.datasets if dataset.recommended}
        self._render_datasets()
        self.status.setText(f"Loaded {len(self.datasets)} datasets from {self.active_pool.name}.")

    def _render_datasets(self) -> None:
        self.table.setRowCount(len(self.datasets))
        for row, dataset in enumerate(self.datasets):
            check = QCheckBox()
            check.setChecked(dataset.name in self.selected)
            check.toggled.connect(lambda checked, name=dataset.name: self._set_selected(name, checked))
            cell = QWidget()
            cell_layout = QHBoxLayout(cell)
            cell_layout.setContentsMargins(0, 0, 0, 0)
            cell_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
            cell_layout.addWidget(check)
            self.table.setCellWidget(row, 0, cell)
            self._item(row, 1, dataset.name)
            self._item(row, 2, dataset.actual_mount)
            encryption = dataset.encryption
            if dataset.encryption_root != "-":
                encryption = f"{encryption}, root {dataset.encryption_root}, key {dataset.key_status}"
            self._item(row, 3, encryption)
            self._item(row, 4, f"mounted={dataset.mounted}, canmount={dataset.can_mount}, {dataset.reason}")
            self.table.setCellWidget(row, 5, self._actions_for(dataset))

    def _item(self, row: int, column: int, text: str) -> None:
        item = QTableWidgetItem(text)
        item.setToolTip(text)
        self.table.setItem(row, column, item)

    def _actions_for(self, dataset: Dataset) -> QWidget:
        widget = QWidget()
        layout = QHBoxLayout(widget)
        layout.setContentsMargins(0, 0, 0, 0)
        for label, handler, enabled in (
            ("Unlock", lambda: self.unlock_dataset(dataset), dataset.needs_key),
            ("Lock", lambda: self.lock_roots([dataset.encryption_root], "Lock Dataset", f"Unload the key for {dataset.encryption_root}?"), dataset.has_loaded_key),
            ("Mount", lambda: self.mount_datasets([dataset]), dataset.mounted != "yes" and dataset.can_be_mounted),
            ("Open", lambda: self.open_mountpoint(dataset), dataset.mounted == "yes"),
        ):
            button = QPushButton(label)
            button.setEnabled(enabled)
            button.clicked.connect(handler)
            layout.addWidget(button)
        return widget

    def _set_selected(self, name: str, selected: bool) -> None:
        if selected:
            self.selected.add(name)
        else:
            self.selected.discard(name)

    def selected_datasets(self) -> list[Dataset]:
        return [dataset for dataset in self.datasets if dataset.name in self.selected]

    def unlock_dataset(self, dataset: Dataset) -> None:
        if dataset.needs_key:
            self.unlock_roots([dataset.encryption_root])

    def unlock_selected(self) -> None:
        roots = self._unique_roots(self.selected_datasets(), loaded=False)
        if not roots:
            self._message("Selected datasets do not have unloaded keys.")
            return
        self.unlock_roots(roots)

    def unlock_roots(self, roots: list[str], done: Callable[[], None] | None = None) -> None:
        for root in roots:
            passphrase, accepted = QInputDialog.getText(self, "Unlock Dataset", f"ZFS encryption passphrase for {root}:", QLineEdit.EchoMode.Password)
            if not accepted:
                return
            if not passphrase:
                self._message("No passphrase entered.")
                return
            try:
                self.service.load_key(root, passphrase)
            except ZFSError as error:
                self._error("Unlock failed", error)
                return
        if done:
            done()
        else:
            self.refresh_datasets()
            self.status.setText(f"Loaded {len(roots)} encryption key(s).")

    def lock_selected(self) -> None:
        roots = self._unique_roots(self.selected_datasets(), loaded=True)
        if not roots:
            self._message("Selected datasets do not have loaded keys.")
            return
        self.lock_roots(roots, "Lock Selected", f"Unload {len(roots)} loaded key(s) used by the selected datasets?")

    def lock_all(self) -> None:
        if self.active_pool is None:
            self._message("Choose and use a pool first.")
            return
        roots = self._unique_roots(self.datasets, loaded=True)
        if not roots:
            self._message(f"No loaded encryption keys were found for {self.active_pool.name}.")
            return
        self.lock_roots(roots, "Lock All", f"Unload all {len(roots)} loaded encryption key(s) for pool {self.active_pool.name}?\n\nZFS may refuse while encrypted datasets are still mounted.")

    def lock_roots(self, roots: list[str], title: str, message: str) -> None:
        if QMessageBox.question(self, title, message) != QMessageBox.StandardButton.Yes:
            return
        try:
            self.service.unload_keys(roots)
        except ZFSError as error:
            self.refresh_datasets()
            self._error("Lock failed", error)
            return
        self.refresh_datasets()
        self.status.setText(f"Unloaded {len(roots)} encryption key(s).")

    def mount_selected(self) -> None:
        targets = self.selected_datasets()
        if not targets:
            self._message("Select one or more datasets to mount.")
            return
        self.mount_datasets(targets)

    def mount_datasets(self, targets: list[Dataset]) -> None:
        mountable = [dataset for dataset in targets if dataset.mounted != "yes" and dataset.can_be_mounted]
        if not mountable:
            self._message("Nothing selected can be mounted.")
            return
        roots = self._unique_roots(mountable, loaded=False)
        if roots:
            self.unlock_roots(roots, lambda: self._mount_names([dataset.name for dataset in mountable]))
        else:
            self._mount_names([dataset.name for dataset in mountable])

    def _mount_names(self, names: list[str]) -> None:
        try:
            self.service.mount_datasets(names)
        except ZFSError as error:
            self.refresh_datasets()
            self._error("Mount failed", error)
            return
        self.refresh_datasets()
        self.status.setText(f"Mounted {len(names)} dataset(s).")

    def open_mountpoint(self, dataset: Dataset) -> None:
        if dataset.actual_mount in {"", "none", "legacy"}:
            self._message(f"No normal mountpoint is available for {dataset.name}.")
            return
        try:
            self.service.open_mountpoint(dataset.actual_mount)
        except ZFSError as error:
            self._error("Could not open file manager", error)

    def store_selected_keys(self) -> None:
        selected = self.selected_datasets()
        roots = sorted({item.encryption_root for item in selected if item.encryption_root != "-"})
        if not roots:
            self._message("Select one or more encrypted datasets first.")
            return
        for root in roots:
            secret, accepted = QInputDialog.getText(
                self, "Store TPM2 Credential", f"ZFS passphrase for {root}:",
                QLineEdit.EchoMode.Password,
            )
            if not accepted:
                return
            datasets = [item.name for item in selected if item.encryption_root == root]
            try:
                self.automation.configure(
                    root, datasets, secret, self.credential_backend.currentText(),
                    self.automount_enabled.isChecked(), self.clear_on_stop.isChecked(),
                    self.credential_directory.text().strip(),
                )
            except (ZFSError, OSError) as error:
                self._error("Could not store credential", error)
                return
        self.status.setText(f"Stored {len(roots)} encrypted credential(s). Enable the daemon to use them at boot.")

    def clear_selected_stored_keys(self) -> None:
        roots = sorted({item.encryption_root for item in self.selected_datasets() if item.encryption_root != "-"})
        if not roots:
            self._message("Select one or more encrypted datasets first.")
            return
        if QMessageBox.question(
            self, "Clear Stored Keys",
            f"Remove {len(roots)} encrypted credential(s) and disable their automount entries?",
        ) != QMessageBox.StandardButton.Yes:
            return
        try:
            removed = sum(self.automation.remove_stored_credential(root) for root in roots)
        except (ZFSError, OSError) as error:
            self._error("Could not clear stored credentials", error)
            return
        self.status.setText(f"Removed {removed} stored credential(s). Active ZFS keys were not changed.")

    def unload_selected_active_keys(self) -> None:
        roots = sorted({item.encryption_root for item in self.selected_datasets() if item.encryption_root != "-"})
        if not roots:
            self._message("Select one or more encrypted datasets first.")
            return
        if QMessageBox.question(
            self, "Unload Active Keys",
            "Unmount configured datasets and unload the selected active ZFS keys?",
        ) != QMessageBox.StandardButton.Yes:
            return
        try:
            for root in roots:
                self.automation.clear_active_key(root)
        except ZFSError as error:
            self._error("Could not unload key", error)
            return
        self.refresh_datasets()
        self.status.setText(f"Unloaded {len(roots)} active key(s). Stored credentials were not changed.")

    def set_daemon_enabled(self, enabled: bool) -> None:
        arguments = ["systemctl", "enable" if enabled else "disable", "--now", "zmnt.service"]
        result = subprocess.run(arguments, text=True, capture_output=True, check=False)
        if result.returncode:
            self._error("Could not update daemon", RuntimeError(result.stderr.strip() or result.stdout.strip()))
            return
        self.status.setText(f"zmnt daemon {'enabled and started' if enabled else 'disabled and stopped'}.")

    @staticmethod
    def _unique_roots(datasets: list[Dataset], loaded: bool) -> list[str]:
        return sorted({dataset.encryption_root for dataset in datasets if (dataset.has_loaded_key if loaded else dataset.needs_key)})

    def _error(self, title: str, error: Exception) -> None:
        QMessageBox.critical(self, title, str(error))

    def _message(self, message: str) -> None:
        QMessageBox.information(self, "zmnt", message)
