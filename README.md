# Useful plugins used by uboe
This repo contains useful plugins used by uboe. Some of them are comfort plugins, some of them are useful for debugging and some of them are used to extend the functionality of klipper.
# Installation

## Option 1: Automatic Updates via Moonraker (Recommended)
If you have Moonraker configured with an update manager, add the following to your `moonraker.conf`:

```ini
[update_manager uboe_klippy_plugins]
type: git_repo
path: ~/uboe_klippy_plugins
origin: https://github.com/CooperGerman/uboe_klippy_plugins.git
install_script: install.sh
managed_services: klipper
is_system_service: False
primary_branch: main
```

Moonraker will then automatically install and manage updates for these plugins.

## Option 2: Manual Installation
Clone the repo in your home directory and run the install script:

```bash
cd ~
git clone https://github.com/CooperGerman/uboe_klippy_plugins.git
cd uboe_klippy_plugins
./install.sh
```

Or use the Makefile:
```bash
cd ~/uboe_klippy_plugins
make setup
```

Both methods will create symlinks for each plugin in the `klipper/klippy/extras` folder so Klipper can load the plugins.

**After installation, you will need to `FIRMWARE_RESTART` the printer or restart the klipper service for the changes to take effect.**

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
