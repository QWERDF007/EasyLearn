# 协议实现索引

- 版本化块身份、文档结构与不可变类型：[schema.py](../src/easylearn/document_ir/schema.py)
- 坐标声明及源区域到 PDF 空间的投影：[coordinates.py](../src/easylearn/document_ir/coordinates.py)
- 可观察错误：[errors.py](../src/easylearn/errors.py)
- 协议行为与固定几何样例：[协议测试](../tests/document_ir)

JSON Schema 从 Pydantic 模型生成，不手写第二份字段或约束定义。
