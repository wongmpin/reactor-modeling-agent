"""Conversation facade tests use explicit model/backend substitutes, never HYSYS results."""
from pathlib import Path
import pytest
from reactor_agent.chat import ChatApplication
from reactor_agent.domain.models import ReactorModelSpec, ReactorType, Assumption, SimulationResult
from reactor_agent.domain.tools import ConversationReply
from reactor_agent.service import ReactorAgentService


class Provider:
    action = "update_spec"
    fail = False
    def conversation_reply(self, spec, message, history, latest_run=None, recent_runs=None):
        if self.fail:
            raise RuntimeError("Model temporarily unavailable")
        return ConversationReply(action=self.action, reply="已收到。")
    def parse_spec(self, request):
        spec = ReactorModelSpec.model_validate_json((Path(__file__).parents[1] / "examples/hydrogenation.json").read_text(encoding="utf-8"))
        spec.user_request = request
        return spec
    def merge_spec(self, spec, request):
        return spec.model_copy(update={"user_request": spec.user_request + request})
    def select_reactor(self, spec):
        return ReactorType.CONVERSION, "User supplied conversion and no kinetics."


@pytest.fixture
def chat(database, tmp_path):
    return ChatApplication(ReactorAgentService(database=database, data_dir=tmp_path, provider=Provider()))


def test_questions_never_change_spec_or_queue_and_modifications_create_versions(chat):
    task_id = chat.send(None, "Model hydrogenation")
    assert chat.database.get_spec(task_id).version == 2
    assert chat.service.execution_readiness(task_id)["ready"]
    chat.service.provider.action = "answer"
    before = chat.database.get_spec(task_id).model_dump_json()
    chat.send(task_id, "Why this reactor? Do not run.")
    assert chat.database.get_spec(task_id).model_dump_json() == before
    assert not chat.database.active_queue_items(task_id)
    chat.service.provider.action = "update_spec"
    chat.send(task_id, "Change outlet conditions")
    assert chat.database.get_spec(task_id).version == 3
    assert chat.database.get_spec(task_id, 2).model_dump_json() == before


def test_explicit_run_queues_current_version_and_readonly_questions_do_not(chat, monkeypatch):
    task_id = chat.send(None, "Model hydrogenation")
    def queue(task_id):
        spec = chat.database.get_spec(task_id)
        return chat.database.enqueue(task_id, {"spec_version": spec.version})
    monkeypatch.setattr(chat.service, "queue_execution", queue)
    chat.service.provider.action = "run"
    chat.send(task_id, "Run current confirmed conditions")
    chat.send(task_id, "Run current confirmed conditions")
    assert len(chat.database.active_queue_items(task_id)) == 1
    assert sum(m["metadata"].get("kind") == "queued" for m in chat.database.chat_messages(task_id)) == 1


def test_missing_data_and_unconfirmed_assumptions_block_execution(chat, monkeypatch):
    task = chat.service.create_task("incomplete", ReactorModelSpec(assumptions=[Assumption(key="hours", value="8000", rationale="proposal")]))
    calls = []
    monkeypatch.setattr(chat.service, "queue_execution", lambda task_id: calls.append(task_id))
    chat.submit_run(task.task_id)
    assert not calls
    assert not chat.database.get_spec(task.task_id).assumptions[0].confirmed_by_user
    with pytest.raises(ValueError, match="版本已更新"):
        chat.confirm(task.task_id, 999, ["hours"])
    with pytest.raises(ValueError, match="已更新"):
        chat.confirm(task.task_id, 1, ["made_up"])


def test_model_failure_preserves_message_and_can_retry(chat):
    chat.service.provider.fail = True
    task_id = chat.send(None, "new requirement")
    assert chat.database.get_spec(task_id).version == 1
    assert chat.database.chat_messages(task_id)[-1]["metadata"]["kind"] == "error"
    chat.service.provider.fail = False
    chat.send(task_id, "new requirement, retry")
    assert chat.database.get_spec(task_id).version == 2


def test_persistent_transcript_dedup_and_task_isolation(chat):
    first = chat.send(None, "first")
    second = chat.send(None, "second")
    run = SimulationResult(task_id=first, spec_version=2)
    chat.database.save_run(run.run_id, first, 2, "failed", run.model_dump(mode="json"))
    restored = ChatApplication(ReactorAgentService(database=chat.database, data_dir=chat.service.data_dir))
    restored.ensure_restored_conversation(first)
    restored.ensure_restored_conversation(first)
    messages = restored.export(first)["messages"]
    assert [m["role"] for m in messages] == ["user", "assistant", "assistant"]
    assert messages[-1]["metadata"]["run_id"] == run.run_id
    assert all(m["task_id"] == first for m in messages)
    assert len(chat.database.chat_messages(second)) == 2


def test_chat_passes_actual_multi_version_runs_to_model(chat, monkeypatch):
    task_id = chat.send(None, 'first')
    for version in [1, 2]:
        result = SimulationResult(task_id=task_id, spec_version=version)
        chat.database.save_run(result.run_id, task_id, version, 'failed', result.model_dump(mode='json'))
    received = []
    def reply(spec, message, history, latest_run=None, recent_runs=None):
        received.extend(recent_runs)
        return ConversationReply(action='answer', reply='These runs failed engineering checks.')
    monkeypatch.setattr(chat.service.provider, 'conversation_reply', reply)
    chat.send(task_id, 'Compare both versions; do not run.')
    assert {row['result']['spec_version'] for row in received} == {1, 2}
    assert all(row['status'] == 'failed' for row in received)
