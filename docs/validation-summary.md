# 最终验证记录

2026-10-25 UTC，Windows / Python3.12.4 / Aspen HYSYS V15 (41.0)。

- 完整测试命令：`RUN_HYSYS_INTEGRATION=1`后执行`.venv/Scripts/python.exe -m pytest`：**50 passed in 36.60s**。
- 其中7项实机测试：空案例连接/保存；4组模型真实求解、工程校验及案例重开；未知组分失败诊断；Gibbs仅给物种全集、不写反应式也可真实建模。
- 其余43项：领域协议/单位/元素守恒/选择性/缺参、模型选型保留、版本/checkpoint/interrupt恢复/有限重试、模型决策版本授权、跨进程SQLite队列、MCP工具/Schema/本机访问约束、Streamlit输入与恢复、会话脱敏导出。
- 最后明确MCP输出Pydantic契约后执行`pytest tests/test_mcp.py`：3 passed，覆盖嵌套输入和明确输出Schema。
- 在线DeepSeek检查：真实API JSON输出、ping工具参数、工具结果回传成功；纯自然语言乙烯加氢从解析到真实HYSYS工程校验成功。
- HTTP MCP客户端：实际initialize、list_tools返回8个工具、能力查询isError=False。
- 本地启动/停止/再启动：UI、MCP、队列消费者均启动；UI `/_stcore/health`返回`ok`。
- PDF技术报告2页，使用Poppler渲染并逐页检查，无截断/重叠。源文件在docs/technical-report.md，PDF在output/pdf/technical-report.pdf。
- Git暂存代码扫描未发现当前.env中的密钥。`.env`、SQLite、运行案例与原始会话导出均被忽略；docs/evidence仅保存脱敏实测结构化证据。

不将普通测试替身当真实模拟，不将单反应或已验证PR能力推断到任意反应器/物性方法。测试输入不代表实际工厂审批，用户录屏需亲自确认假设。

## 2026-10-25 用户反馈修复后

全套54项通过（含8项HYSYS实机）；新增缺参禁用提交、逐项确认与审计、支路质量流约束校验、HYSYS分子量推算总进料。当前用户重整任务按已确认100000t/a、8000h/a重跑，甲烷12500kg/h、混合进料2882.89522kmol/h，两温度均validated；已将先前错误缩放的运行标记为validation_failed保留诊断。
