from types import SimpleNamespace

from reactor_agent.agent.model import DeepSeekProvider


def test_feed_schema_distinguishes_composition_basis_from_flow_basis():
    from reactor_agent.domain.models import FeedStream
    fields = FeedStream.model_json_schema()["properties"]
    assert fields["composition_basis"]["enum"] == ["mole_fraction", "mass_fraction"]
    assert fields["flow_basis"]["enum"] == ["molar", "mass"]


def test_component_flow_response_is_repaired_by_model_without_changing_process_inputs(monkeypatch):
    import copy
    from reactor_agent.domain.models import ReactorModelSpec

    invalid = {"feed": {"components": [
        {"name": "CO", "formula": "CO", "value": 100},
        {"name": "H2", "formula": "H2", "value": 350},
    ], "composition_basis": "molar", "flow": 450,
        "temperature": 250, "pressure": 10},
        "known_parameters": {"conversion": 0.7, "target_outlet_temperature": 350}}
    repaired = copy.deepcopy(invalid)
    repaired["feed"]["composition_basis"] = "mole_fraction"
    for component in repaired["feed"]["components"]:
        component["value"] /= 450
    responses = iter([invalid, repaired, invalid, repaired])
    calls = []
    provider = DeepSeekProvider(api_key="test-only")

    def reply(**kwargs):
        calls.append(copy.deepcopy(kwargs))
        return next(responses)

    monkeypatch.setattr(provider, "_json_chat", reply)
    request = "CO 100 kmol/h、H2 350 kmol/h，转化率70%，进料250°C，出口350°C，10bar"
    spec = provider.parse_spec(request)
    assert spec.feed.flow == 450
    assert [c.value for c in spec.feed.components] == [100 / 450, 350 / 450]
    assert spec.known_parameters.conversion == 0.7
    assert spec.known_parameters.target_outlet_temperature == 350
    assert calls[1]["messages"][1]["content"] == request
    assert "less_than_equal" in calls[1]["messages"][-1]["content"]
    current = ReactorModelSpec(version=4, parent_version=3)
    merged = provider.merge_spec(current, request)
    assert (merged.task_id, merged.version, merged.parent_version) == (current.task_id, 4, 3)


def test_protocol_repair_is_bounded_and_does_not_normalize_ambiguous_values(monkeypatch):
    import pytest
    from reactor_agent.agent.model import ModelUnavailable

    calls = []
    provider = DeepSeekProvider(api_key="test-only")
    invalid = {"feed": {"components": [{"name": "CO", "formula": "CO", "value": 100}],
                        "composition_basis": "molar"}}
    def reply(**kwargs):
        calls.append(kwargs)
        return invalid
    monkeypatch.setattr(provider, "_json_chat", reply)
    with pytest.raises(ModelUnavailable, match="进料组分组成分数.*进料组成基准") as error:
        provider.parse_spec("组分CO数值100，基准待确认")
    assert "本次请求未提交到 HYSYS" in str(error.value)
    assert len(calls) == 2
    assert invalid["feed"]["components"][0]["value"] == 100


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
