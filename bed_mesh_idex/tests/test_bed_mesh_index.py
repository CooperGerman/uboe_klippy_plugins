import unittest
from src.bed_mesh_index import BedMeshIndex

class TestBedMeshIndex(unittest.TestCase):

    def setUp(self):
        self.bed_mesh_index = BedMeshIndex()

    def test_add_toolhead(self):
        self.bed_mesh_index.add_toolhead('toolhead_1', z_offset=0.5)
        self.bed_mesh_index.add_toolhead('toolhead_2', z_offset=1.0)
        self.assertEqual(len(self.bed_mesh_index.toolheads), 2)

    def test_get_z_offset(self):
        self.bed_mesh_index.add_toolhead('toolhead_1', z_offset=0.5)
        self.bed_mesh_index.add_toolhead('toolhead_2', z_offset=1.0)
        self.assertEqual(self.bed_mesh_index.get_z_offset('toolhead_1'), 0.5)
        self.assertEqual(self.bed_mesh_index.get_z_offset('toolhead_2'), 1.0)

    def test_tilt_gantry(self):
        self.bed_mesh_index.add_toolhead('toolhead_1', z_offset=0.5)
        self.bed_mesh_index.add_toolhead('toolhead_2', z_offset=1.0)
        angle = self.bed_mesh_index.calculate_gantry_tilt()
        self.assertIsInstance(angle, float)

    def test_tilt_gantry_with_invalid_toolhead(self):
        with self.assertRaises(ValueError):
            self.bed_mesh_index.calculate_gantry_tilt('invalid_toolhead')

if __name__ == '__main__':
    unittest.main()