# 协议实现索引

- 版本身份、文档结构、定位证据、表格与资产类型：[schema.py](../src/easylearn/document_ir/schema.py)
- 声明坐标到 PDF 空间的投影：[coordinates.py](../src/easylearn/document_ir/coordinates.py)
- 可观察错误：[errors.py](../src/easylearn/errors.py)
- 协议行为与固定几何样例：[协议测试](../tests/document_ir)
- 表格结构与单元格归属：[表格测试](../tests/document_ir/test_tables.py)；图片、公式截图、关系及导出路径：[资产引用测试](../tests/document_ir/test_assets_relations.py)
- 定位降级与坐标来源：[定位测试](../tests/document_ir/test_localization.py)；结构摘要：[摘要测试](../tests/document_ir/test_content_hash.py)
- 独立 MinerU HTTP 协议：[客户端](../src/easylearn/mineru/client.py)、[固定版本与选项](../src/easylearn/mineru/schema.py)、[边界测试](../tests/mineru/test_client.py)
- MinerU 客户端真实网络传输：[HTTP 测试](../tests/mineru/test_live_http.py)，使用[合成协议服务](../tests/mineru/protocol_server.py)，不代表真实推理契约验收。
- MinerU ZIP 结构、校验和与资源限额：[归档检查器](../src/easylearn/mineru/archive.py)、[归档测试](../tests/mineru/test_archive.py)；检查不解压、不发布，也不替代 JSON/图片/坐标语义验证。
- MinerU middle 结构归一化：[Adapter](../src/easylearn/mineru/adapter.py)、[HTML 表格](../src/easylearn/mineru/tables.py)、[内部图片引用](../src/easylearn/mineru/assets.py)、[结构与真实资源组合测试](../tests/mineru/test_adapter.py)。输入样例来源及验收范围见测试标注，运行与真实推理验收证据见 [WORKLOG](../WORKLOG.md)。
- 跨平台资产名称：[共用路径类型](../src/easylearn/paths.py)。
- 模型路径登记、MinerU/LLM 连接、能力与用途路由：[配置类型](../src/easylearn/inference/config.py)、[统一加载](../src/easylearn/config.py)、[文件/覆盖/约束测试](../tests/config/test_settings.py)；服务地址复用 [ServiceUrl](../src/easylearn/urls.py)。

JSON Schema 从 Pydantic 模型生成，不手写第二份字段或约束定义。
