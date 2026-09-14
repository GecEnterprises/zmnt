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

The default encrypted credential location is
`/etc/credstore.encrypted/zmnt`. It can be changed in the GUI or with
`--credential-dir`. `host+tpm2` additionally binds credentials to systemd's
host secret. List settings with `zmnt auto list`.

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
