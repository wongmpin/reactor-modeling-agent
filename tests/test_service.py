import json

import pytest

from reactor_agent.domain.models import (
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
