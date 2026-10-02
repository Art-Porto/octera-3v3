#!/bin/sh
# Octera-3V3 base installer: brings a factory-reset Ender-3 V3 Plus (F002) to
# a current Moonraker + Fluidd, then installs the Octera layer.
#
#   git clone https://github.com/Art-Porto/octera-3v3.git /usr/data/octera
#   sh /usr/data/octera/install-base.sh --check   # report only
#   sh /usr/data/octera/install-base.sh
#
# The printer must be idle. Moonraker and nginx are stopped and started.
# Files it replaces are copied to /usr/data/octera-backups/base_<timestamp>/.
#
# OCTERA_ROOT prefixes every absolute path and skips service control and the
# download (used by the offline tests; put the asset in the cache yourself).

set -e

ROOT="${OCTERA_ROOT:-}"
REPO="$ROOT/usr/data/octera"
DATA="$ROOT/usr/data"
SHARE="$ROOT/usr/share"
INITD="$ROOT/etc/init.d"
CONFIG="$DATA/printer_data/config"
CACHE="$DATA/octera-cache"
BACKUP="$DATA/octera-backups/base_$(date +%Y%m%d_%H%M%S)"
ASSET=octera-base-3v3.tar.gz
ASSET_URL="${OCTERA_BASE_URL:-https://github.com/Art-Porto/octera-3v3/releases/download/$(cat "$REPO/base/RELEASE")/$ASSET}"
CHECK=0
[ "$1" = "--check" ] && CHECK=1
CHANGES=0

say() { echo "octera-base: $*"; }
change() {
  CHANGES=$((CHANGES + 1))
  say "$1"
  shift
  [ "$CHECK" = 1 ] || "$@"
}
backup() {
  [ -e "$1" ] || return 0
  [ "$CHECK" = 1 ] && return 0
  mkdir -p "$BACKUP"
  [ -e "$BACKUP/$(basename "$1")" ] || cp -p "$1" "$BACKUP/"
}
service() { # service <init script> <action>; no-op under OCTERA_ROOT
  [ -n "$ROOT" ] && return 0
  [ -x "$INITD/$1" ] && "$INITD/$1" "$2" || true
}
link() {
  [ "$(readlink "$2" 2>/dev/null)" = "$1" ] && return 0
  if [ -e "$2" ] && [ ! -L "$2" ]; then
    change "remove factory copy $2" rm -rf "$2"
  fi
  change "link $2 -> $1" ln -sfn "$1" "$2"
}

# --- Preflight ------------------------------------------------------------
if [ -z "$ROOT" ]; then
  model=$(sed -n 's/.*"model_str" *: *"\([^"]*\)".*/\1/p' \
    /usr/data/creality/userdata/config/system_config.json 2>/dev/null)
  [ "$model" = "F002" ] || { say "this installer is only for the Ender-3 V3 Plus (F002); found '${model:-unknown}'"; exit 1; }
  if [ -S /tmp/klippy_uds ] && wget -q -O - "http://127.0.0.1:7125/printer/objects/query?print_stats" 2>/dev/null \
      | grep -q '"state": *"\(printing\|paused\)"'; then
    say "a print is active; aborting"; exit 1
  fi
fi

want_asset=0
[ -d "$DATA/moonraker/moonraker-env" ] && [ -d "$DATA/moonraker/moonraker/.git" ] || want_asset=1
[ -f "$DATA/fluidd/index.html" ] || want_asset=1

# --- Moonraker, its environment and Fluidd --------------------------------
if [ "$want_asset" = 1 ]; then
  expected=$(cut -d' ' -f1 "$REPO/base/SHA256")
  fetch() {
    mkdir -p "$CACHE"
    if [ ! -f "$CACHE/$ASSET" ]; then
      "$REPO/files/system/curl" -L --fail -o "$CACHE/$ASSET" "$ASSET_URL"
    fi
    actual=$(sha256sum "$CACHE/$ASSET" | cut -d' ' -f1)
    [ "$actual" = "$expected" ] || { say "asset checksum mismatch: $actual"; rm -f "$CACHE/$ASSET"; exit 1; }
  }
  unpack() {
    service S56moonraker_service stop
    tar xzf "$CACHE/$ASSET" -C "$DATA"
    rm -f "$CACHE/$ASSET"
  }
  change "download and verify $ASSET" fetch
  change "unpack Moonraker and Fluidd into /usr/data" unpack
fi
link /usr/data/moonraker/moonraker "$SHARE/moonraker"
link /usr/data/moonraker/moonraker-env "$SHARE/moonraker-env"
link /usr/data/fluidd "$SHARE/fluidd"

# --- Moonraker service (keeps caches off the root overlay) -----------------
if ! cmp -s "$REPO/base/S56moonraker_service" "$INITD/S56moonraker_service"; then
  backup "$INITD/S56moonraker_service"
  install_service() {
    cp "$REPO/base/S56moonraker_service" "$INITD/S56moonraker_service"
    chmod 755 "$INITD/S56moonraker_service"
  }
  change "install Moonraker service script" install_service
  MOONRAKER_RESTART=1
fi

# --- nginx ----------------------------------------------------------------
if [ -d "$ROOT/etc/nginx" ] && ! cmp -s "$REPO/base/nginx.conf" "$ROOT/etc/nginx/nginx.conf"; then
  backup "$ROOT/etc/nginx/nginx.conf"
  change "install nginx.conf (Fluidd on port 4408)" cp "$REPO/base/nginx.conf" "$ROOT/etc/nginx/nginx.conf"
  NGINX_RESTART=1
fi

# --- Moonraker configuration ----------------------------------------------
# Only written when absent or still the factory one; an existing Octera-era
# moonraker.conf (webcam, companions) is left alone.
if [ ! -f "$CONFIG/moonraker.conf" ] || ! grep -q "^provider: supervisord_cli" "$CONFIG/moonraker.conf"; then
  backup "$CONFIG/moonraker.conf"
  write_conf() {
    mkdir -p "$CONFIG"
    cp "$REPO/base/moonraker.conf" "$CONFIG/moonraker.conf"
  }
  change "install moonraker.conf" write_conf
  MOONRAKER_RESTART=1
fi
# Companion installers append their services to this file; never overwrite it.
if [ ! -f "$DATA/printer_data/moonraker.asvc" ]; then
  change "install moonraker.asvc (allowed services)" cp "$REPO/base/moonraker.asvc" "$DATA/printer_data/moonraker.asvc"
  MOONRAKER_RESTART=1
fi

# --- Octera layer ---------------------------------------------------------
if [ "$CHECK" = 1 ]; then
  sh "$REPO/install.sh" --check
else
  sh "$REPO/install.sh"
fi

# --- Services -------------------------------------------------------------
if [ "$CHECK" = 0 ]; then
  if [ "${NGINX_RESTART:-0}" = 1 ]; then service S50nginx restart; fi
  if [ "${MOONRAKER_RESTART:-0}" = 1 ] || [ "$want_asset" = 1 ]; then
    service S56moonraker_service restart
  fi
fi

if [ "$CHANGES" = 0 ]; then
  say "base already up to date"
elif [ "$CHECK" = 1 ]; then
  say "$CHANGES base change(s) pending"
else
  say "$CHANGES base change(s) applied; backups in $BACKUP"
  say "restart Klipper when the bed is clear; Fluidd is on port 4408"
fi
