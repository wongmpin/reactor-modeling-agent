from __future__ import annotations

import sqlite3
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt
from langgraph.checkpoint.memory import InMemorySaver

from reactor_agent.agent.model import DeepSeekProvider, ModelUnavailable
from reactor_agent.domain.models import ReactorModelSpec, SimulationResult, RunStatus
from reactor_agent.domain.validation import (validate_reactor_choice, validate_spec,
    validate_backend_spec, validate_engineering)


class WorkflowState(TypedDict, total=False):
    request: str
    spec_json: str
    spec: dict[str, Any]
    diagnostics: list[str]
    status: str
    retry_count: int
    model_rationale: str
    execution_requested: bool
    result: dict[str, Any]
    retryable: bool


def build_workflow(provider: Any | None = None, connection: sqlite3.Connection | None = None, executor=None):
    provider = provider or DeepSeekProvider()

    def parse_request(state):
        if state.get("spec_json"):
            spec = ReactorModelSpec.model_validate_json(state["spec_json"])
            if state.get("request"):
                spec = provider.merge_spec(spec, state["request"])
        else:
            spec = provider.parse_spec(state.get("request", ""))
        return {"spec": spec.model_dump(mode="json"), "diagnostics": [], "result": {}, "retry_count": 0}

    def select_reactor(state):
        spec = ReactorModelSpec.model_validate(state["spec"])
        selected, rationale = provider.select_reactor(spec)
        spec = spec.model_copy(update={"reactor_type": selected, "reactor_rationale": rationale})
        return {"spec": spec.model_dump(mode="json"), "model_rationale": rationale}

    def validate(state):
        spec = ReactorModelSpec.model_validate(state["spec"])
        checks = [*validate_spec(spec).errors, *validate_reactor_choice(spec).errors]
        if state.get("execution_requested"):
            checks.extend(validate_backend_spec(spec).errors)
        status = RunStatus.NEEDS_INPUT.value if checks else RunStatus.SPEC_VALIDATED.value
        return {"diagnostics": checks, "status": status, "spec": spec.model_dump(mode="json")}

    def clarify(state):
        answer = interrupt({"type": "clarification", "diagnostics": state.get("diagnostics", []),
                            "spec": state["spec"]})
        if answer.get("spec_json"):
            spec = ReactorModelSpec.model_validate_json(answer["spec_json"])
        else:
            spec = ReactorModelSpec.model_validate(state["spec"])
        if answer.get("request"):
            spec = provider.merge_spec(spec, answer["request"])
        return {"spec": spec.model_dump(mode="json"), "diagnostics": []}

    def execute(state):
        if executor is None:
            return {"status": "failed", "diagnostics": ["No execution backend configured."], "retryable": False}
        try:
            result = executor(ReactorModelSpec.model_validate(state["spec"]), state.get("retry_count", 0))
            return {"result": result.model_dump(mode="json"), "retryable": False, "status": "executed"}
        except Exception as exc:
            return {"status": "failed", "diagnostics": [str(exc)],
                    "retryable": bool(getattr(exc, "retryable", False)),
                    "retry_count": state.get("retry_count", 0) + 1}

    def verify(state):
        result = SimulationResult.model_validate(state["result"])
        report = validate_engineering(ReactorModelSpec.model_validate(state["spec"]), result)
        result.engineering_validation_passed = report.passed
        result.diagnostics.extend(report.errors)
        status = "validated" if report.passed else ("validation_failed" if result.solver_converged else "failed")
        return {"result": result.model_dump(mode="json"), "status": status, "diagnostics": result.diagnostics}

    builder = StateGraph(WorkflowState)
    for name, fn in [("parse_request", parse_request), ("select_reactor", select_reactor),
                     ("validate", validate), ("clarify", clarify), ("execute", execute), ("verify", verify)]:
        builder.add_node(name, fn)
    builder.add_conditional_edges(START, lambda s: "validate" if s.get("execution_requested") else "parse_request")
    builder.add_edge("parse_request", "select_reactor")
    builder.add_edge("select_reactor", "validate")
    builder.add_conditional_edges("validate", lambda s: ("clarify" if s["status"] == "needs_input" and not s.get("execution_requested")
        else END if s["status"] == "needs_input" else "execute" if s.get("execution_requested") else END))
    builder.add_edge("clarify", "select_reactor")
    builder.add_conditional_edges("execute", lambda s: "verify" if s["status"] == "executed" else
                                  "execute" if s.get("retryable") and s.get("retry_count", 0) < 2 else END)
    builder.add_edge("verify", END)
    if connection is not None:
        from langgraph.checkpoint.sqlite import SqliteSaver
        checkpointer = SqliteSaver(connection)
    else:
        checkpointer = InMemorySaver()
    return builder.compile(checkpointer=checkpointer)


def invoke_workflow(graph: Any, request: str = "", spec_json: str = "", thread_id: str = "task") -> dict[str, Any]:
    config = {"configurable": {"thread_id": thread_id}}
    try:
        snapshot = graph.get_state(config)
        if snapshot.next and "clarify" in snapshot.next:
            output = graph.invoke(Command(resume={"request": request, "spec_json": spec_json}), config=config)
        else:
            output = graph.invoke({"request": request, "spec_json": spec_json,
                                   "retry_count": 0, "execution_requested": False}, config=config)
        # Interrupt payloads are represented by the persisted status and diagnostics in the service protocol.
        return {key: value for key, value in output.items() if not key.startswith("__")}
    except ModelUnavailable as exc:
        return {"status": RunStatus.MODEL_UNAVAILABLE.value, "diagnostics": [str(exc)]}
