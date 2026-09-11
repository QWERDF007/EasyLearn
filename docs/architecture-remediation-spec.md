# EasyLearn 架构可靠性整改规格

**状态：Completed，全链路 9 个垂直可靠性工单已全量实现与回归验证。**

本规格将当前架构问题优先级清单收敛为一组可验收的可靠性整改目标。项目结构、模块职责和调用链见 [`project-structure.md`](project-structure.md)；问题证据清单见 [`architecture-priorities.md`](architecture-priorities.md)。

## Problem Statement

从用户和维护者视角，EasyLearn 的主流程在正常路径上可工作，但多个跨模块契约没有被单一模块拥有：

- 解析产物先写文件、后写 SQLite，异常时可能产生数据库记录与文件目录不一致；
- 启动失败时，资源清理路径可能掩盖原始异常或留下锁、连接和日志资源；
- 配置声明的 MinerU backend 范围大于生产实际接线能力，错误会延迟到异步任务执行阶段；
- 原文修订在阅读器和 Markdown 中生效，但翻译、QA、导出主要读取基础 IR，用户无法判断下游结果是否仍然对应当前原文；
- 浏览器脚本依赖手写 DOM、请求和任务字段契约，部分控件未接线，多节点原文修订存在空文本请求路径；
- QA 多轮会话、流式重试和引用解析缺少统一的 provider 与 wire contract；
- TaskManager、具体 SQLite SQL、ParseService 的变化轴和前端大型脚本使后续修复的 locality 较差。

这些问题共同表现为：用户看到的状态、文件系统状态、数据库状态、模型上下文和页面状态可能不是同一个事实源。

## Solution

建立以**持久化发布状态**和**有效文档快照**为核心的可靠性合同，并按垂直切片逐步收敛现有模块：

1. 让解析与导出使用可恢复的 staging → validated → published → recorded 发布状态，异常时由补偿和 reconciliation 收敛文件与 SQLite；
2. 让应用生命周期拥有明确的资源 acquisition/rollback/cleanup 规则，原始启动异常始终保留；
3. 让 backend capability 在任务 admission 阶段与实际 adapter 能力一致；
4. 让所有需要当前文档内容的下游统一消费有效文档快照，而不是各自选择 base IR 或 overlay IR；
5. 让 source edit、翻译状态、QA evidence、导出 snapshot 和浏览器 UI 使用同一个 source revision/fingerprint 契约；
6. 让前端只通过集中 wire adapter 消费稳定字段，并通过真实 DOM smoke 验证页面启动和关键用户路径；
7. 让 QA session capability、流式重试和 citation token 形成可测试的协议；
8. 将任务恢复、资产 reconciliation、数据库持久化深模块和 parser 深化列为后续可独立交付的工作，不在第一批中引入无收益的大规模重构。

推荐的 source edit 默认语义是：**原文修订影响所有下游结果**。如果产品最终决定 source edit 仅限阅读器，则必须在实现前明确记录该例外，并从本规格中移除下游传播验收项。

### 发布状态模型

```text
staged → validated → published → recorded
   └──────────────→ abandoned / reconciled
```

- `staged`：产物位于任务临时目录，尚未对用户可见；
- `validated`：manifest、IR、资产和输入 identity 已通过校验；
- `published`：正式文件目录已原子替换；
- `recorded`：SQLite 已记录正式路径并更新 active pointer；
- `abandoned/reconciled`：失败产物已被清理或被维护任务登记处理。

## User Stories

1. As a document reader, I want a failed parse not to leave a visible or downloadable phantom parse, so that document history reflects only durable results.
2. As a document reader, I want a successful parse to have both its files and database view available together, so that refresh and download show the same result.
3. As a document reader, I want document deletion to converge even when file removal temporarily fails, so that storage cleanup can retry without losing the database intent.
4. As an operator, I want the original startup exception to remain visible, so that a broken configuration or migration can be diagnosed from one error.
5. As an operator, I want a failed startup to release all resources it acquired, so that fixing the configuration allows an immediate restart.
6. As a document reader, I want an unsupported MinerU backend rejected before a task is queued, so that I receive an actionable error instead of a delayed task failure.
7. As a document reader, I want a configured MinerU backend to correspond to a real production adapter, so that the configuration screen does not promise unavailable capability.
8. As a document reader, I want an original-text correction to be reflected consistently in reading, translation, QA evidence and export, so that every downstream result uses the text I approved.
9. As a document reader, I want existing translations to be marked stale or revalidated after a source correction, so that an old translation is never silently presented as current.
10. As a document reader, I want multi-node blocks to be editable without sending fabricated empty node values, so that a correction preserves the semantic structure of the block.
11. As a document reader, I want parse settings shown in the workbench to be the settings actually submitted, so that the visible controls have observable effect.
12. As a document reader, I want the workbench to initialize all controls that it renders, so that one missing DOM element cannot disable unrelated actions.
13. As a document reader, I want task retry and follow-up actions to rely on stable task fields, so that a backend implementation change does not silently break the UI.
14. As a document reader, I want a QA follow-up to restore full-document context when the provider session is unavailable or unknown, so that a lost remote session does not produce an answer without context.
15. As a document reader, I want a partial streamed answer never duplicated by an automatic retry, so that the final answer is coherent.
16. As a document reader, I want every citation marker to resolve to the frozen evidence that produced it, so that I can verify an answer instead of trusting an opaque number.
17. As a document reader, I want ordinary bracketed numbers in an answer not to be mistaken for citations, so that years, indexes and notation do not fail QA tasks.
18. As an operator, I want the application to state whether tasks survive a restart, so that a long-running parse or translation is not mistaken for a recoverable job when it is not.
19. As a maintainer, I want the publication, effective-input and provider contracts to be tested through their highest existing seams, so that one behavior change is verified across API, task, storage and browser consumers.
20. As a maintainer, I want persistence and parser refactors to be introduced after behavior contracts are stable, so that structural work reduces complexity instead of moving unverified behavior.

## Implementation Decisions

### 1. Publication and reconciliation seam

- Treat parse and export publication as one domain operation with a durable state, not as an unstructured sequence of file and SQL side effects.
- Keep long-running parsing, LLM calls and file generation outside SQLite transactions.
- Persist enough publication identity to distinguish a staged result, a published directory, a recorded row and an orphan.
- Make reconciliation idempotent and safe to run at startup and after task failure.
- Preserve existing `TaskContext.publish()` as the highest task seam, but move publication invariants behind a deep publication module rather than adding more route-level cleanup.
- Apply the same contract to parse output, export output and document deletion where the storage state can diverge from database state.

### 2. Lifecycle resource contract

- Initialize every optional resource before entering the lifecycle `try` block.
- Make each cleanup operation conditional on successful acquisition.
- Preserve the first startup or shutdown failure; cleanup failures are secondary diagnostics.
- Ensure a failed database open closes its local connection before returning.
- Keep the configured graceful-shutdown budget as the source for resource cleanup rather than duplicating a hard-coded timeout.

### 3. MinerU capability contract

- The supported backend set exposed to configuration must equal the set that the production composition root can execute.
- Unsupported backend values must fail during admission with a stable domain error, before a task enters the queue.
- If multiple adapters are implemented later, they must satisfy one real backend interface with explicit capability metadata; a schema enum alone is not an adapter seam.
- Health and configuration views must report capability availability separately from model-path configuration.

### 4. Effective document snapshot seam

- Add one highest-level document-input seam that returns the immutable base IR plus persisted source overlays as one effective snapshot.
- Translation, QA and export must consume that seam; they must not independently choose between base IR and source edits.
- The effective snapshot must carry a stable source revision or fingerprint derived from the parse identity and ordered source overlays.
- Translation rows must be associated with the source revision/fingerprint for which they were produced.
- After a source edit, existing translation text and history are preserved for audit, but the current result must be marked stale or excluded from the current effective translation unless it matches the new source fingerprint.
- Manual translation text must not be silently treated as current after its source changes; the UI must expose the stale state and provide an explicit retranslation or review path.
- The parsed JSON remains immutable; overlays continue to be persisted separately and applied through `model_copy`-style derivation.

### 5. Source editing contract

- A block with one editable node may keep a block-level editor.
- A block with multiple editable nodes must expose node-level editing or a structured editor; it must not collapse the block into one string and clear the remaining nodes.
- Each edit remains optimistic-concurrency protected by its node revision.
- Empty text remains rejected unless a future node-removal contract explicitly defines deletion semantics.
- After a successful edit, the browser reloads the effective snapshot and displays the resulting stale/current translation status.

### 6. Browser wire and DOM contract

- Python/Pydantic models remain the field and constraint source of truth.
- The browser gets one centralized adapter for parse requests, task scopes, follow-up references, translation views and QA records.
- DOM event binding must tolerate optional controls only when the feature is genuinely optional; required controls must be present in the template smoke contract.
- Settings controls either submit a supported field or are removed from the visible settings surface.
- The first browser smoke path must cover page initialization, upload, parse submission, delete dialog opening, settings submission and a multi-node source-edit fixture.

### 7. QA session, streaming and citation contract

- Session probing is tri-state: active, inactive or unknown.
- A first turn is required when the session is inactive or unknown and the provider cannot guarantee context continuity.
- Automatic retry is allowed before the first answer delta; after a delta has been emitted, the adapter must either resume from a provider-supported offset or fail without appending a second prefix.
- Citation markers use a reserved, unambiguous syntax rather than treating every bracketed integer as a citation. The backend stores structured citation references and the browser renders every valid reference as a navigable control.
- Invalid references are reported as a protocol failure only when they use the reserved citation syntax; ordinary numeric prose remains ordinary prose.

### 8. Deferred structural work

- Do not perform a repository-wide persistence rewrite before the behavior contracts above are stable.
- When a persistence change is next required, introduce deep domain repositories that own SQL, transaction rules and error semantics; do not add one-line forwarding wrappers.
- Split `ParseService` by real variation points only after publication and backend contracts are covered by behavior tests.
- Add versioned migration files with the next schema change rather than introducing a migration framework without a schema need.
- Asset checksum reconciliation and task recovery are separate follow-up capabilities, not implicit side effects of the first reliability wave.

## Testing Decisions

- Tests must assert observable behavior through the highest existing seam: ASGI endpoints, lifecycle context, task views, published file IDs, downloaded artifacts and real browser controls.
- Do not assert private helper calls, SQL statement text, object layout or implementation-specific forwarding.
- Use failure injection to verify publication behavior: database commit failure after file move, file move failure before recording, process cancellation before and after publication, and repeated reconciliation.
- Extend existing parser tests to verify that a failed publication leaves no user-visible phantom result and that reconciliation is idempotent.
- Extend application tests to verify startup failure preserves the original exception and releases acquired resources.
- Add admission and health tests for supported and unsupported MinerU backends using the existing fake MinerU seam.
- Extend feature tests to edit source text and then verify reader output, translation input, QA frozen evidence and export source content all use the effective snapshot.
- Add a multi-node source-edit fixture and test both API rejection of empty text and browser node-level editing behavior.
- Add QA protocol tests for unknown session capability, a provider session disappearing between turns, a transport failure before the first delta, and a failure after a partial delta.
- Add citation tests for valid reserved markers, out-of-range reserved markers and ordinary bracketed numbers.
- Use the existing real Selenium acceptance layer for required DOM controls and the upload/parse/delete path; keep external MinerU and LLM calls behind the existing protocol/fake seams for deterministic tests.
- During implementation, run the focused test group for the current vertical slice first; defer the project-wide suite until integration checkpoints.

## Out of Scope

- Replacing SQLite with PostgreSQL or introducing a distributed task broker.
- Implementing every backend declared by the MinerU schema; the first capability slice only makes declared and executable capabilities truthful.
- Rewriting the complete browser UI or changing the visual design system.
- Adding GPU scheduling, CUDA device management or a separate worker pool.
- Making source edits silently mutate the immutable parse JSON.
- Introducing a generic repository layer without a concrete domain operation and at least one real implementation variation.
- Persisting all task token events or reconstructing arbitrary in-progress LLM streams; task recovery is a separate follow-up decision.
- Adding a migration framework without a schema change that requires it.

## Further Notes

- **Confirmed source-edit decision:** source edits are global inputs to reader, translation, QA and export. This follows the repository SSOT principle and avoids presenting different effective documents to different callers.
- **Approved ticket granularity:** nine vertical slices with only real dependency edges. The draft ticket index is [`architecture-remediation-tickets/README.md`](architecture-remediation-tickets/README.md).
- **Highest seams:** publication/reconciliation, effective document input, backend capability admission, and the browser wire adapter. These seams hide complexity from routes and individual task executors rather than adding pass-through wrappers.
- **Tracker status:** no issue tracker or triage vocabulary is configured in this repository session. Run `/setup-matt-pocock-skills` before publishing the draft tickets to a tracker.
- The repository documents are the current deliverable; no `.scratch` ticket files are retained.

