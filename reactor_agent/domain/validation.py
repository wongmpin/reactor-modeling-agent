from __future__ import annotations

from dataclasses import dataclass, field

from pint.errors import PintError

from reactor_agent.domain.models import (
    ReactorModelSpec,
    ReactorType,
    SimulationResult,
)
from reactor_agent.domain.units import convert_flow, convert_mass_flow, convert_pressure, convert_temperature


def parse_formula(formula: str) -> dict[str, float]:
    """Parse standard element symbols and nested parenthesized molecular formulas."""
    index = 0

    def number() -> float:
        nonlocal index
        start = index
        while index < len(formula) and (formula[index].isdigit() or formula[index] == "."):
            index += 1
        return float(formula[start:index]) if start != index else 1.0

    def group(nested: bool = False) -> dict[str, float]:
        nonlocal index
        result: dict[str, float] = {}
        while index < len(formula) and formula[index] != ")":
            if formula[index] == "(":
                index += 1
                inner = group(nested=True)
                if index >= len(formula) or formula[index] != ")":
                    raise ValueError("Unclosed parenthesis in molecular formula")
                index += 1
                multiplier = number()
                for element, count in inner.items():
                    result[element] = result.get(element, 0.0) + count * multiplier
                continue
            if not formula[index].isupper():
                raise ValueError(f"Unsupported formula syntax near {formula[index:]!r}")
            element = formula[index]
            index += 1
            if index < len(formula) and formula[index].islower():
                element += formula[index]
                index += 1
            result[element] = result.get(element, 0.0) + number()
        if nested and not result:
            raise ValueError("Empty parenthesis in molecular formula")
        return result

    elements = group()
    if index != len(formula) or not elements:
        raise ValueError(f"Unsupported molecular formula: {formula}")
    return elements


@dataclass
class ValidationReport:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.errors


def validate_spec(spec: ReactorModelSpec) -> ValidationReport:
    report = ValidationReport()
    if spec.feed is None:
        report.errors.append("Feed composition is required.")
    else:
        total = sum(component.value for component in spec.feed.components)
        if abs(total - 1.0) > 1e-6:
            report.errors.append(f"Feed {spec.feed.composition_basis} values sum to {total:.8g}; expected 1.0.")
        names = [component.name.casefold() for component in spec.feed.components]
        if len(names) != len(set(names)):
            report.errors.append("Feed component names must be unique.")
        for component in spec.feed.components:
            try:
                parse_formula(component.formula)
            except ValueError as exc:
                report.errors.append(f"Feed component {component.name}: {exc}")
        if spec.feed.flow is None:
            report.errors.append("Feed flow rate is required.")
        else:
            try:
                flow = (convert_flow(spec.feed.flow, spec.feed.flow_unit)
                        if spec.feed.flow_basis == "molar"
                        else convert_mass_flow(spec.feed.flow, spec.feed.flow_unit))
                if flow <= 0:
                    report.errors.append("Feed flow rate must be positive.")
            except (PintError, TypeError) as exc:
                report.errors.append(f"Invalid feed flow unit {spec.feed.flow_unit!r}: {exc}")
        if spec.feed.temperature is None:
            report.errors.append("Feed temperature is required.")
        else:
            try:
                if convert_temperature(spec.feed.temperature, spec.feed.temperature_unit) <= 0:
                    report.errors.append("Feed temperature must be above absolute zero.")
            except (PintError, TypeError) as exc:
                report.errors.append(f"Invalid temperature unit {spec.feed.temperature_unit!r}: {exc}")
        if spec.feed.pressure is None:
            report.errors.append("Feed pressure is required.")
        else:
            try:
                if convert_pressure(spec.feed.pressure, spec.feed.pressure_unit) <= 0:
                    report.errors.append("Feed pressure must be positive.")
            except (PintError, TypeError) as exc:
                report.errors.append(f"Invalid pressure unit {spec.feed.pressure_unit!r}: {exc}")

        formulas_by_name = {component.name.casefold(): component.formula for component in spec.feed.components}
        for reaction in spec.reactions:
            for term in [*reaction.reactants, *reaction.products]:
                existing = formulas_by_name.get(term.component.casefold())
                if existing is not None and existing != term.formula:
                    report.errors.append(
                        f"Component {term.component} has conflicting formulas {existing} and {term.formula}."
                    )
                formulas_by_name[term.component.casefold()] = term.formula

    for name, value, unit, converter in (
        ("target outlet temperature", spec.known_parameters.target_outlet_temperature,
         spec.known_parameters.target_outlet_temperature_unit, convert_temperature),
        ("target pressure", spec.known_parameters.target_pressure,
         spec.known_parameters.target_pressure_unit, convert_pressure),
    ):
        if value is not None:
            try:
                if converter(value, unit) <= 0:
                    report.errors.append(f"{name} must be above absolute zero or positive.")
            except (PintError, TypeError) as exc:
                report.errors.append(f"Invalid {name} unit {unit!r}: {exc}")

    if not spec.reactions and not (spec.reactor_type == ReactorType.GIBBS and spec.allowed_species):
        report.errors.append("At least one reaction is required.")
    # Availability is evaluated across the reaction network, not only the feed.
    available = {component.name.casefold() for component in spec.feed.components if component.value > 0} if spec.feed else set()
    changed = True
    while changed:
        changed = False
        for reaction in spec.reactions:
            if all(term.component.casefold() in available for term in reaction.reactants):
                generated = {term.component.casefold() for term in reaction.products}
                if not generated <= available:
                    available.update(generated)
                    changed = True
    for index, reaction in enumerate(spec.reactions, start=1):
        try:
            balance: dict[str, float] = {}
            for sign, terms in ((-1.0, reaction.reactants), (1.0, reaction.products)):
                for term in terms:
                    for element, count in parse_formula(term.formula).items():
                        balance[element] = balance.get(element, 0.0) + sign * term.coefficient * count
            unbalanced = {element: value for element, value in balance.items() if abs(value) > 1e-8}
            if unbalanced:
                report.errors.append(f"Reaction {index} is not element-balanced: {unbalanced}.")
        except ValueError as exc:
            report.errors.append(f"Reaction {index}: {exc}")
        if spec.feed:
            missing_reactants = sorted({term.component for term in reaction.reactants
                                        if term.component.casefold() not in available})
            if missing_reactants:
                report.errors.append(f"Reaction {index} reactants are unavailable from the feed/reaction network: {missing_reactants}.")
        product_groups: dict[str, list[str]] = {}
        for term in reaction.products:
            product_groups.setdefault(term.formula, []).append(term.component)
        for formula, isomers in product_groups.items():
            if len(isomers) > 1:
                provided = {name: spec.known_parameters.selectivity[name]
                            for name in isomers if name in spec.known_parameters.selectivity}
                if set(provided) != set(isomers):
                    report.errors.append(
                        f"Reaction {index} has multiple products with formula {formula} ({isomers}); "
                        "provide product selectivity for each isomer or revise the product specification."
                    )
                elif any(abs(term.coefficient / sum(t.coefficient for t in reaction.products if t.formula == formula)
                                 - provided[term.component]) > 1e-6 for term in reaction.products if term.formula == formula):
                    report.errors.append(f"Reaction {index} isomer stoichiometry disagrees with declared selectivity.")
                elif abs(sum(provided.values()) - 1.0) > 1e-6:
                    report.errors.append(
                        f"Reaction {index} selectivity for isomers {isomers} must sum to 1.0."
                    )

    if spec.reactor_type == ReactorType.CONVERSION and spec.known_parameters.conversion is None and any(r.conversion is None for r in spec.reactions):
        report.errors.append("Conversion reactor requires a specified conversion or confirmed assumption.")
    if spec.reactor_type == ReactorType.EQUILIBRIUM:
        if (spec.known_parameters.equilibrium_constant is None
                and spec.known_parameters.gibbs_energy_kj_per_mol is None):
            report.errors.append("Equilibrium reactor requires equilibrium constant or Gibbs energy data.")
    if spec.reactor_type in {ReactorType.CSTR, ReactorType.PFR}:
        if spec.known_parameters.kinetic_model is None or spec.known_parameters.reactor_volume_m3 is None:
            report.errors.append("Kinetic reactor requires a kinetic model and reactor volume.")

    for assumption in spec.assumptions:
        if not assumption.confirmed_by_user:
            report.errors.append(f"Assumption {assumption.key!r} requires user confirmation.")
    for missing in spec.missing_information:
        if missing.blocking:
            report.errors.append(f"Missing {missing.field}: {missing.reason}")
    return report


def validate_reactor_choice(spec: ReactorModelSpec, supported: set[ReactorType] | None = None) -> ValidationReport:
    report = ValidationReport()
    if spec.reactor_type is None:
        report.errors.append("The model must select and explain a reactor type.")
        return report
    if not spec.reactor_rationale:
        report.errors.append("The model must provide a rationale for its reactor selection.")
    if supported is not None and spec.reactor_type not in supported:
        report.errors.append(f"Selected reactor {spec.reactor_type.value} is not supported by this backend.")
    return report


def validate_result(result: SimulationResult) -> ValidationReport:
    report = ValidationReport()
    if not result.case_created:
        report.errors.append("No HYSYS case was created.")
    if not result.solver_converged:
        report.errors.append("HYSYS solver has not confirmed convergence.")
    if not result.streams:
        report.errors.append("No stream results were read from HYSYS.")
    for stream in result.streams:
        if stream.mole_fractions:
            total = sum(stream.mole_fractions.values())
            if abs(total - 1.0) > 1e-5:
                report.errors.append(f"Stream {stream.name} mole fractions sum to {total:.8g}.")
            if any(value < -1e-10 or value > 1.0 + 1e-10 for value in stream.mole_fractions.values()):
                report.errors.append(f"Stream {stream.name} contains out-of-range mole fractions.")
    return report


def validate_element_balance(
    spec: ReactorModelSpec,
    inlet_streams: list,
    outlet_streams: list,
    relative_tolerance: float = 1e-4,
) -> ValidationReport:
    """Compare boundary stream elemental molar rates; intermediate streams must be excluded."""
    report = ValidationReport()
    if spec.feed is None:
        report.errors.append("Element balance requires a feed specification with component formulas.")
        return report
    if not inlet_streams or not outlet_streams:
        report.errors.append("Element balance requires at least one inlet and one outlet stream.")
        return report

    def elemental_rates(streams: list) -> dict[str, float]:
        totals: dict[str, float] = {}
        for stream in streams:
            if stream.molar_flow_kmol_h is not None and abs(stream.molar_flow_kmol_h) < 1e-10:
                continue
            if stream.molar_flow_kmol_h is None or not stream.mole_fractions:
                report.errors.append(f"Stream {stream.name} lacks molar flow or composition for balance.")
                continue
            for name, fraction in stream.mole_fractions.items():
                component = next((item for item in spec.feed.components if item.name == name), None)
                formula = component.formula if component else None
                if formula is None:
                    formula = next((term.formula for reaction in spec.reactions
                                    for term in [*reaction.reactants, *reaction.products]
                                    if term.component == name), None)
                if formula is None:
                    formula = next((item.formula for item in spec.allowed_species if item.name == name), None)
                if formula is None:
                    report.errors.append(f"No molecular formula is recorded for result component {name}.")
                    continue
                for element, count in parse_formula(formula).items():
                    totals[element] = totals.get(element, 0.0) + stream.molar_flow_kmol_h * fraction * count
        return totals

    inlet = elemental_rates(inlet_streams)
    outlet = elemental_rates(outlet_streams)
    for element in sorted(set(inlet) | set(outlet)):
        left, right = inlet.get(element, 0.0), outlet.get(element, 0.0)
        scale = max(abs(left), abs(right), 1e-12)
        error = abs(left - right) / scale
        if error > relative_tolerance:
            report.errors.append(
                f"Element {element} balance relative error {error:.6g} exceeds {relative_tolerance:.6g}."
            )
    return report



def validate_backend_spec(spec: ReactorModelSpec) -> ValidationReport:
    """Backend restrictions diagnose the chosen model; they never choose a replacement."""
    report = ValidationReport()
    if (spec.thermo.property_package or "").casefold() not in {"peng-robinson", "peng robinson", "pr", "pengrob"}:
        report.errors.append("This adapter currently verifies only Peng-Robinson; confirm a suitable property method.")
    kp = spec.known_parameters
    if kp.thermal_mode not in {"specified_temperature", "adiabatic"}:
        report.errors.append("Specify thermal_mode: specified_temperature or adiabatic; no silent thermal assumption.")
    if kp.thermal_mode == "specified_temperature" and kp.target_outlet_temperature is None:
        report.errors.append("Specified-temperature operation requires target_outlet_temperature.")
    if kp.target_pressure is None:
        report.errors.append("Specify target_pressure, including an explicit zero-pressure-drop assumption if appropriate.")
    elif spec.feed and spec.feed.pressure is not None:
        if convert_pressure(kp.target_pressure, kp.target_pressure_unit) > convert_pressure(spec.feed.pressure, spec.feed.pressure_unit) + 1e-6:
            report.errors.append("This reactor adapter cannot increase pressure above the inlet pressure.")
    if spec.reactor_type == ReactorType.GIBBS and not spec.thermo.thermodynamic_data_source:
        report.errors.append("Confirm the thermodynamic data source and the Gibbs species universe (e.g. HYSYS databank; no solids).")
    if spec.reactor_type == ReactorType.CONVERSION:
        if len(spec.reactions) != 1:
            report.errors.append("Only a single conversion reaction has been verified in this adapter.")
        for reaction in spec.reactions:
            if reaction.phase not in {"vapor", "vapour", "gas"}:
                report.errors.append("Only explicitly specified vapor-phase conversion reactions are currently verified.")
            if reaction.basis_component not in {term.component for term in reaction.reactants}:
                report.errors.append("Each conversion reaction requires an explicit reactant basis_component.")
            if len(spec.reactions) > 1 and reaction.conversion is None:
                report.errors.append("Multiple conversion reactions require a separate conversion per reaction.")
    formulas = {}
    terms = [(item.name, item.formula) for item in spec.feed.components] if spec.feed else []
    terms += [(item.name, item.formula) for item in spec.allowed_species]
    terms += [(term.component, term.formula) for rxn in spec.reactions for term in [*rxn.reactants, *rxn.products]]
    for name, formula in terms:
        if name.casefold() in formulas and formulas[name.casefold()] != parse_formula(formula):
            report.errors.append(f"Conflicting formulas for component {name}.")
        formulas[name.casefold()] = parse_formula(formula)
    return report


def validate_engineering(spec: ReactorModelSpec, result: SimulationResult) -> ValidationReport:
    report = validate_result(result)
    if len(result.streams) != 3:
        report.errors.append("Expected one inlet and both vapor/liquid boundary outlets.")
        return report
    feed, *products = result.streams
    report.errors.extend(validate_element_balance(spec, [feed], products).errors)
    for stream in result.streams:
        if stream.molar_flow_kmol_h is None or stream.molar_flow_kmol_h < -1e-9:
            report.errors.append(f"Invalid flow in {stream.name}.")
        if stream.temperature_k is None or stream.temperature_k <= 0 or stream.pressure_kpa is None or stream.pressure_kpa <= 0:
            report.errors.append(f"Unknown or invalid temperature/pressure in {stream.name}.")
    kp = spec.known_parameters
    if spec.feed and spec.feed.flow is not None:
        if spec.feed.flow_basis == "molar":
            actual, expected = feed.molar_flow_kmol_h, convert_flow(spec.feed.flow, spec.feed.flow_unit)
        else:
            actual, expected = result.raw_summary.get("feed_mass_flow_kg_h"), convert_mass_flow(spec.feed.flow, spec.feed.flow_unit)
        if actual is None or abs(actual - expected) > max(abs(expected) * 1e-6, 1e-8):
            report.errors.append("HYSYS feed flow does not match the specification.")
    for stream in products:
        if not stream.molar_flow_kmol_h or stream.molar_flow_kmol_h < 1e-10:
            continue
        if kp.target_pressure is not None and abs(stream.pressure_kpa - convert_pressure(kp.target_pressure, kp.target_pressure_unit)) > 0.1:
            report.errors.append(f"Outlet pressure target missed: {stream.name}.")
        if kp.thermal_mode == "specified_temperature" and kp.target_outlet_temperature is not None:
            if abs(stream.temperature_k - convert_temperature(kp.target_outlet_temperature, kp.target_outlet_temperature_unit)) > 0.1:
                report.errors.append(f"Outlet temperature target missed: {stream.name}.")
    if spec.reactor_type == ReactorType.CONVERSION and len(spec.reactions) == 1:
        reaction = spec.reactions[0]
        name = reaction.basis_component
        if name:
            incoming = (feed.molar_flow_kmol_h or 0) * feed.mole_fractions.get(name, 0)
            outgoing = sum((s.molar_flow_kmol_h or 0) * s.mole_fractions.get(name, 0) for s in products)
            expected = reaction.conversion if reaction.conversion is not None else kp.conversion
            if incoming <= 0 or expected is None or abs((incoming - outgoing) / incoming - expected) > 1e-4:
                report.errors.append("Boundary-stream conversion does not match the specified conversion.")
    return report
