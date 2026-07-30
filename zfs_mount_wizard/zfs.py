"""ZFS command handling shared by graphical and textual frontends."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os
import shutil
import subprocess


DEFAULT_MOUNT_BASE = "/mnt/zfs"


class ZFSError(RuntimeError):
    """A ZFS command could not be completed."""


@dataclass(frozen=True)
class Pool:
    id: str
    name: str
    kind: str
    health: str = ""
    size: str = ""
    free: str = ""
    altroot: str = ""
    import_state: str = ""
    import_id: str = ""
    status: str = ""


@dataclass(frozen=True)
class Dataset:
    name: str
    mounted: str
    mountpoint: str
    actual_mount: str
    encryption_root: str
    encryption: str
    key_status: str
    can_mount: str
    recommended: bool
    reason: str

    @property
    def needs_key(self) -> bool:
        return self.encryption_root != "-" and self.key_status == "unavailable"

    @property
    def has_loaded_key(self) -> bool:
        return self.encryption_root != "-" and self.key_status == "available"

    @property
    def can_be_mounted(self) -> bool:
        return self.mountpoint not in {"", "none", "legacy"} and self.can_mount != "off"


class ZFSService:
    """Runs ZFS utilities and converts their tabular output into application models."""

    def list_pools(self) -> list[Pool]:
        pools: list[Pool] = []
        output = self.run("zpool", "list", "-H", "-o", "name,health,size,free,altroot")
        for line in lines(output):
            parts = line.split("\t")
            if len(parts) < 5:
                continue
            name, health, size, free, altroot = parts[:5]
            pools.append(Pool(
                id=f"{name} - imported, health {health}, free {free}, altroot {altroot}",
                name=name,
                kind="imported",
                health=health,
                size=size,
                free=free,
                altroot=altroot,
            ))

        # "zpool import" returns a non-zero code when no pools are available.
        available_output, _ = self.try_run("zpool", "import")
        pools.extend(parse_importable_pools(available_output))
        return sorted(pools, key=lambda pool: (pool.name, pool.kind))

    def find_imported_pool(self, name: str) -> Pool:
        for pool in self.list_pools():
            if pool.name == name and pool.kind == "imported":
                return pool
        raise ZFSError("Imported pool not found after import")

    def import_pool(self, name: str, altroot: str, force: bool) -> None:
        args = ["import", "-N", "-R", altroot]
        if force:
            args.append("-f")
        self.run("zpool", *args, name)

    def list_datasets(self, pool: Pool) -> list[Dataset]:
        altroot = pool.altroot
        if pool.name:
            output, ok = self.try_run("zpool", "get", "-H", "-o", "value", "altroot", pool.name)
            if ok:
                altroot = output.strip()

        output = self.run(
            "zfs", "list", "-H", "-r", "-t", "filesystem",
            "-o", "name,mounted,mountpoint,encryptionroot,encryption,keystatus,canmount",
            pool.name,
        )
        datasets: list[Dataset] = []
        for line in lines(output):
            parts = line.split("\t")
            if len(parts) < 7:
                continue
            name, mounted, mountpoint, encryption_root, encryption, key_status, can_mount = parts[:7]
            dataset = Dataset(
                name=name,
                mounted=mounted,
                mountpoint=mountpoint,
                actual_mount=determine_mountpoint(altroot, mountpoint),
                encryption_root=encryption_root,
                encryption=encryption,
                key_status=key_status,
                can_mount=can_mount,
                recommended=False,
                reason="",
            )
            recommended, reason = recommendation(dataset, altroot)
            datasets.append(Dataset(**{**dataset.__dict__, "recommended": recommended, "reason": reason}))
        return datasets

    def load_key(self, root: str, passphrase: str) -> None:
        self.run("zfs", "load-key", root, input_text=f"{passphrase}\n")

    def unload_keys(self, roots: list[str]) -> None:
        failures = []
        for root in roots:
            if root and root != "-":
                try:
                    self.run("zfs", "unload-key", root)
                except ZFSError as error:
                    failures.append(f"{root}: {error}")
        if failures:
            raise ZFSError("\n".join(failures))

    def mount_datasets(self, names: list[str]) -> None:
        failures = []
        for name in names:
            if not name:
                continue
            mounted, found = self.try_run("zfs", "get", "-H", "-o", "value", "mounted", name)
            if found and mounted.strip() == "yes":
                continue
            try:
                self.run("zfs", "mount", name)
            except ZFSError as error:
                failures.append(f"{name}: {error}")
        if failures:
            raise ZFSError("\n".join(failures))

    def open_mountpoint(self, path: str) -> None:
        if not Path(path).is_dir():
            raise ZFSError(f"Mountpoint does not exist: {path}")
        for command, arguments in (
            ("dolphin", [path]), ("nautilus", [path]), ("nemo", [path]),
            ("thunar", [path]), ("xdg-open", [path]), ("gio", ["open", path]),
        ):
            executable = shutil.which(command)
            if executable:
                self._start_as_original_user(executable, arguments)
                return
        raise ZFSError("No supported file manager found")

    def run(self, command: str, *args: str, input_text: str | None = None) -> str:
        result = subprocess.run(
            [command, *args], input=input_text, text=True, capture_output=True, check=False,
        )
        if result.returncode:
            raise ZFSError(result.stderr.strip() or f"{command} exited with status {result.returncode}")
        return result.stdout

    def try_run(self, command: str, *args: str) -> tuple[str, bool]:
        try:
            return self.run(command, *args), True
        except ZFSError:
            return "", False

    @staticmethod
    def _start_as_original_user(command: str, arguments: list[str]) -> None:
        original_user = os.environ.get("ZFS_WIZARD_ORIG_USER")
        if os.geteuid() == 0 and original_user and original_user != "root" and shutil.which("run0"):
            args = [f"--user={original_user}"]
            for name in ("DISPLAY", "WAYLAND_DISPLAY", "XAUTHORITY", "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS"):
                if value := os.environ.get(name):
                    args.append(f"--setenv={name}={value}")
            subprocess.Popen(["run0", *args, command, *arguments])
            return
        subprocess.Popen([command, *arguments])


def parse_importable_pools(output: str) -> list[Pool]:
    pools: list[Pool] = []
    current: dict[str, str] | None = None

    def flush() -> None:
        if current is not None and current.get("name"):
            pools.append(Pool(
                id=f"{current['name']} - available, state {current.get('state', '')}, id {current.get('id', '')}",
                name=current["name"], kind="available", import_state=current.get("state", ""),
                import_id=current.get("id", ""), status=current.get("status", ""),
            ))

    for raw_line in output.splitlines():
        line = raw_line.strip()
        if line.startswith("pool:"):
            flush()
            current = {"name": line.removeprefix("pool:").strip()}
        elif current is not None:
            for key in ("id", "state", "status"):
                if line.startswith(f"{key}:"):
                    current[key] = line.removeprefix(f"{key}:").strip()
                    break
    flush()
    return pools


def determine_mountpoint(altroot: str, mountpoint: str) -> str:
    if mountpoint in {"", "none", "legacy"}:
        return mountpoint
    if altroot not in {"", "-"} and mountpoint.startswith("/"):
        return altroot if mountpoint == "/" else str(Path(altroot) / mountpoint.lstrip("/"))
    return mountpoint


def recommendation(dataset: Dataset, altroot: str) -> tuple[bool, str]:
    if dataset.mounted == "yes":
        return False, "already mounted"
    if dataset.mountpoint in {"none", "legacy"}:
        return False, f"mountpoint={dataset.mountpoint}"
    if dataset.key_status == "unavailable":
        return False, "locked"
    if dataset.can_mount == "off":
        return False, "canmount=off"
    if dataset.can_mount == "noauto":
        return False, "canmount=noauto"
    if altroot == "-" and is_critical_mountpoint(dataset.mountpoint):
        return False, "system path"
    return True, "recommended"


def is_critical_mountpoint(path: str) -> bool:
    roots = {"/", "/bin", "/boot", "/dev", "/etc", "/home", "/lib", "/lib64", "/opt", "/proc", "/root", "/run", "/sbin", "/sys", "/usr", "/var"}
    return path in roots or path.startswith(("/usr/", "/var/", "/etc/"))


def lines(output: str) -> list[str]:
    return [line.rstrip("\r") for line in output.strip().splitlines() if line]
