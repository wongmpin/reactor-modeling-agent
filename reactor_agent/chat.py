"""Conversation application service. Chat is persisted separately from simulation specifications."""
from __future__ import annotations

import re
from typing import Any
from reactor_agent.agent.model import DeepSeekProvider
from reactor_agent.domain.models import ReactorModelSpec
from reactor_agent.service import ReactorAgentService

REACTOR_NAMES = {"conversion": "转化率反应器", "gibbs": "Gibbs 反应器", "equilibrium": "平衡反应器",
                 "cstr": "全混流反应器（CSTR）", "pfr": "平推流反应器（PFR）"}
CHEMICAL_NAMES = {
    "toluene": "甲苯", "benzene": "苯", "o-xylene": "邻二甲苯", "m-xylene": "间二甲苯",
    "p-xylene": "对二甲苯", "ortho-xylene": "邻二甲苯", "meta-xylene": "间二甲苯",
    "para-xylene": "对二甲苯", "methane": "甲烷", "water": "水", "hydrogen": "氢气",
    "carbon monoxide": "一氧化碳", "carbon dioxide": "二氧化碳", "ethylene": "乙烯",
    "ethane": "乙烷", "oxygen": "氧气", "nitrogen": "氮气",
    "vapour product": "气相产品", "vapor product": "气相产品", "liquid product": "液相产品",
    "feed": "进料",
}


def chemical_display(name: str) -> str:
    return CHEMICAL_NAMES.get(name.casefold(), name)
ASSUMPTION_NAMES = {"annual_methane_throughput": "甲烷年处理量", "methane_annual_throughput": "甲烷年处理量",
                    "annual_operating_hours": "年运行小时数", "feed_pressure": "进料压力",
                    "pressure_drop": "压降", "no_pressure_drop": "无压降", "thermodynamic_data_source": "热力学数据来源",
                    "no_solid_phase": "不考虑固相", "selectivity_distribution": "二甲苯异构体选择性",
                    "xylene_isomer_distribution": "二甲苯异构体选择性", "thermal_mode": "反应器热模式",
                    "outlet_temperature": "反应器出口温度", "phase": "反应相态",
                    "no_side_reactions": "副反应范围"}
ASSUMPTION_VALUES = {
    "specified_temperature": "指定出口温度（等温/指定温度模式）",
    "adiabatic": "绝热模式",
    "vapor": "气相反应",
    "vapour": "气相反应",
    "gas": "气相反应",
    "confirmed": "已确认",
    "unconfirmed": "待确认",
    "true": "是",
    "false": "否",
}


def assumption_display(assumption: dict[str, Any]) -> tuple[str, str]:
    key = assumption["key"]
    label = ASSUMPTION_NAMES.get(key)
    if label is None:
        # Internal protocol identifiers should not leak into conversational UI.
        label = "其他建模假设"
    value = str(assumption["value"])
    value = ASSUMPTION_VALUES.get(value.casefold(), value)
    return label, value


def diagnostic_display(message: str) -> str:
    """Translate deterministic and backend diagnostics at the UI boundary."""
    exact = {
        "Feed pressure is required.": "必须提供进料压力。",
        "Feed flow rate is required.": "必须提供进料流量。",
        "Specified-temperature operation requires target_outlet_temperature.": "指定出口温度模式需要提供目标出口温度。",
        "Specified-temperature operation requires target_outlet_temperature or an outlet temperature for every operating case.": "指定出口温度模式需要提供目标出口温度，或为每个工况提供出口温度。",
        "Specified-temperature operation requires target_outlet_temperature or an outlet temperature for every operating case.": "指定出口温度模式需要提供目标出口温度，或为每个工况提供出口温度。",
        "Specify target_pressure, including an explicit zero-pressure-drop assumption if appropriate.": "请提供目标压力；如适用，请明确确认压降为零。",
        "This adapter currently verifies only Peng-Robinson; confirm a suitable property method.": "当前适配器仅验证 Peng–Robinson 物性方法，请确认采用适用的物性方法。",
        "Only explicitly specified vapor-phase conversion reactions are currently verified.": "当前仅验证明确指定为气相的转化率反应。",
        "Each conversion reaction requires an explicit reactant basis_component.": "每个转化率反应都必须明确指定反应物基准组分。",
        "Only a single conversion reaction has been verified in this adapter.": "当前适配器仅验证单个转化率反应。",
        "Conversion reactor requires a specified conversion or confirmed assumption.": "转化率反应器需要明确转化率或已确认的转化率假设。",
        "This reactor adapter cannot increase pressure above the inlet pressure.": "当前反应器适配器不能将压力提高到高于进料压力。",
    }
    translated = message
    for source, target in exact.items():
        translated = translated.replace(source, target)
    translated = re.sub(r"^configure_basis:\s*HysysAdapterError:\s*", "配置物性基础：HYSYS 适配器错误：", translated)
    translated = re.sub(r"^configure_streams:\s*HysysAdapterError:\s*", "配置物流：HYSYS 适配器错误：", translated)
    translated = re.sub(r"^configure_reactor:\s*HysysAdapterError:\s*", "配置反应器：HYSYS 适配器错误：", translated)
    translated = re.sub(r"^solve:\s*HysysAdapterError:\s*", "求解：HYSYS 适配器错误：", translated)
    if translated != message:
        message = translated
    translated = re.sub(
        r"Component flow constraint ([^\n]+?) requires a positive feed composition entry\.",
        lambda match: f"组分流量约束 {match.group(1)} 要求进料组成中存在正值。", message)
    translated = re.sub(
        r"Assumption '([^']+)' requires user confirmation\.",
        lambda match: f"假设“{assumption_display({'key': match.group(1), 'value': '待确认'})[0]}”仍需用户确认。", translated)
    translated = re.sub(
        r"Missing ([^:]+): ([^\n]+)",
        lambda match: f"缺少{'进料流量' if match.group(1) == 'feed.flow' else '进料压力' if match.group(1) == 'feed.pressure' else match.group(1)}：{match.group(2)}", translated)
    message = translated
    replacements = {
        "target_outlet_temperature": "目标出口温度", "target_pressure": "目标压力",
        "thermal_mode": "热模式", "basis_component": "基准组分",
        "HYSYS solver has not confirmed convergence.": "HYSYS 尚未确认求解收敛。",
        "No stream results were read from HYSYS.": "未从 HYSYS 读取到物流结果。",
        "Expected one inlet and both vapor/liquid boundary outlets.": "HYSYS 结果应包含一个进料口以及气相、液相两个边界出口。",
    }
    translated = message
    for source, target in replacements.items():
        translated = translated.replace(source, target)
    return translated
INDEPENDENT_SCENARIO = re.compile(
    r"(?:新(?:的)?(?:测试)?(?:场景|任务)|另一个(?:测试)?场景).{0,30}(?:无关|独立|不要沿用|不继承|别带入|不带入)|"
    r"(?:无关|独立|不要沿用|不继承|别带入|不带入).{0,30}(?:新(?:的)?(?:测试)?(?:场景|任务)|之前的场景)", re.I)


def is_independent_scenario_message(text: str) -> bool:
    return bool(INDEPENDENT_SCENARIO.search(text))


class ChatApplication:
    def __init__(self, service: ReactorAgentService | None = None):
        self.service = service or ReactorAgentService()
        self.database = self.service.database

    def task_reply(self, task_id: str, prefix: str = "") -> str:
        spec = self.database.get_spec(task_id)
        readiness = self.service.execution_readiness(task_id)
        lines = [prefix] if prefix else []
        if spec.reactor_type:
            lines.append(f"当前建议采用 **{REACTOR_NAMES.get(spec.reactor_type, spec.reactor_type)}**。")
            if spec.reactor_rationale:
                lines.append(spec.reactor_rationale)
        if len(spec.operating_cases) > 1:
            cases = "、".join(case.name for case in spec.operating_cases)
            lines.append(f"已登记 {len(spec.operating_cases)} 个稳态工况（{cases}），提交后将在同一任务内串行运行并分别返回出口流股组成。")
        questions = list(dict.fromkeys(item["question"] for item in readiness["questions"]))
        if questions:
            lines.append("接下来请补充这些条件，可以一次回答多项：\n\n" +
                         "\n".join(f"{i}. {question}" for i, question in enumerate(questions, 1)))
        if readiness["unconfirmed_assumptions"]:
            lines.append("还有待确认的建模假设，已列在下方。你可以逐项勾选确认，也可以直接回复同意哪些条件或提供其他值。")
        if readiness["ready"]:
            lines.append(f"第 **{spec.version}** 版条件已通过提交前检查。点击 **开始真实模拟** 后会在 HYSYS 中运行；也可以继续提问或修改工况。")
        elif not questions and not readiness["unconfirmed_assumptions"]:
            # A deterministic validation error is not always a missing user input.
            lines.append("当前不能开始模拟，具体检查结果：")
            lines.extend(f"{i}. {diagnostic_display(error)}" for i, error in enumerate(readiness["errors"], 1))
            if any("provide product selectivity" in error for error in readiness["errors"]):
                confirmed_distribution = next((item for item in spec.assumptions
                    if item.confirmed_by_user and any(token in item.key.casefold()
                    for token in ("isomer", "distribution", "selectivity"))), None)
                if confirmed_distribution:
                    lines.append(f"你已确认“{confirmed_distribution.value}”；这是结构化校验字段未同步的问题，不需要你重复提供。请继续对话，我会据此更新规格并重新校验。")
                else:
                    lines.append("这项是确实缺少的产物异构体选择性，请提供各异构体比例；不会从计量式推算比例。")
            elif readiness["errors"]:
                lines.append("这些是规格或后端能力诊断，并非要求你重复提供已经确认的条件。")
            else:
                lines.append("请直接在对话中补充上述具体项目；不会自动改选反应器。")
        return "\n\n".join(lines)

    def ensure_restored_conversation(self, task_id: str) -> None:
        if not self.database.chat_messages(task_id):
            spec = self.database.get_spec(task_id)
            self.database.add_chat_message(task_id, "assistant", self.task_reply(task_id,
                f"已恢复这个任务的第 {spec.version} 版条件和已有运行记录。你可以继续补参、提问，或修改工况。"),
                {"kind": "restored", "spec_version": spec.version}, event_key="restored")
        self.sync_results(task_id)

    def send(self, task_id: str | None, text: str, on_token=None) -> str:
        text = text.strip()
        if not text:
            raise ValueError("请输入消息。")
        independent_task = False
        if task_id and is_independent_scenario_message(text):
            prior_messages = self.database.chat_messages(task_id)
            prior_request = next((message["content"] for message in reversed(prior_messages)
                                  if message["role"] == "user" and
                                  not is_independent_scenario_message(message["content"])), None)
            if prior_request:
                task_id = self.service.create_task(prior_request).task_id
                text = prior_request
                independent_task = True
                self.database.audit(task_id, "independent_task_created", "Created a clean task from the immediately preceding user scenario request.")
        if task_id is None:
            task_id = self.service.create_task(text).task_id
        history = self.database.chat_messages(task_id)
        self.database.add_chat_message(task_id, "user", text)
        try:
            spec = self.database.get_spec(task_id)
            runs = self.database.list_runs(task_id)
            provider = self.service.provider or DeepSeekProvider()
            decision = (None if independent_task else provider.conversation_reply(spec, text, history,
                                                   runs[0]["payload"] if runs else None,
                                                   recent_runs=[{"status": row["status"],
                                                                 "spec": self.database.get_spec(task_id, row["spec_version"]).model_dump(mode="json"),
                                                                 "result": row["payload"]}
                                                                for row in runs[:6]],
                                                   on_token=on_token))
            if decision is not None:
                self.database.audit(task_id, "conversation_action", decision.action)
            if decision is not None and decision.action == "answer":
                self.database.add_chat_message(task_id, "assistant", decision.reply, {"kind": "answer"})
            elif decision is not None and decision.action == "run":
                self._queue(task_id, decision.reply)
            else:
                if spec.version == 1 and spec.feed is None:
                    output = self.service.process_request(task_id, request=text)
                else:
                    output = self.service.resolve_input(task_id, text, [])
                if output.get("status") == "model_unavailable":
                    raise RuntimeError("模型暂时无法完成规格解析，请稍后重试。")
                spec = self.database.get_spec(task_id)
                prefix = "已根据你刚才描述的新场景创建独立任务，只使用该场景的信息；旧任务的规格和运行结果未带入。" if independent_task else decision.reply
                self.database.add_chat_message(task_id, "assistant", self.task_reply(task_id, prefix),
                    {"kind": "spec_update", "spec_version": spec.version})
                if decision is not None and decision.action == "update_and_run" and self.service.execution_readiness(task_id)["ready"]:
                    self._queue(task_id)
        except Exception as exc:
            self._record_error(task_id, exc)
        return task_id

    def confirm(self, task_id: str, version: int, keys: list[str]) -> None:
        spec = self.database.get_spec(task_id)
        if version != spec.version:
            raise ValueError("条件版本已更新，请查看最新假设后再确认。")
        if not keys:
            raise ValueError("请先选择要确认的假设。")
        assumptions = {item.key: item for item in spec.assumptions if not item.confirmed_by_user}
        if set(keys) - set(assumptions):
            raise ValueError("所选假设已更新，请重新查看。")
        text = "我确认以下假设：\n\n" + "\n".join(
            f"- {ASSUMPTION_NAMES.get(key, key)}：{assumptions[key].value}" for key in keys)
        self.database.add_chat_message(task_id, "user", text)
        try:
            output = self.service.resolve_input(task_id, "", keys)
            if output.get("status") == "model_unavailable":
                raise RuntimeError("模型暂时无法完成补参；已保存确认记录，请稍后继续。")
            new_spec = self.database.get_spec(task_id)
            confirmation_note = (
                f"已记录你的确认，并重新核对建模条件。此次确认生成第 **{new_spec.version}** 版条件快照，"
                "用于保留确认前后的可追溯记录；此前版本不会被覆盖。"
            )
            self.database.add_chat_message(task_id, "assistant", self.task_reply(task_id, confirmation_note),
                {"kind": "spec_update", "spec_version": new_spec.version})
        except Exception as exc:
            self._record_error(task_id, exc)

    def submit_run(self, task_id: str) -> None:
        spec = self.database.get_spec(task_id)
        self.database.add_chat_message(task_id, "user", f"请按第 {spec.version} 版已确认条件开始真实模拟。")
        self._queue(task_id)

    def _queue(self, task_id: str, prefix: str = "") -> None:
        readiness = self.service.execution_readiness(task_id)
        if not readiness["ready"]:
            self.database.add_chat_message(task_id, "assistant", self.task_reply(task_id, "还不能开始运行，需要先补齐或确认条件。"),
                                           {"kind": "needs_input"})
            return
        try:
            queue_id = self.service.queue_execution(task_id)
            spec = self.database.get_spec(task_id)
            case_note = ""
            if spec.operating_cases:
                case_note = f"，共 {len(spec.operating_cases)} 个工况，将在同一任务中串行运行"
            self.database.add_chat_message(task_id, "assistant",
                (prefix + "\n\n" if prefix else "") + f"已提交第 **{spec.version}** 版条件{case_note}，正在等待 HYSYS 运行。完成后结果会自动显示在这段对话中。",
                {"kind": "queued", "queue_id": queue_id, "spec_version": spec.version}, event_key="queued:" + queue_id)
        except Exception as exc:
            self._record_error(task_id, exc)

    def _record_error(self, task_id: str, exc: Exception) -> None:
        self.database.add_chat_message(task_id, "assistant",
            "这一步暂时没有完成，已保留当前条件和你的消息。请查看诊断后重试，或继续补充条件。",
            {"kind": "error", "diagnostic": str(exc)})
        self.database.audit(task_id, "chat_error", type(exc).__name__)

    def sync_results(self, task_id: str) -> None:
        existing = {message["event_key"] for message in self.database.chat_messages(task_id)}
        for row in reversed(self.database.list_runs(task_id)):
            if "run:" + row["run_id"] in existing:
                continue
            case_note = f"（工况：{row['payload'].get('case_name')}）" if row["payload"].get("case_name") else ""
            self.database.add_chat_message(task_id, "assistant",
                f"HYSYS 第 **{row['spec_version']}** 版运行记录已返回{case_note}（本任务历史记录）。",
                {"kind": "run_result", "run_id": row["run_id"], "spec_version": row["spec_version"]},
                event_key="run:" + row["run_id"])

    def export(self, task_id: str) -> dict[str, Any]:
        return {"spec": self.database.get_spec(task_id).model_dump(mode="json"),
                "messages": self.database.chat_messages(task_id), "runs": self.database.list_runs(task_id),
                "audit": self.database.audit_events(task_id)}
