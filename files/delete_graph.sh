#!/bin/sh

if [ -f /usr/data/printer_data/config/octera-shapers/resonances_x.png ]; then
  rm -f /usr/data/printer_data/config/octera-shapers/resonances_x.png
fi
if [ -f /usr/data/printer_data/config/octera-shapers/resonances_y.png ]; then
  rm -f /usr/data/printer_data/config/octera-shapers/resonances_y.png
fi
if [ -f /usr/data/printer_data/config/octera-shapers/belts_calibration.png ]; then
  rm -f /usr/data/printer_data/config/octera-shapers/belts_calibration.png
fi

exit 0