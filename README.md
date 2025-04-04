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
** You will need to `FIRMWARE_RESTART` the printer for the changes to take effect. (or restart klipper service)**