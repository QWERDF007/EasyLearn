# Pinaic OpenAI 兼容 API 外部事实

## 范围

本文只记录 Pinaic 官方《API 密钥与客户端配置教程》第 5 节“OpenAI 兼容 API 调用说明”明确给出的接口事实，以及该官方页面中直接出现的相关信息。没有把项目现有配置或实现逻辑复制到本文；页面未说明的协议不作推断。

## 已确认事实

### Base URL

Pinaic 页面给出的 OpenAI 兼容 API 地址是：

```text
https://api.pinaic.com/v1
```

页面同时把 `https://api.pinaic.com/` 称为生产默认 API Base URL，并说明在 OpenAI 兼容 SDK 或工具中通常填写带 `/v1` 的地址。这里的“通常”是页面原文口径；页面没有给出其他文本模型 Base URL。

### 已明确的请求路径

第 5 节明确给出的调用是只读额度接口：

```text
GET https://api.pinaic.com/v1/usage
```

页面说明该接口返回当前 API Key 对应的余额、额度和用量统计，不发起模型请求。第 5 节没有明确给出文本模型的 `chat/completions`、`responses` 或其他生成请求路径；因此不能仅依据该页面确认这些路径中的任一个。

同一节还写明：如果当前使用的 API Base URL 是 `https://api.pinaic.com/`，额度查询示例对应为 `https://api.pinai.icu/v1/usage`。页面只在额度查询示例中这样写，没有把 `api.pinai.icu` 确认为文本模型 Base URL；不能据此替换上面的 OpenAI 兼容 Base URL。

页面的其他章节另行给出了图片接口路径，但它们不属于第 5 节的通用文本模型调用事实：

```text
POST https://api.pinaic.com/v1/images/generations
POST https://api.pinaic.com/v1/images/edits
POST https://api.pinaic.com/v1/files
```

### `model` 字段

第 5 节没有定义文本模型请求体，也没有说明文本调用的 `model` 字段是否必填、字段值从哪里取得或可用模型名是什么。页面第 6 节的图片示例使用 `model: "gpt-image-2"`，这是图片接口示例，不能作为通用文本模型的模型名或字段约束。

### 认证头

页面推荐使用以下 HTTP 认证头：

```http
Authorization: Bearer <你的 PinAI API 密钥>
```

页面明确要求不要把 API Key 放进 URL 查询参数，并说明网关会拒绝查询参数中的 `key` 或 `api_key`。

### SDK 调用方式

页面明确提到可以在“OpenAI 兼容 SDK 或工具”中填写上面的 `base_url`，但没有给出 SDK 包名、初始化代码、方法名、请求体 schema 或独立 SDK/API 参考链接。因此，从该官方页面能够确认的 SDK 接入方式只有：将 SDK 的服务地址配置为 `https://api.pinaic.com/v1`，并按页面给出的 Bearer 头注入 PinAI API Key；具体文本生成调用方式需要 Pinaic 另行提供的官方 API 说明或实际接口契约，本文不补写推测代码。

### 密钥注入注意事项

- API Key 需要处于启用状态，并绑定对应的平台分组；页面还要求余额、订阅、额度、并发、IP 限制和上游账号满足请求条件。
- API Key 不放在 URL 查询参数中，只通过 `Authorization: Bearer ...` 传递。
- 页面说明密钥完整值通常只显示一次，应立即妥善保存。
- 页面要求对外只提供 PinAI API Key，不提供上游 OpenAI、OpenRouter、OAuth、Refresh Token 或其他账号凭据。
- 页面生成的配置脚本内含当前 API Key；脚本、剪贴板、备份、本机配置和包含密钥的截图都应按敏感凭据保护，不上传到聊天、工单、网盘或代码仓库。
- 本研究文件不包含实际 API Key。

## 来源

1. [Pinaic 官方首页指南](https://app.pinaic.com/docs/home-guide)，《PinAI API 密钥与客户端配置教程》/“5. OpenAI 兼容 API 调用说明”；页面显示发布版本 15，发布时间为 `2026-07-14T23:37:16.230717+08:00`。
2. [Pinaic 官方页面内容接口](https://app.pinaic.com/api/v1/docs/home-guide?limit=8)，为上述页面加载的官方正文数据源；仅用于复核页面原文。
3. [页面明确出现的 Pinaic API Base URL](https://api.pinaic.com/v1)。该链接是页面中给出的官方 API 地址，不是独立的 SDK/API 参考文档。

截至上述官方页面内容，未发现第 5 节明确链接的独立文本 API 或 SDK 参考页；因此本文将未说明的文本请求路径、文本模型名和 SDK 方法保持为未确认状态。
