import importlib.util
import json
from pathlib import Path


def test_export_excludes_private_channels_and_redacts_credentials(tmp_path, monkeypatch):
    path = Path(__file__).parents[1] / "scripts/export_session.py"
    module_spec = importlib.util.spec_from_file_location("export_session", path)
    module = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(module)
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("DEEPSEEK_API_KEY=secret-for-export-test", encoding="utf-8")
    rows = []
    for role, channel, text in [("developer", None, "private"), ("assistant", "analysis", "reasoning"),
                                 ("user", None, "secret-for-export-test"), ("assistant", "final", "done")]:
        rows.append({"type": "response_item", "payload": {"type": "message", "role": role,
                     "channel": channel, "content": [{"type": "input_text", "text": text}]}})
    source = tmp_path / "session.jsonl"
    source.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")
    destination = tmp_path / "export.md"
    assert module.export(source, destination) == 2
    output = destination.read_text(encoding="utf-8")
    assert "secret-for-export-test" not in output
    assert "private" not in output and "reasoning" not in output
    assert "[REDACTED]" in output and "done" in output
