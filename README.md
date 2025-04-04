# Useful plugins used by uboe
This repo contains useful plugins used by uboe. Some of them are confort plugins, some of them are useful for debugging and some of them are used to extend the functionality of klipper.
# Installation
1. Clone the repo where preferrably in your home directory.:
```bash
cd ~
git clone https://github.com/print-hive/uboe_klippy_plugins.git
cd uboe_klippy_plugins
make
```
This will create symlinks for each plugin in the klipper/klippy/extras folder in order for klippy to "seee" the plugins.

**You will need to `FIRMWARE_RESTART` the printer for the changes to take effect. (or restart klipper service)**

# Plugins

The table below lists the plugins and their functionality.
| Plugin Name | feature | Description |
| ----------- | ------- | ----------- |
|  |  |  |
| klipper_macros |  | This plugin can be instantiated using `[klipper_macros]` in your printer config files. |
|  | `MAKE_SURFACE_TEMP_PROFILE` command | This command will make a surface temperature profile for the active surface by setting the bed temperature from 40 to max bed temp stepping 5 and saving the temperature profile to the active surface. |
| | `SAVE_TEMP_PROFILE` command |  This command will save the current temperature profile to the active surface. |
| | `CONTINUE_SURFACE_TEMP_PROFILE` command | This command will continue a surface temperature profile for the active surface by setting the bed temperature from 40 to max bed temp stepping 5 and saving the temperature profile to the active surface starting with latest measured temperature entry + 5. |
| | `SET_HEATER_TEMPERATURE_COMPENSATE` command |         This command will try to apply an offset to the heater target temp if the the heater is in the list of heaters with a temp_profile. |
|  |  |  |
| toolhead_bed_temp_sensor |  | This plugin can be instantiated using `[toolhead_bed_temp_sensor]` in your printer config files. It models a toolhead attached temperature sensor that can be x and y offset and can be used for the bed temperature profiling. |
