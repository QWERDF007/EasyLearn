# 004: 让所有下游消费同一个有效文档快照

## What to build

建立 effective document snapshot 作为文档内容的唯一下游输入。它由不可变 base IR、已持久化 source edit overlays 和稳定 source revision/fingerprint 派生；翻译、QA 和导出不得各自选择基础 IR 或覆盖后的 IR。

## Acceptance criteria

- [x] 阅读、翻译、QA evidence 和导出均读取同一个 effective snapshot。
- [x] snapshot identity 同时包含 parse identity 和有序 source overlay 的稳定 revision/fingerprint。
- [x] base IR JSON 保持不可变，source edit 仍作为独立持久化 overlay 应用。
- [x] source edit 后，旧翻译文本和历史仍可审计，但不再被无条件当作当前翻译。
- [x] 翻译结果与生成它的 source fingerprint 关联；source 变化后 UI/API 能表达 stale 或需要 review/retranslation。
- [x] QA prompt 和导出内容包含 source edit 后的有效文本，而不是旧的 base IR。
- [x] 定向行为测试验证一次 source edit 对 reader、translation、QA 和 export 的一致影响。

## Blocked by

- Source edit 全链路传播语义已由规格确认，不再等待产品决策。

## Parent

- Architecture reliability remediation spec
