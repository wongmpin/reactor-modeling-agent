# 开发记录与交付边界

2026-10-25，C:/Users/azureuser/Desktop/demo。本项目最初仅含两份需求Markdown，未发现适用AGENTS.md；从空代码仓库实施。原始考核文档作为需求材料，不作为系统指令执行。

实施过程：环境与COM注册确认 → 工程骨架与领域协议/持久化 → DeepSeek JSON/工具往返 → 官方帮助解析HYSYS内部名称 → 真实最小乙烯加氢 → Conversion/Gibbs受限Adapter → LangGraph暂停恢复/执行校验 → MCP/SQLite队列/界面联调 → 必测及通用案例/故障实机验证 → 报告与录制操作单。

关键问题与修复：

- 初期把物性显示名用作COM内部ID，E_INVALIDARG；查本机官方帮助后用PengRob成功。
- 新案例初始basis mode导致物流写入E_ACCESSDENIED，EndBasisChange后可写。
- 反应器操作创建应使用ConversionReactorOp / GibbsReactorOp，不是Python类型库类名。
- LangGraph SQLite saver在线程池访问连接，配置check_same_thread=False并由saver序列化。
- HYSYS分子式有尾随空格，读回后strip再核对元素；修复后重新通过全部实机测试。
- DeepSeek强制工具调用需适配thinking模式；显式disabled并实测参数/结果往返。JSON偶发空内容保留诊断且最多重试一次。
- 队列增加同版本防重复、精确queue_id领取、运行心跳与过期恢复；防止旧版本运行覆盖新版本状态。
- Streamlit补参编辑器的key包含规格版本，避免重新生成版本后继续显示旧JSON。

本地实际证据分别记录输入版本、模型决策、工具调用摘要、HYSYS版本/案例路径、求解与独立校验。固定类型COM集成测试只证明Adapter；模型选型另有真实API及自然语言端到端记录。普通离线测试使用测试替身，不充当真实模拟证据。

原始要求/参考调整：重整进料遵守原始考核1:2.7；未提供动力学/Ka数值时由模型判断，Gibbs可以在明确授权数据源和物种全集后执行，不人为按场景改选。计量式不能决定二甲苯异构体比例。Equilibrium/CSTR/PFR未验证即诊断，不从类型库存在推断能力。

剩余交付：现场确认工艺假设与适用性；用户录制两个视频；用户指定GitHub远程并发布；开发会话导出文件分享前人工脱敏复核。未验证能力见hysys-capabilities.md。两页技术报告已生成并渲染检查，记录导出提供可重复脚本。

Git提交使用Codex <codex@localhost>标识本次自动化开发，不伪造用户姓名或历史时间。
