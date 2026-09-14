# This in-tree recipe builds the checkout in which makepkg is invoked.
# For AUR publication, replace source=() with a signed release tarball and checksum.
pkgname=zmnt
pkgver=0.1.0
pkgrel=2
pkgdesc='Interactive terminal and graphical ZFS mount wizard'
arch=('any')
url='https://github.com/your-account/zmnt'
license=('GPL-3.0-or-later')
depends=(
  'python'
  'pyside6'
  'zfs-utils'
  'systemd'
  'xdg-utils'
)
makedepends=(
  'python-build'
  'python-installer'
  'python-setuptools'
  'python-wheel'
)
source=()
sha256sums=()

build() {
  cd "$startdir"
  python -m build --wheel --no-isolation
}

package() {
  cd "$startdir"
  python -m installer --destdir="$pkgdir" dist/*.whl
  install -Dm644 data/zmnt.desktop "$pkgdir/usr/share/applications/zmnt.desktop"
}
