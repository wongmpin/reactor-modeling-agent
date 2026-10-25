import json

import pytest

from reactor_agent.domain.models import (
    Assumption,
    BackendCapabilities,
    ComponentFraction,
    FeedStream,
    KnownParameters,
    ReactionSpec,
    ReactionTerm,
    ReactorModelSpec,
    ReactorType,
)
from reactor_agent.service import ReactorAgentService


def usable_conversion_spec():
    return ReactorModelSpec(
        user_request="carbon monoxide shift",
        feed=FeedStream(
            components=[ComponentFraction(name="CO", formula="CO", value=0.5),
                        ComponentFraction(name="Water", formula="H2O", value=0.5)],
            flow=100, temperature=300, pressure=10,
        ),
        reactions=[ReactionSpec(
            reactants=[ReactionTerm(component="CO", formula="CO", coefficient=1),
                       ReactionTerm(component="Water", formula="H2O", coefficient=1)],
            products=[ReactionTerm(component="CO2", formula="CO2", coefficient=1),
                      ReactionTerm(component="Hydrogen", formula="H2", coefficient=1)],
        )],
        reactor_type=ReactorType.CONVERSION,
        reactor_rationale="Known conversion case.",
        known_parameters=KnownParameters(conversion=0.2),
    )


def test_queue_gate_checks_backend_without_replacing_model_choice(database, monkeypatch):
    import reactor_agent.hysys.worker as worker_module

    spec = usable_conversion_spec()
    database.create_task(spec)

    class FakeWorker:
        def __init__(self, **kwargs): pass
        def inspect(self):
            return BackendCapabilities(backend="fake", detected=True, supported_reactors=[])
        def stop(self): pass

    monkeypatch.setattr(worker_module, "HysysWorker", FakeWorker)
    service = ReactorAgentService(database=database)
    with pytest.raises(ValueError, match="Backend capability gate"):
        service.queue_execution(spec.task_id)
    assert database.get_spec(spec.task_id).reactor_type == ReactorType.CONVERSION


def test_queue_worker_failure_is_not_marked_solved(database, monkeypatch):
    import reactor_agent.hysys.worker as worker_module

    spec = usable_conversion_spec()
    database.create_task(spec)
    database.enqueue(spec.task_id, {"spec_version": 1, "spec": json.loads(spec.model_dump_json())})

    class FakeWorker:
        def __init__(self, **kwargs): pass
        def inspect(self):
            return BackendCapabilities(backend="fake", detected=True, supported_reactors=[])
        def stop(self): pass

    monkeypatch.setattr(worker_module, "HysysWorker", FakeWorker)
    outcome = ReactorAgentService(database=database).run_one_queued()
    assert outcome["status"] == "failed"
    assert outcome["run"]["case_created"] is False
    assert outcome["run"]["solver_converged"] is False
    assert outcome["run"]["engineering_validation_passed"] is False


def test_service_checkpoint_and_clarification_preserve_task_and_versions(database, tmp_path):
    class Provider:
        def parse_spec(self, request):
            return usable_conversion_spec()

        def merge_spec(self, current, clarification):
            return current.model_copy(update={"user_request": current.user_request + "; " + clarification})

        def select_reactor(self, spec):
            return ReactorType.CONVERSION, "A known conversion is available."

    service = ReactorAgentService(database=database, data_dir=tmp_path, provider=Provider())
    task = service.create_task("initial")
    first = service.process_request(task.task_id, request="initial")
    assert first["status"] == "spec_validated"
    assert first["spec"]["version"] == 2
    second = service.process_request(task.task_id, request="confirm pressure", spec_json=json.dumps(first["spec"]))
    assert second["spec"]["task_id"] == task.task_id
    assert second["spec"]["version"] == 3
    assert second["spec"]["known_parameters"]["conversion"] == 0.2
    assert database.get_spec(task.task_id, 2).user_request == "carbon monoxide shift"


def test_preflight_never_confirms_or_connects_backend_and_explicit_confirmations_are_audited(database, tmp_path):
    from reactor_agent.domain.models import Assumption
    class Provider:
        def merge_spec(self, current, clarification):
            assert current.assumptions[0].confirmed_by_user
            assert "8000" in clarification
            return current
        def select_reactor(self, spec): return ReactorType.GIBBS, "Thermodynamic species model"
    service = ReactorAgentService(database=database, data_dir=tmp_path, provider=Provider())
    spec = ReactorModelSpec(assumptions=[Assumption(key="hours", value="8000", rationale="proposed")])
    service.create_task("test", spec)
    ready = service.execution_readiness(spec.task_id)
    assert not ready["ready"]
    assert not database.get_spec(spec.task_id).assumptions[0].confirmed_by_user
    out = service.resolve_input(spec.task_id, "user confirms", ["hours"])
    assert out["spec"]["assumptions"][0]["confirmed_by_user"]
    assert database.get_spec(spec.task_id).version == 2
    assert any(e["event_type"] == "user_assumptions_confirmed" for e in database.audit_events(spec.task_id))
    with pytest.raises(ValueError, match="Unknown assumption"):
        service.resolve_input(spec.task_id, "", ["invented_key"])


def test_confirmed_equal_xylene_distribution_expands_lumped_product_only_when_confirmed():
    from reactor_agent.domain.validation import validate_spec
    spec = ReactorModelSpec(
        feed=FeedStream(components=[ComponentFraction(name="甲苯", formula="C7H8", value=1)],
            flow=10000, flow_unit="kg/h", flow_basis="mass", temperature=380, pressure=25),
        reactions=[ReactionSpec(reactants=[ReactionTerm(component="甲苯", formula="C7H8", coefficient=2)],
            products=[ReactionTerm(component="苯", formula="C6H6", coefficient=1),
                      ReactionTerm(component="二甲苯", formula="C8H10", coefficient=1)],
            phase="vapor", basis_component="甲苯", conversion=.5)],
        reactor_type=ReactorType.CONVERSION,
        known_parameters={"thermal_mode":"specified_temperature", "conversion":.5,
                          "target_outlet_temperature":380, "target_pressure":25},
        assumptions=[Assumption(key="二甲苯异构体分布", value="邻、间、对二甲苯等摩尔分配",
                                rationale="用户明确确认", confirmed_by_user=True)])
    unconfirmed = spec.model_copy(deep=True)
    normalized = ReactorAgentService._apply_confirmed_xylene_distribution(spec)
    products = normalized.reactions[0].products
    assert [(p.component, p.coefficient) for p in products[1:]] == [
        ("邻二甲苯", pytest.approx(1/3)), ("间二甲苯", pytest.approx(1/3)),
        ("对二甲苯", pytest.approx(1/3))]
    assert normalized.known_parameters.selectivity == {
        "邻二甲苯": pytest.approx(1/3), "间二甲苯": pytest.approx(1/3), "对二甲苯": pytest.approx(1/3)}
    assert validate_spec(normalized).passed

    unconfirmed.assumptions[0].confirmed_by_user = False
    unchanged = ReactorAgentService._apply_confirmed_xylene_distribution(unconfirmed)
    assert unchanged.reactions[0].products[-1].component == "二甲苯"
