import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

import numpy

import uboe_tenor


class Command:
	def __init__(self, mode):
		self.mode = mode
		self.messages = []

	def get(self, name):
		assert name == 'MODE'
		return self.mode

	def error(self, message):
		return ValueError(message)

	def respond_info(self, message):
		self.messages.append(message)


class Tester:
	def __init__(self, path):
		self.path = path

	def save_calibration_data(self, *args, **kwargs):
		return self.path


class Printer:
	command_error = ValueError

	def __init__(self, tester):
		self.tester = tester
		self.mode = 'INACTIVE'

	def lookup_object(self, name):
		if name == 'resonance_tester':
			return self.tester
		if name == 'dual_carriage':
			return SimpleNamespace(get_status=lambda: {'carriage_1': self.mode})
		raise KeyError(name)

	def get_reactor(self):
		return SimpleNamespace(monotonic=lambda: 1.)


class ShaperTests(unittest.TestCase):
	def setUp(self):
		self.tmp = tempfile.TemporaryDirectory()
		self.addCleanup(self.tmp.cleanup)
		self.path = os.path.join(self.tmp.name, 'input_shaping.cfg')
		self.csv = os.path.join(self.tmp.name, 'measurement.csv')
		with open(self.path, 'w') as f:
			f.write('[unrelated]\noption: keep\n\n[gcode_macro RatOS]\n'
					'variable_shaper_x_type: ["ei"] # keep comment\n'
					'variable_shaper_x_freq: [1]\n'
					'variable_shaper_y_type: ["ei"]\n'
					'variable_shaper_y_freq: [1]\n')
		with open(self.csv, 'w') as f:
			f.write('freq,psd_x,psd_y,psd_z,psd_xyz\n'
					'10,1,1,1,3\n20,2,2,2,6\n30,1,1,1,3\n')
		self.tester = Tester(self.csv)
		self.plugin = uboe_tenor.UboeTenor.__new__(uboe_tenor.UboeTenor)
		self.plugin.shaper_config_path = self.path
		self.plugin.shaper_capture = None
		self.plugin.printer = Printer(self.tester)
		self.plugin.toolhead = SimpleNamespace(
			get_status=lambda eventtime: {'square_corner_velocity': 5.})
		self.helper = SimpleNamespace(numpy=numpy)
		self.axis = lambda name: SimpleNamespace(get_name=lambda: name)
		self.addCleanup(self.plugin._stop_shaper_capture)

	def command(self, mode):
		command = Command(mode)
		self.plugin.cmd_UBOE_SHAPER_CAPTURE(command)
		return command

	def capture(self, mode):
		for tool in ('t0', 't1'):
			for axis in ('x', 'y'):
				name = tool if mode == 'single' else tool + '_' + mode
				self.tester.save_calibration_data('resonances', name, self.helper,
												  self.axis(axis), None)

	def test_complete_run_averages_and_preserves_backup(self):
		with open(self.path) as f:
			original = f.read()
		self.command('start')
		self.command('single')
		self.capture('single')
		self.plugin.printer.mode = 'COPY'
		self.command('copy')
		self.capture('copy')
		self.plugin.printer.mode = 'MIRROR'
		self.command('mirror')
		self.capture('mirror')
		result_freqs = iter([40.0, 42.0, 30.1, 33.2, 45., 47.,
							 50., 52., 60., 64., 70., 72.])
		fitter = SimpleNamespace(
			find_best_shaper=lambda *args, **kwargs: (None, [SimpleNamespace(name='mzv', freq=next(result_freqs))]),
			find_shaper_max_accel=lambda shaper, scv: 3000.)
		with mock.patch.object(uboe_tenor.shaper_calibrate, 'ShaperCalibrate', return_value=fitter):
			command = self.command('finish')
		with open(self.path) as f:
			text = f.read()
		self.assertIn("variable_shaper_x_freq: [40.0, 42.0, 31.65, 46.0]", text)
		self.assertIn("variable_shaper_y_freq: [50.0, 52.0, 62.0, 71.0]", text)
		self.assertIn("variable_shaper_x_type: ['mzv', 'mzv', 'mzv', 'mzv'] # keep comment", text)
		self.assertIn('[unrelated]\noption: keep', text)
		backups = [name for name in os.listdir(self.tmp.name) if name.endswith('.bak')]
		self.assertEqual(len(backups), 1)
		with open(os.path.join(self.tmp.name, backups[0])) as f:
			self.assertEqual(f.read(), original)
		self.assertTrue(any('Restart Klipper' in message for message in command.messages))
		self.assertIsNone(self.plugin.shaper_capture)

	def test_missing_or_duplicate_measurements_fail_without_writing(self):
		with open(self.path) as f:
			original = f.read()
		self.command('start')
		self.command('single')
		self.tester.save_calibration_data('resonances', 't0', self.helper, self.axis('x'), None)
		with self.assertRaisesRegex(ValueError, 'Duplicate'):
			self.tester.save_calibration_data('resonances', 't0', self.helper, self.axis('x'), None)
		with self.assertRaisesRegex(ValueError, 'Missing'):
			self.command('copy')
		with self.assertRaisesRegex(ValueError, 'Incomplete'):
			self.command('finish')
		with open(self.path) as f:
			self.assertEqual(f.read(), original)
		self.command('abort')
		self.assertIsNone(self.plugin.shaper_capture)

	def test_wrong_carriage_and_invalid_csv_are_rejected(self):
		self.command('start')
		self.plugin.printer.mode = 'COPY'
		with self.assertRaisesRegex(ValueError, 'carriage'):
			self.command('single')
		self.plugin.printer.mode = 'INACTIVE'
		self.command('single')
		with open(self.csv, 'w') as f:
			f.write('freq,psd_x,psd_y,psd_z,psd_xyz\n10,0,0,0,0\n20,0,0,0,0\n')
		with self.assertRaisesRegex(ValueError, 'invalid or empty'):
			self.tester.save_calibration_data('resonances', 't0', self.helper, self.axis('x'), None)

	def test_incomplete_config_is_not_overwritten(self):
		with open(self.path, 'w') as f:
			f.write('[gcode_macro RatOS]\nvariable_shaper_x_freq: [1]\n')
		with self.assertRaisesRegex(ValueError, 'all four'):
			self.plugin._write_shaper_config({
				'variable_shaper_x_freq': '[2]', 'variable_shaper_y_freq': '[2]',
				'variable_shaper_x_type': "['mzv']", 'variable_shaper_y_type': "['mzv']"})
		with open(self.path) as f:
			self.assertEqual(f.read(), '[gcode_macro RatOS]\nvariable_shaper_x_freq: [1]\n')


if __name__ == '__main__':
	unittest.main()
