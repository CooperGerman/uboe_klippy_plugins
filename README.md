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
| bed_expansion |  | Records bed meshes over time, determines heat-soak stability, and waits for a stable bed during print start. |
| | `BED_EXPANSION_ANALYZE` | Captures meshes for a fixed duration at a requested bed target. |
| | `BED_EXPANSION_CALIBRATE` | Captures meshes until the configured stability window is reached or the timeout expires. |
| | `BED_EXPANSION_WAIT` | Blocks print-start until the bed mesh is stable or the timeout policy is applied. |
| | `BED_EXPANSION_LOAD` | Loads a captured sample into the live Klipper bed mesh for visualization in Mainsail. |
| | `BED_EXPANSION_STATUS` | Reports run state, sample count, latest deltas, and output file. |
|  |  |  |
| toolhead_bed_temp_sensor |  | This plugin can be instantiated using `[toolhead_bed_temp_sensor]` in your printer config files. It models a toolhead attached temperature sensor that can be x and y offset and can be used for the bed temperature profiling. |
|  |  |  |
| stepper_brake |  | This plugin can be instantiated using `[stepper_brake <name>]` in your printer config files. It drives an electromagnetic stepper brake via a GPIO pin and keeps it synchronised with the stepper driver enable state. See [docs/stepper_brake.md](docs/stepper_brake.md) for detailed documentation. |
| | Auto-engage on motor disable | When `engage_on_motor_off: True` (default), the brake is automatically engaged whenever the associated steppers are disabled (M84 / M18 / end-of-print). |
| | Auto-release on motor enable | When `release_on_move: True` (default), the brake is automatically released when the stepper driver is enabled for the first move after being disabled. |
| | `STEPPER_BRAKE_STATUS` command | Reports the current brake state (ENGAGED / RELEASED) for all configured steppers. |
| | `STEPPER_BRAKE_ENGAGE STEPPER=<name>` command | Manually engages the brake for a specific stepper. |
| | `STEPPER_BRAKE_RELEASE STEPPER=<name>` command | Manually releases the brake for a specific stepper. |
| | `SET_PIN PIN=<brake_name> VALUE=0/1` command | Compatible with standard Klipper macro syntax to engage (1) or release (0) the brake. |

## IDEX input shaper capture

After updating the plugin's Python code, restart the Klipper service to load
it. The `RESTART` and `FIRMWARE_RESTART` G-code commands do not reload Python
modules in the running Klipper process.

Set `shaper_config_path` in `[uboe_tenor]` to the included file containing the four
`variable_shaper_{x,y}_{type,freq}` settings under `[gcode_macro RatOS]`.
After homing and selecting single mode, a full capture runs:

```gcode
UBOE_SHAPER_CAPTURE MODE=START
UBOE_SHAPER_CAPTURE MODE=SINGLE
GENERATE_SHAPER_GRAPHS
IDEX_COPY
UBOE_SHAPER_CAPTURE MODE=COPY
GENERATE_SHAPER_GRAPHS
IDEX_MIRROR
UBOE_SHAPER_CAPTURE MODE=MIRROR
GENERATE_SHAPER_GRAPHS
UBOE_SHAPER_CAPTURE MODE=FINISH
```

The capture requires X and Y measurements from both toolheads in each mode.
It follows the [Rat Rig IDEX commissioning guide](https://wiki.ratrig.com/products/v-core-4-1/commissioning-guide/idex/03-calibration#h-03-input-shaper):
MZV is fitted separately to each measurement, and COPY/MIRROR frequencies are
the mean of the corresponding toolhead frequencies. Fits use the graph script's
default square corner velocity of 5 mm/s; acceleration estimates use the
current printer setting. FINISH writes the four
settings to the configured file and keeps a `.bak` copy. Restart Klipper to
apply them; `SAVE_CONFIG` is not used. If a run fails or is interrupted, use
`UBOE_SHAPER_CAPTURE MODE=ABORT` or start a new capture.

The reported acceleration values estimate input-shaper smoothing, not safe
mechanical limits. Neither acceleration nor speed limits are changed.

## Bed expansion analysis

Add the plugin after the `[bed_mesh]` section in the printer configuration:

```ini
[bed_expansion]
output_dir: ~/printer_data/logs/bed_expansion
interval: 120
duration: 1800
timeout: 7200
tolerance: 0.01
metric: max_abs
stable_window: 5
park_macro: PARK
rehome_z_before_scan: True
relevel_before_scan: True
render_on_finish: True
render_mode: baseline
render_view: surface
render_data_source: mesh
render_fps: 1
render_dpi: 120
render_z_exaggeration: 2
# Optional bed-mesh overrides. Omit these to use [bed_mesh] defaults.
mesh_command: BED_MESH_CALIBRATE
# mesh_speed: 100
# mesh_probe_count: 20,20
# mesh_min: 20,20
# mesh_max: 570,580
mesh_reject_tolerance: 0.1
mesh_retries: 1
```

All command time values are seconds and all mesh-delta values are millimetres.
The plugin does not poll Klipper state. Homing, leveling, temperature
waiting, and bed-mesh commands use Klipper's blocking, reactor-aware command
flow; the only plugin timer wake is the next scan deadline.
The plugin:

1. Homes X/Y/Z when necessary.
2. Runs `QUAD_GANTRY_LEVEL` or `Z_TILT_ADJUST` when either module is present.
3. Sets the requested bed target and lets Klipper's `TEMPERATURE_WAIT` detect
   when the sensor has reached it.
4. Clears the active mesh, reruns QGL/Z-tilt when configured, and rehomes Z
   immediately before each scan. Releveling removes accumulating gantry tilt so
   each sample matches the geometry established by a print-start leveling
   procedure.
5. Captures a bed mesh immediately, then waits the configured interval after
   that mesh completes before starting the next scan. A slow mesh therefore
   never causes scans to overlap or bunch together.
   A mesh is rejected when any point differs from the last accepted raw mesh
   by more than `mesh_reject_tolerance`. The prior valid mesh is restored so a
   bad scan is never left active, and up to `mesh_retries` fresh scans are
   attempted immediately. Rejected samples are stored as `rejected_sample` JSONL
   records, but do not count toward the stable window or replace the previous
   valid mesh. This keeps the mesh used by `BED_EXPANSION_WAIT` valid; a separate
   ordinary `BED_MESH_CALIBRATE` outside the plugin is not covered by this check.
6. Runs `park_macro` after each capture.
7. Turns the bed heater off when the run completes, stabilizes, times out, or
   fails.

Successful runs with at least two samples are rendered automatically. Rendering
uses Klipper's threaded `aio_executor`, so Matplotlib and Pillow do not run in
the printer's main thread. The GIF is saved beside the JSONL file and displayed
in the web console through Moonraker's file server. The printer-data root is
derived from Klipper's startup `config_file` path. Set `render_on_finish: False`
to disable automatic output.

When `BED_EXPANSION_CALIBRATE` reaches stability, the analyzer also stages a
`heatsoak_profiles` autosave value under `[bed_expansion]`. Run
`SAVE_CONFIG` after calibration to persist it. Each temperature key contains
the target, conservative heat-soak completion time, inferred start of the
stable window, tolerance, metric, interval, window size, sample count, and
source run:

```ini
# Written by SAVE_CONFIG
#*# [bed_expansion]
#*# heatsoak_profiles = {"85":{"heatsoak_time_s":1200.0,...}}
```

Macros can read it directly:

```jinja
{% set profiles = printer.bed_expansion.heatsoak_profiles %}
{% set ns = namespace(profile={}) %}
{% for temperature, profile in profiles.items() %}
  {% if temperature|float == params.BED_TEMP|float %}
    {% set ns.profile = profile %}
  {% endif %}
{% endfor %}
```

Use `heatsoak_time_s` for conservative heat-soak timing. It is measured from
the bed reaching the target through the final sample that proves the entire
stable window. `stable_window_started_s` is the earlier estimate at which the
first of the consecutive stable comparisons began.

Capture a fixed-duration series:

```gcode
BED_EXPANSION_ANALYZE TARGET=85 INTERVAL=120 DURATION=1800
```

Find a stable heat-soak time:

```gcode
BED_EXPANSION_CALIBRATE TARGET=85 INTERVAL=120 TIMEOUT=7200 TOLERANCE=0.01 METRIC=max_abs STABLE_WINDOW=5
```

Wait for a thermally stable bed directly during print start:

```gcode
BED_EXPANSION_WAIT TARGET={bed_temp} INTERVAL=120 TIMEOUT=7200 TOLERANCE=0.01 METRIC=stdev STABLE_WINDOW=5 ON_TIMEOUT=error
```

`BED_EXPANSION_WAIT` keeps the bed heater at the target and blocks the
remaining macro until stability is reached. `ON_TIMEOUT=error` is the safe
default and aborts the pending print-start command; `ON_TIMEOUT=warn` keeps the
heater hot and continues. Print-time runs update `printer` status but do not
stage an autosave profile.

Mesh settings can be overridden for either command. Global `mesh_*` options
in `[bed_expansion]` provide defaults for every run; command
parameters override them for one run. If no value is supplied, Klipper's
configured `[bed_mesh]` defaults remain in effect:

```gcode
BED_EXPANSION_CALIBRATE TARGET=85 SPEED=80 PROBE_COUNT=12,12 MESH_MIN=30,30 MESH_MAX=550,550
```

The `mesh_command` config option accepts a G-code command with fixed arguments,
for example `mesh_command: BED_MESH_CALIBRATE METHOD=rapid`.
The `MESH_COMMAND` command parameter can override the command name for one
run. `SPEED` controls
normal bed-mesh point travel and is also sent as `SCAN_SPEED` for rapid-scan
commands.

For `METRIC=max_abs`, a comparison is stable only when every absolute
point-to-point Z delta is at or below `TOLERANCE`. The supported metrics are
`max_abs`, `stdev`, and `rms`. The run stops after the configured number of
consecutive stable comparisons. This window prevents one quiet interval from
ending the analysis early.

Each run is written as one timestamped JSONL file under `output_dir`. Every
sample contains the raw probed matrix, interpolated mesh matrix, mesh
parameters, raw point-to-point delta matrix, mean delta, maximum absolute
delta, population standard deviation, and RMS delta. The final record records
`completed`, `stable`, `timeout`, `failed`, or `interrupted`.

Load a zero-based sample index into Klipper's live mesh:

```gcode
BED_EXPANSION_LOAD RUN=20260101T120000000000Z_calibrate.jsonl SAMPLE=12
```

Mainsail can then display the loaded mesh as the current bed mesh. To also
keep it in Klipper's profile list for the current session, provide `PROFILE`.
Use `SAVE_CONFIG` afterward only if that profile should be written
permanently:

```gcode
BED_EXPANSION_LOAD RUN=20260101T120000000000Z_calibrate.jsonl SAMPLE=12 PROFILE=heatsoak_85c
```

Use `BED_EXPANSION_STATUS` to inspect an active or most recently completed run.

## Rendering a bed expansion GIF

Rendering runs separately from Klipper so image generation cannot block printer
control:

```shell
python3 bed_expansion_render.py \
  ~/printer_data/logs/bed_expansion/20261120T112936537358Z_calibrate.jsonl \
  --mode baseline \
  --view surface \
  --z-exaggeration 2
```

Install its optional dependencies with:

```shell
python3 -m pip install -r requirements-render.txt
```

The output GIF is written beside the run by default. `--output` and `--fps`
allow overriding its destination and playback speed. `--view heatmap` creates
the 2D top view; `--view surface` creates an isometric 3D mesh.
`--data-source mesh` uses Klipper's smoother interpolated bed mesh and is the
default; `--data-source probed` visualizes only the raw sampled probe grid.
`--z-exaggeration` multiplies the visual vertical scale for surface views and
defaults to 2; use 1 for the base scale or 3 for stronger deformation.
Rendering modes are:

- `baseline`: each mesh minus the first mesh, showing thermal deformation
  relative to the starting state.
- `absolute`: the measured mesh at each sample.
- `previous`: each mesh minus the immediately preceding mesh.

All animation frames use the same color and Z-axis range. Baseline and
previous-change animations use a symmetric scale with zero at the center. The
3D camera and aspect ratio remain fixed, while the surface itself updates in
every frame. The 3D vertical axis is normalized to the fixed data range, so it
is intentionally exaggerated relative to the bed's X/Y dimensions. A timeline
below the plot marks every captured sample and advances the active sample
indicator with its elapsed heat-soak time.
