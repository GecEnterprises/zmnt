# zmnt

`zmnt` is an interactive ZFS mount helper for Linux. It discovers imported and
importable pools, helps unlock encrypted datasets, mounts selected filesystems,
and opens mounted paths.

It provides two frontends backed by the same ZFS command layer:

- `zmnt` or `zmnt tui` starts the terminal interface.
- `zmnt gui` starts the PySide6 graphical interface.

Both frontends elevate with `run0` before performing ZFS operations.

## TPM2 boot automount

`zmnt` can install TPM2-protected ZFS credentials and act as a systemd-managed
automount service. The service is a short-lived oneshot operation: plaintext
credentials flow directly from `systemd-creds` to `zfs load-key` and are not
kept by a resident daemon.

Configure an encryption root and the datasets that should be mounted:

```bash
sudo zmnt auto configure tank/secure tank/secure tank/secure/home --backend tpm2
sudo zmnt service enable
```

In the TUI, open a pool, select encrypted datasets with **Space**, then press
**a** for automount configuration. Choose **c** to store a credential (one
passphrase per encryption root), **e/d** to enable/disable selected roots,
**m** to mount all enabled entries now, or **s** to enable the boot service.
Configuring a root replaces its saved dataset list with the selected mountable
datasets. Existing stop behavior and the credential directory are preserved.

Apply saved settings directly from the CLI:

```bash
sudo zmnt automount       # Unlock and mount all enabled entries now
sudo zmnt auto mount      # Equivalent command
sudo zmnt reload          # Reload systemd's unit and apply saved entries
sudo zmnt service reload  # Equivalent command
```

Reload starts an inactive service or applies settings to an active service
without unmounting datasets. Disabling/removing an entry does not unmount it
during reload. Use `zmnt auto unload ROOT` for that. These commands use stored
credentials and import pools that are not imported yet.

The default encrypted credential location is
`/etc/credstore.encrypted/zmnt`. It can be changed in the GUI or with
`--credential-dir`. `host+tpm2` additionally binds credentials to systemd's
host secret. List settings with `zmnt auto list`.

If a pool was not imported by the system's ZFS import services (pools imported
with an altroot are never added to the cachefile), zmnt imports it with
`zpool import -N` using the altroot it had when the entry was configured, or
`/mnt/zfs/POOL` for entries saved before altroots were recorded. It never
force-imports. A failing entry is logged to the journal and skipped; the others
are still mounted and the service stays active, so their keys are unloaded on
stop. `zmnt automount` exits non-zero if any entry fails. Store credentials on a filesystem available before `local-fs.target`,
not on the encrypted dataset they unlock. The unit runs before local filesystems
and ZFS's general mount service, and must use the host mount namespace.
Do not add `PrivateTmp`, `ProtectHome`, or `ProtectSystem` to its service:
these isolate mounts from the host.

After upgrading an existing installation, reload the unit and enable it:

```bash
sudo systemctl daemon-reload
sudo zmnt service enable
systemctl status zmnt.service --no-pager
journalctl -b -u zmnt.service --no-pager
```

`active (exited)` is normal for this oneshot service. If it is already active,
new automount settings can be applied with `sudo zmnt daemon start`.

Key removal is intentionally split into persistent and active state:

```bash
# Remove the encrypted credential and disable this automount entry:
sudo zmnt auto clear tank/secure

# Unmount configured datasets and remove the loaded key from ZFS:
sudo zmnt auto unload tank/secure

# Do both:
sudo zmnt auto clear tank/secure --unload
```

By default, stopping `zmnt.service` unmounts its configured datasets and calls
`zfs unload-key`. Disable that behavior per entry with
`--keep-loaded-on-stop`. Removing a credential file is not equivalent to
forensic erasure of old SSD blocks, snapshots, or backups; TPM protection and
ZFS key rotation remain the relevant security boundaries.

## Requirements

- OpenZFS command-line tools (`zpool` and `zfs`)
- systemd `run0`
- Python 3.11 or later
- PySide6 for the GUI frontend

## Development

Run the backend tests:

```bash
python -m unittest discover -v
```

Install the project locally with its packaging tools, then run either frontend:

```bash
python -m pip install .
zmnt
zmnt gui
```

## Arch Linux and CachyOS

The in-tree `PKGBUILD` builds the current checkout. On Arch Linux or CachyOS,
build and install it with:

```bash
makepkg -sri
```

For AUR publication, update the `PKGBUILD` source stanza to fetch a signed,
versioned release tarball and replace the placeholder project URL.
