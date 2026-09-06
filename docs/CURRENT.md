# 当前状态

## 当前状态

- EasyLearn 使用单进程 FastAPI、SQLite、本地文件和进程内任务队列。
- MinerU 通过仓库内源码在 EasyLearn 进程中运行，使用本机 GPU 和配置登记的 `F:\models\MinerU2.5-Pro-2605-1.2B`。
- 本机配置已将任务临时目录置于 `F:\tmp\easylearn`；模型候选使用 `config.toml` 显式登记，不从目录自动发现。
- `D:\Papers\2403.18819v1.pdf` 已完成真实上传、27 页 GPU 解析、DocumentIR 发布和 Markdown 读取验证。
- `F:\Papers\HOW DO VISION TRANSFORMERS WORK.pdf` 已通过真实浏览器验收：上传、26 页 GPU 解析、结果发布、PDF 翻页、150% 缩放和 1:1 重置。
- Web 工作台已提供解析阶段反馈、任务取消、右侧设置/重新解析/下载操作、PDF 翻页/缩放/重置/适应宽度/旋转、`Ctrl + 滚轮` 缩放、块联动、收藏、删除确认和源文本修订。
- 阅读器网格项已限制最小高度，PDF 内容在内部滚动容器中展示，底部工具栏保持在可操作视口内；对应真实验收见 [test_real_acceptance.py](../tests/browser/test_real_acceptance.py)。
- 无文档时工作台显示上传/拖拽空态；前端不再提供 MinerU 模型选择下拉框，解析请求使用配置中的默认模型。
- Python Selenium 4.48.0 与 ChromeDriver 152.0.7977.82 已安装；现有浏览器上传测试已通过。
- Pinaic 仅完成官方 OpenAI 兼容接口资料索引；API Key、文本模型名和真实调用尚未配置。

## 目标

- [x] 将 MinerU 模型候选改为 `config.toml` 显式配置，不再从目录自动发现。
- [x] 将任务临时目录改为配置项，并在本机配置中使用 `F:\tmp\easylearn`。
- [x] 补齐本轮工作台交互的 Selenium 验收并保持真实 GPU 论文链路可运行。
- 用户提供 Pinaic API Key 和文本模型名后，完成 OpenAI 兼容接口配置与真实联调。

## Plan

1. ✅ 定义强类型 MinerU 模型候选配置，统一默认模型和任务固定模型 ID 的解析来源。
2. ✅ 定义独立任务临时目录配置，将本机值设为 `F:\tmp\easylearn`，补充路径解析和生命周期测试。
3. ✅ 补充解析进度/取消、PDF 缩放与重置、块联动、收藏/删除和源文本保存的 Selenium 用例。
4. ✅ 使用 `learn` 环境运行定向单元测试、静态检查和真实论文 GPU Selenium 回归。
5. 获得 Pinaic 凭据与模型名后配置 `[llm]`，验证认证、模型调用、翻译和问答。

详细历史证据与恢复信息见 [WORKLOG](../WORKLOG.md)。
