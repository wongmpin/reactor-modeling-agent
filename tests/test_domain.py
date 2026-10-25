from reactor_agent.domain.models import (
    Assumption,
    ComponentFraction,
    FeedStream,
    KnownParameters,
    ReactionSpec,
    ReactionTerm,
    ReactorModelSpec,
    ReactorType,
    SimulationResult,
    StreamResult,
)
from reactor_agent.domain.validation import (
    parse_formula,
    validate_element_balance,
    validate_reactor_choice,
    validate_result,
    validate_spec,
)


def water_gas_shift_spec(**overrides):
    payload = {
        "feed": FeedStream(
            components=[
                ComponentFraction(name="CO", formula="CO", value=0.5),
                ComponentFraction(name="Water", formula="H2O", value=0.5),
            ], flow=100, temperature=300, pressure=10,
        ),
        "reactions": [ReactionSpec(
            reactants=[ReactionTerm(component="CO", formula="CO", coefficient=1),
                       ReactionTerm(component="Water", formula="H2O", coefficient=1)],
            products=[ReactionTerm(component="CO2", formula="CO2", coefficient=1),
                      ReactionTerm(component="Hydrogen", formula="H2", coefficient=1)],
        )],
        "reactor_type": ReactorType.CONVERSION,
        "reactor_rationale": "Known conversion is supplied.",
        "known_parameters": KnownParameters(conversion=0.5),
    }
    payload.update(overrides)
    return ReactorModelSpec(**payload)


def test_formula_parser_and_balanced_reaction():
    assert parse_formula("C2H6O") == {"C": 2, "H": 6, "O": 1}
    report = validate_spec(water_gas_shift_spec())
    assert report.passed, report.errors


def test_formula_parser_rejects_unbalanced_parentheses():
    try:
        parse_formula("Al2(SO4")
    except ValueError:
        pass
    else:
        raise AssertionError("parenthesized formulas must not be misread")


def test_unbalanced_reaction_is_diagnosed():
    spec = water_gas_shift_spec(reactions=[ReactionSpec(
        reactants=[ReactionTerm(component="CO", formula="CO", coefficient=1)],
        products=[ReactionTerm(component="CO2", formula="CO2", coefficient=1)],
    )])
    assert any("not element-balanced" in message for message in validate_spec(spec).errors)


def test_invalid_units_and_composition_are_rejected():
    spec = water_gas_shift_spec(feed=FeedStream(
        components=[ComponentFraction(name="CO", formula="CO", value=0.8),
                    ComponentFraction(name="Water", formula="H2O", value=0.8)],
        flow=1, flow_unit="banana/hour", temperature=25, pressure=1,
    ))
    report = validate_spec(spec)
    assert not report.passed
    assert any("sum" in message for message in report.errors)
    assert any("flow unit" in message for message in report.errors)


def test_model_choice_is_checked_but_never_rewritten():
    spec = water_gas_shift_spec(reactor_type=ReactorType.PFR, known_parameters=KnownParameters())
    report = validate_reactor_choice(spec, supported={ReactorType.CONVERSION})
    assert any("not supported" in message for message in report.errors)
    assert spec.reactor_type == ReactorType.PFR


def test_unconfirmed_assumption_blocks_execution():
    spec = water_gas_shift_spec(assumptions=[Assumption(key="flow", value="100 kmol/h", rationale="example")])
    assert any("requires user confirmation" in message for message in validate_spec(spec).errors)


def test_conversion_requires_known_fraction():
    spec = water_gas_shift_spec(known_parameters=KnownParameters())
    assert any("requires a specified conversion" in message for message in validate_spec(spec).errors)


def test_isomer_products_require_selectivity():
    spec = ReactorModelSpec(
        feed=FeedStream(components=[ComponentFraction(name="Toluene", formula="C7H8", value=1)],
                        flow=1, temperature=300, pressure=10),
        reactions=[ReactionSpec(
            reactants=[ReactionTerm(component="Toluene", formula="C7H8", coefficient=2)],
            products=[ReactionTerm(component="Benzene", formula="C6H6", coefficient=1),
                      ReactionTerm(component="o-Xylene", formula="C8H10", coefficient=0.3),
                      ReactionTerm(component="m-Xylene", formula="C8H10", coefficient=0.4),
                      ReactionTerm(component="p-Xylene", formula="C8H10", coefficient=0.3)],
        )],
        reactor_type=ReactorType.CONVERSION,
        reactor_rationale="Known conversion is supplied.",
        known_parameters=KnownParameters(conversion=0.5),
    )
    assert any("provide product selectivity" in message for message in validate_spec(spec).errors)
    spec.known_parameters.selectivity = {"o-Xylene": 0.3, "m-Xylene": 0.4, "p-Xylene": 0.3}
    assert not any("provide product selectivity" in message for message in validate_spec(spec).errors)


def test_selectivity_outside_fraction_range_is_invalid():
    try:
        KnownParameters(selectivity={"o-Xylene": 1.2})
    except ValueError:
        pass
    else:
        raise AssertionError("selectivity fractions above one must be rejected")


def test_component_formula_conflict_is_diagnosed():
    spec = water_gas_shift_spec(reactions=[ReactionSpec(
        reactants=[ReactionTerm(component="CO", formula="CO2", coefficient=1),
                   ReactionTerm(component="Water", formula="H2O", coefficient=1)],
        products=[ReactionTerm(component="CO2", formula="CO2", coefficient=1),
                  ReactionTerm(component="Hydrogen", formula="H2", coefficient=1)],
    )])
    assert any("conflicting formulas" in message for message in validate_spec(spec).errors)


def test_intermediate_can_be_generated_by_another_reaction_independent_of_order():
    spec = water_gas_shift_spec()
    # Methane reforming generates CO used in the first reaction.
    spec.feed.components = [ComponentFraction(name="Methane", formula="CH4", value=0.25),
                            ComponentFraction(name="Water", formula="H2O", value=0.75)]
    spec.reactions.append(ReactionSpec(
        reactants=[ReactionTerm(component="Methane", formula="CH4", coefficient=1),
                   ReactionTerm(component="Water", formula="H2O", coefficient=1)],
        products=[ReactionTerm(component="CO", formula="CO", coefficient=1),
                  ReactionTerm(component="Hydrogen", formula="H2", coefficient=3)],
    ))
    assert not any("unavailable" in error for error in validate_spec(spec).errors)
    spec.feed.components = [ComponentFraction(name="Water", formula="H2O", value=1)]
    assert any("unavailable" in error for error in validate_spec(spec).errors)


def test_mass_flow_units_are_checked_as_mass():
    spec = water_gas_shift_spec(feed=FeedStream(
        components=[ComponentFraction(name="CO", formula="CO", value=0.5),
                    ComponentFraction(name="Water", formula="H2O", value=0.5)],
        flow=10000, flow_unit="kg/h", flow_basis="mass", temperature=300, pressure=10,
    ))
    assert validate_spec(spec).passed


def test_result_validation_rejects_non_normalized_stream():
    result = SimulationResult(
        task_id="task", spec_version=1, case_created=True, solver_converged=True,
        streams=[StreamResult(name="out", mole_fractions={"CO": 0.4, "H2": 0.4})],
    )
    report = validate_result(result)
    assert not report.passed
    assert any("sum to" in message for message in report.errors)


def test_boundary_element_balance_passes_for_water_gas_shift():
    spec = water_gas_shift_spec()
    inlet = [
        StreamResult(name="CO feed", molar_flow_kmol_h=1, mole_fractions={"CO": 1}),
        StreamResult(name="Water feed", molar_flow_kmol_h=1, mole_fractions={"Water": 1}),
    ]
    outlet = [
        StreamResult(name="CO2", molar_flow_kmol_h=1, mole_fractions={"CO2": 1}),
        StreamResult(name="Hydrogen", molar_flow_kmol_h=1, mole_fractions={"Hydrogen": 1}),
    ]
    assert validate_element_balance(spec, inlet, outlet).passed


def test_boundary_element_balance_reports_nonconservation():
    spec = water_gas_shift_spec()
    inlet = [StreamResult(name="CO feed", molar_flow_kmol_h=1, mole_fractions={"CO": 1})]
    outlet = [StreamResult(name="CO2", molar_flow_kmol_h=1, mole_fractions={"CO2": 1})]
    report = validate_element_balance(spec, inlet, outlet)
    assert any("balance relative error" in message for message in report.errors)
