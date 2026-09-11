# EasyLearn 项目结构总结

本文是代码结构导航，回答“模块在哪里、谁调用谁、数据如何流动”。字段、请求约束和状态枚举不在本文复制；唯一真相源仍是 Python 类型和实现，详见 [文档与实现索引](README.md) 与 [协议实现索引](protocols.md)。

## 1. 总体架构

EasyLearn 是单进程本地工作台：

```text
浏览器
  -> FastAPI / HTTP / SSE
  -> ApplicationState
  -> TaskManager（进程内任务状态）
  -> 文档、解析、翻译、问答、导出服务
  -> SQLite + 本地文档目录 + 临时 CAS
  -> 嵌入式 MinerU / OpenAI-compatible LLM / 可选 Office 转换器
```

核心持久边界有两类：

- SQLite：文档、解析版本、译文、译文历史、原文修订、问答记录。
- 文件系统：原件、解析版本目录、preview、DocumentIR、raw Markdown、图片资产、导出结果和任务临时目录。

任务队列、进行中的翻译/问答答案和 SSE 增量只存在当前进程内。应用不会从 SQLite 恢复未完成任务。

## 2. 目录与模块职责

### 2.1 仓库顶层

| 目录/文件 | 职责 |
| --- | --- |
| `src/easylearn/` | EasyLearn Python 应用源码。 |
| `tests/` | v3 定向行为测试、DocumentIR/MinerU 测试、API 测试和浏览器测试。 |
| `docs/` | 实现索引、协议索引、运行指南和设计说明。 |
| `3rdparty/MinerU/` | 仓库内嵌 MinerU 源码，运行时由 `mineru/embedded.py` 动态加载。 |
| `3rdparty/FreeDeepseekAPI-ZH/` | 可选的本地 DeepSeek 网页代理，不属于 EasyLearn Python 主调用链。 |
| `deploy/run.ps1` | Windows 本机启动脚本。 |
| `config.example.toml` / `config.toml` | 配置示例与本机配置；配置模型真相源是 [`config.py`](../src/easylearn/config.py)。 |
| `migrations/versions/` | 当前为空；数据库 schema 和 v1/v2/v3 迁移目前位于 [`database.py`](../src/easylearn/database.py)。 |
| `data/` | 运行时数据目录，由 `app.data_dir` 配置生成，不是业务源码。 |

### 2.2 Python 入口、组合根与基础设施

| 模块 | 职责 |
| --- | --- |
| [`__main__.py`](../src/easylearn/__main__.py) | 解析 `--config`，加载 `Settings`，可选打开浏览器，以单 worker 启动 Uvicorn。 |
| [`main.py`](../src/easylearn/main.py) | FastAPI app、生命周期、`ApplicationState`、异常映射、全部 HTTP/SSE/文件路由；系统组合根。 |
| [`config.py`](../src/easylearn/config.py) | TOML/.env 加载、Pydantic 配置树、路径/LLM/provider/代理/任务参数。 |
| [`paths.py`](../src/easylearn/paths.py) | `DataPaths`、portable path 校验和运行时目录布局。 |
| [`database.py`](../src/easylearn/database.py) | 单连接 SQLite、schema 初始化、内嵌迁移、读锁和短事务。 |
| [`files.py`](../src/easylearn/files.py) | 实例锁、文档目录、上传保存、目录发布、文件 containment 校验。 |
| [`storage.py`](../src/easylearn/storage.py) | 本地 content-addressed storage；对临时对象、SHA-256 和 CAS 读写负责。 |
| [`execution.py`](../src/easylearn/execution.py) | `asyncio.to_thread` 阻塞操作包装，以及 PDF/MinerU 验证子进程生命周期。 |
| [`tasks.py`](../src/easylearn/tasks.py) | 有界 asyncio 队列、按任务类型的 semaphore、取消、进度、发布 fence、follow-up 和任务保留。 |
| [`cache.py`](../src/easylearn/cache.py) | 进程内 DocumentIR cache。 |
| [`maintenance.py`](../src/easylearn/maintenance.py) | 启动清理临时目录、过期导出和过期 parse 版本。 |
| [`logging_setup.py`](../src/easylearn/logging_setup.py) | 日志配置与文件 sink。 |
| [`errors.py`](../src/easylearn/errors.py) | `DomainError`、`ErrorView` 及跨层错误协议。 |

### 2.3 文档、解析与领域模型

| 模块 | 职责 |
| --- | --- |
| [`documents/service.py`](../src/easylearn/documents/service.py) | 上传、文档视图、parse 查询、IR/raw/asset/export 文件映射、删除和翻译统计。 |
| [`documents/schema.py`](../src/easylearn/documents/schema.py) | `DocumentView`、`ParseResultView` 等 HTTP 视图模型。 |
| [`filetypes.py`](../src/easylearn/filetypes.py) | 输入后缀、MIME、magic/container 和输入格式边界。 |
| [`parser.py`](../src/easylearn/parser.py) | Parse admission、图片/Office 转 PDF、PDF 预检、MinerU、结果发布、自动翻译 follow-up 和 parse pruning。 |
| [`previews/pdf.py`](../src/easylearn/previews/pdf.py) | PDF child process 预检、checksum、页数、几何和可渲染性校验。 |
| [`previews/schema.py`](../src/easylearn/previews/schema.py) | PDF 预检请求、报告和限制模型。 |
| [`document_ir/schema.py`](../src/easylearn/document_ir/schema.py) | 不可变 DocumentIR、页面、block、inline node、asset、table、relation、locator 和 snapshot 全局不变量。 |
| [`document_ir/coordinates.py`](../src/easylearn/document_ir/coordinates.py) | MinerU 坐标到 PDF 用户空间的投影。 |

### 2.4 MinerU 适配层

| 模块 | 职责 |
| --- | --- |
| [`mineru/embedded.py`](../src/easylearn/mineru/embedded.py) | 进程内 MinerU VLM runtime、模型生命周期、vendor 动态导入和结果树写入。 |
| [`mineru/models.py`](../src/easylearn/mineru/models.py) | 配置模型候选并解析默认模型路径。 |
| [`mineru/schema.py`](../src/easylearn/mineru/schema.py) | backend、MinerU options、archive、限制和任务协议模型。 |
| [`mineru/archive.py`](../src/easylearn/mineru/archive.py) | ZIP 根目录、成员路径、压缩规模和归档结构校验。 |
| [`mineru/result.py`](../src/easylearn/mineru/result.py) | child process 结果验证、CAS 提取、PDF registration 和 normalize facade。 |
| [`mineru/registration.py`](../src/easylearn/mineru/registration.py) | origin/preview PDF 几何与渲染一致性、坐标 registration。 |
| [`mineru/adapter.py`](../src/easylearn/mineru/adapter.py) | MinerU middle JSON 到 DocumentIR 的严格归一化。 |
| [`mineru/tables.py`](../src/easylearn/mineru/tables.py) | 表格 markup 到 DocumentIR table structure 的转换。 |
| [`mineru/assets.py`](../src/easylearn/mineru/assets.py) | MinerU 图片路径到已登记资产的解析。 |
| [`mineru/client.py`](../src/easylearn/mineru/client.py) | HTTP MinerU adapter；当前生产解析组合根没有接入它。 |

### 2.5 翻译、问答、导出与交互

| 模块 | 职责 |
| --- | --- |
| [`translation.py`](../src/easylearn/translation.py) | translation unit 切分、LLM client、批量翻译、重试、增量持久化、人工译文、锁定和历史。 |
| [`qa.py`](../src/easylearn/qa.py) | evidence/context 构建、引用校验、LLM 流式问答、session 和 `qa_records`。 |
| [`exports.py`](../src/easylearn/exports.py) | IR/译文/revision 快照、Markdown/JSON/ZIP/图片导出和文件发布。 |
| [`source_edits.py`](../src/easylearn/source_edits.py) | immutable IR 上的 source text overlay、revision 冲突和 effective IR。 |
| [`rendering.py`](../src/easylearn/rendering.py) | source、中文、双语 Markdown 渲染。 |
| `templates/index.html` | 工作台 DOM 结构和静态控件契约。 |
| `static/app.js` | 浏览器 state、fetch/XHR、任务轮询、SSE、IR/翻译渲染和交互编排。 |
| `static/pdf-viewer.js` | PDF 页面、缩放和页面导航 adapter。 |
| `static/pdfjs/` / `static/katex/` | PDF 与数学公式的浏览器资源。 |
| `static/app.css` | 工作台样式。 |

### 2.6 测试目录

- `tests/v3/`：应用生命周期、HTTP 行为、任务并发/取消、解析发布、翻译 revision、QA 引用/SSE、导出快照和 source edit。
- `tests/document_ir/`：DocumentIR 身份、几何、坐标、表格、资产和关系不变量。
- `tests/mineru/`：archive、result、adapter、embedded 和 HTTP client 协议测试。
- `tests/api/`：API 层定向测试。
- `tests/config/`：配置加载与约束测试。
- `tests/browser/`：Selenium 真实浏览器上传、解析进度、PDF 页面和缩放验收。

## 3. 核心入口与生命周期

唯一 Python 启动路径：

```text
python -m easylearn [--config path]
  -> __main__.main()
  -> Settings.load()
  -> create_app(settings)
  -> uvicorn.run(workers=1, reload=False)
  -> FastAPI lifespan
```

生命周期装配顺序位于 [`main.py`](../src/easylearn/main.py) 的 `lifespan`：

1. `DataPaths.ensure()` 创建 data、documents、tmp 目录。
2. `InstanceLock.acquire()` 防止同一 data directory 被多个进程使用。
3. 配置日志。
4. 打开 SQLite，并执行 schema 初始化/迁移。
5. 执行 maintenance cleanup。
6. 创建 `DocumentCache`、`DocumentFiles`、`DocumentService`。
7. 创建共享 LLM HTTP client，必要时创建 QA 独立 client。
8. 创建 parser、source edit、translation、export、QA 服务。
9. 注册 `PARSE`、`TRANSLATE`、`EXPORT`、`QA` 四类 executor。
10. 启动 `TaskManager` dispatcher，把服务对象放入 `app.state.services`。

关闭时按相反方向关闭任务、MinerU runtime、HTTP client、SQLite、日志和实例锁。启动方式、数据目录和单 worker 约束见 [`native.md`](native.md)。

## 4. 主要调用链

### 4.1 上传与解析

```text
浏览器 file input / XHR
  -> POST /api/documents
  -> DocumentService.create()
  -> DocumentFiles.save_upload()
  -> validate_input()
  -> INSERT documents
  -> DocumentView

POST /api/documents/{document_id}/parse
  -> ParseService.submit()
  -> TaskManager.submit()
  -> TaskRecord(queued)
  -> TaskManager dispatcher
  -> ParseService.execute()
  -> prepare PDF
  -> PdfPreflight.inspect()
  -> preview CAS
  -> EmbeddedMinerU.parse()
  -> MinerUResultValidator.normalize()
  -> MinerUAdapter.normalize()
  -> DocumentIR
  -> 临时 published 目录
  -> parse_results + active_parse_id
  -> 可选 Translate follow-up
```

解析执行的实现入口是 [`parser.py`](../src/easylearn/parser.py) 的 `ParseService.submit/execute`；PDF 预检、结果验证和 DocumentIR 归一化的协议索引见 [`protocols.md`](protocols.md)。

### 4.2 浏览器读取解析结果

```text
GET /api/documents/{id}
  -> DocumentService.get()
  -> DocumentView + parse_results + translation_status

GET /api/documents/{id}/parses/{parse_id}
  -> SourceEditService.effective_ir()
  -> page/block 过滤
  -> JSON DocumentIR

GET .../markdown?language=source|zh|bilingual|raw
  -> effective IR + effective translations
  -> rendering.render_markdown()
  -> PlainTextResponse

GET .../files/{file_id}
  -> DocumentService.file_path()
  -> parse/asset/export path containment
  -> FileResponse
```

浏览器端的 `app.js` 保存当前 document、parse、translations、source edits 和 task watchers；异步旧响应通过 generation token 避免覆盖当前文档。

### 4.3 翻译与人工修订

```text
POST /api/documents/{id}/translate
  -> TranslationService.submit()
  -> TaskManager
  -> load_ir()
  -> translation_units()
  -> 读取已有 translations
  -> LLMClient JSON batch
  -> 每批写 translations / translation_history
  -> 更新 progress
  -> TaskView.succeeded

PATCH .../translations/{unit_id}
  -> revision / locked / expected_revision 校验
  -> manual_text overlay
  -> effective_text
```

### 4.4 问答与 SSE

```text
POST /api/documents/{id}/qa
  -> QAService.submit()
  -> load_ir() + effective translations
  -> build_context()
  -> TaskManager
  -> LLM stream
  -> TaskContext.append_answer()
  -> citation 校验
  -> qa_records

GET /api/tasks/{task_id}/answer-stream
  -> TaskManager.answer_stream()
  -> Server-Sent Events: delta / done
  -> 客户端断开时请求取消
```

提交阶段会冻结 evidence/context，worker 不重新解释浏览器的选择状态。

### 4.5 导出

```text
POST /api/documents/{id}/exports
  -> ExportService.submit()
  -> TaskManager
  -> _snapshot(): IR + translations + revisions
  -> Markdown / JSON / manifest / assets
  -> export directory
  -> result_ref.file_id
  -> /api/documents/{id}/files/{file_id}
```

导出快照在任务开始时固定，之后的人工译文修改不应污染已开始的导出。

## 5. 数据流与持久化边界

### 5.1 文件系统布局

`DataPaths` 生成的主要布局：

```text
<data_dir>/
  app.db
  instance.lock
  documents/<document_id>/
    original/source.<suffix>
    parses/<parse_id>/
      preview.pdf
      document.json
      raw.zip / mineru.zip
      images/...
    exports/<export_id>/...
  tmp/<task_id>/
    input.pdf
    cas/objects/<sha256>
    published/...
```

实际路径生成集中在 [`paths.py`](../src/easylearn/paths.py)，文件安全映射集中在 [`files.py`](../src/easylearn/files.py) 和 [`documents/service.py`](../src/easylearn/documents/service.py)。

### 5.2 SQLite 边界

[`database.py`](../src/easylearn/database.py) 使用每进程一个 `aiosqlite` connection、`asyncio.Lock`、WAL 和短事务。主要表：

- `documents`：文档元数据与 `active_parse_id`。
- `parse_results`：解析版本、preview/IR/raw 路径和 metadata。
- `translations`：自动译文、人工译文、锁定标记和 revision。
- `translation_history`：人工/自动/恢复历史。
- `source_edits`：原文 overlay 和 revision。
- `qa_records`：问题、答案、冻结 context 和 citations。

长时间的 MinerU 推理、LLM 请求和文件处理不应持有数据库事务锁。

### 5.3 DocumentIR 边界

DocumentIR 是 parse 版本的不可变快照，包含：

- `document_id`、`parse_run_id`、schema/adapter/MinerU provenance；
- 页面几何和坐标投影；
- block、inline node、table、relation、source locator；
- asset identity、SHA、MIME 和可移植导出路径。

`DocumentIR.validate_snapshot()` 集中检查页序、block/order 唯一性、asset 路径、relation、parent cycle、表格子块、坐标区域和引用完整性。业务服务应把它当作不可变输入，不直接修改 JSON 文件。

### 5.4 任务状态边界

`TaskManager` 只保存当前进程内的 `TaskRecord`：

```text
queued -> running -> succeeded
                  -> failed
                  -> cancelled
```

任务还携带 progress、message、failure、result_ref、answer 和 `publication_committed`。任务记录按 retention 清理；进程退出时未完成任务丢弃。文档数据和已完成结果持久化，但任务生命周期不持久化。

## 6. 线程、异步、进程与 GPU 流程

### 6.1 asyncio 任务模型

- Uvicorn 以一个 worker 运行一个 Python 进程和一个主事件循环。
- `TaskManager.start()` 创建 dispatcher；每个任务由 `asyncio.create_task()` 执行。
- 按 `JobKind` 配置 semaphore：parse、translation、QA 使用配置并发度，export 当前固定为 1。
- 默认配置见 [`config.py`](../src/easylearn/config.py)：queue limit 8，parse/translation/QA request concurrency 默认均为 1，translation batch concurrency 默认为 4。
- cancellation 通过 `cancel_requested`、`TaskCancelled` 和 asyncio task cancellation 传播；不可逆发布由 `TaskContext.publish()` shield，避免发布中途被取消。

### 6.2 阻塞工作线程

[`execution.run_blocking()`](../src/easylearn/execution.py) 使用 `asyncio.to_thread()` 执行同步文件、hash、CAS、目录和部分转换操作，并在取消时等待底层线程结束后再释放资源。

因此线程池用于阻塞 I/O 和 CPU/库调用的异步隔离，但不存在独立的业务线程队列或多进程 worker 池。

### 6.3 验证子进程与外部进程

- `run_validation()` 使用 `sys.executable -m <module>` 启动受控 Python 子进程，输入/输出由 Pydantic 请求/响应协议约束。
- PDF 预检子进程执行 pypdf/PDFium 解码、页几何检查和 render smoke。
- MinerU 结果验证子进程执行 ZIP/CAS/JSON/图片检查与 DocumentIR 归一化。
- Office 开启时，parser 根据配置调用 `office_command` 子进程；关闭时不检查外部 Office 组件。

### 6.4 MinerU 与 GPU

`EmbeddedMinerU` 在当前服务进程内维护模型生命周期：

1. 通过配置的 model path 加载 vendored MinerU analyzer。
2. 用 `asyncio.Lock` 串行保护 analyzer/model runtime。
3. 将 PDF bytes 交给 vendor analyzer，指定 `backend="transformers"`。
4. 将 middle/model/Markdown/content-list/images 写入结果树。
5. 关闭时释放 cached models 和 PDF render executor。

EasyLearn 自身没有显式 CUDA device、GPU memory pool 或 GPU worker 管理。是否使用 GPU、使用哪张卡以及 `transformers/torch` 的推理设备，由 vendored MinerU、模型配置和运行环境决定。`EmbeddedMinerU` 当前只接受 `vlm-engine` parse backend；schema 中声明的其他 backend 没有在生产组合根接线。

## 7. 模块依赖关系

### 7.1 主要方向

```text
__main__
  -> config
  -> main

main / lifespan
  -> Database / Files / Cache / TaskManager
  -> DocumentService / ParseService / TranslationService
  -> QAService / ExportService / SourceEditService

DocumentService
  -> Database / DocumentFiles / Cache / DocumentIR / filetypes

ParseService
  -> DocumentService / Database / Files / TaskManager
  -> PdfPreflight / EmbeddedMinerU / ResultValidator
  -> MinerUAdapter / LocalStorage / translation_units

TranslationService
  -> DocumentService / Database / TaskManager / LLMClient / DocumentIR

QAService
  -> DocumentService / Database / TaskManager / TranslationService / rendering

ExportService
  -> DocumentService / Database / TaskManager / translation_units / rendering

MinerUResultValidator
  -> LocalStorage / Archive / PDF registration / MinerUAdapter / DocumentIR

MinerUAdapter
  -> DocumentIR / CoordinateMapper / ImageReferences / table normalizer
```

### 7.2 依赖层次

1. **入口/组合层**：`__main__.py`、`main.py`。
2. **应用服务层**：documents、parser、translation、QA、export、source edits、maintenance。
3. **任务与执行基础设施**：tasks、execution、database、files、storage、cache、paths。
4. **领域/协议层**：DocumentIR、job/document/preview/MinerU schemas、errors。
5. **外部适配层**：FastAPI/Uvicorn、SQLite、HTTPX/LLM、PDFium/pypdf、Pillow、LibreOffice、vendored MinerU。
6. **浏览器层**：template、CSS、app.js、PDF.js、KaTeX。

### 7.3 已知耦合

- 静态顶层 import 未发现直接模块循环，但 `translation.py` 静态依赖 `DocumentService`，`documents/service.py` 又在 `_view()` 内部使用 `translation_units`，形成运行时逻辑环。
- 多个 service 直接注入具体 `Database` 并执行 SQL，尚无 repository interface。
- `DocumentIR` 直接使用 `PortablePath`，并携带 export path、preview asset 和解析引擎 provenance，领域模型知道部分文件布局和引擎事实。
- `MinerUClient` 是独立 HTTP adapter，但生产 `ParseService` 固定使用 `EmbeddedMinerU`。
- vendor MinerU 通过修改 `sys.path` 动态加载，安装/源码布局依赖隐式存在。
- 浏览器端依赖手写的 `TaskView.scope`、`result_ref` 和请求字段，前后端没有生成式客户端契约。

## 8. 当前架构明显问题

### 8.1 高优先级

1. **文件发布与数据库提交不是同一个可回滚事务。** parser 先移动正式 parse 目录，再写 `parse_results` 和 `active_parse_id`。中途失败可能留下没有数据库指针的发布目录。
2. **`ParseService` 变化轴过多。** Office/Pillow、PDF 预检、MinerU、CAS、IR、SQLite 发布和版本清理均在同一编排模块。
3. **`DocumentService` 同时做 repository、文件网关、IR loader 和 translation projection。** 简单文件读取会进入翻译统计和 metadata 回写路径，且与 translation 形成逻辑环。
4. **任务状态是进程内瞬态。** 重启后 queued/running/failed/cancelled 任务不可从 SQLite 恢复；任务 API 只能查询当前进程保留期内的记录。
5. **MinerU backend 声明大于实际能力。** `MinerUBackend` 枚举包含多个 backend，但 `EmbeddedMinerU` 当前只支持 `vlm-engine`，HTTP client 未接入生产解析。

### 8.2 中优先级

6. **数据库 SQL 横穿多个应用服务。** schema、事务和业务持久化规则分散，替换 SQLite 或集中验证迁移的成本高。
7. **source edit 传播边界不统一。** reader/Markdown 使用 effective IR，而翻译、QA、导出主要以 base IR 为输入；人工原文修订可能不会贯穿所有结果。
8. **前端 `app.js` 过大。** UI、协议、状态机、IR 语义、翻译 revision、QA context、任务重试和 SSE 均集中在一个文件。
9. **前端存在手写契约漂移风险（部分待运行确认）。** 设置面板中部分控件没有进入 `ParseRequest`；raw view 没有对应模板 tab，缓存 key 也不一致；多 editable-node source edit 会发送后端拒绝的空文本；`app.js:2503-2506` 直接使用 `#delete-button`，而当前 `index.html` 未声明该节点，可能在初始化阶段中断后续交互。
10. **发布后 asset checksum 闭环不足。** 生成阶段校验 CAS/ZIP checksum，但普通文件读取主要校验路径和存在性，不重新验证 IR 中的 asset SHA。
11. **DocumentIR 混合领域与基础设施 provenance。** `PortablePath`、export path、preview identity 和 MinerU/adapter version 使领域快照与文件布局、解析引擎耦合。
12. **迁移 locality 不完整。** `migrations/versions/` 为空，初始 schema 和历史迁移逻辑集中在 `database.py`，不利于独立审计和版本演进。

## 9. 后续阅读顺序

1. 先读 [`main.py`](../src/easylearn/main.py) 和 [`tasks.py`](../src/easylearn/tasks.py)，理解装配和任务状态。
2. 再读 [`documents/service.py`](../src/easylearn/documents/service.py)、[`database.py`](../src/easylearn/database.py)、[`files.py`](../src/easylearn/files.py)，确认持久化边界。
3. 解析问题读 [`parser.py`](../src/easylearn/parser.py)、[`previews/pdf.py`](../src/easylearn/previews/pdf.py)、[`mineru/result.py`](../src/easylearn/mineru/result.py)、[`mineru/adapter.py`](../src/easylearn/mineru/adapter.py)。
4. 阅读功能链时按 [`translation.py`](../src/easylearn/translation.py)、[`qa.py`](../src/easylearn/qa.py)、[`exports.py`](../src/easylearn/exports.py)、[`source_edits.py`](../src/easylearn/source_edits.py) 顺序进入。
5. 最后对照 [`templates/index.html`](../src/easylearn/templates/index.html)、[`static/app.js`](../src/easylearn/static/app.js) 和 `tests/v3/` / `tests/browser/` 验证浏览器契约。
