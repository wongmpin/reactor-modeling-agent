from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from openai import OpenAI

from reactor_agent.domain.models import ReactorModelSpec, ReactorType


class ModelUnavailable(RuntimeError):
    pass


class DeepSeekProvider:
    def __init__(self, api_key: str | None = None, base_url: str | None = None, model: str | None = None):
        self.api_key = api_key if api_key is not None else os.getenv("DEEPSEEK_API_KEY", "")
        self.base_url = base_url or os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
        self.model = model or os.getenv("DEEPSEEK_MODEL", "deepseek-flash")

    @staticmethod
    def _skill_instructions() -> str:
        path = Path(__file__).parents[1] / "skills" / "hysys-modeling" / "SKILL.md"
        try:
            return path.read_text(encoding="utf-8")
        except OSError:
            return "Use the reactor modeling protocol. Ask for missing inputs and never invent data."

    def _client(self) -> OpenAI:
        if not self.api_key:
            raise ModelUnavailable("DEEPSEEK_API_KEY is not configured.")
        return OpenAI(api_key=self.api_key, base_url=self.base_url, timeout=60, max_retries=1)

    def _chat(self, **kwargs: Any):
        try:
            kwargs.setdefault("extra_body", {"thinking": {"type": "disabled"}})
            return self._client().chat.completions.create(model=self.model, **kwargs)
        except ModelUnavailable:
            raise
        except Exception as exc:
            raise ModelUnavailable(f"DeepSeek API request failed ({type(exc).__name__}).") from exc

    def _json_chat(self, **kwargs):
        # JSON mode can return empty/invalid content; retry at most once, never invent a payload.
        for attempt in range(2):
            response = self._chat(**kwargs)
            try:
                value = json.loads(response.choices[0].message.content or "")
                if not isinstance(value, dict):
                    raise ValueError("Expected a JSON object")
                return value
            except (ValueError, TypeError):
                if attempt == 1:
                    raise ModelUnavailable("DeepSeek returned invalid/empty JSON after two attempts.")

    def parse_spec(self, request: str) -> ReactorModelSpec:
        schema = json.dumps(ReactorModelSpec.model_json_schema(), ensure_ascii=False)
        response = self._chat(
            messages=[
                {"role": "system", "content": (
                    "将用户化工反应需求转换为符合给定 JSON Schema 的 ReactorModelSpec。"
                    "不要补造未知参数；缺失项写入 missing_information；新任务不能继承历史条件。"
                    f"Schema: {schema}\nDomain workflow instructions:\n{self._skill_instructions()}"
                )},
                {"role": "user", "content": request},
            ],
            response_format={"type": "json_object"},
        )
        content = response.choices[0].message.content
        if not content:
            raise ModelUnavailable("DeepSeek returned empty structured output.")
        try:
            return ReactorModelSpec.model_validate_json(content)
        except Exception as exc:
            raise ModelUnavailable(f"DeepSeek response did not match the model protocol: {exc}") from exc

    def merge_spec(self, current: ReactorModelSpec, clarification: str) -> ReactorModelSpec:
        schema = json.dumps(ReactorModelSpec.model_json_schema(), ensure_ascii=False)
        response = self._chat(
            messages=[
                {"role": "system", "content": (
                    "把用户本轮补充合并到当前任务规格，返回符合 Schema 的完整 JSON。"
                    "保留未修改的已确认信息；不推断未给出的参数；新增假设需标为未确认。"
                    f"Schema: {schema}\nDomain workflow instructions:\n{self._skill_instructions()}"
                )},
                {"role": "user", "content": json.dumps({
                    "current_spec": current.model_dump(mode="json"),
                    "clarification": clarification,
                }, ensure_ascii=False)},
            ],
            response_format={"type": "json_object"},
        )
        content = response.choices[0].message.content
        if not content:
            raise ModelUnavailable("DeepSeek returned empty clarification merge.")
        try:
            merged = ReactorModelSpec.model_validate_json(content)
            return merged.model_copy(update={
                "task_id": current.task_id,
                "version": current.version,
                "parent_version": current.parent_version,
            })
        except Exception as exc:
            raise ModelUnavailable(f"Clarification did not match the model protocol: {exc}") from exc

    def select_reactor(self, spec: ReactorModelSpec) -> tuple[ReactorType, str]:
        choices = ", ".join(item.value for item in ReactorType)
        response = self._chat(
            messages=[
                {"role": "system", "content": (
                    "根据反应特征、已知参数和建模规则判断反应器。必须只返回 JSON："
                    '{"reactor_type":"...","rationale":"..."}。'
                    f"reactor_type 只能是：{choices}。缺数据时仍说明最合适候选及其所需数据，"
                    "规则：已给动力学模型和设备参数时选 CSTR 或 PFR，并依据相态/设备描述解释；"
                    "未给动力学但给定转化率、收率或选择性等工艺指标时可选 conversion；"
                    "可逆反应且提供平衡常数或 Gibbs 能数据时可选 equilibrium；"
                    "产物分布未知且热力学数据与组分覆盖足够时可考虑 gibbs。"
                    "注意：thermo.thermodynamic_data_source 只是数据库来源声明，不等于用户提供了 Ka(T) 或反应 Gibbs 能数值；"
                    "缺少这些数值时 equilibrium 候选应明确需要补参；如果用户已明确授权用数据库热力学数据、"
                    "确认物种全集和排除物种，则请独立评估 gibbs 能否按当前数据完整建模，而不是假定已知反应平衡常数。"
                    "这些规则用于判断而非硬编码映射。不得编造参数，不得基于场景名称、固定案例 ID 或案例映射作答。"
                    f"\nDomain workflow instructions:\n{self._skill_instructions()}"
                )},
                {"role": "user", "content": spec.model_dump_json()},
            ],
            response_format={"type": "json_object"},
        )
        content = response.choices[0].message.content
        if not content:
            raise ModelUnavailable("DeepSeek returned empty reactor selection.")
        try:
            data: dict[str, Any] = json.loads(content)
            return ReactorType(data["reactor_type"]), str(data["rationale"])
        except Exception as exc:
            raise ModelUnavailable(f"Invalid reactor selection response: {exc}") from exc

    def check_connectivity(self) -> dict[str, Any]:
        json_output = self._json_chat(
            messages=[{"role": "user", "content": "Return JSON only: {\"ok\": true}."}],
            response_format={"type": "json_object"},
        )
        tool_response = self._chat(
            messages=[{"role": "user", "content": "Use the function ping with value reactor-agent."}],
            tools=[{"type": "function", "function": {
                "name": "ping", "description": "Echo a probe payload", "parameters": {
                    "type": "object", "properties": {"value": {"type": "string"}},
                    "required": ["value"], "additionalProperties": False,
                },
            }}],
            tool_choice={"type": "function", "function": {"name": "ping"}},
        )
        tool_calls = tool_response.choices[0].message.tool_calls or []
        tool_call_received = bool(tool_calls)
        tool_arguments_valid = False
        roundtrip_valid = False
        if tool_calls:
            call = tool_calls[0]
            try:
                arguments = json.loads(call.function.arguments)
                tool_arguments_valid = call.function.name == "ping" and arguments == {"value": "reactor-agent"}
            except (json.JSONDecodeError, TypeError):
                arguments = None
            if tool_arguments_valid:
                completion = self._chat(messages=[
                    {"role": "user", "content": "Use ping with value reactor-agent. After receiving its result, return JSON {\"ok\": true, \"echo\": \"reactor-agent\"}."},
                    tool_response.choices[0].message.model_dump(exclude_none=True),
                    {"role": "tool", "tool_call_id": call.id, "content": json.dumps(arguments)},
                ], response_format={"type": "json_object"})
                try:
                    echoed = json.loads(completion.choices[0].message.content or "null")
                    roundtrip_valid = isinstance(echoed, dict) and echoed.get("ok") is True and echoed.get("echo") == "reactor-agent"
                except json.JSONDecodeError:
                    pass
        return {
            "ok": isinstance(json_output, dict) and json_output.get("ok") is True and tool_arguments_valid and roundtrip_valid,
            "model": self.model,
            "base_url": self.base_url,
            "json_output": json_output,
            "tool_call_received": tool_call_received,
            "tool_arguments_valid": tool_arguments_valid,
            "tool_roundtrip_valid": roundtrip_valid,
            "tool_call_name": tool_calls[0].function.name if tool_calls else None,
        }
