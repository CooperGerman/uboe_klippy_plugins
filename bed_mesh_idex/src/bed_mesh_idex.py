# Dual toolhead mesh compensation with gantry tilt for IDEX
#
# Copyright (C) 2025  Your Name <your@email.com>
#
# This file may be distributed under the terms of the GNU GPLv3 license.

from configfile import ConfigurationError

class BedMeshIDEX:
    FADE_DISABLE = 0x7FFFFFFF

    def __init__(self, config):
        self.printer = config.get_printer()
        self.gcode_move = self.printer.lookup_object('gcode_move')
        self.quad_gantry = self.printer.lookup_object('quad_gantry_level', None)
        self.bed_mesh = self.printer.lookup_object('bed_mesh', None)
        self.dc_module = self.printer.lookup_object('dual_carriage', None)
        # sanity checks
        if self.bed_mesh:
            raise ConfigurationError("bed_mesh_idex cannot be used with bed_mesh. bed_mesh_idex wraps bed_mesh functionality and will instantiate it automatically.")
        if not self.quad_gantry:
            raise ConfigurationError("bed_mesh_idex requires quad_gantry_level to be enabled. bed_mesh_idex cannot function on non-quad gantry printers for now.")

        # Initialize bed_mesh internally
        self.bed_mesh = self.printer.load_object(config, 'bed_mesh')
        # Register as move transform
        self.mono_toolhead_bed_mesh_transform = self.gcode_move.set_move_transform(self)
        # register gcode commands
        self.printer.register_command('TEST_BED_MESH_IDEX', self.cmd_TEST_BED_MESH_IDEX, desc=self.cmd_TEST_BED_MESH_IDEX_help)

    def move(self, newpos, speed):
        # Called for every move
        # 1. Check if in COPY or MIRROR mode
        if not self.dc_module:
            return newpos, speed
        dc_status = self.dc_module.get_status()
        mode = dc_status.get('carriage_1')
        if mode not in ('COPY', 'MIRROR'):
            self.mono_toolhead_bed_mesh_transform.move(newpos, speed)
            return newpos, speed

        # 2. Get XY for both toolheads
        t0_xy = (newpos[0], newpos[1])
        # TODO: Compute t1_xy based on IDEX offset and mode
        t1_xy = self._get_secondary_toolhead_xy(t0_xy, mode)

        # 3. Get mesh Z at both XYs
        z0 = self.bed_mesh.z_mesh.calc_z(*t0_xy) if self.bed_mesh else 0.0
        z1 = self.bed_mesh.z_mesh.calc_z(*t1_xy) if self.bed_mesh else 0.0

        # 4. Calculate required gantry tilt (plane through both Zs)
        # TODO: Implement actual tilt math
        self._adjust_gantry(z0, z1, t0_xy, t1_xy)

        # 5. Optionally, adjust the move's Z for the primary toolhead
        # (Klipper expects the move to be transformed for the active toolhead)
        new_z = newpos[2] + z0
        newpos = (newpos[0], newpos[1], new_z) + tuple(newpos[3:])

        return newpos, speed

    def _get_secondary_toolhead_xy(self, t0_xy, mode):
        # TODO: Compute the secondary toolhead's XY based on IDEX config and mode
        # For now, just return a dummy offset
        offset_x = 50.0  # Replace with actual offset from config
        if mode == 'COPY':
            return (t0_xy[0] + offset_x, t0_xy[1])
        elif mode == 'MIRROR':
            return (t0_xy[0] - offset_x, t0_xy[1])
        return t0_xy

    def _calculate_x_axis_tilt(self, z0, z1, t0_xy, t1_xy):
        """
        Calculate required X-axis tilt to make nozzle line parallel to bed mesh line.

        Args:
            z0: Bed mesh Z value at primary toolhead position
            z1: Bed mesh Z value at secondary toolhead position
            t0_xy: Primary toolhead XY position
            t1_xy: Secondary toolhead XY position

        Returns:
            tilt_adjustment: Amount to adjust left/right motor groups
        """
        # Calculate the "virtual" line slope created by bed mesh values
        # This represents the ideal nozzle height difference
        mesh_p1 = [t0_xy[0], z0]  # [X, Z] point for primary toolhead
        mesh_p2 = [t1_xy[0], z1]  # [X, Z] point for secondary toolhead

        # Use linefit to get the slope of the bed mesh line
        mesh_slope, mesh_intercept = self.linefit(mesh_p1, mesh_p2)

        # Get current gantry position (assuming level for now)
        # In practice, you'd get this from stepper positions
        current_gantry_z = 0.0  # Replace with actual gantry height

        # Calculate required Z adjustment at each toolhead X position
        required_z0 = self.plot([mesh_slope, mesh_intercept], t0_xy[0])
        required_z1 = self.plot([mesh_slope, mesh_intercept], t1_xy[0])

        # Calculate the tilt adjustment needed
        # Positive means right side needs to go up relative to left side
        x_distance = abs(t1_xy[0] - t0_xy[0])
        z_difference = required_z1 - required_z0

        # Convert to motor adjustments (similar to quad_gantry_level logic)
        # Assuming t0_xy[0] < t1_xy[0] (primary toolhead is on left)
        if t0_xy[0] < t1_xy[0]:
            # Standard case: primary on left, secondary on right
            left_adjust = -z_difference / 2.0
            right_adjust = z_difference / 2.0
        else:
            # Reversed case
            left_adjust = z_difference / 2.0
            right_adjust = -z_difference / 2.0

        return left_adjust, right_adjust

    def linefit(self, p1, p2):
        """
        Calculate line slope and intercept from two points.
        Copied from quad_gantry_level.py
        """
        if p1[1] == p2[1]:
            # Straight line (no slope)
            return 0, p1[1]
        m = (p2[1] - p1[1]) / (p2[0] - p1[0])
        b = p1[1] - m * p1[0]
        return m, b

    def plot(self, f, x):
        """
        Calculate Y value from line equation at given X.
        Copied from quad_gantry_level.py
        """
        return f[0] * x + f[1]

    def _adjust_gantry(self, z0, z1, t0_xy, t1_xy):
        """
        Adjust gantry tilt based on bed mesh differences between toolheads.
        """
        left_adjust, right_adjust = self._calculate_x_axis_tilt(z0, z1, t0_xy, t1_xy)

        # Create adjustment array for quad gantry steppers
        # Assuming stepper order: [front_left, front_right, rear_right, rear_left]
        z_adjust = [left_adjust, right_adjust, right_adjust, left_adjust]

        # Get speed from probe helper or use default
        speed = 5.0  # You might want to make this configurable
        if hasattr(self, 'probe_helper'):
            speed = self.probe_helper.get_lift_speed()

        # Apply the adjustment using quad_gantry's helper
        if self.quad_gantry and hasattr(self.quad_gantry, 'z_helper'):
            self.quad_gantry.z_helper.adjust_steppers(z_adjust, speed)

    cmd_TEST_BED_MESH_IDEX_help = "Test bed mesh IDEX compensation by displaying calculated gantry adjustments for mock positions given in the command."
    def cmd_TEST_BED_MESH_IDEX(self, gcmd):
        """
        GCODE command to test bed mesh IDEX compensation calculations.
        Usage: TEST_BED_MESH_IDEX X0 Y0 X1 Y1
        Where (X0, Y0) are the coordinates for toolhead 0
              (X1, Y1) are the coordinates for toolhead 1
        """
        if not self.dc_module:
            raise gcmd.error("dual_carriage module is required for TEST_BED_MESH_IDEX")
        if not self.quad_gantry:
            raise gcmd.error("quad_gantry_level module is required for TEST_BED_MESH_IDEX")
        if not self.bed_mesh:
            raise gcmd.error("bed_mesh module is required for TEST_BED_MESH_IDEX")

        # check if a bed_mesh is loaded
        if not self.bed_mesh.z_mesh or not self.bed_mesh.z_mesh.mesh:
            raise gcmd.error("No bed mesh loaded. Please load a bed mesh first.")

        try:
            x0 = gcmd.get_float('X0')
            y0 = gcmd.get_float('Y0')
            x1 = gcmd.get_float('X1')
            y1 = gcmd.get_float('Y1')
        except:
            raise gcmd.error("Invalid or missing parameters. Usage: TEST_BED_MESH_IDEX X0 Y0 X1 Y1")

        t0_xy = (x0, y0)
        t1_xy = (x1, y1)

        # Get mesh Z at both XYs
        mesh_z0 = self.bed_mesh.z_mesh.calc_z(*t0_xy) if self.bed_mesh else 0.0
        mesh_z1 = self.bed_mesh.z_mesh.calc_z(*t1_xy) if self.bed_mesh else 0.0

        # Calculate required gantry tilt adjustments
        left_adjust, right_adjust = self._calculate_x_axis_tilt(mesh_z0, mesh_z1, t0_xy, t1_xy)

        # Report results
        gcmd.respond_info("TEST_BED_MESH_IDEX Results:")
        gcmd.respond_info(f"Toolhead 0 Position: X={x0}, Y={y0}, Mesh Z={mesh_z0}")
        gcmd.respond_info(f"Toolhead 1 Position: X={x1}, Y={y1}, Mesh Z={mesh_z1}")
        gcmd.respond_info(f"Calculated Gantry Adjustments: Left Group={left_adjust:.4f}, Right Group={right_adjust:.4f}")

def load_config(config):
    return BedMeshIDEX(config)