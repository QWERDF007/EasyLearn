# 001: 让启动失败保留根因并可靠释放资源

## What to build

让应用生命周期在启动、正常运行、取消和关闭四种路径上拥有明确的资源 acquisition、rollback 和 cleanup 契约。用户看到的必须是最初的启动或关闭失败，而不是被清理异常覆盖的二次错误；修复配置后，进程必须可以立即重新启动。

## Acceptance criteria

- [x] 数据库、实例锁、日志控制器、任务管理器和 HTTP 资源按实际获取结果进行条件清理。
- [x] 任一启动步骤失败时，外层异常保留原始异常类型和消息；清理失败只作为次级诊断记录。
- [x] 数据库打开失败、应用启动失败和正常关闭均不会留下仍持有的连接、锁或后台任务。
- [x] 正常启动和关闭仍执行配置的 graceful-shutdown budget，不新增第二套超时值。
- [x] 同一数据目录在一次启动失败后可以立即再次启动。
- [x] 定向生命周期测试覆盖正常路径、启动中途失败和关闭中途失败。

## Blocked by

- None.

## Parent

- Architecture reliability remediation spec
