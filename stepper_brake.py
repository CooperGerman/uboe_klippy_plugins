# Stepper brakes
#
# Copyright (C) 2026 yannick le provost yannick.leprovost@uboe.fr
#
# This file may be distributed under the terms of the GNU GPLv3 license.

import logging

logger = logging.getLogger(__name__)

# Global stepper brake instance
_stepper_brake_instance = None


class StepperBrake:
    """Klipper plugin to control stepper brakes.

    Config example:
     [stepper_brake xz_brakes]
     pin: PB4
     stepper: stepper_x, stepper_z
     release_on_move: True
     engage_on_dwell: True
    """
    def __init__(self, config):
        global _stepper_brake_instance
        logger.info("StepperBrake.__init__ called")
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.name = config.get_name()
        self.pin = config.get("pin")
        self.stepper_names = config.getlist("stepper", [])
        self.release_on_move = config.getboolean("release_on_move", True)
        self.engage_on_dwell = config.getboolean("engage_on_dwell", True)
        self.brake_configs = []
        self.initialized = False
        self._toolhead_augmented = False
        self._pin_obj = None  # Reference to output_pin object
        logger.info(f"StepperBrake initialized: name={self.name}, pin={self.pin}, steppers={self.stepper_names}")

        # Register G-code commands immediately
        self._register_gcode_commands()

        # Create output_pin object programmatically
        self._create_output_pin()

        # Store global reference for monkey-patching
        global _stepper_brake_instance
        _stepper_brake_instance = self

        # Patch stepper module immediately (might fail if not loaded yet, that's OK)
        self._patch_stepper_module()

        # Also register event handler to try again after printer is ready as fallback
        self.printer.register_event_handler("klippy:ready", self._on_printer_ready)

    def _create_output_pin(self):
        """Create a GPIO output pin."""
        try:
            ppins = self.printer.lookup_object("pins")
            # Setup as digital output
            self._pin_obj = ppins.setup_pin("digital_out", self.pin)
            logger.debug(f"Created GPIO output pin for {self.pin}")
        except Exception as e:
            raise self.printer.config_error(
                f"Failed to setup GPIO pin {self.pin}: {e}"
            )

    def _on_printer_ready(self):
        """Called when printer is ready. Patch stepper module if not already done."""
        # Try patching again as a fallback in case it wasn't loaded during __init__
        if not getattr(self, '_patched', False):
            self._patch_stepper_module()

    def _patch_stepper_module(self):
        """Monkey-patch the stepper module to integrate our helper registration."""
        try:
            import sys
            # Try to get stepper from sys.modules if already loaded
            if 'stepper' in sys.modules:
                stepper = sys.modules['stepper']
            else:
                # Try importing as klippy submodule
                from klippy import stepper

            original_PrinterStepper = stepper.PrinterStepper

            def patched_PrinterStepper(config, units_in_radians=False):
                # Call original function to create stepper
                mcu_stepper = original_PrinterStepper(config, units_in_radians)

                # Call our register_stepper hook
                stepper_name = mcu_stepper.get_name()
                logger.info(f"Patched PrinterStepper called for {stepper_name}, calling hook")
                if _stepper_brake_instance is not None:
                    _stepper_brake_instance.register_stepper(config, mcu_stepper)
                else:
                    logger.warning("_stepper_brake_instance is None, cannot register stepper")

                return mcu_stepper

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

        # After first stepper is augmented, augment toolhead
        if len(self.brake_configs) == 1:
            try:
                self._augment_toolhead_for_brake_control()
                self._toolhead_augmented = True
                logger.info("Toolhead augmented for brake control")
            except Exception as e:
                logger.warning(f"Could not augment toolhead: {e}")

        # Mark as initialized once we have all configured steppers
        if len(self.brake_configs) >= len(self.stepper_names):
            self.initialized = True
            logger.info(f"Stepper brakes fully initialized with {len(self.brake_configs)} steppers")

    def _augment_stepper_with_brake(self, stepper):
        """Augment a stepper object with brake pin functionality."""

        # Add brake-related attributes to the stepper
        stepper._brake_pin_obj = self._pin_obj
        stepper._brake_engaged = True  # Default to engaged at startup

        # Add method to engage brake
        def engage_brake():
            if not stepper._brake_engaged:
                try:
                    # Use output_pin's set_level() which handles timing internally
                    self._pin_obj.set_level(1)
                    stepper._brake_engaged = True
                    logger.debug(f"Brake engaged for {stepper.get_name()}")
                except Exception as e:
                    logger.warning(f"Failed to engage brake for {stepper.get_name()}: {e}")

        # Add method to release brake
        def release_brake():
            if stepper._brake_engaged:
                try:
                    # Use output_pin's set_level() which handles timing internally
                    self._pin_obj.set_level(0)
                    stepper._brake_engaged = False
                    logger.debug(f"Brake released for {stepper.get_name()}")
                except Exception as e:
                    logger.warning(f"Failed to release brake for {stepper.get_name()}: {e}")

        # Bind methods to stepper
        stepper.engage_brake = engage_brake
        stepper.release_brake = release_brake
        stepper.get_brake_state = lambda: stepper._brake_engaged

        self.brake_configs.append({
            'stepper': stepper,
            'name': stepper.get_name()
        })

    def _augment_toolhead_for_brake_control(self):
        """Augment toolhead to control brakes during movement sequences."""
        toolhead = self.printer.lookup_object("toolhead")

        # Store original move and dwell methods
        original_move = toolhead.move
        original_dwell = toolhead.dwell

        # Create wrapper for move that releases brakes
        def move_with_brake_control(newpos, speed):
            # Release brakes before movement
            if self.release_on_move:
                for config in self.brake_configs:
                    config['stepper'].release_brake()
            # Call original move
            return original_move(newpos, speed)

        # Create wrapper for dwell that engages brakes
        def dwell_with_brake_control(delay):
            # Call original dwell
            result = original_dwell(delay)
            # Engage brakes after movement completes
            if self.engage_on_dwell:
                for config in self.brake_configs:
                    config['stepper'].engage_brake()
            return result

        # Replace methods (monkey patch)
        toolhead.move = move_with_brake_control
        toolhead.dwell = dwell_with_brake_control

    def _register_gcode_commands(self):
        """Register G-code commands for manual brake control."""
        logger.info("_register_gcode_commands called")
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
        logger.info("G-code commands registered successfully")

    def cmd_STEPPER_BRAKE_ENGAGE(self, gcmd):
        """G-code command to engage brake on a specific stepper."""
        if not self.initialized:
            raise gcmd.error("Stepper brakes not yet initialized. Wait a moment and retry.")

        stepper_name = gcmd.get("STEPPER", None)

        if stepper_name is None:
            raise gcmd.error("STEPPER parameter required")

        # Find the stepper in our brake config
        for config in self.brake_configs:
            if config['name'] == stepper_name or config['name'] == f"stepper_{stepper_name}":
                config['stepper'].engage_brake()
                gcmd.respond_info(f"Engaged brake for {config['name']}")
                return

        raise gcmd.error(f"Stepper '{stepper_name}' not found in brake configuration")

    def cmd_STEPPER_BRAKE_RELEASE(self, gcmd):
        """G-code command to release brake on a specific stepper."""
        if not self.initialized:
            raise gcmd.error("Stepper brakes not yet initialized. Wait a moment and retry.")

        stepper_name = gcmd.get("STEPPER", None)

        if stepper_name is None:
            raise gcmd.error("STEPPER parameter required")

        # Find the stepper in our brake config
        for config in self.brake_configs:
            if config['name'] == stepper_name or config['name'] == f"stepper_{stepper_name}":
                config['stepper'].release_brake()
                gcmd.respond_info(f"Released brake for {config['name']}")
                return

        raise gcmd.error(f"Stepper '{stepper_name}' not found in brake configuration")

    def cmd_STEPPER_BRAKE_STATUS(self, gcmd):
        """G-code command to report brake status for all configured steppers."""
        if not self.initialized:
            raise gcmd.error("Stepper brakes not yet initialized. Wait a moment and retry.")

        if not self.brake_configs:
            gcmd.respond_info("No steppers configured for brakes")
            return

        gcmd.respond_info("Stepper brake status:")
        for config in self.brake_configs:
            state = "ENGAGED" if config['stepper'].get_brake_state() else "RELEASED"
            gcmd.respond_info(f"  {config['name']}: {state}")


def load_config_prefix(config):
    return StepperBrake(config)
