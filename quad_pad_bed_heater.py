# Support for a heated bed
#
# Copyright (C) 2018-2019  Kevin O'Connor <kevin@koconnor.net>
#
# This file may be distributed under the terms of the GNU GPLv3 license.

from extras.heater_bed import PrinterHeaterBed
from extras.pid_calibrate import ControlAutoTune

import logging

class QuadPadBedHeater():
    def __init__(self, config):
        self.printer = config.get_printer()
        self.main_bed_heater : PrinterHeaterBed = self.printer.lookup_object(f'heater_bed')
        self.additional_bed_heaters = []
        for i in range(3):
            self.additional_bed_heaters.append(self.printer.load_object(config, f'heater_generic heater_bed{i+1}'))

        # self.printer.register_event_handler("klippy:mcu_identify", self.handle_ready)

        # Register commands
        gcode = self.printer.lookup_object('gcode')

        # for heater_mux, func in gcode.mux_commands["SET_HEATER_TEMPERATURE"][1].items():
        #     if heater_mux == 'heater_bed':
        #         self.prev_set_temp_heater_bed = func
        #         gcode.mux_commands["SET_HEATER_TEMPERATURE"][1]['heater_bed'] = self.cmd_SET_HEATER_TEMPERATURE
        #         break
        self.prev_m140 = gcode.register_command("M140", None)
        self.prev_m190 = gcode.register_command("M190", None)
        self.prev_pid_cal = gcode.register_command("PID_CALIBRATE", None)
        gcode.register_command("M140", self.cmd_M140, desc=self.cmd_m140_help)
        gcode.register_command("M190", self.cmd_M190, desc=self.cmd_m190_help)
        gcode.register_command("PID_CALIBRATE", self.cmd_PID_CALIBRATE, desc=self.cmd_pid_calibrate_help)

    # def cmd_SET_HEATER_TEMPERATURE(self, gcmd, wait=False):
    #     temp = gcmd.get_float('TARGET', 0.)
    #     pheaters = self.printer.lookup_object('heaters')
    #     pheaters.set_temperature(self.main_bed_heater.heater, temp)
    #     for pad in self.additional_bed_heaters:
    #         pheaters.set_temperature(pad, temp)

    cmd_m140_help = 'M140 <S> This command has been superseded by the quad_pad_bed_heater module in order to provide better support for multi-pad bed heating.'
    def cmd_M140(self, gcmd, wait=False):
        # Set Bed Temperature
        temp = gcmd.get_float('S', 0.)
        pheaters = self.printer.lookup_object('heaters')
        for pad in self.additional_bed_heaters:
            pheaters.set_temperature(pad, temp)
        pheaters.set_temperature(self.main_bed_heater.heater, temp, wait)

    cmd_m190_help = 'M190 <S> This command has been superseded by the quad_pad_bed_heater module in order to provide better support for multi-pad bed heating.'
    def cmd_M190(self, gcmd):
        # Set Bed Temperature and Wait
        self.cmd_M140(gcmd, wait=True)

    cmd_pid_calibrate_help = 'PID_CALIBRATE <HEATER> <TARGET> [WRITE_FILE=<0|1>] This command has been superseded by the quad_pad_bed_heater module in order to provide better support for multi-pad bed heating.'
    def cmd_PID_CALIBRATE(self, gcmd):
        heater_name = gcmd.get('HEATER')
        target = gcmd.get_float('TARGET')
        write_file = gcmd.get_int('WRITE_FILE', 0)
        pheaters = self.printer.lookup_object('heaters')
        try:
            heater = pheaters.lookup_heater(heater_name)
        except self.printer.config_error as e:
            raise gcmd.error(str(e))
        self.printer.lookup_object('toolhead').get_last_move_time()
        calibrate = ControlAutoTune(heater, target)
        old_control = heater.set_control(calibrate)
        if heater_name == 'heater_bed':
            for pad in self.additional_bed_heaters:
                exec(f'calibrate{self.additional_bed_heaters.index(pad)} =  ControlAutoTune(pad, target)')
                exec(f'old_control{self.additional_bed_heaters.index(pad)} =  pad.set_control(calibrate{self.additional_bed_heaters.index(pad)})')
        try:
            if heater_name == 'heater_bed':
                for pad in self.additional_bed_heaters:
                    pheaters.set_temperature(pad, target)
            pheaters.set_temperature(heater, target, True)
        except self.printer.command_error as e:
            heater.set_control(old_control)
            if heater_name == 'heater_bed':
                for pad in self.additional_bed_heaters:
                    exec(f'pad.set_control(old_control{self.additional_bed_heaters.index(pad)})')
            raise
        heater.set_control(old_control)
        if heater_name == 'heater_bed':
            for pad in self.additional_bed_heaters:
                exec(f'pad.set_control(old_control{self.additional_bed_heaters.index(pad)})')
        if write_file:
            calibrate.write_file('/tmp/heattest.txt')
        if calibrate.check_busy(0., 0., 0.):
            raise gcmd.error("pid_calibrate interrupted")
        # Log and report results
        Kp, Ki, Kd = calibrate.calc_final_pid()
        logging.info("Autotune: final: Kp=%f Ki=%f Kd=%f", Kp, Ki, Kd)
        gcmd.respond_info(
            "PID parameters: pid_Kp=%.3f pid_Ki=%.3f pid_Kd=%.3f\n"
            "The SAVE_CONFIG command will update the printer config file\n"
            "with these parameters and restart the printer." % (Kp, Ki, Kd))
        # Store results for SAVE_CONFIG
        cfgname = heater.get_name()
        configfile = self.printer.lookup_object('configfile')
        configfile.set(cfgname, 'control', 'pid')
        configfile.set(cfgname, 'pid_Kp', "%.3f" % (Kp,))
        configfile.set(cfgname, 'pid_Ki', "%.3f" % (Ki,))
        configfile.set(cfgname, 'pid_Kd', "%.3f" % (Kd,))

def load_config(config):
    return QuadPadBedHeater(config)
