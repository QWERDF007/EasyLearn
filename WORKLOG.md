# WORKLOG

> 本文件是项目唯一的任务账本。真实日志按最新在前追加在固定示例条目之后，并固定位于其他真实日志之上；`⏳ 待你裁决` 始终固定在顶部。

## ⏳ 待你裁决

- 无。


### 2026-09-07 — 工作台空态与模型选择收口

**目标**
- 将无文档工作台改为上传/拖拽空态，移除前端 MinerU 模型选择入口，并为后续三栏交互验收保留稳定 DOM 标识。

**当前状态**
- 已完成：无文档时结果栏隐藏、阅读区显示“点击上传或者拖入文件开始解析”上传区域；文件选择与拖拽共用同一上传链路。
- 已完成：前端移除模型状态、模型请求、模型下拉及模型变更监听；解析请求不再从页面读取模型 ID，后端继续从配置快照确定模型。
- 已完成：文档条目增加文档 ID 数据标识，已有侧栏收藏/删除逻辑可被浏览器验收稳定定位。

**验证证据**
- 红灯：`learn python -m pytest tests\\v3\\test_app.py::test_page_exposes_full_result_and_question_history_controls -q ...` → 缺少 `empty-upload-state`。
- 绿灯：同一测试 → `1 passed`。
- `learn python -m pytest tests\\browser\\test_upload.py::test_empty_workspace_exposes_upload_dropzone_without_model_selector -q ...` → `1 passed in 34.28s`；真实 Selenium + ChromeDriver 验证空态可见且不存在 `model-select`。
- `learn python -m pip install -e ".[browser]"` → 在指定环境安装项目声明的 Selenium 4.48.0 及其依赖。
- `node --check src\\easylearn\\static\\app.js`、`git diff --check` → 通过（Git 仅提示 Windows 行尾转换）。

**下一步**
- 增加右侧设置/重新解析/下载按钮及其真实行为，再用 Selenium 验证侧栏收藏、删除确认、PDF 控件和 Ctrl+滚轮缩放。


### 2026-09-06 — 解析反馈与工作台顶部重规划诊断

**目标**
- 为解析中的 Web 页面补充可见进度，减少无意义的任务轮询日志，并按参考页面重规划顶部操作区。

**当前状态**
- 已定位：解析任务在 `parser.py` 中进入内置 MinerU 前只发布 `0.15 / Preview is ready`；内置 MinerU 的 analyzer 在完整 VLM 分析返回前没有进度回调，因此截图中的 `15%` 是真实的阶段值，不是当前推理页数。
- 已定位：`app.js` 的 `waitTask` 每 500ms 请求一次 `/api/tasks/{task_id}`，直到任务终态；日志中的 `200 OK` 是前端轮询成功，不是重复启动解析。当前轮询状态不是集中共享的长期结构，页面任务区也没有主内容区进度视图。
- 已定位：模板把版本、自动翻译、解析、翻译、问答、导出、收藏、删除全部放进同一个 `topbar-actions`，阅读工具栏仍使用多组文字按钮。
- 本轮尚未修改代码，等待进度展示粒度确认。

**验证证据**
- `src/easylearn/parser.py`：`0.15` 后调用 `_run_mineru`，完成后才进入 `0.55` 验证阶段。
- `src/easylearn/mineru/embedded.py`：`_run_analyzer` 等待 `aio_doc_analyze` 完整返回，未向 `TaskContext` 转发 MinerU 内部 `tqdm`。
- `src/easylearn/static/app.js`：`waitTask` 固定 `500ms` 延迟；`renderTasks` 只更新左侧任务列表；模板和 CSS 的顶部控件未分组。

**下一步**
- 用户确认推荐的阶段/不确定进度方案后，先补红灯测试，再实现任务状态共享轮询、主区进度条与取消入口、阶段文案和顶部图标化分组，最后用 Selenium 与真实论文任务验收。




### 2026-09-06 — 内置 MinerU 论文验收与表格公式归一化

**目标**
- 修复真实论文解析在 `15%`/MinerU 启动错误之后的完整链路，并使表格公式标记可发布；确认 MinerU 进程内运行，日志按本地日期写入 `logs/YYYY-MM-DD.log`。

**当前状态**
- 已通过 `src/easylearn/mineru/embedded.py` 在 EasyLearn 进程内加载仓库内 MinerU 3.4.5 与 `D:\Models\MinerU2.5-Pro-2605-1.2B`；解析路径不再调用 MinerU CLI 或 MinerU HTTP 服务。
- 已修复 MinerU 表格单元 `<eq>...</eq>` 未知标记：转换为 `MathNode`，嵌套公式标记仍按协议错误拒绝。
- `D:\Papers\2403.18819v1.pdf` 已通过真实 HTTP 上传、GPU 推理、结果校验、DocumentIR 发布和 Markdown 读取；正式服务当前运行在 `127.0.0.1:8765`。
- 日志当前写入 `logs\2026-09-06.log`，按本地日期切换，不按大小轮转。

**验证证据**
- 红灯：`pytest tests/mineru/test_adapter.py -q -k table_formula_markup` → `1 failed`，复现 `Unsupported table cell markup`。
- 绿灯：同一命令 → `1 passed`；`pytest tests/mineru/test_adapter.py tests/mineru/test_result.py tests/mineru/test_embedded.py -q` → `127 passed, 3 skipped`。
- 真实原始 MinerU 结果归一化 → `27` 页、`53` 个归档成员、`791667` 字节 DocumentIR；`3` 张表、`768` 个表格单元、`63` 个数学节点成功生成。
- 真实服务任务 `8ef74011-7e13-4d43-a744-d920c5f7de3c` → `succeeded / 1.0 / Completed`；解析结果 `cd45f96a-b721-4910-a350-b1ff1b0038fa` 与 Markdown 接口均 HTTP `200`，Markdown `80540` 字符且包含表格。
- `logs\2026-09-06.log` 已记录 Uvicorn 请求和内置 MinerU GPU 推理日志；旧的 `MinerU command could not be started` 仅存在于历史日志，不再是当前执行路径。

**下一步**
- 无必需后续；用户提供 Pinaic API Key 和模型名后，另行完成 LLM 外部接口联调。


### 2026-09-06 — 修复浏览器上传无响应并补充 Selenium 验收

**目标**
- 定位“上传图片后界面没有变化”，修复真实浏览器阻止上传监听器的问题，并补充 Python Selenium + ChromeDriver 测试。

**当前状态**
- 已修复 Windows 静态资源 MIME：`.js` 与 `.mjs` 统一返回 `text/javascript`；上传事件可执行。
- 已新增可选 `browser` 测试依赖、独立临时服务 fixture 和 3 个浏览器场景；默认测试集不启动浏览器，设置 `EASYLEARN_RUN_BROWSER_TESTS=1` 才运行。
- 当前正式服务已停止，8765 无残留 EasyLearn 监听；本轮测试临时目录已清理。

**验证证据**
- Selenium + ChromeDriver 真实浏览器：`EASYLEARN_RUN_BROWSER_TESTS=1 ... -m pytest tests\\browser -q -p no:cacheprovider --basetemp .tmp-browser-final` → `3 passed in 14.72s`；验证模块加载、图片 `POST /api/documents` 返回 201、侧栏展示及连续上传两张图片。
- MIME 回归：`... -m pytest tests\\v3\\test_app.py -q -p no:cacheprovider --tb=short --basetemp .tmp-final-app` → `16 passed`，覆盖 `app.js` 和 `pdf.min.mjs`。
- `... -m ruff check src tests\\v3\\test_app.py tests\\browser\\test_upload.py` → `All checks passed`；`node --check` 两个前端脚本通过；`git diff --check` 无差异空白错误。
- 红灯证据：Chrome Console 原报 `app.js`/`pdf.min.mjs` MIME 为 `text/plain`，无 `POST /api/documents`；修复后真实浏览器出现 `POST /api/documents` → `201 Created`。

**下一步**
- 用户再次启动时从仓库根目录执行 `E:\\Softwares\\Anaconda3\\envs\\learn\\python.exe -m easylearn`；若继续全量论文验收，重新提交被中断的 27 页任务。


---

### 2026-09-06 — 本地权重与论文真实验证

**目标**
- 拉取 `origin/dev` 最新代码，使用 `learn` 环境与 `D:\Models` 启动 EasyLearn，并用 `D:\Papers\2403.18819v1.pdf` 做真实验证。

**当前状态**
- 已快进到 `d3a823a`；按 v3 本机入口使用 SQLite、单进程 FastAPI 和仓库内 MinerU 3.4.5 源码，未安装 MinerU 包。
- 已安装项目声明依赖，服务曾在 `127.0.0.1:8765` 就绪；`D:\Models\MinerU2.5-Pro-2605-1.2B` 在 RTX 4090 上成功加载。
- 指定论文已完成真实上传、PDF 预检和单页真实 MinerU 推理；整篇 27 页任务已进入真实推理但在发布前被用户中断，不能记为整篇解析通过。相关临时进程已清理，当前服务未运行。
- Pinaic 仅完成官方资料调研与索引，运行配置尚未切换；API Key 和文本模型名待补。

**验证证据**
- `git pull --ff-only` → `7d36a55..d3a823a` 快进；`learn python -m pip install -e ".[dev]"` → 成功安装 `aiosqlite` 等依赖。
- `D:\Papers\2403.18819v1.pdf` → 27 页、未加密、2,660,025 字节；单页 MinerU 输出 middle/content-list/Markdown 等产物，模型加载到 `cuda:0`。
- EasyLearn `/api/health` → `200`、`status=ready`、CLI MinerU `configured=true`；整篇任务 `b9a944ba-04d9-45c0-b894-000f75a291de` → `running / 0.15 / Preview is ready` 后被中断，未得到 parse result。
- `nvidia-smi` → RTX 4090 显存约 13 GiB 被 MinerU 计算进程占用；没有留下 EasyLearn/MinerU 服务进程。

**下一步**
- 提供 Pinaic API Key 和实际文本模型名后，将 `[llm]` 配置为 `https://api.pinaic.com/v1`、`api_key_env` 和 `local_only=false`，再做真实 `/chat/completions` 联调。
- 如继续论文全量验收，重新启动服务并重新提交 27 页任务；不能复用被中断的内存任务。

### 2026-09-06 — Pinaic OpenAI 兼容 API 官方资料
### 2026-09-06 — Pinaic OpenAI 兼容 API 官方资料

**目标**
- 按 research skill 核查 Pinaic 官方首页指南第 5 节，保存 OpenAI 兼容 API 的外部事实，不修改代码。

**当前状态**
- 已完成 [Pinaic OpenAI 兼容 API 外部事实](docs/research/pinaic-openai-compatible-api.md)，并加入 [文档与实现索引](docs/README.md)。
- 已明确记录 Base URL、`/v1/usage`、Bearer 认证、禁止 query 传密钥和密钥注入注意事项。
- 官方页面未明确文本生成请求路径、文本 `model` 约束、SDK 包名或方法；文档保留为未确认，不以项目现有实现补齐。
- 未修改代码、配置或实际密钥。

**验证证据**
- 只读请求 `https://app.pinaic.com/docs/home-guide` → HTTP 200；页面官方正文接口 `https://app.pinaic.com/api/v1/docs/home-guide?limit=8` → `code=0`、发布版本 15。
- 从第 5 节原文核对 `https://api.pinaic.com/v1`、`Authorization: Bearer ...`、`/v1/usage` 及 query `key`/`api_key` 拒绝说明；`git diff --check` → 无空白错误。

**下一步**
- 等 API Key 提供后，再按该外部事实配置并进行真实接口联调；在获得 Pinaic 官方文本 API 路径和模型契约前，不假设 `chat/completions` 或 `responses`。

### 2026-09-06 — 轻量版最终校验
## 日志

<!--
建议格式：

### YYYY-MM-DD — 简短任务名

**目标**
- ...

**当前状态**
- 已完成：...
- 未完成：...

**验证证据**
- `command ...` → 关键结果
- 未验证项请明确写“未验证”

**下一步**
- ...
-->


## [示例] 修复订单导出超时

**总目标**：后台订单导出在 1 万行数据量下 30 秒内完成，不再 504。

**状态**：✅ 完成

**干到哪了**：
- [x] 定位根因：导出走了逐行 N+1 查询 —— 证据：慢日志中同款 SELECT 出现 10,412 次
- [x] 改为批量查询 + 流式写出 —— 证据：`export_test.go` 新增用例通过；本地 1 万行实测 4.2s
- [x] 隔离实例真实触发目标路径 —— 证据：staging 实测导出 12,000 行 5.1s，HTTP 200
- [x] 开关两态验证：`export_v2=off` 时回退旧路径正常

**边界**：不动导出的字段结构；不顺手重构 handler。

---

### 2026-09-07 — 内置 MinerU 与工作台交互阶段提交

**目标**
- 将已完成的内置 MinerU、源文本修订、解析反馈、顶部布局、PDF 控件和 Selenium 测试按阶段提交，并建立当前状态与后续计划入口。

**当前状态**
- 后端阶段已提交为 `b71d15e`：进程内 MinerU/GPU 运行时、模型目录、日志、表格公式归一化和源文本 revision overlay。
- Web 阶段已完成：解析阶段/耗时/取消反馈、共享任务轮询、图标化顶部、PDF 底部工具栏、`Ctrl + 滚轮` 缩放、块联动、收藏/删除确认和源文本编辑界面。
- [当前状态、目标与计划](docs/CURRENT.md) 已建立；显式模型候选配置、`D:\tmp` 临时目录、扩展 Selenium 用例和 Pinaic 联调列为后续阶段，本轮没有继续实现。

**验证证据**
- `pytest tests\v3\test_model_catalog.py tests\v3\test_source_edits.py ... tests\v3\test_parser.py` → `15 passed`。
- 固定 ChromeDriver 152.0.7977.82 运行 `pytest tests\browser` → `3 passed in 18.79s`。
- `ruff check src tests\browser tests\v3\test_app.py`、`node --check` 两个前端模块和 `git diff --check` 均通过；UI/MIME 定向测试 `3 passed`。

**下一步**
- 按 [CURRENT](docs/CURRENT.md) 先实现 config 显式模型候选和 `D:\tmp`，再补齐新增工作台交互的 Selenium 验收；用户提供凭据和文本模型名后联调 Pinaic。

---

### 2026-09-06 — 轻量版最终校验

**目标**
- 在 `D:\Software\anaconda3\envs\learn` 环境完成 EasyLearn v3 轻量版的最终可运行性校验，保持不安装 MinerU、使用已有 MinerU CLI/API 的边界。

**当前状态**
- 单进程 FastAPI、SQLite、本地文件、进程内任务队列、PDF/图片/Office 预览、MinerU 结果校验与 DocumentIR、翻译/人工修订、导出和问答链路已落在当前 v3 入口；索引见 [文档与实现索引](docs/README.md)。
- 数据库版本已升至 2，移除文档原件路径的全局唯一约束，并提供 v1→v2 迁移，使不同文档可以使用相同扩展名上传；新旧表结构共用列定义。
- 首次人工编辑会把编辑前有效文本写入修订历史；QA 请求会裁剪并拒绝空白问题；关键词候选保留相关性排序，同时维持必需块和关系块优先。
- 已用本地合成 MinerU HTTP peer 完成一次真实 Uvicorn 上传、解析、结果发布和任务查询；未安装或调用真实 MinerU/LLM。`data/` 用户数据目录未触碰。
- 浏览器控制端返回 `No browser is available`，因此浏览器截图和交互验收未完成；静态资源与 JavaScript 语法已校验。

**验证证据**
- `D:\Software\anaconda3\envs\learn\python.exe -m pytest tests\v3 tests\document_ir tests\mineru -q -p no:cacheprovider --tb=short --basetemp .tmp-final-pytest`（提权执行）→ `368 passed, 5 skipped`，66.77 秒；同扩展名上传、带子表的 v1→v2 迁移、首次人工修订历史、空白 QA 问题和关键词候选排序均通过。
- `D:\Software\anaconda3\envs\learn\python.exe -m pytest tests\v3\test_database.py::test_database_migration_preserves_children_and_foreign_key_cascade -q -p no:cacheprovider --tb=short --basetemp .tmp-fk-migration`（提权执行）→ `1 passed`。
- `D:\Software\anaconda3\envs\learn\python.exe -m ruff check src tests migrations`（提权执行）→ `All checks passed`；`D:\Software\anaconda3\envs\learn\python.exe -m mypy src\easylearn`（提权执行）→ 41 个源文件无问题；`node --check src\easylearn\static\app.js` → 通过；`git diff --check` → 无差异空白错误。
- 临时合成 MinerU `127.0.0.1:8767` 与 EasyLearn `127.0.0.1:8766` 均已关闭，8765/8766/8767 端口均空闲；`.tmp-pdfjs`、`.tmp-pdfjs-extract`、`.tmp-red-db`、`.tmp-green-db`、`.tmp-red-multiple-docs`、`.tmp-fk-migration`、`.tmp-first-edit`、`.tmp-blank-qa`、`.tmp-qa-ranking` 和本轮 pytest 临时目录已清理。

**下一步**
- 按 [本机启动](docs/native.md) 使用正式配置启动；将 `[mineru]` 指向已有 MinerU CLI/API 后，再用真实服务做推理质量与完整产物验收。
- 若需要完成界面验收，提供可用浏览器实例后检查三栏联动、PDF 预览、表格/公式/图片/链接及源码视图。

### 2026-09-05 — 解析执行恢复与产物发布

**目标**
- 延续完整 A–G，将固定配置/输入接到真实 HTTP、下载、归一化及带租约的事务发布，补齐提交中断与取消恢复边界。

**当前状态**
- 上批已提交 `efe91d0`。[ParseWorker](src/easylearn/parses/worker.py) 已接通提交、轮询、流式下载、真实子进程验证/归一化和 fenced 发布。`open(jobs, storage, settings)` 管理复用的认证 HTTP 连接生命周期；`execute(job_id, generation=...)` 执行固定任务。服务/profile/本机模型登记改变时拒绝重定向旧运行，操作型限额仍取当前统一配置。
- [ParseCheckpoint](src/easylearn/parses/schema.py) 保存提交 ID、配置/输入摘要、代次、时间和原始回执 CAS；调用 POST 前提交 SUBMITTING。无回执中断或不确定响应恢复为 SUBMIT_UNKNOWN，通用 retry 拒绝盲目重发；明确 429 拒绝允许新代次新请求并保留旧回执。已接受的任务重试复用 task_id；下载已完成则复用归档。轮询间隔/总预算进入两种配置示例，预算以该次提交时间为起点。
- [迁移 0005](migrations/versions/0005_parse_results.py) 发布 IR/归档/原始产物与固定坐标证据；ParseArtifact 提供运行内逻辑 ID，[MinerUArchiveMember.asset_id](src/easylearn/mineru/schema.py) 是 UUID5 算法唯一源，底层 Asset 继续按 SHA 去重。图片别名仅可从所属文档的已发布运行下载。当前文档指针只向更新创建的成功运行推进，旧运行迟到/新运行失败或取消不覆盖较新的可用结果。
- [资产登记](src/easylearn/assets.py) 已从 uploads 移到公共模块，不保留旧导入别名；上传与解析共用按摘要排序的批量登记入口，避免逐产物 N+1。共享 [run_blocking](src/easylearn/execution.py) 在取消时先等待线程 I/O 完成再释放文件句柄；[write_stream](src/easylearn/storage.py) 以临时文件有界接收，复用唯一 CAS 写入路径。JobService.handle_error 统一预览和解析的失败/取消竞争处理。
- 尚未实现队列消费者/常驻 Outbox 投递与 Reconciler；Web 只受理，当前执行器尚不会自动运行。SUBMIT_UNKNOWN 的显式人工决策/受限再提交策略、清理、SVG、完整上游语义、Markdown 投影、结构查询、三栏 UI、翻译/导出/AI 及真实推理验收仍未完成。未安装或启动 MinerU，没有新截图，没有修改用户论文。

**验证证据**
- 不确定提交持久化、IR 发布、图片别名下载、取消上游提示、认证连接生命周期和拒绝回执保全均观察到 red→green。覆盖取消等待真实 OS fsync 完成、提交中断租约接管不重发、查询/下载/归一化/轮询超时后复用原任务、迟到旧版本、无效归档、新运行下载后取消、跨版本相同图片内容及配置重定向拒绝。
- `EASYLEARN_ACCEPTANCE_PDF=D:\Papers\2403.18819v1.pdf`，learn Python `-m pytest tests/api/test_parses.py tests/api/test_previews.py tests/api/test_uploads.py tests/api/test_job_leases.py tests/mineru/test_result.py tests/mineru/test_client.py tests/config -q -p no:cacheprovider --tb=short` → 200 passed，72.50s；随后新增 I/O 取消与配置保护 3 项通过。未运行全量测试。
- 公共资产模块整理后 `-m pytest tests/api/test_parses.py tests/api/test_uploads.py tests/api/test_live_http.py tests/mineru/test_live_http.py -q -s -p no:cacheprovider --tb=short` → 36 passed，51.17s。TOML/YAML 使用真实 Uvicorn Web、带 Bearer 验证的合成 HTTP peer、真实 PostgreSQL 和归一化子进程；27 页论文各发布 27 个合成文本块，首块 PDF bbox=(10,752,50,772)，端到端分别 1.80s/1.87s，原件与预览摘要不变。这不是实际 MinerU 输出或模型质量验收。
- 在单独随机测试库执行 Alembic upgrade head、check、downgrade 0004、upgrade head、check → 两次均无 ORM 差异，升降级通过，测试库已回收；没有修改已有业务库。Ruff 与格式检查通过，mypy 50 源文件通过。

**下一步**
- 本批可独立中文提交；保留用户未跟踪旧版资料目录。
- 接通原生 Redis/Dramatiq、共享异步运行时、Outbox 投递与常驻 Reconciler，直接复用 PreviewService / ParseWorker / JobService / Outbox；不得为队列另写提交或状态逻辑。用户约束是不使用 Docker、不直接部署 MinerU，learn 环境保持不变。
- 再推进页面/块/Markdown 读取、三栏交互和完整 A–G；明确人工处理未知提交的可见操作，不能靠创建新任务静默消耗重复算力。
- 资产清理必须覆盖 ParseRun/ParseArtifact、Job checkpoint 中的原始回执和未引用 CAS；取消路径只回收自身临时文件，不删共享内容。引用表/证据不代表全部上游语义已校验，仍需固定版本实际服务捕获。

### 2026-09-05 — 解析受理与固定配置快照

**目标**
- 延续完整 A–G，恢复本机测试并将已 READY 的预览接入持久化 ParseRun；从同一 TOML/YAML 配置冻结模型、路径及解析参数。

**当前状态**
- 上批已提交 `041fb75`。[ParseService](src/easylearn/parses/service.py) 已实现解析受理、版本列表及固定 PDF 下载；[解析类型](src/easylearn/parses/schema.py) 是请求与快照定义源。受理与 Job/Outbox/幂等回执同事务，只有所属文档的 READY 预览可受理；配置快照不包含密钥，页数由真实预检结果提供。配置改变不重写旧运行或幂等回执，运行状态仍由 Job 派生。
- [迁移 0004](migrations/versions/0004_parse_runs.py) 与 ORM 用复合外键绑定预览所属文档/固定资产和任务所属文档/运行身份；迁移已在隔离数据库执行，未迁移已有业务库。不同文档可共享 PDF 内容，但不能共享解析任务状态。
- 尚未实现 ParseService 执行、SUBMITTING/SUBMIT_UNKNOWN 与 fenced 结果发布；本批不会调用 MinerU，不将受理的 QUEUED 视为解析完成。SVG、完整上游语义、队列、UI、翻译、导出和 AI 仍未完成；没有新增截图、没有修改论文或安装/启动 MinerU。

**验证证据**
- 解析受理首条测试由缺失路由 404 red 到 green；固定解析版本 PDF 的 Range/HEAD/ETag 接口独立 red→green。额外覆盖未就绪/不存在/跨文档预览、未配置服务、非法参数、并发同键只投递一次、配置变更与幂等冲突、共享 PDF 的运行隔离和取消重试配置保持。
- 设置 `EASYLEARN_ACCEPTANCE_PDF=D:\Papers\2403.18819v1.pdf`，learn Python `-m pytest tests/api/test_parses.py tests/api/test_documents.py tests/api/test_job_delivery.py tests/api/test_live_http.py tests/config -q -s -p no:cacheprovider --tb=short` → 75 passed（25.24s）；未运行全量测试。真实 PostgreSQL 与真实 Uvicorn 子进程，分别读取完整 TOML/YAML；27 页 PDF 预览、解析受理、下载分别 0.61s/0.62s，冻结模型目录与页数正确，原件和固定下载 SHA 一致。无真实 MinerU 或 LLM 推理证据。
- Ruff 全源码/测试/迁移通过，mypy 48 源文件通过；相关格式与 `git diff --check` 通过。
- PostgreSQL 恢复时首次启动遗漏原端口选项，日志证实监听 5432；随即用 `pg_ctl restart -D D:\Project\EasyLearn\.runtime\postgres-data -l D:\Project\EasyLearn\.runtime\postgres-server.log -o '-h 127.0.0.1 -p 55432' -w -t 20` 恢复原端口。最终 PID 29908、pg_isready 55432 accepting；未重新初始化数据。后续启动必须保留 `-o` 端口参数，日志仍放 data 目录外。

**下一步**
- 本批可独立中文提交，保留用户未跟踪旧版资料目录，不混入提交。
- 直接从 ParseRun 固定配置与输入继续执行链路：调用前持久化 request_id/参数摘要/SUBMITTING；取得回执前中断必须进入 SUBMIT_UNKNOWN，不盲目重发。已有 JobService.reconcile/retry 保留 checkpoint，后续执行器需要据此区分恢复与新提交。客户凭据取运行时配置，但不得将旧运行悄悄切换到不同服务/profile。
- 原始归档下载后复用 MinerUResultValidator.normalize 和所有 Settings 验证限额，JobService.publication 做 fenced 发布。注意 Asset.sha256 全局唯一，而 normalize 目前按 UUID5(parse_run_id, member.path) 生成逻辑图片 ID；先统一物理对象/逻辑资产引用模型，不能插入重复 SHA 或随意改写已冻结 IR 身份。
- 完整目标不变；真实 MinerU/LLM 地址缺失不阻塞其余可实现代码。不要重复核查上一条中已有的 PDF 坐标证据。

### 2026-09-05 — 产物归一化与预览坐标登记

**目标**
- 延续完整 A–G，将原始产物验证、固定预览对应证据和 DocumentIR 归一化接到同一个受控子进程。

**当前状态**
- 上批已提交 `265006f`。当前未提交 [结果验证器 normalize](src/easylearn/mineru/result.py) 增加 ParseSource/NormalizedEvidence，复用 validate_result 和 Adapter，稳定 UUID5 图片身份，DocumentIR 存为 CAS 对象，尚不发布数据库 READY。
- [PDF 登记](src/easylearn/mineru/registration.py)：固定 preview SHA/大小、页数及几何核对；字节不同时使用相同配置逐页渲染、要求像素摘要完全一致，并保存方法/分辨率/摘要证据。这是指定渲染分辨率的页面等价证据，不是两个 PDF 完整语义相同的证明。
- 坐标复用真实 PDFium 的位置转换：VLM/hybrid 使用 int(page.get_size()) 归一化坐标，pipeline 按固定上游 200dpi / 3500 上限及 ceil 渲染尺寸回推。PDFium 浮点逆矩阵端点按页面尺度容差匹配唯一 CropBox 顶点，锚点取 PDF 元数据精确值，不将任意内容 bbox 四舍五入或裁剪。此为固定本地上游源码推导并验证的合同，仍需真实服务捕获核验服务自身环境。
- 未完成：原始产物剩余语义/复杂上游变体、SVG、ParseService/队列/UI/翻译/导出/AI 与真实推理验收。没有安装或启动 MinerU，也没有改变用户论文。

**验证证据**
- 首条归档→IR、上游重写 metadata PDF、pipeline/hybrid 小数页坐标分别 red→green；4 种真实旋转/非零 CropBox 与错误同尺寸页面内容已通过。版本、middle 页尺寸、缺失图片和表格限额传播均有失败用例。
- 大页渲染上限用例触发 red，实测 PdfPosConv 在 2000×3000 页/2334×3500 frame 返回左上 y=3000.000244140625、左下 y=0.000244140625；据此把顶点判定从固定绝对容差改为页面尺度的浮点精度预算，仍必须唯一匹配已知顶点。全部 3 后端的小数/大页合同 6 项通过，未放宽最终内容 bbox。
- 设置 `EASYLEARN_ACCEPTANCE_PDF=D:\Papers\2403.18819v1.pdf`，learn Python `-m pytest tests/mineru/test_result.py tests/mineru/test_adapter.py tests/document_ir -q -p no:cacheprovider --tb=short` → 181 passed（Result 50 / Adapter 66 / IR 65），25.82s。用户 27 页论文实际生成并读取 DocumentIR，27 个合成块各自绑定正确页，首块 PDF bbox=(10,752,50,772)，原件 SHA 不变。middle 文本仍为合成，但 page_size 从真实文件获得，不再用未经匹配的 600×800 作为归一化证据；不代表真实推理质量验收。
- Ruff 全源码/测试/迁移通过，mypy 44 源文件通过；相关格式化与 `git diff --check` 通过。未运行全量测试、未修改数据库或 Python 环境、未新增界面截图。

**下一步**
- 本批已审查并通过定向验证，可独立中文提交；保留用户未跟踪旧版资料。
- 下一批直接继续 ParseService 事前 SUBMITTING、SUBMIT_UNKNOWN 恢复与 fenced 发布，使用 MinerUResultValidator.normalize(archive, options, source) 获得 CAS IR 和原始证据。Settings 的 image/preview/mineru archive/table limits 与 validation_timeout 必须显式注入；页面对应证据不是 READY 状态。
- 继续 SVG、Markdown UTF-8/原始产物完整语义、复杂上游变体及服务依赖契约验收；完整 A–G 目标不变，真实服务地址缺失不阻塞其余代码实现。
- 已核对 hybrid/cal_real_bbox 和 pipeline/__fix_axis，txt_spans_extract 填写文本但仍沿用原 span bbox，span_block_fix 用 spans 汇总 line bbox；不要重复调查这些事实。仍需真实服务捕获来完成固定版本模型契约验收。

### 2026-09-05 — 子进程结果验证链路

**目标**
- 继续完整 A–G，将归档、图片、原始 JSON 与 origin PDF 检查接到可取消的真实子进程，向 ParseService 提供未发布产物证据。

**当前状态**
- 上批已提交 `4d778ee`。本批 [结果验证器](src/easylearn/mineru/result.py) 已接通真实 ZIP/图片/PDF 检查与 CAS 资产登记；ResultEvidence 包含 manifest、原始文件存储引用与 origin 预检，不返回或发布 READY，不声称 origin 与 preview 对应。
- 四类原始 JSON 验证可解码、无重复键、有限数字及有效 Unicode；JSON 独立字节限额复用归档配置。这里只验证 JSON 语法，不取代 Adapter 的 middle 版本/块结构校验。Markdown UTF-8 与原始产物完整语义尚未纳入本验证器。
- PDF 与结果验证复用 [执行模块](src/easylearn/execution.py)。真实故障测试发现并修复 OS 启动尚未返回时取消会遗留子进程的问题；启动任务与回收均受 shield 保护，子进程停止后排空管道、再传播取消。timeout 覆盖启动与执行；无法取回 OS 句柄时仍须等待启动握手以完成回收，不能承诺硬实时结束。
- 结果配置使用 `mineru.validation_timeout_seconds`、`mineru.archive_limits.max_json_bytes` 与已有顶层 image_limits/preview_limits。类型和默认 timeout 为单一源，TOML/YAML 等价及覆盖已验证；生产 ParseService 尚未实现，调用方后续须显式注入这些配置。
- CAS 对象在数据库发布前无业务引用；失败/强制结束可能留下未引用对象或 staging，后续仍需实现引用感知清理，不在取消路径删除共享 CAS 内容。
- 未完成：SVG、可信坐标登记与综合归一化、ParseService、队列/UI/翻译/导出/AI 及真实推理验收。没有安装或运行 MinerU。

**验证证据**
- 首条真实子进程、JSON 歧义、页数不匹配、JSON 字节预算、启动阶段取消和文件 timeout 配置均先 red 后 green。外部 OS 启动处注入真实 Python 故障子进程；内部 ZIP/存储/Pillow/PDF 依赖未 mock。
- 设置 `EASYLEARN_ACCEPTANCE_PDF=D:\Papers\2403.18819v1.pdf`，learn Python `-m pytest tests/mineru/test_result.py tests/mineru/test_archive.py tests/mineru/test_adapter.py tests/config -q -p no:cacheprovider --tb=short` → 211 passed（Result 32 / Archive 57 / Adapter 66 / config 56），14.35s。真实 27 页论文在独立子进程内重复验证，证据与对象读取一致，原件 SHA 不变；ZIP/middle 为合成、600×800 middle 尺寸未经匹配，不代表推理/坐标验收。
- 同环境 `-m pytest tests/api/test_previews.py tests/api/test_live_http.py -q -s -p no:cacheprovider --tb=short` → 14 passed，20.01s；TOML/YAML 的真实 27 页 HTTP 预览/下载分别 0.54s / 0.68s。没有全量测试或新界面截图。
- Ruff 全源码/测试/迁移通过，mypy 43 源文件通过；`git diff --check` 通过。
- 旧 PostgreSQL PID 33064 已停止，pg_ctl 在原 `.runtime/postgres-data` 恢复实例，没有重新初始化；启动等待曾超时，但同一 PID 31936 后续完成自动恢复，未重复启动。最终 `Get-Process -Id 31936` 存活、pg_isready 55432 accepting connections，API 测试真实连接成功。此次日志在 data 内导致 fsync sharing violation 重试约 30s；以后启动日志应放数据目录外（如 `.runtime/postgres-server.log`）。

**下一步**
- 本批已审查并通过定向验证，中文独立提交；保留用户未跟踪旧版资料。
- 从 ResultEvidence 继续可信坐标登记与综合归一化，再实现 ParseService 的事前 SUBMITTING、SUBMIT_UNKNOWN 恢复及 fenced 发布。需核对固定预览与上游重写 origin 的页面对应关系；不能只比较尺寸，也不能强求 PDF 字节 SHA 相等。
- 已阅读上游：vlm/model_output_to_middle_json.py 与 pipeline/model_json_to_middle_json.py 记录 int(page.get_size())；VLM MagicModel 将归一化 bbox 乘截断尺寸，pipeline 路径还需按自身缩放合同核对。PDFium 的 PdfPosConv 可从实际页面坐标转换，不复制一套未经验证的旋转矩阵；具体本机源码在 learn 的 pypdfium2/_helpers/bitmap.py。SVG 仍需补齐，不能据本批位图测试缩小整体验收。

### 2026-09-05 — 恢复图片语义与资源边界测试

**目标**
- 延续完整 A–G，完成实际图片解码与资源限额，并复用统一文件配置。

**当前状态**
- 上批结构归一化已提交 `c6c3d83`。恢复的未提交图片草稿位于 [images.py](src/easylearn/images.py)、归档检查器/manifest 与对应测试。
- [图片检查](src/easylearn/images.py) 经真实解码登记 MIME/首帧尺寸/帧数及累计像素；拒绝后缀伪装、缺少 PNG IEND 和超出单帧/帧数/归档总预算。逐帧 seek/load，不提前遍历全部帧计数；不同尺寸 TIFF 按每帧实际大小计量，不以首帧尺寸相乘。Pillow 解压炸弹错误归类为 IMAGE_LIMIT，不改全局阈值。
- 共用 ImageLimits 已接入 [Settings](src/easylearn/config.py) 顶层 `image_limits`，TOML/YAML 示例等价，构造与嵌套环境覆盖已验证。Archive 显式接收该类型，下一步生产结果处理器须从配置注入；本轮没有虚构尚不存在的生产调用入口。
- 未完成：SVG（当前会被 Pillow 拒绝，不能据此将验收范围永久缩为位图）、受限子进程综合结果处理、origin PDF 几何证明、ParseService 与完整 A–G；实际 MinerU/LLM 服务尚未接入。未安装/启动 MinerU，未改 Python 环境、数据库或用户论文，未新增界面截图。

**验证证据**
- 上轮图片元数据/伪装/不完整图像 3 项通过；本轮 learn Python `-m pytest tests/mineru/test_archive.py -k bounds_decoded -q -p no:cacheprovider --tb=short` 重现 3 项 red（缺 ImageLimits），实现后 6 项图片定向测试 green。配置文件限额 2 项先 red（未知字段）后 green。
- 设置 `EASYLEARN_ACCEPTANCE_PDF=D:\Papers\2403.18819v1.pdf`，learn Python `-m pytest tests/mineru/test_archive.py tests/mineru/test_adapter.py tests/config -q -p no:cacheprovider --tb=short` → 177 passed（Archive 57 / Adapter 66 / config 54）。包含 8 种位图格式/后缀组合、不同帧尺寸与精确预算边界、真实 27 页论文只读资源组合；ZIP/middle 仍为合成，不是模型输出捕获。未运行全量测试。
- Ruff 全源码/测试/迁移与 mypy 41 源文件通过；相关源码/测试格式化通过。

**下一步**
- 本批图片校验与配置已通过定向验证，独立中文提交；保留用户未跟踪旧版资料。
- 继续 SVG 处理及受控子进程综合产物验证，复用 PDF 生命周期与存储，验证原始 JSON/origin PDF 并登记可信坐标，再接 ParseService（事前 SUBMITTING、SUBMIT_UNKNOWN 恢复、租约 fenced 发布）。不得把合成产物校验当成真实推理验收；真实服务地址仍未提供，但不阻塞其余实现。

### 2026-09-05 — MinerU 结构归一化内核

**目标**
- 继续完整 A–G，交付固定 middle 协议到 DocumentIR 的结构归一化内核及表格资源边界，不将其冒充真实推理/完整 ParseService。

**当前状态**
- [Adapter](src/easylearn/mineru/adapter.py) 已支持固定版本/backend/page 身份校验、标题与文本、公式、图片/图表及题注脚注、代码语言与缩进、VLM 嵌套列表、pipeline 起始行标记的 list/index、页脚注与 discarded 文本；正文/附属文本保留各自身份。图题在主体上方时保持上游阅读序，不一律排到主体后。
- [HTML 表格归一化](src/easylearn/mineru/tables.py) 使用 learn 中已有 BeautifulSoup 4.15.0（已声明依赖，未安装包）：保留行列、合并、表头、单元格 ID、公式/代码/链接/图片和换行；复用 DocumentIR 的 TableStructure 校验。只跟踪仍在跨行的占用区间，不重复扫描全部历史单元格。行/列/单元格/markup 限额统一在 [MinerUTableLimits](src/easylearn/mineru/schema.py)，可通过 `mineru.table_limits` 文件配置。
- 单元格仅引用所属表格与有证据的页面，不伪造单元格 bbox；合并后 HTML 缺少匹配 preproc 来源时明确 none + TABLE_SOURCE_UNRESOLVED。块层 cross_page 脚注也核对源行。无 HTML 但有已登记截图时标记 TABLE_STRUCTURE_UNAVAILABLE；HTML 畸形嵌套或当前原子行内类型无法表达的结构（如链接内图片）明确拒绝，不静默丢内容。
- [内部图片引用](src/easylearn/mineru/assets.py) 共用名称解析与已使用资产登记，拒绝外部/越界/缺失/非图片引用及同 ID 冲突；这是描述符引用检查，不是生产图片字节解码验证。BlockType 从 DocumentIR 原字段提取为唯一类型定义供 Adapter 复用，没有改变 IR 可选类型集合。
- 未完成：实际 ZIP → 原始 JSON/图片/上游 PDF 语义验证与可信几何登记、完整复杂 HTML/上游变体验收、ParseService/队列、真实 MinerU 与 LLM 联调、阅读器及 A–G 其余功能。不能把合成坐标登记当成实际上游转换契约；本轮未安装/启动 MinerU、未修改用户论文、未新增产品截图。

**验证证据**
- 表格结构/富文本、跨页证据、限额、资产身份冲突、代码/列表、discarded 内容、缺结构截图、阅读序及嵌套内容保全逐项先 red 后 green。审查时修正了叶块隐藏子内容、单元格段落拼词和上方图题顺序；已知无法表达的原子行内嵌套不再假成功。
- 设置 `EASYLEARN_ACCEPTANCE_PDF=D:\Papers\2403.18819v1.pdf`，learn Python `-m pytest tests/mineru/test_adapter.py tests/document_ir tests/config -q -p no:cacheprovider --tb=short` → 183 passed（Adapter 66、DocumentIR 65、配置 52）；未运行全量测试。
- 组合资源用例真实运行 PdfPreflight 子进程，读取 27 页论文，Pillow 编解码 40×20 PNG，生成并检查真实 ZIP，再归一化合成 middle。原件 SHA-256 `9674284d4722ff5596dec155495423b7709d2051fbe8476b09cb9f64f522d5d8` 前后一致。600×800 坐标是用例显式声明的合成坐标，映射到真实预览页；不是实际 MinerU 输出或推理质量验收。
- Ruff 全源码/测试/迁移与 mypy 40 源文件通过。配置示例 TOML/YAML 等价性通过；未修改数据库或 Python 环境。

**下一步**
- 将本结构内核独立中文提交，保留用户未跟踪旧版资料；此提交不宣称完整 Adapter/服务验收。
- 继续产物语义验证：使用归档 manifest 的固定路径、真实图片解码与独立 origin PDF 预检，核对 middle 页数/尺寸及 preview 几何；明确来源证据，不能把 origin 摘要与 preview 摘要强制相等。补齐剩余上游变体/复杂行内结构及失败边界后接入 ParseService。
- ParseService 继续事前 SUBMITTING 持久化、SUBMIT_UNKNOWN 恢复与租约 fenced 发布；随后原生队列与其余 A–G。真实 MinerU/Provider 服务地址仍未提供，不自行部署 MinerU。

### 2026-09-05 — 图像与公式 Adapter 草稿

**目标**
- 继续完整 MinerU Adapter，在已确认的 normalize 接口内保留结构、内部资产和定位证据。

**当前状态**
- 配置批次已独立中文提交：`1f38e16 支持模型路径与 LLM 接入的 TOML 和 YAML 配置`；模型目录、模型/Tokenizer 登记、MinerU、LLM/Embedding/视觉 profile 和路由均有 TOML/YAML 示例及定向验证，详情见下一条。本轮未安装 MinerU 或启动真实推理服务。
- [Adapter](src/easylearn/mineru/adapter.py) 未提交草稿在原文本/标题/公式基础上增加：公式截图、图片主体/图题/脚注、固定版本关系边与内部图片资产。NormalizationContext.assets 登记上游图片相对名称到 AssetDescriptor；原始 image_path 不变成外部 URL。嵌套遍历同时用于 preproc 行来源核对和规范输出，缺少叶块 lines 不再被当作空成功。
- 未完成：表格/单元格、列表、代码、discarded 内容、完整上游语义与资产碰撞验证、真实图片内容校验、PDF 几何登记、ParseService。草稿不作为完整 Adapter 交付，不含真实模型捕获或产品界面验收。
- 后续表格的已确认源码事实：`3rdparty/MinerU/mineru/utils/table_merge.py:perform_table_merge` 会直接修改首表 HTML、清空后表子块并标记 lines_deleted；表体没有 span.cross_page 来源登记，移入脚注则在块层标记 cross_page。不能把合并 HTML 或脚注自动投到首表页面。`vlm_magic_model.py` 的 image/table/chart/code 是带 body/caption/footnote 的 blocks；list 也通过 blocks 包含子项。`pdf_image_tools.py:cut_image` 返回平铺的哈希 JPG 文件名。

**验证证据**
- 公式资产引用、图片关系、缺失叶块 lines 均先 red 后 green；图片相对路径越界、URL 和未登记引用用例通过。
- learn Python `-m pytest tests/mineru/test_adapter.py -q -p no:cacheprovider --tb=short` → 21 passed（0.12s）；Adapter Ruff/格式和 mypy 通过。合成 middle + 已登记合成资产描述，没有真实图片解码或真实推理，未运行全量测试。
- `learn python -m pip show beautifulsoup4 lxml` → learn 环境已有 beautifulsoup4 4.15.0、lxml 6.1.3；只读取版本，尚未用于本项目，也未增加依赖声明。

**下一步**
- 继续表格归一化纵向测试，采用现有 HTML 解析库保留行列合并、表头和单元格身份；不手写 HTML 解析器。单元格不得伪造 bbox，跨页来源不足时明确降级，依据 preproc 原始证据核对。补齐资产碰撞/嵌套输入约束后再扩展列表、代码和 discarded 内容。
- 完整结构和资源/PDF 语义验证后接入 ParseService 的事前 SUBMITTING 持久化、SUBMIT_UNKNOWN 恢复、租约 fenced 发布；随后推进 A–G 其余部分。

### 2026-09-05 — 模型路径与 LLM 文件配置

**目标**
- 按用户补充要求优先完成 TOML/YAML 的模型、模型路径与 LLM 接入配置；保持完整 A–G 目标。

**当前状态**
- 归档批次已提交：`94e1e26 校验 MinerU 结果归档并统一可移植资产路径`。
- [模型配置](src/easylearn/inference/config.py) 已完成本机模型登记、MinerU、Provider、能力与用途路由；[Settings](src/easylearn/config.py) 统一文件加载、嵌套覆盖和模型路径解析。服务地址复用 [ServiceUrl](src/easylearn/urls.py)，MinerU 文件选项和运行请求复用同一解析类型；错误引用、能力组合、预算、并发预留与无穷超时均在配置入口拒绝。
- [TOML](config.example.toml) / [YAML](config.example.yaml) 模型部署示例已补齐并验证等价；[实机运行索引](docs/native.md) 指向类型与示例，不再单独维护模型路径/修订。模型路径只登记，无加载时磁盘写入、模型下载或推理服务启动。真实 LLM/MinerU 服务尚未配置，本批不是生成/推理联调。
- [Adapter 草稿](src/easylearn/mineru/adapter.py) 当前仅支持 title/text/interline_equation，复用坐标映射，保留行级区域并核对跨页来源；表格、图片、嵌套块和 ParseService 未完成，不混入配置批次提交。

**验证证据**
- 本轮恢复后观察已有 13 个配置红灯并修正；完整示例和 MinerU 后端地址规则均先 red 后 green。pytest 临时目录在沙箱内拒绝访问，获准沙箱外定向运行。
- learn Python `-m pytest tests/config tests/mineru/test_client.py tests/mineru/test_adapter.py -q -p no:cacheprovider --tb=short` → 117 passed（1.64s；配置 51、客户端 51、Adapter 15）。含两种示例等价、嵌套环境/构造覆盖、绝对与相对路径；不运行全量测试。Adapter 样例为合成数据。
- 设置 `EASYLEARN_ACCEPTANCE_PDF=D:\Papers\2403.18819v1.pdf`，`-m pytest tests/api/test_live_http.py tests/mineru/test_live_http.py -q -s -p no:cacheprovider --tb=short` → 6 passed（12.18s）。Web 从完整 TOML/YAML 示例加载并迁移，真实 27 页论文预览/下载分别 0.56s/0.55s，原件与下载摘要一致；合成 MinerU HTTP peer 传输分别 0.02s/0.05s，不代表真实模型推理。
- PostgreSQL PID 33064 仍存在，未重新初始化。Ruff 全源码/测试/迁移通过，mypy 38 源文件通过，配置相关格式与 `git diff --check` 通过；本轮未新增截图。

**下一步**
- 本配置批次已独立提交 `1f38e16`；不包含 Adapter 实现/测试草稿和用户未跟踪旧版资料。
- 再继续完整 Adapter、产物语义/几何验证和 ParseService。真实 MinerU 与 LLM 服务地址未提供，不自行安装或部署 MinerU。

### 2026-09-05 — MinerU 结果归档检查

**目标**
- 交付解析 ZIP 的固定布局、文件完整性与资源边界检查，为后续 Adapter 和不可变发布提供证据。

**当前状态**
- 上批已提交：`7ff264d 实现固定版本 MinerU 客户端与实机协议测试`。
- 已实现并定向验证 [归档检查器](src/easylearn/mineru/archive.py)：五类后端输出目录、必需原始产物、名称与文件类型、大小写冲突、CRC/逐项 SHA-256、压缩与解压限额；不解压、不发布，仅输出类型化 manifest。
- DocumentIR 导出名称与归档名称共用 [PortablePath](src/easylearn/paths.py)，删除原有局部校验定义，没有第二套路径规则。
- 阅读固定上游源码确认：`3rdparty/MinerU/mineru/cli/common.py` 的 `_prepare_pdf_bytes` 会通过 PDFium 重写输入，`_process_output` 将处理后的字节保存为 origin.pdf。因此不能假定上游 origin 摘要等于上传/预览摘要；后续须分别保留证据并核对页数及几何，不能静默替换现有预览。
- 未完成：JSON schema/版本、图片实际格式、页面几何与坐标语义、资源引用验证、Adapter、ParseService 和后台队列。此检查器同步执行，接入时须在受限工作执行单元内调用，不在 Web 事件循环中处理大 ZIP。没有实际 MinerU 推理产物捕获，也没有终版/界面验收。

**验证证据**
- 布局/路径、必需文件、重复名称/非普通文件、损坏/不支持格式、资源限额逐条先 red 后 green；真实 zipfile 读写，不 mock 内部组件。Windows 写入器会自动将反斜杠规范化，错误路径样例改用真实 ZIP 字节注入并先断言原始名称。
- 设置 `EASYLEARN_ACCEPTANCE_PDF=D:\Papers\2403.18819v1.pdf`，learn Python `-m pytest tests/mineru/test_archive.py tests/document_ir -q -p no:cacheprovider --tb=short` → 106 passed（0.68s；归档 41、DocumentIR 65）。含真实 27 页论文和 Pillow PNG 的合成 ZIP，论文 2660025 字节、摘要前后一致；不是实际 MinerU 结果或语义验收。未运行全量测试。
- 共享 schema 改动后客户端定向回归 → 51 passed（0.25s）；Ruff 全源码/测试/迁移通过，mypy 34 源文件通过，`git diff --check` 无错误。

**下一步**
- 本批检查全部通过，按中文独立提交交付；保留用户未跟踪旧版资料。恢复后从下项开始，不重复调查已验证的结构边界。
- 继续固定版本原始 JSON Adapter、图片/PDF 几何验证，再实现事前 SUBMITTING 持久化、SUBMIT_UNKNOWN 恢复与 fenced 发布。MinerU 服务地址未配置，不自行安装/部署 MinerU。

### 2026-09-05 — MinerU 固定协议客户端

**目标**
- 完成独立 MinerU 客户端及真实 HTTP 传输边界验证，不安装 MinerU，保持完整 A–G 目标。

**当前状态**
- 配置批次已提交：`cf0da49 支持 TOML 与 YAML 统一配置及实机启动`。
- 已实现并定向验证健康版本检查、提交/查询/ZIP 流下载、原始回执保留、提交不确定性、远程 backend/server_url 一致性与不可用能力声明，入口见 [协议索引](docs/protocols.md)。固定本地上游源码为 3.4.5/protocol 2，没有取消和按请求 ID 核对路由。
- 下载只在自身 HTTP I/O 边界分类网络故障，不改写消费方异常；总 deadline、实际字节限额、编码校验、取消/提前退出释放响应均已验证。下载到 staging，不宣称产物已可发布。
- 真实 Uvicorn 生命周期测试夹具统一到 `tests/conftest.py`，Web 与 MinerU 协议传输测试共用。联合运行曾暴露同名测试模块冲突，按照 [pytest 导入机制](https://docs.pytest.org/en/stable/explanation/pythonpath.html) 采用 importlib 导入；未新增包或改测试文件名规避。
- 尚未接入配置/ParseService/持久状态机、ZIP 校验与 Adapter；合成协议响应和传输用 ZIP 不是实际推理产物。完整 A–G、真实 MinerU 推理、产品界面截图及终版验收仍未完成。

**验证证据**
- 恢复时原 33 项通过。新增远程地址、参数发送、消费方异常与编码边界均先观察失败后实现；最后 `-m pytest tests/mineru/test_client.py tests/config -q -p no:cacheprovider --tb=short` → 70 passed（1.48s，其中客户端 51 项）。真实 HTTPX 客户端，仅外部 transport 注入；未运行全量测试。配置夹具曾因沙箱临时目录权限失败，获准沙箱外复跑后通过。
- 设置 `EASYLEARN_ACCEPTANCE_PDF=D:\Papers\2403.18819v1.pdf`，learn Python `-m pytest tests/api/test_live_http.py tests/mineru/test_live_http.py -q -s -p no:cacheprovider --tb=short` → 6 passed（12.30s）。TOML/YAML 各完成 27 页预览/下载（均 0.55s）；独立合成协议服务完成 1 页及 27 页 PDF 上传/查询/流式 ZIP 下载（0.02s/0.04s），传输前后摘要一致。只读用户原件；短时服务按夹具关闭。
- Ruff 全源码/测试/迁移通过，mypy 32 源文件通过，`git diff --check` 无错误。
- `Get-Process -Id 33064` → PostgreSQL 进程存在，未重新初始化；配置与真实 PDF 预览的通过证据见下一条。

**下一步**
- 本客户端批次已通过定向验证，可独立中文提交；仅暂存本批客户端、测试基础设施、配置工具选项、文档与账本，保留用户旧版资料。
- 继续 ZIP 结构/资源/摘要校验、固定版本 Adapter、ParseService 的事前 SUBMITTING 持久化与恢复，再接入原生队列及 A–G 其余功能。
- 真实 MinerU 服务地址未配置；不得自行安装或启动 MinerU 来替代独立服务接入约束。

### 2026-09-05 — 统一文件配置与实机入口

**目标**
- 完成 TOML/YAML 文件配置和统一实机启动参数，保持配置单一类型源，独立中文提交。

**当前状态**
- 用户明确要求“恢复测试”，旧审批阻塞已解除。新增数据库 URL 用例真实 red 后修正：SQLAlchemy URL/端口错误转换为不含输入的 Pydantic 配置错误；非 PostgreSQL 驱动仍拒绝。
- 参数拼写错误测试先证实原脚本会继续启动 Python，再使用 PowerShell CmdletBinding 拒绝未知参数；已有 `-ConfigPath` 与 EASYLEARN_CONFIG 共用配置选择。
- 支持格式、选择/覆盖规则、错误处理与配置示例均已定向验证，入口见 [实机运行](docs/native.md) 与 [Settings](src/easylearn/config.py)。本批不引入第二套配置字段定义。
- MinerU 客户端为另一条独立草稿，不纳入本配置提交：`src/easylearn/mineru/` 与 `tests/mineru/` 目前只有健康接口实现；提交/查询测试刚观察到缺 MinerUOptions 的 red，尚未实现。
- 完整 A–G 仍未完成，真实 MinerU 服务联调、队列、阅读器、翻译修订、导出与 AI 尚待开发。

**验证证据**
- learn Python `-m pytest tests/config -q -p no:cacheprovider --tb=short` → 19 passed（1.31s），包含真实临时文件和 PowerShell 参数绑定；不运行全量测试。
- 配置相关 Ruff、mypy 均通过。PostgreSQL 主进程 PID 33064 及其子进程仍存在，未重新初始化。
- 设置用户论文环境变量后，`-m pytest tests/api/test_live_http.py -q -s -p no:cacheprovider --tb=short` → 4 passed（11.75s），TOML/YAML 分别完成真实迁移、Uvicorn 上传和 27 页预览/下载，预检与下载用时 0.63s/0.59s。`git diff --check` 无错误。

**下一步**
- 本批已通过定向回归，可中文提交；仅暂存配置相关文件、测试、文档与本账本。
- 继续下一批 MinerU 客户端：健康协议为 3.4.5/protocol 2；只在外部 HTTP 边界注入响应，真实服务契约验收另做。不把合成 payload 标记为上游真实捕获。

### 2026-09-05 — 文件配置支持与验证审批

**目标**
- 按用户追加要求支持 TOML/YAML 文件配置，统一应用、迁移与后续 Worker 的配置来源；完整 A–G 目标不变。

**当前状态**
- PDF 预览批次已中文提交：`22993c2 实现可取消的实机 PDF 预览与资产下载`。
- 配置改动已实现并有定向证据：自动发现 `config.toml/config.yaml/config.yml`，`EASYLEARN_CONFIG` 显式选择，`run.ps1 -ConfigPath`；环境/构造参数覆盖、嵌套环境变量、文件内相对存储路径、歧义/缺失/格式/未知字段/非法限额拒绝。以 Settings 为字段与优先级唯一源，使用 tomllib、PyYAML 与 Pydantic sources，不手写格式解析器。配置示例和文档索引已更新。
- YAML 依赖 PyYAML 6.0.3 已在 learn 环境，只新增显式依赖声明，未执行安装；不安装 MinerU。此前 VLM 模型已下载验证，详情见下一条。
- 最新中断点：新增 `test_database_configuration_errors_are_typed_and_redacted`（错误 URL、错误端口、非 PostgreSQL）后，测试命令尚未执行就被审批拒绝：503 `auth_unavailable`、账号池无可用账号；工具要求明确批准后才能继续，未重试或换路径规避。新增 3 个参数用例未验证；其实现尚未修改，可能需将 SQLAlchemy URL 解析异常转为不含输入的配置校验错误。
- 本配置批次尚未提交。MinerU 客户端/适配只继续阅读了固定 3.4.5 源码，没有新模块或用例；不要将阅读记录当成功能完成。

**验证证据**
- learn Python `-m pytest tests/config -q -p no:cacheprovider --tb=short` → 新增数据库错误用例前 15 passed（0.27s）；TOML、YAML、文件选择、嵌套覆盖和错误配置逐条红绿验证。
- 设置 `EASYLEARN_ACCEPTANCE_PDF=D:\Papers\2403.18819v1.pdf`，`-m pytest tests/api/test_live_http.py -q -s -p no:cacheprovider --tb=short` → 4 passed（10.72s）；分别只靠 TOML/YAML 配置运行迁移与 Uvicorn，没有用数据库环境变量代替文件。27 页预览与 HTTP 下载分别 0.68s、0.58s。
- 审批拒绝后仅进行格式化和静态检查：Ruff 全源码/测试/迁移通过、mypy 29 文件通过、`git diff --check` 无错误；包含最新未运行测试及文档，不把静态检查代替被拒的运行验证。未运行全量测试。
- 最后被拒命令：`learn python -m pytest tests/config -k database_configuration -q -p no:cacheprovider --tb=short`；没有测试输出，不能记为 red 或 green。

**下一步**
- 用户明确允许后，先执行上述新增数据库配置用例，按实际 red 修正 URL 解析错误，再跑配置模块、实机 HTTP 和静态检查；记录结果后独立中文提交。
- 检查 `run.ps1` 参数绑定：目前非 advanced script 可能静默接受拼错参数，可定向测试后加 CmdletBinding，避免误用默认配置；不要未经验证声称已解决。
- PostgreSQL 最近前台会话 `76786`；恢复时只验证进程存活，勿重新初始化。原有未跟踪旧版参考资料保持不动。
- 之后继续 MinerU 客户端/Adapter、原生队列及其余 A–G；产品界面截图与终版验收未执行。

### 2026-09-05 — PDF 预览实测与 MinerU 模型准备

**目标**
- 完成 PDF 预览纵向链路及取消/租约边界；以用户提供的真实 PDF 为只读验收样本，继续原定全功能开发。

**当前状态**
- 用户已提供 `D:\Papers` 并允许真实 PDF 验收和截图；测试审批已恢复，顶部旧审批阻塞已解除。
- 已实现并验证 PDF 独立子进程预检、不可变原件复用、页面几何发布、资产归属检查与 GET/HEAD/Range 下载。修正 PDFium 对象生命周期；统一 Range 错误与 HTTP 错误契约，修正上游 416 的 Content-Range 格式。
- 任务基础设施增加可复用的监督入口：等待耗时操作期间续租，失去租约时取消并等待操作清理；PDF 取消确认在子进程退出后发生。失效/取消竞态不允许发布，超时可重试，损坏/加密/超限无结果资产。
- 真实论文经原生 Uvicorn、真实 PostgreSQL、磁盘与 PDF 子进程通过上传→预检→发布→完整及 Range 下载；执行仍直接调用 PreviewService，尚未接入真实队列消费者，不宣称自动后台全链路完成。
- 用户允许的 VLM 模型已下载并逐文件校验；路径及版本唯一部署入口见 [实机运行](docs/native.md)。未安装 MinerU，没有修改用户级模型配置，没有下载 pipeline 的额外模型。
- PostgreSQL 已使用原数据目录恢复，前台会话 `76786`；恢复后接受连接。原先草稿现在通过定向测试，可独立中文提交。
- 未完成：图片/Office 预览、Redis/Dramatiq 常驻进程、MinerU 客户端/适配与真实服务联调、三栏阅读器、翻译修订、导出、AI 和 A–G 其余门禁。仅检查了原始论文首页渲染；浏览器产品截图和终版验收未执行。

**验证证据**
- 初次预览复测 FAILED 的根因为 PdfPage 不支持 context manager，按实际生命周期实现后首条测试通过；续租和取消、下载错误及 HEAD 分别观察红灯后实现。损坏/加密/限额/超时回归通过。
- learn Python `-m pytest tests/api/test_previews.py tests/api/test_job_leases.py -q -p no:cacheprovider --tb=short` → 16 passed（13.42s）；真实隔离库和子进程，不运行全量测试。
- 设置 `EASYLEARN_ACCEPTANCE_PDF=D:\Papers\2403.18819v1.pdf` 后执行 `-m pytest tests/api/test_live_http.py -q -s -p no:cacheprovider --tb=short` → 2 passed（5.56s）；27 页样本预检及下载 0.69s。输入 2660025 字节，SHA-256 `9674284d4722ff5596dec155495423b7709d2051fbe8476b09cb9f64f522d5d8`，前后原件与下载摘要一致。
- Poppler 首页 PNG 已检查，标题/摘要/首段无明显缺字或裁切；渲染器报 Symbol/ArialUnicode display font 警告，未据此声称全 27 页视觉验收通过。截图在忽略的 `tmp/pdfs/acceptance-paper-page-1.png`。
- 模型官方固定 revision 全部 13 文件、2328028720 字节；逐项核对大小与 LFS SHA-256/Git blob ID，mismatches=[]。权重 SHA-256 `abf8681ca63b8dec7b67de257af47b821f179442f72998d0696ae2ed9232a5f0`，tokenizer SHA-256 `dceac5fc54a795ee7570d17902b47bd05412dc2afa62bdf325c3f97fcb5b87fe`。下载进程 `4402` 已完成。
- `-m ruff check src tests migrations` → All checks passed；`-m mypy src/easylearn` → 29 文件通过；`git diff --check` 无错误。

**下一步**
- 将本批预览与运行文档独立中文提交，保留用户未跟踪旧版规格不纳入。
- 继续 MinerU 固定版本客户端/适配和原生队列，随后剩余 A–G；MinerU 真实服务地址及 Provider 服务仍待落实，模型文件已具备不等于服务可用。

### 2026-09-05 — 结构化文档与定位证据协议

**目标**
- 补齐阶段 A 的 DocumentIR 结构协议，为解析适配、翻译、检索与导出提供同一结构输入；完整 A–G 目标保持不变。

**当前状态**
- 已实现并通过定向验证：页序/阅读序、源框/变换/四边形/归一化坐标一致性、SourceCoordinates 证据持久化；region/page/element/none 定位级别从唯一证据派生，原生元素或页级 fallback 不与精确区域同时声明。
- 已实现并验证：有独立版本身份的表格单元格、行列合并/表头/不完整表格、越界/重叠/父子归属拒绝；图片/引用节点、公式可选截图、内部资产及关系边、完整版本引用校验；可移植导出路径与大小写/目录冲突检查；派生内容摘要与页数。
- 坐标声明定义集中到 schema，Mapper 复用其有效变换，不保留第二套缩放算法；实现及测试入口见 [协议索引](docs/protocols.md)。
- 本批未运行数据库或子进程。PDF 集成复测仍等待顶部列出的明确授权；没有把自动续跑当作许可，也没有换命令执行被拒测试。此前 PDF 草稿及其 `0003` 迁移仍未提交，保持原状。
- 阶段 A 尚未全部完成：MinerU Adapter/版本契约、Provider 协议、30 份人工标注基准集仍待完成。模型约束测试不等于真实解析、Office 锚点可点击或最终质量验收；前端、翻译、导出与 AI 链路尚未完成。

**验证证据**
- 指定 learn Python `-m pytest tests/document_ir -q -p no:cacheprovider --tb=short` → 65 passed（0.15s），逐条观察新增行为失败后实现；只运行协议模块，未运行全量测试。
- `-m ruff check src tests migrations` → All checks passed；`-m mypy src/easylearn` → 28 文件通过。静态检查包含尚未运行验证的 PDF 草稿，不代表该草稿通过集成验证。
- 表格缺格不会制造新单元格；只有整表框时未给单元格生成伪造 bbox。版本、表格归属、坐标和资产冲突测试均通过实际 Pydantic/Mapper 实现，无内部 mock。

**下一步**
- 本批独立中文提交，不暂存 PDF 草稿、其他配置改动或旧版用户参考资料。
- 可继续读取 MinerU 固定版本的真实原始产物，开发 Adapter 与纯文件协议测试；不得把它替代真实 MinerU 服务验收。
- 用户明确允许后，恢复上一条 PDF 工作项：先复测，再补长任务心跳/主动取消监督、错误 PDF 与下载边界，然后接入原生队列及剩余 A–G。

### 2026-09-05 — PDF 预览预检草稿与验证审批

**目标**
- 接通独立 PDF 预检进程、预览资产与页面几何发布、Range 下载，并继续完成原定 A–G 全功能目标。

**当前状态**
- 已提交并经真实环境验证的上一批：`4e90376 实现文档受理与可靠任务持久化`；本轮此前另一批为 `6ef4a90 补齐上传限额与统一错误契约`。
- 工作区已有未提交草稿：`previews/` 的独立 learn 子进程与类型化预检结果；PDF 摘要/页数/尺寸/页面渲染检查；预览执行入口；预览结果迁移 `0003`；文档预览信息、资产归属与 FileResponse 下载；首条纵向测试 `tests/api/test_previews.py`。这些运行行为全部尚未验证，不作为完成能力交付，不提交未过验证的批次。
- 静态检查通过。原件 PDF 未改写；仅在忽略目录 `tmp/pdfs/` 渲染了样本 PNG 并人工检查。新增依赖声明 `pypdf==6.16.2` 已存在 learn 环境，无新包安装；PDFium 现有版本不能取得 UserUnit 且页面盒继承有已知限制，因此使用 pypdf 元数据与 PDFium 渲染几何核对，来源见官方 API 与本地包源码。
- 当前中断点：新增测试先观察到缺少 previews 模块的红灯；写入实现后，复测调用被审批工具拒绝，原因是审批服务 `429 Too Many Requests`、重试耗尽。拒绝明确禁止换路径执行同一操作，要求用户知情后明确允许；没有重试、没有绕过，仅继续沙箱内静态检查。
- 已知待继续开发的点：预览执行尚无长任务心跳/主动取消监督（默认 PDF 超时 120 秒、任务租约 60 秒，不能据此支持长 PDF）；预览详情的状态投影及重复 report 校验需整理；需验证并补齐损坏/加密/超限 PDF、过期发布、跨文档下载拒绝、错误 Range 契约。图片/Office 转换、原生队列与后续 A–G 仍未完成。

**验证证据**
- `learn python -m pytest tests/api/test_previews.py -q -p no:cacheprovider --tb=short` 的实现前执行 → ModuleNotFoundError；实现后复测未获准执行，不能记录为通过。
- 实现后 `-m mypy src/easylearn` → 28 文件通过；Ruff 检查与格式化通过。没有运行全量测试，没有在沙箱内改用另一命令执行被拒的集成测试。
- `pdfinfo 3rdparty/MinerU/tests/unittest/pdfs/test.pdf` → 1 页、612×792 pt、rotation=0、未加密、125121 字节；Poppler 渲染并检查了唯一一页，原图包含图组、公式、文本与竖排表格。
- 官方资料：[PDFium Python API](https://pypdfium2.readthedocs.io/en/stable/python_api.html)、[pypdf 页面属性](https://pypdf.readthedocs.io/en/stable/modules/PageObject.html)。本地 pypdfium2 `page.py` 明确标注 UserUnit 无查询接口及页面盒继承限制。

**下一步**
- 等用户明确允许上述复测后，首先运行 `tests/api/test_previews.py`，修正真实失败，逐条继续 TDD；再完成心跳/取消监督及预览故障用例，独立中文提交。
- 重启或恢复时先验证 PostgreSQL 会话 `31334` 是否仍存活；本次拒绝不代表 PostgreSQL 已退出。不要重新安装已有 PostgreSQL/pypdf，不要安装 MinerU，不要使用 Docker/WSL。
- 已完成批次的证据见下一条日志。旧版未跟踪规格目录仍为用户原资料，不纳入新批次。

### 2026-09-05 — 文档受理与可靠任务持久化

**目标**
- 交付同事务文档/预览运行/任务/Outbox、幂等受理和数据库驱动的租约/取消/恢复机制，作为独立预览 Worker 的基础。

**当前状态**
- 已实现并验证：202 文档创建、文档详情、任务查询、取消与带幂等键的重试；并发相同键仅创建一个逻辑运行，冲突参数返回 409，拒绝请求不留下任务或占用幂等键。
- 已实现并验证：Outbox 并发领取、超时接管、迟到确认拒绝；任务原子领取、DB 时钟租约、心跳、checkpoint、generation 隔离、丢失派发的补投、取消后写回拒绝、退出确认/过期取消、失败可重试性与事务发布入口。
- 公共接口与实现入口统一索引在 [docs/README.md](docs/README.md)，数据库迁移为 `0002`。任务本批只支持 PREVIEW 类型；未宣称通用外部任务恢复策略已经完成。
- 已提交前一批：`6ef4a90 补齐上传限额与统一错误契约`。本批单独中文提交。
- 未完成：实际预览转换/可渲染性验证与资产发布、文档列表/新预览运行、常驻 dispatcher/reconciler 入口、真实 Redis/Dramatiq、Office、MinerU、前端及 A–G 其余功能。当前创建任务后不会自动生成 PDF；不能将数据库恢复测试视为 Redis 故障验收通过。

**验证证据**
- learn Python `-m pytest tests/api/test_documents.py tests/api/test_job_delivery.py tests/api/test_job_leases.py -q -p no:cacheprovider --tb=short` → 9 passed（6.73s）；新增行为逐条红绿推进，真实 PostgreSQL 与磁盘，未 mock 内部依赖。
- `-m pytest tests/api/test_live_http.py tests/api/test_upload_contract.py -q -p no:cacheprovider --tb=short` → 3 passed（2.72s）；短时原生 Uvicorn 经真实 HTTP 接受 PDF，创建任务、取消、重试 generation=2，测试进程已关闭。
- `-m mypy src/easylearn` → 24 文件通过；未运行全量测试。
- PostgreSQL 前台会话仍为 `31334`；没有安装 MinerU，没有 Docker/WSL。

**下一步**
- 实现原生独立进程 PDF 预检和图片标准预览；通过现有 fenced publication 同事务发布预览结果，补充过期发布/错误 PDF 与真实页面几何测试。
- 随后接入本机 Redis/Dramatiq、常驻 Outbox/Reconciler 与 Office 转换；接入独立 MinerU 服务及剩余 A–G 功能。用户已授权总体实施，不重复确认。

### 2026-09-05 — 上传限额与统一错误契约

**目标**
- 补齐上传创建响应限额与统一请求错误，不改变本机运行、完整 A–G 目标与已确认测试接口。

**当前状态**
- 已完成：创建响应派生实际配置限额；校验、领域、HTTP 错误共用类型与请求 ID，响应头可关联错误正文；校验详情不回显提交内容。
- 前两批已提交：`9663f39 实现版本化文档协议与坐标映射基础`、`f531cc4 实现持久上传与本机运行基础`。
- 未完成：文档/预览与可靠任务、PDF 完整预检及后续 A–G 能力；上传资源清理仍待实现。

**验证证据**
- 指定 learn Python 执行 `-m pytest tests/api/test_upload_contract.py tests/api/test_uploads.py -q -p no:cacheprovider --tb=short` → 6 passed，真实 PostgreSQL 与磁盘；新增限额、统一错误分别观察到 KeyError 后实现通过。
- `-m mypy src/easylearn` → 14 文件通过；未运行全量测试。
- PostgreSQL 前台会话 `31334` 仍运行，隔离数据库测试连接成功。

**下一步**
- 实现同事务文档、预览运行、任务与 Outbox，验证幂等键冲突和重复投递，再推进租约/代次/取消/恢复。继续分阶段中文提交。

### 2026-09-05 — 持久上传与实机运行基础

**目标**
- 交付上传会话、不可变资产、数据库迁移和本机 Web 启动的首个真实纵向链路；保持 A–G 全功能目标不变。

**当前状态**
- 已实现并验证：上传创建/查询/续传/完成；PostgreSQL 行锁与相同分块幂等；错误偏移冲突；SHA-256 校验与 INVALID 状态持久化；过期禁止发布；声明格式与文件签名/Office 包入口校验；跨应用实例续传及重复完成；内容寻址文件存储、fsync 与不可覆盖发布。
- 已提供 FastAPI 工厂、存活/数据库及存储就绪检查、Alembic 首个迁移、实机启动入口。入口及配置索引见 [实机运行](docs/native.md)，代码与测试见 [文档索引](docs/README.md)。
- 已将 v2 规格中的 Compose 部署合同调整为本机独立进程，保留共享存储、迁移、恢复和健康检查要求。v2 参考资料作为规格输入随本批纳入版本管理；旧版资料保持原状。
- 第一批中文提交：`9663f39 实现版本化文档协议与坐标映射基础`。本批单独提交，不宣称阶段 A/B 全部完成。
- 未完成：文档创建/预览任务、PDF 完整预检与图片/Office 转换、DocumentIR 剩余结构和 Adapter、Outbox/租约/恢复、工作台、翻译修订、导出、AI 解读、清理与最终验收。上传阶段只检查格式签名与包入口，不能替代尚未实现的预览可渲染性验证。
- 未完成的上传配套：创建响应补充限额、统一请求校验错误、资产引用感知清理及保留窗口、更多中断/磁盘故障用例。文件 staging 在写入失败时清理；无 DB 引用的不可变对象仍需后续 GC。

**验证证据**
- 指定 learn 环境 `-m pytest tests/api -q -p no:cacheprovider --tb=short` → 8 passed（5.19s），覆盖真实 PostgreSQL、跨应用实例恢复、到期、并发重复分块、真实 PDF、格式伪装与实机 HTTP；执行中先观察缺实现/错误状态失败，再实现通过。
- `-m pytest tests/document_ir -q -p no:cacheprovider` → 19 passed；按模块验证，未运行全量测试。
- `-m ruff check src tests migrations` → All checks passed；`-m mypy src/easylearn` → 14 source files 无问题。
- PostgreSQL 17.11 官方二进制来源：https://get.enterprisedb.com/postgresql/postgresql-17.11-3-windows-x64-binaries.zip；本地归档 SHA-256 `4b8db0930c38f6ef845db919551dedda3b6b845aeb0927b3d79a6e8e9e4537cf`。存放 `.runtime/postgresql-17.11/pgsql`，数据 `.runtime/postgres-data`，仅绑定 `127.0.0.1:55432`，未注册系统服务；测试集群使用本机 trust，不用于生产。
- pg_ctl 后台启动报告就绪，但命令结束后进程消失；改为可追踪前台会话后，同一测试成功建立连接并执行迁移。PostgreSQL 前台会话 ID 为 `31334`，恢复工作须先验证会话/进程仍存活，不凭日志认定存活。
- API 测试需沙箱外访问本机数据库；每条测试只创建、删除自己生成的 `easylearn_test_<UUID>` 数据库。已关闭各短时 Uvicorn 测试进程。没有安装 MinerU，没有使用 Docker。
- 真实 PDF fixture 复用 `3rdparty/MinerU/tests/unittest/pdfs/test.pdf`，SHA-256 `ae9e3f14cc3bea88dd0ce4e2715b3b03561378501318df61f0889df207aed25b`；仅读取第三方样本与源码。

**下一步**
- 先补齐上传配套契约与输入资源验证，再实现文档/预览运行、事务 Outbox 和工作任务租约，以真实 PostgreSQL 测试推进。
- 完成 PDF/图片预览模块后接入 Office 转换，接着 MinerU 独立服务协议/Adapter；服务地址、生成及 Embedding 模型和 30 份人工标注基准集仍需在真实联调前落实，不能用 mock 宣称验收通过。
- 原生 Redis/转换服务尚未安装或启动；下一步安全核实可用依赖。继续分批中文提交；不要重新请求已确认的总体方案授权。

### 2026-09-05 — 版本化协议与坐标基础

**目标**
- 实现 v2 方案中版本身份、文档结构和坐标投影的第一批基础能力，按中文 commit 分批提交。

**当前状态**
- 用户已确认完整实施方案与测试边界，并要求分阶段提交、中文 commit。
- 已实现并验证：不可变完整 BlockRef、文档块基础结构、父块环和重复身份检查、文本/公式/代码/链接类型、四种声明坐标空间、非零 CropBox、旋转还原与非法坐标拒绝。实现索引见 [协议文档](docs/protocols.md)。
- 本批不等于 A 阶段全部完成：完整 IR 表格/图片/引用/关系及定位校验、MinerU Adapter、Provider 协议和 30 份标注基准集尚未完成。
- learn 环境已获准安装项目与开发依赖，未安装 MinerU。依赖的已解析直接版本固定在 pyproject.toml；全依赖锁定仍待补齐。
- 上传与实机运行代码已在工作区实施，单独在下一批记录和提交；不将其混入本批协议提交。

**验证证据**
- `E:\Softwares\Anaconda3\envs\learn\python.exe -m pytest tests/document_ir -q -p no:cacheprovider` → 19 passed；开发过程有对应缺模块、缺校验失败记录，随后实现通过。
- `... -m mypy src/easylearn` → 14 个当前源码文件通过严格类型检查。
- `git diff --check` → 无空白错误；未运行全量测试。

**下一步**
- 提交当前协议批次后，将上传、迁移、实机入口和对应验证证据作为下一批独立提交。
- 继续补齐 A–G 其余能力，不缩小全功能目标；已确认的实施与测试边界无需重复询问。

### 2026-09-05 — v2 完整开发启动

**目标**
- 按 [v2 完整方案](docs/FastAPI_MinerU方案与交互示例_v2/FastAPI_MinerU完整开发方案_v2.md) 的 A–G 阶段交付，界面参考同目录交互 HTML 与两张 PNG；不以演示数据替代真实链路。

**当前状态**
- 用户明确：不要 Docker，采用实机 Python，指定环境 `E:\Softwares\Anaconda3\envs\learn`。此决定覆盖原方案的 Compose 部署路径；实施时同步调整规格中的部署章节及交付条目，保留共享存储、迁移、独立服务、恢复和健康检查合同。
- 用户更新目标：不要直接安装 MinerU。只开发独立 MinerU 服务的客户端与 Adapter，现有第三方源码仅作协议核对；不将 MinerU 安装进指定环境，也不自行部署其模型服务。真实解析验收需连接获准的可用服务。
- 已检查方案、交互脚本、两张页面图和 MinerU 接口源码；`src/` 为空，尚无业务应用、测试及部署配置，既有账本无真实日志。
- 用户已明确确认“按上述实施方案与测试边界开始开发”，实施与测试边界授权已具备。
- 拟沿用规格业务技术栈及第 25 节模块边界；Python schema/OpenAPI 派生前端类型，DocumentIR 派生渲染和导出，数据库承担版本与任务状态唯一事实源。按 A–G 依赖顺序逐条纵向 TDD，完成模块后真实集成验证，开发中不运行全量测试。
- 已确认测试边界：版本化 HTTP API（真实数据库/存储）；DocumentIR 适配和坐标投影接口；任务执行接口（真实 PostgreSQL/Redis，注入重复投递、租约和取消故障）；翻译/人工修订/导出服务；问答上下文、检索和引用服务；浏览器用户操作（真实 PDF.js 与后端）。下一层依赖不 mock，更深层外部接口可用于故障注入，另做真实服务契约验证。
- 实机 PostgreSQL、Redis、LibreOffice 的安装位置/可用性及 Windows Worker 启动兼容性尚待核查；真实 MinerU、生成模型、Embedding 服务及人工标注基准集尚未验证。视觉分支按规格能力条件启用。

**验证证据**
- `rg --files -g '!3rdparty/**' -g '!docs/**'` 与 `Get-ChildItem -LiteralPath src -Force`：仅有根目录管理文件，src 无内容。
- `git -c safe.directory=D:/Project/EasyLearn status --short`：已有未跟踪 `docs/`，须保留用户资料；未更改全局 Git 配置。
- `3rdparty/MinerU/mineru/version.py`：3.4.5；`mineru/cli/fast_api.py` 路由包含 `/tasks`、`/tasks/{task_id}`、`/tasks/{task_id}/result`、`/file_parse`、`/health`，未发现取消及按请求标识核对路由，适配器不得假定这两种能力存在。
- `& 'E:\Softwares\Anaconda3\envs\learn\python.exe' --version`：Python 3.12.13。
- 指定解释器执行 `-m pip show`：已有 FastAPI 0.141.1、Pydantic 2.13.5、pytest 9.0.3、httpx 0.28.1、Uvicorn 0.52.4；未安装 SQLAlchemy、Alembic、psycopg、asyncpg、redis、dramatiq、mineru。
- `Get-Command postgres,pg_ctl,redis-server,soffice,nvidia-smi -ErrorAction SilentlyContinue`：仅找到 nvidia-smi；这不代表其他服务未安装。
- 2026-09-05 续查：`Get-Service` 按 postgres/redis/memurai 名称过滤为 0；`E:\Softwares` 一级目录及两个 Program Files 目录未发现匹配的 PostgreSQL/Redis/Memurai/LibreOffice 安装目录。未扫描全盘，不能据此断言未安装。
- 指定解释器 `-m pip show pgvector pymupdf pypdfium2 pillow torch`：已有 pypdfium2 5.10.1、Pillow 12.1.1、torch 2.9.1+cu128；pgvector、pymupdf 未安装。
- [Dramatiq 2.2.0 CLI 源码](https://raw.githubusercontent.com/Bogdanp/dramatiq/v2.2.0/dramatiq/cli.py) 已包含 Windows spawn、条件信号处理及 SIGBREAK 支持；无需仅凭操作系统更换队列框架，固定版本后仍需本机 Worker 生命周期实测。
- [Redis 官方 Windows 原生指南](https://redis.io/tutorials/howtos/how-to-run-redis-on-windows-natively-with-memurai/) 提供 Memurai 路径，不要求 Docker/WSL；这仅是待核实的部署候选，未安装或采用，不代表现有环境已具备 Broker。
- 早前 Docker 沙箱外只读复查被拒，随后用户明确不用 Docker；不再检查或使用 Docker。
- 未运行测试，未启动服务，未安装依赖；真实质量与性能均未验证。

**下一步**
- 从 A 阶段项目骨架、协议及首条行为测试开始，继续 B–G；无需重复询问已经确认的实施与测试边界。
- 所有 Python 命令显式使用用户指定解释器；环境安装涉及工作区外写入时按权限要求申请批准。先核实原生 Windows 服务与 Worker 支持，再制定实机启动入口；不静默换数据库、队列或使用 WSL。

