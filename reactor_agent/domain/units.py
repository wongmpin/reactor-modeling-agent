from __future__ import annotations

from pint import UnitRegistry

ureg = UnitRegistry(autoconvert_offset_to_baseunit=True)


def convert_temperature(value: float, unit: str) -> float:
    return float((value * ureg(unit)).to("kelvin").magnitude)


def convert_pressure(value: float, unit: str) -> float:
    return float((value * ureg(unit)).to("kilopascal").magnitude)


def convert_flow(value: float, unit: str) -> float:
    return float((value * ureg(unit)).to("kilomole / hour").magnitude)


def convert_mass_flow(value: float, unit: str) -> float:
    return float((value * ureg(unit)).to("kilogram / hour").magnitude)
