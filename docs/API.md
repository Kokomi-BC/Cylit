# API 说明（PaperMind 后端）

- 基址：`http://127.0.0.1:8710/api/v1`。**无前缀副本只挂了 `papers` 与 `reader` 两个 router**
  （`/papers/{id}/file`、`/papers/{id}/pages/{n}` 这类资源链接要能直接打开）；`knowledge` / `models` /
  `tasks` / `annotations` / `settings` **只有** `/api/v1` 一份
- 交互文档：`/docs`（Swagger） · `/redoc`
- 统一响应：`{"data": ..., "error": null|{code,message,detail}, "trace_id": "..."}`
- 长任务一律返回 `task_id`，**不阻塞等待**；重复触发返回同一进行中任务（幂等）——
  解析 / 索引是"整篇一个任务"；**翻译与理解按路幂等**（`paths` 里每一路各一个任务，`path` 不同即不同任务）

---

## 0. 前端托管（`/app/`）

前端是 Vite 构建的 SPA（`frontend/dist/`，history 路由，`base=/app/`），与后端同进程：

| 路径 | 行为 |
|---|---|
| `GET /` | 307 → `/app/` |
| `GET /app/` | 返回 `dist/index.html` |
| `GET /app/<真实文件>` | 直接返回（`assets/*.js|css`、KaTeX 字体…） |
| `GET /app/<前端路由>` | 回退 `index.html`（如 `/app/reader/turboquant-online-vector-iclr-26`，刷新不 404） |
| `GET /app/<带扩展名但不存在>` | **404**（返回 HTML 会让浏览器把 HTML 当 JS/CSS 解析，报更难懂的 MIME 错误） |
| `GET /app/...`（未构建） | 503 + 构建指引页（`cd frontend && npm install && npm run build`），不静默 404 |

- 开发模式：`cd frontend && npm run dev` → `http://127.0.0.1:5173/app/`（Vite 已把 `/api`、`/papers`、
  `/health` 代理到 8710；后端地址用 `PR_BACKEND` 覆盖）。
- `?nosse=1`（可加在任意前端路由上）：强制走「SSE 断开 → 3s 轮询」降级路径。
  用于禁用长连接的环境，也让无头浏览器自检能拿到 DOM（长连接会让 virtual-time 永不推进）。
- 前端自检：`python scripts/frontend_check.py --block-markers`（静态+接口+真实渲染，本轮 88 项）。

---

## 1. 论文与解析（§20.1）

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/papers` | 上传 PDF（multipart：`file`,`title`,`authors`,`venue`,`year`,`tags`,`paper_id`,`auto_parse`）。同一文件（sha256）已导入时**复用现有论文**并返回 `reused: true` |
| GET | `/papers` | 列表：`q/status/tag/sort/order/page/page_size`、`include_deleted`（默认 `false`） |
| GET | `/papers/{id}` | 详情（状态、解析摘要、翻译计数）。**复核模式已移除**：不再返回 `review_open`（"待复核 N"计数） |
| PUT | `/papers/{id}` | 更新元数据（title/authors/venue/year/tags/status/in_knowledge_base/abstract） |
| DELETE | `/papers/{id}?hard=false` | 删除：**先删索引 → 取消任务 → 清 chunk → 文件移入 trash** |
| POST | `/papers/{id}/parse` | 触发解析（幂等）。query 参数：`force`、`llm_repair`（LLM 公式修复，需真实模型）、`vision`（扫描页交视觉模型，默认 true）、`vision_model`（指定视觉模型 id）、`then_translate`（解析完成后自动接着开翻译任务）。**没有** `vision_dpi` 参数（dpi 取设置 `render_dpi` / 视觉默认 200，由 worker 读） |
| GET | `/papers/{id}/file` | PDF 流（阅读器左栏「原 PDF」由 pdf.js 渲染；`Content-Disposition: inline`，新标签页可直开） |
| GET | `/papers/{id}/pages/{n}` | 页面图（150dpi PNG） |
| GET | `/papers/{id}/markdown` | Markdown 单一事实源全文 |
| GET | `/papers/{id}/reconcile` | 三存储对账（只报告不自动删） |
| POST | `/papers/{id}/metadata/auto` | 自动补全元数据：`{"model":false,"overwrite":false}`。默认**只读 PDF**（离线、不花钱）：标题/作者/会议/年份/摘要都能从首页读出来；`model=true` 时仍缺的字段再交给模型；`overwrite=true` 连已有值一起覆盖（默认只填空字段） |

**扫描件（影印 PDF）**：解析时自动判定，无需额外参数。某页文本层字符 < 40 且含整页图像
→ 该页交给视觉模型整页识别成 Markdown 原文，再走既有翻译链路。相关字段：

- `POST /papers/{id}/parse` 参数 `vision`（默认 true）、`vision_model`（指定视觉模型 id）；
- 解析结果 `stats.scanned_pages` 列出被判定为扫描的页；这些页 `page.has_text_layer = false`；
- 扫描页的块坐标是整页等分的虚拟横带（没有矢量层），因此**不做**公式卡与插图裁剪，
  公式以 `$$…$$` 留在正文里。

## 2. 阅读器数据（§20.2）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/papers/{id}/reader` | reader.json：`paper / blocks / pages / page_translations / block_rows / progress / annotations / highlights / notes / tasks / outline / stats`；`since` 支持增量，另带 `generated_at` / `paper_dir`。`block_rows` 是**块 id → 正文行 id** 的映射（检索/RAG 给的是解析块 id，前端靠它精确跳到那一行）。批注/笔记/高亮条目均带 `page`（行 id→页，回退块 id→页；都查不到为 `null`，前端只提示不乱跳） |
| GET | `/papers/{id}/content?since=&limit=` | 按 block 取正文（增量） |
| GET | `/papers/{id}/translations?since_page=N&page=M` | 译文增量（LiveUpdater 轮询兜底用）：返回 **`{items: [{page, page_total, rows, align, model, updated_at}], count, since_page}`**（注意是包了一层的结构）；`page=` 可重复，指定页就只回这几页 |
| GET | `/papers/{id}/markdown/translated?write=true` | **带插图的逐页双语 Markdown**：`{paper_id, pages:[{page, page_total, original_md, translation_md, align, model}], markdown, path, count}`；同时落盘 `data/papers/<id>/md/translated.md`（图片用 `../figures/…` 相对路径）。`?write=false` 只返回不落盘 |
| GET | `/papers/{id}/blocks/{block_id}` | 单块详情（含解析阶段公式卡） |
| GET | `/papers/{id}/formulas?source=markdown\|parse` | 公式列表。**默认 `source=markdown`**：从页级产物 Markdown 里提取，返回 `{items, count, source: "markdown", display_count}`；`items[] = {id, page, row_id, latex, display, context, context_zh, model}`（`row_id` 是页级产物的行 id，用来定位正文；`display=true` = 行间 `$$…$$`、`false` = 行内）。传 `source=parse` 才返回解析阶段的公式卡（`latex_final / grade / confidence / vector_audit / validation / crop_url`，含裁图与三重校验证据），**仅供解析质检参考**。旧参数 `grade` 只接受 `A`\|`B`\|`C`，**取值被忽略**；传其它值直接 422 |
| GET | `/papers/{id}/figures` | 插图列表：`{items, count}`；每条含 `block_id` / `row_id`（= 该图**图注行的行 id**，由图号反查）/ `page` / `number` / `caption` / `bbox` / `image_url` / `card_url` / `part` / `parts` / `part_label` / `status` / `audit`。`image_url` 与 `card_url` 都是 **「图 + 图注」合成卡片**（URL 带 `?card=1`），不是裸裁图。行 id 失效（重译后行号变了）时前端按 `page` 定位 |
| GET | `/papers/{id}/figures/region/{page}/{index}` | 插图裁图（**600dpi 按需渲染 + 落盘缓存**），裁图区域**按图注/标题锚定收紧**（`backend/services/paper/figure_regions.py`） |
| GET | `/papers/{id}/outline` | **目录（按页级翻译产物的标题行，中英双语）**：`{items（扁平）, tree（嵌套）, count}`；每项 `block_id / page / level / number / text（英文标题）/ text_zh（中文标题）/ children` |
| GET/PUT | `/papers/{id}/progress` | 阅读进度（服务端权威，含 `view_mode` / `pair_order` / `scroll_ratio`） |

`reader.pages[]` 关键字段：`page / width / height / columns / gutter_x / blocks /
has_text_layer / translated / row_count / align / media / source`（`media` 是该页插图；
`source` 标明这一页的行是 `paths` 两路合并还是旧形态）。
`reader.page_translations[]` 是逐页产物：`page / page_total / original_md / translation_md /
rows[] / align / model / updated_at`。
`reader.stats` = `{pages, pages_done, rows, align, model, blocks, by_type, translated,
translate_total, translate_percent, deleted_at}`（**以页为进度口径**）。**复核模式已移除**：stats 不再有
`reviews_open`，`paper` 也不再有 `review_open` 列（解析阶段的一致性检查与插图体检只进
`result.stats["consistency_findings"]` 与质量报告）。

`reader.blocks[]` 关键字段：`id / page / type / col / order / bbox / anchor / text / content_hash /
section / heading_level / translation / translation_status / translation_revision / formula`。

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
| `left` | `page`\|`text` | 左栏显示原 PDF / 原文文本（非扫描页的 PDF 栏是**真 PDF**：pdf.js 矢量页 + 可复制文字层；扫描页仍是位图页图） |
| `side` | `toc`\|`search`\|`annotations`\|`formulas`\|`figures`\|`terms`\|`tasks`\|`insight`\|`readmap` | 打开左栏（唯一侧栏）的某个页签（会顺带展开左栏）：`insight` = 「论文解读」（A 路论文精读）、`readmap` = 「阅读地图」（B 路 + 相关概念卡片）。旧值 `notes` 仍接受但归一成 `annotations`（批注与笔记已合并为一个页签） |
| `q` / `scope` | 关键词 / `all`\|`en`\|`zh` | 打开时直接跑一遍左栏搜索（`scope` 指定检索范围） |
| `pside` | `en`\|`zh` | 对照模式里先看哪一侧 |
| `size` | `sm`\|`md`\|`lg` | 正文字号 |
| `md` / `mdpage` | `1` / 页号 | 直接打开 Markdown 视图并定位到第 N 页 |
| `nosse` | `1` | 强制走 SSE 降级路径（3s 轮询） |

阅读器布局：**两列**（左栏 + 正文），目录/搜索/批注/笔记/公式/插图/术语/**论文解读**/**阅读地图**/任务
合并进同一栏，顶部一排带计数的图标页签切换；窄屏（<1100px）用工具栏按钮弹出同一栏。
**工具栏没有「解读」按钮**（解读已经是左栏页签，重复入口已删）。
左栏宽度**可拖拽**，按论文存 `localStorage: pm.reader.aside-w.<paperId>`；上下限**按模式给**：
对照模式（`view_mode=bilingual`）220~360px，非对照 220~640px（且不超过视口 60%）；
**进「论文解读」/「阅读地图」页签自动变宽到 520px**（按当前上限夹取，只增不减）。
两个页签顶部各有**一枚只有图标的按钮**（`external`，无可见文字），点击在**应用内标签页**
（顶栏 `TabBar`，`stores/tabs.ts` 的 `openTab`）打开对应的整页视图 ——
`/understand/:paperId`（论文解读，A 路）与 `/readmap/:paperId`（阅读地图 + 相关概念，B 路）；
两个视图共用一个组件，正文排版与阅读器同一套（`--reader-font-size` / 表格 / 公式）。
**文本对照模式下**（对照 + 两端都是文本、没有原 PDF）**表格行左右并排**（两端各占一列、各自横向滚动），
不再上下堆叠；插图仍是通栏。




## 3. 翻译 / 转录（§20.3）—— 两路并发，复核模式已移除

**两路流水线**（`paths`）：A 路 = **英文转录**、B 路 = **中文翻译**，两路是**两个任务、真并发**
（`task_worker_count = 3`；同一路内部页序串行，这是前缀缓存成立的前提）。产物分开落盘：

| `path` | 任务类型 | 产物 | 说明 |
|---|---|---|---|
| `a` | `TRANSCRIBE_EN` | `md/path_a.json` | `original_md` / `breaks` / `src`；事件 `page_transcribed` |
| `b` | `TRANSLATE_ZH` | `md/path_b.json` | `translation_md` / `breaks` / `src`；事件 `page_translated`（带 `path`） |
| `fused` | `TRANSLATE_MD` | `md/pages.json` | 融合**对照臂**（改造前行为，逐字保留）；**只能单独跑**（与 `a`/`b` 同传会 400 —— 它写的是同一份 `pages.json`）。`"both"` / `"none"` / `""` 都归一成 `fused` |

阅读器读的是**两路产物的合并**：两侧都带合法 `src` 时按**解析器块号**交格合并
（`align: "coords"`，1:N 合成一行，既不错位也不留空栏），任一侧缺 `src` 就退回结构配对
（`exact` / `structure`）。设计见 `docs/论文理解系统-设计.md` §3。

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/papers/{id}/translate` | 页级翻译/转录（**唯一粒度**）：`{"paths":["a","b"],"mode":"resume\|retranslate","brief":true,"force":bool,"pages":[3,4]}`。**按路幂等**：该路已有活着的任务时直接返回它并带 `reused: true`（**连 force 也一样** —— 同一路并发两套页翻译会互相覆盖产物；已请求取消/租约过期的僵尸任务会被丢弃后新建）。前置：论文已解析（没有页图时后端会顺带建解析任务并返回它） |
| GET | `/papers/{id}/translation/stats` | `{total, done, stale}` —— **只报 B 路（中文翻译）**完成度；A 路的进度在 `Paper.transcribe_done`。`stale` 目前恒为 `0` |
| GET/POST | `/papers/{id}/brief` | 文档简报（全文脉络 + 统一术语）；POST 可重新生成或写入人工修订（`{"apply":{"glossary":{...}}}`） |
| GET/DELETE | `/papers/{id}/translation/cache` | 页级产物状态（已翻译页数 / 行数 / token 用量 / 对齐质量分布）/ 清空（等于全篇重译） |
| GET/POST | `/papers/{id}/glossary` | 术语表（两级：paper/global） |

**翻译参数**

| 字段 | 默认 | 说明 |
|---|---|---|
| `paths` | `["a","b"]` | 跑哪几路：`a` 英文转录 / `b` 中文翻译 / `fused` 融合对照臂（单独跑）。每路一个任务、真并发 |
| `mode` | `resume` | `resume` = 只翻还没有产物的页（断点续跑）；`retranslate` = 忽略已有产物全部重来（旧值 `retry_failed` 仍接受，按 `resume` 处理） |
| `force` | `false` | 强制重来（等价于 `retranslate`）。**仍会复用该路进行中的任务**（不新建、不取消旧的） |
| `brief` | `true` | 文档简报（每篇 1 次调用，固化术语）；`false` 省一次调用但术语一致性下降（A 路本来就不用简报） |
| `context` | `true` | **任务级参数**（`/translate` 不透传）：是否把**完整前文**（第 1..N-1 页）放进本页 prompt（连贯记忆 + 前缀缓存） |
| `context_chars` | `2000000` | **任务级参数**（同上）：前文长度上限（**仅护栏**：目标模型窗口 1M token，正常论文不截断）。注意别和设置里的 `translate_max_context_chars`（1200，另一回事）混淆 |
| `pages` | 无 | 只翻指定页（如 `[3,4]`），重跑个别页用 |

**粒度**：只有 `page`（一页一次调用）。调用输入 = 该页**页图（PNG，150dpi）** + 解析器抽出的
页内文本 + 稳定前缀（简报 + 术语表 + **全文标题骨架**）+ **完整前文历史**；A 路输出 `original_md`
（+ `breaks` / `src`）、B 路输出 `translation_md`（+ `breaks` / `src`），合并成阅读器的 `rows`。
前文历史必须**完整且逐页追加**（表头不带页码），这样相邻两页的 prompt 共享最长前缀 → 命中前缀缓存；
`granularity` / `block_ids` / `strict_validation` / 回退补译 / 三层缓存**均已删除**。

**完全信任模型**：不做校验、不做回退补译。思考模式把 `max_tokens` 吃光导致正文为空时，
按阶梯升档重试（8192 → 16384 → 32768 → 65536），统计 `max_tokens_escalations`。

任务统计里会返回 `path`（`a`/`b`/`fused`）/ `granularity`（恒为 `page`）/ `pages` / `done` / `failed` / `rows` / `align_structure_pages` /
`max_tokens_escalations` / `max_tokens_final` / `brief` / `usage(prompt_tokens, completion_tokens,
reasoning_tokens, cache_hit_tokens, cache_miss_tokens)` / `elapsed_s` / `scheduler.*` / `concurrency_timeline`，
可直接用作第 5 章实验数据。

## 4. 论文理解（第二阶段：两路各做一次）

理解**不是**翻译的一部分：它读的是**已落库的 chunk**（不是页图），提示词与翻译提示词完全分开，
两路做**不同**的任务，结果是自由 Markdown。设计见 `docs/论文理解系统-设计.md` §4。

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/papers/{id}/understand` | 触发理解：`{"paths":["a","b"],"force":false}`。**两路各一个任务、真并发**；幂等（同类进行中任务复用）。`force=false`（默认）时已有结果直接返回库里那份。前置条件：**已建索引** —— 没有可理解的 chunk 时 400 |
| GET | `/papers/{id}/understanding` | 取结果：`?path=a\|b` 取一路；不传则两路一起返回 `{paper_id, a, b, done}` |
| GET | `/papers/{id}/understanding/status` | `{a:{done,updated_at}, b:{...}, chunk_ready}`（前端按钮与"能不能理解"的判断） |
| GET | `/papers/{id}/related?page=N&tolerance=1` | 与某页相关的概念。**这个接口是给知识库用的**（"读到这一页时相关的知识"），**阅读器不再调用它**。命中判据：概念的引用里有 chunk 落在本页 ±`tolerance`。没有解读结果时返回空列表，不报错 |

**单路结果字段**：`paper_id / path / label / markdown / refs / refs_resolved / stale_refs /
invalid_refs / cross_road_refs / refs_ok / missing_concept_refs / model / updated_at /
prompt_tokens / completion_tokens / inventory_size / inventory / concepts`。

- `markdown`：**自由 Markdown**（不按固定 schema 校验，模型可增删合并小节）；
- `refs`：模型引用过的**短码**（A 路 `A001…`、B 路 `B001…`）；`refs_resolved` 每项是 `{code, chunk_id, chunk_type, page}`；
- `stale_refs`：短码解析出的 chunk id **已不存在**（重新翻译 / 重建索引之后）—— 引用**保留**、页码还在、照样能跳，
  前端标一句"引用已过期"。这是"**重新翻译不影响已有解读**"的落点；
- `invalid_refs` / `cross_road_refs`：不存在的短码 / 跨路引用；非空时 `refs_ok=false`（前端标"引用不可信"，**结果不丢**）；
- `concepts`：**读时投影**（B 路「相关概念」那一节 → `[{name, name_en, body_md, refs[]}]`），不改表、不必重新生成；
- `missing_concept_refs`：B 路"相关概念"里一条本路短码都没有（硬要求，实测 `false`）。

| | A 路（`path=a`，英文侧） | B 路（`path=b`，中文侧） |
|---|---|---|
| 任务 | **论文精读**：基本信息 / 背景与问题 / 动机与 gap / **贡献与创新点** / 方法 / 实验 / 结论、局限与启发 | **阅读地图**：一句话概括 / **相关概念（补充的相关知识，放最前）** / 推荐阅读顺序 / 读前须知 / 难点 / 与其他工作的关系 |
| 材料 | 本路 + 共用 chunk 的 `en` / `any` 行（`unit`/`formula`/`figure`/`table`） | 本路 + 共用 chunk 的 `zh` / `any` 行 |

**引用写法**：`（来源 Chunk: B023）`，只许引**本路**短码。理解阶段**允许**动用通用知识、不强制区分
"论文说的"与"通用知识"；知识库问答 `/rag/query` 相反，仍严格"只依据来源"。

## 5. 任务中心（§20.7）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/tasks?status=&type=&queue=&paper_id=` | 列表（`queue` 对外为 `system/md/pdf/knowledge`） |
| GET | `/tasks/stats?paper_id=` | 统计（前端自检在用） |
| GET | `/tasks/{id}` | 详情（进度、断点 cursor、日志尾部、统计） |
| POST | `/tasks/{id}/pause` | 请求暂停（**块/段落边界生效**，已完成部分保留） |
| POST | `/tasks/{id}/cancel` | 请求取消（已完成译文保留） |
| POST | `/tasks/{id}/resume` | 从断点继续 |
| POST | `/tasks/{id}/retry` | 等价 `resume`（从首个未完成项继续） |
| POST | `/tasks/reconcile` | 触发对账任务 |
| POST | `/tasks/cleanup` | `{paper_id?, statuses?}`：清已结束任务；**不传 `paper_id` 就是全库清**（`pending`/`processing`/`paused` 不动） |
| DELETE | `/tasks/{id}` | 删单条任务 |
| GET | `/tasks/stream` | SSE：`task_progress` |

## 6. SSE 事件（§20）

| 端点 | 事件 |
|---|---|
| `GET /api/v1/papers/{id}/translations/stream` | `page_translated`（B 路 / 融合：payload `paper_id/page/path/page_total/rows/align/model/done/total/task_id`）、`page_transcribed`（A 路）、`paper_status` |
| `GET /api/v1/tasks/stream` | `task_progress` |
| `POST /api/v1/rag/query/stream` | `rag_meta` → `rag_token`* → `rag_done` / `rag_error` |

> 旧版逐块事件 `block_translated` 与通用流 `GET /events/stream` 已删除（前者没有任何发布方，
> 后者零调用方 —— 前端只用上面两个专用流）。事件总线与 `bus.stream()` 本身保留。

SSE 断线时前端按 `GET /api/v1/papers/{id}/translations?since_page=N` 轮询补齐（按**页**返回）。

事件帧：`event: <type>` + `data: {"seq","type","topic","at","payload"}`；每 15s 发一次 `: ping` 心跳；
断线重连可带 `since_seq` 补发历史。

## 7. 模型管理（§20.7）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/models?model_type=` | 配置列表 |
| GET | `/models/types` | 各类型当前默认（前端不写模型名） |
| POST/PUT/DELETE | `/models[/{id}]` | 增删改 |
| POST | `/models/{id}/test` | 连通性 + 维度 + 延迟，写回 `status/last_tested_at` |
| PUT | `/models/{id}/default` | 设为该类型默认 |
| GET | `/models/presets` · POST `/models/import-presets` | 预设（deepseek/dashscope/zhipu/ollama/local_bge） |
| POST | `/models/secret` · GET `/models/secrets/list` | 写/列密钥文件（`data/secrets/`，**不落库**） |

## 8. 高亮 / 笔记 / 批注（§20.6）

`POST/GET/DELETE /highlights` · `POST/GET/PUT/DELETE /notes` · `GET /notes/overview` ·
`GET/POST/PUT/DELETE /annotations` ·
`POST /annotations/from-selection`（划词 AI 解释 → `{explanation, sources, model_id, grounded}`）·
`POST /annotations/save`（存为批注，`type=qa, source=selection`）

## 9. 知识库与检索问答（§20.5 / §20.4）

### 9.1 知识库（§20.5）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/knowledge/overview` | 论文数 / chunk 数 / 索引版本 / 向量库状态；另有 `chunks_by_type`、`chunks_by_lang`、**`chunks_by_road`**（`a` 英文转录 / `b` 中文翻译 / `shared` 图表公式） |
| POST | `/knowledge/papers` | 兼容保留：只翻"已纳入"标记（不动索引）。界面不用它 —— 口径是**建索引即纳入** |
| POST | `/knowledge/remove` | **从知识库移除**：`{paper_id}`（缺则 400）；清向量 + 清 chunk 元数据 + 取消"已纳入"（论文与译文不动），返回 `removed_chunks` |
| POST | `/knowledge/index` | 触发 EMBED+INDEX（`{"paper_id":..., "rebuild":bool}`） |
| POST | `/knowledge/reconcile` | 对账：MySQL chunk ↔ 向量库；`{paper_id?, fix:false}`，`fix=true` 顺手清孤儿 |
| GET | `/knowledge/index/status?paper_id=` | MySQL chunk 与 LanceDB 条数对比 |
| GET | `/knowledge/chunks` | chunk **元数据**（不返回全文）：`paper_id`（必填）、`chunk_type`、`lang`、**`road`**（`a`/`b`/`shared`，正则校验）、`limit`（默认 50，≤500）。默认**不列** `unit`（那是检索粒度，浏览视图会翻倍）；`image_url` 是插图/表格/公式的裁图 |
| GET | `/knowledge/chunk?chunk_id=` | **单个 chunk 的完整正文**（知识库页「展开全文」；只读那一条向量行，不触发任务）。公式条目另带 `latex` / `display` / `row_id` / `context` / `anchor_precision` 等契约字段 |

### 9.2 检索与问答（§20.4，**已实现**）

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/rag/similar` | 相似检索：向量 + 词法 → RRF → 重排，返回 `{implemented: true, query, source, scope, items[], retrieval}`。请求体给 `{query}` 或 `{chunk_id}`（都没有 → 400；给 `chunk_id` 时用它的原文当查询并排除自己）。`scope` ∈ `all` / `cross` / `paper`（**默认**：给了 `paper_id` 就是 `cross`，否则 `all`；其它值 400）；另有 `paper_id` / `chunk_types` / `langs` / **`roads`** / `top_k` |
| POST | `/rag/query` | RAG 问答：混合检索 → ContextBuilder → 生成 → AnswerValidator。请求 `{question, scope, paper_id?, top_k?}`；`scope` 的**真值**只有 `current_paper` / `paper`（同义，需要 `paper_id`）、`cross`，**其它值（含默认的 `knowledge_base`）一律落到全库 `all`**。响应 `{implemented, answer, sources[], grounded, validation, retrieval_metadata, context{sources,chars,truncated}, usage}`；没有可用来源时多一个 `insufficient: true`（答案里带 `【无法回答】` 标记），**不编答案** |
| POST | `/rag/query/stream` | 同上，SSE 流式：`rag_meta` → `rag_token`* → `rag_done` / `rag_error` |

**检索单位**：候选集 = `unit` / `formula` / `figure` / `table`（正文 `text` 块不进候选，避免同一批行两种粒度互相挤掉），
命中单元后按 `kb.unit_expand` 向父块内取 ±1 邻居（small-to-big）。两路分块后可按 `roads` 过滤。
阅读器左栏的「语义」搜索模式走 `/rag/similar` 带 `{scope:'paper', paper_id}`（**只查本文**）。

> 口径差异（有意为之）：**理解**（§4）允许动用通用知识且不区分来源；**问答**（本节）严格"只依据来源"。

## 10. 设置与系统

`GET/PUT /settings`（白名单：并发上限 / RAG 参数 / 允许通用知识 / 渲染 dpi 等）·
`GET /settings/health` · `GET /health`（库、任务池、数据目录）

---

## 11. curl / PowerShell 示例

```powershell
$Base = "http://127.0.0.1:8710/api/v1"

# 导入
curl.exe -X POST "$Base/papers" -F "file=@paper.pdf" -F "auto_parse=true"

# 阅读器
(Invoke-RestMethod "$Base/papers/my-paper/reader").data.blocks.Count

# 触发翻译并看进度（两路并发；返回 tasks.{a,b}）
$t = (Invoke-RestMethod -Method Post "$Base/papers/my-paper/translate" `
      -ContentType "application/json" -Body '{"mode":"resume","paths":["a","b"]}').data
Invoke-RestMethod "$Base/tasks/$($t.tasks.a.id)" | ConvertTo-Json -Depth 4

# 只重跑个别页（每路各一个任务）
Invoke-RestMethod -Method Post "$Base/papers/my-paper/translate" -ContentType "application/json" `
  -Body '{"mode":"retranslate","paths":["b"],"pages":[3,4]}'

# 订阅逐页产物（SSE；A 路发 page_transcribed，B 路发 page_translated）
curl.exe -N "$Base/papers/my-paper/translations/stream"

# 轮询兜底（按页）：只要页号大于 3 的产物（注意外层是 {items,count,since_page}）
(Invoke-RestMethod "$Base/papers/my-paper/translations?since_page=3").data.items.Count

# ---- 第二阶段「论文理解」（前置：已建索引）----
# 两路各一个任务：A 论文精读（含创新点） / B 阅读地图与相关概念
$u = (Invoke-RestMethod -Method Post "$Base/papers/my-paper/understand" `
      -ContentType "application/json" -Body '{"paths":["a","b"]}').data
$u.task_ids

# 取结果（两路一起）；单路用 ?path=b
$r = (Invoke-RestMethod "$Base/papers/my-paper/understanding").data
$r.b.markdown.Length; $r.b.refs_ok; $r.b.concepts.Count

# 与第 3 页相关的概念（阅读器左栏「解读」跟随阅读位置用）
(Invoke-RestMethod "$Base/papers/my-paper/related?page=3&tolerance=1").data.concepts.name

# 公式列表（默认 source=markdown：从页级产物 Markdown 提取，只读）
(Invoke-RestMethod "$Base/papers/my-paper/formulas").data.display_count

# 带插图的逐页双语 Markdown（落盘 md/translated.md；?write=false 只返回不落盘）
Invoke-RestMethod "$Base/papers/my-paper/markdown/translated" | ConvertTo-Json -Depth 3

# 限本文的语义检索（阅读器左栏「语义」模式就是这条）
Invoke-RestMethod -Method Post "$Base/rag/similar" -ContentType "application/json" `
  -Body '{"query":"how is the distortion bound proved","paper_id":"my-paper","scope":"paper","top_k":8}'

# RAG 问答（严格只依据来源）
Invoke-RestMethod -Method Post "$Base/rag/query" -ContentType "application/json" `
  -Body '{"question":"TurboQuant 的失真上界是多少？","scope":"current_paper","paper_id":"my-paper"}' |
  ConvertTo-Json -Depth 3

# 划词解释
Invoke-RestMethod -Method Post "$Base/annotations/from-selection" -ContentType "application/json" `
  -Body '{"paper_id":"my-paper","block_id":"my-paper-p02-b03","selected_text":"random rotation"}'
```

新增错误码：`NOT_FOUND(404)` `VALIDATION_ERROR(400)` `CONFLICT(409)`
`PARSE_ERROR(422)` `RATE_LIMITED(429)` `MODEL_NOT_CONFIGURED(503)` `PROVIDER_ERROR(502)` `TASK_ERROR(500)`。
