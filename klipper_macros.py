# Support for servos
#
# Copyright (C) 2024-2026 Yannick Le Provost <yannick.leprovost@uboe.fr>
#
# This file may be distributed under the terms of the GNU GPLv3 license.
import configparser
import logging
import reactor

# for linting purposes
# import heater_bed
# import save_variables
# import gcode
# import reactor

class klipperMacros:
    '''
    Helper class for klipper macros. It primarily offers more advanced commands
    than jinja constructed ones in klipper macros. (ie while looping etc...)
    '''
    def __init__(self, config):
        self.printer = config.get_printer()
        self.reactor = reactor.Reactor()
        self._trigger_completion = False
        self.gcode = self.printer.lookup_object('gcode')

        # Internal state
        self.min_event_systime = self.reactor.NEVER
        # attributes for temp profiling
        self._iteration_value = 0
        self._autorun = False
        self._missing_temp_profile_displayed = False
        # Register commands and event handlers
        self.printer.register_event_handler("klippy:ready", self._handle_ready)
        self.printer.register_event_handler("klipper_macros:trigger_completion", self._handle_trigger_completion)
        self.gcode.register_command(
            "CONTINUE_SURFACE_TEMP_PROFILE",
            self.cmd_CONTINUE_SURFACE_TEMP_PROFILE,
            desc=self.cmd_CONTINUE_SURFACE_TEMP_PROFILE_help)
        self.gcode.register_command(
            "MAKE_SURFACE_TEMP_PROFILE",
            self.cmd_MAKE_SURFACE_TEMP_PROFILE,
            desc=self.cmd_MAKE_SURFACE_TEMP_PROFILE_help)
        self.gcode.register_command(
            "SAVE_TEMP_PROFILE",
            self.cmd_SAVE_TEMP_PROFILE,
            desc=self.cmd_SAVE_TEMP_PROFILE_help)
        self.gcode.register_command(
            "SET_HEATER_TEMPERATURE_COMPENSATE",
            self.cmd_SET_HEATER_TEMPERATURE_COMPENSATE,
            desc=self.cmd_SET_HEATER_TEMPERATURE_COMPENSATE_help)

        self.prev_SET_PRESSURE_ADVANCE = self.gcode.register_command("SET_PRESSURE_ADVANCE", None)
        self.gcode.register_command(
            "SET_PRESSURE_ADVANCE",
            self.cmd_SET_PRESSURE_ADVANCE,
            desc=self.cmd_SET_PRESSURE_ADVANCE_help)

    def _handle_ready(self):
        self.min_event_systime = self.printer.get_reactor().monotonic() + 2.
        self.bed_heater  = self.printer.lookup_object('heater_bed')
        self.bed_pheaters = self.printer.lookup_object('heaters')
        self.toolhead = self.printer.lookup_object('toolhead')
        try :
            self.th_sensor  = self.printer.lookup_object('toolhead_bed_temp_sensor')
        except :
            self.th_sensor  = None
        self._autorun = self.th_sensor is not None
        self.save_variables = self.printer.lookup_object('save_variables')

    def _handle_trigger_completion(self, gcmd):
        self.min_event_systime = self.printer.get_reactor().monotonic() + 2.
        self._iterate_temps(self._iteration_value+5, gcmd)

    def _go_middle(self, gcmd):
        gcmd.respond_info("Going to middle of bed ...")
        # get bed size from config
        x_max = self.toolhead.get_kinematics().axes_max.x
        x_min = self.toolhead.get_kinematics().axes_min.x
        y_max = self.toolhead.get_kinematics().axes_max.y
        y_min = self.toolhead.get_kinematics().axes_min.y
        speed = self.toolhead.max_velocity * 0.5
        # calculate middle for temp sensor accounting for offset
        x_middle = (x_max - x_min) / 2 + x_min - (self.th_sensor.x_nozzle_to_sensor_offset if self.th_sensor else 0)
        y_middle = (y_max - y_min) / 2 + y_min - (self.th_sensor.y_nozzle_to_sensor_offset if self.th_sensor else 0)
        # move to middle
        self.gcode.run_script_from_command("G0 X%f Y%f F%f" % (x_middle, y_middle, speed) )

    cmd_CONTINUE_SURFACE_TEMP_PROFILE_help = "Continues a surface temperature profile. Usage: CONTINUE_SURFACE_TEMP_PROFILE AUTORUN=<0|1>"
    def cmd_CONTINUE_SURFACE_TEMP_PROFILE(self, gcmd):
        '''
        This command will continue a surface temperature profile for the active surface by
        setting the bed temperature from 40 to max bed temp stepping 5 and saving the
        temperature profile to the active surface starting with latest measured temperature
        entry + 5.
        '''
        # get active sheet from saved variables
        self.save_variables.loadVariables()
        variables = self.save_variables.allVariables

        if not 'bed_surfaces' in variables:
            # run gcode command to initialize bed_surfaces
            gcmd.respond_info("No bed surfaces found. _init_surfaces will be run now!")
            self.gcode.run_script_from_command('_init_surfaces')
            return
        if not 'active' in variables['bed_surfaces']:
            # run gcode command to initialize active surface
            gcmd.respond_info("No active surface found. Please set one with SET_SURFACE_ACTIVE")
            return
        if self._iteration_value != 0:
            # run gcode command to initialize active surface
            gcmd.respond_info("Previous MAKE_SURFACE_TEMP_PROFILE or CONTINUE_SURFACE_TEMP_PROFILE seems to be ongoing. This command should be called alone.")
            return
        active_sheet = variables['bed_surfaces']['active']
        if not 'temp_profile' in variables:
            # run gcode command to initialize active surface
            gcmd.respond_info("No temperature profile found for %s. Please make one with MAKE_SURFACE_TEMP_PROFILE" % active_sheet)
            return
        self._iteration_value = max([t for t in variables['temp_profile'][active_sheet].keys()])+5
        msg = "Continue surface temp profile for %s with autorun = %s" % (active_sheet, self._autorun)
        gcmd.respond_info(msg)
        self._iterate_temps(self._iteration_value+5, gcmd)

    cmd_MAKE_SURFACE_TEMP_PROFILE_help = "Makes a surface temperature profile. Usage: MAKE_SURFACE_TEMP_PROFILE AUTORUN=<0|1>"
    def cmd_MAKE_SURFACE_TEMP_PROFILE(self, gcmd):
        '''
        This command will make a surface temperature profile for the active surface by
        setting the bed temperature from 40 to max bed temp stepping 5 and saving the
        temperature profile to the active surface.
        '''
        # get active sheet from saved variables
        self.save_variables.loadVariables()
        variables = self.save_variables.allVariables

        if not 'bed_surfaces' in variables:
            # run gcode command to initialize bed_surfaces
            gcmd.respond_info("No bed surfaces found. _init_surfaces will be run now!")
            self.gcode.run_script_from_command('_init_surfaces')
            return
        if not 'active' in variables['bed_surfaces']:
            # run gcode command to initialize active surface
            gcmd.respond_info("No active surface found. Please set one with SET_SURFACE_ACTIVE")
            return
        active_sheet = variables['bed_surfaces']['active']
        msg = "Make surface temp profile for %s with autorun = %s" % (active_sheet, self._autorun)
        gcmd.respond_info(msg)
        # home printer first
        self.gcode.run_script_from_command("G28")
        self._iterate_temps(0, gcmd)

    def _iterate_temps(self, iteration_value, gcmd):
        max_temp = int(self.bed_heater.heater.max_temp)
        if iteration_value == 0:
            self._iteration_value = 30
        elif iteration_value >= max_temp:
            self._iteration_value = 0
            return
        else :
            self._iteration_value = iteration_value
        self.gcode.run_script_from_command("M73 P%d" % int(self._iteration_value/max_temp*100))

        self._go_middle(gcmd)
        # set bed temp
        self.bed_pheaters.set_temperature(self.bed_heater.heater, self._iteration_value, True)
        gcmd.respond_info("Waiting for plate temp to stabilize ...")
        if self._autorun :
            # dwell command for 6 minutes
            self.gcode.run_script_from_command("G4 P360000")
            self._save_prfofile(round(self.th_sensor.last_temp, 2), gcmd)
        else :
            # dwell command for 6 minutes
            self.gcode.run_script_from_command("G4 P360000")
            # beep to notify user
            self.gcode.run_script_from_command("M300 P3000")
            # prompt user to save temp profile using SAVE_TEMP_PROFILE MEASURED=<float>
            gcmd.respond_info("Please save the temperature profile using SAVE_TEMP_PROFILE MEASURED=<float>")
        return

    cmd_SAVE_TEMP_PROFILE_help = "Saves the current temperature profile to the active surface. Usage: SAVE_TEMP_PROFILE MEASURED=<float>"
    def cmd_SAVE_TEMP_PROFILE(self, gcmd):
        '''
        This command will save the current temperature profile to the active surface.
        '''
        measured = gcmd.get_float("MEASURED")
        self._save_prfofile(measured, gcmd)

    def _save_prfofile(self, measured, gcmd):
        # get active sheet from saved variables
        self.save_variables
        variables = self.save_variables.allVariables

        if not 'bed_surfaces' in variables:
            # run gcode command to initialize bed_surfaces
            gcmd.respond_info("No bed surfaces found. _init_surfaces will be run now!")
            self.gcode.run_script_from_command('_init_surfaces')
            return
        if not 'active' in variables['bed_surfaces']:
            # run gcode command to initialize active surface
            gcmd.respond_info("No active surface found. Please set one with SET_SURFACE_ACTIVE")
            return
        if not self._iteration_value:
            # run gcode command to initialize active surface
            gcmd.respond_info("You need to be running MAKE_SURFACE_TEMP_PROFILE in order to start saving a temperature profile")
            return
        active_sheet = variables['bed_surfaces']['active']
        msg = "Save surface temp profile for %s" % (active_sheet)
        gcmd.respond_info(msg)
        # get bed settings
        bed_heater  = self.printer.lookup_object('heater_bed')
        # get temperature profile
        # save temperature profile to active surface like this: {'surface_name': {'temp_profile': {40 : 45.2, ..., 100 : 92.3}}}
        if not 'temp_profile' in variables:
            variables['temp_profile'] = {}
        if not active_sheet in variables['temp_profile']:
            variables['temp_profile'][active_sheet] = {}
        if not bed_heater.heater.target_temp in variables['temp_profile'][active_sheet]:
            variables['temp_profile'][active_sheet][bed_heater.heater.target_temp] = measured
        else:
            gcmd.respond_info("Temperature profile for %s at temp %s already exists. Overwriting ..." % (active_sheet, bed_heater.heater.target_temp))
            variables['temp_profile'][active_sheet][bed_heater.heater.target_temp] = measured
        # save variables
        # Write file
        varfile = configparser.ConfigParser()
        varfile.add_section('Variables')
        for name, val in sorted(variables.items()):
            varfile.set('Variables', name, repr(val))
        try:
            f = open(self.save_variables.filename, "w")
            varfile.write(f)
            f.close()
        except:
            msg = "Unable to save variable"
            logging.exception(msg)
            raise gcmd.error(msg)
        self.printer.send_event("klipper_macros:trigger_completion", gcmd)

    cmd_SET_HEATER_TEMPERATURE_COMPENSATE_help = "Trys to apply an offest to the heater target temp if the the heater is in the list of heaters with a temp_profile. Usage: SET_HEATER_TEMPERATURE_COMPENSATE HEATER=<heater> TARGET=<target>"
    def cmd_SET_HEATER_TEMPERATURE_COMPENSATE(self, gcmd):
        '''
        This command will try to apply an offset to the heater target temp if the the heater is in the list of heaters with a temp_profile.
        '''
        # retrieve heater and target temp
        heater = gcmd.get('HEATER')
        target = gcmd.get_float('TARGET')
        # check if heater is bed_heater
        if heater != self.bed_heater.heater.name:
            # gcmd.respond_info("Only %s is supported for now." % self.bed_heater.heater.name)
            self.gcode.run_script_from_command("SET_HEATER_TEMPERATURE HEATER=%s TARGET=%f" % (heater, target))
            return
        else :
            # get active sheet from saved variables
            self.save_variables.loadVariables()
            variables = self.save_variables.allVariables
            if not 'bed_surfaces' in variables:
                # run gcode command to initialize bed_surfaces
                gcmd.respond_info("No bed surfaces found. _init_surfaces will be run now!")
                self.gcode.run_script_from_command('_init_surfaces')
                return
            if not 'active' in variables['bed_surfaces']:
                # run gcode command to initialize active surface
                gcmd.respond_info("No active surface found. Please set one with SET_SURFACE_ACTIVE")
                return
            active_sheet = variables['bed_surfaces']['active']
            if not 'temp_profile' in variables or not active_sheet in variables['temp_profile']:
                # run gcode command to initialize active surface
                if not self._missing_temp_profile_displayed :
                    gcmd.respond_info("No temperature profile found for %s. Not applying any offset." % active_sheet)
                    self._missing_temp_profile_displayed = True
                self.bed_pheaters.set_temperature(self.bed_heater.heater, target)
                return
            else :
                # find closest temp above target in temp_profile
                temp_profile = variables['temp_profile'][active_sheet]
                # closest measured
                closest_m = min(temp_profile.values(), key=lambda x:abs(x-target))
                # firs key with this value in temp_profile
                closest = {k:v for k,v in temp_profile.items() if v == closest_m}
                if not closest:
                    gcmd.respond_info("No temperature profile found for %s at target temp %f. Not applying any offset." % (active_sheet, target))
                    self.bed_pheaters.set_temperature(self.bed_heater.heater, target)
                    return
                # new target is list(closest.keys())[0]
                self.bed_pheaters.set_temperature(self.bed_heater.heater, list(closest.keys())[0])
                gcmd.respond_info("[COMPENSATION] : Compensating from %.2f to %.2f for heater %s" % (target, list(closest.keys())[0], heater))
                return

    # Override to add QUIET option to control console logging from https://github.com/moggieuk/Happy-Hare/blob/76eca598d7301d6e834ed39068e83270d318afff/extras/mmu_machine.py#L1276
    cmd_SET_PRESSURE_ADVANCE_help = "Sets the pressure advance value. Usage: SET_PRESSURE_ADVANCE ADVANCE=<float> SMOOTH_TIME=<float> QUIET=<0|1>"
    def cmd_SET_PRESSURE_ADVANCE(self, gcmd):
        ext_step = self.toolhead.get_extruder().extruder_stepper
        pressure_advance = gcmd.get_float('ADVANCE', ext_step.pressure_advance, minval=0.)
        smooth_time = gcmd.get_float('SMOOTH_TIME', ext_step.pressure_advance_smooth_time, minval=0., maxval=.200)
        ext_step._set_pressure_advance(pressure_advance, smooth_time)
        msg = "pressure_advance: %.6f\n" "pressure_advance_smooth_time: %.6f" % (pressure_advance, smooth_time)
        self.printer.set_rollover_info(ext_step.name, "%s: %s" % (ext_step.name, msg))
        if not gcmd.get_int('QUIET', 1, minval=0, maxval=1):
            gcmd.respond_info(msg, log=False)

def load_config(config):
    return klipperMacros(config)
