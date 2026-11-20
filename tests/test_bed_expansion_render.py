import json
import os
import tempfile
import unittest

import bed_expansion_render


class BedExpansionRenderTests(unittest.TestCase):
    def samples(self):
        return [
            {
                "type": "sample",
                "sample_index": 0,
                "elapsed_s": 0.,
                "probed_matrix": [[0., .1], [.2, .3]],
                "delta_from_previous": None,
                "metrics": None,
                "mesh_params": {
                    "min_x": 10., "max_x": 20.,
                    "min_y": 10., "max_y": 20.,
                },
            },
            {
                "type": "sample",
                "sample_index": 1,
                "elapsed_s": 120.,
                "probed_matrix": [[.02, .08], [.25, .28]],
                "delta_from_previous": [[.02, -.02], [.05, -.02]],
                "metrics": {"max_abs_delta_mm": .05},
                "mesh_params": {
                    "min_x": 10., "max_x": 20.,
                    "min_y": 10., "max_y": 20.,
                },
            },
        ]

    def test_load_samples_rejects_too_few_meshes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "run.jsonl")
            with open(path, "w") as output:
                output.write(json.dumps({"type": "run"}) + "\n")
            with self.assertRaisesRegex(ValueError, "two bed-mesh"):
                bed_expansion_render.load_samples(path)

    def test_prepare_frames_supports_each_mode(self):
        samples = self.samples()
        baseline = bed_expansion_render.prepare_frames(
            samples, "baseline")
        previous = bed_expansion_render.prepare_frames(
            samples, "previous")
        absolute = bed_expansion_render.prepare_frames(
            samples, "absolute")

        self.assertAlmostEqual(float(baseline[1, 0, 0]), .02)
        self.assertAlmostEqual(float(previous[1, 1, 0]), .05)
        self.assertAlmostEqual(float(absolute[0, 1, 1]), .3)
        self.assertAlmostEqual(float(previous[0, 0, 0]), 0.)

    def test_render_writes_nonempty_gif(self):
        with tempfile.TemporaryDirectory() as directory:
            output = os.path.join(directory, "run.gif")
            rendered = bed_expansion_render.render_gif(
                self.samples(), output, mode="baseline", fps=2., dpi=40)
            self.assertEqual(rendered, output)
            with open(output, "rb") as result:
                self.assertEqual(result.read(6), b"GIF89a")
            self.assertGreater(os.path.getsize(output), 1000)


if __name__ == "__main__":
    unittest.main()
