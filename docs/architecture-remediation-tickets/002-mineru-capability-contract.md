# 002: 让 MinerU 配置能力与真实执行能力一致

## What to build

收敛 MinerU backend capability 合同。配置、健康状态和任务 admission 只能暴露生产组合根真正可以执行的 backend；不支持的值必须在排队前返回稳定、可行动的领域错误。

## Acceptance criteria

- [x] 当前可执行 backend 的配置值、健康状态和 admission 结果一致。
- [x] 不支持的 backend 在任务创建前被拒绝，不产生排队中的伪任务或延迟失败任务。
- [x] 支持的 backend 仍可以进入真实解析流程，并沿用现有任务进度和失败语义。
- [x] backend 不可用、模型路径缺失和输入格式不支持分别返回可区分的错误。
- [x] 能力元数据来自真实 adapter 接线，不以 schema enum 单独宣称能力。
- [x] 定向 API、健康检查和解析任务测试覆盖支持与不支持两条路径。

## Blocked by

- None.

## Parent

- Architecture reliability remediation spec
