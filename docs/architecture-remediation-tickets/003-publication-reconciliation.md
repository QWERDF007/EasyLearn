# 003: 让解析与导出发布可恢复且不产生幻影结果

## What to build

把文件生成、manifest/资产校验和 SQLite 状态更新收敛为可重试的 publication/reconciliation 操作。用户只能看到同时具备可下载文件和持久化记录的结果；异常中断的 staging 目录、半发布目录和孤立数据库记录必须能被幂等收敛。

## Acceptance criteria

- [x] 解析和导出产物遵循 staged、validated、published、recorded 的可观察状态语义。
- [x] 文件移动失败、数据库提交失败、任务取消和进程中断分别产生明确结果，不把临时产物标成可用结果。
- [x] 发布成功后刷新、下载和任务详情引用同一个 durable artifact identity。
- [x] 发布失败不会创建用户可见的 phantom parse/export；已有旧的 active 结果不被半成品覆盖。
- [x] 启动或显式维护触发的 reconciliation 可重复执行，重复执行不会重复发布、删除有效文件或制造新记录。
- [x] 文档删除在文件暂时不可删除时保留可重试意图，并最终使数据库和文件状态收敛。
- [x] 定向测试覆盖发布各阶段失败、取消前后和重复 reconciliation。

## Blocked by

- None.

## Parent

- Architecture reliability remediation spec
