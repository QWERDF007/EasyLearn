# 协议实现索引

- DocumentIR、块身份、表格、资产和定位等级：[schema.py](../src/easylearn/document_ir/schema.py)
- 声明坐标到 PDF 用户空间的投影：[coordinates.py](../src/easylearn/document_ir/coordinates.py)
- 文档输入格式与路径边界：[filetypes.py](../src/easylearn/filetypes.py)、[paths.py](../src/easylearn/paths.py)
- MinerU HTTP 协议与固定版本选项：[client.py](../src/easylearn/mineru/client.py)、[schema.py](../src/easylearn/mineru/schema.py)
- MinerU ZIP、图片和 JSON 结果验证：[archive.py](../src/easylearn/mineru/archive.py)、[images.py](../src/easylearn/images.py)、[result.py](../src/easylearn/mineru/result.py)
- MinerU middle 到 DocumentIR 的归一化：[adapter.py](../src/easylearn/mineru/adapter.py)、[tables.py](../src/easylearn/mineru/tables.py)
- PDF 预检与解析发布：[previews/pdf.py](../src/easylearn/previews/pdf.py)、[parser.py](../src/easylearn/parser.py)
- 翻译结构保护、人工修订和问答引用：[translation.py](../src/easylearn/translation.py)、[qa.py](../src/easylearn/qa.py)

对应行为测试分别位于 [tests/document_ir](../tests/document_ir)、[tests/mineru](../tests/mineru) 和 [tests/v3](../tests/v3)。Pydantic 模型是字段和约束的唯一来源，文档只维护索引。
