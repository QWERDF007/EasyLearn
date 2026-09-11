# 当前架构问题优先级清单

本清单基于当前源码结构、依赖关系和核心执行流程的静态分析。它是问题决策清单，不替代项目结构导航；模块职责与调用链见 [`project-structure.md`](project-structure.md)。

当前没有足够证据判定 P0。最高优先级为 P1；P0 仅适用于已确认的数据丢失、安全边界失效或生产不可用问题。

---

## 1. P1

### 问题
文件发布与 SQLite 提交不是同一可回滚事务。

### 代码位置

- `src/easylearn/parser.py:219-253`
- `src/easylearn/files.py:91-98`
- `src/easylearn/documents/service.py:119-130`

### 为什么是问题

解析结果目录先通过文件系统发布，之后才写入 `parse_results` 和 `active_parse_id`。删除流程则先提交数据库删除，再删除文件。文件系统和 SQLite 没有共同事务，也没有完整的补偿协议。

数据库提交失败时，正式 parse 目录可能已经存在但没有数据库指针；文件删除失败时，数据库记录可能已经消失但目录仍然存在。

### 影响范围

- 解析结果可见性
- `active_parse_id` 一致性
- 磁盘空间与维护清理
- 文档删除、parse pruning 和导出产物
- 异常恢复与重试

### 建议改法

建立统一的 publication protocol：

1. 所有产物先写 staging 目录；
2. 生成完整 manifest 和 identity；
3. 以数据库状态记录 publication fence；
4. 原子移动后再确认数据库状态；
5. 启动维护任务负责 orphan reconciliation；
6. 删除流程采用可重试的 tombstone/cleanup 状态。

跨文件系统和 SQLite 无法实现真正原子事务时，必须依靠幂等状态机与补偿清理保证最终一致。

### 是否值得现在修改

值得，现在优先处理。

---

## 2. P1

### 问题
应用启动异常路径可能掩盖根因并泄漏资源。

### 代码位置

- `src/easylearn/main.py:88-117`
- `src/easylearn/main.py:129-155`
- `src/easylearn/main.py:180-206`
- `src/easylearn/database.py:105-144`

### 为什么是问题

部分资源在统一 `try/finally` 之前创建，`qa_http` 在后续配置分支中才赋值，而 finally 阶段统一读取并清理资源。数据库连接也可能在初始化完成前尚未写入 `self.connection`。

因此启动过程在数据库迁移、日志配置、HTTP client、MinerU 初始化等阶段失败时，清理代码可能覆盖原始异常，或无法释放实例锁、日志 handler 和局部 SQLite 连接。

### 影响范围

- 首次启动失败
- 数据库迁移失败
- MinerU 初始化失败
- Windows 实例锁残留
- 后续重新启动
- 故障诊断日志

### 建议改法

- 所有可清理资源在进入 `try` 前显式初始化为 `None`；
- 将资源获取和清理放进统一生命周期 scope；
- `Database.open()` 对局部连接增加异常关闭路径；
- 区分资源尚未创建和资源创建失败；
- 保留原始启动异常，不让清理异常覆盖它。

### 是否值得现在修改

值得，现在处理。修改范围小，但能显著提升启动失败时的可诊断性和可恢复性。

---

## 3. P1

### 问题
MinerU backend 配置契约大于生产实际能力。

### 代码位置

- `src/easylearn/mineru/schema.py:13-51`
- `src/easylearn/mineru/embedded.py:47-65`
- `src/easylearn/parser.py:90-112`
- `src/easylearn/mineru/client.py:57-258`
- `src/easylearn/main.py:135-160`

### 为什么是问题

配置模型声明了多个 backend，但生产解析固定使用 `EmbeddedMinerU`，实际只接受 `vlm-engine`。`MinerUClient` 没有接入生产 `ParseService`，不支持的 backend 可能先排队、后在执行阶段失败。

配置校验因此不能证明用户选择的 backend 可执行。

### 影响范围

- 解析任务提交
- MinerU 配置和健康检查
- 远程 MinerU 能力
- 异步任务失败信息
- 部署文档和用户预期

### 建议改法

二选一并保持单一真相源：

1. 当前只支持嵌入式 VLM：缩窄配置枚举，并在 admission 阶段拒绝不支持值；
2. 确实支持多 backend：定义统一 MinerU backend interface，由组合根按配置接线 `EmbeddedMinerU` 和 HTTP client。

不能继续保留 schema 支持但生产不接线的伪能力。

### 是否值得现在修改

值得，现在处理。

---

## 4. P1

### 问题
Source edit 没有统一传播到所有下游流程。

### 代码位置

- `src/easylearn/source_edits.py:51-169`
- `src/easylearn/main.py:365-425`
- `src/easylearn/translation.py:643-644`
- `src/easylearn/qa.py:92,312`
- `src/easylearn/exports.py:169-198`

### 为什么是问题

阅读器和 Markdown 路径显式使用 `effective_ir()`；翻译、QA 和导出主要读取 base IR。用户修改原文后，页面可能显示新文本，但翻译、QA 证据和导出仍使用旧文本。

这会破坏原文修订作为统一输入的单一真相源。

### 影响范围

- 阅读内容与翻译结果
- QA 证据和引用
- 导出文件
- revision、缓存和任务快照
- 用户对人工修改的信任

### 建议改法

先明确产品契约：

- 如果 source edit 应影响所有下游，让翻译、QA、导出统一通过 effective-IR input facade；
- 如果 source edit 仅限阅读器，明确命名和文档契约，并在 UI 说明下游不会自动更新。

不要让不同调用方自行决定读取 raw IR 还是 effective IR。

### 是否值得现在修改

值得现在确认和设计；代码修改取决于产品对 source edit 传播范围的最终定义。

---

## 5. P1

### 问题
前端模板、设置和后端请求存在契约漂移。

### 代码位置

- `src/easylearn/static/app.js:2503-2506`
- `src/easylearn/templates/index.html:1-534`
- `src/easylearn/static/app.js:894-919`
- `src/easylearn/static/app.js:1694-1724`
- `src/easylearn/source_edits.py:96-99`
- `src/easylearn/static/app.js:1970-1990`

### 为什么是问题

静态分析发现：

- `app.js` 直接使用 `#delete-button`，当前模板没有对应节点；
- 设置面板部分控件没有进入 `ParseRequest`；
- 多 editable-node source edit 会向后端发送空文本，而后端拒绝空文本；
- 前端依赖 `TaskView.scope`、`result_ref.follow_up_task_id(s)` 等内部结构；
- raw view、模板 tab 和缓存 key 存在不一致。

`#delete-button` 是否会在真实初始化阶段阻断后续脚本，仍需浏览器 smoke 确认，但契约风险已经明确。

### 影响范围

- 首屏初始化
- 删除、设置和解析参数
- 多节点原文编辑
- 任务重试和 follow-up
- 前后端版本演进

### 建议改法

先建立真实契约边界：

1. 运行浏览器 smoke 确认 DOM 初始化；
2. 清理不存在或未接线的控件；
3. 让设置字段拥有唯一提交入口；
4. 让 source edit 按 node 保存，不发送空文本；
5. 降低前端对 task scope 私有结构的依赖；
6. 对稳定 wire model 集中维护客户端类型。

### 是否值得现在修改

值得现在验证并修复。

---

## 6. P2

### 问题
数据库 schema 与 SQL 规则横穿多个应用服务。

### 代码位置

- `src/easylearn/database.py:18-203`
- `src/easylearn/documents/service.py:65-130`
- `src/easylearn/parser.py:221-253`
- `src/easylearn/translation.py:649-781,869-1163`
- `src/easylearn/qa.py:294-460`
- `src/easylearn/exports.py:172-198`
- `src/easylearn/source_edits.py:60-149`
- `src/easylearn/maintenance.py:28-166`

### 为什么是问题

`Database` 只封装连接、锁和事务；表结构和业务持久化规则散落在多个应用服务。字段、事务策略或 schema 变更都可能横向修改多个模块。

简单增加一批一对一 repository wrapper 不会增加模块深度，只会增加层数。

### 影响范围

- schema 演进
- 迁移和回滚
- SQLite 替换
- 事务一致性
- 单元测试和集成测试隔离
- 业务模块 locality

### 建议改法

在稳定业务边界上抽取真正有行为的持久化模块，例如 document/parse、translation/revision、QA record 和 publication repository。每个模块应封装查询、状态约束、事务边界和错误语义，而不是只转发 SQL。

### 是否值得现在修改

值得规划，但不建议立即大规模重构。优先在下一次真实 schema 或持久化需求变更时切入。

---

## 7. P2

### 问题
`ParseService` 承担过多变化轴。

### 代码位置

- `src/easylearn/parser.py:67-131`
- `src/easylearn/parser.py:131-284`
- `src/easylearn/parser.py:286-506`

### 为什么是问题

输入转换、PDF 预检、Office、MinerU、CAS、IR 发布、数据库写入、自动翻译 follow-up 和版本清理都集中在同一编排模块。

Office、MinerU、发布和清理任一变化都会触及同一大模块，测试也必须构造大量具体基础设施。

### 影响范围

- 解析可靠性
- 外部转换器替换
- MinerU backend 扩展
- 测试成本
- 发布和清理失败处理

### 建议改法

按真实变化点逐步划分深模块：

```text
InputNormalizer
  → PdfPreflight
  → MinerURunner
  → ResultNormalizer
  → ArtifactPublisher
  → ParseRecordWriter
```

每个模块应拥有完整的不变量和错误语义，避免浅层 forwarding wrapper。

### 是否值得现在修改

值得中期处理，不建议在 P1 正确性问题解决前立即拆分。

---

## 8. P2

### 问题
`TaskManager` 只保存进程内状态，重启后无法恢复。

### 代码位置

- `src/easylearn/tasks.py:112-140`
- `src/easylearn/tasks.py:236-496`
- `src/easylearn/jobs/schema.py:36-39`
- `src/easylearn/main.py:307-334`

### 为什么是问题

进程重启或异常退出后，queued/running 任务、进度、QA 流式答案和取消状态全部消失。翻译虽然可能已经部分落库，但任务级状态不再可查询。

这是当前单 worker/local workspace 的明确设计，不是偶然实现。

### 影响范围

- 服务重启
- 长时间解析和翻译
- QA 流式任务
- 用户重试体验
- 崩溃恢复和运维

### 建议改法

如果产品需要恢复能力：

1. 增加持久化 job record；
2. 为任务阶段和 publication 定义可恢复 checkpoint；
3. 启动时恢复 queued/running 状态；
4. 保证 executor 幂等；
5. 区分任务状态持久化和流式答案持久化。

如果产品明确只支持单进程 local-only，应把“不支持重启恢复”写入运行契约。

### 是否值得现在修改

有条件值得。如果用户经常运行长解析或长翻译，值得现在规划；否则可以延后。

---

## 9. P2

### 问题
QA 会话能力探测和流式重试可能导致上下文丢失或答案重复。

### 代码位置

- `src/easylearn/translation.py:317-443`
- `src/easylearn/translation.py:479-493`
- `src/easylearn/qa.py:293-310`
- `src/easylearn/qa.py:369-374`

### 为什么是问题

provider session 探测结果允许为 `None`，但 QA 只在明确为 `False` 时重新视为首轮。另一方面，流式请求在已经产生部分 delta 后可能重新请求，QA 又直接追加重试结果，没有 offset、request id 或去重机制。

这是静态推断，具体表现取决于 provider 的会话和重试行为。

### 影响范围

- 多轮 AI 解读
- 全文上下文
- 流式回答质量
- 引用编号
- 最终答案重复

### 建议改法

- 将 session capability 区分为 `active / inactive / unknown`；
- `unknown` 时采用安全的完整上下文策略；
- 已产生 delta 后不要透明重试并直接拼接；
- 使用 idempotency key、续传 offset，或重试时重置答案缓冲；
- 增加真实 provider 和 FakeLLM 的重复输出测试。

### 是否值得现在修改

值得现在验证。如果 QA 是核心功能，应尽早建立明确的流式重试契约。

---

## 10. P2

### 问题
QA 引用解析与前端定位能力不一致。

### 代码位置

- `src/easylearn/qa.py:378-407`
- `src/easylearn/qa.py:480-491`
- `src/easylearn/static/app.js:995-1028`
- `src/easylearn/static/app.js:2087-2117`

### 为什么是问题

后端会把回答中的 `[数字]` 解析为引用，普通编号或年份可能被误识别；后端保存多条 citation，但前端目前只使用有限的首 citation/context 作为定位锚点。

因此后端保存的数据结构与用户实际可用的引用交互不一致。

### 影响范围

- QA 任务成功率
- 引用准确性
- PDF/结果块定位
- 问答历史
- 用户对答案可信度的判断

### 建议改法

- 定义明确的 citation token 语法；
- 后端只解析符合协议的引用标记；
- 前端逐引用渲染可点击 token；
- 将普通方括号文本与 citation 分开处理；
- 将 citation view 作为稳定 wire contract。

### 是否值得现在修改

值得现在处理。引用是 QA 的核心可验证能力。

---

## 11. P2

### 问题
发布后的资产 checksum 没有形成完整闭环。

### 代码位置

- `src/easylearn/mineru/result.py:125-219`
- `src/easylearn/parser.py:458-468`
- `src/easylearn/documents/service.py:168-178`
- `src/easylearn/files.py:104-112`

### 为什么是问题

生成阶段会校验 CAS/ZIP 内容，但发布后的普通资产读取主要校验路径和存在性，不重新核对 IR 中声明的 SHA-256。文件被替换、截断或复制损坏时，读取路径可能仍然成功。

### 影响范围

- 图片和资产显示
- 导出文件
- 文件损坏诊断
- 解析产物可信度
- 长期存储一致性

### 建议改法

根据性能需求选择：

- 发布时 read-back 校验；
- 读取时按需校验并缓存结果；
- 以 CAS object 作为唯一读取来源；
- 启动 maintenance 执行资产 reconciliation。

不应在每次普通请求中无条件重新计算全部大文件 hash。

### 是否值得现在修改

值得规划，但优先级低于发布一致性问题。

---

## 12. P2

### 问题
数据库迁移 locality 不完整。

### 代码位置

- `migrations/versions/`
- `src/easylearn/database.py:18-203`

### 为什么是问题

当前独立 migration 目录为空，初始 schema 和历史迁移逻辑集中在 `database.py`。迁移审计、回滚、中断恢复和 schema 变更影响范围都不够清晰。

### 影响范围

- 旧数据库升级
- schema 变更
- 回滚和部署
- 数据库问题排查
- 团队协作

### 建议改法

下一次实际发生 schema 变更时建立 versioned migration source：

- 每个版本独立迁移；
- 明确向前/向后兼容策略；
- 迁移前后校验；
- 保留数据库版本历史；
- 不再把历史迁移全部放在连接类实现中。

### 是否值得现在修改

暂不建议单独提前改。等下一次真实 schema 变更时一起迁移。

---

## 建议处理顺序

### 第一批

1. 文件发布与 SQLite 一致性；
2. 生命周期异常清理；
3. MinerU backend 能力契约；
4. 确认 source edit 传播语义；
5. 前端模板和请求契约 smoke 验证；
6. QA 流式重试与引用契约。

### 第二批

7. 数据库持久化边界；
8. ParseService 拆分；
9. 资产 checksum reconciliation；
10. 任务恢复能力。

### 第三批

11. 数据库迁移 locality；
12. DocumentIR 中领域模型与基础设施 provenance 的进一步解耦。

以上内容只记录问题、证据和建议，不包含代码修改。
