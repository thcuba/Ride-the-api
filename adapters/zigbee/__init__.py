"""
Zigbee Protocol Adapter — generic, vendor-neutral Zigbee message handling.

Zigbee is a low-power wireless mesh protocol (IEEE 802.15.4) widely used
in smart home devices. This adapter translates standard Zigbee Cluster
Library (ZCL) messages into standardized CommandTypes.
"""

from __future__ import annotations

from adapters.zigbee.adapter import (
    ATTR_BATTERY_PERCENT,
    ATTR_BATTERY_VOLTAGE,
    ATTR_HUMIDITY_MEASURED,
    ATTR_INSTANT_POWER,
    ATTR_LEVEL,
    ATTR_ON_OFF,
    ATTR_TEMP_MEASURED,
    ATTR_TOTAL_ENERGY,
    CMD_OFF,
    CMD_ON,
    CMD_TOGGLE,
    ZCL_CLUSTER_BASIC,
    ZCL_CLUSTER_GROUPS,
    ZCL_CLUSTER_HUMIDITY,
    ZCL_CLUSTER_IAS_ZONE,
    ZCL_CLUSTER_LEVEL,
    ZCL_CLUSTER_METERING,
    ZCL_CLUSTER_ON_OFF,
    ZCL_CLUSTER_POWER_CONFIG,
    ZCL_CLUSTER_SCENES,
    ZCL_CLUSTER_TEMPERATURE,
    ZigbeeProtocolAdapter,
)
from adapters.zigbee.definition import PROTOCOL_DEFINITION
from adapters.zigbee.server import ZigbeeBridgePlugin

__all__ = [
    "ATTR_BATTERY_PERCENT",
    "ATTR_BATTERY_VOLTAGE",
    "ATTR_HUMIDITY_MEASURED",
    "ATTR_INSTANT_POWER",
    "ATTR_LEVEL",
    "ATTR_ON_OFF",
    "ATTR_TEMP_MEASURED",
    "ATTR_TOTAL_ENERGY",
    "CMD_OFF",
    "CMD_ON",
    "CMD_TOGGLE",
    "PROTOCOL_DEFINITION",
    "ZCL_CLUSTER_BASIC",
    "ZCL_CLUSTER_GROUPS",
    "ZCL_CLUSTER_HUMIDITY",
    "ZCL_CLUSTER_IAS_ZONE",
    "ZCL_CLUSTER_LEVEL",
    "ZCL_CLUSTER_METERING",
    "ZCL_CLUSTER_ON_OFF",
    "ZCL_CLUSTER_POWER_CONFIG",
    "ZCL_CLUSTER_SCENES",
    "ZCL_CLUSTER_TEMPERATURE",
    "ZigbeeBridgePlugin",
    "ZigbeeProtocolAdapter",
]
