from pathlib import Path
import pytest
from langgraph.types import Command
from reactor_agent.domain.models import ReactorModelSpec, ReactorType, SimulationResult, StreamResult
from reactor_agent.domain.validation import validate_engineering, validate_backend_spec
from reactor_agent.agent.graph import build_workflow, invoke_workflow
from reactor_agent.harness import Harness


def example():
    spec = ReactorModelSpec.model_validate_json((Path(__file__).parents[1] / "examples/hydrogenation.json").read_text(encoding="utf-8"))
    spec.reactor_type = ReactorType.CONVERSION
    spec.reactor_rationale = "Known conversion supplied"
    return spec


def result(spec, ethylene=0.375):
    return SimulationResult(task_id=spec.task_id, spec_version=spec.version, case_created=True,
        solver_converged=True, streams=[
            StreamResult(name="Feed", temperature_k=423.15, pressure_kpa=1000, molar_flow_kmol_h=100,
                         mole_fractions={"Ethylene": .5, "Hydrogen": .5}),
            StreamResult(name="Vapor", temperature_k=423.15, pressure_kpa=1000, molar_flow_kmol_h=80,
                         mole_fractions={"Ethylene": ethylene, "Hydrogen": .375, "Ethane": .25}),
            StreamResult(name="Liquid", temperature_k=423.15, pressure_kpa=1000, molar_flow_kmol_h=0),
        ])


def test_engineering_checks_independent_boundary_conversion_and_zero_phase():
    spec = example()
    assert validate_engineering(spec, result(spec)).passed
    spec.known_parameters.conversion = .3
    assert any("conversion" in error for error in validate_engineering(spec, result(spec)).errors)
    assert not validate_engineering(spec, result(spec, ethylene=.3)).passed


def test_harness_requires_exact_model_decision_after_change():
    import json
    spec = example()
    event = {"event_type": "model_decision", "summary": json.dumps({"spec_hash": Harness.fingerprint(spec)})}
    assert Harness().authorize_execution(spec, [event]).passed
    spec.known_parameters.conversion = .2
    assert not Harness().authorize_execution(spec, [event]).passed


def test_missing_execution_boundaries_are_diagnosed_without_changes():
    spec = example()
    spec.known_parameters.thermal_mode = None
    assert not validate_backend_spec(spec).passed
    assert spec.known_parameters.thermal_mode is None
    assert spec.reactor_type == ReactorType.CONVERSION


def test_workflow_interrupt_resume_and_finite_retry():
    class Provider:
        def parse_spec(self, request): return ReactorModelSpec(user_request=request)
        def merge_spec(self, current, request): return example()
        def select_reactor(self, spec): return ReactorType.CONVERSION, "Known conversion"
    graph = build_workflow(provider=Provider())
    config = {"configurable": {"thread_id": "interrupt"}}
    first = invoke_workflow(graph, request="incomplete", thread_id="interrupt")
    assert first["status"] == "needs_input"
    assert graph.get_state(config).next == ("clarify",)
    second = invoke_workflow(graph, request="complete", thread_id="interrupt")
    assert second["status"] == "spec_validated"
    attempts = []
    from reactor_agent.hysys.adapter import HysysAdapterError
    def executor(spec, attempt):
        attempts.append(attempt)
        raise HysysAdapterError("timeout", "test timeout", retryable=True)
    graph = build_workflow(provider=Provider(), executor=executor)
    out = graph.invoke({"execution_requested": True, "spec": example().model_dump(mode="json")}, config=config)
    assert out["status"] == "failed"
    assert attempts == [0, 1]


def test_result_schema_rejects_nan():
    with pytest.raises(ValueError):
        StreamResult(name="invalid", temperature_k=float("nan"))


def test_assumption_and_parameter_changes_need_new_model_authorization(database, tmp_path):
    from reactor_agent.service import ReactorAgentService
    class Provider:
        def select_reactor(self, spec): return ReactorType.CONVERSION, "Explicit conversion without kinetics"
    service = ReactorAgentService(database=database, data_dir=tmp_path, provider=Provider())
    task = service.create_task("test", example())
    service.process_request(task.task_id, spec_json=example().model_dump_json())
    saved = database.get_spec(task.task_id)
    assert Harness().authorize_execution(saved, database.audit_events(task.task_id)).passed
    service.revise_spec(task.task_id, {"known_parameters": {**saved.known_parameters.model_dump(), "conversion": .2}})
    assert not Harness().authorize_execution(database.get_spec(task.task_id), database.audit_events(task.task_id)).passed


def test_component_throughput_constraint_detects_branch_vs_total_flow_error():
    from reactor_agent.domain.validation import validate_spec
    spec = example()
    spec.feed.component_mass_flows_kg_h = {"Ethylene": 1400.0}
    actual = result(spec)
    actual.raw_summary["hysys_molecular_weights_kg_kmol"] = {"Ethylene": 28.0}
    assert validate_engineering(spec, actual).passed
    spec.feed.component_mass_flows_kg_h["Ethylene"] = 2800.0
    assert any("Component feed" in e for e in validate_engineering(spec, actual).errors)
    spec.feed.flow = None
    assert validate_spec(spec).passed


def test_hysys_component_aliases_cover_chinese_aromatics_and_reject_lumped_xylene():
    from reactor_agent.hysys.adapter import HysysComAdapter, HysysAdapterError
    aliases = {"甲苯":"Toluene", "苯":"Benzene", "邻二甲苯":"o-Xylene",
               "间二甲苯":"m-Xylene", "对二甲苯":"p-Xylene"}
    assert {name:HysysComAdapter._component_id(name) for name in aliases} == aliases
    with pytest.raises(HysysAdapterError, match="异构体总称"):
        HysysComAdapter._component_id("二甲苯")
