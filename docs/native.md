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

`data_dir` 首次启动时自动创建 `app.db`、文档、导出和临时目录。日志目录由 `files.log_dir` 配置，默认是仓库根目录的 `logs`；每个本地日期写入一个 `YYYY-MM-DD.log` 文件，不按大小轮转。实例锁阻止两个进程同时使用同一个 `data_dir`。成功结果、译文、修订和已完成问答保留；进程退出时未完成任务丢弃，重新提交即可。

## MinerU 与可选能力

项目通过 `src/easylearn/mineru/embedded.py` 调用仓库内 `3rdparty/MinerU` 的 Python API，在当前服务进程内使用 `transformers` 后端加载 `[mineru].model_path`。默认模型路径为 `D:/Models/MinerU2.5-Pro-2605-1.2B`；不需要 `mineru` 命令、MinerU API 服务或单独启动进程。模型首次解析时加载并复用，服务关闭时释放。

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

浏览器端到端测试位于 [tests/browser/test_upload.py](../tests/browser/test_upload.py)，依赖 Selenium 和 ChromeDriver。Selenium Manager 默认自动匹配本机 ChromeDriver；需要固定驱动时设置 `EASYLEARN_CHROMEDRIVER`。显式运行：

```powershell
$env:EASYLEARN_RUN_BROWSER_TESTS = "1"
E:\Softwares\Anaconda3\envs\learn\python.exe -m pip install -e ".[browser]"
E:\Softwares\Anaconda3\envs\learn\python.exe -m pytest tests\browser -q -p no:cacheprovider --basetemp .tmp-browser
```
