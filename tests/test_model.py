from types import SimpleNamespace

from reactor_agent.agent.model import DeepSeekProvider


def test_provider_uses_non_thinking_mode_for_forced_tool_probe(monkeypatch):
    calls = []

    def create(**kwargs):
        calls.append(kwargs)
        # Reproduce the API incompatibility seen in the live probe.
        if kwargs.get("tool_choice") and kwargs.get("extra_body") != {"thinking": {"type": "disabled"}}:
            raise ValueError("Thinking mode does not support this tool_choice")
        message = SimpleNamespace(content='{"ok": true}', tool_calls=[])
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])

    provider = DeepSeekProvider(api_key="test-only")
    monkeypatch.setattr(provider, "_client", lambda: SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))))
    probe = provider.check_connectivity()
    assert calls[1]["tool_choice"]["function"]["name"] == "ping"
    assert probe["ok"] is False  # A text response cannot pass a tool capability probe.
    assert probe["tool_roundtrip_valid"] is False


def test_invalid_json_retries_once_then_fails_without_fallback(monkeypatch):
    import pytest
    from reactor_agent.agent.model import ModelUnavailable
    count = []
    provider = DeepSeekProvider(api_key="test-only")
    def invalid(**kwargs):
        count.append(1)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=""))])
    monkeypatch.setattr(provider, "_chat", invalid)
    with pytest.raises(ModelUnavailable, match="two attempts"):
        provider._json_chat(messages=[], response_format={"type": "json_object"})
    assert len(count) == 2


def test_conversation_reply_consumes_sse_json_and_emits_incremental_text(monkeypatch):
    provider = DeepSeekProvider(api_key="test-only")
    sent = []
    chunks = ['{"action":"answer","reply":"甲烷重整', '，两个工况', '\\n读取已完成的第6版', ' \\"710°C\\"。"}']
    def stream(**kwargs):
        sent.append(kwargs)
        return [SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=value))]) for value in chunks]
    monkeypatch.setattr(provider, "_chat", stream)
    emitted = []
    from reactor_agent.domain.models import ReactorModelSpec
    reply = provider.conversation_reply(ReactorModelSpec(), "compare", [], on_token=emitted.append)
    assert reply.action == "answer"
    assert reply.reply == '甲烷重整，两个工况\n读取已完成的第6版 "710°C"。'
    assert ''.join(emitted) == reply.reply
    assert sent[0]["stream"] is True
    assert sent[0]["response_format"] == {"type": "json_object"}
