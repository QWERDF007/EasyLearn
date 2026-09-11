# 架构可靠性整改工单

这些是根据 [`../architecture-remediation-spec.md`](../architecture-remediation-spec.md) 和已确认的全链路 source edit 语义整理的 9 个实施工单草稿。工单按依赖顺序编号；每张工单的 `Blocked by` 是唯一阻塞信息源。

1. [让启动失败保留根因并可靠释放资源](001-lifecycle-failure-contract.md)
2. [让 MinerU 配置能力与真实执行能力一致](002-mineru-capability-contract.md)
3. [让解析与导出发布可恢复且不产生幻影结果](003-publication-reconciliation.md)
4. [让所有下游消费同一个有效文档快照](004-effective-document-snapshot.md)
5. [让原文编辑和工作台浏览器契约可用](005-source-editor-browser-contract.md)
6. [让 QA 会话、流式重试和引用可验证](006-qa-stream-citation-contract.md)
7. [让进程重启后的任务状态明确且可重试](007-task-restart-contract.md)
8. [让资产完整性和孤立文件可收敛](008-asset-integrity-reconciliation.md)
9. [深化持久化与解析模块的 variation seams](009-persistence-parser-deepening.md)

当前仅生成仓库内文档草稿；issue tracker 尚未配置。发布到外部 tracker 前需运行 `/setup-matt-pocock-skills`，并保持本目录作为规格索引而非第二套验收定义。
