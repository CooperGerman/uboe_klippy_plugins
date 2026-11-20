import datetime
import importlib
import json
import logging
import math
import os


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


class BedExpansionAnalyzer:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.gcode = self.printer.lookup_object("gcode")

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
        self.target_tolerance = config.getfloat(
            "target_tolerance", .5, minval=0.)
        self.heat_timeout = config.getfloat(
            "heat_timeout", 7200., above=0.)
        self.poll_interval = config.getfloat(
            "poll_interval", .5, above=0.)
        self.park_macro = config.get("park_macro", "PARK").strip()

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
        self.last_error = None
        self.analysis_started_at = None
        self.phase_started_at = None
        self.next_scan_at = None
        self.level_object = None
        self.level_command = None
        self.level_started = False
        self.home_started = False

        self.printer.register_event_handler(
            "klippy:shutdown", self._handle_shutdown)
        self.gcode.register_command(
            "BED_EXPANSION_ANALYZE", self.cmd_BED_EXPANSION_ANALYZE,
            desc=self.cmd_BED_EXPANSION_ANALYZE_help)
        self.gcode.register_command(
            "BED_EXPANSION_CALIBRATE", self.cmd_BED_EXPANSION_CALIBRATE,
            desc=self.cmd_BED_EXPANSION_CALIBRATE_help)
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
    def _utc_now():
        return datetime.datetime.now(datetime.timezone.utc).isoformat()

    def _set_phase(self, phase, eventtime):
        self.phase = phase
        self.phase_started_at = eventtime
        logging.info("bed_expansion_analyzer: phase %s", phase)

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

    def _start_run(self, gcmd, mode, parameters):
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
        self.analysis_started_at = None
        self.next_scan_at = None
        self.level_object = None
        self.level_command = None
        self.level_started = False
        self.home_started = False
        try:
            self._create_run_file(mode, parameters, eventtime)
        except Exception as error:
            self.active = False
            self.run = None
            self.run_file = None
            raise gcmd.error(
                "Unable to create bed expansion run file: %s" % error)
        self._set_phase("home", eventtime)
        self.reactor.update_timer(self.timer, self.reactor.NOW)
        gcmd.respond_info(
            "Bed expansion %s started; output: %s"
            % (mode, self.run_file))

    def _start_mesh_scan(self, eventtime):
        bed_mesh = self.printer.lookup_object("bed_mesh")
        self.next_scan_at = eventtime + self.run["interval_s"]
        self.gcode.run_script_from_command(
            "BED_MESH_CALIBRATE PROFILE=__bed_expansion_analysis__")
        calibrate = bed_mesh.bmc
        calibrate._profile_name = None
        self._set_phase("scanning", eventtime)

    def _capture_mesh(self, eventtime):
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
        self.reactor.update_timer(
            self.timer, max(eventtime + self.poll_interval,
                            self.next_scan_at))
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
        self.active = False
        self.reactor.update_timer(self.timer, self.reactor.NEVER)
        try:
            self._set_bed_target(0.)
        except Exception:
            logging.exception(
                "bed_expansion_analyzer: unable to turn off bed heater")
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
                "bed_expansion_analyzer: unable to write terminal result")
        self.last_error = error
        self._set_phase(result, eventtime)
        if error is None:
            self.gcode.respond_info(
                "Bed expansion analysis %s after %d samples; output: %s"
                % (result, self.sample_count, self.run_file))
        else:
            self.gcode.respond_info(
                "Bed expansion analysis failed: %s; partial output: %s"
                % (error, self.run_file))

    def _timer_callback(self, eventtime):
        if not self.active:
            return self.reactor.NEVER
        try:
            return self._timer_step(eventtime)
        except Exception as error:
            logging.exception("bed_expansion_analyzer: run failed")
            self._finish_run(eventtime, "failed", str(error))
            return self.reactor.NEVER

    def _timer_step(self, eventtime):
        if self.phase == "home":
            if self._is_homed(eventtime):
                self.level_object, self.level_command = self._find_leveler()
                if self.level_object is not None:
                    self._set_phase("level", eventtime)
                else:
                    self._set_bed_target(self.run["target_c"])
                    self._set_phase("heating", eventtime)
                return self.reactor.NOW
            if not self.home_started:
                self.home_started = True
                self.gcode.run_script_from_command("G28 X Y Z")
            return eventtime + self.poll_interval

        if self.phase == "level":
            if not self.level_started:
                self.level_started = True
                self.gcode.run_script_from_command(self.level_command)
                return eventtime + self.poll_interval
            if not self.level_object.get_status(eventtime).get("applied"):
                return eventtime + self.poll_interval
            self._set_bed_target(self.run["target_c"])
            self._set_phase("heating", eventtime)
            return eventtime + self.poll_interval

        if self.phase == "heating":
            heater_temp, _ = self._bed_heater().get_temp(eventtime)
            target = self.run["target_c"]
            if heater_temp < target - self.target_tolerance:
                if eventtime - self.phase_started_at >= self.heat_timeout:
                    raise self.gcode.error(
                        "bed did not reach %.1f C within %.0f seconds"
                        % (target, self.heat_timeout))
                return eventtime + self.poll_interval
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
            return eventtime + self.poll_interval

        if self.phase == "scanning":
            self._capture_mesh(eventtime)
            return self.reactor.NEVER if not self.active else (
                eventtime + self.poll_interval)

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
            return eventtime + self.poll_interval

        raise self.gcode.error("Unknown analyzer phase: %s" % self.phase)

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
        self._start_run(gcmd, "analyze", {
            "target_c": target,
            "interval_s": interval,
            "duration_s": duration,
            "metric": metric,
            "tolerance_mm": gcmd.get_float(
                "TOLERANCE", self.default_tolerance, minval=0.),
            "stable_window": gcmd.get_int(
                "STABLE_WINDOW", self.default_stable_window, minval=1),
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
        self._start_run(gcmd, "calibrate", {
            "target_c": target,
            "interval_s": interval,
            "timeout_s": timeout,
            "tolerance_mm": tolerance,
            "metric": metric,
            "stable_window": stable_window,
        })

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
            raise gcmd.error("RUN must reference the analyzer output directory")
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
    return BedExpansionAnalyzer(config)