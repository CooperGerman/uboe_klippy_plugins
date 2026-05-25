# Stepper brakes
#
# Copyright (C) 2026 yannick le provost yannick.leprovost@uboe.fr
#
# This file may be distributed under the terms of the GNU GPLv3 license.

import logging

logger = logging.getLogger(__name__)

class StepperBrake:
    """Klipper plugin to control stepper brakes.

    Config example:
     [stepper_brake xz_brakes]
     pin: PB4
     stepper: stepper_x, stepper_z
     release_on_move: True
     engage_on_motor_off: True
    """

    # Updated on each init so the permanently-installed PrinterStepper patch
    # closure always routes stepper registration to the current instance.
    _current_instance = None

    def __init__(self, config):
        logger.info("StepperBrake.__init__ called")
        self.printer = config.get_printer()
        self.name = config.get_name()
        self.pin = config.get("pin")
        self.stepper_names = config.getlist("stepper", [])
        self.release_on_move = config.getboolean("release_on_move", True)
        self.engage_on_motor_off = config.getboolean("engage_on_motor_off", True)
        self.brake_configs = []
        self.initialized = False
        self._stepper_enable_hooked = False
        self._patched = False
        self._pin_obj = None  # Reference to output_pin object
        logger.info(f"StepperBrake initialized: name={self.name}, pin={self.pin}, steppers={self.stepper_names}")

        # Register G-code commands immediately
        self._register_gcode_commands()

        # Create output_pin object programmatically
        self._create_output_pin()

        # On MCU reset a new StepperBrake instance is created; updating the
        # class-level reference ensures the closure routes to the new instance.
        StepperBrake._current_instance = self

        # Patch stepper module immediately (might fail if not loaded yet, that's OK)
        self._patch_stepper_module()

        # Also register event handler to try again after printer is ready as fallback
        self.printer.register_event_handler("klippy:ready", self._on_printer_ready)

    def _create_output_pin(self):
        """Create a GPIO output pin."""
        try:
            ppins = self.printer.lookup_object("pins")
            self._pin_obj = ppins.setup_pin("digital_out", self.pin)
            # Disable max_duration (default 2s causes "exceed max_duration" shutdown
            # when pin changes are scheduled far ahead via register_lookahead_callback)
            self._pin_obj.setup_max_duration(0)
            # Start and shutdown value both 0 (brake engaged = pin low by default)
            self._pin_obj.setup_start_value(0, 0)
            logger.debug(f"Created GPIO output pin for {self.pin}")
        except Exception as e:
            raise self.printer.config_error(
                f"Failed to setup GPIO pin {self.pin}: {e}"
            )

    def _on_printer_ready(self):
        """Called when printer is ready: patch stepper module and hook
        stepper_enable callbacks for auto engage/release."""
        if not self._patched:
            self._patch_stepper_module()
        if not self._stepper_enable_hooked:
            self._hook_stepper_enable()

    def _patch_stepper_module(self):
        """Monkey-patch the stepper module to integrate our helper registration."""
        try:
            import sys
            if 'stepper' in sys.modules:
                stepper = sys.modules['stepper']
            else:
                from klippy import stepper

            # If already patched by us, just update the class ref and skip re-wrapping
            if getattr(stepper.PrinterStepper, '_stepper_brake_patched', False):
                logger.info("stepper.PrinterStepper already patched, skipping re-wrap")
                self._patched = True
                return

            original_PrinterStepper = stepper.PrinterStepper

            def patched_PrinterStepper(config, units_in_radians=False):
                mcu_stepper = original_PrinterStepper(config, units_in_radians)
                stepper_name = mcu_stepper.get_name()
                logger.info(f"Patched PrinterStepper called for {stepper_name}, calling hook")
                instance = StepperBrake._current_instance
                if instance is not None:
                    instance.register_stepper(config, mcu_stepper)
                else:
                    logger.warning("No StepperBrake instance available, cannot register stepper")
                return mcu_stepper

            patched_PrinterStepper._stepper_brake_patched = True
            stepper.PrinterStepper = patched_PrinterStepper
            logger.info("Successfully patched stepper.PrinterStepper")
            self._patched = True
        except Exception as e:
            logger.warning(f"Could not patch stepper module: {e}")
            self._patched = False

    def register_stepper(self, config, mcu_stepper):
        """Called when a stepper is registered.
        This is the hook point where we augment steppers with brake control.
        """
        stepper_name = mcu_stepper.get_name()
        logger.debug(f"register_stepper called for {stepper_name}")

        # Check if this stepper should have a brake
        if stepper_name not in self.stepper_names:
            logger.debug(f"Stepper {stepper_name} not in brake config, skipping")
            return

        logger.info(f"Augmenting stepper {stepper_name} with brake control")
        self._augment_stepper_with_brake(mcu_stepper)

        # Mark as initialized once we have all configured steppers
        if len(self.brake_configs) >= len(self.stepper_names):
            self.initialized = True
            logger.info(f"Stepper brakes fully initialized with {len(self.brake_configs)} steppers")

    def _augment_stepper_with_brake(self, stepper):
        """Tag a stepper so its brake state can be tracked."""
        stepper._brake_engaged = True  # Default to engaged at startup
        self.brake_configs.append({'stepper': stepper, 'name': stepper.get_name()})

    def _set_all_brakes(self, print_time, engage):
        """Set the shared brake pin and sync all per-stepper state flags."""
        self._pin_obj.set_digital(print_time, 1 if engage else 0)
        for cfg in self.brake_configs:
            cfg['stepper']._brake_engaged = engage

    def _hook_stepper_enable(self):
        """Register state callbacks on stepper_enable to engage brakes on M84/M18."""
        stepper_enable = self.printer.lookup_object("stepper_enable", None)
        if stepper_enable is None:
            logger.warning("stepper_enable module not found, cannot hook motor disable")
            return
        hooked = 0
        for cfg in self.brake_configs:
            name = cfg['name']
            try:
                enable_tracking = stepper_enable.lookup_enable(name)
                enable_tracking.register_state_callback(self._on_stepper_enable_change)
                hooked += 1
                logger.info(f"Hooked stepper_enable state callback for {name}")
            except Exception as e:
                logger.warning(f"Could not hook stepper_enable for {name}: {e}")
        if hooked:
            self._stepper_enable_hooked = True

    def _on_stepper_enable_change(self, print_time, is_enabled):
        """Called when any braked stepper's enable state changes.

        The print_time here comes directly from the stepper_enable system:
          - motor_enable: step-generation time (same time the enable pin goes high)
          - motor_disable: get_last_move_time() after a dwell (same time the enable pin goes low)
        Both contexts are safe for direct set_digital() — same pattern as the
        stepper enable pin itself. No lookahead indirection needed.

        The callback is registered for every braked stepper, so it fires once
        per stepper per event. The any_engaged/any_released guards ensure the
        shared brake pin is only written once regardless of stepper count.
        """
        if is_enabled:
            if self.release_on_move and any(
                cfg['stepper']._brake_engaged for cfg in self.brake_configs
            ):
                self._set_all_brakes(print_time, False)
                logger.debug("Auto-released brakes on motor enable")
        else:
            if self.engage_on_motor_off and any(
                not cfg['stepper']._brake_engaged for cfg in self.brake_configs
            ):
                self._set_all_brakes(print_time, True)
                logger.debug("Auto-engaged brakes on motor disable")

    def _register_gcode_commands(self):
        """Register G-code commands for manual brake control."""
        logger.debug("_register_gcode_commands called")
        gcode = self.printer.lookup_object("gcode")

        # Register STEPPER_BRAKE_ENGAGE command
        gcode.register_command(
            "STEPPER_BRAKE_ENGAGE",
            self.cmd_STEPPER_BRAKE_ENGAGE,
            desc="Engage stepper brake"
        )

        # Register STEPPER_BRAKE_RELEASE command
        gcode.register_command(
            "STEPPER_BRAKE_RELEASE",
            self.cmd_STEPPER_BRAKE_RELEASE,
            desc="Release stepper brake"
        )

        # Register STEPPER_BRAKE_STATUS command
        gcode.register_command(
            "STEPPER_BRAKE_STATUS",
            self.cmd_STEPPER_BRAKE_STATUS,
            desc="Report stepper brake status"
        )

        # Register SET_PIN mux handler so macros can use SET_PIN PIN=<brake_name>
        brake_name = self.name.split()[-1]
        gcode.register_mux_command(
            "SET_PIN", "PIN", brake_name,
            self.cmd_SET_PIN_brake,
            desc="Set stepper brake via SET_PIN"
        )
        logger.debug("G-code commands registered successfully")

    def _cmd_brake_action(self, gcmd, engage):
        """Shared handler for STEPPER_BRAKE_ENGAGE / RELEASE commands."""
        if not self.initialized:
            raise gcmd.error("Stepper brakes not yet initialized. Wait a moment and retry.")
        stepper_name = gcmd.get("STEPPER", None)
        if stepper_name is None:
            raise gcmd.error("STEPPER parameter required")
        matched = any(
            cfg['name'] == stepper_name or cfg['name'] == f"stepper_{stepper_name}"
            for cfg in self.brake_configs
        )
        if not matched:
            raise gcmd.error(f"Stepper '{stepper_name}' not found in brake configuration")
        toolhead = self.printer.lookup_object('toolhead')
        toolhead.register_lookahead_callback(
            lambda pt: self._set_all_brakes(pt, engage)
        )
        action = "Engaged" if engage else "Released"
        gcmd.respond_info(f"{action} brake for {stepper_name}")

    def cmd_STEPPER_BRAKE_ENGAGE(self, gcmd):
        """G-code command to engage brake on a specific stepper."""
        self._cmd_brake_action(gcmd, engage=True)

    def cmd_STEPPER_BRAKE_RELEASE(self, gcmd):
        """G-code command to release brake on a specific stepper."""
        self._cmd_brake_action(gcmd, engage=False)

    def cmd_STEPPER_BRAKE_STATUS(self, gcmd):
        """G-code command to report brake status for all configured steppers."""
        if not self.initialized:
            raise gcmd.error("Stepper brakes not yet initialized. Wait a moment and retry.")

        if not self.brake_configs:
            gcmd.respond_info("No steppers configured for brakes")
            return

        gcmd.respond_info("Stepper brake status:")
        for cfg in self.brake_configs:
            state = "ENGAGED" if cfg['stepper']._brake_engaged else "RELEASED"
            gcmd.respond_info(f"  {cfg['name']}: {state}")

    def cmd_SET_PIN_brake(self, gcmd):
        """Handle SET_PIN PIN=<brake_name> VALUE=0/1 from macros."""
        value = gcmd.get_float("VALUE", minval=0.0, maxval=1.0)
        engage = value >= 0.5
        toolhead = self.printer.lookup_object("toolhead")
        toolhead.register_lookahead_callback(
            lambda print_time: self._set_all_brakes(print_time, engage)
        )


def load_config_prefix(config):
    return StepperBrake(config)
