#!/bin/sh
# Octera-3V3 installer for the Creality Ender-3 V3 Plus (F002).
#
#   sh /usr/data/octera/install.sh            install or refresh the Octera layer
#   sh /usr/data/octera/install.sh --check    report what would change, touch nothing
#   sh /usr/data/octera/install.sh --lis2dw   also switch the accelerometers to LIS2DW
#                                             (boards whose ACCELEROMETER_QUERY fails
#                                             with "Invalid adxl345 id")
#
# Idempotent: running it twice changes nothing the second time. Every file it
# edits is copied to /usr/data/octera-backups/install_<timestamp>/ first.
# Klipper is NOT restarted here; do it when the bed is clear.
#
# OCTERA_ROOT prefixes every absolute path (used by the offline tests).

set -e

ROOT="${OCTERA_ROOT:-}"
REPO="$ROOT/usr/data/octera"
CONFIG="$ROOT/usr/data/printer_data/config"
EXTRAS="$ROOT/usr/share/klipper/klippy/extras"
PRINTER_CFG="$CONFIG/printer.cfg"
MACROS_CFG="$CONFIG/gcode_macro.cfg"
MOONRAKER_CFG="$CONFIG/moonraker.conf"
MATPLOTLIB="$ROOT/usr/lib/python3.8/site-packages/matplotlib"
BACKUP="$ROOT/usr/data/octera-backups/install_$(date +%Y%m%d_%H%M%S)"
ORIGIN="${OCTERA_ORIGIN:-$(git -C "$REPO" config --get remote.origin.url 2>/dev/null || true)}"
CHECK=0
LIS2DW=0
for arg in "$@"; do
  case "$arg" in
    --check) CHECK=1 ;;
    --lis2dw) LIS2DW=1 ;;
  esac
done
CHANGES=0

say() { echo "octera: $*"; }

# change <description> <command...>: run the command unless in --check mode.
change() {
  CHANGES=$((CHANGES + 1))
  say "$1"
  shift
  [ "$CHECK" = 1 ] || "$@"
}

backup() {
  [ -f "$1" ] || return 0
  [ "$CHECK" = 1 ] && return 0
  mkdir -p "$BACKUP"
  [ -f "$BACKUP/$(basename "$1")" ] || cp -p "$1" "$BACKUP/"
}

link() { # link <target> <link path>
  [ "$(readlink "$2" 2>/dev/null)" = "$1" ] && return 0
  change "link $2 -> $1" ln -sfn "$1" "$2"
}

comment_block() { # comment_block <file> <section header regex>: comment until blank line
  grep -q "^$2" "$1" || return 0
  backup "$1"
  change "disable $2 in $(basename "$1")" \
    sed -i "/^$2/,/^[[:space:]]*\$/ s/^\([[:space:]]*\)\([^#]\)/#\1\2/" "$1"
}

[ -f "$REPO/config/octera.cfg" ] || { say "repository not found at $REPO"; exit 1; }
[ -f "$PRINTER_CFG" ] || { say "printer.cfg not found at $PRINTER_CFG"; exit 1; }

# --- Klipper extras -------------------------------------------------------
for module in octera_mesh_reuse octera_mesh_verify gcode_shell_command virtual_pins calibrate_shaper_config; do
  target="/usr/data/octera/klippy/extras/$module.py"
  if [ -f "$EXTRAS/$module.py" ] && [ ! -L "$EXTRAS/$module.py" ]; then
    backup "$EXTRAS/$module.py"
    change "replace copied $module.py with a link" rm -f "$EXTRAS/$module.py"
  fi
  link "$target" "$EXTRAS/$module.py"
done

# --- Service control shims used by Moonraker ------------------------------
for tool in supervisorctl sudo systemctl; do
  link "/usr/data/octera/files/system/$tool" "$ROOT/usr/bin/$tool"
done

# --- Config directory -----------------------------------------------------
link "/usr/data/octera/config" "$CONFIG/Octera"
[ -d "$CONFIG/octera-shapers" ] || change "create octera-shapers output folder" mkdir -p "$CONFIG/octera-shapers"
if [ ! -f "$CONFIG/octera-variables.cfg" ] && [ -f "$CONFIG/Helper-Script/variables.cfg" ]; then
  change "carry saved variables over from the Helper Script" \
    cp -p "$CONFIG/Helper-Script/variables.cfg" "$CONFIG/octera-variables.cfg"
fi

# --- printer.cfg ----------------------------------------------------------
if grep -q "^\[include Helper-Script/" "$PRINTER_CFG"; then
  backup "$PRINTER_CFG"
  change "remove Helper-Script includes from printer.cfg" \
    sed -i '/^\[include Helper-Script\//d' "$PRINTER_CFG"
fi
if grep -q "^\[restore_bed_mesh\]" "$PRINTER_CFG"; then
  backup "$PRINTER_CFG"
  change "remove obsolete [restore_bed_mesh] section" \
    sed -i '/^\[restore_bed_mesh\]/d' "$PRINTER_CFG"
fi
if ! grep -q "^\[include Octera/octera.cfg\]" "$PRINTER_CFG"; then
  backup "$PRINTER_CFG"
  change "include Octera/octera.cfg in printer.cfg" \
    sed -i '/^\[include printer_params\.cfg\]/a [include Octera/octera.cfg]' "$PRINTER_CFG"
fi
# The adaptive plan and the full-bed fallback assume a 9x9 cap (33.75 mm).
if sed -n '/^\[bed_mesh\]/,/^\[/p' "$PRINTER_CFG" | grep -q '^probe_count' \
    && ! sed -n '/^\[bed_mesh\]/,/^\[/p' "$PRINTER_CFG" | grep -q '^probe_count:[[:space:]]*9,[[:space:]]*9'; then
  backup "$PRINTER_CFG"
  change "set bed_mesh probe_count to 9,9" \
    sed -i '/^\[bed_mesh\]/,/^\[/ s/^probe_count:.*/probe_count:9,9/' "$PRINTER_CFG"
fi
# Newer boards carry LIS2DW accelerometers although printer.cfg says adxl345.
if [ "$LIS2DW" = 1 ] && grep -q '^\[adxl345 adx_[xy]\]' "$PRINTER_CFG"; then
  backup "$PRINTER_CFG"
  to_lis2dw() {
    sed -i \
      -e 's/^\[adxl345 adx_x\]/[lis2dw adx_x]/' \
      -e 's/^\[adxl345 adx_y\]/[lis2dw adx_y]/' \
      -e 's/^accel_chip_x:[[:space:]]*adxl345 adx_x/accel_chip_x:lis2dw adx_x/' \
      -e 's/^accel_chip_y:[[:space:]]*adxl345 adx_y/accel_chip_y:lis2dw adx_y/' \
      -e '/^\[lis2dw adx_[xy]\]/,/^$/ s/^spi_speed:.*/spi_speed: 5000000/' \
      -e '/^\[lis2dw adx_y\]/,/^$/ s/^axes_map:.*/axes_map: x,y,z/' \
      "$PRINTER_CFG"
  }
  change "switch accelerometers to lis2dw" to_lis2dw
fi
# Printer-specific settings (input shaper, overrides) live outside the repo.
if [ ! -f "$CONFIG/octera-local.cfg" ]; then
  local_cfg() {
    printf '%s\n' \
      '# Settings specific to this printer; included by Octera/octera.cfg.' \
      '# Not part of the octera repository, so updates never touch it.' \
      '# Example, after measuring with TEST_RESONANCES_GRAPHS:' \
      '#[input_shaper]' \
      '#shaper_type_x: mzv' \
      '#shaper_freq_x: 50.0' \
      '#shaper_type_y: mzv' \
      '#shaper_freq_y: 50.0' > "$CONFIG/octera-local.cfg"
  }
  change "create octera-local.cfg for printer-specific settings" local_cfg
fi
# M600 support owns the filament sensor and the idle timeout.
comment_block "$PRINTER_CFG" '\[idle_timeout\]'
comment_block "$PRINTER_CFG" '\[filament_switch_sensor filament_sensor\]'

# --- gcode_macro.cfg ------------------------------------------------------
if grep -q "^\[gcode_macro START_PRINT\]" "$MACROS_CFG"; then
  backup "$MACROS_CFG"
  # The stock macro has blank lines inside its body; close them up so the
  # whole block is commented out.
  change "close up stock START_PRINT" \
    sed -i '/^\[gcode_macro START_PRINT\]/,/^[[:space:]]*CX_PRINT_DRAW_ONE_LINE/ { /^[[:space:]]*$/d }' "$MACROS_CFG"
fi
comment_block "$MACROS_CFG" '\[gcode_macro START_PRINT\]'
comment_block "$MACROS_CFG" '\[gcode_macro RESUME\]'
if grep -q "^[[:space:]]*variable_autotune_shapers:" "$MACROS_CFG"; then
  backup "$MACROS_CFG"
  change "disable stock autotune_shapers variable" \
    sed -i 's/^\([[:space:]]*\)variable_autotune_shapers:/#\1variable_autotune_shapers:/' "$MACROS_CFG"
fi

# --- matplotlib font module needed by the shaper graphs --------------------
SO=ft2font.cpython-38-mipsel-linux-gnu.so
if [ -d "$MATPLOTLIB" ] && ! cmp -s "$REPO/files/$SO" "$MATPLOTLIB/$SO"; then
  backup "$MATPLOTLIB/$SO"
  change "install matplotlib $SO" cp "$REPO/files/$SO" "$MATPLOTLIB/$SO"
fi

# --- Moonraker update manager ---------------------------------------------
if [ -f "$MOONRAKER_CFG" ]; then
  if grep -q "^\[update_manager Creality-Helper-Script\]" "$MOONRAKER_CFG"; then
    backup "$MOONRAKER_CFG"
    change "remove the Helper Script from the update manager" \
      sed -i '/^\[update_manager Creality-Helper-Script\]/,/^[[:space:]]*$/d' "$MOONRAKER_CFG"
  fi
  if ! grep -q "^\[update_manager octera\]" "$MOONRAKER_CFG"; then
    if [ -n "$ORIGIN" ]; then
      backup "$MOONRAKER_CFG"
      add_update_manager() {
        printf '\n[update_manager octera]\ntype: git_repo\nchannel: dev\npath: /usr/data/octera\norigin: %s\nprimary_branch: main\nmanaged_services: klipper\n' \
          "$ORIGIN" >> "$MOONRAKER_CFG"
      }
      change "register octera in the update manager" add_update_manager
    else
      say "no git origin set; update manager entry skipped"
    fi
  fi
fi

if [ "$CHANGES" = 0 ]; then
  say "already up to date"
elif [ "$CHECK" = 1 ]; then
  say "$CHANGES change(s) pending"
else
  say "$CHANGES change(s) applied; backups in $BACKUP"
  say "restart the Klipper SERVICE when the bed is clear (and Moonraker if the"
  say "update manager changed). The RESTART command does not reload Python modules."
fi
