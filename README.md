# zmnt

`zmnt` is an interactive ZFS mount helper for Linux. It discovers imported and
importable pools, helps unlock encrypted datasets, mounts selected filesystems,
and opens mounted paths.

It provides two frontends backed by the same ZFS command layer:

- `zmnt` or `zmnt tui` starts the terminal interface.
- `zmnt gui` starts the PySide6 graphical interface.

Both frontends elevate with `run0` before performing ZFS operations.

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
