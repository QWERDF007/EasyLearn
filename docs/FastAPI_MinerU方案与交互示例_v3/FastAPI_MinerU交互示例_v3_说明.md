# FastAPI + MinerU 交互示例 v3 说明

> 本交互示例与同目录下的《FastAPI_MinerU完整开发方案_v3.md》配套，完整反映当前最新 EasyLearn 工作台的真实 UI 布局、色彩规范与交互链路。

---

## 1. 文件结构与打开方式

| 文件名 | 用途与说明 |
|---|---|
| [FastAPI_MinerU工作台交互示例_v3.html](./FastAPI_MinerU工作台交互示例_v3.html) | **单文件自包含交互原型**。双击即可在任意现代浏览器（Chrome / Edge / Firefox / Safari）中直接打开，无任何外部 CDN 依赖、构建步骤或后端依赖。 |
| [FastAPI_MinerU完整开发方案_v3.md](./FastAPI_MinerU完整开发方案_v3.md) | **系统级架构与实现规格方案**。遵循单一真相源原则，直接指向工程源码（FastAPI、SQLite、MinerU、批量翻译引擎、KaTeX 等）。 |

---

## 2. 核心交互特性与视觉规范

### 2.1 侧边栏文档与实时进度感知
- **SVG 环形进度条**：外圈包裹高分辨率 SVG 环（`r=15.5`，`stroke-width=2.2`），通过圆环描边偏移（`stroke-dashoffset`）精确展示实际进度，模拟了当前系统的 5 种典型任务状态：
  - `HOW DO VISION TRANSFORMERS WORK.pdf`：绿色对勾圆环（`.doc-ring-complete`），解析完成 26 页；
  - `Attention Is All You Need.pdf`：品牌蓝环形推进，显示 `解析中 72%`；
  - `Deep Residual Learning.docx`：环形推进，显示 `上传中 50%`；
  - `Diffusion Models Overview.pptx`：环形顺时针旋转动效（`.doc-ring-spin`），显示 `排队中`；
  - `Vision Benchmark Results.xlsx`：红叉警示，显示 `解析失败`（支持单据重试）。
- **实心文件类型徽标**：与现有项目代码完全对齐的实心圆角徽标（`.doc-badge`），区分 PDF（红）、DOCX（蓝）、PPTX（橙）、XLSX（绿）、图片（靛蓝）。
- **统一矢量操作按钮**：收藏五角星（黄金高亮）、删除垃圾桶、刷新重试，尺寸统一为 26x26px 并自带平滑悬浮过渡。

### 2.2 PDF 阅读器与选框高亮
- **双行顶栏与阅读控制条**：高度 84px 的顶部栏。上行展示文件图标、文件名、大小与状态；下行包含“原文件”Tab 与阅读控制工具栏（`#pdf-toolbar`），支持翻页（◀ 当前页/总页数 ▶）、比例缩放（-、100%、+）与一键重置 1:1；
- **几何选框图层**：正文段落与块级数学公式均精准覆盖选框，支持悬浮预览标签与点击常驻选中。

### 2.3 结果面板（三大真实视图 Tab）与公式色彩规范
- **双行顶栏与操作工具栏**：高度 84px 的顶部栏。上行为解析模型下拉选择器（`.model-select-group`）；下行左侧为三大真实视图切换 Tab（`中文 Markdown`、`原文 Markdown`、`JSON`，无独立“双语”Tab，由左栏原文件与右栏中文 Markdown 并列自然形成对照阅读），右侧为 6 大操作按钮：AI 解读、翻译全文、设置、重新解析、复制、下载；
- **公式块常态与选中态交互**（严格对应当前 [`app.css`](../../src/easylearn/static/app.css) 规范）：
  - **常态（未选中 / 未悬浮）**：保持完全透明、无边框、无底色，与普通文本段落融为一体，保持沉浸式学术排版质感；
  - **悬浮与选中态**：激活专属粉红/洋红高亮边框（`#f43f5e`）、柔粉背景底色（`#fff1f2`）与粉红“公式”角标；
- **双向平滑联动与聚焦动画**：点击左侧 PDF 原文选框时，右侧卡片自动平滑滚动居中，并触发 `targetFlash` 聚焦闪烁光圈动画；
- **人工译文锁定保护**：卡片底部提供锁定复选框（`locked: true`），保护人工校对成果不被全局机器翻译覆盖。

### 2.4 右上角 AI 解读抽屉与溯源问答
- **抽屉式不挤压三栏**：点击右上角“AI 解读”按钮平滑滑出抽屉；
- **动态上下文锚点框**：自动捕获当前选中的段落或公式，点击锚点框可立即反向平滑定位并闪烁证据卡片；
- **快捷提问药丸**：提供一键填入“解释核心概念”、“公式推导说明”、“提炼主旨术语”等提示词；
- **交互式引用溯源跳转**：回答中包含标注文献索引 `[1]`、`[2]`，点击药丸瞬间触发左侧 PDF 选框与右侧卡片高亮定位。

### 2.5 系统设置抽屉
- 展示 LLM 连接状态、自动探测系统代理（如 `127.0.0.1:7890`）、MinerU 本地模型权重与 `config.toml` 配置指引。

---

## 3. 推荐体验操作顺序

1. **观察侧边栏进度状态**：对比 5 个文档的环形进度条动画（完成对勾、百分比推进、排队旋转与失败）。
2. **测试公式交互规范**：
   - 观察公式 (1) 和公式 (2)：常态下干净利落，与周围普通段落无框线异样；
   - 鼠标悬浮到公式上，即刻呈现专属粉红色边框与“公式”角标；
   - 单击左侧公式选框，观察右侧卡片平滑滚动居中并触发粉红聚焦光圈动画。
3. **体验工具栏缩放**：点击底部工具栏的 `+` 与 `-` 缩放纸张，点击 `1:1` 即刻复位。
4. **打开 AI 解读抽屉**：
   - 点击右上角“AI 解读”按钮展开抽屉；
   - 查看当前锚点框显示的选中块；
   - 点击回答中的引用标号 `[2]`，观察页面即刻自动跳转定位至公式 (1) 证据块；
   - 点击任一快捷提示词药丸（如“公式推导说明”），观察输入框自动填充。
5. **打开系统设置抽屉**：点击左上角齿轮图标，查看本地代理与模型挂载配置。

---

## 4. 源码实现对应关系

| 原型组件与行为 | 对应后端与前端代码实现源 |
|---|---|
| 侧边栏环形进度与徽标 | [src/easylearn/static/app.css](../../src/easylearn/static/app.css)、[src/easylearn/static/app.js](../../src/easylearn/static/app.js) |
| PDF 选框图层与缩放控制 | [src/easylearn/static/pdf-viewer.js](../../src/easylearn/static/pdf-viewer.js) |
| 公式粉红色系与 KaTeX 渲染 | [src/easylearn/static/katex/](../../src/easylearn/static/katex/)、[app.css:L543](../../src/easylearn/static/app.css#L543-L557) |
| 平滑居中滚动与聚焦闪烁 | [app.js:scrollToResultBlock](../../src/easylearn/static/app.js)、[app.css:targetFlash](../../src/easylearn/static/app.css#L574-L582) |
| AI 解读抽屉与流式引用溯源 | [src/easylearn/qa.py](../../src/easylearn/qa.py)、[app.js:ai-drawer](../../src/easylearn/static/app.js) |
| 批量断点续传与代理重试 | [src/easylearn/translation.py](../../src/easylearn/translation.py) |
