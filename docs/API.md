# API 说明（PaperMind 后端）

- 基址：`http://127.0.0.1:8710/api/v1`（同时挂一份无前缀副本，便于直接打开资源链接）
- 交互文档：`/docs`（Swagger） · `/redoc`
- 统一响应：`{"data": ..., "error": null|{code,message,detail}, "trace_id": "..."}`
- 长任务一律返回 `task_id`，**不阻塞等待**；重复触发同类任务返回同一进行中任务（幂等）

---

## 0. 前端托管（`/app/`）

前端是 Vite 构建的 SPA（`frontend/dist/`，history 路由，`base=/app/`），与后端同进程：

| 路径 | 行为 |
|---|---|
| `GET /` | 307 → `/app/` |
| `GET /app/` | 返回 `dist/index.html` |
| `GET /app/<真实文件>` | 直接返回（`assets/*.js|css`、KaTeX 字体…） |
| `GET /app/<前端路由>` | 回退 `index.html`（如 `/app/reader/turboquant-iclr26`，刷新不 404） |
| `GET /app/<带扩展名但不存在>` | **404**（返回 HTML 会让浏览器把 HTML 当 JS/CSS 解析，报更难懂的 MIME 错误） |
| `GET /app/...`（未构建） | 503 + 构建指引页（`cd frontend && npm install && npm run build`），不静默 404 |

- 开发模式：`cd frontend && npm run dev` → `http://127.0.0.1:5173/app/`（Vite 已把 `/api`、`/papers`、
  `/health` 代理到 8710；后端地址用 `PR_BACKEND` 覆盖）。
- `?nosse=1`（可加在任意前端路由上）：强制走「SSE 断开 → 3s 轮询」降级路径。
  用于禁用长连接的环境，也让无头浏览器自检能拿到 DOM（长连接会让 virtual-time 永不推进）。
- 前端自检：`python scripts/frontend_check.py --block-markers`（静态+接口+真实渲染，40 项）。

---

## 1. 论文与解析（§20.1）

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/papers` | 上传 PDF（multipart：`file`,`title`,`authors`,`venue`,`year`,`tags`,`paper_id`,`auto_parse`）。同一文件（sha256）已导入时**复用现有论文**并返回 `reused: true` |
| GET | `/papers` | 列表：`q/status/tag/sort/order/page/page_size` |
| GET | `/papers/{id}` | 详情（状态、解析摘要、翻译计数）。**复核模式已移除**：不再返回 `review_open`（"待复核 N"计数） |
| PUT | `/papers/{id}` | 更新元数据（title/authors/venue/year/tags/status/in_knowledge_base/abstract） |
| DELETE | `/papers/{id}?hard=false` | 删除：**先删索引 → 取消任务 → 清 chunk → 文件移入 trash** |
| POST | `/papers/{id}/parse?force=false` | 触发解析（幂等） |
| GET | `/papers/{id}/file` | PDF 流（供 pdf.js） |
| GET | `/papers/{id}/pages/{n}` | 页面图（150dpi PNG） |
| GET | `/papers/{id}/markdown` | Markdown 单一事实源全文 |
| GET | `/papers/{id}/reconcile` | 三存储对账（只报告不自动删） |

**扫描件（影印 PDF）**：解析时自动判定，无需额外参数。某页文本层字符 < 40 且含整页图像
→ 该页交给视觉模型整页识别成 Markdown 原文，再走既有翻译链路。相关字段：

- `POST /papers/{id}/parse` 参数 `vision`（默认 true）、`vision_model`（指定视觉模型 id）、`vision_dpi`（默认 200）；
- 解析结果 `stats.scanned_pages` 列出被判定为扫描的页；这些页 `page.has_text_layer = false`；
- 扫描页的块坐标是整页等分的虚拟横带（没有矢量层），因此**不做**公式卡与插图裁剪，
  公式以 `$$…$$` 留在正文里。

## 2. 阅读器数据（§20.2）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/papers/{id}/reader` | reader.json：`paper / blocks / pages / page_translations / progress / annotations / highlights / notes / tasks / outline / stats`；`since` 支持增量。批注/笔记/高亮条目均带 `page`（行 id→页，回退块 id→页；都查不到为 `null`，前端只提示不乱跳） |
| GET | `/papers/{id}/content?since=&limit=` | 按 block 取正文（增量） |
| GET | `/papers/{id}/translations?since_page=N` | 译文增量（LiveUpdater 轮询兜底用）：**按页**返回 `{page, page_total, rows, align, model, updated_at}` |
| GET | `/papers/{id}/markdown/translated?write=true` | **带插图的逐页双语 Markdown**：`{paper_id, pages:[{page, page_total, original_md, translation_md, align, model}], markdown, path, count}`；同时落盘 `data/papers/<id>/md/translated.md`（图片用 `../figures/…` 相对路径）。`?write=false` 只返回不落盘 |
| GET | `/papers/{id}/blocks/{block_id}` | 单块详情（含解析阶段公式卡） |
| GET | `/papers/{id}/formulas?source=markdown\|parse` | 公式列表。**默认 `source=markdown`**：从页级产物 Markdown 里提取，返回 `{items, count, source: "markdown", display_count}`；`items[] = {id, page, row_id, latex, display, context, context_zh, model}`（`row_id` 是页级产物的行 id，用来定位正文；`display=true` = 行间 `$$…$$`、`false` = 行内）。传 `source=parse` 才返回解析阶段的公式卡（`latex_final / grade / confidence / vector_audit / validation / crop_url`，含裁图与三重校验证据），**仅供解析质检参考**。旧参数 `grade` 已失效（被忽略） |
| GET | `/papers/{id}/figures` | 插图列表：`{items, count}`；每条含 `block_id` / `row_id`（= 该图**图注行的行 id**，由图号反查）/ `page` / `number` / `caption` / `bbox` / `image_url`。行 id 失效（重译后行号变了）时前端按 `page` 定位 |
| GET | `/papers/{id}/figures/region/{page}/{index}` | 插图裁图（**600dpi 按需渲染 + 落盘缓存**），裁图区域**按图注/标题锚定收紧**（`backend/services/paper/figure_regions.py`） |
| GET | `/papers/{id}/outline` | **目录（按页级翻译产物的标题行，中英双语）**：`{items（扁平）, tree（嵌套）, count}`；每项 `block_id / page / level / number / text（英文标题）/ text_zh（中文标题）/ children` |
| GET/PUT | `/papers/{id}/progress` | 阅读进度（服务端权威，含 `view_mode` / `pair_order` / `scroll_ratio`） |

`reader.pages[]` 关键字段：`page / width / height / columns / gutter_x / blocks /
has_text_layer / translated / row_count / align / media`（`media` 是该页插图）。
`reader.page_translations[]` 是逐页产物：`page / page_total / original_md / translation_md /
rows[] / align / model / updated_at`。
`reader.stats` = `{pages, pages_done, rows, align, model, blocks, by_type, translated,
translate_total, translate_percent}`（**以页为进度口径**）。**复核模式已移除**：stats 不再有
`reviews_open`，`paper` 也不再有 `review_open` 列（解析阶段的一致性检查与插图体检只进
`result.stats["consistency_findings"]` 与质量报告）。

`reader.blocks[]` 关键字段：`id / page / type / col / order / bbox / anchor / text /
heading_level / translation / translation_status / translation_revision / formula`。

**目录（outline）的语义**：**中英双语**——每个标题项同时带 `text`（英文标题）与 `text_zh`
（中文标题），来自页级产物的 `type=heading` 行；**没有页级产物时**退回解析阶段的标题索引
（只有英文，`text_zh` 为空）。`heading_level = 0` 的块被排除（算法伪代码行、表格数值行这类
"假标题"），编号断链的标题（如找不到父级 `0` 的 `0.1`）按顶级处理，不会挂到无关标题下。
`GET /papers/{id}/reader` 的响应里已内嵌同一份 `outline`。

**公式（formulas）的语义**：公式列表**从 Markdown（页级翻译产物）里提取**，与页面上读到的公式
一字不差，只读、不复核。实测 TurboQuant：**行间 31 / 行内 313 / 合计 344**；
解析阶段的公式卡 68 张只在 `source=parse` 时返回。提取要点：行内公式必须**成对扫描**
（直接正则会把两段公式之间的正文当公式），单字符变量这类噪声被过滤。
`display` 区分行间（`$$…$$`）与行内；`row_id` 用来把公式定位回正文行。

**阅读进度字段**：

| 字段 | 取值 | 说明 |
|---|---|---|
| `view_mode` | `original` \| `translation` \| `bilingual` | 原文 / 译文 / **左右对照** |
| `pair_order` | `en_left` \| `zh_left` | 对照模式下原文在左 / 译文在左（旧值 `en_top`/`zh_top` 仍接受，读回时归一化为 `en_left`/`zh_left`） |
| `last_block_id` / `last_page` / `percent` / `scroll_ratio` | — | 断点位置（刷新后恢复） |

**阅读器深链参数**（`/app/reader/<paperId>?...`，均可与 `?row=<rowId>`（兼容旧参数 `?block=<blockId>`）组合）：

| 参数 | 取值 | 说明 |
|---|---|---|
| `row` / `block` | 行 id / block_id | 打开后自动定位到该行（`block` 为兼容旧链接保留） |
| `view` | `original`\|`translation`\|`bilingual` | 覆盖服务端记录的视图模式 |
| `order` | `en_left`\|`zh_left` | 覆盖左右顺序（旧值 `en_top`/`zh_top` 仍接受） |
| `left` | `page`\|`text` | 左栏显示原 PDF 版面页图 / 原文文本 |
| `side` | `toc`\|`annotations`\|`notes`\|`formulas`\|`figures`\|`terms`\|`tasks` | 打开左栏（唯一侧栏）的某个页签 |
| `size` | `sm`\|`md`\|`lg` | 正文字号 |
| `md` / `mdpage` | `1` / 页号 | 直接打开 Markdown 视图并定位到第 N 页 |
| `nosse` | `1` | 强制走 SSE 降级路径（3s 轮询） |

阅读器布局：**两列**（左栏 300px + 正文），目录与批注/笔记/公式/插图/术语/任务合并进同一栏，
顶部一排带计数的图标页签切换；窄屏（<1100px）用工具栏按钮弹出同一栏。


## 3. 翻译（§20.3）—— 复核模式已移除

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/papers/{id}/translate` | 产线 A（**逐页翻译，唯一粒度**）：`{"mode":"resume\|retranslate","brief":true,"force":bool,"pages":[3,4]}` |
| GET | `/papers/{id}/translation/stats` | `{total, done, stale}`（**以页为单位**：total = 页数，done = 已有对齐行的页数） |
| GET/POST | `/papers/{id}/brief` | 文档简报（全文脉络 + 统一术语）；POST 可重新生成或写入人工修订（`{"apply":{"glossary":{...}}}`） |
| GET/DELETE | `/papers/{id}/translation/cache` | 页级产物状态（已翻译页数 / 行数 / token 用量 / 对齐质量分布）/ 清空（等于全篇重译） |
| GET/POST | `/papers/{id}/glossary` | 术语表（两级：paper/global） |

**翻译参数**

| 字段 | 默认 | 说明 |
|---|---|---|
| `mode` | `resume` | `resume` = 只翻还没有产物的页（断点续跑）；`retranslate` = 忽略已有产物全部重来（旧值 `retry_failed` 仍接受，按 `resume` 处理） |
| `force` | `false` | 强制重来（等价于 `retranslate`，且不复用进行中的同类任务） |
| `brief` | `true` | 文档简报（每篇 1 次调用，固化术语）；`false` 省一次调用但术语一致性下降 |
| `context` | `true` | 是否把**完整前文**（第 1..N-1 页原文+译文）放进本页 prompt（连贯记忆 + 前缀缓存） |
| `context_chars` | `2000000` | 前文长度上限（**仅护栏**：目标模型窗口 1M token，正常论文不截断） |
| `pages` | 无 | 只翻指定页（如 `[3,4]`），重跑个别页用 |

**粒度**：只有 `page`（一页一次调用）。调用输入 = 该页**页图（PNG，150dpi）** + 解析器抽出的
页内文本 + 稳定前缀（简报 + 术语表 + **全文标题骨架**）+ **完整前文历史**；输出 = `{"original_md", "translation_md"}`
两份**块划分一一对应**的 Markdown，再对齐成 `rows` 落盘 `md/pages.json`。
前文历史必须**完整且逐页追加**（表头不带页码），这样相邻两页的 prompt 共享最长前缀 → 命中前缀缓存；
`granularity` / `block_ids` / `strict_validation` / 回退补译 / 三层缓存**均已删除**。

**完全信任模型**：不做校验、不做回退补译。思考模式把 `max_tokens` 吃光导致正文为空时，
按阶梯升档重试（8192 → 16384 → 32768 → 65536），统计 `max_tokens_escalations`。

任务统计里会返回 `granularity / pages / done / failed / rows / align_structure_pages /
max_tokens_escalations / max_tokens_final / brief / usage(prompt_tokens, completion_tokens,
reasoning_tokens) / elapsed_s / scheduler.* / concurrency_timeline`，可直接用作第 5 章实验数据。

## 4. 任务中心（§20.7）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/tasks?status=&type=&queue=&paper_id=` | 列表（`queue` 对外为 `system/md/pdf/knowledge`） |
| GET | `/tasks/{id}` | 详情（进度、断点 cursor、日志尾部、统计） |
| POST | `/tasks/{id}/pause` | 请求暂停（**页边界生效**，已完成部分保留） |
| POST | `/tasks/{id}/cancel` | 请求取消（已完成译文保留） |
| POST | `/tasks/{id}/resume` | 从断点继续 |
| POST | `/tasks/reconcile` | 触发对账任务 |
| GET | `/tasks/stream` | SSE：`task_progress` |

## 5. SSE 事件（§20）

| 端点 | 事件 |
|---|---|
| `GET /api/v1/papers/{id}/translations/stream` | `page_translated`（**逐页**译文，payload：`paper_id/page/page_total/rows/align/model/done/total/task_id`）、`paper_status` |
| `GET /api/v1/tasks/stream` | `task_progress` |

> 旧版逐块事件 `block_translated` 与通用流 `GET /events/stream` 已删除（前者没有任何发布方，
> 后者零调用方 —— 前端只用上面两个专用流）。事件总线与 `bus.stream()` 本身保留。

SSE 断线时前端按 `GET /api/v1/papers/{id}/translations?since_page=N` 轮询补齐（按**页**返回）。

事件帧：`event: <type>` + `data: {"seq","type","topic","at","payload"}`；每 15s 发一次 `: ping` 心跳；
断线重连可带 `since_seq` 补发历史。

## 6. 模型管理（§20.7）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/models?model_type=` | 配置列表 |
| GET | `/models/types` | 各类型当前默认（前端不写模型名） |
| POST/PUT/DELETE | `/models[/{id}]` | 增删改 |
| POST | `/models/{id}/test` | 连通性 + 维度 + 延迟，写回 `status/last_tested_at` |
| PUT | `/models/{id}/default` | 设为该类型默认 |
| GET | `/models/presets` · POST `/models/import-presets` | 预设（deepseek/dashscope/zhipu/ollama/local_bge） |
| POST | `/models/secret` · GET `/models/secrets/list` | 写/列密钥文件（`data/secrets/`，**不落库**） |

## 7. 高亮 / 笔记 / 批注（§20.6）

`POST/GET/DELETE /highlights` · `POST/GET/PUT/DELETE /notes` · `GET /notes/overview` ·
`GET/POST/PUT/DELETE /annotations` ·
`POST /annotations/from-selection`（划词 AI 解释 → `{explanation, sources, model_id, grounded}`）·
`POST /annotations/save`（存为批注，`type=qa, source=selection`）

## 8. 知识库（§20.5）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/knowledge/overview` | 论文数 / chunk 数 / 索引版本 / 向量库状态 |
| POST | `/knowledge/papers` | 纳入或移出知识库 |
| POST | `/knowledge/index` | 触发 EMBED+INDEX（`{"paper_id":..., "rebuild":bool}`） |
| GET | `/knowledge/index/status` | MySQL chunk 与 LanceDB 条数对比 |
| GET | `/knowledge/chunks?paper_id=` | chunk **元数据**（不返回全文） |
| POST | `/rag/query` · `/rag/similar` | **未实现**（阶段 14–17），返回 `implemented: false` |

## 9. 设置与系统

`GET/PUT /settings`（白名单：并发上限 / RAG 参数 / 允许通用知识 / 渲染 dpi 等）·
`GET /settings/health` · `GET /health`（库、任务池、数据目录）

---

## 10. curl / PowerShell 示例

```powershell
$Base = "http://127.0.0.1:8710/api/v1"

# 导入
curl.exe -X POST "$Base/papers" -F "file=@paper.pdf" -F "auto_parse=true"

# 阅读器
(Invoke-RestMethod "$Base/papers/my-paper/reader").data.blocks.Count

# 触发翻译并看进度
$t = (Invoke-RestMethod -Method Post "$Base/papers/my-paper/translate" `
      -ContentType "application/json" -Body '{"mode":"resume"}').data
Invoke-RestMethod "$Base/tasks/$($t.id)" | ConvertTo-Json -Depth 4

# 订阅逐页译文（SSE）
curl.exe -N "$Base/papers/my-paper/translations/stream"

# 轮询兜底（按页）：只要页号大于 3 的产物
Invoke-RestMethod "$Base/papers/my-paper/translations?since_page=3" | ConvertTo-Json -Depth 4

# 公式列表（默认 source=markdown：从页级产物 Markdown 提取，只读）
(Invoke-RestMethod "$Base/papers/my-paper/formulas").data.display_count

# 带插图的逐页双语 Markdown（落盘 md/translated.md；?write=false 只返回不落盘）
Invoke-RestMethod "$Base/papers/my-paper/markdown/translated" | ConvertTo-Json -Depth 3

# 划词解释
Invoke-RestMethod -Method Post "$Base/annotations/from-selection" -ContentType "application/json" `
  -Body '{"paper_id":"my-paper","block_id":"my-paper-p02-b03","selected_text":"random rotation"}'
```

新增错误码：`NOT_FOUND(404)` `VALIDATION_ERROR(400)` `CONFLICT(409)`
`PARSE_ERROR(422)` `RATE_LIMITED(429)` `MODEL_NOT_CONFIGURED(503)` `PROVIDER_ERROR(502)` `TASK_ERROR(500)`。
