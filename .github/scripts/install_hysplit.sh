#!/usr/bin/env bash
# Install a Linux HYSPLIT as a HYSPLIT folder: /opt/hysplit/exec/hycs_std and
# the four data tables in /opt/hysplit/bdyfiles/, with hycs_std on PATH.
# runtraj includes no HYSPLIT, so this takes the v5.1.0 build that the
# repository below publishes, at the commit below, for the Kubernetes guide
# (docs/guides/kubernetes_setup.rst). CI does not use it: the tests, the docs
# build and the Kubernetes test replay recorded runs (CONTRIBUTING.md,
# "Testing against real HYSPLIT").
set -euo pipefail
sha="278b1ef6d542efd21c6ccab2a85b796acd9812a6"
repo="uataq/stilt"
base="https://raw.githubusercontent.com/$repo/$sha"
tmp="$(mktemp -d)"
mkdir -p "$tmp/exec" "$tmp/bdyfiles"
curl -fsSL --retry 3 -o "$tmp/exec/hycs_std" "$base/bin/linux_x64/hycs_std"
for table in ASCDATA.CFG LANDUSE.ASC ROUGLEN.ASC TERRAIN.ASC; do
  curl -fsSL --retry 3 -o "$tmp/bdyfiles/$table" "$base/exe/$table"
done
chmod +x "$tmp/exec/hycs_std"
sudo mkdir -p /opt
sudo mv "$tmp" /opt/hysplit
sudo ln -sf /opt/hysplit/exec/hycs_std /usr/local/bin/hycs_std
echo "HYSPLIT of $repo@$sha in /opt/hysplit:"
(cd /opt/hysplit && sha256sum exec/hycs_std bdyfiles/*)
