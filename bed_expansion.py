import datetime
import html
import importlib
import json
import logging
import math
import os
import subprocess
import sys
import threading
from urllib.parse import quote


VALID_METRICS = ("max_abs", "stdev", "rms")


def mesh_delta(previous, current):
    if len(previous) != len(current):
        raise ValueError("bed meshes have different row counts")
    delta = []
    for previous_row, current_row in zip(previous, current):
        if len(previous_row) != len(current_row):
            raise ValueError("bed meshes have different column counts")
        delta.append([
            float(current_value) - float(previous_value)
            for previous_value, current_value
            in zip(previous_row, current_row)
        ])
    return delta


def delta_metrics(delta):
    values = [value for row in delta for value in row]
    if not values:
        raise ValueError("cannot analyze an empty bed mesh delta")
    mean = sum(values) / len(values)
    max_abs = max(abs(value) for value in values)
    variance = sum((value - mean) ** 2 for value in values) / len(values)
    rms = math.sqrt(sum(value ** 2 for value in values) / len(values))
    return {
        "mean_delta_mm": mean,
        "max_abs_delta_mm": max_abs,
        "stdev_delta_mm": math.sqrt(variance),
        "rms_delta_mm": rms,
    }


class BedExpansion:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.gcode = self.printer.lookup_object("gcode")
        self.config_name = config.get_name()
        self.configfile = self.printer.lookup_object("configfile")

        self.output_dir = os.path.abspath(os.path.expanduser(
            config.get("output_dir",
                       "~/printer_data/logs/bed_expansion")))
        self.default_interval = config.getfloat(
            "interval", 120., above=0.)
        self.default_duration = config.getfloat(
            "duration", 1800., above=0.)
        self.default_timeout = config.getfloat(
            "timeout", 7200., above=0.)
        self.default_tolerance = config.getfloat(
            "tolerance", .01, minval=0.)
        self.default_metric = self._parse_metric(
            config.get("metric", "max_abs"), config.error)
        self.default_stable_window = config.getint(
            "stable_window", 5, minval=1)
        self.mesh_command = config.get(
            "mesh_command", "BED_MESH_CALIBRATE").strip()
        if not self.mesh_command or "\n" in self.mesh_command:
            raise config.error(
                "mesh_command must be a single non-empty G-code command")
        self.default_mesh_speed = config.getfloat(
            "mesh_speed", None, above=0.)
        self.default_probe_count = self._config_pair(
            config, "mesh_probe_count", int, 3)
        self.default_mesh_min = self._config_pair(
            config, "mesh_min", float)
        self.default_mesh_max = self._config_pair(
            config, "mesh_max", float)
        self.park_macro = config.get("park_macro", "PARK").strip()
        self.rehome_z_before_scan = config.getboolean(
            "rehome_z_before_scan", True)
        self.relevel_before_scan = config.getboolean(
            "relevel_before_scan", True)
        self.mesh_reject_tolerance = config.getfloat(
            "mesh_reject_tolerance", .1, minval=0.)
        self.mesh_retries = config.getint(
            "mesh_retries", 1, minval=0)
        self.printer_data_dir = self._printer_data_dir(
            self.printer.get_start_args())
        self.render_on_finish = config.getboolean(
            "render_on_finish", True)
        self.render_mode = config.get(
            "render_mode", "baseline").strip().lower()
        self.render_view = config.get(
            "render_view", "surface").strip().lower()
        if self.render_mode not in ("absolute", "baseline", "previous"):
            raise config.error(
                "render_mode must be absolute, baseline, or previous")
        if self.render_view not in ("heatmap", "surface"):
            raise config.error("render_view must be heatmap or surface")
        self.render_fps = config.getfloat(
            "render_fps", 1., above=0.)
        self.render_dpi = config.getint(
            "render_dpi", 80, minval=20)
        self.render_z_exaggeration = config.getfloat(
            "render_z_exaggeration", 2., above=0.)
        self.render_data_source = config.get(
            "render_data_source", "mesh").strip().lower()
        if self.render_data_source not in ("mesh", "probed"):
            raise config.error(
                "render_data_source must be mesh or probed")
        self.heatsoak_profiles = self._load_heatsoak_profiles(
            config.get("heatsoak_profiles", "{}"), config.error)
        self.save_wait_profile = config.getboolean(
            "save_wait_profile", True)

        self.timer = self.reactor.register_timer(
            self._timer_callback, self.reactor.NEVER)
        self.active = False
        self.phase = "idle"
        self.run = None
        self.run_file = None
        self.previous_mesh = None
        self.latest_metrics = None
        self.sample_count = 0
        self.stable_count = 0
        self.sample_elapsed_times = []
        self.last_valid_z_mesh = None
        self.last_mesh_rejected = False
        self.last_error = None
        self.analysis_started_at = None
        self.phase_started_at = None
        self.next_scan_at = None

        self.printer.register_event_handler(
            "klippy:shutdown", self._handle_shutdown)
        self.gcode.register_command(
            "BED_EXPANSION_ANALYZE", self.cmd_BED_EXPANSION_ANALYZE,
            desc=self.cmd_BED_EXPANSION_ANALYZE_help)
        self.gcode.register_command(
            "BED_EXPANSION_CALIBRATE", self.cmd_BED_EXPANSION_CALIBRATE,
            desc=self.cmd_BED_EXPANSION_CALIBRATE_help)
        self.gcode.register_command(
            "BED_EXPANSION_WAIT", self.cmd_BED_EXPANSION_WAIT,
            desc=self.cmd_BED_EXPANSION_WAIT_help)
        self.gcode.register_command(
            "BED_EXPANSION_STATUS", self.cmd_BED_EXPANSION_STATUS,
            desc=self.cmd_BED_EXPANSION_STATUS_help)
        self.gcode.register_command(
            "BED_EXPANSION_LOAD", self.cmd_BED_EXPANSION_LOAD,
            desc=self.cmd_BED_EXPANSION_LOAD_help)

    @staticmethod
    def _parse_metric(metric, error):
        normalized = str(metric).strip().lower()
        aliases = {
            "max": "max_abs",
            "max_abs_delta": "max_abs",
            "standard_deviation": "stdev",
            "stddev": "stdev",
        }
        normalized = aliases.get(normalized, normalized)
        if normalized not in VALID_METRICS:
            raise error(
                "BED_EXPANSION metric must be one of: %s"
                % ", ".join(VALID_METRICS))
        return normalized

    @staticmethod
    def _printer_data_dir(start_args):
        config_file = start_args.get("config_file", "")
        if not config_file:
            return ""
        return os.path.dirname(os.path.dirname(os.path.abspath(config_file)))

    @staticmethod
    def _load_heatsoak_profiles(value, error):
        try:
            profiles = json.loads(value)
        except (TypeError, ValueError) as exc:
            raise error("heatsoak_profiles must be valid JSON: %s" % exc)
        if not isinstance(profiles, dict):
            raise error("heatsoak_profiles must be a JSON object")
        for temperature, profile in profiles.items():
            try:
                float(temperature)
            except (TypeError, ValueError):
                raise error(
                    "heatsoak_profiles keys must be temperatures")
            if not isinstance(profile, dict):
                raise error(
                    "heatsoak profile %s must be a JSON object"
                    % temperature)
        return profiles

    @staticmethod
    def _config_pair(config, option, parser, minval=None):
        value = config.get(option, None)
        if value is None:
            return None
        try:
            pair = [parser(item.strip()) for item in value.split(",")]
        except (TypeError, ValueError):
            raise config.error("%s must contain one or two values" % option)
        if len(pair) == 1:
            pair *= 2
        if len(pair) != 2:
            raise config.error("%s must contain one or two values" % option)
        if minval is not None and any(item < minval for item in pair):
            raise config.error(
                "%s values must be at least %s" % (option, minval))
        return tuple(pair)

    @staticmethod
    def _utc_now():
        return datetime.datetime.now(datetime.timezone.utc).isoformat()

    def _set_phase(self, phase, eventtime):
        self.phase = phase
        self.phase_started_at = eventtime
        logging.info("bed_expansion: phase %s", phase)

    def _is_homed(self, eventtime):
        toolhead = self.printer.lookup_object("toolhead")
        homed_axes = toolhead.get_status(eventtime).get("homed_axes", "")
        return all(axis in homed_axes for axis in "xyz")

    def _find_leveler(self):
        leveler = self.printer.lookup_object("quad_gantry_level", None)
        if leveler is not None:
            return leveler, "QUAD_GANTRY_LEVEL"
        leveler = self.printer.lookup_object("z_tilt", None)
        if leveler is not None:
            return leveler, "Z_TILT_ADJUST"
        return None, None

    def _bed_heater(self):
        heaters = self.printer.lookup_object("heaters")
        try:
            return heaters.lookup_heater("heater_bed")
        except Exception as error:
            raise self.gcode.error("heater_bed is not configured: %s" % error)

    def _set_bed_target(self, target):
        self.gcode.run_script_from_command("M140 S%.3f" % target)

    def _park(self):
        if self.park_macro:
            self.gcode.run_script_from_command(self.park_macro)

    def _append_record(self, record):
        if self.run_file is None:
            raise self.gcode.error("No bed expansion run file is active")
        with open(self.run_file, "a") as output:
            json.dump(record, output, separators=(",", ":"), allow_nan=False)
            output.write("\n")

    def _create_run_file(self, mode, parameters, eventtime):
        os.makedirs(self.output_dir, exist_ok=True)
        timestamp = datetime.datetime.now(datetime.timezone.utc)
        run_id = timestamp.strftime("%Y%m%dT%H%M%S%fZ")
        self.run_file = os.path.join(
            self.output_dir, "%s_%s.jsonl" % (run_id, mode))
        self.run = dict(parameters)
        self.run.update({
            "run_id": run_id,
            "mode": mode,
            "created_at": timestamp.isoformat(),
            "created_monotonic": eventtime,
        })
        self._append_record({
            "type": "run",
            "run": dict(self.run),
        })

    def _initialize_run(self, gcmd, mode, parameters):
        if self.active:
            raise gcmd.error(
                "A bed expansion analysis is already active")
        self.printer.lookup_object("bed_mesh")
        self._bed_heater()
        eventtime = self.reactor.monotonic()
        self.active = True
        self.last_error = None
        self.previous_mesh = None
        self.latest_metrics = None
        self.sample_count = 0
        self.stable_count = 0
        self.sample_elapsed_times = []
        self.last_valid_z_mesh = None
        self.last_mesh_rejected = False
        self.analysis_started_at = None
        self.next_scan_at = None
        try:
            self._create_run_file(mode, parameters, eventtime)
        except Exception as error:
            self.active = False
            self.run = None
            self.run_file = None
            raise gcmd.error(
                "Unable to create bed expansion run file: %s" % error)
        return eventtime

    def _start_run(self, gcmd, mode, parameters):
        eventtime = self._initialize_run(gcmd, mode, parameters)
        self._set_phase("home", eventtime)
        self.reactor.update_timer(self.timer, self.reactor.NOW)
        gcmd.respond_info(
            "Bed expansion %s started; output: %s"
            % (mode, self.run_file))

    def _start_mesh_scan(self, eventtime):
        for attempt in range(self.mesh_retries + 1):
            if self.relevel_before_scan or self.rehome_z_before_scan:
                self.gcode.run_script_from_command("BED_MESH_CLEAR")
            if self.relevel_before_scan:
                _, level_command = self._find_leveler()
                if level_command is not None:
                    self.gcode.run_script_from_command(level_command)
            if self.rehome_z_before_scan:
                self.gcode.run_script_from_command("G28 Z")
            if self._perform_mesh_scan(attempt):
                return

    def _perform_mesh_scan(self, attempt=0):
        bed_mesh = self.printer.lookup_object("bed_mesh")
        command = [self.run["mesh_command"]]
        if "PROFILE=" not in self.run["mesh_command"].upper() and (
                self.run.get("mesh_profile") is None):
            command.append("PROFILE=__bed_expansion_analysis__")
        mesh_options = (
            ("speed", "SCAN_SPEED"),
            ("probe_count", "PROBE_COUNT"),
            ("mesh_min", "MESH_MIN"),
            ("mesh_max", "MESH_MAX"),
            ("mesh_profile", "PROFILE"),
        )
        for key, option in mesh_options:
            values = self.run.get(key)
            if values is None:
                continue
            if isinstance(values, tuple):
                value = ",".join(str(item) for item in values)
            else:
                value = str(values)
            command.append("%s=%s" % (option, value))

        probe_manager = getattr(bed_mesh.bmc, "probe_mgr", None)
        probe_helper = getattr(probe_manager, "probe_helper", None)
        previous_speed = None
        if probe_helper is not None and self.run["speed"] is not None:
            previous_speed = probe_helper.speed
            probe_helper.speed = self.run["speed"]
        try:
            self.gcode.run_script_from_command(" ".join(command))
        finally:
            if previous_speed is not None:
                probe_helper.speed = previous_speed
        completed_at = self.reactor.monotonic()
        self.next_scan_at = completed_at + self.run["interval_s"]
        self._set_phase("scanning", completed_at)
        return self._capture_mesh(completed_at, attempt)

    def _capture_mesh(self, eventtime, attempt=0):
        bed_mesh = self.printer.lookup_object("bed_mesh")
        z_mesh = bed_mesh.get_mesh()
        if z_mesh is None:
            return False
        probed_matrix = [
            [float(value) for value in row]
            for row in z_mesh.get_probed_matrix()
        ]
        delta = None
        metrics = None
        if self.previous_mesh is not None:
            delta = mesh_delta(self.previous_mesh, probed_matrix)
            metrics = delta_metrics(delta)

        rejection_reason = None
        if (
            metrics is not None
            and metrics["max_abs_delta_mm"] > self.mesh_reject_tolerance
        ):
            rejection_reason = (
                "maximum point delta %.6f mm exceeds %.6f mm"
                % (
                    metrics["max_abs_delta_mm"],
                    self.mesh_reject_tolerance,
                ))
        if rejection_reason is not None:
            bed_mesh = self.printer.lookup_object("bed_mesh")
            bed_mesh.set_mesh(self.last_valid_z_mesh)
            self.stable_count = 0
            self.last_mesh_rejected = True
            self._append_record({
                "type": "rejected_sample",
                "run_id": self.run["run_id"],
                "sample_index": self.sample_count,
                "attempt": attempt,
                "elapsed_s": eventtime - self.analysis_started_at,
                "captured_at": self._utc_now(),
                "probed_matrix": probed_matrix,
                "mesh_matrix": [
                    [float(value) for value in row]
                    for row in z_mesh.get_mesh_matrix()
                ],
                "delta_from_previous": delta,
                "metrics": metrics,
                "reason": rejection_reason,
            })
            self.sample_count += 1
            self.gcode.respond_info(
                "Rejected bed mesh sample %d (attempt %d): %s; "
                "previous valid mesh restored"
                % (self.sample_count, attempt + 1, rejection_reason))
            return False

        stable = False
        if metrics is not None:
            metric_key = {
                "max_abs": "max_abs_delta_mm",
                "stdev": "stdev_delta_mm",
                "rms": "rms_delta_mm",
            }[self.run["metric"]]
            stable = metrics[metric_key] <= self.run["tolerance_mm"]
            if stable:
                self.stable_count += 1
            else:
                self.stable_count = 0

        record = {
            "type": "sample",
            "run_id": self.run["run_id"],
            "sample_index": self.sample_count,
            "elapsed_s": eventtime - self.analysis_started_at,
            "captured_at": self._utc_now(),
            "probed_matrix": probed_matrix,
            "mesh_matrix": [
                [float(value) for value in row]
                for row in z_mesh.get_mesh_matrix()
            ],
            "mesh_params": dict(z_mesh.get_mesh_params()),
            "delta_from_previous": delta,
            "metrics": metrics,
            "stable_metric": self.run["metric"],
            "tolerance_mm": self.run["tolerance_mm"],
            "comparison_stable": stable if metrics is not None else None,
            "stable_window_count": self.stable_count,
        }
        self._append_record(record)
        self.sample_count += 1
        self.last_mesh_rejected = False
        self.sample_elapsed_times.append(
            eventtime - self.analysis_started_at)
        self.last_valid_z_mesh = z_mesh
        self.previous_mesh = probed_matrix
        self.latest_metrics = metrics
        self.gcode.respond_info(
            "Bed expansion sample %d captured%s"
            % (
                self.sample_count,
                " (%s=%.6f mm, stable %d/%d)" % (
                    self.run["metric"],
                    metrics[self._metric_key()],
                    self.stable_count,
                    self.run["stable_window"],
                ) if metrics is not None else "",
            ))
        self._park()

        if (
            self.run["mode"] == "calibrate"
            and self.stable_count >= self.run["stable_window"]
        ):
            self._finish_run(eventtime, "stable")
            return True
        duration = (
            self.run["duration_s"]
            if self.run["mode"] == "analyze"
            else self.run["timeout_s"]
        )
        if eventtime - self.analysis_started_at >= duration:
            result = (
                "completed"
                if self.run["mode"] == "analyze"
                else "timeout"
            )
            self._finish_run(eventtime, result)
            return True
        self._set_phase("interval", eventtime)
        return True

    def _metric_key(self):
        return {
            "max_abs": "max_abs_delta_mm",
            "stdev": "stdev_delta_mm",
            "rms": "rms_delta_mm",
        }[self.run["metric"]]

    def _finish_run(self, eventtime, result, error=None):
        if not self.active:
            return
        eventtime = self.reactor.monotonic()
        self.active = False
        self.reactor.update_timer(self.timer, self.reactor.NEVER)
        if not self.run.get("keep_heater_on"):
            try:
                self._set_bed_target(0.)
            except Exception:
                logging.exception(
                    "bed_expansion: unable to turn off bed heater")
        record = {
            "type": "result",
            "run_id": self.run["run_id"],
            "result": result,
            "ended_at": self._utc_now(),
            "elapsed_s": (
                None if self.analysis_started_at is None
                else eventtime - self.analysis_started_at
            ),
            "sample_count": self.sample_count,
            "stable_window_count": self.stable_count,
        }
        if error is not None:
            record["error"] = error
        try:
            self._append_record(record)
        except Exception:
            logging.exception(
                "bed_expansion: unable to write terminal result")
        self.last_error = error
        self._set_phase(result, eventtime)
        if error is None:
            self.gcode.respond_info(
                "Bed expansion analysis %s after %d samples; output: %s"
                % (result, self.sample_count, self.run_file))
            if result == "stable" and self.run.get("save_profile", True):
                self._save_heatsoak_profile(eventtime)
        else:
            self.gcode.respond_info(
                "Bed expansion analysis failed: %s; partial output: %s"
                % (error, self.run_file))
        if error is None and self.render_on_finish:
            if self.sample_count < 2:
                self.gcode.respond_info(
                    "Bed expansion GIF not rendered: at least two samples "
                    "are required")
            else:
                self._render_finished_run()

    def _save_heatsoak_profile(self, eventtime):
        heatsoak_time = eventtime - self.analysis_started_at
        stable_window_started = self.sample_elapsed_times[
            -self.run["stable_window"]]
        temperature = "%.3f" % self.run["target_c"]
        temperature = temperature.rstrip("0").rstrip(".")
        profile = {
            "heatsoak_time_s": round(heatsoak_time, 3),
            "stable_window_started_s": round(stable_window_started, 3),
            "target_c": self.run["target_c"],
            "tolerance_mm": self.run["tolerance_mm"],
            "metric": self.run["metric"],
            "interval_s": self.run["interval_s"],
            "stable_window": self.run["stable_window"],
            "stable_comparisons": self.stable_count,
            "sample_count": self.sample_count,
            "calibrated_at": self._utc_now(),
            "run_id": self.run["run_id"],
            "run_file": self.run_file,
        }
        profiles = dict(self.heatsoak_profiles)
        profiles[temperature] = profile
        serialized = json.dumps(
            profiles, sort_keys=True, separators=(",", ":"))
        self.configfile.set(
            self.config_name, "heatsoak_profiles", serialized)
        self.heatsoak_profiles = profiles
        self.gcode.respond_info(
            "Heat-soak profile for %.3f C recorded as %.1f s. "
            "Run SAVE_CONFIG to persist it."
            % (self.run["target_c"], heatsoak_time))

    def _render_finished_run(self):
        run_file = self.run_file
        self.gcode.respond_info(
            "Rendering bed expansion GIF in the background")
        thread = threading.Thread(
            target=self._render_finished_run_worker,
            args=(run_file,),
            name="bed-exp-render",
            daemon=True,
        )
        thread.start()

    def _render_finished_run_worker(self, run_file):
        try:
            output_file = self._render_run_file(run_file)
        except Exception as error:
            logging.exception(
                "bed_expansion: unable to render completed run")
            self.reactor.register_async_callback(
                lambda eventtime, error=error:
                self._report_render_error(error))
            return
        self.reactor.register_async_callback(
            lambda eventtime: self._show_rendered_file(output_file))

    def _render_run_file(self, run_file):
        base, _ = os.path.splitext(run_file)
        output_file = "%s_%s_%s.gif" % (
            base, self.render_mode, self.render_view)
        renderer_script = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "bed_expansion_render.py",
        )
        subprocess.run(
            [
                sys.executable,
                renderer_script,
                run_file,
                "--mode", self.render_mode,
                "--view", self.render_view,
                "--data-source", self.render_data_source,
                "--fps", str(self.render_fps),
                "--dpi", str(self.render_dpi),
                "--z-exaggeration", str(self.render_z_exaggeration),
                "--output", output_file,
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=120,
        )
        return output_file

    def _report_render_error(self, error):
        self.gcode.respond_info(
            "Bed expansion GIF rendering failed: %s" % error)

    def _show_rendered_file(self, output_file):
        output_file = os.path.realpath(output_file)
        printer_data_dir = os.path.realpath(self.printer_data_dir)
        try:
            within_printer_data = (
                os.path.commonpath((printer_data_dir, output_file))
                == printer_data_dir
            )
        except ValueError:
            within_printer_data = False
        if not within_printer_data:
            self.gcode.respond_info(
                "Bed expansion GIF: %s" % output_file)
            return
        relative_path = os.path.relpath(output_file, printer_data_dir)
        url = "../server/files/%s" % quote(
            relative_path.replace(os.sep, "/"), safe="/")
        name = html.escape(os.path.basename(output_file))
        self.gcode.respond_raw(
            '<p style="font-weight:bold;margin:0;color:white">'
            'Bed expansion animation: %s</p>'
            'Click image to download or right-click for options.'
            '<a href="%s" target="_blank"><img src="%s" '
            'alt="%s" width="100%%"></a>'
            % (name, url, url, name))

    def _timer_callback(self, eventtime):
        if not self.active:
            return self.reactor.NEVER
        try:
            return self._timer_step(eventtime)
        except Exception as error:
            logging.exception("bed_expansion: run failed")
            self._finish_run(self.reactor.monotonic(), "failed", str(error))
            return self.reactor.NEVER

    def _timer_step(self, eventtime):
        if self.phase == "home":
            if not self._is_homed(eventtime):
                self.gcode.run_script_from_command("G28 X Y Z")

            _, level_command = self._find_leveler()
            if level_command is not None:
                self._set_phase("level", eventtime)
                self.gcode.run_script_from_command(level_command)

            self._set_bed_target(self.run["target_c"])
            self._set_phase("heating", eventtime)
            target = self.run["target_c"]
            self.gcode.run_script_from_command(
                "TEMPERATURE_WAIT SENSOR=heater_bed MINIMUM=%.3f"
                % target)
            eventtime = self.reactor.monotonic()
            heater_temp, _ = self._bed_heater().get_temp(eventtime)
            self.analysis_started_at = eventtime
            self._append_record({
                "type": "target_reached",
                "run_id": self.run["run_id"],
                "target_c": target,
                "measured_c": heater_temp,
                "reached_at": self._utc_now(),
                "elapsed_from_start_s": (
                    eventtime - self.run["created_monotonic"]),
            })
            self._start_mesh_scan(eventtime)
            return (
                self.reactor.NEVER
                if not self.active
                else self.next_scan_at
            )

        if self.phase == "interval":
            limit = (
                self.run["duration_s"]
                if self.run["mode"] == "analyze"
                else self.run["timeout_s"]
            )
            if eventtime - self.analysis_started_at >= limit:
                self._finish_run(
                    eventtime,
                    "completed" if self.run["mode"] == "analyze"
                    else "timeout")
                return self.reactor.NEVER
            if eventtime < self.next_scan_at:
                return self.next_scan_at
            self._start_mesh_scan(eventtime)
            return (
                self.reactor.NEVER
                if not self.active
                else self.next_scan_at
            )

        raise self.gcode.error("Unknown bed expansion phase: %s"
                               % self.phase)

    def _handle_shutdown(self):
        if not self.active:
            return
        eventtime = self.reactor.monotonic()
        self._finish_run(eventtime, "interrupted",
                         "Klipper shut down during analysis")

    def get_status(self, eventtime=None):
        eventtime = eventtime or self.reactor.monotonic()
        elapsed = None
        if self.analysis_started_at is not None:
            elapsed = eventtime - self.analysis_started_at
        return {
            "active": self.active,
            "phase": self.phase,
            "run_id": None if self.run is None else self.run.get("run_id"),
            "mode": None if self.run is None else self.run.get("mode"),
            "target_c": None if self.run is None else self.run.get("target_c"),
            "run_file": self.run_file,
            "sample_count": self.sample_count,
            "stable_window_count": self.stable_count,
            "latest_metrics": self.latest_metrics,
            "last_mesh_rejected": self.last_mesh_rejected,
            "heatsoak_profiles": self.heatsoak_profiles,
            "elapsed_s": elapsed,
            "last_error": self.last_error,
        }

    cmd_BED_EXPANSION_ANALYZE_help = (
        "Run a fixed-duration bed expansion mesh analysis")

    def cmd_BED_EXPANSION_ANALYZE(self, gcmd):
        target = gcmd.get_float("TARGET", above=0.)
        interval = gcmd.get_float(
            "INTERVAL", self.default_interval, above=0.)
        duration = gcmd.get_float(
            "DURATION", self.default_duration, above=0.)
        metric = self._parse_metric(
            gcmd.get("METRIC", self.default_metric), gcmd.error)
        mesh_parameters = self._mesh_parameters(gcmd)
        self._start_run(gcmd, "analyze", {
            "target_c": target,
            "interval_s": interval,
            "duration_s": duration,
            "metric": metric,
            "tolerance_mm": gcmd.get_float(
                "TOLERANCE", self.default_tolerance, minval=0.),
            "stable_window": gcmd.get_int(
                "STABLE_WINDOW", self.default_stable_window, minval=1),
            **mesh_parameters,
        })

    cmd_BED_EXPANSION_CALIBRATE_help = (
        "Find the stable heat-soak time by comparing successive bed meshes")

    def cmd_BED_EXPANSION_CALIBRATE(self, gcmd):
        target = gcmd.get_float("TARGET", above=0.)
        interval = gcmd.get_float(
            "INTERVAL", self.default_interval, above=0.)
        timeout = gcmd.get_float(
            "TIMEOUT", self.default_timeout, above=0.)
        tolerance = gcmd.get_float(
            "TOLERANCE", self.default_tolerance, minval=0.)
        metric = self._parse_metric(
            gcmd.get("METRIC", self.default_metric), gcmd.error)
        stable_window = gcmd.get_int(
            "STABLE_WINDOW", self.default_stable_window, minval=1)
        mesh_parameters = self._mesh_parameters(gcmd)
        self._start_run(gcmd, "calibrate", {
            "target_c": target,
            "interval_s": interval,
            "timeout_s": timeout,
            "tolerance_mm": tolerance,
            "metric": metric,
            "stable_window": stable_window,
            **mesh_parameters,
        })

    cmd_BED_EXPANSION_WAIT_help = (
        "Block until the hot bed mesh remains stable or the timeout expires")

    def cmd_BED_EXPANSION_WAIT(self, gcmd):
        target = gcmd.get_float("TARGET", above=0.)
        interval = gcmd.get_float(
            "INTERVAL", self.default_interval, above=0.)
        timeout = gcmd.get_float(
            "TIMEOUT", self.default_timeout, above=0.)
        tolerance = gcmd.get_float(
            "TOLERANCE", self.default_tolerance, minval=0.)
        metric = self._parse_metric(
            gcmd.get("METRIC", self.default_metric), gcmd.error)
        stable_window = gcmd.get_int(
            "STABLE_WINDOW", self.default_stable_window, minval=1)
        timeout_action = gcmd.get(
            "ON_TIMEOUT", "error").strip().lower()
        if timeout_action not in ("error", "warn"):
            raise gcmd.error("ON_TIMEOUT must be error or warn")
        parameters = {
            "target_c": target,
            "interval_s": interval,
            "timeout_s": timeout,
            "tolerance_mm": tolerance,
            "metric": metric,
            "stable_window": stable_window,
            "keep_heater_on": True,
            "save_profile": bool(self.save_wait_profile),
            **self._mesh_parameters(gcmd),
        }
        eventtime = self._initialize_run(gcmd, "wait", parameters)
        gcmd.respond_info(
            "Waiting for bed expansion stability; output: %s"
            % self.run_file)
        try:
            self._prepare_synchronous_run(eventtime)
            deadline = self.analysis_started_at + timeout
            while not self.printer.is_shutdown():
                self._start_mesh_scan(self.reactor.monotonic())
                eventtime = self.reactor.monotonic()
                if self.stable_count >= stable_window:
                    self._finish_run(eventtime, "stable")
                    gcmd.respond_info(
                        "Bed is stable after %.1f seconds at %.1f C"
                        % (
                            eventtime - self.analysis_started_at,
                            target,
                        ))
                    return
                if eventtime >= deadline:
                    message = (
                        "Bed did not stabilize within %.1f seconds"
                        % timeout)
                    if timeout_action == "error":
                        self._finish_run(eventtime, "failed", message)
                        raise gcmd.error(message)
                    self._finish_run(eventtime, "timeout")
                    gcmd.respond_info(
                        "Continuing after bed expansion timeout: %s"
                        % message)
                    return
                next_wake = min(self.next_scan_at, deadline)
                self.reactor.pause(next_wake)
        except gcmd.error:
            raise
        except Exception as error:
            self._finish_run(
                self.reactor.monotonic(), "failed", str(error))
            raise gcmd.error(
                "Bed expansion wait failed: %s" % error)
        finally:
            self.reactor.update_timer(self.timer, self.reactor.NEVER)

    def _prepare_synchronous_run(self, eventtime):
        if not self._is_homed(eventtime):
            self._set_phase("home", eventtime)
            self.gcode.run_script_from_command("G28 X Y Z")
        _, level_command = self._find_leveler()
        if level_command is not None:
            self._set_phase("level", self.reactor.monotonic())
            self.gcode.run_script_from_command(level_command)
        self._set_bed_target(self.run["target_c"])
        self._set_phase("heating", self.reactor.monotonic())
        self.gcode.run_script_from_command(
            "TEMPERATURE_WAIT SENSOR=heater_bed MINIMUM=%.3f"
            % self.run["target_c"])
        eventtime = self.reactor.monotonic()
        heater_temp, _ = self._bed_heater().get_temp(eventtime)
        self.analysis_started_at = eventtime
        self._append_record({
            "type": "target_reached",
            "run_id": self.run["run_id"],
            "target_c": self.run["target_c"],
            "measured_c": heater_temp,
            "reached_at": self._utc_now(),
            "elapsed_from_start_s": (
                eventtime - self.run["created_monotonic"]),
        })

    def _mesh_parameters(self, gcmd):
        speed = gcmd.get_float("SPEED", self.default_mesh_speed, above=0.)
        mesh_command = gcmd.get(
            "MESH_COMMAND", self.mesh_command).strip()
        if not mesh_command or "\n" in mesh_command:
            raise gcmd.error(
                "MESH_COMMAND must be a single non-empty G-code command")
        return {
            "mesh_command": mesh_command,
            "mesh_profile": gcmd.get("PROFILE", None),
            "speed": speed,
            "probe_count": self._gcmd_pair(
                gcmd, "PROBE_COUNT", self.default_probe_count, int, 3),
            "mesh_min": self._gcmd_pair(
                gcmd, "MESH_MIN", self.default_mesh_min, float),
            "mesh_max": self._gcmd_pair(
                gcmd, "MESH_MAX", self.default_mesh_max, float),
        }

    @staticmethod
    def _gcmd_pair(gcmd, option, default, parser, minval=None):
        value = gcmd.get(option, None)
        if value is None:
            return default
        try:
            pair = [parser(item.strip()) for item in value.split(",")]
        except (TypeError, ValueError):
            raise gcmd.error("%s must contain one or two values" % option)
        if len(pair) == 1:
            pair *= 2
        if len(pair) != 2:
            raise gcmd.error("%s must contain one or two values" % option)
        if minval is not None and any(item < minval for item in pair):
            raise gcmd.error(
                "%s values must be at least %s" % (option, minval))
        return tuple(pair)

    cmd_BED_EXPANSION_STATUS_help = (
        "Report the current bed expansion analysis state")

    def cmd_BED_EXPANSION_STATUS(self, gcmd):
        gcmd.respond_info(json.dumps(self.get_status(), sort_keys=True))

    cmd_BED_EXPANSION_LOAD_help = (
        "Load a saved bed expansion sample into the live Klipper bed mesh")

    def cmd_BED_EXPANSION_LOAD(self, gcmd):
        filename = gcmd.get("RUN", None)
        if filename is None:
            filename = gcmd.get("FILE")
        sample_index = gcmd.get_int("SAMPLE", 0, minval=0)
        profile = gcmd.get("PROFILE", None)
        if os.path.basename(filename) != filename:
            raise gcmd.error("RUN must be a filename, not a path")
        path = os.path.realpath(os.path.join(self.output_dir, filename))
        output_dir = os.path.realpath(self.output_dir)
        if os.path.commonpath((output_dir, path)) != output_dir:
            raise gcmd.error(
                "RUN must reference the bed expansion output directory")
        if not os.path.isfile(path):
            raise gcmd.error("Bed expansion run not found: %s" % filename)

        sample = None
        try:
            with open(path, "r") as input_file:
                for line in input_file:
                    try:
                        record = json.loads(line)
                    except (TypeError, ValueError) as error:
                        raise gcmd.error(
                            "Invalid JSONL in %s: %s" % (filename, error))
                    if not isinstance(record, dict):
                        raise gcmd.error(
                            "Invalid JSONL record in %s" % filename)
                    if (
                        record.get("type") == "sample"
                        and record.get("sample_index") == sample_index
                    ):
                        sample = record
                        break
        except OSError as error:
            raise gcmd.error("Unable to read %s: %s" % (filename, error))
        if sample is None:
            raise gcmd.error(
                "Sample %d was not found in %s"
                % (sample_index, filename))
        if not isinstance(sample.get("probed_matrix"), list):
            raise gcmd.error("Saved sample has no valid probed mesh")
        if not isinstance(sample.get("mesh_params"), dict):
            raise gcmd.error("Saved sample has no valid mesh parameters")

        bed_mesh = self.printer.lookup_object("bed_mesh")
        bed_mesh_module = importlib.import_module(
            bed_mesh.__class__.__module__)
        ZMesh = bed_mesh_module.ZMesh
        BedMeshError = bed_mesh_module.BedMeshError
        z_mesh = ZMesh(
            sample["mesh_params"],
            profile or "bed_expansion_sample_%d" % sample_index)
        try:
            z_mesh.build_mesh(sample["probed_matrix"])
        except BedMeshError as error:
            raise gcmd.error("Unable to load saved mesh: %s" % error)
        bed_mesh.set_mesh(z_mesh)
        if profile:
            bed_mesh.save_profile(profile)
        gcmd.respond_info(
            "Loaded bed expansion sample %d from %s%s"
            % (
                sample_index,
                filename,
                " as profile [%s]" % profile if profile else "",
            ))


def load_config(config):
    return BedExpansion(config)