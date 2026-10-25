"""Conversational Streamlit client; modeling operations go through application services."""
from __future__ import annotations

import json
import os
from pathlib import Path

import streamlit as st
from dotenv import load_dotenv
from reactor_agent.chat import ASSUMPTION_NAMES, ChatApplication

load_dotenv()
st.set_page_config(page_title="化工反应器建模 Agent", page_icon="⚗️", layout="wide")
st.title("化工反应器建模 Agent")
st.caption("用对话描述反应、补充条件、确认假设并查看真实 HYSYS 结果")
application = ChatApplication()
service = application.service
tasks = service.database.list_tasks()
task_ids = {task["task_id"] for task in tasks}
NEW = "__new__"

if "pending_task_id" in st.session_state:
    st.session_state["selected_task"] = st.session_state.pop("pending_task_id")
if "selected_task" not in st.session_state:
    requested = st.query_params.get("task")
    st.session_state["selected_task"] = requested if requested in task_ids else (tasks[0]["task_id"] if tasks else NEW)
if st.session_state["selected_task"] not in task_ids | {NEW}:
    st.session_state["selected_task"] = NEW


def new_conversation():
    st.session_state["selected_task"] = NEW
    st.query_params.pop("task", None)


with st.sidebar:
    st.button("＋ 新建对话", key="new_chat", on_click=new_conversation, width="stretch")
    labels = {item["task_id"]: f"{item['user_request'][:36] or '建模对话'} · v{item['current_version']}" for item in tasks}
    selected_id = st.selectbox("历史对话", [NEW, *labels], key="selected_task",
                               format_func=lambda key: "新对话" if key == NEW else labels[key])
    st.caption("每段新对话独立保存条件；历史对话可继续补参和修改。")
    with st.expander("本地运行设置"):
        st.write("模型：", os.getenv("DEEPSEEK_MODEL", "deepseek-flash"))
        st.write("API Key：", "已配置" if os.getenv("DEEPSEEK_API_KEY") else "未配置")
        if st.button("探测 HYSYS V15", key="probe_hysys"):
            try:
                with st.spinner("正在查询后端能力…"):
                    st.json(service.backend_capabilities())
            except Exception as exc:
                st.error(str(exc))

task_id = None if selected_id == NEW else selected_id
if task_id:
    st.query_params["task"] = task_id
    application.ensure_restored_conversation(task_id)
else:
    st.query_params.pop("task", None)


def render_run(row):
    result = row["payload"]
    flags = {"案例已保存": result.get("case_created", False),
             "求解已收敛": result.get("solver_converged", False),
             "工程校验通过": result.get("engineering_validation_passed", False)}
    st.write(" · ".join(f"{'✅' if value else '❌'} {label}" for label, value in flags.items()))
    if all(flags.values()):
        st.success(f"第 {row['spec_version']} 版真实模拟通过工程校验。")
    else:
        st.error("本次运行未通过全部检查，请查看诊断。")
    st.caption(f"HYSYS {result.get('hysys_version') or '版本未读取'} · 运行 {row['run_id'][:8]}")
    for diagnostic in result.get("diagnostics", []):
        st.warning(diagnostic)
    streams = result.get("streams", [])
    if streams:
        st.dataframe([{"物流": stream["name"], "温度 °C": stream["temperature_k"] - 273.15 if stream.get("temperature_k") is not None else None,
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
            st.markdown(message["content"])
            metadata = message["metadata"]
            if metadata.get("kind") == "run_result" and metadata.get("run_id") in runs:
                render_run(runs[metadata["run_id"]])
            if metadata.get("diagnostic"):
                with st.expander("本步诊断"):
                    st.write(metadata["diagnostic"])

    if task_id:
        spec = service.database.get_spec(task_id)
        readiness = service.execution_readiness(task_id)
        pending = service.database.active_queue_items(task_id)
        if polling and not pending:
            # Stop timed rerenders once the submitted job completes.
            st.rerun()
        if pending:
            labels = [f"第 {json.loads(item['payload'])['spec_version']} 版：{'正在模拟' if item['status'] == 'running' else '等待 Worker'}" for item in pending]
            st.info("；".join(labels) + "。完成后会自动显示结果，你仍可继续对话。")
        assumptions = readiness["unconfirmed_assumptions"]
        if assumptions:
            with st.chat_message("assistant"):
                st.markdown("**请确认建模假设**\n\n仅勾选你同意的项目，然后点击确认；也可以在下方直接告诉我修改值。")
                keys = []
                for item in assumptions:
                    if st.checkbox(f"{ASSUMPTION_NAMES.get(item['key'], item['key'])}：{item['value']}", value=False,
                                   key=f"confirm_{task_id}_v{spec.version}_{item['key']}",
                                   help=item["rationale"] + "\n" + (item.get("impact") or "")):
                        keys.append(item["key"])
                if st.button("确认所选假设", key="confirm_inputs", disabled=not keys):
                    with st.status("正在记录确认并核对条件…", expanded=True) as status:
                        application.confirm(task_id, spec.version, keys)
                        status.update(label="已处理本轮确认", state="complete")
                    st.rerun()
        active_version = any(json.loads(item["payload"])["spec_version"] == spec.version for item in pending)
        if st.button("开始真实模拟", key="submit_hysys", type="primary",
                     disabled=not readiness["ready"] or active_version):
            with st.spinner("正在检查后端能力并提交…"):
                application.submit_run(task_id)
            st.rerun()
        if not readiness["ready"]:
            st.caption("条件尚未齐备，请在对话中补充或确认；不会自动运行。")
        with st.expander(f"当前条件与诊断 · 第 {spec.version} 版"):
            st.json(spec.model_dump(mode="json"))
            for error in readiness["errors"]:
                st.write(error)
        if len(runs) > 1:
            with st.expander("不同版本的出口结果对比"):
                comparison = []
                for row in reversed(list(runs.values())):
                    result = row["payload"]
                    if result.get("engineering_validation_passed"):
                        for stream in result.get("streams", []):
                            if stream["name"] == "Vapour Product":
                                comparison.append({"版本": row["spec_version"], "运行": row["run_id"][:8],
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
                active_id = application.send(task_id, prompt)
                status.update(label="已处理本轮消息", state="complete")
        st.session_state["pending_task_id"] = active_id
        st.rerun()


conversation()
