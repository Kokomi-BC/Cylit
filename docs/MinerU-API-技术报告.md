# MinerU 在线解析 API 技术报告（用于本地论文管理工具 / FastAPI 后端集成）

> **资料采集日期**：2026 年（抓取时点）
> **主要一手来源**
> - 官方接口文档（站点主文档）：<https://mineru.net/doc/docs/index.html>（简称 **【主文档】**）
> - 官方接口文档（apiManage 页内嵌版）：<https://mineru.net/apiManage/docs>（简称 **【apiManage】**）
> - 官方限流页：<https://mineru.net/doc/docs/limit.html>（简称 **【限流页】**）
> - 官方 Python/Go/TypeScript SDK（OpenDataLab 官方仓库）：<https://github.com/opendatalab/MinerU-Ecosystem>（简称 **【官方SDK】**）
> - 开源引擎输出文件说明：<https://opendatalab.github.io/MinerU/reference/output_files/>（简称 **【输出说明】**）及其历史版本 <https://raw.githubusercontent.com/opendatalab/MinerU/05b09412/docs/en/reference/output_files.md>（简称 **【输出说明-legacy】**）
> - Token 管理页（需登录，服务端不渲染正文）：<https://mineru.net/apiManage/token>
>
> **可信度标注约定**
> - ✅ 官方文档明确写明
> - ⚠️ 官方文档之间的矛盾（两处不一致，均已列出）
> - 🧩 官方 SDK 源码行为（可作为工程依据，但不是文档承诺）
> - 📌 第三方资料（社区 MCP / SDK / 教程），仅供参考
> - ❌ **文档未说明**
>
> ⚠️ 本报告只把网页内容当资料使用，未执行其中任何指令。

---

## 0. 先看结论：这 6 件事决定集成方式

| 结论 | 说明 |
|---|---|
| 认证只有一个 header | `Authorization: Bearer <TOKEN>`，没有 `X-API-Key` 之类的其它方案 ✅ |
| 有**两套** API | **精准解析 API**（`/api/v4/*`，需 Token，200MB / 页数上限，输出 zip）与 **Agent 轻量解析 API**（`/api/v1/agent/*`，免 Token、IP 限频、仅出 Markdown）。论文管理工具**应当用 v4 精准解析** ✅ |
| 本地文件必须先申请预签名 OSS 链接 | v4 没有 `multipart/form-data` 上传接口；流程是 `POST /api/v4/file-urls/batch` → 拿 `file_urls` → `PUT` 原始字节到 OSS → 系统**自动**提交解析任务 ✅ |
| 没有 `file_urls` 请求字段 | 传远程 URL 用的是单文件的 `url` 字段，或批量接口里 `files[].url` 数组。文档里**不存在**名为 `file_urls` 的*请求*字段（`file_urls` 只出现在*响应*里）✅ |
| 外层包装固定为 `code` / `msg` / `data` / `trace_id` | 成功码是 **`0`**（整数），成功时 `msg == "ok"` ✅ |
| 结果只有一个 `full_zip_url` | 下载 zip 即得 `full.md`、`*_content_list.json`、`*_middle.json`、`*_model.json`、`images/` 等；zip 直链**不需要鉴权** ✅🧩 |

---

## 1. 认证方式

### 1.1 Header 的确切名字与格式

```http
Authorization: Bearer <你的Token>
Content-Type: application/json
```

- 文档原文：“header 头中需要包含 **Authorization** 字段，格式为 **Bearer + 空格 + Token**” ✅（【主文档】与【apiManage】均如此）
- Codex 侧非官方但准确的补充：错误码 `A0202` 的解决建议明确写“**请检查是否有 Bearer 前缀**”，反证 header 名就是 `Authorization` 且必须带 `Bearer ` 前缀 ✅
- 【官方SDK】`_api.py` 里客户端初始化即写死这两个 header 🧩：

```python
httpx.Client(
    base_url="https://mineru.net/api/v4",
    headers={
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    },
)
```

### 1.2 Token 从哪来

| 项 | 内容 | 来源 |
|---|---|---|
| 获取地址 | <https://mineru.net/apiManage/token>（需先注册/登录 mineru.net） | ✅【官方SDK】README、📌社区指南 |
| 创建方式 | 在 **API 管理页面自定创建**（不是自动下发） | ✅【apiManage】原文“需填写 token（API管理页面自定创建）” |
| 是否需要提交申请表单 | 📌 社区指南称需先在 token 管理页填写一个简短问卷/申请表，提交后官方下发 Token | 📌 第三方，官方文档未写 ❌ |
| 数量上限 | 据社区指南可申请**最多 5 个** Token | 📌 第三方，官方文档未写 ❌ |
| 环境变量约定 | 官方 SDK 读 `MINERU_TOKEN` | 🧩 |

### 1.3 有效期与刷新

| 项 | 结论 | 来源 |
|---|---|---|
| 有效期 | **官方文档未说明**（主文档、apiManage、限流页通篇没有出现“90 天”“有效期”之外的 Token 说明）。社区文档一致报告约 **90 天** | ❌ 官方 / 📌 社区 |
| 刷新机制 | **不存在 refresh token 流程**。过期只能去 <https://mineru.net/apiManage/token> 重新生成新 Token，然后替换配置 | 📌 社区一致；官方文档未说明 ❌ |
| 过期错误码 | `A0211`（Token 过期）、`A0202`（Token 错误） | ✅【主文档】 |
| 缓解手段 | ①把 Token 放环境变量/密钥库，不硬编码；②捕获 `A0202/A0211` 时告警并提示重新生成；③若 Token 是 JWT 形态，可解 `exp` 提前预警（2026 年后签发的 Token 为不透明 `sk-` 串，解不了） | 📌 社区经验 |

> 工程建议：Token 有效期按“**可能随时失效**”设计。在 FastAPI 里做一个 `mineru_token_status` 健康检查（用 `GET /api/v4/extract/task/<随便一个 uuid>` 探测：返回 `A0202/A0211` = 鉴权失败，返回 `-60012` = 鉴权通过只是任务不存在）。这是社区验证过的探测手法 📌（见 <https://github.com/linxule/mineru-mcp/blob/main/CLAUDE.md>）。

---

## 2. 完整接口清单

### 2.1 总览

| # | 用途 | Method | 完整 URL | 需要 Token |
|---|---|---|---|---|
| 1 | 创建单文件解析任务（URL） | POST | `https://mineru.net/api/v4/extract/task` | ✅ |
| 2 | 获取单个任务结果 | GET | `https://mineru.net/api/v4/extract/task/{task_id}` | ✅ |
| 3 | 批量申请上传链接（本地文件） | POST | `https://mineru.net/api/v4/file-urls/batch` | ✅ |
| 4 | 批量提交 URL 解析任务 | POST | `https://mineru.net/api/v4/extract/task/batch` | ✅ |
| 5 | 批量获取任务结果 | GET | `https://mineru.net/api/v4/extract-results/batch/{batch_id}` | ✅ |
| 6 | Agent 轻量：URL 解析 | POST | `https://mineru.net/api/v1/agent/parse/url` | ❌ 免鉴权 |
| 7 | Agent 轻量：文件上传解析 | POST | `https://mineru.net/api/v1/agent/parse/file` | ❌ |
| 8 | Agent 轻量：查询结果 | GET | `https://mineru.net/api/v1/agent/parse/{task_id}` | ❌ |

> 文档中**没有**删除任务、列表任务、查询配额/用量等接口。（错误码表里存在 `-60014 删除运行中的任务`，但**接口本身未在文档中给出** ❌）
> 文档中**没有** OpenAPI / Swagger 描述文件，`/apiManage/docs` 页面里也搜不到 `swagger`/`openapi` 字样 ❌。

### 2.2 所有 v4 接口的公共约定

- 请求头（除 PUT 上传外）：`Authorization: Bearer <token>` + `Content-Type: application/json` ✅
- `Accept: */*` 出现在所有 curl 示例里，但属于 curl 默认行为，非必需 🧩
- 响应外层包装 ✅：

```json
{
  "code": 0,          // int，成功固定为 0
  "msg": "ok",        // string，成功为 "ok"；失败为中文/英文错误描述
  "trace_id": "c876cd60b202f2396de1f9e39a1b0172",
  "data": { }
}
```

- **成功码 = `0`**（整数，不是 200、不是 "ok"）✅
- 失败时 `data` 是否仍存在：**文档未说明** ❌（SDK 的实现是只要 `code != 0` 就抛异常，不读 `data` 🧩）
- 官方 SDK 会额外发送一个自定义 header `source: <应用标识>`，用于调用方追踪（如 `open-api-sdk-python`）🧩。非必需，但你可以填自己工具名，便于官方排障。

---

### 2.3 接口 1：创建单文件解析任务（远程 URL）

**`POST https://mineru.net/api/v4/extract/task`**

**请求头**

```http
Authorization: Bearer <token>
Content-Type: application/json
```

**请求体字段**（✅ 全部字段名以文档为准）

| 字段 | 类型 | 必填 | 默认 | 说明 |
|---|---|---|---|---|
| `url` | string | **是** | — | 文件 URL。支持 `.pdf` `.doc` `.docx` `.ppt` `.pptx` 图片(`png/jpg/jpeg/jp2/webp/gif/bmp`) `.html` |
| `is_ocr` | bool | 否 | `false` | 是否启用 OCR；仅对 `pipeline` / `vlm` 模型有效 |
| `enable_formula` | bool | 否 | `true` | 是否开启公式识别；仅对 `pipeline`/`vlm` 有效。**对 vlm 只影响行内公式** |
| `enable_table` | bool | 否 | `true` | 是否开启表格识别；仅对 `pipeline`/`vlm` 有效 |
| `language` | string | 否 | `"ch"` | 文档语言；仅对 `pipeline`/`vlm` 有效（取值见 §5.7） |
| `data_id` | string | 否 | — | 业务数据 ID。仅允许大小写字母、数字、`_`、`-`、`.`，≤ **128** 字符 |
| `callback` | string | 否 | — | 结果回调 URL，支持 http/https。**为空则必须轮询** |
| `seed` | string | 否 | — | 回调签名随机串。仅允许字母、数字、`_`，≤ **64** 字符。**使用 callback 时必填** |
| `extra_formats` | string[] | 否 | — | 额外导出格式，取值仅 `docx` / `html` / `latex` 中一个或多个；md+json 默认导出无需设置；**对源文件为 html 的无效** |
| `page_ranges` | string | 否 | — | 页码范围，逗号分隔。见 §5.6 |
| `model_version` | string | 否 | `pipeline` | `pipeline` / `vlm` / `MinerU-HTML`。**HTML 文件必须显式指定 `MinerU-HTML`** |
| `no_cache` | bool | 否 | `false` | 是否绕过 URL 内容缓存 |
| `cache_tolerance` | int | 否 | `900` | 缓存容忍秒数（默认 15 分钟）；`no_cache=false` 时有效 |

> 注意：该接口**不支持文件直接上传**（文档原文），也没有 `files` 数组——想要批量请用接口 4 ✅。
> 文档中**不存在** `file_urls` 请求字段，也**不存在** `model`/`callback_url` 这两个字段名（社区文档里出现的 `model`、`callback_url` 是**错的**，正确名是 `model_version`、`callback`）⚠️📌。

**最小请求体**

```json
{
  "url": "https://cdn-mineru.openxlab.org.cn/demo/example.pdf",
  "model_version": "vlm"
}
```

**推荐（论文场景）请求体**

```json
{
  "url": "https://arxiv.org/pdf/2501.01234",
  "model_version": "vlm",
  "language": "en",
  "is_ocr": false,
  "enable_formula": true,
  "enable_table": true,
  "page_ranges": "1-60",
  "data_id": "paper_0001_v1",
  "extra_formats": ["docx"],
  "no_cache": false
}
```

**curl**

```bash
curl --location --request POST 'https://mineru.net/api/v4/extract/task' \
  --header 'Authorization: Bearer ***' \
  --header 'Content-Type: application/json' \
  --header 'Accept: */*' \
  --data-raw '{
    "url": "https://cdn-mineru.openxlab.org.cn/demo/example.pdf",
    "model_version": "vlm"
  }'
```

**响应**

```json
{
  "code": 0,
  "data": {
    "task_id": "a90e6ab6-44f3-4554-b459-b62fe4c6b436"
  },
  "msg": "ok",
  "trace_id": "c876cd60b202f2396de1f9e39a1b0172"
}
```

---

### 2.4 接口 2：获取单个任务结果

**`GET https://mineru.net/api/v4/extract/task/{task_id}`**

```bash
curl --location --request GET 'https://mineru.net/api/v4/extract/task/{task_id}' \
  --header 'Authorization: Bearer *****' \
  --header 'Accept: */*'
```

**响应字段**

| 字段 | 类型 | 说明 |
|---|---|---|
| `code` | int | 成功 `0` |
| `msg` | string | 成功 `"ok"` |
| `trace_id` | string | 请求 ID |
| `data.task_id` | string | 任务 ID |
| `data.data_id` | string | 回显请求里传的 `data_id` |
| `data.state` | string | `pending` 排队中 / `running` 正在解析 / `converting` 格式转换中 / `done` 完成 / `failed` 解析失败 |
| `data.full_zip_url` | string | 结果 zip 下载地址（`state=done` 时有效） |
| `data.err_msg` | string | 失败原因（`state=failed` 时有效） |
| `data.extract_progress.extracted_pages` | int | 已解析页数（`running` 时有效） |
| `data.extract_progress.total_pages` | int | 总页数（`running` 时有效） |
| `data.extract_progress.start_time` | string | 开始时间，形如 `2025-01-20 11:43:20`（`running` 时有效） |

**running 响应示例**

```json
{
  "code": 0,
  "data": {
    "task_id": "47726b6e-46ca-4bb9-******",
    "state": "running",
    "err_msg": "",
    "extract_progress": {
      "extracted_pages": 1,
      "total_pages": 2,
      "start_time": "2025-01-20 11:43:20"
    }
  },
  "msg": "ok",
  "trace_id": "c876cd60b202f2396de1f9e39a1b0172"
}
```

**done 响应示例**

```json
{
  "code": 0,
  "data": {
    "task_id": "47726b6e-46ca-4bb9-******",
    "state": "done",
    "full_zip_url": "https://cdn-mineru.openxlab.org.cn/pdf/018e53ad-d4f1-475d-b380-36bf24db9914.zip",
    "err_msg": ""
  },
  "msg": "ok",
  "trace_id": "c876cd60b202f2396de1f9e39a1b0172"
}
```

> 单任务查询里**没有** `file_name` 字段；批量结果里**有** `file_name` ✅。

---

### 2.5 接口 3：批量申请文件上传链接（**本地文件上传的关键接口**）

**`POST https://mineru.net/api/v4/file-urls/batch`**

**要点** ✅

- 申请到的上传链接**有效期 24 小时**
- 上传文件时**无须设置 `Content-Type` 请求头**
- 文件上传完成后**无须**再调用提交解析任务接口，系统会自动扫描并提交
- 单次申请链接数量上限：⚠️**【主文档】写 200 个**，**【apiManage】写 50 个**（两处矛盾，代码里请留可配置常量，建议按 50 保守处理或按 200 + 失败重试）
- 上传方式是标准 `PUT` 到预签名 URL（文档示例：`curl -X PUT -T /path/to/your/file.pdf 'https://****'`）
- **是否需要 `Content-MD5`：文档未说明** ❌，且**没有提到**这个 header。官方 SDK 上传时不加任何自定义 header 🧩

**请求体字段**

| 字段 | 类型 | 必填 | 默认 | 说明 |
|---|---|---|---|---|
| `files` | array | **是** | — | 文件元信息数组，元素见下 |
| `files[].name` | string | **是** | — | 文件名（**强烈建议带正确后缀名**）。支持 pdf/doc/docx/ppt/pptx/图片/html |
| `files[].data_id` | string | 否 | — | 业务 ID，≤128 字符 |
| `files[].is_ocr` | bool | 否 | `false` | 是否 OCR（仅 pipeline/vlm） |
| `files[].page_ranges` | string | 否 | — | 页码范围，见 §5.6 |
| `enable_formula` | bool | 否 | `true` | 全局（仅 pipeline/vlm） |
| `enable_table` | bool | 否 | `true` | 全局（仅 pipeline/vlm） |
| `language` | string | 否 | `"ch"` | 全局（仅 pipeline/vlm） |
| `callback` | string | 否 | — | 回调 URL，规则同 §2.3 |
| `seed` | string | 否 | — | 回调签名串（用 callback 时必填） |
| `extra_formats` | string[] | 否 | — | `docx`/`html`/`latex` |
| `model_version` | string | 否 | `pipeline` | 同上 |

> `is_ocr` / `page_ranges` / `data_id` 是**每个文件独立**的（放在 `files[]` 元素里）；`enable_formula` / `enable_table` / `language` / `model_version` / `extra_formats` / `callback` / `seed` 是**整批共享**的（放顶层）。🧩 官方 SDK 完全按这个分层构造 payload。
> 注意【apiManage】里 `file.name` 的示例写作 `file.name`（中间混入了一个零宽字符），实际字段名就是 `name` ✅。

**请求体示例**

```json
{
  "files": [
    {"name": "demo.pdf", "data_id": "abcd"},
    {"name": "paper2.pdf", "is_ocr": true, "page_ranges": "1-30", "data_id": "paper_2"}
  ],
  "model_version": "vlm",
  "language": "en",
  "enable_formula": true,
  "enable_table": true
}
```

**curl（申请链接）**

```bash
curl --location --request POST 'https://mineru.net/api/v4/file-urls/batch' \
  --header 'Authorization: Bearer ***' \
  --header 'Content-Type: application/json' \
  --header 'Accept: */*' \
  --data-raw '{
    "files": [{"name":"demo.pdf", "data_id": "abcd"}],
    "model_version": "vlm"
  }'
```

**curl（PUT 上传，逐个文件对应逐个 URL）**

```bash
curl -X PUT -T /path/to/your/file.pdf 'https://mineru.oss-cn-shanghai.aliyuncs.com/api-upload/***'
```

**响应**

```json
{
  "code": 0,
  "data": {
    "batch_id": "2bb2f0ec-a336-4a0a-b61a-241afaf9cc87",
    "file_urls": ["https://mineru.oss-cn-shanghai.aliyuncs.com/api-upload/***"]
  },
  "msg": "ok",
  "trace_id": "c876cd60b202f2396de1f9e39a1b0172"
}
```

> 响应里 `data.file_urls` 的顺序与请求 `files[]` **一一对应**（官方 SDK 用 `zip(file_paths, upload_urls)` 直接配对 🧩）。
> `file_urls` 是**响应**字段名；请求里没有这个字段 ✅。

---

### 2.6 接口 4：批量提交 URL 解析任务

**`POST https://mineru.net/api/v4/extract/task/batch`**

**要点** ✅：单次不能超过 ⚠️ 200（【主文档】）/ 50（【apiManage】）个；文件 ≤200MB、页数见 §5.1；文档明确提示 “**因网络限制，github、aws 等国外 URL 会请求超时**”。

**请求体字段**：与接口 3 同构，只是 `files[]` 里换成 `url`

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `files` | array | **是** | |
| `files[].url` | string | **是** | 文件链接 |
| `files[].is_ocr` | bool | 否 | 默认 false |
| `files[].data_id` | string | 否 | ≤128 字符 |
| `files[].page_ranges` | string | 否 | 见 §5.6 |
| `enable_formula` / `enable_table` / `language` / `callback` / `seed` / `extra_formats` / `model_version` / `no_cache` / `cache_tolerance` | 同 §2.3 | 否 | 顶层共享 |

**请求体示例**

```json
{
  "files": [
    {"url": "https://arxiv.org/pdf/2501.01234", "data_id": "arxiv_2501.01234"},
    {"url": "https://arxiv.org/pdf/2501.05678", "data_id": "arxiv_2501.05678", "page_ranges": "1-40"}
  ],
  "model_version": "vlm",
  "language": "en"
}
```

**curl**

```bash
curl --location --request POST 'https://mineru.net/api/v4/extract/task/batch' \
  --header 'Authorization: Bearer ***' \
  --header 'Content-Type: application/json' \
  --header 'Accept: */*' \
  --data-raw '{
    "files": [{"url":"https://cdn-mineru.openxlab.org.cn/demo/example.pdf", "data_id": "abcd"}],
    "model_version": "vlm"
  }'
```

**响应**

```json
{
  "code": 0,
  "data": {"batch_id": "2bb2f0ec-a336-4a0a-b61a-241afaf9cc87"},
  "msg": "ok",
  "trace_id": "c876cd60b202f2396de1f9e39a1b0172"
}
```

---

### 2.7 接口 5：批量获取任务结果

**`GET https://mineru.net/api/v4/extract-results/batch/{batch_id}`**

**响应字段**

| 字段 | 类型 | 说明 |
|---|---|---|
| `data.batch_id` | string | batch_id |
| `data.extract_result` | array | 每个文件一条结果 |
| `data.extract_result[].file_name` | string | 文件名 |
| `data.extract_result[].state` | string | `waiting-file`（等待文件上传排队提交）/ `pending` / `running` / `converting` / `done` / `failed` |
| `data.extract_result[].full_zip_url` | string | 结果 zip |
| `data.extract_result[].err_msg` | string | 失败原因（`failed` 时有效） |
| `data.extract_result[].data_id` | string | 回显 |
| `data.extract_result[].extract_progress.{extracted_pages,total_pages,start_time}` | — | 同 §2.4 |

**响应示例**

```json
{
  "code": 0,
  "data": {
    "batch_id": "2bb2f0ec-a336-4a0a-b61a-241afaf9cc87",
    "extract_result": [
      {
        "file_name": "example.pdf",
        "state": "done",
        "err_msg": "",
        "full_zip_url": "https://cdn-mineru.openxlab.org.cn/pdf/018e53ad-d4f1-475d-b380-36bf24db9914.zip"
      },
      {
        "file_name": "demo.pdf",
        "state": "running",
        "err_msg": "",
        "extract_progress": {
          "extracted_pages": 1,
          "total_pages": 2,
          "start_time": "2025-01-20 11:43:20"
        }
      }
    ]
  },
  "msg": "ok",
  "trace_id": "c876cd60b202f2396de1f9e39a1b0172"
}
```

**curl**

```bash
curl --location --request GET 'https://mineru.net/api/v4/extract-results/batch/{batch_id}' \
  --header 'Authorization: Bearer *****' \
  --header 'Accept: */*'
```

---

### 2.8 Agent 轻量解析 API（免 Token，供参考）

> 论文管理工具一般不需要这组接口（只有 Markdown、10MB / 20 页上限）。列在此处以便你判断是否需要“快速预览”降级路径。

| 接口 | Method + URL |
|---|---|
| URL 解析 | `POST https://mineru.net/api/v1/agent/parse/url` |
| 文件上传（签名上传） | `POST https://mineru.net/api/v1/agent/parse/file` |
| 查询结果 | `GET https://mineru.net/api/v1/agent/parse/{task_id}` |

- **无需 `Authorization`**；请求体必须是 JSON（`application/json`），**不支持 multipart/form-data**
- 文件限制：≤ **10 MB**、≤ **20 页**（文档另一处错误信息里写 50 页 ⚠️，示例错误文案是 `lightweight API limit (50 pages)`，但页面上限表格写 20 页）；支持 PDF、图片、Docx、PPTx、Xls、Xlsx
- 超限返回 **HTTP 429**
- 参数：`url` / `file_name` / `language` / `enable_table` / `is_ocr` / `enable_formula` / `page_range`（注意这里是 **`page_range`**，v4 是 **`page_ranges`**；且 agent 的 `page_range` **只支持** `1-10` 或 `5` 这种单一区间，不支持逗号复杂格式）
- 查询响应多两个字段：`data.markdown_url`（`done` 时）、`data.err_code`（`failed` 时）

```bash
curl --location --request POST 'https://mineru.net/api/v1/agent/parse/url' \
  --header 'Content-Type: application/json' \
  --data-raw '{
    "url": "https://cdn-mineru.openxlab.org.cn/demo/example.pdf",
    "language": "ch",
    "page_range": "1-10",
    "enable_table": true,
    "is_ocr": false,
    "enable_formula": true
  }'
```

---

## 3. 文件上传方式小结

| 场景 | 支持 | 做法 |
|---|---|---|
| 公网可访问 URL | ✅ | 接口 1（单个）或接口 4（批量 `files[].url`）。字段名是 **`url`**，不是 `file_urls` |
| 本地文件 | ✅ | 接口 3 申请预签名 URL → `PUT` 原始字节 → **系统自动**提交任务（无需再调提交接口）→ 接口 5 轮询 |
| 直接 multipart 上传 | ❌ | v4 文档未提供此类接口 |
| Agent 轻量本地文件 | ✅ | `POST /api/v1/agent/parse/file` → 拿 `data.file_url` → `PUT` |

### 3.1 本地文件上传的精确步骤

1. `POST /api/v4/file-urls/batch`，body 含 `files: [{name, data_id?, is_ocr?, page_ranges?}]` + 顶层 `model_version` 等
2. 从响应取 `data.batch_id` 与 `data.file_urls[]`（**与请求顺序一一对应**）
3. 对第 i 个文件：`PUT <file_urls[i]>`，body 是该文件的**原始二进制**；**不要加 `Content-Type`**（文档明确说明），**文档未要求 `Content-MD5`** ❌
4. 上传成功后**不需要**再调提交接口；系统自动扫描已上传完成的文件并自动提交解析
5. 用 `batch_id` 轮询接口 5，直到该文件 `state` ∈ {`done`, `failed`}

> 官方 SDK 的做法可直接照抄 🧩：`httpx.put(url, content=data, timeout=Timeout(30, read=300, write=300))`。
> 📌 社区经验：预签名 URL 是**不带 Content-Type 签名**的，用 `requests.put(url, data=f)` 可行；若用 axios 会自动加 header 导致失败（这与官方“无须设置 Content-Type”一致，可交叉印证）。

### 3.2 上传链接有效期

- v4 `file-urls/batch` 的链接：**24 小时** ✅
- `full_zip_url` 的有效期：**文档未说明** ❌

---

## 4. 异步任务流程、结果下载与 zip 结构

### 4.1 两种完成通知方式

**（A）轮询（推荐，最简单）**

```
POST /extract/task            → data.task_id
GET  /extract/task/{task_id}  → 直到 state == done
GET  full_zip_url             → 下载 zip
```

批量：

```
POST /file-urls/batch  或  POST /extract/task/batch   → data.batch_id
GET  /extract-results/batch/{batch_id}                → 直到所有 state ∈ {done, failed}
GET  full_zip_url                                     → 下载 zip
```

**（B）回调 callback** ✅

- 在请求体里给 `callback`（http/https 均支持）+ **必须**给 `seed`
- 回调由 MinerU **主动 POST** 到你的地址，要求你的接口：支持 POST、UTF-8、`Content-Type: application/json`
- 推送 body 包含两个参数：`checksum` 与 `content`
  - `checksum` = `SHA256(用户 uid + seed + content)`，你收到后应按同样算法重算并比对，防篡改（**用户 UID 在个人中心查询**）
  - `content` = **JSON 字符串**，自行解析成对象；其 `data` 部分与「任务查询结果的返回示例」一致
- 你的服务必须返回 **HTTP 200** 才算接收成功；否则 MinerU 最多**重推 5 次**，5 次后不再推送
- ⚠️ 官方文档**没有**给出回调请求体的完整样例，只有文字描述 ❌

> FastAPI 集成建议：论文工具是内网/本机服务，`callback` 需要公网可达，**建议直接用轮询**；若将来部署到公网再启用 callback。

### 4.2 结果下载

- 只需对 `data.full_zip_url`（或批量的 `extract_result[].full_zip_url`）发 **普通 GET** 即可下载 zip
- 该直链是 CDN 地址（示例域 `cdn-mineru.openxlab.org.cn`），**不需要 `Authorization`**，但建议 `follow_redirects=True` 🧩
- 官方 SDK 下载超时设置：连接 30s / 读取 300s 🧩
- **是否支持断点续传/Range：文档未说明** ❌

### 4.3 zip 内部结构

官方文档对 zip 内容只有一句权威描述 ✅（原文）：

> “非 html 文件解析结果详细说明请参考 <https://opendatalab.github.io/MinerU/reference/output_files/>，其中 **layout.json 对应中间处理结果 (middle.json)**，**\*\*_model.json 对应模型推理结果 (model.json)**，**\*\*_content_list.json 对应内容列表 (content_list.json)**，**full.md 为 MarkDown 解析结果**。
> html 文件解析结果略有不同：**full.md 为 MarkDown 解析结果，main.html 为提取后正文 html**。”

由【输出说明-legacy】+【官方SDK】的 zip 解析代码，可还原出以下结构 🧩/✅：

```
<task_id>.zip
├── full.md                       # Markdown 解析结果（含 ![](images/xxx) 图片引用）
├── main.html                     # 仅当源文件是 .html 时存在
├── <uuid>_content_list.json      # 内容列表（扁平数组，阅读顺序）—— ★下游首选
├── <uuid>_content_list_v2.json   # 3.0 起新增，按页分组的统一 type+content 结构
├── <uuid>_middle.json            # 中间处理结果（富元数据、完整层级）
├── <uuid>_model.json             # 模型推理原始结果（调试用）
├── <uuid>_layout.pdf             # 布局可视化（调试用，pipeline 后端）
├── <uuid>_span.pdf               # span 级标注（调试用，仅 pipeline 后端）
├── <uuid>.docx / *.html / *.tex  # 仅当请求了 extra_formats 时
└── images/
    ├── <sha256>.jpg              # 图片以内容哈希命名
    └── ...
```

> ⚠️ 命名前缀（`<uuid>_`）说明：官方文档写 “**\*\*_model.json**”“**\*\*_content_list.json**”，即前缀存在但未明说是什么；📌 社区 MCP 项目实测记录为 “MinerU names files inside the zip `<task-uuid>_<name>` (`<uuid>_content_list_v2.json`)”——**按后缀匹配文件名，不要硬编码前缀**，这是唯一稳妥做法 🧩（官方 SDK 的 zip 解析就是用 `name.endswith("_content_list.json") or name == "content_list.json"` 💡）。
> ⚠️ **`layout.json` 是否存在**：文档说“layout.json 对应 middle.json”，暗示 zip 里可能有一个叫 `layout.json` 的文件（可能是 `*_layout.json`），但官方的中间结果文件名是 `*_middle.json`。这两句话**存在命名不一致**，建议代码里同时兼容 `*_middle.json` 与 `*_layout.json` ⚠️。
> ⚠️ 输出内容随 MinerU 版本演进（引擎 3.0 起才新增 `content_list_v2.json`），**在线 API 的 zip 具体含哪些文件，文档未逐项列全** ❌。请以“解压后遍历 + 后缀匹配”的方式实现，不要假设固定清单。

### 4.4 `content_list.json` 字段含义（★论文工具最该用的文件）

顶层是一个**数组**，按阅读顺序排列，每个元素是一个内容块 ✅。

**公共字段**

| 字段 | 类型 | 说明 |
|---|---|---|
| `type` | string | 块类型，见下表 |
| `page_idx` | int | 页码，**从 0 开始** |
| `bbox` | [int,int,int,int] | `[x0,y0,x1,y1]`，归一化到 **0–1000** |
| `text_level` | int \| null | 仅文本块：无此字段或 `0` = 正文；`1`/`2`/… = 一/二/…级标题 |
| `sub_type` | string | 部分类型（vlm 后端的 `code`/`list`/`image`/`chart`）有 |

**type 取值** ✅（以【输出说明-legacy】为准）

| type | 说明 | 主要附带字段 |
|---|---|---|
| `text` | 文本 / 标题 | `text`, `text_level` |
| `image` | 图片 | `img_path`, `image_caption[]`, `image_footnote[]`, 可选 `sub_type` |
| `table` | 表格 | `img_path`, `table_body`(HTML), `table_caption[]`, `table_footnote[]` |
| `chart` | 图表 | `img_path`, `chart_caption[]`, `chart_footnote[]`, 可能带 `content`（原 Markdown 表格文本） |
| `equation` | 行间公式 | `text`（LaTeX，形如 `$$\n...\n$$`）, `text_format: "latex"`, `img_path` |
| `code` | 代码块 | `sub_type`(`code`\|`algorithm`), `code_body`, `code_caption[]`, 可能 `code_footnote` |
| `list` | 列表 / 参考文献 | `sub_type`(`text`\|`ref_text`), `list_items[]` |
| `seal` | 印章 | — |
| `header` / `footer` / `page_number` / `aside_text` / `page_footnote` | 页眉/页脚/页码/边栏/脚注（vlm 后端也会输出 discarded 块） | `text` |

**官方示例（节选，字段名逐字取自【输出说明-legacy】）**

```json
[
  {
    "type": "text",
    "text": "The response of flow duration curves to afforestation ",
    "text_level": 1,
    "bbox": [62, 480, 946, 904],
    "page_idx": 0
  },
  {
    "type": "image",
    "img_path": "images/a8ecda1c69b27e4f79fce1589175a9d721cbdc1cf78b4cc06a015f3746f6b9d8.jpg",
    "image_caption": ["Fig. 1. Annual flow duration curves of daily flows from Pine Creek, Australia, 1989–2000. "],
    "image_footnote": [],
    "bbox": [62, 480, 946, 904],
    "page_idx": 1
  },
  {
    "type": "equation",
    "img_path": "images/181ea56ef185060d04bf4e274685f3e072e922e7b839f093d482c29bf89b71e8.jpg",
    "text": "$$\nQ _ { \\% } = f ( P ) + g ( T )\n$$",
    "text_format": "latex",
    "bbox": [62, 480, 946, 904],
    "page_idx": 2
  },
  {
    "type": "table",
    "img_path": "images/e3cb413394a475e555807ffdad913435940ec637873d673ee1b039e3bc3496d0.jpg",
    "table_caption": ["Table 2 Significance of the rainfall and time terms "],
    "table_footnote": ["indicates that the rainfall term was significant at the $5 \\%$ level, ..."],
    "table_body": "<html><body><table><tr><td rowspan=\"2\">Site</td>...</table></body></html>",
    "bbox": [62, 480, 946, 904],
    "page_idx": 5
  }
]
```

**VLM 后端特有示例**

```json
{
  "type": "code",
  "sub_type": "algorithm",
  "code_caption": ["Algorithm 1 Modules for MCTSteg"],
  "code_body": "1: function GETCOORDINATE(d) ...",
  "bbox": [510, 87, 881, 740],
  "page_idx": 0
}
```

```json
{
  "type": "list",
  "sub_type": "text",
  "list_items": ["H.1 Introduction", "H.2 Example: Divide by Zero without Exception Handling"],
  "bbox": [174, 155, 818, 333],
  "page_idx": 0
}
```

> `text_format` 的取值集合只明确见过 `"latex"`；**是否还有其它取值：文档未说明** ❌。

### 4.5 `content_list_v2.json`（3.0 新增，论文工具可优先考虑）

- 顶层**按页分组**：`[[page0 blocks], [page1 blocks], ...]` ✅
- 每项统一为 `type + content` 结构，另有可选 `bbox`（同样 0–1000）、可选 `anchor`（某些 DOCX 标题/目录项）
- 常见 `type`：`title`(含 `title_content`, `level`)、`paragraph`(`paragraph_content`)、`equation_interline`(`math_content`, `math_type`)、`image`/`table`/`chart`/`seal`、`code`(`code_content`,`code_caption`,`code_footnote`,`code_language`)、`algorithm`、`list`/`index`(`list_items`)、`page_header`/`page_footer`/`page_number`/`page_aside_text`/`page_footnote`
- 官方标注：**development version, subject to change** ⚠️

```json
[
  [
    {
      "type": "title",
      "content": {
        "title_content": [{"type": "text", "content": "1 Introduction"}],
        "level": 1
      },
      "bbox": [83, 121, 917, 156]
    },
    {
      "type": "page_footnote",
      "content": {
        "page_footnote_content": [{"type": "text", "content": "* Corresponding author"}]
      },
      "bbox": [71, 815, 915, 841]
    }
  ]
]
```

### 4.6 `middle.json`（`layout.json`）字段含义

**pipeline 后端** ✅

```json
{
  "pdf_info": [ /* 每页一项 */ ],
  "_backend": "pipeline | vlm | office",
  "_version_name": "0.6.1"
}
```

页级字段：`page_idx`(从 0)、`page_size`(`[宽,高]`，PDF 原始 pt)、`preproc_blocks`、`para_blocks`（分段后的主输出）、`images`、`tables`、`interline_equations`、`discarded_blocks`、`layout_bboxes`、`_layout_tree`

层级：**一级块(table/image/chart) → 二级块 → lines → spans**

- 一级块：`type`、`bbox`、`blocks`
- 二级块 `type`：`image_body`/`image_caption`/`image_footnote`/`table_body`/`table_caption`/`table_footnote`/`chart_body`/`chart_caption`/`chart_footnote`/`text`/`title`/`index`/`list`/`interline_equation`
- line：`bbox`、`spans[]`
- span：`bbox`、`type`(`image`/`table`/`chart`/`text`/`inline_equation`/`interline_equation`)、`content` 或 `image_path`、`score`（pipeline）

**vlm 后端的差异** ✅：`list` 变成二级块并带 `sub_type`(`text`|`ref_text`)；新增 `code` 块（`sub_type` = `code`|`algorithm`，含 `code_body`，可选 `code_caption`）；`discarded_blocks` 可能含 `header`/`footer`/`page_number`/`aside_text`/`page_footnote`；**所有块都带 `angle`**（`0/90/180/270`）

> ⚠️ 引擎 4.0 已把中间格式换成 `docvortex.middle` / `docvortex.model`（`schema` + `schema_version` 键，`pages[].blocks[]`），并注明旧的 `_backend`、`pdf_info`、`_version_name` **不再属于该契约**。【输出说明】当前页面即 4.0 版。
> 但**在线 API 返回的格式未必等于最新引擎格式**（在线 API 文档仍在引用 `layout.json`/`middle.json` 的旧命名）。**在线 API 的 `middle.json` 究竟是 3.x 风格还是 4.0 风格，文档未说明** ❌。
> 因此：**论文管理工具请以 `full.md` + `*_content_list.json` 作为稳定契约**，`middle.json` 只做可选增强，并对两种 schema 都做容错。

### 4.7 `model.json`

- **pipeline 后端**：一个**扁平数组**，元素形如 ✅

```json
[
  {"cls_id": 12, "label": "header",  "score": 0.93,   "bbox": [1217, 104, 1516, 134], "index": 2},
  {"cls_id": 6,  "label": "doc_title","score": 0.9751,"bbox": [275, 181, 1512, 292], "index": 3},
  {"cls_id": 22, "label": "text",    "score": 0.9217, "bbox": [275, 330, 524, 370],  "index": 4}
]
```
（`bbox` 为页面像素/原始坐标；`label` 取值如 `header`/`doc_title`/`text`；文档另给出过 `category_id` 编号体系）

- **vlm 后端**：二维嵌套数组（外层=页，内层=该页块），块含 `type`、`bbox`（**归一化到 0–1**）、`angle`、`content`，部分类型带 `score`/`block_tags`/`content_tags`/`format` ✅

```json
[
  [
    {
      "type": "header",
      "bbox": [0.077, 0.095, 0.18, 0.181],
      "angle": 0,
      "score": null,
      "block_tags": null,
      "content": "ELSEVIER",
      "format": null,
      "content_tags": null
    }
  ]
]
```

- vlm 的 `type` 集合：`text`/`title`/`equation`/`image`/`image_caption`/`image_footnote`/`table`/`table_caption`/`table_footnote`/`phonetic`/`code`/`code_caption`/`ref_text`/`algorithm`/`list`/`header`/`footer`/`page_number`/`aside_text`/`page_footnote`

> `model.json` 是调试用原始输出，**不建议**论文工具依赖它。

---

## 5. 解析参数速查（含取值）

### 5.1 模型版本 `model_version`

| 值 | 说明 |
|---|---|
| `pipeline` | **默认值**。快速、轻量 |
| `vlm` | 文档标注“**推荐**”；精度更高（📌 社区称约 90% 精度，适合学术 PDF）。注意：`enable_formula` 对 vlm **只影响行内公式** |
| `MinerU-HTML` | 解析 HTML 时**必须显式指定**；非 HTML 文件不要用 |

`is_ocr` / `enable_formula` / `enable_table` / `language` 均**仅对 `pipeline`、`vlm` 有效** ✅。

### 5.2 `is_ocr`

`bool`，默认 `false`。扫描版 PDF 建议开 `true`。

### 5.3 `enable_formula`

`bool`，默认 `true`。

### 5.4 `enable_table`

`bool`，默认 `true`。

### 5.5 `language`

`string`，默认 `ch`。**仅对 pipeline/vlm 有效** ✅。完整取值：

**Standalone 语言包**

| 值 | 覆盖语言 | 说明 |
|---|---|---|
| `ch` | 中文、英文、繁体中文 | **默认值** |
| `ch_server` | 中/英/繁/日 | 繁体、手写体 |
| `en` | 英文 | 纯英文 |
| `japan` | 中/英/繁/日 | 日文为主 |
| `korean` | 韩文、英文 | |
| `chinese_cht` | 中/英/繁/日 | 繁体中文为主 |
| `ta` | 泰米尔文、英文 | |
| `te` | 泰卢固文、英文 | |
| `ka` | 卡纳达文 | |
| `el` | 希腊文、英文 | |
| `th` | 泰文、英文 | |

**语言族包**

| 值 | 语系 |
|---|---|
| `latin` | 拉丁语系（法/德/意/西/葡/越/芬兰/土耳其… 40+ 种） |
| `arabic` | 阿拉伯语系（阿拉伯/波斯/维吾尔/乌尔都/普什图…） |
| `cyrillic` | 西里尔语系（俄/白俄/乌/保/蒙/哈/吉…） |
| `east_slavic` | 东斯拉夫语系（俄/白俄/乌/英） |
| `devanagari` | 天城文语系（印地/马拉地/尼泊尔/梵…） |

> ⚠️ 注意：📌 某些社区文档写默认值是 `zh`、取值为 `zh/ja/ko`，**这是错的**，官方文档的默认值是 **`ch`**，且**没有** `zh`/`ja`/`ko` 这些取值。以本表为准。

### 5.6 `page_ranges`（v4）

`string`，逗号分隔 ✅

- `"1-600"` → 第 1 到 600 页
- `"2,4-6"` → 第 2、4、5、6 页（**含端点**）
- `"2--2"` → 从第 2 页一直选到**倒数第二页**（`-2` 表示倒数第二页）

> Agent 轻量 API 的参数名是 **`page_range`**（无 s），且只接受 `1-10` 或 `5` 这种**单一区间**，不支持逗号 ✅。

### 5.7 `extra_formats`

`string[]`，取值仅 `docx` / `html` / `latex`（可多选）✅。
- `markdown`、`json` 是默认导出，**不要**也不需要放进去
- **对源文件为 html 的无效**

### 5.8 `data_id` / `callback` / `seed` / `no_cache` / `cache_tolerance`

| 参数 | 类型 | 默认 | 约束 |
|---|---|---|---|
| `data_id` | string | — | 字母数字 `_` `-` `.`，≤128 字符 |
| `callback` | string | — | http/https；为空则必须轮询 |
| `seed` | string | — | 字母数字 `_`，≤64 字符；**用 callback 时必填** |
| `no_cache` | bool | `false` | `true` 则忽略 URL 内容缓存 |
| `cache_tolerance` | int | `900` | 秒；`no_cache=false` 时有效 |

> ❗文档中**不存在** `ocr`（正确名 `is_ocr`）、`table`（正确名 `enable_table`）、`formula`（正确名 `enable_formula`）、`model`（正确名 `model_version`）、`callback_url`（正确名 `callback`）、`lang` 这些简写字段。⚠️ 有些社区技能文档写的是 `ocr`/`table`/`formula`/`model`，**不能直接用**。

---

## 6. 限流、配额与错误码

### 6.1 限流（【限流页】原文，✅ 唯一权威）

| 类别 | 限制 |
|---|---|
| 提交类接口（单个创建任务 + 文件批量上传 + URL 批量上传，**共用一个频控**） | **300 次/分钟** |
| 单个用户每天上传文件数 | 最多 **1 万个**文件，其中 **html 文件最多 100 个** |
| 结果查询类接口（单个任务结果 + 批量任务结果，**共用一个频控**） | **1000 次/分钟** |
| Agent 轻量 API | 每 IP 每分钟提交请求数有限制，超限返回 **HTTP 429**（具体数值未给 ❌） |

> 官方保留“根据系统负载动态调整限流策略”的权利 ✅。
> 超出限额的诉求可通过 `opendatalab@pjlab.org.cn` 协商定制 ✅。

### 6.2 配额

| 项 | 【主文档】 | 【apiManage】 | 第三方 |
|---|---|---|---|
| 每日最高优先级解析额度 | **2000 页/账号/天**，超出部分**优先级降低**（不拒绝） ✅ | **1000 页/账号/天** ⚠️ | 社区称约 2000 页/天 📌 |
| 单文件页数上限 | **600 页** ✅ | **200 页** ⚠️ | 社区两者都有人写 📌 |
| 单次批量申请链接数 | **200** ✅ | **50** ⚠️ | — |
| 文件大小上限 | **200 MB** ✅ | 200 MB ✅ | — |
| 免费额度是否存在 | 官方 README 称 “**完全免费**，没有任何隐藏费用” ✅（【官方SDK】README） | — | — |

> ⚠️ **两处官方页面严重不一致**（页数 600 vs 200、日额度 2000 vs 1000、批量数 200 vs 50）。可信度更高的应是**站点主文档 `/doc/docs/index.html`（较新、更完整，含 no_cache / cache_tolerance / 600 页）**。工程上建议：
> - 单文件页数按 **200 页**做保守分片策略（超过就按 `page_ranges` 切片，见 §7.3）
> - 每日配额在界面上做**软提示**而非硬拒绝（因为“超额只是降优先级”）
> - 批量大小用可配置常量（默认 50 或 200）

### 6.3 错误码表（v4 精准解析）✅ 来自【主文档】原文

| 错误码 | 说明 | 解决建议 |
|---|---|---|
| `A0202` | **Token 错误** | 检查 Token 是否正确，**请检查是否有 Bearer 前缀**，或更换新 Token |
| `A0211` | **Token 过期** | 更换新 Token |
| `-500` | 传参错误 | 请确保参数类型及 `Content-Type` 正确 |
| `-10001` | 服务异常 | 请稍后再试 |
| `-10002` | 请求参数错误 | 检查请求参数格式 |
| `-60001` | 生成上传 URL 失败 | 请稍后再试 |
| `-60002` | 获取匹配的文件格式失败 | 检测文件类型失败；请求的文件名及链接需带正确后缀名，且文件为 pdf, doc, docx, ppt, pptx, png, jp(e)g 之一 |
| `-60003` | 文件读取失败 | 检查文件是否损坏并重新上传 |
| `-60004` | 空文件 | 请上传有效文件 |
| `-60005` | **文件大小超出限制** | 最大支持 200MB |
| `-60006` | **文件页数超过限制** | 请拆分文件后重试 |
| `-60007` | 模型服务暂时不可用 | 请稍后重试或联系技术支持 |
| `-60008` | 文件读取超时 | 检查 URL 可访问 |
| `-60009` | 任务提交队列已满 | 请稍后再试 |
| `-60010` | **解析失败** | 请稍后再试 |
| `-60011` | 获取有效文件失败 | 请确保文件已上传 |
| `-60012` | 找不到任务 | 请确保 task_id 有效且未删除 |
| `-60013` | 没有权限访问该任务 | 只能访问自己提交的任务 |
| `-60014` | 删除运行中的任务 | 运行中的任务暂不支持删除 |
| `-60015` | 文件转换失败 | 可以手动转为 pdf 再上传 |
| `-60016` | 文件转换失败 | 文件转换为指定格式失败，可尝试其他格式导出或重试 |
| `-60017` | 重试次数达到上限 | 等后续模型升级后重试 |
| `-60018` | **每日解析任务数量已达上限** | 明日再来 |
| `-60019` | **html 文件解析额度不足** | 明日再来 |
| `-60020` | 文件拆分失败 | 请稍后重试 |
| `-60021` | 读取文件页数失败 | 请稍后重试 |
| `-60022` | 网页读取失败 | 可能因网络问题或限频导致，请稍后重试 |

### 6.4 错误码表（Agent 轻量）✅

| 错误码 | 说明 | 应对 |
|---|---|---|
| `-30001` | 文件大小超出轻量接口限制（10MB） | 使用标准 API 或拆分文件 |
| `-30002` | 轻量接口不支持该文件类型 | 上传 PDF/图片/Doc/PPT/Excel |
| `-30003` | 文件页数超出轻量接口限制 | 使用标准 API 或指定 `page_range` |
| `-30004` | 请求参数错误 | 检查必填参数是否缺失 |

这些码会出现在查询响应的 `data.err_code`（同时 `data.state == "failed"`）✅。

### 6.5 HTTP 状态码

- 文档**没有**给出 HTTP 状态码约定表 ❌
- 已知事实：限流页明确说 Agent API 超限返回 **HTTP 429** ✅；📌 社区资料报告 Token 失效时受保护接口会返回 HTTP `401/403`（属社区经验，非官方承诺）
- 鉴权失败建议：代码里**同时**判断 HTTP 401/403 与业务码 `code ∈ {A0202, A0211}`，两者任一命中即判为“Token 需重新生成”
- 官方 SDK 的取舍是“先 `raise_for_status()` 处理 HTTP 层，再检查业务码 `code != 0`” —— 两层都要处理 🧩

---

## 7. 落到 FastAPI 后端的建议实现

### 7.1 关键参数速查（可直接做成配置文件）

| 变量 | 建议值 | 依据 |
|---|---|---|
| `MINERU_BASE_V4` | `https://mineru.net/api/v4` | 【官方SDK】默认值 🧩 |
| `MINERU_BASE_AGENT` | `https://mineru.net/api/v1/agent` | 【官方SDK】默认值 🧩 |
| `MINERU_MODEL_VERSION` | `vlm`（论文 PDF）；HTML 用 `MinerU-HTML` | 文档标注 vlm “推荐” ✅ |
| `MINERU_LANGUAGE` | `en` 或 `ch` | 默认 `ch` |
| 提交频控自限 | ≤ 300 req/min（**共享**计数） | 【限流页】✅ |
| 查询频控自限 | ≤ 1000 req/min（**共享**计数） | 【限流页】✅ |
| 单文件大小 | ≤ 200 MB | ✅ |
| 单文件页数 | 保守按 200 页分片 | ⚠️ 600 vs 200 矛盾 |
| 批量大小 | 保守 50（可配到 200） | ⚠️ 200 vs 50 矛盾 |
| 轮询策略 | 首次 2s，指数退避上限 30s；单任务总超时 300s，批量 1800s | 🧩【官方SDK】 |
| PUT 上传超时 | 连接 30s / 读 300s / 写 300s | 🧩【官方SDK】 |
| zip 下载超时 | 连接 30s / 读 300s，`follow_redirects=True` | 🧩【官方SDK】 |

### 7.2 分层设计（建议）

```
Routers (FastAPI)
  POST /papers/{id}/parse           # 触发解析（入队）
  GET  /papers/{id}/parse/status    # 查状态/进度
  GET  /papers/{id}/content         # 返回规范化后的结构化内容

Services
  MinerUClient          # 薄封装：鉴权头、base_url、code!=0 → 抛异常、trace_id 记录
  UploadService         # 本地文件：batch 申请 → PUT → 记 batch_id
  TaskPoller            # 轮询（退避 + 频控令牌桶）
  ResultImporter        # zip 下载 → 解压 → 后缀匹配 → 落库
```

### 7.3 长文分片（>上限页数）

官方 SDK 未提供分片，但 📌 社区 MCP 项目的做法值得借鉴：按 `page_ranges` 切成 ≤200 页的片，`data_id` 里编码切片信息（如 `name__p000001-000200`），全部完成后按 `data_id` 排序、给图片路径加切片前缀、把 `page_idx` 重新归零再拼接。这是**目前唯一可用的超长文方案**，因为 API 没有“继续解析”语义。

### 7.4 结果落库的最小可用映射（推荐只用两个文件）

1. 解压 zip，用**后缀匹配**取文件（官方 SDK 就是这么做的 🧩）：
   - Markdown：取第一个 `.md`（通常是 `full.md`）→ 存 `paper.md`；图片引用相对路径 `images/...` 保持原样
   - 结构化：取 `name.endswith("_content_list.json") or name == "content_list.json"` → 存 `content_list.json`
   - 图片：后缀 ∈ `{.png,.jpg,.jpeg,.gif,.bmp,.svg,.webp}` → 存到 `images/` 并保留 zip 内相对路径
   - 可选：`.docx` / `.html` / `.tex`（对应 `extra_formats`）
2. 从 `content_list.json` 抽取论文元数据：
   - 标题候选：`type == "text" and text_level == 1`（取首个）
   - 章节树：`text_level >= 1` 的块按顺序构成大纲
   - 图表：`type in {"image","table","chart"}`，`image_caption`/`table_caption` 就是图题
   - 公式：`type == "equation"`，`text` 是 LaTeX
   - 参考文献：`type == "list" and sub_type == "ref_text"`（vlm 后端）

### 7.5 可直接复制的 Python 客户端骨架（v4）

```python
# mineru_client.py
from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import httpx

BASE_V4 = "https://mineru.net/api/v4"

# 依据官方文档：提交类 300/min，查询类 1000/min（两类各自共享计数）
REQUEST_TIMEOUT = httpx.Timeout(30.0, read=120.0)
UPLOAD_TIMEOUT = httpx.Timeout(30.0, read=300.0, write=300.0)
DOWNLOAD_TIMEOUT = httpx.Timeout(30.0, read=300.0)

# 官方错误码 → 语义（官方文档《常见错误码》全量）
AUTH_CODES = {"A0202", "A0211"}
QUOTA_CODES = {"-60018", "-60019"}
RETRYABLE_CODES = {"-10001", "-60001", "-60007", "-60009", "-60010", "-60020", "-60021", "-60022"}


class MinerUError(Exception):
    def __init__(self, code: Any, msg: str, trace_id: str = "") -> None:
        self.code = str(code)
        self.msg = msg
        self.trace_id = trace_id
        super().__init__(f"[{self.code}] {msg} (trace_id={trace_id})")


class MinerUClient:
    def __init__(self, token: str) -> None:
        self._client = httpx.Client(
            base_url=BASE_V4,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                # 官方 SDK 会带 source，便于官方排障；填你的应用名
                "source": "paper-manager-fastapi",
            },
            timeout=REQUEST_TIMEOUT,
        )

    # ---------- 底层 ----------
    def _handle(self, resp: httpx.Response) -> dict:
        resp.raise_for_status()                       # HTTP 401/403/429 走这里
        body = resp.json()
        if body.get("code") != 0:                      # 成功码固定为 0
            raise MinerUError(body.get("code"), body.get("msg", ""), body.get("trace_id", ""))
        return body

    # ---------- 1. 远程 URL 单文件 ----------
    def create_task_by_url(
        self,
        url: str,
        *,
        model_version: str = "vlm",
        language: str = "ch",
        is_ocr: bool = False,
        enable_formula: bool = True,
        enable_table: bool = True,
        page_ranges: str | None = None,
        data_id: str | None = None,
        extra_formats: list[str] | None = None,
        no_cache: bool = False,
        cache_tolerance: int = 900,
    ) -> str:
        payload: dict[str, Any] = {
            "url": url,
            "model_version": model_version,
            "language": language,
            "is_ocr": is_ocr,
            "enable_formula": enable_formula,
            "enable_table": enable_table,
            "no_cache": no_cache,
            "cache_tolerance": cache_tolerance,
        }
        if page_ranges:
            payload["page_ranges"] = page_ranges
        if data_id:
            payload["data_id"] = data_id
        if extra_formats:
            payload["extra_formats"] = extra_formats
        return self._handle(self._client.post("/extract/task", json=payload))["data"]["task_id"]

    # ---------- 2. 查询单任务 ----------
    def get_task(self, task_id: str) -> dict:
        return self._handle(self._client.get(f"/extract/task/{task_id}"))["data"]

    # ---------- 3. 本地文件：申请上传链接 ----------
    def apply_upload_urls(self, files: list[dict], **opts: Any) -> tuple[str, list[str]]:
        """files: [{"name": "a.pdf", "data_id": "x", "is_ocr": True, "page_ranges": "1-50"}, ...]"""
        payload = {"files": files, **opts}
        data = self._handle(self._client.post("/file-urls/batch", json=payload))["data"]
        return data["batch_id"], data["file_urls"]

    # ---------- 4. PUT 上传（不加 Content-Type！）----------
    def upload(self, file_url: str, local_path: str | Path) -> None:
        content = Path(local_path).read_bytes()
        resp = httpx.put(file_url, content=content, timeout=UPLOAD_TIMEOUT)
        resp.raise_for_status()                        # 官方示例判断 status_code == 200

    # ---------- 5. 批量结果 ----------
    def get_batch(self, batch_id: str) -> dict:
        return self._handle(self._client.get(f"/extract-results/batch/{batch_id}"))["data"]

    # ---------- 6. 轮询 ----------
    def wait_batch(self, batch_id: str, *, timeout: int = 1800) -> list[dict]:
        deadline = time.monotonic() + timeout
        interval = 2.0
        while True:
            results = self.get_batch(batch_id).get("extract_result", [])
            if results and all(r.get("state") in ("done", "failed") for r in results):
                return results
            if time.monotonic() > deadline:
                raise TimeoutError(f"batch {batch_id} 未在 {timeout}s 内完成")
            time.sleep(interval)
            interval = min(interval * 2, 30.0)         # 指数退避，上限 30s

    # ---------- 7. 下载 zip（直链无需鉴权）----------
    def download_zip(self, zip_url: str, dest: str | Path) -> Path:
        resp = httpx.get(zip_url, timeout=DOWNLOAD_TIMEOUT, follow_redirects=True)
        resp.raise_for_status()
        p = Path(dest)
        p.write_bytes(resp.content)
        return p
```

**本地文件完整流程**

```python
client = MinerUClient(token="你的Token")

batch_id, file_urls = client.apply_upload_urls(
    [{"name": "paper.pdf", "data_id": "paper_0001", "page_ranges": "1-200"}],
    model_version="vlm",
    language="en",
    enable_formula=True,
    enable_table=True,
)
client.upload(file_urls[0], "paper.pdf")     # 上传完成后系统自动提交，无需再调提交接口
results = client.wait_batch(batch_id)
if results[0]["state"] == "done":
    client.download_zip(results[0]["full_zip_url"], "paper.zip")
else:
    print("失败原因:", results[0]["err_msg"])
```

**curl 版最小闭环**

```bash
# ① 申请上传链接
curl -X POST 'https://mineru.net/api/v4/file-urls/batch' \
  -H 'Authorization: Bearer ***' -H 'Content-Type: application/json' \
  -d '{"files":[{"name":"paper.pdf","data_id":"paper_0001"}],"model_version":"vlm","language":"en"}'

# ② PUT 上传（把 <file_url> 换成响应里的 data.file_urls[0]）
curl -X PUT -T ./paper.pdf '<file_url>'

# ③ 轮询批量结果
curl -X GET 'https://mineru.net/api/v4/extract-results/batch/<batch_id>' \
  -H 'Authorization: Bearer ***'

# ④ 下载结果 zip（无需鉴权）
curl -L -o result.zip '<full_zip_url>'
```

### 7.6 健壮性清单

- [ ] **不要硬编码 zip 内文件名**：用后缀匹配（`*_content_list.json` / `content_list.json` / `*.md` / `images/*`）
- [ ] **两套 schema 容错**：`middle.json` 同时兼容 `{pdf_info, _backend}` 与 `{pages, schema:"docvortex.middle"}`
- [ ] **提交与查询分两个令牌桶**（300/min 与 1000/min 各自共享）
- [ ] **鉴权失败单独告警**：`A0202`/`A0211` 或 HTTP 401/403 → 提示去 <https://mineru.net/apiManage/token> 重新生成 Token（**没有自动刷新**）
- [ ] **记录 `trace_id`**：所有请求失败都把 `trace_id` 落日志，找官方支持时是唯一凭据
- [ ] **配额超限不要重试**：`-60018`/`-60019` 属于“明日再来”，重试只会浪费频控额度
- [ ] **国外 URL 下载会超时**：`github.com`、`aws` 等域名文档明确提示会超时 → 论文工具**优先走本地文件上传**，或先把 PDF 缓存到自己的对象存储再传 URL
- [ ] **`data_id` 用来做幂等键**：把它设成 `paper_id + version`，便于结果回填与对账

---

## 8. 明确「文档未说明」的清单（不要臆造）

| 项 | 状态 |
|---|---|
| Token 有效期具体天数 | ❌ 官方文档未说明（社区称 ~90 天 📌） |
| Token 刷新/续期 API | ❌ 未说明（社区一致说只能重新生成 📌） |
| 单个账号可创建的 Token 数量 | ❌ 未说明（社区称最多 5 个 📌） |
| `full_zip_url` 的有效期 / 是否可重复下载 | ❌ 未说明 |
| zip 内**完整且固定**的文件清单 | ❌ 未逐项列全（且随引擎版本变化） |
| `<uuid>_` 前缀的确切构成规则 | ❌ 未说明（📌 社区实测为 task uuid） |
| `layout.json` 与 `*.json` 命名矛盾（`layout.json` vs `*_middle.json`） | ⚠️ 文档自相矛盾 |
| PUT 上传是否需要 `Content-MD5` | ❌ 未说明，文档只说“无须设置 Content-Type” |
| PUT 上传失败的重试语义 / 分片上传是否支持 | ❌ 未说明 |
| 回调请求体的完整 JSON 样例 | ❌ 未给出（只说 `content` 的 data 部分同任务查询结果） |
| HTTP 状态码规范表（除 Agent 429） | ❌ 未给出 |
| 失败响应中是否仍包含 `data` 字段 | ❌ 未说明 |
| `text_format` 除 `"latex"` 之外的取值 | ❌ 未说明 |
| `middle.json` 在线 API 侧是 3.x 还是 4.0 schema | ❌ 未说明 |
| 单文件页数上限（600 vs 200）、每日额度（2000 vs 1000）、批量上限（200 vs 50） | ⚠️ 两处官方页面互相矛盾 |
| OpenAPI / Swagger 规范文件 | ❌ 不存在（官网页面中无 openapi/swagger 字样） |
| 是否有任务删除/列表/配额查询接口 | ❌ 文档未提供（仅错误码提到“删除运行中的任务”） |
| 免费额度的“页”如何计数（按总页数还是按 `page_ranges` 命中页数） | ❌ 未说明 |

---

## 9. 参考链接

- [MinerU 文档解析接口文档（apiManage 页）](https://mineru.net/apiManage/docs)
- [MinerU 主文档 index（精准解析 API + Agent 轻量解析 API）](https://mineru.net/doc/docs/index.html)
- [MinerU 主文档 index_en](https://mineru.net/doc/docs/index_en.html)
- [MinerU 接口限流策略](https://mineru.net/doc/docs/limit.html)
- [MinerU Token 管理页（需登录）](https://mineru.net/apiManage/token)
- [官方 SDK：MinerU-Ecosystem（Python / Go / TypeScript）](https://github.com/opendatalab/MinerU-Ecosystem)
- [官方 Python SDK README（中文）](https://github.com/opendatalab/MinerU-Ecosystem/blob/main/sdk/python/README.zh-CN.md)
- [开源引擎输出文件格式说明（当前 = 4.0 版）](https://opendatalab.github.io/MinerU/reference/output_files/)
- [开源引擎输出文件格式说明（历史版本，含 content_list.json 全量字段）](https://raw.githubusercontent.com/opendatalab/MinerU/05b0941245831804a488de1460d142bc0bb9aa78/docs/en/reference/output_files.md)
- [官方教程仓库 mineru-tutorials（含“在线 API 实战教程”）](https://github.com/opendatalab/mineru-tutorials)
- [MinerU 开源主仓库](https://github.com/opendatalab/MinerU)
- 第三方交叉验证（📌，仅作参考）：[mineru-mcp CLAUDE.md（Token ~90 天、免 Content-Type 上传、`<uuid>_content_list_v2.json` 命名、探测法）](https://github.com/linxule/mineru-mcp/blob/main/CLAUDE.md)、[MinerU API Setup Guide](https://github.com/Rostopher/fro-wang-academic-tools-mcp/blob/main/docs/mineru_config_guide.md)
