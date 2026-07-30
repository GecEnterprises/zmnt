"""Terminal interface for zmnt, using the shared ZFS command backend."""

from __future__ import annotations

import curses
import os
import sys
from collections.abc import Callable

from .__main__ import APP_NAME, ensure_elevated_from_start
from .zfs import DEFAULT_MOUNT_BASE, Dataset, Pool, ZFSError, ZFSService


class TUIApp:
    def __init__(self, screen: curses.window, service: ZFSService | None = None) -> None:
        self.screen = screen
        self.service = service or ZFSService()
        self.pools: list[Pool] = []
        self.pool_index = 0
        self.active_pool: Pool | None = None
        self.datasets: list[Dataset] = []
        self.dataset_index = 0
        self.selected: set[str] = set()
        self.altroot = ""
        self.force = False
        self.view = "pools"
        self.status = "Starting as root."

    def run(self) -> None:
        curses.curs_set(0)
        self.screen.keypad(True)
        self.refresh_pools()
        while True:
            self.render()
            key = self.screen.getch()
            if key in (ord("q"), 27):
                return
            try:
                if self.view == "pools":
                    self.handle_pool_key(key)
                else:
                    self.handle_dataset_key(key)
            except ZFSError as error:
                self.status = str(error)

    def handle_pool_key(self, key: int) -> None:
        if key in (curses.KEY_UP, ord("k")):
            self.pool_index = max(0, self.pool_index - 1)
        elif key in (curses.KEY_DOWN, ord("j")):
            self.pool_index = min(max(0, len(self.pools) - 1), self.pool_index + 1)
        elif key == ord("r"):
            self.refresh_pools()
        elif key == ord("a") and self.current_pool and self.current_pool.kind == "available":
            default = self.altroot or f"{DEFAULT_MOUNT_BASE}/{self.current_pool.name}"
            value = self.prompt("Import altroot", default)
            if value is not None:
                self.altroot = value
        elif key == ord("f") and self.current_pool and self.current_pool.kind == "available":
            self.force = not self.force
        elif key in (curses.KEY_ENTER, 10, 13):
            self.use_pool()

    def handle_dataset_key(self, key: int) -> None:
        if key in (curses.KEY_UP, ord("k")):
            self.dataset_index = max(0, self.dataset_index - 1)
        elif key in (curses.KEY_DOWN, ord("j")):
            self.dataset_index = min(max(0, len(self.datasets) - 1), self.dataset_index + 1)
        elif key == ord(" ") and self.current_dataset:
            self.toggle_selected(self.current_dataset)
        elif key == ord("r"):
            self.refresh_datasets()
        elif key == ord("b"):
            self.view = "pools"
        elif key == ord("u"):
            self.unlock_selected()
        elif key == ord("l"):
            self.lock_selected()
        elif key == ord("L"):
            self.lock_all()
        elif key == ord("m"):
            self.mount_selected()
        elif key == ord("o") and self.current_dataset:
            if self.current_dataset.actual_mount in {"", "none", "legacy"}:
                self.status = f"No normal mountpoint is available for {self.current_dataset.name}."
            else:
                self.service.open_mountpoint(self.current_dataset.actual_mount)
                self.status = f"Opened {self.current_dataset.actual_mount}."

    @property
    def current_pool(self) -> Pool | None:
        return self.pools[self.pool_index] if self.pools else None

    @property
    def current_dataset(self) -> Dataset | None:
        return self.datasets[self.dataset_index] if self.datasets else None

    def refresh_pools(self) -> None:
        self.pools = self.service.list_pools()
        self.pool_index = min(self.pool_index, max(0, len(self.pools) - 1))
        self.status = f"Found {len(self.pools)} pool entries."

    def use_pool(self) -> None:
        pool = self.current_pool
        if pool is None:
            self.status = "No pool is selected."
            return
        if pool.kind == "available":
            altroot = self.altroot or f"{DEFAULT_MOUNT_BASE}/{pool.name}"
            if not altroot.startswith("/"):
                self.status = "Import altroot must be an absolute path."
                return
            self.service.import_pool(pool.name, altroot, self.force)
            try:
                pool = self.service.find_imported_pool(pool.name)
            except ZFSError:
                pool = Pool(**{**pool.__dict__, "kind": "imported", "altroot": altroot})
        self.active_pool = pool
        self.refresh_datasets()
        self.view = "datasets"

    def refresh_datasets(self) -> None:
        if self.active_pool is None:
            self.status = "Choose and use a pool first."
            return
        self.datasets = self.service.list_datasets(self.active_pool)
        self.dataset_index = min(self.dataset_index, max(0, len(self.datasets) - 1))
        self.selected = {dataset.name for dataset in self.datasets if dataset.recommended}
        self.status = f"Loaded {len(self.datasets)} datasets from {self.active_pool.name}."

    def toggle_selected(self, dataset: Dataset) -> None:
        if dataset.name in self.selected:
            self.selected.remove(dataset.name)
        else:
            self.selected.add(dataset.name)

    def selected_datasets(self) -> list[Dataset]:
        return [dataset for dataset in self.datasets if dataset.name in self.selected]

    def unlock_selected(self) -> None:
        roots = self.unique_roots(self.selected_datasets(), loaded=False)
        if not roots:
            self.status = "Selected datasets do not have unloaded keys."
            return
        self.unlock_roots(roots, self.refresh_datasets)

    def lock_selected(self) -> None:
        roots = self.unique_roots(self.selected_datasets(), loaded=True)
        if not roots:
            self.status = "Selected datasets do not have loaded keys."
            return
        self.lock_roots(roots, f"Unload {len(roots)} loaded key(s) used by the selected datasets?")

    def lock_all(self) -> None:
        roots = self.unique_roots(self.datasets, loaded=True)
        if not roots:
            self.status = "No loaded encryption keys were found."
            return
        self.lock_roots(roots, f"Unload all {len(roots)} loaded encryption key(s)?")

    def lock_roots(self, roots: list[str], question: str) -> None:
        if not self.confirm(question):
            return
        self.service.unload_keys(roots)
        self.refresh_datasets()
        self.status = f"Unloaded {len(roots)} encryption key(s)."

    def mount_selected(self) -> None:
        targets = [dataset for dataset in self.selected_datasets() if dataset.mounted != "yes" and dataset.can_be_mounted]
        if not targets:
            self.status = "Nothing selected can be mounted."
            return
        roots = self.unique_roots(targets, loaded=False)
        self.unlock_roots(roots, lambda: self.mount_names([dataset.name for dataset in targets]))

    def mount_names(self, names: list[str]) -> None:
        self.service.mount_datasets(names)
        self.refresh_datasets()
        self.status = f"Mounted {len(names)} dataset(s)."

    def unlock_roots(self, roots: list[str], done: Callable[[], None]) -> None:
        for root in roots:
            passphrase = self.prompt(f"Passphrase for {root}", password=True)
            if passphrase is None:
                self.status = "Unlock cancelled."
                return
            if not passphrase:
                self.status = "No passphrase entered."
                return
            self.service.load_key(root, passphrase)
        done()
        self.status = f"Loaded {len(roots)} encryption key(s)."

    @staticmethod
    def unique_roots(datasets: list[Dataset], loaded: bool) -> list[str]:
        return sorted({dataset.encryption_root for dataset in datasets if (dataset.has_loaded_key if loaded else dataset.needs_key)})

    def render(self) -> None:
        self.screen.erase()
        height, width = self.screen.getmaxyx()
        self.add(0, 0, f"{APP_NAME}  |  {'Pools' if self.view == 'pools' else 'Datasets'}", curses.A_BOLD)
        if self.view == "pools":
            self.render_pools(height, width)
            help_text = "Up/Down or j/k: select  Enter: use pool  a: altroot  f: force  r: refresh  q: quit"
        else:
            self.render_datasets(height, width)
            help_text = "Up/Down or j/k: select  Space: toggle  u: unlock  l: lock  L: lock all  m: mount  o: open  b: pools  r: refresh  q: quit"
        self.add(height - 2, 0, self.status[:width - 1], curses.A_REVERSE)
        self.add(height - 1, 0, help_text[:width - 1], curses.A_DIM)
        self.screen.refresh()

    def render_pools(self, height: int, width: int) -> None:
        self.add(2, 0, "Pool", curses.A_UNDERLINE)
        if not self.pools:
            self.add(4, 0, "No imported or importable pools found.")
            return
        for index, pool in enumerate(self.pools[:height - 6]):
            marker = ">" if index == self.pool_index else " "
            detail = f"{pool.kind}, health {pool.health or pool.import_state}, free {pool.free or '-'}, altroot {pool.altroot or '-'}"
            self.add(4 + index, 0, f"{marker} {pool.name:<24} {detail}"[:width - 1], curses.A_REVERSE if index == self.pool_index else 0)
        pool = self.current_pool
        if pool and pool.kind == "available":
            self.add(height - 4, 0, f"Import altroot: {self.altroot or f'{DEFAULT_MOUNT_BASE}/{pool.name}'}  Force: {'yes' if self.force else 'no'}"[:width - 1])

    def render_datasets(self, height: int, width: int) -> None:
        if self.active_pool:
            self.add(2, 0, f"Pool: {self.active_pool.name}", curses.A_UNDERLINE)
        self.add(3, 0, "Sel Dataset                         Mountpoint                     Encryption / state", curses.A_BOLD)
        for index, dataset in enumerate(self.datasets[:height - 7]):
            marker = ">" if index == self.dataset_index else " "
            selected = "x" if dataset.name in self.selected else " "
            encryption = dataset.key_status if dataset.encryption_root != "-" else "unencrypted"
            state = f"{encryption}; mounted={dataset.mounted}; {dataset.reason}"
            line = f"{marker}[{selected}] {dataset.name:<30.30} {dataset.actual_mount:<30.30} {state}"
            self.add(5 + index, 0, line[:width - 1], curses.A_REVERSE if index == self.dataset_index else 0)
        if not self.datasets:
            self.add(5, 0, "No datasets loaded.")

    def prompt(self, label: str, initial: str = "", password: bool = False) -> str | None:
        curses.curs_set(1)
        value = list(initial)
        while True:
            self.screen.erase()
            height, width = self.screen.getmaxyx()
            shown = "*" * len(value) if password else "".join(value)
            self.add(height // 2 - 1, 2, label[:width - 4], curses.A_BOLD)
            self.add(height // 2, 2, shown[:width - 4])
            self.add(height // 2 + 2, 2, "Enter: confirm  Esc: cancel", curses.A_DIM)
            self.screen.move(height // 2, min(width - 3, 2 + len(shown)))
            self.screen.refresh()
            key = self.screen.getch()
            if key in (10, 13, curses.KEY_ENTER):
                curses.curs_set(0)
                return "".join(value)
            if key == 27:
                curses.curs_set(0)
                return None
            if key in (curses.KEY_BACKSPACE, 127, 8):
                if value:
                    value.pop()
            elif 32 <= key <= 126:
                value.append(chr(key))

    def confirm(self, question: str) -> bool:
        response = self.prompt(f"{question} [y/N]")
        return response is not None and response.lower() in {"y", "yes"}

    def add(self, y: int, x: int, text: str, attributes: int = 0) -> None:
        try:
            self.screen.addstr(y, x, text, attributes)
        except curses.error:
            pass


def main() -> None:
    try:
        ensure_elevated_from_start("zfs_mount_wizard.tui", relaunch_args=[])
    except RuntimeError as error:
        print(f"{APP_NAME}: {error}", file=sys.stderr)
        raise SystemExit(1) from error
    if not os.isatty(sys.stdin.fileno()) or not os.isatty(sys.stdout.fileno()):
        print(f"{APP_NAME}: zmnt-tui requires an interactive terminal.", file=sys.stderr)
        raise SystemExit(1)
    curses.wrapper(lambda screen: TUIApp(screen).run())


if __name__ == "__main__":
    main()
