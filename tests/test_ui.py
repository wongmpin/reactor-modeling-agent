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
    app.text_input(key=f"resume_text_{task.task_id}").input("more input")
    app.button(key="resume_task").click().run()
    assert not app.exception
    assert service.database.get_spec(task.task_id).version == 2
    assert app.session_state["last_task_id"] == task.task_id
