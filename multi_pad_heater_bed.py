# Support for a heated bed
#
# Copyright (C) 2018-2019  Kevin O'Connor <kevin@koconnor.net>
#
# This file may be distributed under the terms of the GNU GPLv3 license.

from extras.heater_bed import PrinterHeaterBed
from extras.pid_calibrate import ControlAutoTune

import configparser, logging

MANDATORY_CONFIG_OPTIONS = [
    'heater_pin',
    'sensor_pin',
    'control'
]

class PrinterHeaterBed():
    '''
    Class to wrap all pads into a single PrinterHeaterBed object for mainsail and other utils to hook onto
    '''
    def __init__(self, config):
        self.printer = config.get_printer()
        self.config = config
        self.name = self.config.get_name().split()[1]
        self.pads = []

        if self.config.fileconfig.has_section('heater_bed'):
            raise self.printer.config_error("The [heater_bed] section is not compatible with the multi_pad_heater_bed module. Please remove it from the config file.")
        # Register commands
        gcode = self.printer.lookup_object('gcode')
        gcode.register_command("M140", self.cmd_M140)
        gcode.register_command("M190", self.cmd_M190)
        self.prev_pid_cal = gcode.register_command("PID_CALIBRATE", None)
        gcode.register_command("PID_CALIBRATE", self.cmd_PID_CALIBRATE, desc=self.cmd_pid_calibrate_help)

    cmd_m140_help = 'M140 <S> This command has been superseded by the multi_pad_heater_bed module in order to provide better support for multi-pad bed heating.'
    def cmd_M140(self, gcmd, wait=False):
        all_pads = [pad[1] for pad in self.printer.lookup_objects('multi_pad_heater_bed')]
        # Set Bed Temperature
        temp = gcmd.get_float('S', 0.)
        pheaters = self.printer.lookup_object('heaters')
        for pad in all_pads:
            pheaters.set_temperature(pad.heater, temp, wait)

    cmd_m190_help = 'M190 <S> This command has been superseded by the multi_pad_heater_bed module in order to provide better support for multi-pad bed heating.'
    def cmd_M190(self, gcmd):
        # Set Bed Temperature and Wait
        self.cmd_M140(gcmd, wait=True)

    cmd_pid_calibrate_help = 'PID_CALIBRATE <HEATER> <TARGET> [WRITE_FILE=<0|1>] This command has been superseded by the multi_pad_heater_bed module in order to provide better support for multi-pad bed heating.'
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

    def add_pad(self, config):
        self.pads.append(config)

class MultiPadHeaterBed():
    def __init__(self, config):
        self.printer = config.get_printer()
        self.config = config
        self.name = self.config.get_name().split()[1]
        # We need to retrieve gcode_id and make sure other minimum config options are present
        self.gcode_id = self.config.getint('gcode_id')
        for opt in MANDATORY_CONFIG_OPTIONS:
            if not self.config.fileconfig.has_option(f'multi_pad_heater_bed {self.name}', opt):
                raise self.printer.config_error(f"Missing mandatory config option '{opt}' in section [{self.config.get_name()}]")

        # if a MultiPadHeaterBed already exists in the printer get it to use its config as defaults for new ones
        self.configured_mphb = self.printer.lookup_objects('multi_pad_heater_bed')
        if self.configured_mphb:
            _prev_mphb = self.configured_mphb[0]
            logging.info(f"MultiPadHeaterBed: Found existing MultiPadHeaterBed object ({_prev_mphb[1]}), using its config as defaults for new one.")
            for opt in _prev_mphb[1].config.fileconfig.options(f'multi_pad_heater_bed {_prev_mphb[1].name}'):
                logging.debug(f"MultiPadHeaterBed: Checking if option '{opt}' exists in new MultiPadHeaterBed config...")
                if not self.config.fileconfig.has_option(f'multi_pad_heater_bed {self.name}', opt):
                    self.config.fileconfig.set(f'multi_pad_heater_bed {self.name}', opt, _prev_mphb[1].config.fileconfig.get(f'multi_pad_heater_bed {_prev_mphb[1].name}', opt))
                    logging.debug(f"MultiPadHeaterBed: Option '{opt}' added to new MultiPadHeaterBed config with value '{self.config.fileconfig.get(f'multi_pad_heater_bed {self.name}', opt)}'.")

        pheaters = self.printer.load_object(self.config, 'heaters')
        self.heater = pheaters.setup_heater(self.config, f'B{self.gcode_id}')
        self.get_status = self.heater.get_status
        self.stats = self.heater.stats

def load_config_prefix(config):
    printer = config.get_printer()
    phb = printer.lookup_object('PrinterHeaterBed', None)
    if phb is None:
        phb = PrinterHeaterBed(config)
        printer.add_object('PrinterHeaterBed', phb)
    phb.add_pad(config)

