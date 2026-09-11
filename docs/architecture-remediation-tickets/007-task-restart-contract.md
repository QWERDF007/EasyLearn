# 007: 让进程重启后的任务状态明确且可重试

## What to build

为内存任务队列建立明确的 restart contract。第一阶段不伪造任意流式任务的恢复能力：进程重启后，未完成任务必须被识别为 interrupted/unknown，并向用户提供安全重试入口；已发布结果不能被重复发布。

## Acceptance criteria

- [x] 应用重启后，用户不会看到一个仍在运行但永远不会推进的任务。
- [x] 重启前未完成的 parse、translation、QA 和 export 任务都有稳定的 interrupted 或 equivalent terminal state。
- [x] 任务详情说明该状态是否可重试；重试创建新的 task identity，不覆盖原任务历史。
- [x] 已进入 published/recorded 的结果在任务重试时不会重复发布或改变 active pointer。
- [x] 取消、关闭和重启路径与任务 retention policy 一致，不泄漏后台协程。
- [x] 定向测试覆盖重启前后任务可见性、重试和已发布结果幂等性。

## Blocked by

- 001: 让启动失败保留根因并可靠释放资源
- 003: 让解析与导出发布可恢复且不产生幻影结果

## Parent

- Architecture reliability remediation spec
