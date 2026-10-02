#!/bin/sh
# Run ON a working printer: packs the Moonraker tree, its Python environment
# and Fluidd into the base asset that install-base.sh restores after a
# factory reset. Output: /usr/data/octera-cache/octera-base-3v3.tar.gz
set -e
OUT=/usr/data/octera-cache
mkdir -p "$OUT"
cd /usr/data
tar czf "$OUT/octera-base-3v3.tar.gz" \
  --exclude 'moonraker/tmp' --exclude '__pycache__' --exclude '*.pyc' \
  --exclude 'moonraker/moonraker/moonraker.conf' \
  --exclude 'moonraker/moonraker/.files-list.before' \
  moonraker fluidd
sha256sum "$OUT/octera-base-3v3.tar.gz"
ls -la "$OUT/octera-base-3v3.tar.gz"
