from __future__ import annotations

import json
import os

import streamlit as st
from dotenv import load_dotenv

from reactor_agent.domain.models import ReactorModelSpec
from reactor_agent.service import ReactorAgentService

load_dotenv()
st.set_page_config(page_title="化工反应器建模 Agent", layout="wide")
st.title("化工反应器建模 Agent")
st.caption("本地工作台 · Aspen HYSYS V15")

service = ReactorAgentService()
left, right = st.columns([3, 2])

with left:
    st.subheader("新建建模任务")
    request = st.text_area("反应需求", height=140, placeholder="描述反应、进料、工况和已知工艺参数")
    input_mode = st.radio("输入方式", ["自然语言解析", "结构化规格"], horizontal=True)
    spec_json = ""
    if input_mode == "结构化规格":
        spec_template = ReactorModelSpec(user_request=request).model_dump_json(indent=2)
        spec_json = st.text_area("ReactorModelSpec JSON", value=spec_template, height=300, key="spec_json")
    if st.button("解析并校验", type="primary", use_container_width=True):
        try:
            spec = ReactorModelSpec.model_validate_json(spec_json) if spec_json else ReactorModelSpec(user_request=request)
            task = service.create_task(request, spec)
            with st.spinner("模型正在解析需求并判断反应器…"):
                output = service.process_request(task.task_id, request=request, spec_json=spec_json)
            st.session_state["last_output"] = output
            st.session_state["last_task_id"] = task.task_id
        except Exception as exc:
            st.error(str(exc))

    output = st.session_state.get("last_output")
    if output:
        st.subheader("工作流状态")
        st.write(output.get("status", "unknown"))
        for message in output.get("diagnostics", []):
            st.warning(message)
        if output.get("spec"):
            for missing in output["spec"].get("missing_information", []):
                st.warning(missing["question"])
            for assumption in output["spec"].get("assumptions", []):
                st.write(f"假设：{assumption['key']} = {assumption['value']} · {'已确认' if assumption['confirmed_by_user'] else '待确认'}")
            st.json(output["spec"])
            if output.get("status") == "spec_validated":
                st.success("规格通过前置校验。提交前还会检查后端能力与运行边界。")

with right:
    st.subheader("运行能力")
    st.write("DeepSeek 模型", os.getenv("DEEPSEEK_MODEL", "deepseek-flash"))
    st.write("API Key", "已配置" if os.getenv("DEEPSEEK_API_KEY") else "未配置")
    if st.button("探测 HYSYS V15", use_container_width=True):
        try:
            st.json(service.backend_capabilities())
        except Exception as exc:
            st.error(str(exc))

    tasks = service.database.list_tasks()[:10]
    st.subheader("继续任务")
    if tasks:
        selected_id = st.selectbox(
            "选择已有任务",
            options=[task["task_id"] for task in tasks],
            format_func=lambda task_id: next(
                f"{item['status']} · {item['user_request'][:32] or task_id[:8]}"
                for item in tasks if item["task_id"] == task_id
            ),
        )
        current_spec = service.database.get_spec(selected_id)
        resume_json = st.text_area(
            "当前任务规格（修改后生成新版本）",
            value=current_spec.model_dump_json(indent=2),
            height=260,
            key=f"resume_spec_{selected_id}_v{current_spec.version}",
        )
        clarification = st.text_input("补充条件或修改说明", key=f"resume_text_{selected_id}")
        if st.button("保存新版本并重新校验", key="resume_task", use_container_width=True):
            try:
                output = service.process_request(selected_id, request=clarification, spec_json=resume_json)
                st.session_state["last_output"] = output
                st.session_state["last_task_id"] = selected_id
                st.rerun()
            except Exception as exc:
                st.error(str(exc))

    if tasks:
        if st.button("提交当前任务到真实 HYSYS 队列", use_container_width=True):
            try:
                queue_id = service.queue_execution(selected_id)
                st.success(f"已入队 {queue_id}，后台 Worker 将串行执行。点击刷新查看结果。")
            except Exception as exc:
                st.error(str(exc))
        if st.button("刷新运行状态", use_container_width=True):
            st.rerun()
        st.subheader("真实运行记录")
        from pathlib import Path
        for row in service.database.list_runs(selected_id):
            result = row["payload"]
            with st.expander(f"{row['status']} · v{row['spec_version']} · {row['run_id'][:8]}", expanded=True):
                st.write({"案例已保存": result["case_created"], "求解已收敛": result["solver_converged"],
                          "工程校验通过": result["engineering_validation_passed"]})
                st.write("HYSYS", result.get("hysys_version"))
                for message in result.get("diagnostics", []):
                    st.warning(message)
                if result.get("streams"):
                    st.dataframe(result["streams"], use_container_width=True)
                st.download_button("下载运行记录 JSON", json.dumps(result, ensure_ascii=False, indent=2),
                                   file_name=f"{row['run_id']}.json", key="run-" + row["run_id"])
                if result.get("case_path"):
                    case_path = Path(result["case_path"]).resolve()
                    if case_path.is_relative_to(service.data_dir / "cases") and case_path.is_file():
                        st.download_button("下载 HYSYS 案例", case_path.read_bytes(),
                                           file_name=case_path.name, key="case-" + row["run_id"])
        export = {"spec": service.database.get_spec(selected_id).model_dump(mode="json"),
                  "runs": service.database.list_runs(selected_id), "audit": service.database.audit_events(selected_id)}
        st.download_button("导出任务规格、运行和审计", json.dumps(export, ensure_ascii=False, indent=2),
                           file_name=f"task-{selected_id}.json")

    st.subheader("近期任务")
    for task in tasks:
        with st.expander(f"{task['status']} · {task['task_id'][:8]}"):
            st.write(task["user_request"] or "（无自然语言描述）")
            st.write(f"规格版本 {task['current_version']}")
            st.json(service.database.audit_events(task["task_id"]))
