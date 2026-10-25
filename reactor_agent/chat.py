"""Conversation application service. Chat is persisted separately from simulation specifications."""
from __future__ import annotations

from typing import Any
from reactor_agent.agent.model import DeepSeekProvider
from reactor_agent.domain.models import ReactorModelSpec
from reactor_agent.service import ReactorAgentService

REACTOR_NAMES = {"conversion": "转化率反应器", "gibbs": "Gibbs 反应器", "equilibrium": "平衡反应器",
                 "cstr": "全混流反应器（CSTR）", "pfr": "平推流反应器（PFR）"}
ASSUMPTION_NAMES = {"annual_methane_throughput": "甲烷年处理量", "methane_annual_throughput": "甲烷年处理量",
                    "annual_operating_hours": "年运行小时数", "feed_pressure": "进料压力",
                    "pressure_drop": "压降", "no_pressure_drop": "无压降", "thermodynamic_data_source": "热力学数据来源",
                    "no_solid_phase": "不考虑固相"}


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
        questions = list(dict.fromkeys(item["question"] for item in readiness["questions"]))
        if questions:
            lines.append("接下来请补充这些条件，可以一次回答多项：\n\n" +
                         "\n".join(f"{i}. {question}" for i, question in enumerate(questions, 1)))
        if readiness["unconfirmed_assumptions"]:
            lines.append("还有待确认的建模假设，已列在下方。你可以逐项勾选确认，也可以直接回复同意哪些条件或提供其他值。")
        if readiness["ready"]:
            lines.append(f"第 **{spec.version}** 版条件已通过提交前检查。点击 **开始真实模拟** 后会在 HYSYS 中运行；也可以继续提问或修改工况。")
        elif not questions and not readiness["unconfirmed_assumptions"]:
            # Deterministic validators supply additional questions even if the parser missed them.
            if spec.feed is None:
                lines.append("请描述反应物、产物，以及进料组成、流量、温度和压力。")
            elif spec.feed.flow is None and not spec.feed.component_mass_flows_kg_h:
                lines.append("还需要进料流量；也可以给出某组分的年处理量和年运行小时数。")
            elif spec.feed.pressure is None:
                lines.append("进料压力是多少？如果采用无压降假设，请明确确认。")
            else:
                lines.append("还有条件需要核对，详情见“当前条件与诊断”。请补充条件，我会继续检查；不会自动改选反应器。")
        return "\n\n".join(lines)

    def ensure_restored_conversation(self, task_id: str) -> None:
        if not self.database.chat_messages(task_id):
            spec = self.database.get_spec(task_id)
            self.database.add_chat_message(task_id, "assistant", self.task_reply(task_id,
                f"已恢复这个任务的第 {spec.version} 版条件和已有运行记录。你可以继续补参、提问，或修改工况。"),
                {"kind": "restored", "spec_version": spec.version}, event_key="restored")
        self.sync_results(task_id)

    def send(self, task_id: str | None, text: str) -> str:
        text = text.strip()
        if not text:
            raise ValueError("请输入消息。")
        if task_id is None:
            task_id = self.service.create_task(text).task_id
        history = self.database.chat_messages(task_id)
        self.database.add_chat_message(task_id, "user", text)
        try:
            spec = self.database.get_spec(task_id)
            runs = self.database.list_runs(task_id)
            provider = self.service.provider or DeepSeekProvider()
            decision = provider.conversation_reply(spec, text, history,
                                                   runs[0]["payload"] if runs else None,
                                                   recent_runs=[{"status": row["status"], "result": row["payload"]}
                                                                for row in runs[:6]])
            self.database.audit(task_id, "conversation_action", decision.action)
            if decision.action == "answer":
                self.database.add_chat_message(task_id, "assistant", decision.reply, {"kind": "answer"})
            elif decision.action == "run":
                self._queue(task_id, decision.reply)
            else:
                if spec.version == 1 and spec.feed is None:
                    output = self.service.process_request(task_id, request=text)
                else:
                    output = self.service.resolve_input(task_id, text, [])
                if output.get("status") == "model_unavailable":
                    raise RuntimeError("模型暂时无法完成规格解析，请稍后重试。")
                spec = self.database.get_spec(task_id)
                self.database.add_chat_message(task_id, "assistant", self.task_reply(task_id, decision.reply),
                    {"kind": "spec_update", "spec_version": spec.version})
                if decision.action == "update_and_run" and self.service.execution_readiness(task_id)["ready"]:
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
            self.database.add_chat_message(task_id, "assistant", self.task_reply(task_id, "已记录你的确认，并重新核对建模条件。"),
                {"kind": "spec_update", "spec_version": self.database.get_spec(task_id).version})
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
            self.database.add_chat_message(task_id, "assistant",
                (prefix + "\n\n" if prefix else "") + f"已提交第 **{spec.version}** 版条件，正在等待 HYSYS 运行。完成后结果会自动显示在这段对话中。",
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
            self.database.add_chat_message(task_id, "assistant", f"HYSYS 第 **{row['spec_version']}** 版运行记录已返回。",
                {"kind": "run_result", "run_id": row["run_id"], "spec_version": row["spec_version"]},
                event_key="run:" + row["run_id"])

    def export(self, task_id: str) -> dict[str, Any]:
        return {"spec": self.database.get_spec(task_id).model_dump(mode="json"),
                "messages": self.database.chat_messages(task_id), "runs": self.database.list_runs(task_id),
                "audit": self.database.audit_events(task_id)}
