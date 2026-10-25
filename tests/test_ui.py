from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest
from reactor_agent.agent.model import DeepSeekProvider
from reactor_agent.domain.models import Assumption, ReactorModelSpec, ReactorType, SimulationResult
from reactor_agent.domain.tools import ConversationReply
from reactor_agent.service import ReactorAgentService

SCRIPT = Path(__file__).parents[1] / "reactor_agent" / "ui.py"


@pytest.fixture
def ui_service(tmp_path, monkeypatch):
    monkeypatch.setenv("REACTOR_AGENT_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("REACTOR_AGENT_DB", str(tmp_path / "ui.sqlite3"))
    monkeypatch.setattr(DeepSeekProvider, "conversation_reply", lambda self, spec, message, history, latest_run=None, recent_runs=None:
        ConversationReply(action="answer" if message.endswith("？") else "update_spec", reply="已理解你的消息。"))
    monkeypatch.setattr(DeepSeekProvider, "parse_spec", lambda self, request: ReactorModelSpec(user_request=request))
    monkeypatch.setattr(DeepSeekProvider, "merge_spec", lambda self, spec, request: spec.model_copy(update={"user_request": spec.user_request + "；" + request}))
    monkeypatch.setattr(DeepSeekProvider, "select_reactor", lambda self, spec: (ReactorType.GIBBS, "需要确认热力学条件。"))
    return ReactorAgentService()


def test_chat_renders_without_hysys_or_api_key(ui_service, monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    app = AppTest.from_file(SCRIPT, default_timeout=15).run()
    assert not app.exception
    assert app.title[0].value == "化工反应器建模 Agent"
    assert len(app.chat_input) == 1
    assert len(app.chat_message) == 1
    assert not app.text_area


def test_two_turns_and_question_keep_task_and_spec_versions(ui_service):
    app = AppTest.from_file(SCRIPT, default_timeout=15).run()
    app.chat_input[0].set_value("模拟甲烷重整").run()
    assert not app.exception
    task_id = app.session_state["selected_task"]
    assert ui_service.database.get_spec(task_id).version == 2
    app.chat_input[0].set_value("进料压力13.5bar").run()
    assert not app.exception
    assert app.session_state["selected_task"] == task_id
    assert ui_service.database.get_spec(task_id).version == 3
    app.chat_input[0].set_value("为什么选择这个反应器？").run()
    assert not app.exception
    assert ui_service.database.get_spec(task_id).version == 3
    messages = ui_service.database.chat_messages(task_id)
    assert [m["role"] for m in messages] == ["user", "assistant"] * 3
    assert len(app.chat_message) == 6


def test_new_chat_isolated_and_old_chat_restores(ui_service):
    app = AppTest.from_file(SCRIPT, default_timeout=15).run()
    app.chat_input[0].set_value("旧任务的条件").run()
    first_id = app.session_state["selected_task"]
    app.button(key="new_chat").click().run()
    assert app.session_state["selected_task"] == "__new__"
    assert len(app.chat_message) == 1
    app.chat_input[0].set_value("乙醇脱水").run()
    second_id = app.session_state["selected_task"]
    assert second_id != first_id
    assert "旧任务" not in ui_service.database.get_spec(second_id).user_request
    app.selectbox(key="selected_task").select(first_id).run()
    assert not app.exception
    assert any(m.value == "旧任务的条件" for m in app.markdown)
    # A fresh browser session can restore the same conversation through its URL.
    restored = AppTest.from_file(SCRIPT, default_timeout=15)
    restored.query_params["task"] = first_id
    restored.run()
    assert not restored.exception
    assert restored.session_state["selected_task"] == first_id
    assert len(restored.chat_message) == 2


def test_missing_inputs_disable_run_and_require_explicit_confirmation(ui_service, monkeypatch):
    task = ui_service.create_task("needs inputs", ReactorModelSpec(assumptions=[Assumption(key="hours", value="8000 h/a", rationale="proposal")]))
    called = []
    def resolve(self, task_id, clarification, confirmed_keys, spec_json=""):
        called.append((task_id, confirmed_keys))
        return {"status": "needs_input"}
    monkeypatch.setattr(ReactorAgentService, "resolve_input", resolve)
    app = AppTest.from_file(SCRIPT, default_timeout=15).run()
    assert app.button(key="submit_hysys").disabled
    assert app.button(key="confirm_inputs").disabled
    key = f"confirm_{task.task_id}_v1_hours"
    assert not app.checkbox(key=key).value
    app.checkbox(key=key).check().run()
    assert not app.button(key="confirm_inputs").disabled
    assert not ui_service.database.get_spec(task.task_id).assumptions[0].confirmed_by_user
    app.button(key="confirm_inputs").click().run()
    assert called == [(task.task_id, ["hours"])]
    assert not app.exception


def test_assumption_card_uses_chinese_labels_and_enum_descriptions(ui_service):
    from reactor_agent.domain.models import Assumption
    task = ui_service.create_task("localized assumptions", ReactorModelSpec(assumptions=[
        Assumption(key="thermal_mode", value="specified_temperature", rationale="用户指定出口温度"),
        Assumption(key="phase", value="vapor", rationale="气相条件已确认"),
    ]))
    app = AppTest.from_file(SCRIPT, default_timeout=15).run()
    labels = [checkbox.label for checkbox in app.checkbox]
    assert "反应器热模式：指定出口温度（等温/指定温度模式）" in labels
    assert "反应相态：气相反应" in labels
    assert not any("thermal_mode" in label or "specified_temperature" in label or label.endswith(": vapor")
                   for label in labels)


def test_failed_result_is_in_chat_and_never_shown_as_success(ui_service):
    task = ui_service.create_task("failed simulation")
    result = SimulationResult(task_id=task.task_id, spec_version=1, case_created=True,
                              solver_converged=True, diagnostics=["Element imbalance detected"])
    ui_service.database.save_run(result.run_id, task.task_id, 1, "validation_failed", result.model_dump(mode="json"))
    app = AppTest.from_file(SCRIPT, default_timeout=15).run()
    assert not app.exception
    assert any("未通过全部检查" in item.value for item in app.error)
    assert not app.success
    app.run()
    assert sum(m["metadata"].get("kind") == "run_result" for m in ui_service.database.chat_messages(task.task_id)) == 1
