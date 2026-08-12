# Support for a heated bed
#
# Copyright (C) 2018-2019  Kevin O'Connor <kevin@koconnor.net>
#
# This file may be distributed under the terms of the GNU GPLv3 license.


from extras.pid_calibrate import ControlAutoTune

import configparser, logging, threading

MANDATORY_CONFIG_OPTIONS = [
    'heater_pin',
    'sensor_pin',
    'control'
]

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
        heater_bed = self.printer.lookup_object('heater_bed', None)
        if heater_bed is not None and heater_bed.pads:
            prev_pad = heater_bed.pads[-1]
            logging.info(f"MultiPadHeaterBed: Found existing MultiPadHeaterBed object ({prev_pad}), using its config as defaults for new one.")
            for opt in prev_pad.config.fileconfig.options(f'multi_pad_heater_bed {prev_pad.name}'):
                logging.debug(f"MultiPadHeaterBed: Checking if option '{opt}' exists in new MultiPadHeaterBed config...")
                if not self.config.fileconfig.has_option(f'multi_pad_heater_bed {self.name}', opt):
                    self.config.fileconfig.set(f'multi_pad_heater_bed {self.name}', opt, prev_pad.config.fileconfig.get(f'multi_pad_heater_bed {prev_pad.name}', opt))
                    logging.debug(f"MultiPadHeaterBed: Option '{opt}' added to new MultiPadHeaterBed config with value '{self.config.fileconfig.get(f'multi_pad_heater_bed {self.name}', opt)}'.")

        pheaters = self.printer.load_object(self.config, 'heaters')
        self.heater = pheaters.setup_heater(self.config, f'B{self.gcode_id}')
        self.get_status = self.heater.get_status
        self.stats = self.heater.stats

class PrinterHeaterBed():
    '''
    Class to wrap all pads into a single PrinterHeaterBed object for mainsail and other utils to hook onto
    '''
    def __init__(self, config):
        self.printer = config.get_printer()
        self.config = config
        self.name = self.config.get_name().split()[1]
        self.short_name = short_name = self.name.split()[-1]
        self.pads = []

        if self.config.fileconfig.has_section('heater_bed'):
            raise self.printer.config_error("The [heater_bed] section is not compatible with the multi_pad_heater_bed module. Please remove it from the config file.")
        self.add_pad(MultiPadHeaterBed(config))

        # Register commands
        gcode = self.printer.lookup_object('gcode')
        gcode.register_command("M140", self.cmd_M140)
        gcode.register_command("M190", self.cmd_M190)
        # self.prev_pid_cal = gcode.register_command("PID_CALIBRATE", None)
        # gcode.register_command("PID_CALIBRATE", self.cmd_PID_CALIBRATE, desc=self.cmd_pid_calibrate_help)

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

    def add_pad(self, multi_pad : MultiPadHeaterBed):
        self.pads.append(multi_pad)
        self.printer.add_object(f'multi_pad_heater_bed {multi_pad.name}', multi_pad)

    def stats(self, eventtime):
        # build mean stats for all pads
        target_temp = []
        last_temp = []
        last_pwm_value = []
        is_active = []
        for pad in self.pads:
            with pad.heater.lock:
                target_temp.append(pad.heater.target_temp)
                last_temp.append(pad.heater.last_temp)
                last_pwm_value.append(pad.heater.last_pwm_value)
                is_active.append(pad.heater or pad.heater.last_temp > 50.)
        return all(is_active), '%s: target=%.0f temp=%.1f pwm=%.3f' % (
            self.short_name, sum(target_temp) / len(target_temp), sum(last_temp) / len(last_temp), sum(last_pwm_value) / len(last_pwm_value))

    def get_status(self, eventtime):
        # build mean status for all pads
        target_temp = []
        last_temp = []
        last_pwm_value = []
        for pad in self.pads:
            with pad.heater.lock:
                target_temp.append(pad.heater.target_temp)
                last_temp.append(pad.heater.last_temp)
                last_pwm_value.append(pad.heater.last_pwm_value)
        smoothed_temp = sum(last_temp) / len(last_temp)
        status = {'temperature': round(smoothed_temp, 2), 'target': sum(target_temp) / len(target_temp),
                'power': sum(last_pwm_value) / len(last_pwm_value)}
        for pad in self.pads:
            status[pad.name] = pad.get_status(eventtime)
        return status

def load_config_prefix(config):
    printer = config.get_printer()
    phb = printer.lookup_object('heater_bed', None)
    if phb is None:
        phb = PrinterHeaterBed(config)
        printer.add_object('heater_bed', phb)
    else :
        phb.add_pad(MultiPadHeaterBed(config))


