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
- [统一错误类型](../src/easylearn/errors.py)、[上传契约验证](../tests/api/test_upload_contract.py)
- [文档与预览受理](../src/easylearn/documents/service.py)、[请求幂等](../src/easylearn/idempotency.py)
- [任务执行与发布](../src/easylearn/jobs/service.py)、[Outbox 领取与确认](../src/easylearn/jobs/delivery.py)、[任务协议类型](../src/easylearn/jobs/schema.py)
- [PDF 子进程预检](../src/easylearn/previews/pdf.py)、[预览执行](../src/easylearn/previews/service.py)、[预览协议与限额](../src/easylearn/previews/schema.py)
- [流式资产下载](../src/easylearn/downloads.py)、[预览边界测试](../tests/api/test_previews.py)、[实机 HTTP 测试](../tests/api/test_live_http.py)
- [数据库迁移](../migrations/versions)、[资产存储](../src/easylearn/storage.py)
- [协议测试](../tests/document_ir)、[真实 PostgreSQL 接口测试](../tests/api)
