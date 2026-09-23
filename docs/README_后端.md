# PaperMind · 智能论文阅读与知识管理系统（后端）

> 依据《智能论文阅读与知识管理系统 —— 总体规划 v3.0（合并版）》实现。
> **本地优先**、**解析与质保为核心**、**逐页翻译（一页一次调用）**、**任务可控可恢复**。

本轮交付范围（与规划阶段对应）：**阶段 01–10 —— 解析与阅读主干**

```
PDF 论文 → 可靠解析 → 结构化(paper/page/block) → 公式与图片正确处理
        → 原文 + 中文翻译（逐页渐进上线） → 阅读进度
```

---

## 1. 快速开始

```powershell
# 1) 依赖（conda 环境 scenemind）
conda run -n scenemind python -m pip install -r requirements.txt

# 2) 一键启动（自动：检查环境 → 初始化库 → 起后端；dist 缺失时会提示并代跑 npm run build）
.\scripts\start.bat
#    打开 http://127.0.0.1:8710/docs 查看全部 API

# 3) 导入 + 解析一篇论文
python cli/pr.py build "C:\path\to\paper.pdf" --id my-paper

# 4) 逐页翻译（未接入真实模型时用 Mock 占位，链路完全可跑）
python cli/pr.py translate my-paper [--mode resume|retranslate] [--pages 3,4]

# 5) 端到端验收（35 项自检）
python scripts/e2e_check.py
```

配置：复制 `.env.example` 为 `.env`（数据库、并发、密钥目录等）。
**密钥不落库、不进代码**：写到 `data/secrets/<name>` 文件即可（`/models` 页也可写入）。

---

## 2. 本轮交付了什么

| 模块 | 内容 | 规划对应 |
|---|---|---|
| **提取层** | 文本层（span 级 bbox/字体）、矢量层（线段/路径）、位图层、页面渲染 | §13.1 |
| **结构层** | 自研行聚类（不依赖 PyMuPDF 块边界）、双栏阅读顺序、段落切分、页眉页脚剔除、跨页续接与拆词修复、公式块识别、插图切分与体检、表格降级 | §13.2 |
| **公式链** | 矢量层审计（判根号范围/分式线/上下标）、LaTeX 候选恢复、三重校验、A/B/C 分级、高倍裁图、C 级 Markdown 兜底写法、LLM 修复证据契约（可选） | §13.3、§14 |
| **质保层** | 一致性约束（常数/幂次/定理↔附录）、插图体检、质量指标表（复核清单已随复核模式移除） | §13.3 C2–C4、§14.6 |
| **IR** | 每篇一个 SQLite（9 张表，`review` 表已随复核模式删除）+ `block_id` 稳定重绑定（page + bbox + 文本指纹） | §9 |
| **Markdown** | 解析单一事实源：块标记 + 锚点 + `blocks.json` 阅读器索引；**译文产物独立**在 `md/pages.json`（页级）；另有**带插图的双语 Markdown** 导出（`md/translated.md`，`GET /papers/{id}/markdown/translated`） | §8、§15 |
| **业务库** | MySQL 12 张表（原 `review` 表已随复核模式删除，含 `setting`），绝不存正文（只存 ≤200 字预览）；**启动时 `init_db()` 会幂等加宽历史窄列**（如 `chunk.id = "{paper_id}:{type}:{块范围}"` 最长 ~105 字，老库 `varchar(64)` 会报 1406） | §10 |
| **模型层** | Provider 抽象（generation/embedding/reranker）+ ModelManager（默认路由/能力探测/维度校验）+ Mock 离线兜底 + OpenAI 兼容 + 本地 BGE（可选） | §12 |
| **任务系统** | MySQL 任务表 + 租约 + 心跳 + 断点续跑 + 暂停/取消（页边界生效）+ 回收过期租约 | §10 ⑨、R13 |
| **自适应并发** | N∈[1,8] 动态窗口、429 退避（遵守 `Retry-After`）、连续成功回升、连续失败熔断、token/耗时记录、并发时间线 | §3.1 A3 |
| **翻译产线 A** | **页级翻译（唯一粒度）**：页图（PNG, 150dpi）+ 页内文本 + 稳定前缀 → 同一次调用产出 `original_md` / `translation_md` 两份逐块对应的 Markdown → 行对齐落盘；断点续跑 + 暂停取消 + 逐页 SSE | §15、§21.3 |
| **API** | 约 40 个端点：论文/阅读器/内容/译文（按页）/公式（**从 Markdown 提取**）/插图（含按需裁图）/双语 Markdown/进度/任务/模型/设置/高亮/笔记/批注/知识库 + SSE | §20 |
| **CLI** | `init / import / parse / build / structure / qa / translate / index / reconcile / stats / serve / bench` | §23 |
| **测试** | 单元测试（解析/调度/页级翻译/Markdown 往返/插图裁图/列宽守卫）+ 端到端（真实 PDF）+ 35 项验收脚本 + **41 项前端渲染自检**（含原 PDF 文字层 / 扫描件位图回退两个用例）+ 跨页衔接体检（`scripts/check_boundaries.py`）（`pytest` 102 passed / 1 skipped） | §24 每阶段验收 |

**明确未做（下一轮）**：RAG 检索与问答（阶段 14–17，接口占位并返回 `implemented: false`）、
版面保留 PDF（阶段 18–20，任务类型已注册但返回"未实现"）、AI 辅助阅读与结构分析（P1）、Vision/OCR（P2）。

---

## 3. 目录结构

```
paper-reader/
├─ backend/
│  ├─ api/            薄路由（统一响应 {data,error,trace_id}）
│  ├─ services/       ★ 唯一允许访问 repositories/providers/向量库的层
│  │  ├─ paper/       paper_service · reader_service · figure_regions（图注锚定的插图裁图 + 600dpi 按需渲染）· parse_hooks
│  │  ├─ translation/ translation_service · page_translate · brief
│  │  ├─ task/        task_service(+runner) · scheduler(自适应并发) · handlers
│  │  ├─ model/       model_manager · model_service · presets
│  │  ├─ annotation/  annotation_service（高亮/笔记/批注/划词解释）
│  │  └─ knowledge/   knowledge_service（chunk/embedding/索引）
│  ├─ providers/      base · generation(openai_compatible/mock) · embedding · factory
│  ├─ repositories/   mysql(12 表) · vector(LanceDB) · fs · ir
│  ├─ parsing/        ★ 解析与质保（CLI 与服务端共用）
│  │  ├─ extract/     text_layer · vector_layer · image_layer · render
│  │  ├─ structure/   reading_order · paragraph · formula_block · figure_cut
│  │  ├─ formula/     audit · latex_recover · validate
│  │  ├─ qa/          consistency · figure_audit · report
│  │  ├─ ir/          schema.sql · ir_writer · ir_reader · rebind
│  │  ├─ md/          generator（Markdown/译文/reader 索引）
│  │  ├─ models.py    数据结构契约
│  │  └─ pipeline.py  PARSE 全流程编排
│  ├─ core/           config · logging · errors · utils · events
│  └─ main.py         入口（REST + SSE + 静态资源 + TaskRunner）
├─ cli/pr.py          命令行（与服务端共用 parsing）
├─ scripts/           start.bat（一键启动） · e2e_check.py · frontend_check.py（验收）
├─ tests/             pytest（单元 + 端到端）
├─ data/              papers/ · index/ · secrets/ · logs/ · trash/
└─ docs/              README · dev-log · API 说明
```

---

## 4. 数据流（实际实现）

### 4.1 解析（`PARSE`）

```
render_pages(150dpi) → pages/*.png
提取 span / 矢量路径 / 位图（中间 JSON 落 reports/）
自研行聚类 → 双栏阅读顺序 → 段落切分（行距基线 + 缩进 + 句末标点）
跨页段落续接 + 页边界拆词修复
公式：区域候选 → 矢量层审计 → LaTeX 候选 → 三重校验 → A/B/C → 高倍裁图
插图：图注定位 → 图体 bbox（矢量 ∪ 位图）→ 裁剪 → 边缘墨迹/dpi/长宽比体检
质保：一致性约束 → 质量指标（复核清单已随复核模式移除，隐患只落进统计与质量报告）
写 IR（ir.sqlite）+ Markdown（md/full.md + blocks.json + 逐页 md）
回写 MySQL：block 定位 / formula / paper 统计
```

实测（TurboQuant, 22 页 ICLR 论文）：解析 **3.5s**，480 块、68 条公式（A 50 / B 17 / C 1，C 占比 1.5%）、5 张插图（复核项计数已随复核模式移除）。

**扫描件兜底（规划 §13.4）**

影印/扫描 PDF 里**一个字符都没有**（`page.get_text()` 返回空），行聚类、段落切分、
公式矢量层整套失效——内容只存在于像素里，所以**正文与译文都只能来自模型**：

```
页图（另渲 200dpi）→ 视觉模型 → 该页 Markdown 原文（只转录、不翻译）
                       ↓ 切块：heading / para / formula / caption / table / ref
                走既有页级翻译链路 → 中文译文（该页与整页一起出原文 + 译文，下游一行都不用改）
```

| 机制 | 实现 |
|---|---|
| 判定 | 该页文本层字符 < 40 **且**含整页图像 → 扫描页（不能用 `== 0`：影印件常带零星 OCR 残渣） |
| 读页 | `parsing/ocr/vision_reader.py` + `VisionReadFn` 钩子（服务层注入，`parsing` 不依赖 `services`）；**另渲 200dpi 页图**，150dpi 对小字号上下标偏糊 |
| 切块 | `split_page_markdown`：空行分段、`#` 成标题、`$$…$$` 成公式、`\| a \| b \|` 成表格、`- [n]` 成参考文献 |
| 文本约定 | Block 存**裸文本**（标题不带 `#`、图注不带 `**`），标记由 `md/generator` 统一加；公式包成 `$$ … $$`（下游 `formula_shapes` 只认带 `$` 的锚点） |
| 跳过 | 扫描页**不跑**公式候选与插图检测：整页图会被误判成一张巨型插图 |
| 元信息 | 第 1 页是扫描页时，标题取模型识别出的**最浅层标题**（`#` 优于 `##`）；取不到就沿用文件名兜底 |
| 离线 | 内置 `mock-vision`（**非默认**）：无密钥也能跑通"扫描页 → 块 → 翻译"整条链路 |

约定：读页只转录不翻译；看不清的符号写 `[?]`，**不允许靠语义猜**；页眉页脚与页码不转录。
实测（TurboQuant 第 3–4 页转成纯图像 PDF，2 页）：识别 **13s / 每页 ≈4.9k 字符**，
翻译 36s、2/2 页成功。

### 4.2 页级翻译（产线 A）—— 一页一次调用

**只有一种粒度**：一页一次调用。模型输入 = 该页**页图（PNG，150dpi）** + 解析器抽出的页内文本
+ 稳定前缀（文档简报 + 术语表）；输出 = 一个 JSON `{"original_md": "...", "translation_md": "..."}`，
即**两份互相独立、块划分一一对应的 Markdown**（原文是模型的忠实转录 / OCR，译文是中文翻译）。

```
取页（按页号升序，跳过已有产物的页）
① 页清单：以渲染出的页图为准（每页一个调用单元）
② 文档简报（每篇 1 次调用，可关）：标题+摘要+章节抽样 → 领域 / 统一术语 / 语体 / 注意点
   → 落盘 data/papers/<id>/md/translation_brief.json（可人工编辑）
③ 逐页调用（并发受自适应调度器控制）：
      input  = 稳定前缀（简报 + 术语表）+ 本页页图 + 解析器抽出的本页文本
      output = {original_md（忠实转录）, translation_md（逐块对应的中文译文）}
④ 两份 Markdown 按块对齐成行 → 原子写 data/papers/<id>/md/pages.json + SSE page_translated
完成 → paper.status / 进度（页）回写
```

**对齐是天然的**：左右两侧用同一套纯结构切块（空行分段、`$$` 围栏成块、列表项并块），
块数相同就按序号一一对应（`align=exact`）；只有块数不同才用 `difflib` 做一次序列对齐
（`align=structure`，块不丢）；两侧都空是 `align=empty`。**没有锚点、没有保序 DP、没有回退补译**
（旧版的 `page_aligner`、块级回退、严格校验档均已删除）。

**完全信任模型**：不做校验、不做回退补译。图注不再单独调用，公式不再特殊处理，
参考文献由模型原样保留（不翻译）；C 级公式（Markdown 里只有裁图、没有 LaTeX）也不额外处理。

**思考模式吃光 token 的兜底**：DeepSeek 等模型默认开思考模式，思维链计入 `completion_tokens`，
`max_tokens` 不设够就会出现"推理占满、正文为空"。此时按阶梯升档重试
（8192 → 16384 → 32768 → 65536），并在统计里记 `max_tokens_escalations`。

**产物 `md/pages.json`（翻译的唯一事实源）**：逐页
`{page, page_total, original_md, translation_md, rows[], align, model, prompt/completion/reasoning tokens, latency_ms, updated_at}`。
`rows` 是行对齐结果，每行
`{id: "p003-00", page, index, type(heading|para|formula|caption|list|table|code|ref), level, number, original, translation}`。

**断点续跑**：`cursor.done_pages` + `pages.json` 里已有的行；每页落盘即原子写；
取消 / 暂停在**页边界**生效。

**实测（TurboQuant 22 页 / 真实 DeepSeek 视觉模型）**：22 次调用 / 186.8 s / 成功 22 失败 0 /
**277 行全部 `exact`**；prompt 60.7k tokens、completion 143.6k（推理 97.6k）；
3 页触发过 max_tokens 升档。

### 4.3 自适应并发（实验亮点）

| 机制 | 实现 |
|---|---|
| 并发窗口 | N ∈ [1, 8]，默认 4（`PR_TRANSLATE_CONCURRENCY`） |
| 降速 | 429/超时/连接错误 → N 减半，退避 `Retry-After` 或 `base×2^k×rand(0.8,1.2)`，上限 30s |
| 恢复 | 连续 5 次成功 → N +1 |
| 熔断 | 连续失败 ≥8 → 暂停任务并写明原因 |
| 幂等 | 已有页级产物（`pages.json`）与 `cursor.done_pages` 里的页不再调用模型 |
| 观测 | 每次调用记 token/耗时/并发窗口，输出 `concurrency_timeline` |

对照实验（可复现）：`python cli/pr.py bench --mode both --n 60 --rate-limit-every 7`

### 4.4 阅读器公式列表（从 Markdown 提取，只读不复核）

阅读器展示的公式本来就来自模型（看页图生成的 LaTeX，见 §4.2），解析器的公式卡只是解析质检的
副产物，因此公式列表**直接从页级产物的 Markdown 里抽**：

| 项 | 实现 |
|---|---|
| 接口 | `GET /papers/{id}/formulas`，默认 `source=markdown` → `{items, count, source:"markdown", display_count}`；`items[] = {id, page, row_id, latex, display, context, context_zh, model}` |
| 行间 / 行内 | 行间 `$$…$$`（`display=true`）与行内 `$…$`（`display=false`）分别提取；**行内必须成对扫描**——直接正则 `\$([^$]+)\$` 会把两段公式之间的正文（`$d$-dimensional … $B$`）也当公式，实测多出成百条噪声；单字符变量这类噪声被过滤 |
| 定位 | `row_id` 是页级产物的行 id（`p008-03`），前端据此定位到正文行 |
| 实测 | TurboQuant：行间 **31** / 行内 **313** / 合计 **344**；解析阶段公式卡 68 张 |
| `source=parse` | 才返回解析阶段的公式卡（`latex_final / grade / confidence / vector_audit / validation / crop_url`），**仅供解析质检参考** |
| 已下线 | 公式人工复核整条产线：旧的 `?grade=A\|B\|C` 过滤、`accept/fix/ignore` 定稿为 B 级、阅读器侧栏的公式卡与"采纳/修正/定稿"；**复核模式整体已移除**：复核页与 `/review/:paperId` 路由、`GET /papers/{id}/review`、`PUT /review/{reviewId}` 接口、`Review` 模型与 `paper.review_open` 计数（"待复核 N"）均已删除；一致性检查与插图体检结果只进 `result.stats["consistency_findings"]` 与质量报告（`build_quality_report`） |

**插图定位（本轮修复）**：侧栏插图条目的"定位"曾拿合成 id（`fig-p008-00`）去匹配行 id
（`p008-03`），必然找不到锚点、跳到第 1 页。现在 `GET /papers/{id}/figures` 的每条带
`block_id` / `row_id`（= 该图**图注行的行 id**，由图号反查）/ `page`；`GET /reader` 的
批注/笔记/高亮条目也补了 `page`（行 id→页，回退块 id→页），行 id 失效（重译后行号变了）时
**按页定位**而不是乱跳。裁图区域按图注/标题锚定收紧（`services/paper/figure_regions.py`），
`GET /api/v1/papers/{id}/figures/region/{page}/{index}` **600dpi 按需渲染 + 落盘缓存**。

**双语 Markdown 导出**：`GET /api/v1/papers/{id}/markdown/translated` 按图号把插图行插到对应
图注上方，返回 `{paper_id, pages:[{page, page_total, original_md, translation_md, align, model}],
markdown, path, count}`，并落盘 `data/papers/<id>/md/translated.md`（图片用 `../figures/…`
相对路径，编辑器直接可用）；`?write=false` 只返回不落盘。

---

## 5. 关键设计决策

1. **不用 PyMuPDF 的文本块边界**：实测其块会把段落切碎、把展示公式拆成 `h` / `x−Q⁻¹(Q(x))` / `2`
   多个块。因此只把 span 当证据，自己按基线聚行、按行距基线切段。
2. **段落判据用 baseline→baseline 的 lead，而不是行 bbox 间距**：inline 公式的上下标会把
   行 bbox 撑高，使 gap 变成负值，无法作为判据。
3. **块 ID 带论文前缀**：`turboquantic-p07-b12`。MySQL 的 `block.id` 是全局主键，
   若用 `p07-b12` 会导致第二篇论文导入时主键冲突（已修）。
4. **Markdown 是内容事实源，MySQL 只存定位**：避免"三处存正文 → 三处不一致"。
   阅读器读 `md/blocks.json`（由 Markdown 派生），既快又与事实源同源。
5. **同一 PDF 重复导入复用现有论文**（sha256 判定），避免演示/回归测试产生 `-2/-3` 副本。
6. **公式 C 级不产出 LaTeX**：Markdown 用高倍裁图兜底 + 候选与证据写注释，
   并把 C 级占比作为质量指标暴露出来。页级翻译时整页页图与页内文本一起交给模型，
   模型返回的 LaTeX **直接采用**（允许它修好抽取坏的公式）。
7. **模型零硬编码**：业务只写 `model_type`，内置 Mock 保证完全离线可跑（规划 §1.2）。
8. **原文也由模型产出**：同一次页级调用里先忠实转录、再翻译，两份 Markdown 块序号天然一一对应，
   因此**不需要把整页译文切回 `block_id`**——旧版的 `page_aligner`（锚点 + 保序 DP）、
   严格校验档、回退补译与三层缓存**均已删除**。
9. **前缀缓存靠"字节级稳定的前缀"**：简报与术语表在整篇内不变且放在 prompt 最前面，
   严禁混入时间戳/页码/统计量，否则 provider 前缀缓存必然不命中。
10. **公式不做人工复核（复核模式已移除），展示即以 Markdown 为准**：阅读器公式列表直接从页级产物 Markdown 提取、
    只读展示（见 §4.4），解析公式卡降级为解析质检参考（`?source=parse`）。人工闸门是空转的
    ——页级调用本就让模型重新给出整页 LaTeX，复核一条抽取出来的旧公式没有意义。

---

## 6. 常用命令

```powershell
python cli/pr.py init                      # 建库建表 + 内置模型
python cli/pr.py build <pdf> --id <pid>     # 导入 + 解析
python cli/pr.py structure <pid>            # 打印结构（人工核对阅读顺序）
python cli/pr.py qa <pid>                   # 质保报告（A/B/C 分级、插图体检）
python cli/pr.py translate <pid> [--mode resume|retranslate] [--pages 3,4] [--concurrency N] [--no-brief]
python cli/pr.py index <pid> [--rebuild]    # chunk + embedding + LanceDB
python cli/pr.py reconcile [pid]            # 三存储对账（只报告）
python cli/pr.py bench --mode both          # 自适应 vs 固定并发对照实验
python cli/pr.py serve --port 8710
python -m pytest tests -q                   # 单元 + 端到端
python scripts/e2e_check.py                 # 35 项验收自检
python scripts/frontend_check.py --block-markers   # 41 项前端验收自检（真实浏览器渲染）

# ---- 扫描件（影印 PDF）----
python scripts/make_scanned_pdf.py --src 论文.pdf --out scanned.pdf --pages 3-4 --gray
python cli/pr.py build scanned.pdf --id scan-demo     # 自动走视觉模型读页
python cli/pr.py translate scan-demo

# ---- 人工检查译文 ----
# 译文对照稿 = 带插图的逐页双语 Markdown：
#   GET /api/v1/papers/<id>/markdown/translated?write=true
#   → 落盘 data/papers/<id>/md/translated.md
# （旧脚本 scripts/export_review_md.py 已随复核模式删除）
```

---

## 7. 验收状态（`scripts/e2e_check.py` 实测 35/35）

| 规划验收项 | 状态 | 证据 |
|---|---|---|
| A1 导入 → 解析 → 可读全链路 | ✅ | `build` 3.5s / 480 块 / Markdown 152KB |
| A2 IR 可追溯（页码 + bbox + 原文） | ✅ | `/reader` 每块含 `page/bbox/text`；公式含裁图 |
| A4 逐页流式（页级可见 + 断点） | ✅ | cursor 逐页落库；SSE `page_translated`；`rows` 行对齐 |
| A5 自适应并发（含对比数据） | ✅ | `bench` 双模式对比；并发时间线可导出 |
| A7 论文库字段（含进度） | ✅ | `/papers` 列表（翻译进度）；**待复核计数已随复核模式移除**（不再有 `review_open`） |
| A8 进度持久化（含视图模式） | ✅ | `PUT/GET /progress` 往返一致 |
| A9 Chunk + 索引（可重建） | ✅ | 129 chunk / 4 类型 / LanceDB 重建稳定 |
| A12 笔记/高亮/批注增删改查 | ✅ | API + `/notes/overview` |
| A13 模型管理（零硬编码、可测试） | ✅ | `/models/types` 三类型默认齐全 |
| 阶段 03 重跑 ID 稳定 | ✅ | pytest `test_reparse_keeps_block_ids` |
| 阶段 08 注入 429 / 持续失败 | ✅ | pytest `test_scheduler.py` 11 项 |
| 三存储对账 | ✅ | `reconcile` issues=0 |

**未覆盖（下一轮）**：A6 版面保留 PDF、A10 基础 RAG、A11 划词解释来源校验（当前为单块上下文，尚未接检索）。

---

## 8. 模型接入要点（DeepSeek 实测）

实测环境：`deepseek-flash`（V4.1 Flash），2026-09。**踩过的坑都写在这里，换模型时对照检查**。

| 项 | 事实 | 本系统的处理 |
|---|---|---|
| 模型名 | 平台只接受 `deepseek-flash` / `deepseek-v4-pro`；`deepseek-chat`/`deepseek-v4-flash`/`deepseek-reasoner` 都路由到 `deepseek-flash` | 预设写 `deepseek-flash` |
| **思考模式** | **默认开启**（effort=high），思维链计入 `completion_tokens` | 预设默认 `thinking=enabled`；可在 `/models` 切到 `deepseek-fast`（关闭思考） |
| `max_tokens` | **不设够会导致正文为空**（finish=length）。实测一整页译文需 4000–8500 completion tokens，其中推理占 92% | 页级调用按阶梯 8192/16384/32768/65536，命中空正文自动升档重试 |
| `temperature` | 思考模式下**完全无效** | provider 主动剥掉无效参数，避免"设了以为在生效" |
| `top_p` | 仅思考模式生效，下限 0.95；非思考模式恒为 1.0 | 预设 `top_p=0.95`，按模式自动规范化 |
| 前缀缓存 | 返回 `prompt_cache_hit_tokens` / `prompt_cache_miss_tokens` | 简报表 + 缓存的稳定前缀 + 命中统计 |
| 多模态 | `deepseek-v4-flash-vision-exp`，单图 ≤384 tokens，支持 base64 / URL / Files API | 已注册为 `vision` 类型模型（实测能准确读出图中文字与数值） |

成本参考（真实模型，第 3 页试译）：**41.7s / 输入 1633 token / 输出 6956 token
（推理 6215，占 89%）/ 完成 19 行**（该页含 3 个公式）。
整篇 22 页实测 **186.8 s / 22 次调用 / 成功 22 / 277 行全部 `exact` / 3 页触发过 max_tokens 升档**，
合计 prompt 60.7k、completion 143.6k（推理 97.6k）token。公式随整页一起翻，
`模型改过公式 0 条`（模型按提示原样保留了 LaTeX）。
如果只关心成本与速度，把生成模型切到 `deepseek-fast` 预设（关闭思考）可显著下降。

探测脚本：`python scripts/probe_deepseek.py --key sk-xxx --image 任意图片.png`
（会依次测模型名、纯文本、KV 缓存、图像理解、结构化 JSON）。

---

## 9. 已知限制

- **未接入真实 LLM 时**，翻译/解释走 Mock 占位（链路真实、内容为占位），`/models` 填密钥后自动切换。
- **公式回渲染校验**（规划 §14.1 ⑥ 的"回渲染比对"）需要在服务端跑 KaTeX/MathJax 渲染，
  当前记为 `rerender: skipped` 并**不会因此判 A**（A 级只在无结构性符号时给出）。
- **公式候选质量**受文本层质量限制：带根号/分式的复杂公式仍会落到 B/C 级——
  这正是设计预期（C 级用裁图兜底）。**公式人工复核（复核模式）已移除**（见 §4.4），阅读器展示的公式
  一律以页级产物 Markdown 里的 LaTeX 为准，解析公式卡只在 `?source=parse` 时供质检参考。
- **向量检索/RAG 未实现**（阶段 14–17）：`/rag/query` 返回 `implemented: false`；
  但 chunk、embedding、LanceDB 入库与删除一致性已打通。
- **单用户免登录**（`user_id=1`），无鉴权；仅监听本机。
- **扫描件的正文质量 = 视觉模型质量**：识别错的字会被翻译放大，且没有文本层可对照。
  缓解手段：读页提示要求"看不清写 `[?]`、不许猜"，并且扫描页不产出公式卡
  （公式以 `$$…$$` 留在正文里，无法回渲染校验）。
- **扫描件没有 bbox**：块的坐标是等分整页的虚拟横带（只为稳定阅读顺序），
  页图上叠加块框的可点选定位在扫描页不可用（页级对照仍是整页页图 + 该页行）。
