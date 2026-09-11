# 008: 让资产完整性和孤立文件可收敛

## What to build

在 publication/reconciliation 契约之上，为解析结果中的图片、表格资产和导出附件建立 identity/checksum 检查。缺失、变更或孤立资产必须可识别、可重建或可安全清理，不通过静默降级掩盖结果损坏。

## Acceptance criteria

- [x] 已发布结果引用的资产都有可验证的 identity 和 checksum/等价完整性证据。
- [x] 资产缺失或校验失败时，结果状态表达为损坏/不可下载，而不是继续返回误导性的成功状态。
- [x] reconciliation 能区分仍被 active/result manifest 引用的资产和真正孤立的资产。
- [x] 重复 reconciliation 幂等，不删除仍被任何有效结果引用的资产。
- [x] 导出下载在资产损坏时返回稳定的领域错误，并保留可诊断信息。
- [x] 定向测试覆盖缺失资产、checksum mismatch、重复清理和旧版本结果引用。

## Blocked by

- 003: 让解析与导出发布可恢复且不产生幻影结果

## Parent

- Architecture reliability remediation spec
