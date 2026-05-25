# stepper_brake — Klipper Plugin

Drives an electromagnetic stepper brake via a GPIO pin and keeps it automatically synchronised with the stepper driver enable state.

**Core rule:**
```
stepper enable ON  →  brake OFF  (motor free to move)
stepper enable OFF →  brake ON   (motor held by brake)
```

---

## Table of Contents

- [Configuration](#configuration)
- [Architecture](#architecture)
- [State Machine](#state-machine)
- [Initialisation Sequence](#initialisation-sequence)
- [Runtime Behaviour](#runtime-behaviour)
- [G-code Commands](#g-code-commands)
- [Pin Logic](#pin-logic)
- [Notes](#notes)

---

## Configuration

```ini
[stepper_brake xz_brakes]
pin: !PB4                        # GPIO pin (! = inverted, common for N.C. brakes)
stepper: stepper_x, stepper_z    # Steppers to associate with this brake
release_on_move: True            # Release brake when stepper is enabled (default True)
engage_on_motor_off: True        # Engage brake when stepper is disabled (default True)
```

Multiple `[stepper_brake <name>]` sections are supported. Each section controls one physical brake output shared across one or more steppers.

---

## Architecture

```mermaid
graph TD
    subgraph Klipper core
        SE["stepper_enable\n(EnableTracking × N)"]
        PS["stepper module\n(PrinterStepper — patched)"]
        GC["G-code parser"]
    end

    subgraph stepper_brake plugin
        SB["StepperBrake instance"]
        CB["_on_stepper_enable_change()"]
    end

    subgraph MCU
        PIN["digital_out pin\n(e.g. !PB4)"]
    end

    PS -- "register_stepper()" --> SB
    SE -- "motor_enable / motor_disable\ncallback with print_time" --> CB
    CB -- "set_digital(print_time, 0/1)" --> PIN

    GC -- "STEPPER_BRAKE_STATUS\nSTEPPER_BRAKE_ENGAGE\nSTEPPER_BRAKE_RELEASE\nSET_PIN PIN=xz_brakes" --> SB
    SB -- "register_lookahead_callback\n→ set_digital" --> PIN
```

**Key design decision:** the `_on_stepper_enable_change` callback receives the exact same `print_time` the `stepper_enable` module uses to toggle the driver enable pin. Calling `set_digital()` directly at that time is therefore safe — it is the identical mechanism used by the stepper enable pin itself, requiring no additional lookahead indirection.

---

## State Machine

```mermaid
stateDiagram-v2
    direction LR

    [*] --> Engaged : Klipper starts\n(logical 1 → physical LOW → brake ON)

    Engaged --> Released : motor_enable\n(first step scheduled\nfor a move or homing)

    Released --> Engaged : motor_disable\n(M84 / M18 /\nend-of-print)

    Engaged --> Engaged : motor_disable\n(already engaged — no-op)
    Released --> Released : motor_enable\n(already released — no-op)

    Released --> Engaged : Emergency stop\n(MCU shutdown_value → physical LOW)
    Engaged --> Engaged : Emergency stop\n(already engaged — no-op)
```

The no-op guards prevent writing the shared GPIO pin twice when multiple braked steppers are disabled together in `motor_off()` (both `stepper_x` and `stepper_z` fire the callback at the same `print_time`).

---

## Initialisation Sequence

```mermaid
flowchart TD
    A([Klipper loads config]) --> B["load_config_prefix()\nStepperBrake.__init__()"]
    B --> C["_register_gcode_commands()\nSTEPPER_BRAKE_*, SET_PIN mux"]
    C --> D["_create_output_pin()\nsetup_max_duration(0)\nsetup_start_value(1, 1)"]
    D --> E["_patch_stepper_module()"]
    E --> F{PrinterStepper\nalready patched?}
    F -- No --> G["Wrap PrinterStepper\nset _stepper_brake_patched sentinel"]
    F -- Yes --> H["Update class ref\n(MCU reset path)"]
    G --> I
    H --> I["register_event_handler\nklippy:ready + klippy:shutdown"]

    I --> J([klippy:ready event])
    J --> K["_hook_stepper_enable()"]
    K --> L["stepper_enable.lookup_enable()\nfor each braked stepper"]
    L --> M["EnableTracking.\nregister_state_callback()\n× N steppers"]
    M --> N([Plugin fully active])
```

The `PrinterStepper` patch runs at config-load time (before steppers are created). Each new stepper that matches a name in `stepper:` is tagged with a `_brake_engaged` state flag and appended to `brake_configs`.

---

## Runtime Behaviour

### Typical homing + disable + re-home cycle

```mermaid
sequenceDiagram
    actor User
    participant GCode
    participant StepperEnable
    participant StepperBrake
    participant MCU

    Note over MCU: PB4 = LOW → brake ON
    Note over StepperBrake: _brake_engaged = True

    User->>GCode: G28
    GCode->>StepperEnable: (first step triggers motor_enable)
    StepperEnable->>StepperBrake: _on_state_change(T, is_enabled=True)
    StepperBrake->>MCU: set_digital(T, 0) — brake OFF
    Note over MCU: PB4 = HIGH → brake OFF
    StepperEnable->>MCU: set_enable(T) — driver ON
    GCode-->>User: homing complete

    User->>GCode: M84
    GCode->>StepperEnable: motor_off() → motor_disable(T)
    StepperEnable->>StepperBrake: _on_state_change(T, is_enabled=False)
    StepperBrake->>MCU: set_digital(T, 1) — brake ON
    Note over MCU: PB4 = LOW → brake ON
    StepperEnable->>MCU: set_disable(T) — driver OFF

    User->>GCode: G28 (re-home)
    GCode->>StepperEnable: (first step triggers motor_enable)
    StepperEnable->>StepperBrake: _on_state_change(T, is_enabled=True)
    StepperBrake->>MCU: set_digital(T, 0) — brake OFF
    Note over MCU: PB4 = HIGH → brake OFF
    StepperEnable->>MCU: set_enable(T) — driver ON
    GCode-->>User: homing complete
```

### Timing guarantees

| Event | Source of `print_time` | Margin |
|-------|------------------------|--------|
| `motor_enable` | Step generation — start of first scheduled step | Equal to the move start time |
| `motor_disable` | `get_last_move_time()` after `dwell(0.1s)` | ≥ 250 ms ahead of MCU clock |

Both are the same `print_time` passed to `EnableTracking.motor_enable/motor_disable`. The brake pin and the driver enable pin are toggled at **identical** print times, so they change atomically from the MCU's perspective.

---

## G-code Commands

| Command | Description |
|---------|-------------|
| `STEPPER_BRAKE_STATUS` | Reports `ENGAGED` / `RELEASED` for every configured stepper |
| `STEPPER_BRAKE_ENGAGE STEPPER=<name>` | Manually engages the brake for one stepper |
| `STEPPER_BRAKE_RELEASE STEPPER=<name>` | Manually releases the brake for one stepper |
| `SET_PIN PIN=<brake_name> VALUE=1` | Engage (compatible with standard Klipper macro syntax) |
| `SET_PIN PIN=<brake_name> VALUE=0` | Release (compatible with standard Klipper macro syntax) |

Manual commands (`ENGAGE`, `RELEASE`, `SET_PIN`) route through `register_lookahead_callback` because they are called from G-code context, not from within the step-generation pipeline.

---

## Pin Logic

The plugin uses a standard Klipper `digital_out` pin with `setup_max_duration(0)` to remove the default 2-second cap (which would cause a "Scheduled digital out event will exceed max_duration" shutdown when pin changes are scheduled far ahead in the lookahead queue).

`setup_start_value(1, 1)` sets both the boot value and the emergency-shutdown value to logical `1`. The MCU XORs this with the pin's invert flag before writing the physical line:

$$\text{physical} = \text{logical} \oplus \text{invert}$$

| Config pin | `set_digital` / start value | Physical level | Brake state |
|-----------|------------------------------|----------------|-------------|
| `!PB4` (inverted) | `1` (start / shutdown) | LOW | **Engaged** |
| `!PB4` (inverted) | `0` (release) | HIGH | **Released** |
| `PB4` (normal) | `1` (start / shutdown) | HIGH | **Engaged** |
| `PB4` (normal) | `0` (release) | LOW | **Released** |

With an inverted pin (`!PB4`), the brake is engaged when the line is pulled LOW — typical for normally-closed electromagnetic brakes powered by an open-collector/drain output. `setup_start_value(1, 1)` ensures the brake is engaged both at Klipper boot and on any emergency stop or MCU shutdown, with no host-side intervention required.

### Optional: firmware-level startup pin

The `setup_start_value` guarantee takes effect when Klipper Python initialises the pin (a second or two after MCU power-on). For hardware-level assurance from the very first millisecond of power, add `!PB4` to the **"GPIO pins to set at micro-controller startup"** field in `make menuconfig`:

```
(!PB4) GPIO pins to set at micro-controller startup
```

The `!` prefix drives the pin LOW at MCU startup (before the host connects), keeping the brake engaged during the boot window.

---

## Notes

- **MCU reset** (`FIRMWARE_RESTART`): a new `StepperBrake` instance is created. The class attribute `StepperBrake._current_instance` is updated so the permanently-installed `PrinterStepper` patch closure routes calls to the new instance.
- **Emergency stop**: the `klippy:shutdown` event handler marks all `_brake_engaged` flags `True` in software. The MCU independently drives the pin to its `shutdown_value` (logical `1` → physical LOW → brake engaged) at the hardware level without needing a Python command.
- **Multiple steppers, one pin**: all steppers listed under `stepper:` share the single GPIO pin. The brake engages or releases together; per-stepper manual commands still operate on the shared pin.
- **`release_on_move: False`**: disables auto-release on motor enable. The brake must be released manually before motion; useful for testing or fail-safe configurations.
- **`engage_on_motor_off: False`**: disables auto-engage on motor disable. The brake remains in whatever state it was last set to.
