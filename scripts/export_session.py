"""Export user/assistant messages from an explicitly selected local Codex JSONL session.
Tools, system/developer messages and reasoning are excluded. Credentials are redacted.
"""
import argparse
import json
import os
from pathlib import Path
import re


def export(source, destination):
    secrets = []
    env = Path(".env")
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            if "=" in line:
                key, value = line.split("=", 1)
                if any(word in key.upper() for word in ("KEY", "TOKEN", "SECRET", "PASSWORD")):
                    value = value.strip().strip(chr(34)).strip(chr(39))
                    if len(value) >= 8:
                        secrets.append(value)
    blocks = ["# AI 开发会话导出", "仅包含用户与助手的可见消息；排除系统、工具输出与内部分析。密钥已做基础脱敏，分享前仍请人工复核。"]
    count = 0
    with Path(source).open(encoding="utf-8") as stream:
        for line in stream:
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            item = entry.get("payload", {})
            if entry.get("type") != "response_item" or item.get("type") != "message":
                continue
            if item.get("role") not in {"user", "assistant"} or item.get("channel") == "analysis":
                continue
            parts = [part.get("text", "") for part in item.get("content", []) if part.get("type") in {"input_text", "output_text", "text"}]
            text = "\n".join(parts)
            if not text or text.startswith("<environment_context>"):
                continue
            for secret in secrets:
                text = text.replace(secret, "[REDACTED]")
            text = re.sub(r"sk-[A-Za-z0-9_-]{12,}", "[REDACTED]", text)
            text = re.sub(r"(?i)(api[_ -]?key\s*[:=]\s*)[^\s`]+", r"\1[REDACTED]", text)
            blocks.append(f"## {entry.get('timestamp', '')} · {item['role']}\n\n{text}")
            count += 1
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text("\n\n".join(blocks), encoding="utf-8")
    return count


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("source", help="Exact session JSONL file selected for export")
    parser.add_argument("destination")
    args = parser.parse_args()
    print(f"Exported {export(args.source, args.destination)} visible messages")
