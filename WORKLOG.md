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

