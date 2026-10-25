# 架构

```text
Streamlit -> ReactorAgentService -> LangGraph -> DeepSeek provider
                                      |              (解析/反应器判断)
                                      v
                               Harness 校验
                                      |
MCP Server -> SQLite WAL queue <- 本地 Worker -> 受限 HYSYS COM Adapter -> HYSYS V15
                     |                 |
          tasks/specs/runs/audit      案例文件/流股结果
```

- `domain` 定义 Pydantic 协议、单位转换和确定性校验。工程校验器不替换模型的反应器判断，并提供边界物流元素流率衡算。
- `agent` 负责结构解析、模型驱动的反应器判断和状态化工作流。DeepSeek 不直接接触 COM。
- `service` 是界面与应用/领域层之间的入口。Streamlit 不调用 Worker 对象。
- `mcp_server` 提供带输入 Schema 的领域工具、能力查询和任务审计访问。
- `storage` 用 SQLite WAL 保存任务、规格版本、运行、审计和跨进程队列；不在模拟求解期间持有数据库事务。
- `hysys.worker` 在独立进程的主线程初始化 STA COM。Adapter 暴露有限操作；执行已验证的气相单反应Conversion和指定物种Gibbs配置；其他选择诊断后停止。

规格版本是建模输入的来源；聊天文字只提供上下文。新任务由新任务 ID 隔离，修改规格通过 `next_version()` 保存新版本。创建案例、求解收敛和工程校验分别记录，三者不能互相推断。


执行授权绑定结构化规格版本的SHA256与模型决策审计，手改类型或条件后必须重新调用模型。UI与MCP共享应用服务，主图执行分支通过闭包注入受限executor，模型接触不到COM对象。执行图与补参图使用独立thread_id持久化checkpoint；新任务不继承旧任务状态。

本地脚本启动Streamlit、MCP和单个队列消费者。消费者用数据库旁锁文件防止重复启动；运行中记录有心跳，过期running记录最多恢复两次。各COM调用还持有全局HYSYS锁。事务仅用于队列领取和结构化存储，不跨越网络请求或HYSYS求解。
