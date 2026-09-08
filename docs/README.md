# 文档与实现索引

- [FastAPI + MinerU 完整方案与交互示例 v3](FastAPI_MinerU方案与交互示例_v3/FastAPI_MinerU完整开发方案_v3.md)：基于当前代码整理的系统架构方案与独立 HTML 交互原型。
- [轻量完整开发方案 v3 (早期规格)](FastAPI_MinerU轻量完整开发方案_v3.md)：产品边界、接口和验收标准。
- [本机启动](native.md)：`learn` 环境、配置和 Python 入口。
- [当前状态、目标与计划](CURRENT.md)：当前可用能力和下一阶段工作入口。
- [协议实现索引](protocols.md)：DocumentIR、MinerU 归档和坐标相关实现。
- [Pinaic OpenAI 兼容 API 外部事实](research/pinaic-openai-compatible-api.md)：仅记录 Pinaic 官方页面明确说明的外部接口事实。
- [任务账本](../WORKLOG.md)：当前工作的唯一进度记录。

实现的唯一入口：

- [项目依赖](../pyproject.toml)、[配置类型与加载](../src/easylearn/config.py)、[配置示例](../config.example.toml)
- [应用生命周期与 HTTP 路由](../src/easylearn/main.py)、[Python 启动入口](../src/easylearn/__main__.py)
- [SQLite 访问与初始化](../src/easylearn/database.py)、[路径与文件操作](../src/easylearn/paths.py)、[实例锁](../src/easylearn/files.py)
- [内存任务队列](../src/easylearn/tasks.py)、[任务类型](../src/easylearn/jobs/schema.py)
- [文档上传、目录和下载](../src/easylearn/documents/service.py)、[输入格式边界](../src/easylearn/filetypes.py)
- [解析执行](../src/easylearn/parser.py)、[内置 MinerU 运行时](../src/easylearn/mineru/embedded.py)、[结果归一化](../src/easylearn/mineru/result.py)
- [DocumentIR](../src/easylearn/document_ir/schema.py)、[坐标转换](../src/easylearn/document_ir/coordinates.py)
- [翻译、人工修订与 LLM 客户端](../src/easylearn/translation.py)、[Markdown 渲染](../src/easylearn/rendering.py)
- [导出快照](../src/easylearn/exports.py)、[问答与引用](../src/easylearn/qa.py)
- [页面模板](../src/easylearn/templates/index.html)、[样式](../src/easylearn/static/app.css)、[交互脚本](../src/easylearn/static/app.js)

定向行为测试位于 [tests/v3](../tests/v3)，浏览器端到端测试位于 [tests/browser](../tests/browser)。字段和约束均从 Python 类型派生，不在文档中维护第二套接口定义。
