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

