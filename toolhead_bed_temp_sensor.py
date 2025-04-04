# Support Toolhead Bed Temperature Sensor
#
# Copyright (C) 2024  Yannick Le Provost <yannick.leprovost@print-hive.com>
#
# This file may be distributed under the terms of the GNU GPLv3 license.

from . temperature_sensor import PrinterSensorGeneric

class ToolheadBedTempSensor(PrinterSensorGeneric):
    def __init__(self, config):
        super().__init__(config)
        self.printer.add_object("toolhead_bed_temp_sensor", self)
        self.x_nozzle_to_sensor_offset = config.getfloat('x_nozzle_to_sensor_offset', 0.0)
        self.y_nozzle_to_sensor_offset = config.getfloat('y_nozzle_to_sensor_offset', 0.0)

def load_config_prefix(config):
    return ToolheadBedTempSensor(config)
