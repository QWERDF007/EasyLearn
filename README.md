# EasyLearn

EasyLearn 是轻量级本地学术文档阅读与 AI 解读工作台。单进程架构，内嵌 MinerU 高性能文档解析、双语排版对照阅读、异步批量翻译及多轮 AI 深度解读追问。

[文档与实现索引](docs/README.md) · [实机运行指南](docs/native.md) · [任务账本](WORKLOG.md)

---

## 环境要求

- **Python**：3.10+（推荐使用 Python 3.12 / Conda 环境）
- **Node.js**（可选）：18.0+（若使用仓库内嵌的 `3rdparty/FreeDeepseekAPI-ZH` 网页端免费代理）
- **GPU 权重**（可选）：MinerU 本地模型权重（用于本地文档高精解析）

---

## 安装说明

### 1. 克隆仓库与安装依赖

```powershell
git clone <repo-url>
cd EasyLearn

# 安装项目与开发依赖
pip install -e ".[dev]"
```

### 2. 初始化配置文件

从模板复制并生成本地配置文件：

```powershell
Copy-Item .\config.example.toml .\config.toml
```

> [!NOTE]
> 配置项的唯一真相源定义位于 [src/easylearn/config.py](src/easylearn/config.py)，示例说明见 [config.example.toml](config.example.toml)。
> 按需调整 `config.toml` 中的数据目录（`app.data_dir`）、MinerU 模型路径（`mineru.models`）与 LLM 接口（`llm`）。

---

## 使用说明

### 1. 启动服务

在仓库根目录下执行：

```powershell
python -m easylearn
```

或者显式指定配置文件启动：

```powershell
python -m easylearn --config .\config.toml
```

默认在浏览器打开或手动访问：`http://127.0.0.1:8765`。服务为单进程异步架构，在前台终端按 `Ctrl + C` 即可正常退出。

---

### 2. （可选）使用内置 FreeDeepseekAPI 代理

如果使用 DeepSeek Web 网页端作为大模型后端：

```powershell
cd 3rdparty/FreeDeepseekAPI-ZH
npm install
npm run auth      # 完成 DeepSeek 网页端账号授权
node server.js    # 启动代理服务（监听 127.0.0.1:9655）
```

随后在 `config.toml` 中配置：
```toml
[llm]
active_provider = "deepseek"

[llm.providers.deepseek]
base_url = "http://127.0.0.1:9655/v1"
model = "deepseek-chat"
qa_model = "deepseek-reasoner"
api_key = "sk-freedeepseek"
local_only = true
```

---

### 3. 工作台主要操作

1. **文档导入**：在首页上传 PDF 或文档文件。
2. **结构解析**：点击“解析”，调用内置 MinerU 进行版面分析，提取段落、表格与 LaTeX 公式。
3. **双语阅读与编辑**：
   - 切换原文、译文或中英对照视图；
   - 支持单段翻译、整篇批量翻译与人工译文修订/锁定。
4. **AI 智能解读**：
   - 支持全篇全局脉络总结；
   - 选定重点段落发起追问，大模型将自动融合全文背景与焦点证据进行深度解读。

---

## 验证与测试

```powershell
pytest tests/v3 -q
```
