"""Conversational Streamlit client; modeling operations go through application services."""
from __future__ import annotations

import json
import os
from pathlib import Path

import streamlit as st
from dotenv import load_dotenv
from reactor_agent.chat import REACTOR_NAMES, assumption_display, chemical_display, diagnostic_display, ChatApplication

load_dotenv()
st.set_page_config(page_title="化工反应器建模 Agent", page_icon="⚗️", layout="wide")
st.title("化工反应器建模 Agent")
st.caption("用对话描述反应、补充条件、确认假设并查看真实 HYSYS 结果")
application = ChatApplication()
service = application.service
tasks = service.database.list_tasks()
task_ids = {task["task_id"] for task in tasks}
NEW = "__new__"
STATUS_LABELS = {
    "needs_input": "待补充", "spec_validated": "待运行", "ready": "待运行",
    "queued": "排队中", "running": "运行中", "validated": "已完成",
    "solved": "已求解", "validation_failed": "校验失败", "failed": "运行失败",
}

pending_task_id = st.session_state.pop("pending_task_id", None)
if pending_task_id:
    # A prompt can create a task immediately before rerun. Refresh the list
    # before validating the pending id, otherwise it can fall back to NEW.
    if pending_task_id not in task_ids:
        tasks = service.database.list_tasks()
        task_ids = {task["task_id"] for task in tasks}
    st.session_state["selected_task"] = pending_task_id
requested_task_id = st.query_params.get("task")
if "selected_task" not in st.session_state:
    st.session_state["selected_task"] = (requested_task_id if requested_task_id in task_ids
                                          else (tasks[0]["task_id"] if tasks else NEW))
if st.session_state["selected_task"] not in task_ids | {NEW}:
    st.session_state["selected_task"] = requested_task_id if requested_task_id in task_ids else NEW
elif st.session_state["selected_task"] == NEW and requested_task_id in task_ids:
    # Restore a task after a fragment/app rerun if the URL still identifies it.
    # Clicking "新建对话" removes the query parameter, so it remains explicit.
    st.session_state["selected_task"] = requested_task_id


def new_conversation():
    st.session_state["selected_task"] = NEW
    st.query_params.pop("task", None)


with st.sidebar:
    st.button("＋ 新建对话", key="new_chat", on_click=new_conversation, width="stretch")
    st.markdown("**历史对话**")
    history_query = st.text_input("筛选历史对话", key="history_filter",
                                  placeholder="按标题或任务 ID 筛选", label_visibility="collapsed")
    visible_tasks = [item for item in tasks if not history_query or
                     history_query.casefold() in (item["user_request"] or "").casefold() or
                     history_query.casefold() in item["task_id"].casefold()]
    st.caption(f"显示 {len(visible_tasks)} / {len(tasks)} 个任务 · 条件和运行记录独立保存")
    with st.container(height=400, border=False):
        if not visible_tasks:
            st.caption("暂无历史任务")
        for item in visible_tasks:
            task_key = item["task_id"]
            title = " ".join((item["user_request"] or "建模对话").split())
            title = title[:24] + ("…" if len(title) > 24 else "")
            state = STATUS_LABELS.get(item["status"], item["status"])
            label = f"{'● ' if task_key == st.session_state.get('selected_task') else ''}{title}\n{state} · v{item['current_version']} · {task_key[:8]}"
            open_col, delete_col = st.columns([0.86, 0.14], gap="small")
            if open_col.button(label, key=f"open_task_{task_key}", width="stretch",
                               type="primary" if task_key == st.session_state.get("selected_task") else "secondary"):
                st.session_state["selected_task"] = task_key
                st.query_params["task"] = task_key
                st.rerun()
            if delete_col.button("", key=f"delete_task_{task_key}", icon=":material/delete:",
                                 help="删除这段历史对话及其运行记录"):
                st.session_state["delete_task_id"] = task_key
                st.rerun()
    delete_id = st.session_state.get("delete_task_id")
    if delete_id:
        target = next((item for item in tasks if item["task_id"] == delete_id), None)
        if target:
            st.warning(f"确定删除“{(target['user_request'] or '建模对话')[:30]}”吗？规格、消息、运行记录和审计记录都会删除。已保存的 HYSYS 案例文件不受影响。")
            confirm_col, cancel_col = st.columns(2)
            if confirm_col.button("确认删除", key="confirm_delete_task", type="primary", width="stretch"):
                try:
                    service.delete_task(delete_id)
                    st.session_state.pop("delete_task_id", None)
                    if st.session_state.get("selected_task") == delete_id:
                        st.session_state["selected_task"] = NEW
                        st.query_params.pop("task", None)
                    st.rerun()
                except Exception as exc:
                    st.error(str(exc))
            if cancel_col.button("取消", key="cancel_delete_task", width="stretch"):
                st.session_state.pop("delete_task_id", None)
                st.rerun()
    st.caption("每段对话独立保存；删除只影响历史记录，不会删除已下载的案例文件。")
    with st.expander("本地运行设置"):
        st.write("模型：", os.getenv("DEEPSEEK_MODEL", "deepseek-flash"))
        st.write("API Key：", "已配置" if os.getenv("DEEPSEEK_API_KEY") else "未配置")
        if st.button("探测 HYSYS V15", key="probe_hysys"):
            try:
                with st.spinner("正在查询后端能力…"):
                    st.json(service.backend_capabilities())
            except Exception as exc:
                st.error(str(exc))

selected_id = st.session_state.get("selected_task", NEW)
task_id = None if selected_id == NEW else selected_id
if task_id:
    st.query_params["task"] = task_id
    application.ensure_restored_conversation(task_id)
else:
    st.query_params.pop("task", None)


def render_run(row, current_version: int | None = None):
    result = row["payload"]
    flags = {"案例已保存": result.get("case_created", False),
             "求解已收敛": result.get("solver_converged", False),
             "工程校验通过": result.get("engineering_validation_passed", False)}
    st.write(" · ".join(f"{'✅' if value else '❌'} {label}" for label, value in flags.items()))
    if all(flags.values()):
        st.success(f"第 {row['spec_version']} 版真实模拟通过工程校验。")
    else:
        st.error("本次运行未通过全部检查，请查看诊断。")
    version_context = f" · 当前条件第 {current_version} 版" if current_version is not None else ""
    case_context = f" · 工况 {result.get('case_name')}" if result.get('case_name') else ""
    st.caption(
        f"本任务 {row['task_id'][:8]} · 第 {row['spec_version']} 版历史运行记录{case_context}{version_context} · "
        f"HYSYS {result.get('hysys_version') or '版本未读取'} · 运行 {row['run_id'][:8]}"
    )
    for diagnostic in result.get("diagnostics", []):
        st.warning(diagnostic_display(diagnostic))
    streams = result.get("streams", [])
    if streams:
        st.dataframe([{"物流": chemical_display(stream["name"]), "温度 °C": stream["temperature_k"] - 273.15 if stream.get("temperature_k") is not None else None,
                       "压力 bar": stream["pressure_kpa"] / 100 if stream.get("pressure_kpa") is not None else None,
                       "流量 kmol/h": stream["molar_flow_kmol_h"],
                       **stream["mole_fractions"]} for stream in streams], width="stretch")
        st.caption("组成为湿基摩尔分数；所有数值来自该运行记录。")
    with st.expander("运行详情与文件"):
        st.json(result)
        st.download_button("下载运行记录 JSON", json.dumps(result, ensure_ascii=False, indent=2),
                           file_name=f"{row['run_id']}.json", key="run-" + row["run_id"])
        if result.get("case_path"):
            path = Path(result["case_path"]).resolve()
            if path.is_relative_to(service.data_dir / "cases") and path.is_file():
                st.download_button("下载 HYSYS 案例", path.read_bytes(), file_name=path.name,
                                   key="case-" + row["run_id"])


polling = bool(task_id and service.database.active_queue_items(task_id))


@st.fragment(run_every="2s" if polling else None)
def conversation():
    runs = {}
    if task_id:
        application.sync_results(task_id)
        runs = {row["run_id"]: row for row in service.database.list_runs(task_id)}
        messages = service.database.chat_messages(task_id)
    else:
        messages = [{"role": "assistant", "content": "你好！请告诉我你想模拟什么反应，以及已知的进料和工况。我会解释选型依据、询问缺少的条件，再由你确认是否运行。\n\n例如：甲烷与水蒸气重整，想比较不同出口温度的组成。", "metadata": {}}]
    for message in messages:
        with st.chat_message(message["role"]):
            metadata = message["metadata"]
            if metadata.get("kind") == "run_result" and metadata.get("run_id") in runs:
                run_payload = runs[metadata["run_id"]]["payload"]
                case_note = f"（工况：{run_payload['case_name']}）" if run_payload.get("case_name") else ""
                st.markdown(
                    f"HYSYS 第 **{metadata.get('spec_version', runs[metadata['run_id']]['spec_version'])}** "
                    f"版运行记录已返回{case_note}（本任务历史记录）。"
                )
                render_run(runs[metadata["run_id"]], current_version=service.database.get_spec(task_id).version)
            else:
                content = diagnostic_display(message["content"]) if message["role"] == "assistant" else message["content"]
                st.markdown(content)
            if metadata.get("diagnostic"):
                with st.expander("本步诊断"):
                    st.write(metadata["diagnostic"])

    if task_id:
        spec = service.database.get_spec(task_id)
        readiness = service.execution_readiness(task_id)
        pending = service.database.active_queue_items(task_id)
        if polling and not pending:
            # Recompute the fragment polling interval after the queue drains.
            st.rerun(scope="app")
        if pending:
            labels = []
            for item in pending:
                payload = json.loads(item["payload"])
                case_name = f" · {payload['case_name']}" if payload.get("case_name") else ""
                labels.append(f"第 {payload['spec_version']} 版{case_name}：{'正在模拟' if item['status'] == 'running' else '等待 Worker'}")
            st.info("；".join(labels) + "。完成后会自动显示结果，你仍可继续对话。")
        assumptions = readiness["unconfirmed_assumptions"]
        if assumptions:
            with st.chat_message("assistant"):
                st.markdown("**请确认建模假设**\n\n仅勾选你同意的项目，然后点击确认；也可以在下方直接告诉我修改值。")
                keys = []
                for item in assumptions:
                    label, value = assumption_display(item)
                    if st.checkbox(f"{label}：{value}", value=False,
                                   key=f"confirm_{task_id}_v{spec.version}_{item['key']}",
                                   help=f"依据：{item['rationale']}\n影响：{item.get('impact') or '无其他影响说明'}"):
                        keys.append(item["key"])
                if st.button("确认所选假设", key="confirm_inputs", disabled=not keys):
                    with st.status("正在记录确认并核对条件…", expanded=True) as status:
                        application.confirm(task_id, spec.version, keys)
                        status.update(label="已处理本轮确认", state="complete")
                    st.rerun()
        active_version = any(json.loads(item["payload"])["spec_version"] == spec.version for item in pending)
        if st.button("开始真实模拟", key="submit_hysys", type="primary",
                     disabled=not readiness["ready"] or active_version):
            st.query_params["task"] = task_id
            with st.spinner("正在检查后端能力并提交…"):
                application.submit_run(task_id)
            st.rerun(scope="app")
        if not readiness["ready"]:
            st.caption("条件尚未齐备，请在对话中补充或确认；不会自动运行。")
        with st.expander(f"当前条件与诊断 · 第 {spec.version} 版"):
            st.markdown(f"**反应器**：{REACTOR_NAMES.get(spec.reactor_type.value, spec.reactor_type.value) if spec.reactor_type else '尚未选择'}")
            if spec.reactor_rationale:
                st.write(spec.reactor_rationale)
            if spec.feed:
                st.markdown("**进料**")
                st.write(f"{chemical_display(spec.feed.name)}；温度 {spec.feed.temperature} {spec.feed.temperature_unit}；"
                         f"压力 {spec.feed.pressure} {spec.feed.pressure_unit}；"
                         f"流量 {spec.feed.flow} {spec.feed.flow_unit}")
                st.write("组分：" + "、".join(f"{chemical_display(c.name)}（{c.formula}）：{c.value:g}" for c in spec.feed.components))
            if spec.reactions:
                st.markdown("**反应**")
                for reaction in spec.reactions:
                    left = " + ".join(f"{t.coefficient:g} {chemical_display(t.component)}" for t in reaction.reactants)
                    right = " + ".join(f"{t.coefficient:g} {chemical_display(t.component)}" for t in reaction.products)
                    reaction_name = "甲苯歧化" if (reaction.name or "").casefold() == "toluene disproportionation" else reaction.name or "反应"
                    st.write(f"{reaction_name}：{left} → {right}")
            if spec.operating_cases:
                st.markdown("**稳态工况**")
                for case in spec.operating_cases:
                    boundaries = []
                    if case.feed_temperature is not None:
                        boundaries.append(f"进料温度 {case.feed_temperature:g} {case.feed_temperature_unit}")
                    if case.feed_pressure is not None:
                        boundaries.append(f"进料压力 {case.feed_pressure:g} {case.feed_pressure_unit}")
                    if case.outlet_temperature is not None:
                        boundaries.append(f"出口温度 {case.outlet_temperature:g} {case.outlet_temperature_unit}")
                    if case.pressure is not None:
                        boundaries.append(f"出口压力 {case.pressure:g} {case.pressure_unit}")
                    st.write(f"{case.name}：" + ("；".join(boundaries) if boundaries else "未提供边界条件"))
            st.markdown("**建模假设**")
            for assumption in spec.assumptions:
                label, value = assumption_display(assumption.model_dump())
                st.write(f"{label}：{value}（{'已确认' if assumption.confirmed_by_user else '待确认'}）")
            if spec.thermo.property_package:
                st.write(f"物性方法：{spec.thermo.property_package}")
            if spec.missing_information:
                st.markdown("**待补充信息**")
                for item in spec.missing_information:
                    st.write(item.question)
            dynamic_questions = readiness["questions"]
            if dynamic_questions:
                st.markdown("**本次提交前仍需确认**")
                for item in dynamic_questions:
                    st.write(item["question"])
            for error in readiness["errors"]:
                st.write(diagnostic_display(error))
        if len(runs) > 1:
            with st.expander("不同版本的出口结果对比"):
                comparison = []
                for row in reversed(list(runs.values())):
                    result = row["payload"]
                    if result.get("engineering_validation_passed"):
                        for stream in result.get("streams", []):
                            if stream["name"] == "Vapour Product":
                                comparison.append({"版本": row["spec_version"], "工况": result.get("case_name") or "默认",
                                                   "运行": row["run_id"][:8],
                                                   "温度 °C": stream["temperature_k"] - 273.15,
                                                   "流量 kmol/h": stream["molar_flow_kmol_h"], **stream["mole_fractions"]})
                st.caption("仅比较工程校验通过的记录。修改过的其他条件请核对各版规格。")
                st.dataframe(comparison, width="stretch")
        with st.expander("对话、规格和审计导出"):
            st.caption(f"任务 {task_id} · 刷新浏览器或重启后可从历史对话恢复。")
            st.download_button("导出完整任务", json.dumps(application.export(task_id), ensure_ascii=False, indent=2),
                               file_name=f"task-{task_id}.json", key="export_task")

    if prompt := st.chat_input("描述需求、回答问题，或说“将出口温度改为600°C”", key="chat_prompt"):
        with st.chat_message("user"):
            st.markdown(prompt)
        with st.chat_message("assistant"):
            with st.status("正在理解本轮消息并核对条件…", expanded=True) as status:
                streamed = st.empty()
                streamed_parts = []
                def render_stream_chunk(chunk: str) -> None:
                    streamed_parts.append(chunk)
                    streamed.markdown("".join(streamed_parts))
                active_id = application.send(task_id, prompt, on_token=render_stream_chunk)
                status.update(label="已处理本轮消息", state="complete")
        st.session_state["pending_task_id"] = active_id
        st.rerun()


conversation()
