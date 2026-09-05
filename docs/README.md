# 文档索引

- [产品规格与验收](FastAPI_MinerU方案与交互示例_v2/FastAPI_MinerU完整开发方案_v2.md)
- [交互参考与示例范围](FastAPI_MinerU方案与交互示例_v2/FastAPI_MinerU交互示例_v2_说明.md)
- [实机运行](native.md)
- [协议实现索引](protocols.md)
- [任务账本](../WORKLOG.md)

实现入口：

- [依赖与开发工具配置](../pyproject.toml)
- [DocumentIR 与身份类型](../src/easylearn/document_ir/schema.py)、[坐标映射](../src/easylearn/document_ir/coordinates.py)
- [HTTP 应用与 OpenAPI](../src/easylearn/main.py)、[上传模块](../src/easylearn/uploads/service.py)
- [数据库迁移](../migrations/versions)、[资产存储](../src/easylearn/storage.py)
- [协议测试](../tests/document_ir)、[真实 PostgreSQL 接口测试](../tests/api)
