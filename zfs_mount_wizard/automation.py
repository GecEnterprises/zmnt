"""Persistent automount configuration and TPM2 credential handling."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile

from .zfs import DEFAULT_MOUNT_BASE, ZFSError, ZFSService


DEFAULT_CONFIG = Path("/etc/zmnt/config.json")
DEFAULT_CREDENTIAL_DIR = Path("/etc/credstore.encrypted/zmnt")
VALID_BACKENDS = {"tpm2", "host+tpm2"}
# Failures that affect a single entry; the daemon reports them and continues.
ENTRY_ERRORS = (ZFSError, OSError, ValueError)


@dataclass
class AutomountEntry:
    encryption_root: str
    datasets: list[str] = field(default_factory=list)
    automount: bool = True
    credential: str = ""
    backend: str = "tpm2"
    clear_on_stop: bool = True
    # Pool altroot used when zmnt has to import the pool itself: "" for none,
    # None when unknown (entries saved before this was recorded).
    altroot: str | None = None


@dataclass
class AutomountConfig:
    credential_dir: str = str(DEFAULT_CREDENTIAL_DIR)
    entries: list[AutomountEntry] = field(default_factory=list)


def credential_name(encryption_root: str) -> str:
    digest = hashlib.sha256(encryption_root.encode()).hexdigest()[:20]
    return f"zfs-{digest}"


def validate_zfs_name(value: str) -> str:
    if not value or value.startswith("-") or any(char.isspace() for char in value):
        raise ValueError(f"Invalid ZFS dataset name: {value!r}")
    return value


def pool_name(dataset: str) -> str:
    return dataset.split("/", 1)[0]


class AutomationManager:
    def __init__(
        self, config_path: Path = DEFAULT_CONFIG, service: ZFSService | None = None,
    ) -> None:
        self.config_path = config_path
        self.service = service or ZFSService()

    def load(self) -> AutomountConfig:
        if not self.config_path.exists():
            return AutomountConfig()
        try:
            raw = json.loads(self.config_path.read_text())
            entries = [AutomountEntry(**entry) for entry in raw.get("entries", [])]
            return AutomountConfig(raw.get("credential_dir", str(DEFAULT_CREDENTIAL_DIR)), entries)
        except (OSError, ValueError, TypeError) as error:
            raise ZFSError(f"Invalid zmnt automation config: {error}") from error

    def save(self, config: AutomountConfig) -> None:
        self.config_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        payload = json.dumps(asdict(config), indent=2, sort_keys=True) + "\n"
        descriptor, temporary = tempfile.mkstemp(
            prefix=".config-", dir=self.config_path.parent,
        )
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.config_path)
        except Exception:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
            raise

    def configure(
        self, encryption_root: str, datasets: list[str], secret: str,
        backend: str = "tpm2", automount: bool = True,
        clear_on_stop: bool = True, credential_dir: str | None = None,
    ) -> AutomountEntry:
        validate_zfs_name(encryption_root)
        datasets = sorted({validate_zfs_name(name) for name in datasets})
        if backend not in VALID_BACKENDS:
            raise ZFSError(f"Credential backend must be one of: {', '.join(sorted(VALID_BACKENDS))}")
        if not secret:
            raise ZFSError("The encryption passphrase may not be empty")

        config = self.load()
        if credential_dir is not None:
            directory = Path(credential_dir)
            if not directory.is_absolute():
                raise ZFSError("Credential directory must be an absolute path")
            config.credential_dir = str(directory)
        directory = Path(config.credential_dir)
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(directory, 0o700)
        name = credential_name(encryption_root)
        destination = directory / f"{name}.cred"
        temporary = directory / f".{name}.{os.getpid()}.tmp"
        command = [
            "systemd-creds", "encrypt", f"--with-key={backend}", f"--name={name}",
            "-", str(temporary),
        ]
        result = subprocess.run(
            command, input=f"{secret}\n".encode(), stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE, check=False,
        )
        if result.returncode:
            temporary.unlink(missing_ok=True)
            raise ZFSError(result.stderr.decode(errors="replace").strip() or "Could not encrypt credential")
        os.chmod(temporary, 0o600)
        os.replace(temporary, destination)

        existing = next((item for item in config.entries if item.encryption_root == encryption_root), None)
        altroot = self.service.pool_altroot(pool_name(encryption_root))
        if altroot is None and existing is not None:
            altroot = existing.altroot
        entry = AutomountEntry(
            encryption_root, datasets, automount, destination.name, backend, clear_on_stop, altroot,
        )
        config.entries = [item for item in config.entries if item.encryption_root != encryption_root]
        config.entries.append(entry)
        config.entries.sort(key=lambda item: item.encryption_root)
        self.save(config)
        return entry

    def remove_stored_credential(self, encryption_root: str) -> bool:
        config = self.load()
        entry = next((item for item in config.entries if item.encryption_root == encryption_root), None)
        if entry is None:
            return False
        if entry.credential:
            (Path(config.credential_dir) / entry.credential).unlink(missing_ok=True)
        entry.credential = ""
        entry.automount = False
        self.save(config)
        return True

    def remove_entry(self, encryption_root: str) -> bool:
        config = self.load()
        entries = [item for item in config.entries if item.encryption_root != encryption_root]
        if len(entries) == len(config.entries):
            return False
        entry = next(item for item in config.entries if item.encryption_root == encryption_root)
        if entry.credential:
            (Path(config.credential_dir) / entry.credential).unlink(missing_ok=True)
        config.entries = entries
        self.save(config)
        return True

    def set_automount(self, encryption_root: str, enabled: bool) -> None:
        config = self.load()
        entry = next((item for item in config.entries if item.encryption_root == encryption_root), None)
        if entry is None:
            raise ZFSError(f"No automation entry for {encryption_root}")
        if enabled and not entry.credential:
            raise ZFSError("Cannot enable automount without a stored credential")
        entry.automount = enabled
        self.save(config)

    def start(self) -> list[str]:
        """Import, unlock and mount every enabled entry.

        A failing entry does not stop the others; failures are returned instead
        of raised. Only an unreadable configuration raises.
        """
        config = self.load()
        failures: list[str] = []
        pool_errors: dict[str, str] = {}
        for entry in config.entries:
            if not entry.automount:
                continue
            try:
                validate_zfs_name(entry.encryption_root)
                pool = pool_name(entry.encryption_root)
                if pool not in pool_errors:
                    pool_errors[pool] = self._ensure_pool_imported(pool, entry.altroot)
                if pool_errors[pool]:
                    raise ZFSError(f"pool {pool} is not imported: {pool_errors[pool]}")
                if not self.service.key_is_loaded(entry.encryption_root):
                    self._load_credential(config, entry)
                self.service.mount_datasets(entry.datasets)
            except ENTRY_ERRORS as error:
                failures.append(f"{entry.encryption_root}: {error}")
        return failures

    def stop(self) -> list[str]:
        """Unmount and unload entries that clear on stop, returning failures."""
        config = self.load()
        failures: list[str] = []
        for entry in reversed(config.entries):
            if not entry.clear_on_stop:
                continue
            try:
                self.service.unmount_datasets(list(reversed(entry.datasets)))
                self.service.unload_keys([entry.encryption_root])
            except ENTRY_ERRORS as error:
                failures.append(f"{entry.encryption_root}: {error}")
        return failures

    def _ensure_pool_imported(self, pool: str, altroot: str | None) -> str:
        """Import the pool if needed. Returns an error message, or "" on success."""
        try:
            if self.service.pool_is_imported(pool):
                return ""
            if altroot is None:
                altroot = f"{DEFAULT_MOUNT_BASE}/{pool}"
            self.service.import_pool(pool, altroot, force=False)
        except ENTRY_ERRORS as error:
            return str(error) or "import failed"
        return ""

    def clear_active_key(self, encryption_root: str) -> None:
        config = self.load()
        entry = next((item for item in config.entries if item.encryption_root == encryption_root), None)
        if entry is None:
            raise ZFSError(f"No automation entry for {encryption_root}")
        self.service.unmount_datasets(list(reversed(entry.datasets)))
        self.service.unload_keys([entry.encryption_root])

    def _load_credential(self, config: AutomountConfig, entry: AutomountEntry) -> None:
        if not entry.credential:
            raise ZFSError("No stored credential")
        source = Path(config.credential_dir) / entry.credential
        name = credential_name(entry.encryption_root)
        decrypt = subprocess.Popen(
            ["systemd-creds", "decrypt", f"--name={name}", str(source), "-"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        assert decrypt.stdout is not None
        load = subprocess.run(
            ["zfs", "load-key", "-L", "prompt", entry.encryption_root], stdin=decrypt.stdout,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )
        decrypt.stdout.close()
        decrypt_error = decrypt.stderr.read() if decrypt.stderr else b""
        decrypt_status = decrypt.wait()
        if decrypt_status:
            message = decrypt_error.decode(errors="replace").strip()
            load_error = load.stderr.decode(errors="replace").strip()
            detail = message or f"Credential decryption exited with status {decrypt_status}"
            if load.returncode and load_error:
                detail += f"; zfs load-key: {load_error}"
            raise ZFSError(detail)
        if load.returncode:
            message = load.stderr.decode(errors="replace").strip()
            raise ZFSError(message or "ZFS key loading failed")
