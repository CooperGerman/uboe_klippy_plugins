import gc
import sys
import copy
import logging
import mcu
from extras.tmc import TMCCommandHelper

from configfile import (
   ConfigWrapper,
   error as ConfigError
)
from toolhead import ToolHead
from stepper import (
	PrinterRail,
 	error as StepperError
)
from kinematics.ratos_hybrid_corexy import RatOSHybridCoreXYKinematics

class UboeTenor:
	def __init__(self, config : ConfigWrapper):
		self.config = config
		self.printer = config.get_printer()
		self.gcode = self.printer.lookup_object('gcode')
		self.ratos = self.printer.lookup_object('ratos')
		self.toolhead = None
		mcu.TRSYNC_TIMEOUT = config.getfloat('trsync_timeout', 0.05, above=0.)
		# motor idling
		self.idle_motor_current_percentage = config.getfloat('idle_motor_current_percentage', 100.0, above=0., below=100.)
		self.woken_up = False
		self.kin_tmc_drivers = {}

		# z offset
		self.z_offset_probe_x_coord = config.getfloat('z_offset_probe_x_coord')
		self.z_offset_probe_y_coord = config.getfloat('z_offset_probe_y_coord')

		# z safeguard
		self.z_safeguard_speed = config.getfloat('z_safeguard_speed', None, above=0.)
		self.z_safeguard_retract_dist = config.getfloat('z_safeguard_retract_dist', None, above=0.)
		self.position_endstop = config.getfloat('z_safeguard_position')
		self.z_safeguard_endstop_pins = config.getlist('z_safeguard_endstop_pins', '')
		# Register event handlers to correctly interact and follow homing (for safeguarding especially)
		self.printer.register_event_handler("homing:homing_move_begin",
                                            self.handle_homing_move_begin)
		self.printer.register_event_handler("homing:homing_move_end",
                                            self.handle_homing_move_end)

		self.kin = None
		self.ratos_homing = None
		self.prev_z_rail : PrinterRail = None
		self.default_zhop = None
		self.safeguard_rail : PrinterRail = None
		self.safeguard_state = None
		self.selected_endstops = "default"

		self.printer.register_event_handler("klippy:mcu_identify", self.handle_ready)
		self.printer.register_event_handler('klippy:connect', self.handle_connect)
		self.printer.register_event_handler("stepper_enable:motor_off", self._motor_off)
		self.gcode.register_command('SET_Z_SAFEGUARDS', self.cmd_set_z_safeguards, desc=self.cmd_set_z_safeguards_help)
		self.gcode.register_command('SET_Z_ENDSTOPS', self.cmd_set_z_endstops, desc=self.cmd_set_z_endstops_help)
		self.gcode.register_command('IDLE_MOTORS', self.cmd_idle_motors, desc=self.cmd_idle_motors_help)
		self.gcode.register_command('WAKE_UP', self.cmd_wake_up, desc=self.cmd_wake_up_help)
		self.gcode.register_command('HEATSOAK', self.cmd_HEATSOAK, desc=self.cmd_heatsoaK_help)
		self.gcode.register_command('ECHO_UBOE_TENOR', self.cmd_echo_uboe_tenor, desc=self.cmd_echo_uboe_tenor_help)

	def _motor_off(self, print_time):
		self.cmd_set_z_safeguards(None)
		if self.safeguard_state:
			self.safeguard_state = None
			logging.info("UBOE : Motor off - Setting machine as unsafeguarded.")

	def handle_homing_move_begin(self, eventtime):
		if self.selected_endstops == "safeguards":
			self.safeguard_state = "started"

	def handle_homing_move_end(self, eventtime):
		if self.selected_endstops == "safeguards":
			self.safeguard_state = "done"

	def _handle_host_temp_sensor(self):
		self.rpi_temp_sensor = self.printer.lookup_object('temperature_sensor raspberry_pi', None)
		self.linux_host_temp_sensor = self.printer.lookup_object('temperature_sensor Linux_Host', None)

		if self.linux_host_temp_sensor and self.rpi_temp_sensor:
			logging.info("UBOE : raspberry_pi Temperature sensor detected aswell as Linux_Host Temperature sensor. Removing rpi sensor.")
			self.printer.objects.pop('temperature_sensor raspberry_pi')
			pheaters = self.printer.load_object(self.config, 'heaters')
			pheaters.available_sensors.pop(pheaters.available_sensors.index('temperature_sensor raspberry_pi'))

	def _get_kin_tmcs(self):
		for stepper in self.kin.get_steppers():
			logging.debug("UBOE : interating on stepper %s" % (stepper.get_name(),))
			self.kin_tmc_drivers.update({stepper.get_name(): {'tmc' : None, 'tmc_helper' : None, 'default_current' : None}})
			for _elem_name, _elem in self.printer.objects.items():
				if _elem_name.startswith('tmc'):
					logging.debug("UBOE : Found driver %s" % (_elem_name,))
					if len(_elem_name.split(' ')) > 1 and _elem_name.split(' ')[1].lower() == stepper.get_name().lower():
						logging.debug("UBOE : Found matching driver %s for stepper %s" % (_elem_name, stepper.get_name()))
						self.kin_tmc_drivers[stepper.get_name()]['tmc'] = _elem
						self.kin_tmc_drivers[stepper.get_name()]['default_current'] = _elem.get_status(0)['run_current']
      # Use gc to find all active TMC current helpers - used for direct stepper current control (code from Happy Hare)
		refcounts = {}
		for obj in gc.get_objects():
			if isinstance(obj, TMCCommandHelper):
				ref_count = sys.getrefcount(obj)
				if hasattr(obj, 'stepper_name'):
					stepper_name = obj.stepper_name
					if stepper_name not in refcounts or ref_count > refcounts[stepper_name]:
						refcounts[stepper_name] = ref_count
						if self.kin_tmc_drivers.get(stepper_name, None):
							self.kin_tmc_drivers[stepper_name]['tmc_helper'] = obj.current_helper
							logging.info("UBOE : Found TMCCommandHelper for %s" % (stepper_name.lower(),))

	def handle_connect(self):
		self._get_kin_tmcs()

	def _is_debug_enabled(self):
		return self.printer.lookup_object('gcode_macro DEBUG_ECHO').get_status(self.toolhead.get_last_move_time)['enabled']

	def handle_ready(self):
		self.toolhead : ToolHead = self.printer.lookup_object('toolhead')
		self.kin : RatOSHybridCoreXYKinematics = self.toolhead.get_kinematics()
		self._handle_host_temp_sensor()

		self.ratos_homing = self.printer.load_object(self.config, 'ratos_homing')
		# save default z config
		self.prev_z_rail = self.kin.rails[2]
		# save default z hop
		self.default_zhop = self.ratos_homing.z_hop
		# set z hop to 0 as long as the machine has not been safeguarded
		self.ratos_homing.z_hop = 0
		# create a new rail with the safeguards as endstops

		self.safeguard_rail = copy.copy(self.prev_z_rail)
		self.safeguard_rail.endstops = []
		self.safeguard_rail.endstop_map = {}
		self.safeguard_rail.position_endstop = self.position_endstop
		if not self.z_safeguard_speed:
			self.z_safeguard_speed = self.prev_z_rail.homing_speed
		if not self.z_safeguard_retract_dist:
			self.z_safeguard_retract_dist = self.prev_z_rail.homing_retract_dist
		steppers = self.safeguard_rail.get_steppers()
		if len(steppers) != len(self.z_safeguard_endstop_pins):
			msg = "UBOE : Mismatch between steppers and endstop pins. When overriding z_safeguard_endstop_pins, ensure the number of pins matches the number of steppers (if no endstop for one stepper simply set empty element)."
			logging.error(msg)
			raise ValueError(msg)
		for stepper in steppers:
			endstop_pin = self.z_safeguard_endstop_pins[steppers.index(stepper)]
			logging.info("UBOE : Registering stepper %s to endstops.", stepper.get_name())
			if self.safeguard_rail.endstops and 'endstop_pin' is '':
				# No endstop defined - use primary endstop
				self.safeguard_rail.endstops[0][0].add_stepper(stepper)
			else:
				logging.info("UBOE : 	- endstop %s", endstop_pin)
				ppins = self.printer.lookup_object('pins')
				pin_params = ppins.parse_pin(endstop_pin, True, True)
				# Normalize pin name
				pin_name = "%s:%s" % (pin_params['chip_name'], pin_params['pin'])
				# Look for already-registered endstop
				endstop = self.safeguard_rail.endstop_map.get(pin_name, None)
				if endstop is None:
					# New endstop, register it
					mcu_endstop = ppins.setup_pin('endstop', endstop_pin)
					self.safeguard_rail.endstop_map[pin_name] = {'endstop': mcu_endstop,
															'invert': pin_params['invert'],
															'pullup': pin_params['pullup']}
					name = stepper.get_name(short=True)
					name = 'safeguard_' + name
					self.safeguard_rail.endstops.append((mcu_endstop, name))
					query_endstops = self.printer.load_object(self.config, 'query_endstops')
					query_endstops.register_endstop(mcu_endstop, name)
				else:
					mcu_endstop = endstop['endstop']
					changed_invert = pin_params['invert'] != endstop['invert']
					changed_pullup = pin_params['pullup'] != endstop['pullup']
					if changed_invert or changed_pullup:
						raise StepperError("Pinter rail %s shared endstop pin %s "
										"must specify the same pullup/invert settings" % (
											self.safeguard_rail.get_name(), pin_name))
				mcu_endstop.add_stepper(stepper)

   	# Homing mechanics
		self.safeguard_rail.homing_speed = self.z_safeguard_speed
		self.safeguard_rail.homing_retract_dist = self.z_safeguard_retract_dist
		self.second_homing_speed = self.config.getfloat(
			'second_z_safeguarding_speed', self.safeguard_rail.homing_speed/2., above=0.)
		self.safeguard_rail.homing_retract_speed = self.config.getfloat(
			'z_safeguard_retract_speed', self.safeguard_rail.homing_speed, above=0.)
		self.safeguard_rail.homing_positive_dir = self.config.getboolean(
			'z_safeguard_positive_dir', None)
		if self.safeguard_rail.homing_positive_dir is None:
			axis_len = self.prev_z_rail.position_max - self.prev_z_rail.position_min
			if self.position_endstop <= self.prev_z_rail.position_min + axis_len / 4.:
					self.safeguard_rail.homing_positive_dir = False
			elif self.position_endstop >= self.prev_z_rail.position_max - axis_len / 4.:
					self.safeguard_rail.homing_positive_dir = True
			else:
					raise ConfigError(
						"Unable to infer homing_positive_dir in section '%s'"
						% (self.config.get_name(),))
			self.config.getboolean('z_safeguard_positive_dir', self.safeguard_rail.homing_positive_dir)
		elif ((self.safeguard_rail.homing_positive_dir
				and self.position_endstop == self.prev_z_rail.position_min)
				or (not self.safeguard_rail.homing_positive_dir
					and self.position_endstop == self.prev_z_rail.position_max)):
			raise ConfigError(
					"Invalid homing_positive_dir / position_endstop in '%s'"
					% (self.config.get_name(),))
		self.woken_up = True

	cmd_idle_motors_help = "Idle the motors by reducing the current to a lower value specified by 'idle_motor_current'. This value should be  just enough to keep the z axis in place."
	def cmd_idle_motors(self, gcmd):
		msg = []
		title = "Idling motors... (idling current : %s%%)" % (self.idle_motor_current_percentage,)
		for stepper, tmc_info in self.kin_tmc_drivers.items():
			run_current = (tmc_info['default_current'] * self.idle_motor_current_percentage) / 100.
			current_helper = tmc_info['tmc_helper']
			if current_helper :
				print_time = self.toolhead.get_last_move_time()
				c = list(current_helper.get_current())
				req_hold_cur, max_cur = c[2], c[3] # Kalico now has 5 elements rather than 4 in tuple, so unpack just what we need...
				new_cur = max(min(run_current, max_cur), 0)
				current_helper.set_current(new_cur, req_hold_cur, print_time)
			else :
				self.gcode.run_script_from_command("SET_TMC_CURRENT STEPPER=%s CURRENT=%.2f" % (stepper, run_current))

			msg.append("tmc : %s, idle_current : %.2f (default : %.2f)" % (stepper, run_current, tmc_info['default_current']))
		# Note all axes as unhomed and unsafeguarded
		self.kin.limits = [(1.0, -1.0)] * 3
		self.safeguard_state = None
		self.cmd_set_z_safeguards(None)
		if self.printer.lookup_object('quad_gantry_level', None) is not None:
			self.printer.lookup_object('quad_gantry_level').z_status.reset()
			msg.append("Resetting QuadGantryLeveling z status")
		if self.printer.lookup_object('z_tilt', None) is not None:
			self.printer.lookup_object('z_tilt').z_status.reset()
			msg.append("Resetting ZTilt z status")
		if self._is_debug_enabled():
			self.ratos.console_echo(title, 'debug', '_N_'.join(msg))
		self.woken_up = False

	cmd_wake_up_help = "Restore the motors to their default current values."
	def cmd_wake_up(self, gcmd):
		title = "Restoring motors to default current..."
		msg = []
		for stepper, tmc_info in self.kin_tmc_drivers.items():
			run_current = tmc_info['default_current']
			current_helper = tmc_info['tmc_helper']
			if current_helper :
				print_time = self.toolhead.get_last_move_time()
				c = list(current_helper.get_current())
				req_hold_cur, max_cur = c[2], c[3] # Kalico now has 5 elements rather than 4 in tuple, so unpack just what we need...
				new_cur = max(min(run_current, max_cur), 0)
				current_helper.set_current(new_cur, req_hold_cur, print_time)
			else :
				self.gcode.run_script_from_command("SET_TMC_CURRENT STEPPER=%s CURRENT=%.2f" % (stepper, run_current))

			msg.append("tmc : %s, idle_current : %.2f (default : %.2f)" % (stepper, run_current, tmc_info['default_current']))

		self.woken_up = True
		if self._is_debug_enabled():
			self.ratos.console_echo(title, 'debug', '_N_'.join(msg))

	cmd_set_z_safeguards_help = "Set the Z-axis safeguards. This command allows you to configure the endstops provided through the 'z_safeguards' list to be set to the z rail."
	def cmd_set_z_safeguards(self, gcmd):
		self.kin.rails[2] = self.safeguard_rail
		self.ratos_homing.z_hop = 0
		if self._is_debug_enabled():
			title = "Set Z rail to safeguards"
			msg = ["Endstops are now: %s" % (self.kin.rails[2].endstops,)]
			msg.append("	- homing direction is : %s" % ("positive" if self.kin.rails[2].homing_positive_dir else "negative"))
			msg.append("	- endstop position is : %s" % (self.kin.rails[2].position_endstop,))
			self.ratos.console_echo(title, 'debug', '_N_'.join(msg))
		self.selected_endstops = "safeguards"

	cmd_set_z_endstops_help = "Revert to default Z axis endstops"
	def cmd_set_z_endstops(self, gcmd):
		self.kin.rails[2] = self.prev_z_rail
		if self._is_debug_enabled():
			title = "Reverted to default Z axis endstops"
			msg = ["Endstops are now: %s" % (self.kin.rails[2].endstops,)]
			msg.append("	- homing direction is : %s" % ("positive" if self.kin.rails[2].homing_positive_dir else "negative"))
			msg.append("	- endstop position is : %s" % (self.kin.rails[2].position_endstop,))
			self.ratos.console_echo(title, 'debug', '_N_'.join(msg))
		self.ratos_homing.z_hop = self.default_zhop
		self.selected_endstops = "default"

	cmd_heatsoaK_help = '''
Iterate over multiple probe commands to analyze heatsoaking effects.
	Usage : HEATSOAKING BED_TEMP=85 NOZZLE_TEMP=150 X_LOCATION=100 Y_LOCATION=100 ITERATIONS=20
	With the following
		optional parameters :
			Where BED_TEMP is the target bed temperature to heat soak at (default 85°C)
			Where NOZZLE_TEMP is the target nozzle temperature to heat soak at (default 150°C)
			Where X_LOCATION is the X coordinate to probe at (default center of the bed)
			Where Y_LOCATION is the Y coordinate to probe at (default center of the bed)
			Where ITERATIONS is the number of iterations to perform (default 20)
'''
	def cmd_HEATSOAK(self, gcmd):
		from datetime import datetime
		measured = {}
		break_on_met_tol = False
		stabilized_at = None
		# Get parameters
		bed_temp = gcmd.get_float('BED_TEMP', 85)
		nozzle_temp = gcmd.get_float('NOZZLE_TEMP', 150)
		x_location = gcmd.get_float('X', (self.kin.axes_max[0] - self.kin.axes_min[0]) / 2.)
		y_location = gcmd.get_float('Y', (self.kin.axes_max[1] - self.kin.axes_min[1]) / 2.)
		action = gcmd.get('ACTION', 'analyze').lower()
		if action == 'analyze':
			iterations = gcmd.get_int('ITERATIONS', 50, minval=0)
		elif action == 'calibrate':
			break_on_met_tol = True
			iterations = 9999 # effectively infinite (no use for while loop to not lock up klipper)
		else :
			raise gcmd.error("Invalid ACTION parameter - must be 'analyze' or 'calibrate'")
		tolerance = gcmd.get_float('TOLERANCE', 0.02, minval=0)
		# First ensure temperature are under 40°C for the bed and 50°C for the nozzle
		self.gcode.run_script_from_command("M106 S255") # Fan at full speed to help cooling down
		self.gcode.run_script_from_command("M140 S40")
		self.gcode.run_script_from_command("M104 S50")
		self.gcode.run_script_from_command("M190 S40")
		self.gcode.run_script_from_command("M109 S50")
		self.gcode.run_script_from_command("M106 S0") # Fan off
		# Home and level the machine
		self.gcode.run_script_from_command("G28")
		self.gcode.run_script_from_command("QUAD_GANTRY_LEVEL")
		title = "Analyzing heatsoaking... (bed temp : %s°C, nozzle temp : %s°C)" % (bed_temp, nozzle_temp)
		self.ratos.console_echo(title, 'info', None)
		# Move to location
		self.gcode.run_script_from_command("G1 X%s Y%s F6000" % (x_location, y_location))
		# Set bed temperature
		self.gcode.run_script_from_command("M140 S%s" % (bed_temp,))
		# Set nozzle temperature
		self.gcode.run_script_from_command("M104 S%s" % (nozzle_temp,))
		# Wait for bed temperature
		self.gcode.run_script_from_command("M190 S%s" % (bed_temp,))
		# Wait for nozzle temperature
		self.gcode.run_script_from_command("M109 S%s" % (nozzle_temp,))
		# start a timer that will later help to determine how long the machine has been heatsoaking
		start_time = datetime.now()
		# Save gcode state
		self.gcode.run_script_from_command("SAVE_GCODE_STATE NAME=before_heatsoaking_st")
		for i in range(iterations):
			# Run bed mesh command
			duration_seconds = (int((datetime.now() - start_time).total_seconds()))
			# Use a profile name that includes the duration of heatsoaking
			# so that we can later analyze the results
			# and see how the mesh evolves over time
			title = "	- Iteration %s : running PROBE for heatsoaking duration of %s seconds" % (i+1, duration_seconds)
			self.ratos.console_echo(title, 'info', None)
			self.gcode.run_script_from_command("PROBE SAMPLES_TOLERANCE_RETRIES=10")
			self.gcode.run_script_from_command("G0 Z5 F6000")
			# save the probed result
			measure = self.printer.lookup_object('probe').get_status(self.toolhead.get_last_move_time())['last_z_result']
			measured.update({str(i): {'measure' : measure, 'duration' : duration_seconds}})
			# If in calibrate mode, check if we have 5 consecutive samples within tolerance and stop if so
			if i >= 4 :
				m1 = measured[str(i)]['measure']
				m2 = measured[str(i-1)]['measure']
				m3 = measured[str(i-2)]['measure']
				m4 = measured[str(i-3)]['measure']
				m5 = measured[str(i-4)]['measure']
				from numpy import std
				if std([m1, m2, m3, m4, m5]) <= tolerance:
					stabilized_at = duration_seconds
					if break_on_met_tol:
						break
		# Restore gcode state
		self.gcode.run_script_from_command("RESTORE_GCODE_STATE NAME=before_heatsoaking_st")
		self.gcode.run_script_from_command("TURN_OFF_HEATERS")
		# Output results
		msg = []
		if action =='analyze':
			title = "Heatsoaking analysis - Results :"
			for i in range(len(measured)):
				msg.append("	- Iteration %s (duration %s seconds) : measured z offset : %s" % (i+1, measured[str(i)]['duration'], measured[str(i)]['measure']))
			self.ratos.console_echo(title, 'info', ('_N_'.join(msg)))
		msg = []
		if stabilized_at:
			title = "Heatsoaking appears to have stabilized after %s iterations over %s seconds (tolerance %.2fmm)." % (i+1, duration_seconds, tolerance)
			msg.append('The SAVE_CONFIG command will update the printer config file')
			configfile = self.printer.lookup_object('configfile')
			configfile.set('[gcode_macro RatOS]', 'variable_bed_heat_soak_time', "%.3f" % (stabilized_at,))
		else:
			title = "Heatsoaking did not stabilize within the provided iterations and tolerance."
			max_duration = measured[str(len(measured)-1)]['duration']
			msg.append("The machine did not stabilize within the provided %s iterations over %s seconds (tolerance %.2fmm)." % (iterations, max_duration, tolerance))
			msg.append("Consider increasing the number of iterations or the tolerance.")
		self.ratos.console_echo(title, 'info', ('_N_'.join(msg)))

	cmd_echo_uboe_tenor_help = "Echo UboeTenor configuration"
	def cmd_echo_uboe_tenor(self, gcmd):
		title = "UboeTenor configuration"
		msg = ["Idle motor configuration:"]
		msg.append("	- idle_motor_current_percentage: %s" % (self.idle_motor_current_percentage,))
		msg.append("Safeguarding configuration:")
		msg.append("	- z_offset_probe_x_coord: %s" % (self.z_offset_probe_x_coord,))
		msg.append("	- z_offset_probe_y_coord: %s" % (self.z_offset_probe_y_coord,))
		msg.append("	- z_safeguard_speed: %s" % (self.z_safeguard_speed,))
		msg.append("	- z_safeguard_retract_dist: %s" % (self.z_safeguard_retract_dist,))
		msg.append("	- z_safeguard_retract_speed: %s" % (self.safeguard_rail.homing_retract_speed,))
		msg.append("	- z_safeguard_positive_dir: %s" % (self.safeguard_rail.homing_positive_dir,))
		msg.append("Current z endstop configuration is %s" % (self.kin.rails[2].endstops,))
		msg.append("	- homing direction is : %s" % ("positive" if self.kin.rails[2].homing_positive_dir else "negative"))
		msg.append("	- endstop position is : %s" % (self.kin.rails[2].position_endstop,))
		msg.append("Is safeguarded : %s" % (self.safeguard_state == "done"))
		msg.append("TRSYNC timeout : %s" % (mcu.TRSYNC_TIMEOUT,))
		msg.append("Woken up : %s" % (self.woken_up,))
		self.ratos.console_echo(title, 'info', ('_N_'.join(msg)))

	# Override to add QUIET option to control console logging from https://github.com/moggieuk/Happy-Hare/blob/76eca598d7301d6e834ed39068e83270d318afff/extras/mmu_machine.py#L1276
	def cmd_SET_PRESSURE_ADVANCE(self, gcmd):
		pressure_advance = gcmd.get_float('ADVANCE', self.pressure_advance, minval=0.)
		smooth_time = gcmd.get_float('SMOOTH_TIME', self.pressure_advance_smooth_time, minval=0., maxval=.200)
		self._set_pressure_advance(pressure_advance, smooth_time)
		msg = "pressure_advance: %.6f\n" "pressure_advance_smooth_time: %.6f" % (pressure_advance, smooth_time)
		self.printer.set_rollover_info(self.name, "%s: %s" % (self.name, msg))
		if not gcmd.get_int('QUIET', 0, minval=0, maxval=1):
			gcmd.respond_info(msg, log=False)

	def get_status(self, evnttime):
		return {
			"safeguard_state": self.safeguard_state,
			"woken_up": self.woken_up,
			"z_offset_probe_x_coord": self.z_offset_probe_x_coord,
			"z_offset_probe_y_coord": self.z_offset_probe_y_coord,
		}

def load_config(config):
	return UboeTenor(config)