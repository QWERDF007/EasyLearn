# 006: 让 QA 会话、流式重试和引用可验证

## What to build

建立 QA provider 的可观察协议：session capability 为 active、inactive 或 unknown；上下文恢复、流式重试和 citation token 均由一个 adapter 负责。答案只能产生一次连续文本，引用必须映射到本次冻结的 evidence。

## Acceptance criteria

- [x] session inactive 或 unknown 且无法保证上下文连续时，follow-up 自动带上完整有效文档上下文。
- [x] 首个 answer delta 之前的传输失败可以按策略重试，重试不会留下重复的用户可见请求记录。
- [x] 已产生部分 delta 后失败时，不追加第二个答案前缀；系统要么按 provider 能力续传，要么明确失败。
- [x] citation 使用保留语法，只有合法且范围内的引用标记才转为结构化 citation。
- [x] 越界保留引用报告为协议错误；普通方括号数字、年份和公式索引不被误判为 citation。
- [x] 每个有效 citation 指向本次 QA 使用的冻结 evidence block，并能被浏览器定位。
- [x] 定向测试覆盖 unknown session、provider session 消失、首 delta 前失败、部分 delta 后失败和 citation 边界。

## Blocked by

- 004: 让所有下游消费同一个有效文档快照

## Parent

- Architecture reliability remediation spec
