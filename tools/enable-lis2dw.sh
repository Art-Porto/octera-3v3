#!/bin/sh
# Switch the accelerometer sections of printer.cfg from ADXL345 to LIS2DW.
#
# Newer Ender-3 V3 Plus boards (nozzle/leveling MCU firmware e415346a, 122
# commands) carry LIS2DW chips, but Creality's printer.cfg still declares
# [adxl345]; every accelerometer command then fails with
#   Invalid adxl345 id (got 0 vs e5)
# Run this once when ACCELEROMETER_QUERY shows that error, then restart the
# Klipper service and check: ACCELEROMETER_QUERY CHIP=adx_x / CHIP=adx_y
# (the vertical axis must read about 9800 mm/s^2).
#
#   sh /usr/data/octera/tools/enable-lis2dw.sh
set -e
CFG=/usr/data/printer_data/config/printer.cfg
BACKUP=/usr/data/octera-backups/lis2dw_$(date +%Y%m%d_%H%M%S)

if ! grep -q '^\[adxl345 adx_[xy]\]' "$CFG"; then
  echo "octera: no [adxl345 adx_x/adx_y] sections found; nothing to do"
  exit 0
fi
mkdir -p "$BACKUP"
cp -p "$CFG" "$BACKUP/"
sed -i \
  -e 's/^\[adxl345 adx_x\]/[lis2dw adx_x]/' \
  -e 's/^\[adxl345 adx_y\]/[lis2dw adx_y]/' \
  -e 's/^accel_chip_x:[[:space:]]*adxl345 adx_x/accel_chip_x:lis2dw adx_x/' \
  -e 's/^accel_chip_y:[[:space:]]*adxl345 adx_y/accel_chip_y:lis2dw adx_y/' \
  -e '/^\[lis2dw adx_[xy]\]/,/^$/ s/^spi_speed:.*/spi_speed: 5000000/' \
  "$CFG"
echo "octera: accelerometers switched to lis2dw; backup in $BACKUP"
echo "octera: restart the Klipper service, then run ACCELEROMETER_QUERY CHIP=adx_x"
