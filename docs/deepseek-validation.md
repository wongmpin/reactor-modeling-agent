# DeepSeek 在线验证记录

日期：2026-10-25 UTC。端点：`https://api.deepseek.com`，请求模型：`deepseek-flash`。

## 已验证

- 真实 API 请求成功，JSON 输出可解析。
- 模型生成 `ping` 工具调用，参数严格为 `{"value":"reactor-agent"}`。
- 将本地工具结果回传后，模型返回 JSON 确认相同值；CLI `model-check` 返回 `ok: true`。
- 甲苯歧化自然语言解析：模型选择 conversion，识别 50% 转化率及 10,000 kg/h 质量流量，并主动询问二甲苯异构体分布。
- 通用乙烯加氢需求：模型选择 conversion，保留 40% 转化率，并询问物性包确认。
- 两项任务补充后均通过规格校验：任务 ID 保持不变，规格生成第 2 版；转化率保持不变。测试中的二甲苯 0.2/0.5/0.3 比例是在线验证脚本的显式输入，仅用于验证补参协议，不是考核模型已确认的工程数据。
- 甲烷重整解析保留两条反应和给定工况；模型选择 equilibrium 候选，要求确认平衡数据来源、流量/年运行假设、物性包及第二温度工况。校验器没有替换该选择，也没有生成模拟结果。

## 修复

默认思考模式不接受探针指定的 `tool_choice`，返回 HTTP 400。Provider 现显式发送 `thinking.type=disabled`，已实测工具往返成功。服务入口的 LangGraph SQLite checkpoint 连接也已改为允许 saver 的执行线程访问，解决真实补参时的 `sqlite3.ProgrammingError`。

反应网络校验修复：上游反应生成的中间组分可用于下游反应，不能要求所有反应物均直接出现在进料；不可由进料及反应网络生成的反应物仍返回诊断。

## 证据与限制

在线需求及恢复输出位于本地 `data/online-validation/`，不进入 Git。文件是模型解析/规格校验记录，不是 HYSYS 模拟结果。HYSYS后续已打通真实反应器链路，见 `hysys-capabilities.md`。本记录不包含密钥。

## 后续端到端实测

- 从纯自然语言输入乙烯加氢，模型独立解析并选择conversion，经Harness、SQLite队列、真实HYSYS执行与工程校验，状态validated。
- 结构化测试输入仍经模型独立选型：甲苯选择conversion、710°C重整选择gibbs，均真实执行通过。600°C初次选择equilibrium，校验器保留该选择并诊断缺数据；补充数据库/物种边界与假设确认后，模型重新选择gibbs并真实运行通过。全过程没有后端强制改选。
- 2026-10-25再次获取DeepSeek官方pricing、tool_calls、json_mode页面，确认当前模型ID和功能；官方说明JSON模式偶有空内容。本机复验也遇到一次无效JSON，失败诊断后重测通过，现探针最多两次重试。
- 模型别名服务可能变化，现场先执行model-check；已测试能力不保证每次模型选型或API响应都相同。
