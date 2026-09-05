# 实机运行

## 入口

Python 解释器默认值与启动参数以 [run.ps1](../deploy/run.ps1) 为准；配置字段、环境变量前缀和默认限额以 [Settings](../src/easylearn/config.py) 为准。依赖版本见 [pyproject.toml](../pyproject.toml)。

准备 PostgreSQL 数据库后，将 [.env.example](../.env.example) 复制为仓库根目录 `.env` 并填写实际连接；示例连接仅用于隔离的本机开发实例，不是生产认证配置。

在仓库根目录执行：

```powershell
.\deploy\run.ps1 -Action migrate
.\deploy\run.ps1 -Action web
```

API 文档由运行中的 `/docs` 和 `/openapi.json` 提供。存活与就绪端点见 [HTTP 应用](../src/easylearn/main.py)。前台进程保持运行期间才可访问；结束进程后不保证由外部工具启动的子进程继续存活。

## 依赖部署约束

应用采用本机 Python 进程，不使用 Docker。MinerU 仅作为独立服务接入，本项目不安装 MinerU 包或模型。Redis、转换服务、LLM 与索引能力的最终部署及验收合同见[产品规格第 20 节](FastAPI_MinerU方案与交互示例_v2/FastAPI_MinerU完整开发方案_v2.md#20-部署拓扑与配置契约)。

所有访问资产的进程必须使用同一存储根目录及有效读写权限；禁止为不同进程配置互不相通的私有资产目录。

## 针对性测试

接口测试使用真实 PostgreSQL，连接设置及隔离数据库生命周期见 [测试 fixture](../tests/api/conftest.py)。每次运行只提供当前模块的测试路径；测试会创建并删除自己随机命名的数据库，不修改已有业务数据库。

当前实现与未完成项的唯一记录在 [WORKLOG](../WORKLOG.md)；本页不重复维护功能完成状态。
