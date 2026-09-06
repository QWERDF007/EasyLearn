# 本机运行

应用是单个 FastAPI/Uvicorn 进程，使用 SQLite、本地文件和进程内任务队列。不会启动或要求 PostgreSQL、Redis、Dramatiq、Docker 或 Nginx。配置字段的唯一来源是 [config.py](../src/easylearn/config.py)，示例见 [config.example.toml](../config.example.toml)。

## 安装与启动

在仓库根目录执行：

```powershell
D:\Software\anaconda3\envs\learn\python.exe -m pip install -e ".[dev]"
Copy-Item .\config.example.toml .\config.toml
.\deploy\run.ps1 -ConfigPath .\config.toml
```

也可以直接启动：

```powershell
D:\Software\anaconda3\envs\learn\python.exe -m easylearn --config .\config.toml
```

默认地址由配置中的 `app.host` 和 `app.port` 决定，通常是 `http://127.0.0.1:8765`。结束前台进程即可停止服务；不要使用 Uvicorn 多 worker 或 reload，因为任务状态只属于当前进程。

`data_dir` 首次启动时自动创建 `app.db`、文档、导出、临时文件和日志目录。实例锁阻止两个进程同时使用同一个 `data_dir`。成功结果、译文、修订和已完成问答保留；进程退出时未完成任务丢弃，重新提交即可。

## MinerU 与可选能力

项目不安装 MinerU。`[mineru]` 可以配置已有 MinerU API，或配置已有命令的完整路径以按任务调用 CLI；命令由参数数组直接执行，不经过 shell。API 模式下填写 `base_url`，密钥通过 `api_key_env` 指向环境变量。

LLM 使用 `[llm]` 的 OpenAI-compatible 地址。`local_only = true` 时，非本机地址会被拒绝；密钥只从 `api_key_env` 指定的环境变量读取。Office 转换和问答分别由 `[extensions]` 中的开关控制，关闭时不会检查对应外部组件。

## 主要接口

- `/api/health`：服务、MinerU 和扩展能力状态。
- `/api/documents`：上传、列表、收藏、删除和文档详情。
- `/api/documents/{id}/parse`、`translate`、`exports`、`qa`：创建内存任务。
- `/api/tasks/{task_id}`：轮询进度、结果和错误；`/cancel` 请求取消。
- `/api/documents/{id}/parses/{parse_id}`：读取固定解析版本及 Markdown。
- `/api/documents/{id}/files/{file_id}`：读取服务端映射的原件、预览、JSON、图片和导出文件。

完整接口和验收范围见 [v3 方案](FastAPI_MinerU轻量完整开发方案_v3.md)。

## 验证

使用指定环境运行定向测试：

```powershell
D:\Software\anaconda3\envs\learn\python.exe -m pytest tests\v3 tests\document_ir tests\mineru -q -p no:cacheprovider
D:\Software\anaconda3\envs\learn\python.exe -m ruff check src tests migrations
D:\Software\anaconda3\envs\learn\python.exe -m mypy src\easylearn
```
