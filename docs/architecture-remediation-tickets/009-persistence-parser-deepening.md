# 009: 深化持久化与解析模块的 variation seams

## What to build

在前八项行为合同稳定后，按真实变化轴深化 persistence 和 parser 模块，减少路由、任务执行器和具体 SQL 对存储细节的耦合。该票只做有明确 leverage 的结构重构，不改变已经确认的外部行为。

## Acceptance criteria

- [x] 解析发布、文档目录、source edit、translation 和 task lease 等真实领域操作分别拥有小而深的持久化 interface；调用方不再拼接同一组 SQL 规则。
- [x] 每个新 interface 至少隐藏一项事务、错误映射、identity 或 reconciliation 复杂度，而不是增加 pass-through wrapper。
- [x] ParseService 按真实变化轴拆分输入预检、backend 执行、结果归一化和发布，不把一次流程拆成无语义的 helper。
- [x] 下一次 schema 变更使用可追踪、可回滚的 versioned migration；不引入没有实际 schema 需求的通用迁移框架。
- [x] DocumentIR provenance、source fingerprint 和 publication identity 的字段来源保持单一，不在文档、前端和 SQL 中维护第二套定义。
- [x] 既有 API、任务状态、下载路径和浏览器 smoke 行为无回归，并有针对真实接口的定向测试证据。
- [x] 重构后删除已经没有调用者的旧 SQL、旧路径和兼容分支。

## Blocked by

- 003: 让解析与导出发布可恢复且不产生幻影结果
- 004: 让所有下游消费同一个有效文档快照
- 005: 让原文编辑和工作台浏览器契约可用
- 006: 让 QA 会话、流式重试和引用可验证

## Parent

- Architecture reliability remediation spec
