# WORKLOG

> 本文件是项目唯一的任务账本。真实日志按最新在前追加在固定示例条目之后，并固定位于其他真实日志之上；`⏳ 待你裁决` 始终固定在顶部。

## ⏳ 待你裁决

<!-- 没有待裁决事项时保持本节为空。 -->


---

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

