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
    monkeypatch.setattr(DeepSeekProvider, "conversation_reply", lambda self, spec, message, history, latest_run=None, recent_runs=None, on_token=None:
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
    app.button(key=f"open_task_{first_id}").click().run()
    assert not app.exception
    assert any(m.value == "旧任务的条件" for m in app.markdown)
    # A fresh browser session can restore the same conversation through its URL.
    restored = AppTest.from_file(SCRIPT, default_timeout=15)
    restored.query_params["task"] = first_id
    restored.run()
    assert not restored.exception
    assert restored.session_state["selected_task"] == first_id
    assert len(restored.chat_message) == 2


def test_pending_task_id_is_refreshed_after_error_and_stays_selected(ui_service, monkeypatch):
    """A failed first turn must not send the browser back to the blank chat."""
    monkeypatch.setattr(DeepSeekProvider, "conversation_reply",
                        lambda self, *args, **kwargs: (_ for _ in ()).throw(RuntimeError("temporary provider error")))
    app = AppTest.from_file(SCRIPT, default_timeout=15).run()
    app.chat_input[0].set_value("新的独立测试场景").run()
    task_id = app.session_state["selected_task"]
    assert task_id != "__new__"
    assert app.query_params["task"] == task_id
    assert any("这一步暂时没有完成" in item.value for item in app.markdown)


def test_history_labels_include_task_short_id(ui_service):
    first = ui_service.create_task("相同名称的甲苯任务")
    second = ui_service.create_task("相同名称的甲苯任务")
    app = AppTest.from_file(SCRIPT, default_timeout=15).run()

    labels = [button.label for button in app.button if button.key.startswith("open_task_")]
    assert any(first.task_id[:8] in label for label in labels)
    assert any(second.task_id[:8] in label for label in labels)


def test_delete_history_requires_confirmation_and_removes_task(ui_service):
    first = ui_service.create_task("待删除的测试任务")
    second = ui_service.create_task("保留的测试任务")
    app = AppTest.from_file(SCRIPT, default_timeout=15)
    app.query_params["task"] = first.task_id
    app.run()
    app.button(key=f"delete_task_{first.task_id}").click().run()
    assert "确认删除" in [button.label for button in app.button]
    assert ui_service.database.get_spec(first.task_id).task_id == first.task_id
    app.button(key="confirm_delete_task").click().run()
    with pytest.raises(KeyError):
        ui_service.database.get_spec(first.task_id)
    assert ui_service.database.get_spec(second.task_id).task_id == second.task_id
    assert app.session_state["selected_task"] == "__new__"


def test_delete_history_rejects_active_queue(ui_service):
    task = ui_service.create_task("运行中的任务")
    ui_service.database.enqueue(task.task_id, {"spec_version": 1})
    with pytest.raises(ValueError, match="正在运行"):
        ui_service.database.delete_task(task.task_id)


def test_historical_assistant_diagnostics_are_localized_in_ui(ui_service):
    task = ui_service.create_task("历史诊断")
    ui_service.database.add_chat_message(task.task_id, "user", "测试")
    ui_service.database.add_chat_message(task.task_id, "assistant",
        "当前不能开始模拟：\\nComponent flow constraint CH4 requires a positive feed composition entry.\\nFeed pressure is required.",
        {"kind": "needs_input"})
    app = AppTest.from_file(SCRIPT, default_timeout=15).run()

    assert not app.exception
    assert any("组分流量约束 CH4" in item.value for item in app.markdown)
    assert any("必须提供进料压力" in item.value for item in app.markdown)
    assert not any("Feed pressure is required" in item.value for item in app.markdown)


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


def test_run_record_is_labeled_as_current_task_history(ui_service):
    task = ui_service.create_task("historical run")
    result = SimulationResult(task_id=task.task_id, spec_version=1, case_created=True)
    ui_service.database.save_run(result.run_id, task.task_id, 1, "failed", result.model_dump(mode="json"))
    app = AppTest.from_file(SCRIPT, default_timeout=15).run()

    assert not app.exception
    assert any("本任务" in item.value and "历史运行记录" in item.value for item in app.caption)
    assert any("本任务历史记录" in item.value for item in app.markdown)


def test_starting_simulation_keeps_task_selected_and_shows_pending_state(ui_service, monkeypatch):
    import json
    from reactor_agent.domain.models import ReactorModelSpec, ReactorType
    from reactor_agent.harness import Harness

    spec = ReactorModelSpec.model_validate_json((Path(__file__).parents[1] / "examples" / "hydrogenation.json").read_text(encoding="utf-8"))
    spec.reactor_type = ReactorType.CONVERSION
    spec.reactor_rationale = "Model-selected conversion reactor for a specified conversion."
    spec = ui_service.create_task("ready task", spec)
    ui_service.database.audit(spec.task_id, "model_decision", json.dumps({"spec_hash":Harness.fingerprint(spec)}))
    def queue(task_id):
        current = ui_service.database.get_spec(task_id)
        return ui_service.database.enqueue(task_id, {"spec_version":current.version,"spec":json.loads(current.model_dump_json())})
    monkeypatch.setattr(ui_service, "queue_execution", queue)

    app = AppTest.from_file(SCRIPT, default_timeout=15)
    app.query_params["task"] = spec.task_id
    app.run()
    assert app.button(key="submit_hysys").disabled is False
    app.button(key="submit_hysys").click().run()

    assert not app.exception
    assert app.session_state["selected_task"] == spec.task_id
    assert app.query_params["task"] == spec.task_id
    assert len(ui_service.database.active_queue_items(spec.task_id)) == 1
    assert any("等待 Worker" in item.value for item in app.info)


def test_url_task_is_restored_when_success_rerun_temporarily_selects_new_chat(ui_service):
    task = ui_service.create_task("completed task")
    result = SimulationResult(task_id=task.task_id, spec_version=1, case_created=True,
                              solver_converged=True, engineering_validation_passed=True)
    ui_service.database.save_run(result.run_id, task.task_id, 1, "validated", result.model_dump(mode="json"))
    app = AppTest.from_file(SCRIPT, default_timeout=15)
    app.query_params["task"] = task.task_id
    app.run()
    assert app.session_state["selected_task"] == task.task_id
    app.session_state["selected_task"] = "__new__"
    app.run()
    assert app.session_state["selected_task"] == task.task_id
    assert any("真实模拟通过工程校验" in item.value for item in app.success)
