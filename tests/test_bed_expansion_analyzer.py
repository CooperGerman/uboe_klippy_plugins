import json
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

import bed_expansion_analyzer


class FakeGcode:
    error = ValueError

    def __init__(self):
        self.messages = []
        self.scripts = []
        self.raw_messages = []

    def respond_info(self, message):
        self.messages.append(message)

    def run_script_from_command(self, script):
        self.scripts.append(script)

    def respond_raw(self, message):
        self.raw_messages.append(message)


class FakeReactor:
    NOW = 0.
    NEVER = float("inf")

    def __init__(self):
        self.updated_timer = None
        self.current_time = 0.

    def monotonic(self):
        return self.current_time

    def update_timer(self, timer, wake_time):
        self.updated_timer = (timer, wake_time)


class FakeZMesh:
    def __init__(self, probed, profile=None):
        self.probed = probed

    def get_probed_matrix(self):
        return self.probed

    def get_mesh_matrix(self):
        return self.probed

    def get_mesh_params(self):
        return {
            "min_x": 10.,
            "max_x": 20.,
            "min_y": 10.,
            "max_y": 20.,
            "x_count": len(self.probed[0]),
            "y_count": len(self.probed),
            "mesh_x_pps": 0,
            "mesh_y_pps": 0,
            "algo": "direct",
            "tension": .2,
        }

    def build_mesh(self, probed):
        self.probed = probed


class LoadCommand:
    error = ValueError

    def __init__(self, **parameters):
        self.parameters = parameters
        self.messages = []

    def get(self, name, default=None):
        return self.parameters.get(name, default)

    def get_int(self, name, default=None, minval=None):
        value = int(self.parameters.get(name, default))
        if minval is not None and value < minval:
            raise ValueError(name)
        return value

    def respond_info(self, message):
        self.messages.append(message)


class MeshCommand(LoadCommand):
    def get_float(self, name, default=None, above=None):
        value = float(self.parameters[name]) if name in self.parameters else default
        if above is not None and value is not None and value <= above:
            raise ValueError(name)
        return value


class BedExpansionMathTests(unittest.TestCase):
    def test_heatsoak_profiles_must_be_temperature_keyed_json_object(self):
        profiles = bed_expansion_analyzer.BedExpansionAnalyzer \
            ._load_heatsoak_profiles(
                '{"85":{"heatsoak_time_s":1200}}', ValueError)
        self.assertEqual(profiles["85"]["heatsoak_time_s"], 1200)
        with self.assertRaisesRegex(ValueError, "keys"):
            bed_expansion_analyzer.BedExpansionAnalyzer \
                ._load_heatsoak_profiles(
                    '{"hot":{"heatsoak_time_s":1200}}', ValueError)

    def test_mesh_delta_preserves_per_point_values(self):
        previous = [[0., .1], [.2, .3]]
        current = [[.02, .09], [.25, .27]]
        delta = bed_expansion_analyzer.mesh_delta(previous, current)
        self.assertEqual(delta, [
            [.02, -.010000000000000009],
            [.04999999999999999, -.02999999999999997],
        ])

    def test_mesh_delta_rejects_shape_changes(self):
        with self.assertRaisesRegex(ValueError, "row counts"):
            bed_expansion_analyzer.mesh_delta([[0.]], [[0.], [0.]])
        with self.assertRaisesRegex(ValueError, "column counts"):
            bed_expansion_analyzer.mesh_delta([[0.]], [[0., 1.]])

    def test_delta_metrics_include_population_stdev_rms_and_max(self):
        metrics = bed_expansion_analyzer.delta_metrics([
            [.01, -.01],
            [.02, -.02],
        ])
        self.assertAlmostEqual(metrics["mean_delta_mm"], 0.)
        self.assertAlmostEqual(metrics["max_abs_delta_mm"], .02)
        self.assertAlmostEqual(
            metrics["stdev_delta_mm"],
            (.001 / 4.) ** .5,
        )
        self.assertAlmostEqual(
            metrics["rms_delta_mm"],
            (.001 / 4.) ** .5,
        )


class BedExpansionStateTests(unittest.TestCase):
    def make_plugin(self, output_dir):
        plugin = bed_expansion_analyzer.BedExpansionAnalyzer.__new__(
            bed_expansion_analyzer.BedExpansionAnalyzer)
        plugin.gcode = FakeGcode()
        plugin.reactor = FakeReactor()
        plugin.timer = object()
        plugin.park_macro = "PARK"
        plugin.rehome_z_before_scan = False
        plugin.render_on_finish = False
        plugin.target_tolerance = .5
        plugin.output_dir = output_dir
        plugin.active = True
        plugin.phase = "scanning"
        plugin.run = {
            "run_id": "run",
            "mode": "calibrate",
            "target_c": 85.,
            "interval_s": 120.,
            "timeout_s": 600.,
            "metric": "max_abs",
            "tolerance_mm": .01,
            "stable_window": 2,
            "created_monotonic": 0.,
        }
        plugin.run_file = os.path.join(output_dir, "run.jsonl")
        plugin.previous_mesh = [[0., 0.], [0., 0.]]
        plugin.latest_metrics = None
        plugin.sample_count = 1
        plugin.stable_count = 1
        plugin.sample_elapsed_times = [10.]
        plugin.analysis_started_at = 10.
        plugin.phase_started_at = 10.
        plugin.next_scan_at = 130.
        plugin.last_error = None
        plugin.config_name = "bed_expansion_analyzer"
        plugin.heatsoak_profiles = {}
        plugin.config_saves = []
        plugin.configfile = SimpleNamespace(
            set=lambda section, option, value: plugin.config_saves.append(
                (section, option, value)))
        plugin._park = lambda: plugin.gcode.scripts.append("PARK")
        plugin._set_bed_target = lambda target: (
            plugin.gcode.scripts.append("M140 S%.3f" % target))
        return plugin

    def test_stable_window_finishes_after_configured_comparisons(self):
        with tempfile.TemporaryDirectory() as output_dir:
            plugin = self.make_plugin(output_dir)
            mesh = FakeZMesh([[.005, -.005], [.009, -.009]])
            plugin.printer = SimpleNamespace(
                lookup_object=lambda name, default=None: (
                    SimpleNamespace(get_mesh=lambda: mesh)
                    if name == "bed_mesh" else default
                ))

            plugin._capture_mesh(20.)

            self.assertFalse(plugin.active)
            self.assertEqual(plugin.phase, "stable")
            self.assertEqual(plugin.stable_count, 2)
            with open(plugin.run_file) as result_file:
                records = [json.loads(line) for line in result_file]
            self.assertEqual(
                records[0]["delta_from_previous"],
                [[.005, -.005], [.009, -.009]],
            )
            self.assertEqual(records[-1]["result"], "stable")
            self.assertIn("PARK", plugin.gcode.scripts)
            self.assertIn("M140 S0.000", plugin.gcode.scripts)
            self.assertEqual(
                plugin.config_saves[0][:2],
                ("bed_expansion_analyzer", "heatsoak_profiles"),
            )
            profile = plugin.heatsoak_profiles["85"]
            self.assertEqual(profile["stable_window"], 2)
            self.assertEqual(profile["stable_window_started_s"], 10.0)

    def test_first_unstable_comparison_resets_stable_window(self):
        with tempfile.TemporaryDirectory() as output_dir:
            plugin = self.make_plugin(output_dir)
            mesh = FakeZMesh([[.011, 0.], [0., 0.]])
            plugin.printer = SimpleNamespace(
                lookup_object=lambda name, default=None: (
                    SimpleNamespace(get_mesh=lambda: mesh)
                    if name == "bed_mesh" else default
                ))

            captured = plugin._capture_mesh(20.)

            self.assertTrue(captured)
            self.assertTrue(plugin.active)
            self.assertEqual(plugin.stable_count, 0)
            self.assertEqual(plugin.phase, "interval")

    def test_failure_elapsed_time_uses_current_monotonic_time(self):
        with tempfile.TemporaryDirectory() as output_dir:
            plugin = self.make_plugin(output_dir)
            plugin.run_file = os.path.join(output_dir, "run.jsonl")
            plugin.reactor.current_time = 310.
            plugin.analysis_started_at = 100.

            plugin._finish_run(25., "failed", "mesh failure")

            with open(plugin.run_file) as result_file:
                records = [json.loads(line) for line in result_file]
            self.assertEqual(records[-1]["elapsed_s"], 210.)

    def test_render_completed_run_displays_console_image(self):
        with tempfile.TemporaryDirectory() as output_dir:
            plugin = self.make_plugin(output_dir)
            plugin.render_mode = "baseline"
            plugin.render_view = "surface"
            plugin.printer_data_dir = "/home/uboe/printer_data"
            output_file = (
                "/home/uboe/printer_data/logs/bed_expansion/run.gif")
            plugin.render_executor = SimpleNamespace(
                submit=lambda function, run_file: output_file)

            plugin._render_finished_run()

            self.assertEqual(len(plugin.gcode.raw_messages), 1)
            message = plugin.gcode.raw_messages[0]
            self.assertIn(
                "../server/files/logs/bed_expansion/run.gif", message)
            self.assertIn("<img", message)

    def test_preparation_uses_synchronous_klipper_commands(self):
        with tempfile.TemporaryDirectory() as output_dir:
            plugin = self.make_plugin(output_dir)
            plugin.active = True
            plugin.phase = "home"
            toolhead = SimpleNamespace(
                get_status=lambda eventtime: {"homed_axes": ""})
            bed_mesh = SimpleNamespace(
                bmc=SimpleNamespace(probe_mgr=SimpleNamespace(
                    probe_helper=SimpleNamespace(speed=100.))),
                get_mesh=lambda: FakeZMesh([[0., 0.], [0., 0.]]))
            heater = SimpleNamespace(get_temp=lambda eventtime: (85., 85.))
            heaters = SimpleNamespace(lookup_heater=lambda name: heater)
            plugin.printer = SimpleNamespace(
                lookup_object=lambda name, default=None: (
                    toolhead if name == "toolhead"
                    else bed_mesh if name == "bed_mesh"
                    else heaters if name == "heaters"
                    else default
                ))
            plugin.run.update({
                "mesh_command": "BED_MESH_CALIBRATE",
                "speed": None,
                "probe_count": None,
                "mesh_min": None,
                "mesh_max": None,
            })
            plugin._find_leveler = lambda: (
                SimpleNamespace(), "QUAD_GANTRY_LEVEL")
            plugin._bed_heater = lambda: heater
            plugin.previous_mesh = None
            plugin.stable_count = 0

            wake_time = plugin._timer_step(1.)

            self.assertEqual(wake_time, 120.)
            self.assertEqual(plugin.phase, "interval")
            self.assertEqual(plugin.gcode.scripts[:5], [
                "G28 X Y Z",
                "QUAD_GANTRY_LEVEL",
                "M140 S85.000",
                "TEMPERATURE_WAIT SENSOR=heater_bed MINIMUM=84.500",
                "BED_MESH_CALIBRATE "
                "PROFILE=__bed_expansion_analysis__",
            ])

    def test_mesh_parameters_override_defaults(self):
        plugin = self.make_plugin(".")
        plugin.mesh_command = "BED_MESH_CALIBRATE"
        plugin.default_mesh_speed = 75.
        plugin.default_probe_count = (20, 20)
        plugin.default_mesh_min = (10., 10.)
        plugin.default_mesh_max = (200., 200.)

        parameters = plugin._mesh_parameters(MeshCommand(
            SPEED="40",
            PROBE_COUNT="8,10",
            MESH_MIN="20,25",
            MESH_MAX="180,190",
            MESH_COMMAND="MY_BED_MESH",
        ))

        self.assertEqual(parameters, {
            "mesh_command": "MY_BED_MESH",
            "speed": 40.,
            "probe_count": (8, 10),
            "mesh_min": (20., 25.),
            "mesh_max": (180., 190.),
        })

    def test_mesh_scan_uses_overrides_without_retaining_speed(self):
        with tempfile.TemporaryDirectory() as output_dir:
            plugin = self.make_plugin(output_dir)
            plugin.run.update({
                "mesh_command": "BED_MESH_CALIBRATE METHOD=rapid",
                "speed": 60.,
                "probe_count": (10, 12),
                "mesh_min": (20., 25.),
                "mesh_max": (500., 550.),
            })
            observed_speeds = []

            class ProbeManager:
                def __init__(self):
                    self.probe_helper = SimpleNamespace(speed=100.)

            probe_manager = ProbeManager()
            bed_mesh = SimpleNamespace(
                bmc=SimpleNamespace(probe_mgr=probe_manager),
                get_mesh=lambda: FakeZMesh([[0., 0.], [0., 0.]]))
            plugin.printer = SimpleNamespace(
                lookup_object=lambda name: bed_mesh)
            plugin.gcode.run_script_from_command = (
                lambda script: (
                    observed_speeds.append(
                        (script, probe_manager.probe_helper.speed)),
                    setattr(plugin.reactor, "current_time", 55.),
                ))

            plugin._start_mesh_scan(10.)

            self.assertEqual(probe_manager.probe_helper.speed, 100.)
            self.assertEqual(observed_speeds, [(
                "BED_MESH_CALIBRATE METHOD=rapid "
                "PROFILE=__bed_expansion_analysis__ "
                "SCAN_SPEED=60.0 PROBE_COUNT=10,12 "
                "MESH_MIN=20.0,25.0 MESH_MAX=500.0,550.0",
                60.,
            )])
            self.assertEqual(plugin.next_scan_at, 175.)

    def test_mesh_scan_without_overrides_uses_configured_command_only(self):
        with tempfile.TemporaryDirectory() as output_dir:
            plugin = self.make_plugin(output_dir)
            plugin.run.update({
                "mesh_command": "BED_MESH_CALIBRATE",
                "speed": None,
                "probe_count": None,
                "mesh_min": None,
                "mesh_max": None,
            })
            bed_mesh = SimpleNamespace(
                bmc=SimpleNamespace(probe_mgr=SimpleNamespace(
                    probe_helper=SimpleNamespace(speed=100.))),
                get_mesh=lambda: FakeZMesh([[0., 0.], [0., 0.]]))
            plugin.printer = SimpleNamespace(
                lookup_object=lambda name: bed_mesh)

            plugin._start_mesh_scan(10.)

            self.assertEqual(
                plugin.gcode.scripts[:1],
                ["BED_MESH_CALIBRATE PROFILE=__bed_expansion_analysis__"],
            )

    def test_mesh_scan_rehomes_z_immediately_before_scanning(self):
        with tempfile.TemporaryDirectory() as output_dir:
            plugin = self.make_plugin(output_dir)
            plugin.run.update({
                "mesh_command": "BED_MESH_CALIBRATE",
                "speed": None,
                "probe_count": None,
                "mesh_min": None,
                "mesh_max": None,
            })
            plugin.rehome_z_before_scan = True
            plugin.reactor.current_time = 10.
            bed_mesh = SimpleNamespace(
                bmc=SimpleNamespace(probe_mgr=SimpleNamespace(
                    probe_helper=SimpleNamespace(speed=100.))),
                get_mesh=lambda: FakeZMesh([[0., 0.], [0., 0.]]))
            plugin.printer = SimpleNamespace(
                lookup_object=lambda name: bed_mesh)

            plugin._start_mesh_scan(10.)

            self.assertEqual(
                plugin.gcode.scripts[:3],
                [
                    "BED_MESH_CLEAR",
                    "G28 Z",
                    "BED_MESH_CALIBRATE "
                    "PROFILE=__bed_expansion_analysis__",
                ],
            )
            self.assertEqual(plugin.phase, "stable")
            self.assertEqual(plugin.active, False)

    def test_load_restores_saved_sample_and_optional_profile(self):
        with tempfile.TemporaryDirectory() as output_dir:
            filename = "saved_run.jsonl"
            sample = {
                "type": "sample",
                "sample_index": 3,
                "probed_matrix": [[.1, .2], [.3, .4]],
                "mesh_params": {
                    "min_x": 10.,
                    "max_x": 20.,
                    "min_y": 10.,
                    "max_y": 20.,
                    "x_count": 2,
                    "y_count": 2,
                    "mesh_x_pps": 0,
                    "mesh_y_pps": 0,
                    "algo": "direct",
                    "tension": .2,
                },
            }
            with open(os.path.join(output_dir, filename), "w") as output:
                output.write(json.dumps(sample) + "\n")

            class FakeBedMesh:
                def __init__(self):
                    self.mesh = None
                    self.profiles = []

                def set_mesh(self, mesh):
                    self.mesh = mesh

                def save_profile(self, name):
                    self.profiles.append(name)

            class FakeBedMeshError(Exception):
                pass

            bed_mesh = FakeBedMesh()
            plugin = bed_expansion_analyzer.BedExpansionAnalyzer.__new__(
                bed_expansion_analyzer.BedExpansionAnalyzer)
            plugin.output_dir = output_dir
            plugin.printer = SimpleNamespace(
                lookup_object=lambda name: bed_mesh)
            command = LoadCommand(
                RUN=filename, SAMPLE="3", PROFILE="heatsoak_85c")
            fake_module = SimpleNamespace(
                ZMesh=FakeZMesh, BedMeshError=FakeBedMeshError)

            with mock.patch.object(
                bed_expansion_analyzer.importlib,
                "import_module",
                return_value=fake_module,
            ):
                plugin.cmd_BED_EXPANSION_LOAD(command)

            self.assertEqual(
                bed_mesh.mesh.get_probed_matrix(),
                [[.1, .2], [.3, .4]],
            )
            self.assertEqual(bed_mesh.profiles, ["heatsoak_85c"])


if __name__ == "__main__":
    unittest.main()
