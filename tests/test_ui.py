from pathlib import Path

from streamlit.testing.v1 import AppTest


def test_streamlit_page_renders_without_hysys_or_api_key(tmp_path, monkeypatch):
    monkeypatch.setenv("REACTOR_AGENT_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("REACTOR_AGENT_DB", str(tmp_path / "ui.sqlite3"))
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    script = Path(__file__).parents[1] / "reactor_agent" / "ui.py"
    app = AppTest.from_file(script, default_timeout=15).run()
    assert not app.exception
    assert app.title[0].value == "化工反应器建模 Agent"
    assert any("反应需求" in item.label for item in app.text_area)



def test_streamlit_resume_creates_version_and_keeps_selected_task(tmp_path, monkeypatch):
    from reactor_agent.service import ReactorAgentService
    from reactor_agent.domain.models import ReactorModelSpec, ReactorType
    monkeypatch.setenv("REACTOR_AGENT_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("REACTOR_AGENT_DB", str(tmp_path / "ui.sqlite3"))
    service = ReactorAgentService()
    task = service.create_task("UI clarification")
    def process(self, task_id, request="", spec_json=""):
        current = ReactorModelSpec.model_validate_json(spec_json)
        revised = current.next_version(user_request=current.user_request + request)
        self.database.save_spec(revised, "needs_input")
        return {"status": "needs_input", "spec": revised.model_dump(mode="json"), "diagnostics": ["Supply flow"]}
    monkeypatch.setattr(ReactorAgentService, "process_request", process)
    script = Path(__file__).parents[1] / "reactor_agent/ui.py"
    app = AppTest.from_file(script, default_timeout=15).run()
    app.text_area(key=f"resume_text_{task.task_id}").input("more input")
    app.button(key="resume_task").click().run()
    assert not app.exception
    assert service.database.get_spec(task.task_id).version == 2
    assert app.session_state["last_task_id"] == task.task_id


def test_missing_inputs_disable_submit_and_confirmation_is_explicit(tmp_path, monkeypatch):
    from reactor_agent.service import ReactorAgentService
    from reactor_agent.domain.models import Assumption
    monkeypatch.setenv("REACTOR_AGENT_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("REACTOR_AGENT_DB", str(tmp_path / "ui.sqlite3"))
    service = ReactorAgentService()
    task = service.create_task("needs inputs")
    service.revise_spec(task.task_id, {"assumptions": [Assumption(key="hours", value="8000 h/a", rationale="proposal").model_dump()]})
    called = []
    def resolve(self, task_id, clarification, confirmed_keys, spec_json=""):
        called.append((task_id, confirmed_keys))
        return {"status": "needs_input", "diagnostics": []}
    monkeypatch.setattr(ReactorAgentService, "resolve_input", resolve)
    app = AppTest.from_file(Path(__file__).parents[1] / "reactor_agent/ui.py", default_timeout=15).run()
    assert app.button(key="submit_hysys").disabled
    assert app.button(key="confirm_inputs").disabled
    key = f"confirm_{task.task_id}_v2_hours"
    assert not app.checkbox(key=key).value
    app.checkbox(key=key).check().run()
    assert not app.button(key="confirm_inputs").disabled
    assert not service.database.get_spec(task.task_id).assumptions[0].confirmed_by_user
    app.button(key="confirm_inputs").click().run()
    assert called == [(task.task_id, ["hours"])]
    assert not app.exception
