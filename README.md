# 化工反应器建模 Agent

面向装有 Aspen HYSYS V15 的 Windows 工作站。Streamlit → 应用服务 → DeepSeek/LangGraph → Harness → SQLite 队列 → 独立 STA Worker/受限 COM Adapter → HYSYS → 独立工程校验。

## 已实现与实测

2026-10-25 已完成真实 HYSYS 建模验证及多轮聊天界面，测试记录详见 `docs/validation-summary.md`。已打通真实链路，当前支持 Peng-Robinson、气相单反应 Conversion 和指定物种 Gibbs；**反应器由模型选型，Harness 只诊断，不按场景名称映射或替换**。

- DeepSeek `deepseek-flash`：真实 API、JSON、工具参数及工具结果回传已验证。官方当前文档对应 V4.1 Flash 1017；密钥由环境提供。
- 新建 HYSYS 案例并配置物流、反应/反应器、求解、读回流量/组成/温压/热负荷、保存并重开；每次为独立 `.hsc`。
- 必测两个温度工况、甲苯歧化及额外乙烯加氢均有真实执行记录，见 `docs/evidence/` 和 `docs/hysys-capabilities.md`。其中异构体比例、年运行小时和热边界是明确的**集成测试条件，现场需用户确认**。
- 自然语言解析、模型选型、缺参 interrupt/resume、规格版本、SQLite checkpoint/队列/审计、有限重试、独立结果校验、MCP Schema 工具和 Streamlit 任务/运行/案例下载。
- `case_created`、`solver_converged`、`engineering_validation_passed` 分开记录，失败不返回预设结果。

未验证：Equilibrium/CSTR/PFR、多条 Conversion 的顺序/竞争反应、固相/电解质及动态工况；这些能力返回诊断，不宣称支持。Gibbs 为所声明物种和数据库下的平衡估算，不代替实际反应炉动力学/结焦/传热分析。

## 安装与配置

需要 Windows、Python 3.11/3.12、HYSYS V15（有效许可与 COM 注册）。从项目目录运行：

```powershell
.\scripts\setup.ps1
notepad .env
```

`.env` 中配置（已有配置不会被 setup 覆盖）：

```dotenv
DEEPSEEK_API_KEY=你的密钥
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_MODEL=deepseek-flash
```

不要提交 `.env` 或在录屏中显示密钥。其余目录、COM ProgID、超时和端口见 `.env.example`。已验证依赖快照见 `docs/validated-environment.txt`；项目采用 `pyproject.toml` 安装。

## 启动、使用和关闭

```powershell
.\.venv\Scripts\python.exe -m reactor_agent.cli model-check
.\.venv\Scripts\python.exe -m reactor_agent.cli hysys-check
.\scripts\start.ps1
```

启动脚本一次启动 Streamlit、MCP 和 SQLite 队列消费者，仅本机访问。打开输出的 URL（默认 `http://127.0.0.1:8501`，被占用时顺延）。主界面是多轮对话：输入反应需求 → 助手询问缺参并解释选型 → 在同一对话回答问题或逐项勾选确认假设 → 点击“开始真实模拟”（也可明确回复运行）→ 真实结果自动加入对话。对话回复通过 DeepSeek SSE 分块生成并即时显示；Streamlit 页面使用其原生连接向浏览器刷新内容。可继续问“为什么选择这个反应器？”或说“保留其他条件，出口温度改为600°C”，参数修改生成新规格版本，解释性提问不修改规格。

左侧“＋ 新建对话”建立独立任务，不继承参数；“历史对话”恢复已保存的消息、规格和运行。URL 带任务标识，刷新后可继续。当前条件/诊断、版本对比、案例下载及完整任务导出折叠显示，聊天不代替结构化规格或运行记录。历史任务首次使用聊天界面时仅显示明确标注的恢复说明，不伪造之前的对话。

MCP 端点默认 `http://127.0.0.1:8765/mcp`。外部 MCP 客户端可复用与 UI 相同的应用服务：能力查询、结构化校验、新建/补参、规格版本查询、提交运行、结果和审计查询；不提供任意脚本或 COM 调用。

```powershell
.\scripts\stop.ps1
```

关闭脚本只停止本项目启动的三个进程树，保留 HYSYS 应用。停止前检查没有运行中的任务；强制关闭期间任务会留下 `running`，消费者重启时只恢复超时过期记录，最多两次队列尝试。HYSYS 超时终止的是 Python Worker，HYSYS 本体可能仍在计算，应先人工检查后再提交。

日志在 `logs/`；数据库/案例/运行产物在 `data/`，均不进入 Git。每个 HYSYS Worker 在独立 STA 进程执行，全局文件锁跨进程序列化 HYSYS。无 Docker/PostgreSQL/Redis/Celery。

## 命令行演示与测试

可单独运行 `worker`、`worker-once`、`mcp`；使用启动脚本时不必重复启动。对已审阅的测试文件：

```powershell
.\.venv\Scripts\python.exe -m reactor_agent.cli run-spec examples/hydrogenation.json --test-data-confirmed
```

该命令仍调用大模型选型，**不是固定类型测试执行器**。测试文件不能直接代表现场用户批准；若模型询问条件，请在 UI 恢复对应任务。已有后台消费者时由其处理队列，CLI 立即查询可能尚未完成，在 UI 查看。

```powershell
.\.venv\Scripts\python.exe -m pytest
$env:RUN_HYSYS_INTEGRATION='1'
.\.venv\Scripts\python.exe -m pytest tests/hysys
```

普通测试使用明示的替身以隔离后端，不产生或冒充真实模拟。实机测试另行标识，覆盖空案例、四组求解与重开、未知组分错误。模型在线能力用 `model-check`；自然语言端到端实测详情在 `docs/deepseek-validation.md`。

## 项目结构与交付

- `reactor_agent/domain/`：协议、Pint单位、反应/边界工程校验。
- `agent/` 与 `skills/`：模型解析/选择、LangGraph主图、领域工作流说明。
- `harness.py`、`service.py`、`mcp_server.py`：执行策略、应用入口、可复用领域工具。
- `hysys/`：受限 Adapter、串行 STA Worker；`storage/`：任务/版本/运行/队列/审计。
- `scripts/`、`tests/`：本地启动、停止、记录导出、离线与实机测试。
- `docs/demo-runbook.md`：两个必测场景录屏流程、演示顺序、检查单和开发记录导出。
- `docs/technical-report.md` 与 `output/pdf/technical-report.pdf`：两页技术报告。
- `docs/evidence/`：去除密钥的实际结果与规格快照。原始 `.hsc` 保存在本机 `data/cases/`。

本地 Git 已初始化。GitHub 仓库地址/账号尚未指定，未擅自发布。运行录屏由用户完成；AI可见会话记录已导出到本机 `data/deliverables/ai-development-session.md`，可用 `scripts/export_session.py` 更新。

官方参考：DeepSeek `https://api-docs.deepseek.com/quick_start/pricing`、`/guides/tool_calls`、`/guides/json_mode`；HYSYS 本机帮助位于 `C:\ProgramData\AspenTech\Aspen HYSYS V15.0\HtmlHelp`，特别是 Customization FAQ 与 Key HYSYS Objects。
