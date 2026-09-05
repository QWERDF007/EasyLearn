# 实机运行

## 入口

Python 解释器默认值与启动参数以 [run.ps1](../deploy/run.ps1) 为准；配置字段、环境变量前缀和默认限额以 [Settings](../src/easylearn/config.py) 为准。依赖版本见 [pyproject.toml](../pyproject.toml)。

准备 PostgreSQL 数据库后，使用 [TOML 示例](../config.example.toml) 或 [YAML 示例](../config.example.yaml) 创建仓库根目录 `config.toml`、`config.yaml` 或 `config.yml`，填写实际连接；示例连接仅用于隔离的本机开发实例，不是生产认证配置。

文件选择、覆盖顺序和相对路径规则以 [Settings 配置入口](../src/easylearn/config.py) 为准；格式和嵌套字段示例见 [配置行为测试](../tests/config/test_settings.py)。如使用环境变量保存本机连接，可参考 [.env.example](../.env.example)。

在仓库根目录执行：

```powershell
.\deploy\run.ps1 -Action migrate
.\deploy\run.ps1 -Action web
```

指定其他位置的配置文件：

```powershell
.\deploy\run.ps1 -Action migrate -ConfigPath D:\Project\EasyLearn\config.toml
.\deploy\run.ps1 -Action web -ConfigPath D:\Project\EasyLearn\config.toml
```

直接运行 Python 入口时，设置进程环境变量 `EASYLEARN_CONFIG` 指向同一文件。应用与迁移读取同一个 Settings，不另建数据库配置入口。

API 文档由运行中的 `/docs` 和 `/openapi.json` 提供。存活与就绪端点见 [HTTP 应用](../src/easylearn/main.py)。前台进程保持运行期间才可访问；结束进程后不保证由外部工具启动的子进程继续存活。

## 依赖部署约束

应用采用本机 Python 进程，不使用 Docker。MinerU 仅作为独立服务接入，本项目不安装 MinerU 包或启动 MinerU 服务。Redis、转换服务、LLM 与索引能力的最终部署及验收合同见[产品规格第 20 节](FastAPI_MinerU方案与交互示例_v2/FastAPI_MinerU完整开发方案_v2.md#20-部署拓扑与配置契约)。

所有访问资产的进程必须使用同一存储根目录及有效读写权限；禁止为不同进程配置互不相通的私有资产目录。

## 模型与 LLM 接入

[TOML](../config.example.toml) / [YAML](../config.example.yaml) 提供等价的模型登记、MinerU 解析、聊天/Embedding profile 和用途路由示例。将占位服务地址、LLM 名称、能力及预算替换为实际部署值；外部 API 的填写位置和密钥环境变量也在示例中。模型参数与能力约束的唯一类型源是 [推理配置](../src/easylearn/inference/config.py)，MinerU 解析参数复用[协议类型](../src/easylearn/mineru/schema.py)。路径解析和覆盖语义见 [Settings](../src/easylearn/config.py)。

模型目录登记不等于部署服务；应用不据此下载模型、检查权重完整性或启动推理进程。已有 MinerU VLM 模型的本机路径与固定 revision 见示例的 `local_models.mineru_vlm`，下载缓存位于 `D:\Models\.cache\huggingface`；官方来源对应仓库内 [MinerU 模型声明](../3rdparty/MinerU/mineru/utils/enum_class.py)。独立服务自身使用本机权重的契约见[上游模型来源说明](https://opendatalab.github.io/MinerU/usage/model_source/)，不修改用户级全局模型配置；下载与验证证据见 [WORKLOG](../WORKLOG.md)。

## 针对性测试

接口测试使用真实 PostgreSQL，连接设置及隔离数据库生命周期见 [测试 fixture](../tests/api/conftest.py)。每次运行只提供当前模块的测试路径；测试会创建并删除自己随机命名的数据库，不修改已有业务数据库。

用户论文的 HTTP 预览测试入口见 [test_live_http.py](../tests/api/test_live_http.py)，通过 `EASYLEARN_ACCEPTANCE_PDF` 指定只读样本。测试内冻结样本摘要，未配置时跳过；该用例仅验证预览阶段，不代表全功能验收。

当前实现与未完成项的唯一记录在 [WORKLOG](../WORKLOG.md)；本页不重复维护功能完成状态。
