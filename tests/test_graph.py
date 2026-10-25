from reactor_agent.agent.graph import build_workflow, invoke_workflow
from reactor_agent.domain.models import ReactorModelSpec, ReactorType


class FakeProvider:
    def parse_spec(self, request):
        return ReactorModelSpec(user_request=request)

    def select_reactor(self, spec):
        return ReactorType.GIBBS, "The user describes product distribution without specifying reaction extents."


def test_reactor_choice_comes_from_provider_and_invalid_choice_is_not_replaced():
    spec = ReactorModelSpec(user_request="generic request")
    graph = build_workflow(provider=FakeProvider())
    out = graph.invoke({"spec_json": spec.model_dump_json()}, config={"configurable": {"thread_id": "one"}})
    assert out["spec"]["reactor_type"] == "gibbs"
    assert out["spec"]["reactor_rationale"].startswith("The user")
    assert out["status"] == "needs_input"
    assert out["spec"]["reactor_type"] == ReactorType.GIBBS.value


def test_checkpoint_can_resume_same_thread(tmp_path):
    import sqlite3

    spec = ReactorModelSpec(user_request="checkpoint")
    conn = sqlite3.connect(tmp_path / "checkpoint.sqlite", check_same_thread=False)
    graph = build_workflow(provider=FakeProvider(), connection=conn)
    first = graph.invoke({"spec_json": spec.model_dump_json()}, config={"configurable": {"thread_id": "persisted"}})
    second = graph.invoke({"spec_json": spec.model_dump_json()}, config={"configurable": {"thread_id": "persisted"}})
    assert first["spec"]["reactor_type"] == second["spec"]["reactor_type"]
    conn.close()


def test_missing_model_key_pauses_without_fake_reactor_choice(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    graph = build_workflow()
    spec = ReactorModelSpec(user_request="unparsed")
    out = invoke_workflow(graph, request="simulate something", thread_id="no-key")
    assert out["status"] == "model_unavailable"
    assert "DEEPSEEK_API_KEY" in out["diagnostics"][0]


def test_skill_is_versioned_and_loaded_from_project():
    from reactor_agent.agent.model import DeepSeekProvider

    skill = DeepSeekProvider._skill_instructions()
    assert "hysys-reactor-modeling" in skill
    assert "never invent" in skill.lower()
