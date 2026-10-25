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
                    "面向中文用户，澄清问题、假设理由和影响用中文表达，组分名称和协议枚举保留合法值。"
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
                    "澄清问题、假设理由和影响请用中文，用户只确认部分假设时不要确认其他项。"
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

    def conversation_reply(self, spec: ReactorModelSpec, message: str, history: list[dict], latest_run: dict | None = None,
                           recent_runs: list[dict] | None = None):
        from reactor_agent.domain.tools import ConversationReply
        data = self._json_chat(
            messages=[
                {"role": "system", "content": (
                    "你是化工反应器建模对话助手。用户用中文时请自然地用中文交流。"
                    "返回JSON {action,reply}，action只能是update_spec、answer、run、update_and_run。"
                    "update_spec：用户提出新建模需求、补参、确认假设、修改条件；由后续规格工作流处理。"
                    "answer：用户提问解释、询问进度/能力、问候，不修改规格。结合实际规格和真实运行回答。"
                    "run：用户明确要求按当前已确认条件运行；update_and_run：用户明确补参或改条件并要求运行。"
                    "仅在用户明确要求执行时使用run/update_and_run，否定、假设、询问如何运行不能视为执行授权。"
                    "reply为简洁友好的自然语言回复。不要输出内部JSON字段名或假装已经调用工具。"
                    "当action为update_spec或update_and_run时，reply只简短说明将核对条件；"
                    "不要提前列问题或宣称已保存条件，后续建模节点会产生完整澄清问题和选型依据。"
                    "不要编造参数、假设已被确认、运行状态或结果；只引用结构化真实结果。"
                    "recent_real_runs包含当前任务最近多版的真实记录；比较工况时应查阅这些记录而非只看最新一次。"
                    "工程校验失败的记录不能作为有效结果；规格不同的运行不能默认为相同条件。"
                    "先按真实读数核对增减方向，再解释；单个反应的平衡移动不等于耦合体系的最终组分变化。"
                    "不要用原理推断推翻已读出的真实数值；此前回答误读时应明确纠正。"
                    "若用户说按上一版/上述条件，优先参考当前任务规格及对话；新任务不得继承旧任务。"
                    "聊天上下文帮助理解指代，但规格和运行才是事实来源。不要在本步选择或修改反应器，选型由专门模型节点完成。"
                    "若有未确认假设，提醒用户逐项确认或在对话明确说明。"
                    "固定温压和进料配比下，总流量的同比缩放未必改变平衡摩尔组成，但绝对产量和热负荷需要流量。"
                    f"\nDomain workflow instructions:\n{self._skill_instructions()}" )},
                {"role": "user", "content": json.dumps({
                    "current_spec": spec.model_dump(mode="json"),
                    "recent_conversation": [{"role": item["role"], "content": item["content"]} for item in history[-10:]],
                    "latest_real_run": latest_run,
                    "recent_real_runs": recent_runs or [],
                    "message": message,
                }, ensure_ascii=False)},
            ], response_format={"type": "json_object"},
        )
        try:
            return ConversationReply.model_validate(data)
        except Exception as exc:
            raise ModelUnavailable("Conversation response did not match its action schema.") from exc

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
