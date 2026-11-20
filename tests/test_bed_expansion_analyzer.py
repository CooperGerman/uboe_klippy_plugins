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

    def respond_info(self, message):
        self.messages.append(message)

    def run_script_from_command(self, script):
        self.scripts.append(script)


class FakeReactor:
    NOW = 0.
    NEVER = float("inf")

    def __init__(self):
        self.updated_timer = None

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


class BedExpansionMathTests(unittest.TestCase):
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
        plugin.poll_interval = .5
        plugin.park_macro = "PARK"
        plugin.heat_timeout = 100.
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
        plugin.analysis_started_at = 10.
        plugin.phase_started_at = 10.
        plugin.next_scan_at = 130.
        plugin.last_error = None
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

    def test_home_without_leveler_starts_heating(self):
        with tempfile.TemporaryDirectory() as output_dir:
            plugin = self.make_plugin(output_dir)
            plugin.active = True
            plugin.phase = "home"
            plugin.home_started = False
            plugin.level_object = None
            toolhead = SimpleNamespace(
                get_status=lambda eventtime: {"homed_axes": "xyz"})
            plugin.printer = SimpleNamespace(
                lookup_object=lambda name, default=None: (
                    toolhead if name == "toolhead" else default
                ))

            wake_time = plugin._timer_step(1.)

            self.assertEqual(wake_time, FakeReactor.NOW)
            self.assertEqual(plugin.phase, "heating")
            self.assertIn("M140 S85.000", plugin.gcode.scripts)

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
