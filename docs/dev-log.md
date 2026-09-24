# 开发日志 · PaperMind 后端（解析与阅读主干）

> 按规划 §24「每阶段固定动作」记录：最小可测代码 → 人工验证 → 稳定性检查 → 状态。
> 环境：conda `scenemind`（Python 3.13 / torch 2.12+cu132 / PyMuPDF 1.28），业务库 MySQL 8.4（root@127.0.0.1）。

---

## 阶段对照

| # | 阶段 | 状态 | 交付与验证 |
|---|---|---|---|
| 01 | `project-init` | ✅ | `backend/main.py` 可起、`/health` 返回库/任务池/数据目录；`pr.py init` 建 13 表 |
| 02 | `pdf-parser` | ✅ | `parsing/extract`：span 级文本层 + 矢量层 + 位图层 + 页面渲染；`reports/raw_text_blocks.json` 落盘可核对 |
| 03 | `ir-structure` | ✅ | 自研行聚类 + 双栏顺序 + 段落切分 + 页眉页脚剔除；`rebind` 保证重跑 ID 稳定（pytest 覆盖率 ≥90%） |
| 04 | `markdown-export` | ✅ | `md/full.md`（块标记+锚点）+ `blocks.json` + 逐页 md；每块都有锚点（pytest 断言 ≥95%） |
| 05 | `mysql-schema` | ✅ | 13 张表（规划 12 张 + `setting`）；`init_db` 可重复执行 |
| 06 | `model-provider` | ✅ | Provider 抽象 5 类 + ModelManager（默认路由/测试/维度校验）+ Mock 离线兜底 + 3 个内置模型 |
| 07 | `translation-stream` | ✅ | 逐块落库 + 落 Markdown + SSE；断点 `cursor_json`；刷新/切换不丢进度 |
| 08 | `adaptive-concurrency` | ✅ | 自适应调度器 + `bench` 双模式对照；11 项调度器测试（429/Retry-After/熔断/恢复/幂等/停止） |
| 09 | `reader-views`（数据面） | 🟡 | `/reader` 提供 `view_mode / pair_order / heading_level / bbox` 与增量接口；**三视图前端未实现** |
| 10 | `annotation-highlight` | ✅ | 高亮/笔记/批注 CRUD + 划词解释 + 存为批注 + 跨论文聚合 |
| 11–17 | `chunk → rag` | 🟡 | chunk（4 类）+ embedding + LanceDB 入库/删除/重建已完成；**检索与 RAG 问答未实现（接口占位）** |
| 18–20 | `pdf-translate`（产线 B） | ⬜ | 任务类型已注册，handler 明确返回"未实现"（不留静默失败） |
| 21–22 | `formula-repair / review` | 🟡 | 候选+审计+三重校验+分级+复核闭环已完成；LLM 修复走证据契约（真实模型未接入，Mock 保守返回原候选并标 uncertain） |
| 23 | `ai-reading-p1` | ⬜ | 占位 |
| 24 | `integration` | 🟡 | `scripts/e2e_check.py` 32/32 通过；`reconcile` issues=0 |
| — | `page-translation`（第二轮新增） | ✅ | 页级批量 + 文档简报 + 三层缓存 + 整页 Markdown 切回块 + 宽松校验/有上限回退；`scripts/compare_granularity.py` 出对照数据 |
| — | `scanned-pdf`（第三轮新增） | ✅ | 无文本层页面交视觉模型整页识别 → Markdown → 切块 → 走既有翻译链路；`scripts/make_scanned_pdf.py` 造测试件，实测 2 页 24 块全译、回退 0 |

---

## 关键问题与修复记录（真实踩坑）

### 1. PyMuPDF 的文本块边界不可用

**现象**：段落被切成十几个窄块（`L=1` 居多）；展示公式被拆成 `h` / `x −Q⁻¹ (Q(x))` / `i` / `2` 独立块；
`mse(Q(x)) 4b for any b ≥0.` 被误判成公式块。

**修复**：`extract/text_layer.py` 改为**只用 span 作证据**，自研：
- `cluster_spans_into_lines`：按基线（容忍 2.2pt 或 22% 字号）+ 字号聚类，再按水平间隙切分同行左右栏；
- `group_lines_into_blocks`：按纵向连续性成块（块只是公式/插图的几何容器）。

**结果**：段落数从 448（含大量碎块）收敛到 132 个真实段落。

### 2. 段落判据不能用行 bbox 间距

**现象**：`gap` 出现 **负值**（如 -6.39），因为 math span 的 bbox 比正文行高，`y1` 被撑到下一行内部。

**修复**：改用 **baseline→baseline 的 lead**，并先用「正文字号 + 单倍行距样本」统计中位 lead；
段落阈值 = `clamp(median_lead × 1.45, 1.05×字号, 3×字号)`。图内文字/页码/公式编号另用白名单剔除。

### 3. inline 数学字体导致段落被切碎

**现象**：`_same_style` 比较"字体集合"，而同一段落里公式 span 用 CMMI、正文用 NimbusRom → 每行都判为样式变化。

**修复**：比较**主导字体**而非集合；并把"数学行 → 正文行"的切分收紧为「短数学行 + 下一行长且行距偏大」。

### 4. `block_id` 跨论文主键冲突（设计缺陷）

**现象**：导入第二篇论文时报 `Duplicate entry 'p01-b01' for key 'block.PRIMARY'`。

**根因**：块 ID 只含页码与页内序号，而 MySQL `block.id` 是全局主键（规划 §10 ②）。

**修复**：ID 带论文前缀 —— `turboquantic-p07-b12`（`rebind.id_prefix`，≤12 字符）。
同时 `rebind_block_ids` 先把新块改成临时 ID，避免"新块占用旧块 ID"。

### 5. 公式候选落在同一宿主块 → 主键冲突

**修复**：`_dedupe_formulas` 合并同一 `block_id` 的公式（bbox 取并集、候选合并、分级取更保守一侧）；
落在非正文行上的候选补"孤儿块"（`...-p07-x03`）保证外键一致且阅读器可见。

### 6. 任务终态进度回退（`done_items` 归零）

**现象**：暂停后 `done_items` 显示 0 / 33，而 cursor 已有 300+。

**根因**：进度写库有限流（0.5s），终态 `finish()` 之前可能被更早的快照覆盖；
且 `progress()` 未记住已知完成量。

**修复**：`TaskContext` 记录 `done_items/total_items` 单调不减；`finish()` 支持
`done_items/total_items` 参数强制校准终态；翻译收尾额外 `force=True` 落一次进度。

### 7. Windows `os.replace` 偶发 `WinError 5`

**修复**：`write_text_atomic / write_bytes_atomic` 加 4 次退避重试 + 直接覆盖兜底。

### 8. Mock provider 场景判别错误

**现象**：翻译返回"模型未返回 translation_md 字段"。

**根因**：翻译 prompt 结尾含 `translation_md` 字样，公式修复 prompt 的 schema 含 `repaired_latex`，
用子串判断会串台；且 `OUTPUT_SCHEMA` 当时是扁平映射而非标准 JSON Schema。

**修复**：`OUTPUT_SCHEMA` 改为标准 JSON Schema；Mock 用「带引号的 JSON 键」精确判别，
并兼容扁平写法；schema 为空时返回显式结构而不是空 dict。

### 9. FastAPI / Starlette 版本不兼容

**现象**：`Router.__init__() got an unexpected keyword argument 'on_startup'`（Starlette 1.6 移除该参数）。

**修复**：`pip install -U "fastapi>=0.118"` → fastapi 0.141.1 + starlette 1.6.0 正常。

### 10. 翻译粒度：从"逐块"改为"混合粒度"（第二轮）

**讨论**：逐块调用虽然保证"单块 ≤2s 可见"，但每块只看得到前后各一段上下文，
术语一致性弱、调用次数多（498 块 = 498 次调用）。用户提出按页喂给模型更合适。

**结论与实现**：
- 正文（`para`/`heading`）**按页一次调用**，输出整页 Markdown；
- 图注按块（图号必须严格对应）、公式**直接透传**（禁止模型改写 LaTeX）；
- 新增**文档简报**（每篇 1 次调用）固化全文脉络与统一术语，作为**字节级稳定的前缀**；
- 新增 `page_aligner` 把整页 Markdown 切回 `block_id`（公式/标题/图注锚点 + 保序 DP）；
- 三层缓存：前缀缓存 / 批结果缓存（落盘）/ 块级缓存。

**实测（120 块，mock）**：批次数 120 → 27（降 4.4×），输入 token 8691，
回退补译 0 块，术语一致率 90–100%。`block` 粒度保留，用于 A1 验收与对照实验。

### 11. 校验过严会拉低成功率（重要教训）

**现象**：第一版页级校验把"长度比越界"和"置信度偏低"都当硬问题 → 触发块级重译，
块级补译本身也可能不达标 → `fallback` 反复堆叠，成功率被拉低、额度成倍消耗。

**修复（区分硬问题与软提示）**：

| 判据 | 处理 | 理由 |
|---|---|---|
| 空译文 | **硬问题** → 重译 | 确实没译出来 |
| 公式被改写 | **不处理**（只统计 `formula_changed`） | 见第 15 条：抽取稿本身可能是坏的，模型修得更可渲染 |
| 长度比偏离（<6% 或 >300%） | **软提示** → 标记复核 | 中英字符比波动大，误报率高 |
| 页级置信度 < 0.20 / 译文短到像截断 | **软提示** → 标记复核 | 重译收益低、成本高 |
| 相邻段共享译文（模型合并段落） | **硬问题** → 重译 | 见第 14 条：这是内容错位，不是"已翻译" |

另加**回退总量上限**（默认 `fallback_ratio=0.25`），防止个别难页拖垮整篇。
需要严格档做对照实验时传 `strict_validation=true`（该模式下长度比越界也算硬问题）。

### 12. DeepSeek 思考模式实测（默认开启，必须正确处理）

用真实 key 实测 `deepseek-flash`（V4.1 Flash）得到的事实：

| 配置 | 延迟 | content | reasoning | completion | finish |
|---|---|---|---|---|---|
| 默认（思考开启，effort=high） | 1860ms | **0 字符** | 355 字符 | 200（=max_tokens） | `length` |
| `thinking=disabled` | 860ms | 64 字符 | 0 | 35 | `stop` |
| thinking 开启 + `json_object` | 1031ms | 54 字符 | 300 字符 | 121 | `stop` |

1. **平台只接受 `deepseek-flash` / `deepseek-v4-pro`**；`deepseek-chat`、
   `deepseek-v4-flash`、`deepseek-reasoner` 都能调通但 model 字段都返回 `deepseek-flash`；
   `deepseek-v4.1-flash` 直接 400。
2. **思考模式默认开启**且 effort=high；**思维链计入 `completion_tokens`**。
3. **思考模式下 `temperature` 完全无效**；`top_p` 生效且下限 0.95。

由此修掉的四个真实缺陷：

| # | 缺陷 | 修复 |
|---|---|---|
| 1 | 连通性测试用 `max_tokens=4`，思考模式下**永远假失败** | `test()` 改用 2048 tokens + 极短问题 |
| 2 | 页级翻译 `max_tokens` 默认回落到 4096，**不够写一整页**（推理占 92%），偶发整批失败 | 阶梯默认值提到 8192/16384/32768，并自动升档重试 |
| 3 | 思考模式下设 `temperature=0.2` 是**无效配置**，容易造成误解 | provider 主动剥掉无效参数并在文档中标注 |
| 4 | 正文为空时被当成"模型返回空"往上层传，会写出空译文段 | provider 显式抛错（含 reasoning token 数与处理建议） |

**成本实测**（真实模型，第 3 页 17 块，页级粒度）：38.5s、输入 1527 token、
输出 8545 token 其中**推理占 7827（92%）**、回退补译 5 块。
推算整篇 22 页约 14 分钟、19 万 completion token——**思考模式主要在花钱买"推理"，
而翻译并不需要推理**，这是当前配置最大的成本项（要不要开由使用者在 `/models` 切换预设决定）。

### 13. 页级翻译的第二个真实缺陷：模型合并段落导致译文错位

**现象**：`p03-b19` 与 `p03-b20` 两块的译文**完全相同**（模型把两句合译成一段），
p03-b19 实际上没被翻译。

**根因**：保序 DP 在"模型输出段数少于源块数"时，会把同一段译文分给相邻两块
（这是为了让"短段被合并"时不至于整块判 missing）。但**除首块外其余块的内容是错的**。

**修复**：对齐器记录 `shared_with`（哪些块共享了同一段译文），
`needs_retranslate` 把这些**尾块**与真正 missing 的块一起交给块级补译；
且这些块**不产出首次译文**（避免把错位内容先写进 Markdown 事实源）。

**连带揪出的实现 bug**：`accepted` 集合只排除了 `report.problems`，
没排除共享尾块，导致 `if job is not None and bid not in accepted` 恒为假 →
**回退队列从未被填充**（`fallback_blocks` 永远是 0），
修复前表现为"检测对了但没人处理"。

同轮还修了：`nonlocal max_tokens_current` 缺失导致页批次整体失败（靠回退兜住）、
对齐片段带回 Markdown 标题标记导致 `## ### 0.3` 双层标记。

### 14. 测试污染（重要教训）

**现象**：反复出现"论文不存在：turboquant-iclr26"，演示论文每隔几轮就被删掉。

> 留档说明：该论文 2026-09 按标题重建后 id 变为 `turboquant-online-vector-iclr-26`，
> 下面这条教训本身与 id 无关（同 sha256 被复用 → 被测试删掉）。

**根因**：`test_import_reuses_existing_paper` 用**真实示例 PDF** 导入，
而 `import_pdf` 对同 sha256 会**复用**已有论文 —— 于是它复用了演示论文，
再在清理时把它硬删了。测试污染了开发数据。

**修复**：测试改用"示例 PDF + 追加标记字节"的独立副本（sha256 不同），
并加 `_safe_delete()` 只删 `it-` / `e2e-` 前缀的自建论文。

### 15. 校验再放松 + 公式不再特殊处理（模型可信任）

**背景（两次收敛）**

1. 第一版把"公式与原文不一致"当硬问题（规划 §14.5：读者看到的公式必须永远正确），
   公式甚至**完全不交给模型**（透传）。但实测发现**抽取出的 LaTeX 本身就可能是坏的**
   （B 级公式的括号/命令/上下标常有问题），锁死等于"忠实地保留错误"。
2. 第二轮给公式加了一整套机器：单独的公式批次 + 专用提示 + `formula_fix` 开关 +
   `sanitize_formula_text` 守门人（中文说明/无 LaTeX/长度失控三种底线）。
   **用户否掉了这套复杂度**：模型足够稳定，整页翻译时公式就在上下文里，
   直接用模型译文里的公式即可。

**最终实现（一句话）：公式在原位跟整页一起翻，模型返回的 LaTeX 直接采用。**

| 层 | 改动 |
|---|---|
| 分批 | `PAGE_KINDS` 加入 `formula`；取消 `passthrough` 批次与 `formula_mode` 开关 |
| 对齐 | `similarity()` 增加"公式块 ↔ 公式段"分支：按 LaTeX 相似度给分、**下限 0.6**，模型改过公式也能按顺序落回同一个 `block_id`；`pending_segs` 纳入公式段 |
| 切块 | C 级公式（Markdown 里只有裁图、没有 LaTeX）在 `collect_jobs` 直接跳过（计入 `skip.formula`）——没有可译内容，塞进 prompt 只白烧 token |
| 校验 | 硬问题**只剩"空译文"**；公式与原文不一致**连告警都不发**，只统计 `formula_changed`（模型动过多少条公式）作为实验指标 |
| 对齐参数 | `min_confidence` 0.20→0.15、`merge_confidence` 0.12→0.10、`max_extra_ratio` 2.5→3.0、`SUSPICIOUS_LEN_RATIO` 0.15→0.08 |

**为什么公式可以放心交给模型**：公式块在页内是**有序**的，保序 DP + 公式专属相似度
（下限 0.6）保证它只会落到同一页后面的公式段上，不会串块；
而"忠实保留一个渲染不出来的坏公式"对读者毫无价值。
`block` 粒度 / 回退补译仍保留一个极短的公式提示（`FORMULA_USER_TEMPLATE`），
免得逐块模式下模型把公式当正文去"翻译"。

**任务统计**：`formula_changed`（本页模型改动过 LaTeX 的公式条数）。
`strict_validation=true` 时长度比越界重新算硬问题（对照实验用）。

**验证**（真实模型，第 3 页 20 块含 3 个公式）：41.7s / 输入 1633 token /
输出 6956 token（推理 6215）/ 完成 19 / 失败 0 / 模型改过公式 0 条。
两条公式在模型原始输出里位于原位，对齐器按顺序精确落回 `p03-b02` / `p03-b14`。

### 16. 用真实模型输出反查对齐，揪出两个连锁缺陷

**做法**：把缓存里的**模型原始页级输出**拿出来，离线跑一遍 `align_page` 并打印
每个块的 anchor/置信度/译文。这比"看最终 Markdown"有效得多——最终的
`_render_heading` 会掩盖标记污染，而 `shared_with` 会把错配伪装成"模型合并段落"。

**缺陷 1：`looks_like_heading` 把中文句子判成标题 → 整页连锁错位**

判据里用 `t.isupper()` 判"全大写标题"，而 `str.isupper()` 只看**有大小写的字符**：
"上述期望是针对量化器 Q(·) 的随机性取的。……我们要求：" 里唯一的拉丁字母是大写 `Q`
→ `isupper()` 返回 `True` → 这句话被当成标题进入 `heading_segs`，
于是标题锚点把它配对给了 `p03-b10`（真正的 "0.2 PRIOR WORK"），
`p03-b10` 拿到了正文、`p03-b12` 的标题被挤到保序 DP，
连带 `p03-b01/b03/b04` 三个块全部堆到同一段译文上（假的 `shared_with` 三联）。

修复：加"**含中文且不带编号 → 不是标题**"的门槛（编号标题是最可靠判据，中英通用）。
修复后 `p03-b10 → "0.2 先前工作"`、`p03-b12 → "0.3 技术与贡献概述"` 都锚定正确，
假 `shared_with` 从 4 组降到 3 组（剩下的是模型真的把两段合并了）。

**缺陷 2：标题标记叠成两层，`###` 漏进译文**

标题块的 `text` 来自 Markdown 事实源、**本身就带 `###`**，而 `build_page_body`
又按层级加了一次 → 喂给模型的是 `### ### 0.2PRIOR WORK`。模型老实回显
`## ### 0.2 先前工作`，而 `_HEADING_PREFIX` 只剥一层 → 译文变成 `### 0.2 先前工作`。

修复：`build_page_body` 先剥掉原有 `#` 再按层级重加；`_HEADING_PREFIX` 改成
`^[\s#]+` 一次剥干净。两个修复都补了回归测试
（`test_looks_like_heading_rejects_chinese_sentence_with_capital_letter`、
`test_page_body_strips_existing_heading_markers`、`test_heading_anchor_not_stolen_by_sentence`）。

**顺带补的测试盲区**：`pytest` 曾经全绿，但
`backend.services.translation.translation_service` 有一处缩进错误——
因为没有任何测试 import 过它（翻译任务只在脚本和服务里跑）。
新增 `tests/test_imports.py`：把 `backend` 包下所有模块导入一遍，几十毫秒换掉一整类
"跑起来才发现"。

### 17. 扫描件（影印 PDF）兼容：正文与译文都交给模型

**问题**：影印 PDF 的 `page.get_text()` 返回空串，没有 span、没有字号、没有矢量层——
现有的行聚类 / 段落切分 / 公式恢复**整套失效**。内容只存在于像素里。

**结论（用户拍板）**：**正文和译文都依靠模型**。于是走"两步"而不是"一步"：

```
页图（另渲 200dpi）→ 视觉模型 → 该页 Markdown 原文（只转录、不翻译）
                       ↓ 切块（heading/para/formula/caption/table/ref）
                既有页级翻译链路 → 中文译文
```

为什么不做"一次调用直接出中文"：那样就没有原文事实源，三视图的对照阅读
（本系统的核心）直接失效，翻译质量也无从校验。分两步后**下游一行都不用改**——
扫描页产出的块与文本层论文完全同构，翻译 / 阅读器 / 对账全部复用。

| 要点 | 做法 |
|---|---|
| 判定 | 文本层字符 < 40（**不能用 `== 0`**：影印件常带零星 OCR 残渣）且该页含整页图像 |
| 分层 | `parsing/ocr/vision_reader.py` 纯函数 + `VisionReadFn` 钩子；钩子在 `services/paper/parse_hooks.py`（要 `model_manager`），`parsing` 不反向依赖 `services` |
| 读页 dpi | **200**（阅读器页图是 150）：小字号上下标在 150dpi 下会糊，OCR 丢符号 |
| 跳过 | 扫描页**不跑**公式候选与插图检测：整页图会被判成一张巨型插图，并且没有矢量层可审计 |
| 元信息 | 第 1 页是扫描页时标题取模型识别出的**最浅层标题** |
| 离线 | 内置 `mock-vision`（**非默认**，否则真视觉模型永远选不上）：无密钥也能跑通整条链路 |

**实测（把 TurboQuant 第 3–4 页渲染成纯图像 PDF）**：识别 13s（每页 ≈4.9k 字符，
`1354+1303` / `1354+2043` token），翻译 36s，**24/24 成功、回退 0**。
模型识别质量很高：`\mathbb{E}_Q\left[\left\|x - Q^{-1}\left(Q(x)\right)\right\|_2^2\right] \tag{1}`
这种带上下标与范数的公式一次读对。

### 18. 这一轮又揪出三个真实缺陷

**缺陷 1：扫描出的公式块没包 `$$`，3 个公式块全部回退**

`split_page_markdown` 抽出的公式文本是**裸 LaTeX**（这是对的，与 `page_aligner`
的 `Segment.text` 一致），但我直接把裸文本塞进了 `Block.text`。而下游约定的
`Block.text` 是 **Markdown 形态**（`$$ … $$`），`formula_shapes` 也只认带 `$` 的文本
→ 公式块在对齐里拿不到任何证据分 → 每块多花一次块级调用（实测 `fallback_blocks: 3`）。
修复：`blocks_from_page_markdown` 统一包成 `$$ … $$`，回退降到 **0**。

**缺陷 2：Block 里存了 Markdown 标记，生成事实源时又叠一层**

`md/generator.block_body_text` 的契约是"`Block.text_clean` 存裸文本，
标题标记与图注加粗由生成器统一加"。我在 OCR 侧存了 `## ## 0.2 PRIOR WORK`
和带 `**` 的图注 → 事实源里变成 `## ## 0.2 PRIOR WORK`、`****Figure 1**: …**`，
阅读器与翻译都跟着错。修复：`_make_block` 存裸文本（标题剥 `#`、图注剥 `**`，
层级记在 `meta["heading_level"]`），并补回归测试。

**缺陷 3：`translation_stats` 的 `done` 统计的是行数不是块数**

重译会为同一块新建一行（`revision` 递增），于是重译过的论文报出
**`done=48 / total=24`**，进度超过 100%（前端进度条与断点续跑都据此判断）。
修复：`count(distinct Translation.block_id)`。

**顺带**：`POST /papers/{id}/parse` 的 `llm_repair` 参数一直是"声明了没往下传"，
和新的 `vision` / `vision_model` 一起接进任务体（`create_parse_task(params=…)`）。

---

## 实测数据（TurboQuant, ICLR 2026, 22 页）

| 指标 | 值 |
|---|---|
| 解析总耗时 | 3.5 s（含 150dpi 渲染 22 页 + 全量公式裁图） |
| 阶段耗时 | render 1.01s / extract 0.22s / structure 0.33s / formula 1.06s / figure 0.76s / persist 0.10s |
| 块 | 480（段落 132 / 标题 38 / 公式 100 / 图注 8 / 插图 5 / 表格 1 / 参考文献 214） |
| 公式 | 68 条唯一公式：A 50（73.5%）/ B 17（25.0%）/ C 1（1.5%） |
| 插图 | 5 张，体检告警 2 条（长宽比/边缘墨迹） |
| 一致性约束命中 | 3 条高严重度（如符号 `b` 出现 1 与 2 两个取值） |
| 复核清单 | 4 条 |
| 翻译（Mock，40 块） | 0.7 s，成功 40 / 失败 0；并发窗口 4→8 |
| 翻译（真实模型，第 3 页 20 块含 3 公式） | 41.7 s，完成 19 / 失败 0；输入 1633 / 输出 6956 token（推理 6215）；回退补译 5 块 |
| 扫描件（真实模型，第 3–4 页转纯图像 PDF） | 识别 13 s（2 页 ≈9.8k 字符）/ 翻译 36 s；24 块全部成功、回退 0；输入 5814 / 输出 13965 token（推理 11498） |
| 索引 | 127 chunk（text 54 / formula 68 / figure 4 / table 1），向量 127 条，512 维 |
| Markdown | 257 KB（含 451 块译文） |
| 对账 | issues = 0 |

---

## 下一轮建议（按优先级）

1. **阶段 14–17（检索与 RAG）**：`LexicalRetriever`（LanceDB FTS 或 SQLite FTS5 影子索引）、
   RRF 融合、Reranker 降级链、`ContextBuilder`、`AnswerValidator`（来源 100% 可校验）。
2. **阶段 09 前端**：React 三视图 + `LiveUpdater`（SSE 局部刷新）+ `SyncController`（PDF↔译文联动）。
3. **阶段 18–20（产线 B）**：独立队列 + L1 原位排版（CJK 字体嵌入、溢出处理）+ 双 PDF 对比。
4. **回渲染校验**：接入 KaTeX 渲染 → 与原裁图做相似度比对，把 A 级判据补齐（当前 `skipped`）。
5. **公式质量继续投入**：当前 C 占比 1.5% 已优于 15% 目标，但 B 级中仍有"候选与文本层不一致"的情况，
   可引入 VLM 复核（P2）。

---

## 2026-09-22 第四轮：补齐前端（Vite + Vue 3 + TS），修 4 处后端适配缺陷

### 背景与决策

上一轮锚定的方案是"零构建前端"（Vue 3 ESM 浏览器版 + `template` 字符串 + 手写 `app.css`，
依赖用 `scripts/vendor_frontend.mjs` 手工 vendor）。它能跑通并被自检覆盖，但代价明显：
样式只能全局、没有类型、没有 HMR、每加一个库都要手工 vendor。
**本轮按"要有构建"重做**：Vite 6 + Vue 3.5（`<script setup>` + TS）+ vue-router（history）。
规划 §22 原写 React + TS + Vite，经确认"不限制前端开发框架"，选 Vue 的直接理由是
上一轮的 8 个视图逻辑已实测跑通，可以整体迁移（保住行为，不重写业务）。

技术验证事实：
- `registry.npmjs.org` 在本机不通（超时），`registry.npmmirror.com` 通（npm 已配置该镜像）；
- `npm install` 一次装好 56 个包（vue / vue-router / marked / marked-katex-extension / katex /
  vite / @vitejs/plugin-vue / typescript / vue-tsc / @types/node）；
- 构建 1.7 s，产物按路由分割：每个视图独立 chunk，vue 与 markdown（marked+KaTeX）各自分包。

### 关键坑（都已实测确认，不只是猜测）

1. **runtime 模板里属性中的 `&&` 会被当 HTML 实体**（零构建方案下的杀手级坑）。
   用探针页实测：`v-if="a && b"`（a=b=true）**元素不渲染**且不报错；`v-if="a &amp;&amp; b"` 正常；
   `v-if="a || b"` 正常（`|` 不是实体字符）；`v-if="and(a,b)"` 正常。
   迁移到 SFC 后由 `@vue/compiler-sfc` 编译，不再有此问题——但**"编译不报错却白屏"**这类问题
   只能靠真实渲染断言兜住，这正是 `scripts/frontend_check.py` 存在的理由。
2. **`data-*` 与 `vueCompilerOptions.strictTemplates` 冲突**：阅读器用 `data-block-id` /
   `data-page` / `data-side` 做滚动同步与划词归属，开 strictTemplates 后 vue-tsc 报这些属性
   不存在（原生元素属性类型无索引签名）；而给 `@vue/runtime-dom` 打补丁会**连带打坏 `vue` 的类型
   导出**（`Module 'vue' has no exported member 'computed'`）。结论：typecheck 用默认档，
   模板正确性交给浏览器渲染自检。
3. **markdown 必须先接管公式再交给 marked**：扫描页块是模型返回的 Markdown（含 `$…$`/`$$…$$`），
   若先 marked 再补渲染，`\{0,1\}^{b \cdot d}` 的反斜杠会被 marked 的转义规则吃掉、`_` 会被当强调。
   方案：`marked-katex-extension`（`nonStandard: true` 支持单 `$`）在 tokenizer 阶段接管，
   并把 marked 的 `html` renderer 覆写为转义（论文正文里的 `<` 不能变成注入）。
4. **SSE 让无头浏览器永远"加载不完"**：`--virtual-time-budget` 在有挂起网络请求时不推进虚拟时间，
   而 `/translations/stream`、`/tasks/stream` 是长连接 → `--dump-dom` 拿不到 DOM。
   解法：前端支持 `?nosse=1` 强制走"SSE 断开 → 3s 轮询"降级路径；这既是禁用长连接环境的运行期开关，
   也让自检可跑。`--timeout=8000` 试过，它会在渲染完成前就 dump（733 ms、1.5 KB），不可用。

### 修掉的后端真实缺陷（都是前端一接就暴露的）

| # | 缺陷 | 现象 | 修法 |
|---|---|---|---|
| 1 | 公式裁图 URL `/papers/<id>/assets/formula_crops/...` 与实际目录不符 | 公式卡裁图 404 | 改为 `/papers/<id>/formula_crops/<name>`（插图同理） |
| 2 | `GET /models/presets` 被 `GET /models/{model_id}` 抢先匹配 | 预设列表返回"模型不存在：presets"，前端"一键导入预设"点不动 | 把 `presets` / `import-presets` / `secret` / `secrets/list` 声明移到 `/models/{model_id}` **之前** |
| 3 | 无前缀的 `GET /papers/{id}/pages/{page_no}` 是 int 路由 | 静态页图 `page-0003.png` 被它挡成 422 | 前端页图改走 `/api/v1/papers/{id}/pages/{n}`（API 路由优先于静态目录） |
| 4 | 前端托管缺失 | 只能手开 file:// | `/app/` 托管 `dist/`：文件优先、否则回退 `index.html`；带扩展名却缺失 → 404（避免把 HTML 当 JS 解析）；未构建 → 503 构建指引页 |

### 交付与验收

- 前端：8 个页面 + 三视图阅读器（SyncController / LiveUpdater / 划词 / 公式复核 / 简报术语编辑 /
  窄屏退化 / 快捷键 / 深链 `?block=`）。
- 自检：`scripts/frontend_check.py` → **28/28**（静态 4 + 接口 12 + 打包产物 3 + 8 路由真实渲染 9，
  含"公式是否渲染成 KaTeX"）。阅读器路由 DOM 265 KB，说明 480 块与公式都真渲染了。
- 回归：`pytest` 100 passed / 1 skipped；后端 `scripts/e2e_check.py` 32/32。
- 代码量：前端 6,983 行（ts+vue）+ 199 行 CSS，与迁移前基本持平（诚实记录，见
  `frontend/README.md` §6 的收敛建议）。
- 迁移参考用的旧"零构建"实现（`frontend/.legacy/`）在自检通过后已删除，避免死代码堆积。

---

## 2026-09-22 第四轮（续）：按用户口径改目录与对照

用户两点反馈，都是实质性改动，均已落到后端 + 前端并自检通过。

### 1. 目录改为"按 Markdown 标题结构"

**问题**：目录是前端按块的字号/编号猜的层级。实测 `turboquant-iclr26` 出来 38 条，其中
算法伪代码行（`3: Generate a random projection matrix …`、`4: Procedure QUANT(x) …`、
`6: r ← x − DEQUANT(idx)`）与表格数值行（`1645.2945.16`、`68.3859.5446.28`）都被当成标题；
`0.1 PROBLEM DEFINITION` 还被挂到了 `ABSTRACT` 下面。

**根因（都在后端数据面）**：
1. `md/blocks.json` 里标题的 `text` **带着 `##` 标记**（`## ABSTRACT`）——与"Block 存裸文本、
   Markdown 标记由生成器统一加"的既定决策不一致，阅读器会把 `##` 直接显示出来；
2. `blocks.json` **没有 `heading_level` 字段**，阅读器只能回退到字号启发式；
3. `heading_level()` 只看"开头是不是编号"：`3:` 这种行号也被当成章节号；
4. 生成 Markdown 时对所有 `type=heading` 的块都加 `#`，于是假标题也进了 Markdown 与目录。

**改法**：
- `parsing/md/generator.py`：新增 `heading_level_or_none()`（伪代码行/数值行 → `None`）、
  `normalize_heading_text()`（只取首行 + 章节号补空格 + 附录字母补空格）；
  `blocks_index` 现在写**裸文本** + `heading_level`（假标题记 0），生成 Markdown 时假标题不加 `#`；
- `services/paper/reader_service.py`：新增 `build_outline()`（嵌套目录 + 编号），
  `_heading_level` 优先读索引字段，`build_reader` 内嵌 `outline`；新增
  `GET /papers/{id}/outline`；对照方向新增 `en_left/zh_left`（旧的 `en_top/zh_top` 仍接受并归一化）。
- 前端：`ReaderToc.vue` 改为渲染**可折叠嵌套目录**（缩进按层级、显示编号与页码），
  `flattenOutline()` 在 `utils/format.ts`。

**效果**：23 条（原 38），层级与编号正确（`1 → 1.1/1.2`、`2 → 2.1/2.2/2.3`、`B → B.1/B.2`、
`C → C.1/C.2/C.3`），`0.1` 不再挂在 `ABSTRACT` 下，附录粘连也修好了：
`AEXTENDED PRIOR WORK` → `A EXTENDED PRIOR WORK`、`B.1SHANNON…` → `B.1 SHANNON…`。
规范化只在**阅读器索引/目录显示**这一层做，**不动 Markdown 事实源**，因此 `content_hash`
与已译内容不受影响（重跑 `pr.py build` 后译文按 block_id 保留）。

排查脚本：`scripts/show_outline.py`（打印任意论文的目录树，人工核对用）。

### 2. 对照改为"左右对照"，左栏可放原 PDF 或原文

用户口径：**对照应该是左右两栏，左侧可以原 PDF 或者 AI 给出的原文**。

**改法**：中栏在对照模式下切成两栏（`ReaderOriginalPane` + `ReaderTranslationPane`）：
- 左栏内容可切 **原 PDF 版面页图**（+ bbox 叠加，可点选、可缩放、可开关块框）或
  **原文文本**（Markdown 渲染，标题按层级）；
- 右栏是译文；`pair_order = en_left | zh_left` 决定原文在左还是译文在左；
- **SyncController**：`composables/useScrollSync.ts` 以 `[data-page]` 容器为锚点做双向联动
  （页图每页等高、译文页内长短差异大，按比例硬映射会越读越偏），页内再按相对偏移微调，
  写入对侧时加 120ms 守卫防回环；工具栏"联动"或快捷键 `L` 可关；
- 点目录项/任一侧的块 → 两栏一起跳到同一页同一块；
- 窄屏（<1100px）对照自动退化为单栏（A2-5）；
- 深链参数：`?view=` `?order=` `?left=` `?block=`（分享与自动化自检都用它）。

`ReaderPane.vue`（旧的"上下对照"单组件）已删除，拆成上面的两个 pane + `ReaderBlock.vue`
（块渲染唯一实现：标题层级、KaTeX 公式、C 级公式附高倍裁图、高亮注入）。

### 3. 自检与回归

- `scripts/frontend_check.py` 升级为**按窗口尺寸断言**：桌面 1680×1050 断言左右两栏
  （`cmp-pane`、原文在左/译文在左、联动），900×900 断言窄屏退化（有 `single-pane`、无 `cmp-pane`）；
  另加"左栏=原 PDF"（`class="canvas"`、`bbox`）与"左栏=原文文本"（`rblock`）两个用例，
  以及 `/papers/{id}/outline` 的接口冒烟 → **34/34 通过**（阅读器桌面 DOM 484 KB，
  左栏=原文文本时 667 KB）。
- 后端回归：`pytest` 100 passed / 1 skipped；`scripts/e2e_check.py` **33/33**。

---

## 2026-09-22 第四轮（再续）：修联动手感 / 块对齐 / 整页滚动

用户反馈三条，全部定位到根因并修复，且都做成了自检项。

### 1. 滚动批注栏导致整页滚动（页面尺寸问题）—— CSS Grid 只写 columns 的坑

**测量**（iframe 内 1600×1000 视口，probe 读布局盒）：`.reader` 容器 948px，
但 `.reader-content` 与 `.reader-panel` 都被撑到 **1372px**；`documentElement.scrollHeight=1513`
→ 整页可滚，侧栏自身永远不滚。

**根因**：`.reader` 只声明了 `grid-template-columns`。没有显式 `grid-template-rows` 时，
内容行是 `auto`，按 max-content 撑开并溢出固定高度的容器。另有两个次要问题：
侧栏长文本（block id / 长公式串）不换行、各栏缺 `min-height: 0`。

**修法**：`.reader { grid-template-rows: auto minmax(0,1fr); overflow: hidden }`、
三栏 `min-width/min-height: 0`、`.app-main.wide { height: calc(100vh - header); overflow: hidden }`、
`.panel-body { overflow-y:auto; overflow-x:hidden; overflow-wrap:anywhere }`。
复测：`docScroll == viewport (1600×1000)`、无越界元素。

### 2. 块没有对齐译文端 / 译文排版与原 PDF 差异过大

**修法**：新增 `ReaderCompareRows.vue` —— **一行一个块的左右对照**（左原文、右译文），
单一滚动容器 → 每个块天然对齐、也没有联动开销；中英两侧统一字号（14.5px）、行距 1.8、
标题层级同一套 `.lvl-*`（标题、作者、摘要行都逐行对齐）。**并把它设为对照的默认形态**；
想左右分别滚、看原始版面时点「原 PDF」切到两栏模式（`ReaderOriginalPane`）。

### 3. 联动卡顿

**根因**：每次滚动事件都对 480 个块做 `querySelectorAll + getBoundingClientRect`（布局抖动），
且用 `throttle(180ms)` 节流 → 一跳一跳。
**修法**：`useScrollSync` 改为**偏移缓存 + 二分查找 + rAF 合并 + 90ms 写回守卫**；
缓存失效时机：切视图/顺序/左栏、缩放、窄屏切换、resize、译文流式上线（防抖 250ms），
`getMetrics` 还会比对 `scrollHeight` 自动重测（图片加载完高度变化）。

### 4. 自检升级（37 项）

`scripts/frontend_check.py` 新增 **§③ 整屏布局断言**：用 iframe（1600×1000）加载
「左右对照(块对齐) / 左右对照(原PDF) / 纯译文」三种模式，断言 `docScroll == viewport`、
侧栏底边不出视口、无越界元素；路由用例改为断言块对齐（`rows-wrap`/`row-pair`/`cell side-`）
与原 PDF 两栏（`cmp-pane`/`canvas`/`bbox`/`联动`）。结果：**37/37 通过**。

截图对比留档：`data/logs/reader_before.png`（修复前：侧栏被裁、整页滚）、
`reader_after_rows.png`（块对齐对照）、`reader_after_pdf.png`（原 PDF 两栏）。

---

# 第四轮：翻译改成"纯页级 + 原文/译文同源"，阅读器对照重做，前端整体 WinUI 3

> 用户口径（四条）：① 大幅简化翻译过程——每页同时给页图与文本，让模型分开输出 OCR 原文与译文
> （Markdown，尽量还原原图），**移除分块翻译逻辑，只保留按页翻译，完全信任模型输出**；
> ② 换个方式实现前端「原 PDF / 译文」对照，现有方式完全无法对齐，且对照时两侧文字观感要接近；
> ③ 目录支持同时显示中英文；④ 移除蓝紫色图标、去掉页面多余解释、整体 WinUI 3、禁止 emoji、图标只用 SVG。

## 1. 翻译：从"三次产出 + 对齐算法"变成"一次产出 + 序号对齐"

| | 旧（第三轮） | 新（本轮） |
|---|---|---|
| 调用单元 | 正文按页、图注按块、公式透传、失败再回退块级 | **只有页**：一页一次调用 |
| 模型输入 | 该页抽取文本（无图） | **该页页图（150dpi PNG）+ 页内抽取文本 + 稳定前缀** |
| 模型输出 | 整页中文 Markdown | `{"original_md": …, "translation_md": …}`（原文转录 + 译文，块划分一一对应） |
| 对齐 | `page_aligner`：公式/标题/图注锚点 + 保序 DP 切回 `block_id` | `pair_rows`：块序号直接配对；块数不等才跑一次 difflib 序列对齐 |
| 校验/回退 | 宽松校验 + 有上限的块级回退补译 | **无校验、无回退**（`align` 只做质量标记，供前端提示） |
| 缓存 | 稳定前缀 + 批结果 + 块级三层 | `md/pages.json` 逐页原子落盘（断点续跑就是缓存） |
| 进度口径 | 块 | 页 |

**为什么这样改**：对不齐的根因不是"算法不够好"，而是**两栏内容来自两次不同的产出**
（PDF 抽取 vs 模型整页译文），行数本来就不一致；切块算法再准也只能逼近。让模型在同一次
调用里同时给原文与译文，块序号**天生一一对应**，整条对齐/回退链路就可以删掉。

新增/改写：`backend/services/translation/page_translate.py`（prompt 契约 / `call_page` /
`split_blocks` / `pair_rows` / `PageStore`）、`translation_service.py`（逐页任务体）。
删除：`page_aligner.py`、`scripts/compare_granularity.py`、`tests/test_page_aligner.py`、
`md/translation_cache.json` 与 `granularity=page|block`、`block_ids`、`strict_validation`、
`fallback_*`、`formula_changed` 等参数与指标。
新增事件 `page_translated`（payload 带整页 rows），轮询兜底改为
`GET /papers/{id}/translations?since_page=N`。

## 2. 对照：一个滚动容器，两种呈现

- **左栏=原文文本（默认）**：`ReaderPageRows.vue` 一行两列（左原文 / 右译文），
  两侧只差字体族（英文衬线 / 中文无衬线），字号 15px、行距 1.78 完全一致 → 逐段天然对齐。
- **左栏=原 PDF**：每页页图贴在该页译文左侧，页内 `position: sticky`（滚该页译文时页图不跑），
  页与页按文档顺序排列 → 按页对齐。

删除 `useScrollSync.ts` / `ReaderBlock.vue` / `ReaderCompareRows.vue` /
`ReaderOriginalPane.vue` / `ReaderTranslationPane.vue`；滚动跟踪改为**偏移缓存 + 二分查找**。

## 3. 双语目录

`build_outline` 改为从页级产物的 `type=heading` 行生成：每项同时带 `text`（英文原文标题）
与 `text_zh`（中文译文标题）。前端 `ReaderToc.vue` 渲染成**英文在上、中文在下**两行；
没有页级产物时退回解析索引（只有英文）。

## 4. 前端 WinUI 3

`tokens.css` 换成 Fluent 令牌（单支强调色 #0078D4 / 深色 #60CDFF，**删掉紫色与渐变**），
`base.css` 换成 Fluent 控件（控件高 32 / 圆角 4 / 底部加深描边 / 焦点框 / 分段控件）；
新增 `components/AppIcon.vue` —— **全站唯一图标入口**（内联 SVG，`currentColor`，50 个语义图标）。
全量清理：emoji（`ReviewView` 的 🎉）、字符图标（`← × − ＋ ▸ ▾`）、
页面里解释实现细节的说明段落（"规划 §…"、"阶段 14–17"、"产线 A/B/C"、"IR 可追溯"、
接口路径科普、密钥策略说明等）。核查脚本一次性扫描确认：`var(--purple)`、`linear-gradient`、
emoji、字符图标 全部 0 命中。

## 5. 实测与验收

- **真实 DeepSeek 视觉模型翻译 TurboQuant 22 页**：22 次调用 / **186.8 s** / 成功 22 失败 0 /
  **277 行全部 `align=exact`**；token：prompt 60.7k、completion 143.6k（推理 97.6k）；
  3 页因思考模式吃光 `max_tokens` 触发升档（8192→16384→32768→65536）后成功，无需回退。
- `python -m pytest tests -q`：**77 passed / 1 skipped**（`test_page_translate.py` 已按新契约重写）。
- `scripts/e2e_check.py`：**35/35 通过**（含"页产物 = 原文 + 译文两份 Markdown"、
  "行对齐逐块成对"、"双语目录 24 条全部带译文"三项新断言）。
- `scripts/frontend_check.py --block-markers`：**36/36 通过**（14 条路由真实渲染 + 4 种阅读器形态
  + 整屏布局约束）。
- 留档截图：`data/logs/shot/reader-text.png`（文本对照）、`reader-page2.png`（原 PDF 对照）、
  `reader-zh.png`（纯译文）、`library.png`（论文库 WinUI 3）。

## 6. 已知取舍

- 思考模式对"整页转录 + 翻译"这种长输出并不划算（推理 token 占 completion 的 68%）。
  若只追求成本/速度，可把 vision 模型的 `parameters.thinking` 设为 `disabled`（任务与前端无需改动）。
- 行 id 是页内序号（`p003-00`），**重译会重建行 id**，历史高亮/批注可能落空；
  未翻译页退回解析块时使用的行 id 是 `block_id`（稳定）。
- 原 PDF 模式下页图与译文只保证**页级**对齐（像素级逐行对齐在这个数据模型下不可能，
  除非回到"两栏各自滚动"，而那正是本轮要淘汰的方案）。

## 7. 第五轮修补（用户反馈两条）

**① 插图不再显示在译文左侧 / 页图下方**。上一版把解析切出的 figure/table 图放在
"原 PDF"栏的页图**下方**（`.media-strip`），于是图片出现在译文左边、与译文毫无关系。
现在改为**跟随原文**：`ReaderPageRows` 按图号把 `pages[].media` 挂到该页**图注行之后的原文侧**
单元格里（`.row-media`）；匹配不上图号就按顺序挂到未占用的图注行，没有图注行则放页尾；
"原 PDF"模式下不再重复渲染缩略图（页图里本来就有）。
实测 DOM：`row-media=5`、`media-strip=0`（全站不再有那种贴页图下方的缩略图条）。

**② 正文字号可调，默认"小"**。工具栏新增 小 / 中 / 大 分段控件（13 / 15 / 17px，**默认 13px**），
`ReaderPageRows` 用 `--reader-font-size` 下发，`ReaderRow` 的正文、标题（1.4/1.24/1.1/1em）、
图注（0.92em）、文献（0.88em）与 KaTeX 公式全部相对缩放；选择写 `localStorage`。
字号变化会让行高变化，滚动跟踪的偏移缓存在 `watch` 里一并失效重测。

## 8. 第六轮修补（用户反馈两条：图片要像论文、提示词以图片为准）

**① 插图改成"按论文的排法"显示，并修掉裁图重叠**。两处改动：

- **裁剪**（新增 `backend/services/paper/figure_regions.py`）：解析阶段给的 figure bbox 会合并相邻图 ——
  TurboQuant p8 的 Figure 3 记录 bbox 是 `[95.8, 535.0]`，把上面的 Figure 2 与中间的
  `2.2 NEEDLE-IN-A-HAYSTACK` 标题一起框了进来，裁出来就是"同一张图出现两次、图里夹着正文"。
  现在**按图注/标题锚定**收紧：图注在图下方时 `top = max(图块 y0, 上一张图底边, 图注上方最近标题/图注底边 + 6pt)`、
  `bottom = 图注顶边`；题注在表上方时反向。实测 p8 Figure 3 收到 `[304.6, 541.6]`，干净了。
  裁图**按需 600dpi 渲染**（`GET /api/v1/papers/{id}/figures/region/{page}/{i}`）并落盘缓存，
  缓存名带 bbox 哈希；`ir mtime` 级缓存让阅读器请求不再逐页读库。回归测试 `tests/test_figure_regions.py`。
- **排版**：正文里的插图挂在该图**图注行的上方**（论文排法：图在上、图注在下），
  对照模式 `grid-column: 1 / -1` **通栏居中**，原文/译文/对照三种视图都显示，
  点击打开大图；「原 PDF」模式不重复显示（页图里已有）。
- 侧栏"插图"标签也改用同一套裁图（`figures_payload` 走 `figure_regions`），不再显示旧的重叠裁图。
- 表格不贴图：提示词已要求逐格转录成 Markdown 表格，贴图会与文字表格重复。

**② 提示词强化"以页图显示的内容为准"**。`page_translate.SYSTEM_PROMPT` 加了最高原则一节：
页图是唯一事实来源，解析器文本只是参考，**冲突时一律以页图为准**；图注编号必须与页图一致；
图内的子图标题/坐标轴/刻度/图例看得清就抄；**表格逐格照页图抄数字**（Markdown 表格 + `**分组行**`），
译文侧只翻译表头这类自然语言、数字与方法名原样保留；禁止用常识补全。
实测 p8/p9 重译后：Figure 2 的图内文字（子图标题、坐标轴、图例）被如实转录成一段，
Table 1 变成完整的 Markdown 表格（数字照抄、表头译为「方法 / KV 大小 / 摘要 / 合成 / 代码」）。

**③ 整篇按新提示词重译 + 表格通栏排版**。
- 用 API 触发整篇 `retranslate`（force）：**22 页 / 80.8 s / 成功 22 失败 0 / 280 行全部 `exact`**；
  prompt 67.8k + completion 81.4k（推理 34.1k）——比第一次整篇（186.8 s、推理 97.6k）快一倍多，
  主要是前缀缓存命中 + 提示词更明确后模型少绕弯。升档 1 次。
- 抽查新产物的提示词效果：参考文献行 `original == translation`（**不翻译**，共 121 行属于公式/
  文献/代码这类"原样保留"）；p9 出现 `type=table` 行；p8/p9/p7/p10/p21 都多了"图内文字"转录段。
- **表格改为通栏**：9 列数字在半栏里会把 `45.29` 折成两行，因此 `type=table` 的行跨两列、
  两侧上下排（原文表在上、译文表在下），单元格 `nowrap` + 数字右对齐，放不下时横向滚动。

## 9. 第七轮修补（用户反馈：图片还是不能在 Markdown 中正常显示）

**根因**：页级产物里的两份 Markdown **完全没有 `![]()` 图片语法** —— 模型只写图注，
插图是前端从 `pages[].media` 侧挂上去的，所以"Markdown 本体"和任何 Markdown 渲染器里都看不到图。

**修法**（读取时注入，**不需要重新翻译**）：

- `figure_regions.markdown_refs()`：按图号生成图片行（并 `ensure_region_crop` 保证裁图已落盘）；
- `figure_regions.inject_images()`：把图片行插到**对应图注的上方**（论文排法），
  模型自己写了 `![` 就尊重不动，匹配不到的图放末尾、绝不丢图；
- `reader_service.bilingual_markdown()`：逐页输出 `original_md` / `translation_md`（带图），
  并落盘 `md/translated.md`（图片用 `../figures/…` 相对路径，编辑器直接可看）；
- 新接口 `GET /api/v1/papers/{id}/markdown/translated`（应用内用 `/api/v1/...` 引用形式）；
- 阅读器工具栏新增 **Markdown 视图**（文档图标）：上下对照 / 原文 / 译文 + 逐页切换 +
  下载整篇（下载时把引用补成 `http://127.0.0.1:8710/api/v1/…`，Typora/VS Code 也能看图）；
  深链 `?md=1&mdpage=8`。「上下对照」里译文段的重复图片行会被去掉，图只看一次。
- 回归测试：`tests/test_figure_regions.py` 新增 4 项（图片插在图注上方 / 未匹配放末尾 /
  已有图片语法不动 / 空输入不报错）。整篇实测：22 页、10 条图片引用（5 图 × 原文/译文）。

## 10. 第八轮修补（合并左右栏；公式改从 Markdown 提取、下线复核；修插图定位）

**① 插图"定位"失效（真 bug）**。合并前的右栏插图条目用的是合成 id（`fig-p008-00`），
阅读器的行 id 是页内序号（`p008-03`），于是 `focusRow` 找不到锚点、退化成跳到第 1 页。
修法：`reader_service.row_id_of_caption()` 用**图号**反查该页图注行的行 id，
`figures_payload` 的 `block_id` / `row_id` 直接给行 id，并附 `page`；
前端 `focusRow(id, {page})` 在行 id 失效时**按页定位**而不是乱跳，彻底失效时给一次性提示。
批注/笔记/高亮同理：`build_reader` 给每条锚点补 `page`（行 id→页，回退块 id→页）。

**② 左右栏合并成一栏**（用户口径）。`ReaderView` 从三列 `260 / 1fr / 340` 改成两列
`300 / 1fr`：目录与批注/笔记/公式/插图/术语/任务合并进同一个左栏，顶部一排图标页签切换
（带计数），正文宽了 340px —— 对照、插图、通栏表格都更好读。窄屏弹同一个栏。
`ReaderSidePanel` 由"自带页签"改成受 `tab` 属性驱动；深链 `?side=formulas`。
`frontend_check.py` 的布局探针与阅读器标记随之更新（`.reader-aside` + `.panel-body`/`.toc`）。

**③ 公式：改为"从 Markdown 单独提取"，复核整条下线**（用户选 A）。理由：阅读器展示的公式
本来就来自模型（看页图生成的 LaTeX），解析器的公式卡只是解析质检副产物，人工复核产线空转。
- 新增 `reader_service.markdown_formulas()`：从页级产物的行里抽公式（行间 `$$…$$` + 行内 `$…$`），
  带页码、行 id、上下文（原文/译文）。**行内公式必须成对扫描**：直接正则会把两段公式之间的正文
  也当公式（`$d$-dimensional … $B$`），实测多出成百条噪声；成对扫描后 = 行间 31 / 行内 313。
- `GET /papers/{id}/formulas` 默认 `source=markdown`（`source=parse` 才回解析公式卡）；
  顺手修掉一个 Python 坑：文件里**旧版 `formulas_payload` 定义在后面，把新定义遮蔽了**，
  导致 `source` 被当成 `grade` 过滤、返回 0 条。
- 前端新增 `ReaderFormulaList.vue`（只读：KaTeX 渲染 / 复制 LaTeX / 定位到正文行，行间/行内/全部筛选）；
  阅读器侧栏去掉公式卡与"复核"按钮，工具栏去掉"复核 N"徽章，删掉公式复核弹窗与 `review` 事件；
  `ReviewView` 只保留插图体检 / 一致性 / 翻译存疑（`isFormulaKind` 过滤）；
  `paper.review_open` 也只统计非公式类待办。
- 自检：`frontend_check.py` **37/37**（新增"公式页签"用例）、`e2e_check.py` **35/35**、
  `pytest` **87 passed / 1 skipped**。

## 11. 第九轮小改（用户反馈：侧栏只有图标不直观；批注与笔记合并）

- **侧栏页签加文字**：原来 6 个页签只有图标 + 计数，用户看不出哪个是哪个。改成
  **图标 + 文字两行**（`目录 / 批注·笔记 / 公式 / 插图 / 术语 / 任务`），窄栏自动换行；
  `AppIcon` 新增 `list` 图标给"目录"用。
- **批注 + 笔记合并为一个页签**：`SideTab` 去掉 `notes`，页签内按时间倒序合并两类条目
  （徽章区分 `划词解释 / 批注 / 笔记`，显示页码与相对时间），每条都能定位；
  笔记也支持删除（新增 `deleteNote()` → `DELETE /notes/{id}`）；`?side=notes` 兼容映射到
  `annotations`。`ReaderSidePanel` 的 `notes` 属性保留（用于合并项），事件新增 `delete-note`。

## 12. 第十轮修补（用户反馈：多行 `$$…$$` 公式没被正常解析）

**现象**：p019-08 那类**多行**公式（`$$\begin{aligned} … \\ … \end{aligned}$$`）没渲染，
前端显示原始 LaTeX，而且 `_{\text{mse}}` 里的下划线被当成强调渲染成了 `<em>`。

**根因**（读 `marked-katex-extension` 源码定位）：它的 block 规则是
`/^(\${1,2})\n((?:\\[^]|[^\\])+?)\n\1(?:\n|$)/` —— 只认「`$$` **单独占一行**」的写法
（`$$`⏎内容⏎`$$`）。模型输出的是 `$$\begin{aligned}`（定界符与内容同行），
而它的行内规则又排除换行（`[^\\\n]`），所以整块公式两边都匹配不上、掉进普通段落；
单行 `$$ … $$` 之所以正常，是因为被**行内**规则以 `displayMode=true` 接走了。

**修法**（`frontend/src/composables/useMarkdown.ts`）：在交给 marked **之前**自己接管行间公式 ——
`extractBlockMath()` 扫描 `$$…$$`（可跨行、定界符可与内容同行；跳过 ``` 代码围栏）→ 换成
`%%BLKMATH<i>%%` 占位符（与高亮同一套占位符思路）→ `marked.parse` → `renderBlockMath()`
用 `latexToHtml(tex, true)`（KaTeX displayMode、`throwOnError:false`）替换回去。
行内 `$…$` 仍交给扩展，行为不变。

**验证**：p019 的 3 条 aligned 公式左右两侧都渲染成 KaTeX 对齐公式（`katex-display=62`、
`katex-error=0`、DOM 里 `$$` 残留 **0**）；`frontend_check.py` 新增回归用例
`reader-多行公式`（断言 `katex-display` 出现 + DOM 无 `$$`），**38/38 通过**。

## 13. 第十一轮修补（「左 PDF / 右译文」不显示插图；顺带修文字列取错侧）

- **插图在页图模式也显示**：`ReaderPageRows` 原先在 `left=page` 时**故意不渲染插图**
  （理由：页图里本来就有）。但页图缩放后图内数字往往看不清，用户要的是"译文列里也能看到图"。
  现在插图照旧挂在**译文列的图注上方**（`.col-rows > .row-pair > .row-figure`，点击看大图）；
  只有"纯原 PDF"单栏（`view=original&left=page`）不再重复渲染（那一种整页就是页图、没有文本行）。
- **文字列永远是译文**：`pageRowSide` 之前写成 `enLeft ? 'zh' : 'en'`，于是「译文在左」时
  文字列也放**原文** —— 结果是"页图 + 原文"两栏都是原文、译文消失。现在恒为 `'zh'`：
  左 PDF→[页图 | 译文]，译文在左→[译文 | 页图]，两种顺序都正确。
- 实测截图：`pdf-ok.png`（左 PDF / 右译文，插图在译文列）、`pdf-ok-zh.png`（译文在左，同理）。

## 14. 第十二轮：整体移除「复核模式」（前后端 + 数据库）

用户口径：复核模式不再需要，前后端代码一并移除。范围与事实：

**前端**
- 删除 `views/ReviewView.vue`、`components/FormulaCard.vue`；路由 `/review/:paperId` 移除
  （访问会走 `/:pathMatch(.*)*` → `/library`）；`AppHeader` 的 active 判断去掉 `review`；
  论文库卡片去掉"待复核 N"徽章与"复核"按钮；`ReaderView` 去掉 `reviews` 状态与 `reviews_open`；
  `types.ts` 去掉 `ReviewItem` / `Paper.review_open` / `ReaderStats.reviews_open`；
  `utils/format.ts` 去掉 `REVIEW_KIND_LABEL` / `SEVERITY_TEXT` / `GRADE_CLS`。

**后端**
- 接口：`GET /papers/{id}/review`、`PUT /review/{reviewId}` 删除（均 404）。
- 服务：`translation_service.review_list/resolve_review` 删除；`reader_service` payload 去 `reviews`、
  `stats` 去 `reviews_open`（`_stats` 签名少一个参数）；`paper_service` 去 `review_open`
  （`_to_dict` / `refresh_progress_counters` / 删除论文时清理 review 行）。
- 模型与库：删除 `Review` 模型与 `Paper.review_open` 列，并执行迁移
  `ALTER TABLE paper DROP COLUMN review_open` + `DROP TABLE review`
  （业务库 13 → **12 张表**；`review_open` 是 NOT NULL 无默认值，不删列会让新论文 INSERT 失败）。
- 解析产物：删除 `ReviewItem` 与 `ParseResult.reviews`、`build_review_checklist()`、
  `ir_writer` 的 review 写入/清理、`ir_reader.reviews()` 与 snapshot/stats 里的 review 字段、
  `schema.sql` 的 review 表；`handlers._sync_to_mysql` 不再写 review 行；
  Markdown 里 C 级公式的说明由"（待校对，见复核项 R-xxxx）"改为"（C 级，只有裁图）"。
  一致性检查与插图体检**保留**，只落进 `result.stats["consistency_findings"]` 与质量报告。
- 脚本：删除 `scripts/export_review_md.py`；`e2e_check.py` 用"带插图双语 Markdown"检查替换两条复核检查；
  `frontend_check.py` 去掉 review 路由用例。

**踩坑与善后（记录在案）**
- 用 PowerShell `(Get-Content …) -replace … | Set-Content -NoNewline` 改文件，把
  `reader_service.py` 的换行全吃了（32194 字节单行）；随后 `Remove-Item` + `Copy-Item` 的组合
  又误删了 `scripts/frontend_check.py`。两个文件都已按内容重建（`reader_service.py` 749 行、
  `frontend_check.py` 364 行），并重跑自检确认行为一致。**教训：文本替换一律走编辑工具，不用 PowerShell 改写文件。**
- 自检：`pytest` **87 passed / 1 skipped**、`frontend_check.py` **40/40**（新增 formulas/figures/markdown
  三条接口冒烟）、`e2e_check.py` **35/35**。





## 15. 第十五轮：插图裁图三类修复 + 「图+图注」卡片 + 顶栏改多标签页

用户口径（5 条）：① 侧栏插图卡片改成图注在上/图在两枚徽章在下（图号靠右）；
② 裁图不要多截少截，并允许一张图截成两份；③ 搜索框移到「更多操作」左侧并可用；
④ 原文在左/译文在左改成 SVG 图标；⑤ 顶栏改多标签页、移除返回按钮。
第 ⑤ 条第一版理解错了（把论文标题做成标签）——用户澄清：**要真正的多标签页外壳**：
引入首页概念、清空顶栏原有内容（品牌 + 主导航）、顶栏只做标签条。下面是最终实现。

### 插图裁图（`backend/services/paper/figure_regions.py` + `parsing/extract/render.py`）

实测三类"多截/少截"的根因，逐条修：

1. **边界从文字中间穿过**（多截）。排版软件给文字块的 bbox 比字形墨迹宽 1~2pt，解析阶段
   给的插图 bbox 又是"位图 + 矢量"并集，两个边界差一点点就会把图注顶部切掉一条
   （TurboQuant p8 的 Figure 3 实测如此）。新增 `_snap_edges`：把四条边统一贴到相邻文字的
   边上（"离文字太近"就推到文字边上），**只认贴着这条边界的文字**——早期版本没加这条护栏，
   结果 p3 的图体 bbox 被下方的正文平白推掉一大截。
2. **裁图带白边**（少截的观感）。新增 `render.ink_bbox` + `render_crop_png(ink_trim=True)`：
   先 110dpi 灰度渲染量出非白像素包围盒，再按它 600dpi 出图。改用 `Pixmap.samples` 自己扫
   （PyMuPDF 1.28 已删除 `Pixmap.get_bbox`，旧写法直接 AttributeError）。
   同时把 `ensure_region_crop` 的 `padding` 从 3.0 改成 0：**余量必须在量出墨迹之后加**，
   先用 padding 撑大裁剪框，撑出去的正是旁边图注/图题的半行字。
3. **一张图塞进两张图的内容**。新增 `_parts_of`：区域内部夹着**别的图注**时按图注上下边切成
   两段（TurboQuant p8 的 Figure 3 框住了 Figure 2 的图注，硬裁必然既多截又少截）；图体画到
   页脚时最后一段打 `continues` 标记，跨页续图由 `_continuation_page` 在下一页补一条"跨页续图"
   条目（保守判据：源图必须画到页脚 + 下一页页首有横向重叠的内容）。多段各自是独立区域下标，
   URL 不变，缓存文件名带 `part`。

### 「图 + 图注」图表卡片

用户指定"试一试" GitHub 上的 PDF-Figure-Extractor（`Aaron-gx/PDF-Figure-Extractor`）。
本机 github.com 直连不通，走 `codeload.github.com` 拉到 `data/logs/_pfe` 实测两篇论文：
它 5/5 命中、判位思路与本项目同源（caption 锚定 + 向上找位图/矢量并集），但 TurboQuant p8
的 Figure 3 **上沿把第一行子图的标题切掉了**。结论：**不 vendor 它的代码，只借"图 + 图注合成"**。
新增 `render.figure_card_png`：分别按墨迹裁出图体与图注（图注先用 96dpi 量一次墨迹行范围，
避免裁出半行字），拼成白底卡片；接口加 `?card=1`，侧栏与"放大看大图"都用卡片
（正文里的图仍是纯图 —— 图注在正文里本来就有自己的一行，重复显示没意义）。

### 搜索（ReaderView）

搜索框移到「更多操作」左侧（它属于"对整篇文档的操作"，与那组低频动作同一簇），加了清空按钮、
Esc 清空、命中条数 toast，命中后自动切到目录页签并把**命中列表排到目录最前面**（`order:-1`），
目录页签徽章显示命中数。新增深链 `?q=xxx`（分享一条检索、也供自检脚本用）。

### 原文栏呈现 → SVG 图标

`AppIcon` 新增 `layout-original-left` / `layout-original-right`（左格实心 = 原文所在侧），
两个按钮只留图标 + tooltip。

### 应用外壳 → 多标签页（本轮最大的改动）

- **新增首页** `views/HomeView.vue`：主导航（6 张卡片）+ 继续阅读 + 最近导入。
- **顶栏清空重做**：`components/TabBar.vue` 取代 `components/AppHeader.vue`（后者删除）。
  顶栏只剩"品牌标 + 标签条 + 首页/新建按钮"，不再有主导航。
- **标签状态** `stores/tabs.ts`：一个标签 = 一个路由位置（`name + params`，同篇论文不会开两个），
  `localStorage` 持久化，支持关闭 / 关闭其它 / 双击重命名 / 拖拽换序；论文标题这类异步信息
  由视图加载完调 `setTabTitle()` 回填。路由 `afterEach` 调 `activateRoute()`，所以浏览器
  前进/后退、外部深链进来都会正确落到标签上。
- 各视图"打开论文"统一走 `openTab()`：`LibraryView`（标题 + 阅读按钮）、`HomeView`、
  `NotesView`、`KnowledgeView`；`/` 与通配路由改为重定向 `/home`。

**验证**：`pytest` 113 passed / 1 skipped（唯一失败项是 `test_imports` 要求 fastapi，
本机 anaconda 主环境没装，与本次改动无关）；`vue-tsc` 干净；`frontend_check.py` **43/43**
（新增 `home` 用例 + `reader-搜索` 用例）。用户口径：**UI 验证由他自己做**。

**注意**：用户原先在 8710 端口跑着旧进程，它会加载新构建的前端产物但后端还是旧代码；
核验后端改动（`?card=1`、插图接口）时我另起了 8711（`cli/pr.py serve --port 8711`，已完成任务）。

### 15.1 用户回看后的两条修正

1. **首页不可关闭 + 去掉顶栏右侧两个按钮**。`stores/tabs.ts` 加 `HOME_KEY`：`closeTab` 对首页
   直接拒绝、`restoreTabs` 保证首页永远存在且固定在最左、`closeOtherTabs` 也保留首页；
   标签条上首页那个标签**不渲染 ×**，右键菜单也不给"关闭标签"。按口径同时删掉顶栏右侧的
   ⊕（新建）与 🏠（首页）两个按钮 —— 导航靠首页标签即可，`.tabbar-btn` 样式一并删除。
2. **前端样式异常**（截图里论文库卡片的论文标题被套了一个"输入框壳子"）。根因：
   把标题从 `<a>` 改成 `<button>` 之后，`base.css` 里那条 `input, select, textarea { … }`
   **并不包含 button**，所以它并没有"覆盖"UA 给 `<button>` 的默认盒子 —— `<button>` 的 UA
   默认就是 `appearance: auto` + `background-image: linear-gradient(…)` + `padding: 1px 6px`，
   于是标题长出了边框/内边距/居中，看起来像个输入框。
   修法是在 `base.css` 里加一条**全局重置**：
   `button:not(.btn) { appearance: none; background: none; border: 0; padding: 0; font: inherit; … }`，
   把"没有 `.btn` 的按钮"还原成纯文字；按钮外观一律靠 `.btn`（规则在后面且优先级更高）。
   审查同类风险：`HomeView` 的导航卡与论文行、`TabBar` 的标签都自带 padding/尺寸，不受影响。
   实测（iframe 量 computed style）：`.pc-title` → border 0 / 背景透明 / padding 0 / 高 20px；
   `.btn.primary`（阅读）→ 1px 边框 + 强调底色 + padding 11px，**没有被误伤**。
   这类"元素语义一换就长出 UA 盒子"的问题以后还会遇到，所以修在全局而不是某个页面。
**⚠️ 上一段那句"同类风险不受影响"的结论是错的 —— 见第 16 节 16.1：它把首页的卡片全抹平了。**

## 16. 第十六轮：首页异常（按钮重置的优先级陷阱 + 标签条 + 缓存头）

用户口径：「还是解决首页异常问题 —— 首页标签页始终高亮，并且胶囊中的文字和图标要居中显示」。
按用户之前的要求，UI 最终由他本人验收，这一轮只做可编程核验（pytest / frontend_check / headless 量盒模型）。

### 16.1 首页卡片被"抹平"：`:where()` 修掉按钮重置的优先级陷阱（本轮真正的异常）

**现象**：首页的 6 张导航卡、卡片里的论文行退化成"没有卡片、没有内边距的一行字"
（headless 截图实测：`.nav-card` 没有白底、没有描边、没有圆角）。

**根因**：15.1 加的全局重置写成了裸选择器 `button:not(.btn)`，它的优先级是 **(0,1,1)**，
**比任何单类规则都高**（`.card` / `.nav-card` / `.paper-row` 都只有 (0,1,0)）。于是这条只想
"抹掉 UA 盒子"的重置，把有意写在类上的 `background` / `border` / `border-radius` / `padding`
一并抹掉了 —— `<button>` 化的卡片就是这么被害的。当时"审查同类风险"只看了一眼"它们自带
padding/尺寸"，**漏了优先级这一层**，结论写反了。

**修法**：整条包进 `:where()`（优先级降为 0）：`:where(button:not(.btn)) { … }`。
作者样式照样稳压 UA 默认样式，但类规则可以正常接管。实测（iframe 量 computed style）：
`.nav-card` → padding `13px 14px` / 白底 / 1px 描边 / 圆角 22px / 柔光投影；`.paper-row` →
padding `7px 8px` / 圆角 14px；`.nav-icon` → 34×34 / pill / `accent-soft`。
**教训**：给"元素默认样式"做重置时，选择器不能压过组件类 —— 审查清单加一条：
新写的全局重置必须用 `getComputedStyle` 抽查若干带类的 `<button>`。

### 16.2 标签条：高亮只认路由 + 「首页」常驻标签不铺胶囊

1. `TabBar.currentKey` 从 `tabsState.activeKey || routeKey(route)` 改成 **`routeKey(route)`**。
   `activeKey` 只是"持久化 + 挤掉最老标签"用的，一旦它残留旧值（上次会话停在首页、或路由还没
   落定），标签条就会点亮错的标签 —— 观感就是"首页标签始终高亮"。现在高亮永远跟着地址栏走。
2. 「首页」是常驻标签（= 被删掉的 🏠 按钮的替身），**不给它填充胶囊**：落在首页时只把图标 +
   文字染成强调色（`.tab.pinned.active { background: transparent; box-shadow: none }`）。
   一个一直亮着的填充块看起来就是"始终高亮的按钮"（用户原话）；内容标签照旧用填充胶囊表示
   当前页，要改回来只是一行 CSS。
3. **胶囊里的图标 + 文字居中**：以前所有标签统一 `padding: 0 4px 0 9px`（右边留给 ×），
   而首页标签没有 × → 内容整体偏右。现在 `.tab` 用 `0 6px 0 9px`（6px + 关闭按钮自身的 3px
   余量 = 视觉左右各 9px），无 × 的 `.tab.pinned` 用对称的 `0 9px`。
   实测：首页标签 `gapL = gapR = 9`，上下各 7.5；论文库标签左右字形留白也是 9 / 9。
4. 首页 hero 文案里残留的 Markdown 星号 `**新的标签页**` 改成 `<strong>`（页面上一直显示字面
   星号，同属"首页异常"）。

### 16.3 静态资源缓存头：改完前端用户还在跑旧 bundle

`backend/main.py` 的 SPA 路由以前给 `index.html` 什么 `Cache-Control` 都没带，浏览器就按
**启发式缓存**把它存下来；一旦 index.html 被缓存住，页面引用的还是旧哈希资源，用户刷新多少次
都是旧界面（本轮排查中真实遇到：用户截图里的首页卡片是旧构建的样子，而新构建已经把卡片抹平了
—— 两边现象对不上，就是版本错位）。现在：

- `index.html`（含 SPA 回退与 `/app/` 深链）→ `Cache-Control: no-cache`（每次回源校验，
  命中 ETag 就是 304，仍然很便宜）；
- `/app/assets/**`（文件名带内容哈希）→ `public, max-age=31536000, immutable`。

实测：`/app/` 与 `/app/library` → `no-cache` + ETag；`/app/assets/index-*.js` → `immutable`。
**注意**：这是后端改动，用户必须重启自己的服务进程才生效；重启后建议 Ctrl+F5 硬刷新一次。

### 16.4 验证

- `vue-tsc` 干净；`npm run build` 通过。
- `frontend_check.py` **43/43**；新增两条回归断言：「首页上 `tab active pinned` 必须存在」
  与「论文库上 `tab active pinned` 必须不存在」（后者正是"首页一直亮着"那个回归）。
- `pytest tests -q` 全绿（跳过项同往常）。
- headless 量盒模型 / 截图复核：首页标签 `bg: transparent`（不再铺胶囊）+ 内容居中；
  论文库标签仍是填充胶囊 + 强调色；首页导航卡恢复白底 / 描边 / 22px 圆角 / 内边距。

### 16.5 「定位到图注」从图片右上角移到图注标题右侧（用户口径）

侧栏插图卡片原来是"图注 / 图 / 徽章"三行，定位按钮用绝对定位**浮在图片右上角**（为了不占
三行的位置）。用户口径改成：**跟着图注标题、落在标题右侧** —— 它是"回到正文里这张图的图注"
这个动作，本来就属于标题行；浮在图上还会压住图的内容。

改法：`.fig-head { display:flex; align-items:flex-start }`，图注 `flex:1; min-width:0`（可换行），
按钮 `flex:none`；`.fig-body` 不再需要 `position: relative`，按钮的半透明底 + `backdrop-filter`
也一起去掉（不再压在图上，直接用它所在面板统一的 `.btn.icon.sm`）。图注多行时按钮只跟**首行**
同高：按钮 26px、图注行盒 18px → `margin-top: -4px` 让两者中心线重合（光学对齐，实测差 0px）。
没有图注的区域（切分出来的续图段）用一个 `.spacer` 把按钮顶到右边，不会挤出一个空标签。

实测（headless 量盒模型）：按钮父节点 = `fig-head`、`btnInsideImageBox = false`、
右边缘与图片右边缘**完全对齐**、在图片上方 22px、与图注首行中心差 `0.0px`；
`vue-tsc` 干净；`frontend_check.py` 43/43。

### 16.6 首页"圆角矩形大小不一致"：`.card + .card` 的竖向间距漏进了网格

用户口径：「首页的圆角矩形大小不一致」。量了截图（1630×766，1.348 px/CSS）里 8 张卡的
真实边界，规律很清楚：

| 卡片 | 高（CSS） |
| --- | --- |
| 论文库（网格第一张，无上边距） | 73.4 |
| 知识库 / 笔记（有上边距） | 60.8 |
| 模型 / 任务 / 设置 | 60.8 |
| 继续阅读 | 高于"最近导入"16px |

**根因**：`base.css` 的 `.card + .card { margin-top: 12px }` 本是给**竖向堆叠**的卡片留间距的，
但它按"相邻兄弟"匹配 —— 网格里的卡片也是相邻兄弟。于是：
① 每排**第一张**（前面没有 `.card`）不带上边距，被 grid 的 `stretch` 拉到"行高"，
比同排其它卡高 12px；
② 行高按"内容 + 上边距"计算，整行白白高 12px（首页顶部因此多出一条空白）；
③ 换行后卡片顶边与同排不齐（底部"继续阅读 / 最近导入"就是左高右低）；
④ 窄屏退化成单列时，`gap` 还会被加成 24px。
网格里本来就有 `gap: 12px`，这条 margin 纯属多余。

**修法**：`:where(.grid, .row) > .card + .card { margin-top: 0 }`（`gap` 负责间距；
`:where()` 保证优先级只有 (0,2,0)、不会反压别的规则）。竖向堆叠（`.col`、以及直接相邻的
`section.card`）不受影响。实测：

```
修前  nav-grid 行1 = 81 / 69 / 69      cols-2 = 203 / 191
修后  nav-grid 行1 = 69 / 69 / 69      cols-2 = 191 / 191
      nav-grid 行2 = 69 / 69 / 69      行的间距 = 69 + 12(gap) ✓
```

**回归项**：`frontend_check.py` 新增第 ④ 节「网格卡片等高等宽」——用 `GRID_PROBE` 量每个
`.grid` 里的卡片，**按"竖直重叠"分行**（不能用 top 相等分行：被 margin 顶下去的卡片会另起
一行，那样就抓不到这个 bug），断言同排等高等宽、且 `.nav-grid` 的 6 张跨排等高。
**反向验证**：把构建产物里的这条规则删掉再跑，探针如实报出 `81 / 69 / 69` 与 `203 / 191`
（证明这条回归项真的抓得住），随后重新 build 还原。现在 45/45。


## 17. 第十七轮：解析清零进度修复 + 搜索搬进侧栏 + 侧栏页签左右切换

用户口径（3 条）：① 「点击解析后，翻译进度清零（实际翻译完成）」；
② 「搜索移到侧栏。侧栏图标支持左右切换（搜索页要支持类似目录页的跳转，并且显示相关词的情况）」；
③ 「翻译完成后只显示图标，不显示『全部 15 页已翻译』」。

### 17.1 解析不再清零翻译进度（用户报的 ①）

**现象**：对一篇**已译完**的论文点一次「解析」，论文库卡片立刻变成
「已解析 + 逐页翻译 0 / 15 + 继续翻译」按钮，而点进阅读器却是「全部 15 页已翻译」。

**根因**：`backend/services/task/handlers.py::_sync_to_mysql`（解析回写 MySQL 的收尾）里有一行
`row.translate_done = 0`。它的本意是"解析换了 block，翻译进度得重来"，但**解析并不删译文**：
它只重建 `BlockRow` / `FormulaRow`，`md/pages.json`（页级译文与对齐行）和页图都还在。
于是这行代码把一个**派生字段**（progress）无条件写坏了。

**修法**：改成"**以磁盘为权威重新数一遍**" —— 复用 `paper_service.translation_stats()`
（`total` = 页图数，`done` = `pages.json` 里已有对齐行的页数），并把 `status` 一并对齐
（译完就该是 `translated`，而不是 `parsed`）：

```python
stats = paper_service.translation_stats(paper_id)
total = stats["total"] or len(result.pages) or row.translate_total
row.translate_total = total
row.translate_done  = stats["done"]
if total and stats["done"] >= total: row.status = "translated"
elif stats["done"] > 0:              row.status = "translating"
```

**历史脏数据**：加了一个 CLI 入口 `python cli/pr.py refresh-progress [paper_id]`，
按磁盘重算所有论文的这两个字段（磁盘是权威，不删任何产物）。已对现有 3 篇跑过一遍：
`attention-is-all-you-need` 从 `parsed 0/15` 修正为 `translated 15/15`，另外两篇本来就对。

**⚠️ 验证踩的坑（值得记）**：任务队列在 **MySQL 里**，两个后端进程会一起抢任务。
我在 8711 上新建 PARSE 任务，结果任务被**用户 8710 上那个跑着旧代码的进程**执行了 ——
现象就是"我明明改了代码，重跑还是 0/15"。判据：新代码里那行诊断日志没出现在 `backend.log`。
所以核验后端改动要么用 **CLI 同步路径**（`cli/pr.py parse/refresh-progress`，
`_run_parse_sync` 直接调同一份 `_sync_to_mysql`，不经过队列），要么先确认只有一个后端在跑。
实测（CLI 同步解析 scanned-demo）：修前 MySQL 行 `parsed 0/2` → 修后 `translated 2/2`，
日志 `解析后按磁盘重算进度 scanned-demo：done=2 total=2`。

### 17.2 搜索搬进侧栏的独立「搜索」页签（用户报的 ② 前半）

- 工具栏那个搜索框删除：工具栏是"视图切换 + 字号 + 翻译 + 更多操作"，塞一个输入框进去主次不分。
- 新增 `frontend/src/components/reader/ReaderSearch.vue`：**上面输入、下面命中列表**，
  成为侧栏的第 2 个页签（目录 / **搜索** / 批注·笔记 / 公式 / 插图 / 术语 / 任务）。
- **"显示相关词"**：每条命中给 `p3 · 原文/译文` + 命中词所在的原句片段，并把命中词本身用
  `<mark>` 标出来（青绿底，不用浏览器默认黄底）。`snippet` 是纯文本，"切开 + 逐段渲染"实现，
  不拼 HTML、不用 `v-html`。
- **"类似目录页的跳转"**：命中条目与目录项同一种交互（整块可点 → `focusRow(id, {page})`）。
  深链 `?q=` 与快捷键 `/` 都改成：先展开侧栏（窄屏是抽屉）、切到「搜索」页签、再聚焦输入框。
- `ReaderToc` 里那段"命中排在目录最前面"**移除了**：搜索已有自己的页签，一份结果放两处既冗余、
  又会在翻目录时被顶开（上一轮那条口径是在"没有搜索页"的前提下提的）。要恢复只是把
  `searchHits` 传回去。

### 17.2.1 用户回看后的三条修正（搜索面板）

1. **冗余文字太多**。上一版面板里有三层文字：输入框 placeholder、状态行（"命中 N 处 / 没有命中 /
   回车搜索原文 + 译文"）、空结果行（"没有匹配的段落"）—— 搜不到时屏幕上同时出现"没有命中"和
   "没有匹配的段落"两句一样的话。现在**只剩输入框 + 结果**：命中条数由页签角标表示（"搜索 22"），
   placeholder 缩成"搜索"，空结果只留一句"没有匹配的段落"，没查询词时面板就是空的。
2. **"搜索不到东西"**。根因不是检索本身，而是**触发时机**：搜索搬进侧栏后仍要求"打完字按回车"，
   打完 "transformer" 没回车时，面板停在上一轮的 0 命中上（旧版状态行还会写"没有命中"），
   看起来就是"搜不到"。改成**边打边搜**（`debounce(…, 220)`，静默、不刷 toast），回车仍然立即搜
   （那条路径保留 toast）。顺带补了一条：正文行是异步来的（payload + SSE 逐页回填），
   `watch(() => allRows.length)` 在数据到位后按当前查询词**静默重跑**，避免"刚打开就搜"永远 0 命中。
   实测：只打字不回车 → 220ms 后 **22 条命中**、命中词高亮 24 处；"trans" → 33 条；
   "zzzqqq" → 0 条且只有一句提示；清空 → 结果与提示都清掉。

### 17.3 侧栏页签：**一行到底、不换行、可左右滚动**（用户报的 ② 后半，改了两版才对齐）

口径演进（值得记下来，我把前两版都做错了）：
1. 原话"侧栏图标支持左右切换" → 我理解成 **←/→ 键切换页签**，加了两条快捷键；
2. 用户："侧栏顶部的图标换行显示，而不是可左右切换的显示方式" → 我理解成 **要换行**，
   把 `.side-tabs` 改成网格（4 + 3 两行）；
3. 用户："不换行显示" + "侧栏上面的图标我的想法是可以左右滚动" → 真实意图是
   **一行排开、放不下就左右滚动**（"左右切换"指的是滚动，不是键盘）。

最终实现：
- `.side-tabs { display: flex; flex-wrap: nowrap; overflow-x: auto; overflow-y: hidden;
  scrollbar-width: none }` + `::-webkit-scrollbar { display: none }`（隐藏滚动条保持干净）；
  `.side-tab { flex: none; min-width: 64px }` —— 7 个页签等宽排在一行。
- **滚得动**：`onSideTabsWheel` 把普通鼠标滚轮的纵向 deltaY 转成横向 `scrollLeft`
  （横向滚动容器默认只吃水平轴，触控板/Shift+滚轮才行；不转的话鼠标用户根本滚不动）。
- **切过去看得见**：`watch(sideTab)` → `scrollIntoView({ block: 'nearest', inline: 'nearest' })`
  （`/` 快捷键切到"搜索"时也会把它滚进可视区）。
- 实测（300px 侧栏）：`overflowX=auto`、`flexWrap=nowrap`、`scrollWidth 470 > clientWidth 300`、
  7 个页签**同一个 y**（1 行）、间距 66px 等宽、`scrollLeft` 可滚到 170（到底）、
  滚轮实测 `0 → 80`；按 ←/→ 不再切换页签。

### 17.4 翻译完成后只留一个对勾（用户报的 ③）

工具栏那条 `全部 N 页已翻译` 文案换成**纯图标徽章**（`.done-badge`：22px 见方 + 内容居中），
完整文案进 `title`。完成态不再占着工具栏的横向空间。

### 17.5 验证

- `vue-tsc` 干净；`npm run build` 通过。
- `frontend_check.py` **45/45**：`reader-搜索` 用例改成断言新结构
  （`side-search` / `ss-hit` / `ss-snippet` / 清空搜索，且 `search-compact` **必须不存在**）。
- headless 探针（reader，`?q=attention`）：工具栏无搜索框；侧栏 7 个页签；
  命中 50 条、`<mark>` 64 处（首条标记词 `Attention`）；完成徽章 `text="" / hasSvg=true /
  title="全部 15 页已翻译"`。
- 侧栏页签排布探针：`.side-tabs` = flex / nowrap / `overflowX=auto` / 470 > 300 可滚 /
  **7 个页签同一行**、等宽 66px；滚轮把 `scrollLeft` 从 0 推到 80；方向键**不切换**页签。
- 边打边搜探针：只打字不回车 → 220ms 后 22 条命中、`<mark>` 24 处；"trans" 33 条；
  "zzzqqq" 0 条 + 一句提示；清空后结果与提示都为空。
- `pytest tests -q` 全绿（跳过项同往常）。
- CLI `refresh-progress`：3 篇论文进度与磁盘一致（修正 0 篇）。

### 17.6 首页文案改主题色（用户口径）

「首页文字以主题色显示」：`.home-hero h1`（PaperMind）用实心 `--accent-text`，副标题用
`color-mix(in srgb, var(--accent-text) 68%, var(--text-dim))`（同一支色的浅一档，保住层级），
`<strong>新的标签页</strong>` 也走强调色。**只改首页自己的文案**（hero 区）—— 导航卡标题、
章节标题、论文标题保持中性色：那些是"内容/入口"，涂成强调色会变成一整页链接色。
想再扩大范围就是在这个文件里加一行。

### 17.7 侧栏页签条：左右两端按需出现圆形箭头 + 排得更紧凑（用户口径）

用户口径：「侧栏图标列当左侧或者右侧有内容时显示圆形的向左或向右按钮，其他的图标排列稍微紧凑一些」。

- 外层加 `.side-tabs-wrap`（`position: relative`）当定位参照，两端各一枚 **20px 圆形按钮**
  （`.side-scroll-btn`，`border-radius: 50%` + 描边 + 柔光投影），**浮在滚动条两端、不占布局** ——
  所以"出现/消失"不会让页签跳动。
- 显隐判据（`tabsScroll`）：`scrollLeft > 1` → 左有内容；`scrollLeft + clientWidth < scrollWidth - 1`
  → 右有内容。同步时机：`@scroll`（用户拖动/触控板）、点箭头后**立即**同步、滚轮后同步、
  `ResizeObserver` 盯容器宽度（侧栏开合动画）、`watch(SIDE_TABS)` 盯角标数字（角标变宽会改 scrollWidth）。
- **紧凑**：`.side-tab` 的 `min-width` 64 → 56px、内边距 `4px 6px 3px` → `3px 4px 2px`、
  图标与文字的 gap 2 → 1px、`.side-label` 字号 10.5 → 10px。7 个页签的 scrollWidth 470 → **414**
  （300px 侧栏下少藏一点）。
- 还加了 `scroll-padding: 0 24px`：`scrollIntoView` 落位时不会把当前页签藏在圆按钮底下。
- **踩坑（重要）**：`scrollBy({ behavior: 'smooth' })`（以及 CSS `scroll-behavior: smooth`）
  的补间由合成器推进，**无头 + 虚拟时间下完全不走** —— 实测"点了按钮纹丝不动"，
  而这种失败在自检里看不出来（真实浏览器还会受系统"减少动效"影响）。改成**瞬时**滚动
  （直接赋 `scrollLeft`），行为确定、也验证得到：翻一屏本来就很短，瞬时更跟手。
- 实测探针：初始 `scrollLeft 0` → 只出现**右**按钮；点右 → `scrollLeft 114`（到底）→ 只出现**左**按钮；
  点左 → 回到 0 → 只出现**右**按钮；滚轮到中间（60）→ **左右都出现**；按钮 20px / `border-radius: 50%`；
  7 个页签仍然**同一行**（`rows = 1`）、等宽 56px。

### 17.8 「首页」标签**任何时候都特殊显示**（用户口径，修正 16.2）

16.2 里因为"首页看起来一直高亮"的反馈，把常驻首页标签的填充胶囊整个去掉了（只在落在首页时
把图标+文字染成强调色）。用户现在要求：**「即使在没选中时也能特殊显示」** —— 也就是说它平时
（在论文库/阅读器里时）也不该是一枚普通灰字标签。

改法（`components/TabBar.vue`，纯 CSS）：

- **平时**：`background: var(--accent-soft)` + `color: var(--accent-text)` + `font-weight: 600`
  → 一枚淡青绿"回首页"小胶囊；
- **落在首页**：同一块底色再加 `inset 0 0 0 1px`（强调色 45%）内描边，"你在这里"仍然看得出来；
- hover：底色加深到 `color-mix(in srgb, var(--accent) 20%, transparent)`。

⚠️ 关键差别：底色**只用强调色系**，而内容标签激活时用的是**中性灰胶囊**（`--bg`）——
两者不会混，所以不会重演"首页看起来像是被选中的标签"那个反馈。

实测（headless 量 computed style）：

```
在 /app/library（首页没选中）：首页 tab → bg rgb(230,243,245)=accent-soft / color rgb(10,90,102) /
                              weight 600 / 无内描边；论文库(active) → bg rgb(242,244,245)=--bg
在 /app/home（首页选中）  ：首页 tab → 同底色 + inset 内描边；论文库 → 透明 / 灰字
```

`vue-tsc` 干净、`frontend_check.py` 45/45（`tab active pinned` 的两条断言仍然成立）。

### 17.9 搜索范围：原文 / 译文（用户口径）

用户口径：「搜索栏下实现选择搜索范围（原文还是译文），风格要适配」。

- `ReaderSearch.vue` 在输入框**下面**加一行分段控件：**全部 / 原文 / 译文**（默认"全部"，
  与改动前的行为一致）：`<div class="seg ss-scope">` —— 直接用全站那套 `.seg` 胶囊分段
  （与工具栏"原文 / 译文 / 对照"、正文字号同一个外观语言：pill、26px 按钮、激活态白底+强调色+加粗），
  只额外让 `flex: 1` 三段等分、拉满侧栏宽度。实测：分段与输入框**同宽 284px、同高 32px**、
  圆角 999px、按钮 26px。
- 范围状态放在视图里（`searchScope = ref<SearchScope>('all')`），与 `searchQuery` 一样走
  `v-model:scope`；`runSearch()` 按范围**只在该侧文本里找**（`inEn` / `inZh` 两行判断），
  命中的 `side` 自然只会是所选那侧。
- 改范围立刻**静默重跑**（`watch(searchScope)`），不然结果和分段控件对不上；
  深链也支持 `?scope=all|en|zh`（与 `?q=` 一起用，便于分享/自检）。
- 实测（headless，arXiv 那篇）："注意力" → 全部 46 条（全是"译文"）/ **原文 0 条** / 译文 46 条；
  "attention" → 译文 4 条（中文里保留的英文术语）/ 原文 50 条 / 全部 50 条；
  每条命中的侧标签始终只出现所选那侧。
- `vue-tsc` 干净、`frontend_check.py` 45/45。

### 17.10 搜索范围文字居中 + 已译论文的「重新翻译」收进二级菜单（用户口径）

**① 「搜索按钮的文字没有居中显示」**：`.ss-scope button { flex: 1 }` 把三段拉开后，文字贴在
格子左边 —— 根因是 base.css 的 `.seg button` 只写了 `display: inline-flex`、**没有
`justify-content`**（原来三段是 content 宽，看不出来）。补一行 `justify-content: center`。
实测三段按钮：`justifyContent=center`，按钮中心 57/150/243 与文字中心**完全一致**（delta=0）。

**② 论文库：已译完的论文，卡片上不再有「重新翻译」一级按钮**（用户口径：放到二级界面）：

- 卡片 footer 的流水线按钮加 `v-if="stage(p) !== 'translated'"`：已译完只留「阅读」+
  「⋯」；未解析 / 翻译中 / 暂停态照旧给「翻译 / 继续翻译 / 暂停翻译」（那些是"继续干活"，
  不是"重跑"，留在明面上）。
- `rowMenu(p)` 在 `stage(p) === 'translated'` 时**在最前面**插入 `{ key: 'retranslate',
  label: '重新翻译', icon: 'refresh' }`；`onRowAction` 走新的 `askRetranslate(p)`。
- **确认用 HTML 原生 `window.confirm`**（用户明确要求）：文案带上论文名与页数 ——
  「重新翻译《…》？会忽略已有的 22 页译文，从第 1 页重跑（现有对齐结果会被覆盖）。」。
  删除那条路仍然用站内 `ConfirmModal`（不可逆、要显示影响范围），两者分工写在注释里。

实测（headless 探针，3 篇论文全部已译完）：卡片一级按钮只有 `["阅读"]`；「⋯」菜单第一项是
「重新翻译」，其后是重新解析 / 建立索引 / 纳入知识库 / 编辑元数据 / 删除 / 彻底删除；
点「重新翻译」→ `window.confirm` 被调用 **1 次**且带上面那段文案；返回 false（取消）后不会建任务。
`vue-tsc` 干净、`frontend_check.py` 45/45。

### 17.11 正文 / Markdown 里的插图：不加暗裁、补白边、限制过大（用户口径）

用户口径：「① markdown 侧的图片希望能不自主上下裁剪（因为点击查看和实际显示的上下范围不太一致），
完整显示，保留圆角裁剪；② 部分情况下图片显示过大的问题」。

**先查清事实**（headless 量盒模型）：`getBoundingClientRect` 的宽高比与图片自然宽高比**完全一致**
（0.68/0.68、1.862/1.866、1.871/1.875），滚动祖先只有 `.reader-content` 这类正当的滚动容器 ——
CSS 里**没有**任何裁切。真正"上下范围不一致"的原因是两张**不同的图**：
正文里是 `region/N/0`（后端按墨迹裁到边、四周没有留白），点开看大图是 `region/N/0?card=1`
（"图+图注"卡片，`figure_card_png` 合成时四周留了 12px 白边）。于是正文那张贴着边看就像
"被上下裁掉了"。

**改法（只动渲染，不重新裁图）**：

1. `.row-figure img` / `.md-doc img` 加 `padding: 6px; box-sizing: border-box;`（白底 + 圆角照旧）——
   正文那张图也有一圈白边，和放大卡片的留白观感一致，四周不再"顶着边"。
2. 同时给 `max-width` 与 `max-height`（正文 `min(74vh, 840px)`、Markdown 视图 `min(62vh, 780px)`）：
   浏览器会**保持宽高比**缩到两个约束都满足为止 —— 不裁、不变形，又高又窄的整页插图
   （attention 的 Figure 1 是 1834×2699）不再按列宽铺满、高到要滚好几屏。
   实测：该图从 780×1147 变成 457×666，`content` 比例 0.679 vs 自然 0.680（无变形）；
   横图仍是 780×425（比例 1.864 vs 1.866）。
3. **顺手修掉一个自己踩的坑**：`.row-figure img` 里的 `max-width: min(100%, 780px)` 的 `100%`
   参照的是 `.row-figure` 的宽度，而它当时只是两列网格里的一个 shrink-to-fit 格子（`grid-column`
   解析成 `auto`）→ 百分比遇上"由内容决定的宽度"就是循环依赖，实测同一张图一会 780px、
   一会 483px（"部分情况下显示过大/过小"这类飘忽现象就是这么来的）。现在 `.row-figure`
   显式 `grid-column: 1 / -1` + `width: 100%`，`100%` 有了确定参照（实测 1150px，稳定）。
   （教训：**百分比尺寸不能放在 shrink-to-fit 的容器里当唯一约束**。）

`vue-tsc` 干净、`frontend_check.py` 45/45；截图确认插图带白边、圆角、完整可见。

### 17.12 插图统一用「图 + 图注」卡片（口径返工后的最终版）

**过程（记下来免得再翻烧饼）**：

1. 用户："markdown 侧的图片不要上下裁剪（点击查看和论文里的上下范围不一致）" → 我把**放大**改成
   与正文同源的**纯图**（`region/N/0`）；
2. 用户："选错图片了，这是裁剪不好的" → 纯图那张才是裁得不好的那张（贴边、少了图注）→
   **最终统一到卡片**（`?card=1`）：正文插图、侧栏缩略图、点开看大图**三处同源**，
   范围完全一致（实测三处 src 都是 `...region/3/0?card=1`，自然尺寸都 1834×2806）。

**先量清楚的事实**（免得把"观感差异"当成裁剪 bug）：把 5 张图的纯图与卡片的**逐行墨迹**比了一遍 ——
墨迹范围逐行一致（差 0~34 行，是重采样误差），卡片只是多了图注与四周留白；也就是说
**纯图并没有丢内容**，差别在"要不要带图注、边距多大"。另外 `INK_PAD=1.5pt`（≈600dpi 下 12.5px）
与卡片的 `margin=12px` 基本相等，所以两者的留白本来就差不多。

**改动**：

- 前端 `ReaderPageRows.vue`：新增 `figureSrc()`，正文插图与 `zoomUrl()` 都取 `card_url || url`；
- 前端 `ReaderSidePanel.vue`：放大取 `card_url`（缩略图用后端的 `image_url`，现已同卡片）；
- 后端 `reader_service.figures_payload`：`image_url` 也返回卡片（原先改成了纯图，现回滚），
  docstring 写明"三处必须同源，纯图那张是裁得不好的那张"。

**代价（已在交付里说明）**：正文里那一行图注（原文 + 译文）与图片里烘进去的图注会各出现一次 ——
这是"和点击查看完全一致"的直接结果；若不想重复，只需把正文插图改回 `m.url`（一行）。

`vue-tsc` 干净、`frontend_check.py` 45/45；截图确认正文插图已带图注与留白、圆角照旧。


### 17.13 侧栏术语页：只读视图 + 按需编辑（附两处真 bug）

用户口径：*优化侧栏术语页的显示，仅在编辑时才显示编辑框，其他时候为正常样式内容（符合页面风格）*。

**改法**（`ReaderSidePanel.vue` + `ReaderView.vue`）：

- 术语页默认**只读**：领域/风格/要点/保持英文不译渲染成"小标题 + 正文"的排版
  （`.brief-k` 10.5px 灰字 + `.brief-v` 12.5px 正文，要点是 `<ul>`，保持英文用 `.badge` 芯片），
  下面接术语表逐行；工具栏只有「编辑」「重新生成」；
- 点「编辑」才出现 5 个输入框（领域/风格/要点/保持英文不译/术语表），工具栏换成「取消」「保存」；
- 换页签自动退出编辑态（免得切回来还停在半改一半的输入框上）；
- 「取消」= 丢弃本地改动（emit `reload-brief` → 父组件用服务端那份**重新灌草稿**）；保存失败也回灌，
  不让界面显示没存上的内容（`syncBriefDraft()` 抽成函数，`watch(brief)` 与两处回灌共用）。

**顺带发现的真 bug（不修就是"页面坏了"）**：

1. **术语表一直是空的**。视图只列 IR 术语表（`/glossary` → `ir.sqlite` 的 `term` 表），
   而简报里那 20 条"统一译法"是另一份 —— 于是编辑框里明明有 20 条、下面却写"没有术语"、
   页签也没有角标。现在视图 = 简报统一译法 ∪ IR 术语表（按英文去重，`termRows`），
   页签角标 `termCount` 同样按并集算（实测 attention 页签显示"术语 20"、列表 20 行）。
2. **`keep_as_is` 谁都看不到也改不了**。它参与每次翻译的 prompt（`prompt_block()` 里的
   "保持英文不译"），但界面上从来没有入口。现在它是编辑态的第 4 个输入框 + 只读态的芯片行，
   `/brief` 的 `apply` 也支持写它（`keep_as_is`）。
3. 顺手修掉 `/brief` `apply` 的"真值判断"：以前 `if apply.get("domain")`，**清空某个字段会被静默丢掉**
   （用户以为改了其实没改），现在一律 `is not None` 判存在。

探针实测（headless Edge）：只读态 `input/textarea` 数量 **0**、4 个标题栏 + 20 行术语；
点「编辑」→ 5 个输入框、按钮变「取消/保存」；点「取消」→ 回到只读、术语仍在。
另在 `scripts/frontend_check.py` 里加了常驻回归用例 `reader-术语只读`（负向断言编辑态独有文案），
共 **46/46**。

### 17.14 论文元数据自动填写（PDF 离线抽取 + 模型与术语同一次产出）

用户口径：*论文元数据我想要么从论文获取自动填写，要么模型翻译后填写（和术语一同生成）*。
背景是论文库里「会议 / 期刊」「年份」「摘要」「作者」长期为空，只能手打。

**来源 ①：新模块 `backend/parsing/metadata.py`（纯离线启发式，不调模型）**

- 标题/作者：PDF 元数据 → 首页最大字号行（作者从"标题→Abstract"之间的人名行取，
  排除机构/邮箱/页脚；判不准就留空 —— 作者写错比留空更糟）；
- 会议/期刊 + 年份：按可信度排序的规则表（`Published as a conference paper at …` >
  `… Conference on … (NIPS 2017)` > `Proceedings of …` > 会议缩写+年份 > IEEE/ACM 期刊 > arXiv 兜底），
  年份回退链：会议串里的年份 → 版权行/`arXiv:1706.` 编号 → PDF `creationDate`；
- 摘要：按 **PyMuPDF 的块**切（标题块 + 紧随其后的那一段，遇到章节标题或"上一块已把话说完整"就停），
  再退回纯文本行扫描；连字（ﬁ/ﬂ）还原、跨行连字符复原，截断到 MySQL 的 2000 字符。

**实测踩到的坑（都修了）**：

- **竖排 arXiv 水印被当成标题**：`arXiv:1706.03762v5 [cs.CL] 6 Dec 2017` 字号 20pt，
  比真标题 17.2pt 还大，且落在页面上部 —— attention 那篇论文的标题在库里**一直**显示成这串水印。
  现在 `_page_lines()` 直接丢掉旋转文本（`line["dir"]` 非水平），标题候选再排除页边（x < 5% 或 > 95%）
  与 `arXiv/doi/copyright` 之类噪声行。修复后这篇的标题回到 `Attention Is All You Need`。
- **摘要吞进引言**：ICLR 模板的引言没有小标题（TurboQuant 的 `1 INTRODUCTION` 在后一页），
  只按"下一节标题"停会把整段引言吃进来。判据改成"摘要一般**只有一段**"：
  上一块以句末标点收尾就不再往后吃（实测两篇的摘要长度 1138 / 1686 字符，都是原文那一段）。
- **模型交回 Python 单引号字典**：`structured()` 把 schema 用 f-string 插进 system prompt，
  出来的是 Python 字典的**单引号**形式，模型照着抄成 `{'venue': '', 'year': 0, …}`，
  `json.loads` 当场失败（一次真实调用就中招，整次结构化输出作废）。现在 schema 用 `json_dumps()` 展开，
  并加 `_loads_loose()`（`ast.literal_eval` 兜底）—— 这条对所有结构化调用（简报/翻译/视觉读页）都有效。

**来源 ②：模型 —— 与术语同一次调用（零额外成本）**

- `BRIEF_SCHEMA` 里嵌了 `metadata_fill.META_SCHEMA`（venue/year/tags/abstract_zh），
  `BRIEF_SYSTEM` 加了第 5 条要求 → 生成术语表的**那一次**调用顺带产出元数据；
- `DocumentBrief.meta` 落盘（可复查），但**刻意不进 `prompt_block()`**：它不是翻译指令，
  进前缀只会让"人工改元数据"把 provider 的前缀缓存打掉（简报前缀必须字节级稳定，pytest 里有断言）；
- `generate_brief(apply_meta=True)` 在保存简报后把 meta **只填空字段**地写回论文行；
- 读不出来的场景（扫描件、首页没写会议）另有 `metadata_fill.from_model()`，走 `/metadata/auto?model=true`。

**写回策略**：`apply_fills(..., overwrite=False)` **只填空字段** —— 元数据没有"来源"标记，
覆盖掉用户手改过的值是不可逆的丢失（pytest 锁了这条）。标题单独判"明显不对"才改：
文件名/`arXiv:`/`Microsoft Word` 前缀才算错。年份做合理性校验（1950 ~ 当年+1），
模型给的"一句话假摘要"（< 60 字）直接丢掉。

**入口**：导入（`import_pdf`）与解析（`handlers._persist_parse`）**自动**填一次；
论文库「编辑元数据」新增两个按钮 ——「从论文识别」（离线、不花钱）与「模型补全」（PDF 里读不出来时问模型）；
CLI 有 `pr.py metadata-auto [paper_id] [--model] [--overwrite]`（批量补历史数据）。

**实测**（对库里 3 篇跑 `pr.py metadata-auto`）：

| 论文 | 标题 | 作者 | 会议/年份 | 摘要 |
|---|---|---|---|---|
| attention-is-all-you-need | arXiv 水印 → `Attention Is All You Need` | 8 位（含 Ł/角标） | NeurIPS 2017 / 2017 | 1138 字（英文原文） |
| turboquant-online-vector-iclr-26 | 不变（本来就对） | 4 位 | ICLR 2026 / 2026 | 1686 字 |
| scanned-demo | 无文本层 → 只能靠模型（`--model` 补到 6 个中文标签） | — | — | — |

**验证**：`pytest tests -q` 全绿（新增 `tests/test_metadata.py` 8 项：竖排水印不当标题、摘要不吃引言、
只填空字段、脏值过滤、单引号 JSON、简报同调用写回元数据）；`vue-tsc` 干净；
`frontend_check.py` **46/46**；浏览器探针验证「编辑元数据」弹窗里两个新按钮工作正常
（点「从论文识别」后回执 toast = 后端 envelope 文案）。


### 17.15 触摸原生灰色遮罩 + 多标签页切论文（实例复用导致数据串台）

用户口径：*1. 移除触摸时的浏览器原生遮罩显示（就是浏览器触摸事件的灰色遮罩）2. 解决多标签页切换论文失效的问题*。

**① 触摸灰色遮罩 = `-webkit-tap-highlight-color`**

那是 UA 自己叠在"被点的元素"上的一层灰/蓝半透明方块（Chrome 默认 `rgba(0,0,0,0.18)`）：
圆角与尺寸都跟本站的按钮语言不一致，点一下闪一下。本站的 hover / `:active`
（`.btn:active` 会下沉 1px）已经把按下反馈表达清楚了，于是全局关掉：

```css
* { -webkit-tap-highlight-color: transparent; }
```

- 必须写在 `*` 上：**该属性不继承**，只写 `body` 管不到按钮与链接；
- 只关高亮，**不动** `touch-action` / `user-select` —— 正文还要划词、长按选择、复制。
- 探针实测（`getComputedStyle`）：`.btn` 与 `body` 都是 `rgba(0, 0, 0, 0)`。

**② 多标签页切论文：`RouterView` 没有 `:key`，组件实例被复用**

根因（两条一起才解释得清现象）：

1. `/reader/A` 切 `/reader/B` 只是换 `route.params`，vue-router **复用同一个 ReaderView 实例**，
   `onMounted` 里那套加载逻辑不会再跑；
2. 而这个实例里的数据是**挂载那篇论文**的，`paperId` 却是 `route.params.paperId` 的 computed ——
   于是切到 B 的标签后：SSE 流还是 A 的（挂载时建的），而 4s 轮询 `pollTranslations()` 拿的是
   **B 的页行**并 `applyPage()` 打进当前文档 → 两篇的行混在一起。

实测复现（无头浏览器，A=attention 123 行 / B=turboquant 266 行）：

| 步骤 | 修复前 | 修复后 |
|---|---|---|
| 打开 A | 123 行 | 123 行 |
| 点 B 标签 | 266 行（混入了 B 的行） | 266 行（B 自己的） |
| 点回 A 标签 | **211 行**（A 的行被 B 覆盖/追加了一部分） | **123 行** |
| 再点 B | — | 266 行（不再累积） |

**改法**：

- `App.vue`：`<RouterView :key="routeKey(route)" />` —— key = **标签键**（路由名 + 参数），
  切论文 = 换实例 = 走一遍完整加载，跟"新开一个标签"的语义一致；只按 `name+params` 计算，
  所以**同一篇论文内**切侧栏页签 / 跳到某一行（query 变化）不会重建整个阅读器。
- `ReaderView.vue`：新增 `instancePaperId`（挂载时固定），**异步落盘一律用它** ——
  `saveProgressNow()`、`pollTranslations()`、`fetchTasks()`、SSE URL。
  为什么必须：切论文时**路由先变、旧实例后卸载**，`onUnmounted` 里那次
  `saveProgressNow()` 读 `route.params` 已经是新论文了，会把上一篇的滚动位置写进**下一篇**的进度行
  （这个坑在旧代码里也存在，只是被"实例复用"掩盖着）。
- `stores/tabs.ts`：`AppTab.query` 之前是**摆设**（永远 `{}`）。现在 `activateRoute` 记住该位置
  的 query、`tabLocation` 还原它 —— 切走再切回来能回到上次的 `?side=terms&row=…`（深链状态不丢）；
  而"显式重新打开"（论文库点「阅读」）算从头看，`openTab` 会先把该标签记住的 query 清掉。

**验证**：`frontend_check.py` 新增第 ⑤ 节并做成**常驻回归**（`TABS_PROBE`：预置两个论文标签 →
来回点 4 次，断言"每个标签每次的正文行数一致"；它对串台极敏感，且顺带断言触摸高亮为透明）
→ **51/51**；`vue-tsc` 干净；`pytest tests -q` 全绿。


### 17.16 搜索全量高亮 + 命中块闪烁；Markdown 视图留白/去分割线/全屏/按版本下载

用户口径三条：*1. 搜索实现对所有命中文字高亮，点击的块（搜索页里点的那个 markdown 块）短暂闪烁提醒；
2. markdown 每一页顶部略微加点间距（与论文顶部留白相似）并移除分割线；
3. Markdown 视图右上角加全屏按钮，底部下载按左上角选中的版本（中英对照 / 纯原文 / 纯译文）*。

**① 搜索：全量高亮 + 跳过去闪一下**

- **全量高亮**（`frontend/src/utils/searchMarks.ts`）：把命中词在**已渲染的 DOM** 上包成
  `<mark class="srch">`（浏览器 Ctrl+F 那种整篇一起亮），而不是只标当前这一条。
  为什么走 DOM 而不是复用用户高亮的 Markdown 占位符方案（`htmlWithHighlights`）：
  命中词是**用户随手输入的**，直接替换 Markdown 源会撞上下标/强调/链接语法，也会破坏 KaTeX 的 HTML；
  DOM 方案只在文本节点外套一层，不碰文档结构。跳过 `script/style/svg/katex` 子树。
  `ReaderRow.vue` 在自己的 `.rr-body` 上 `flush: 'post'` 重画（`v-html` 重设时标记本来就被抹掉，
  随后再包一遍，所以不需要额外同步）。配色用**主题色**（黄/绿/蓝/粉已被用户高亮占用），
  实测 attention 搜 "attention" 命中 **98 处**全部高亮；点「清空搜索」后标记归零。
- **闪烁提醒**：`focusRow(..., { flash: true })` → 整行加 `.flash`，`::before` 覆盖层 1.5s 淡出
  （`ReaderPageRows.vue`）。入口是"跳到这里"的三类动作：搜索命中、目录、批注/笔记定位 ——
  而滚动恢复、切视图模式这些**不闪**（只闪"用户主动跳"的动作）。
  两个细节：底色默认就**可见**（不靠动画从 0 淡入），万一动画没跑（无头浏览器会冻结 CSS 动画）
  也照样亮，1.6s 后由 JS 摘类；同一行连点两次时先摘类再挂上，动画才会重播。
  实测：点命中 → `p001-00` 带 `.flash`（不透明度 0.95）→ 2.2s 后自动消失；连点两次仍亮。

**② Markdown 视图：顶部留白 + 去掉分割线**

- `.md-doc` 顶部内边距 4px → **16px**（与论文正文顶部留白同一种观感），并**去掉 `border-top`**；
- 上下对照原来用 `---` 连接原文/译文（渲染成一条横线）→ 改成空行，分割线消失；
- 同时把 `hr` 本身改成**纯间距**（`border:0; height:18px; background:none`）：
  模型偶尔会在正文里自己写 `---`，不能让它又冒出一条线（实测注入 `hr`：高 18px、边框 0、背景透明）。
- 现网 `pages.json` 里没有任何 `---`，所以"看不出来"这一点是用注入元素量的，不是靠肉眼。

**③ 全屏 + 按版本下载**

- `Modal.vue` 新增 `full` 属性与 `#tools` 插槽（标题栏右侧的附加工具）。
  `full` = 遮罩去内边距、弹窗去圆角/描边/最大宽高 → 铺满视口。**用 CSS 铺满而不是 Fullscreen API**：
  不弹浏览器权限提示、Esc/关闭逻辑不用另写一套（遮罩 z-index 本来就在标签条之上）。
  实测：点「全屏」→ `.modal.wide.full`、弹窗盒子 = 视口（1500×1000）、`.md-doc` 上限
  `calc(100vh - 190px)`=810px、内容高度 680 → 715；按钮 title 变「退出全屏」，再点回到 `680`。
  关闭弹窗时顺手退出全屏（下次打开是常规尺寸，不"记住"上次状态）。
- **下载跟着左上角的分段控件走**：新增 `markdownFor(mode)` 按模式拼整篇
  （`## 第 N 页` + 原文/译文，对照版带 `### 原文 / ### 译文`），文件名后缀
  `-bilingual/-original/-translation.md`，按钮文案同步成「下载中英对照/纯原文/纯译文 Markdown」。
  以前无论看哪一版，下载的都是同一份双语文件（`mdView.combined`），现在"下载什么 = 你正在看什么"。
  实测三种模式的按钮文案与文件名后缀都对得上。

**验证**：`frontend_check.py` 加了两条常驻用例（`reader-搜索` 增加 `class="srch"` 断言、
新增 `reader-Markdown视图`）→ **52/52**；`vue-tsc` 干净；`pytest tests -q` 全绿；
另用无头探针逐项量了上面的数值（标记数、闪烁类与不透明度、padding/hr/全屏尺寸/下载文案）。


### 17.17 修正：留白/去分割线是**正常阅读模式**；Markdown 视图改为整篇一次铺开

上一轮我把「顶部留白 + 移除分割线」做到了 Markdown 弹窗上（那是**误读**）。用户澄清：
*指的是正常模式下（正常模式里有分割线），并且右侧的 markdown 文本顶部留一点白，
与左侧论文对比不显那么突兀*。同时把 Markdown 视图改成不翻页。

**① 正常阅读模式：去掉页头横线 + 右侧正文顶部留白**

- `.pg-head`（"第 N 页"那行）去掉 `border-bottom` —— 那正是用户说的那根分割线。
  页与页之间改由间距分开（`.pg { margin-bottom: 18px → 26px }`），页码只剩一枚很淡的标注；
- **只给内容列**加 `padding-top: 18px`（`:where(.pg-rows, .col-rows)`）：
  左栏是论文页图，页图自带纸面页边距；右栏原来贴着页头就开始，两栏顶部一高一低显得突兀。
  ⚠️ 第一版我加在 `.pg-pair`（左右两列的父容器）上，等于**把页图也一起推下去**，抵消掉了对比 ——
  实测三页的 `firstRowOffset` 都是 18px、页图不动，才是要的效果（页图列不参与这条规则）。
- 正文里模型自己写的 `---` 也不画线（`.rr-body :deep(hr) { border:0; height:12px }`），
  与 Markdown 视图同一口径：**只留空白，不留横线**。

**② Markdown 视图：移除翻页，整篇一次渲染（保留"从该区域开始"）**

- 翻页器（上一页/下一页/第 N 页 / M）整组删掉；`mdFullHtml` 把**所有页**拼成一个 HTML，
  每页一个 `<section data-md-page="N">` 锚点 + 一枚淡页码（页与页之间不再有横线）；
- **打开时滚到当前阅读页那一段**（"保留从该区域开始的功能"）：`scrollMdToPage(page)` 用
  "相对位移"算（`rect.top - (box.top - box.scrollTop)`，不依赖 offsetParent），
  再延迟 260ms 校准一次（图片是异步加载的，会把后面的内容往下顶）；
- 右上角保留一个「当前页」按钮：任何时候点一下回到当前阅读的那一页（替代原翻页器的定位作用）；
- 切换 上下对照/原文/译文 后仍停在**同一页那一段**（内容整块替换会丢失滚动位置，所以 watch 里重新定位）。

实测（turboquant，22 页，`?md=1&mdpage=4`）：`.md-page` 22 段（1…22）、无翻页器、
打开即 `data-md-page="4"` 的段落顶端对齐内容区（偏移 6px）、点「当前页」后回到同一位置、
切「纯译文」后仍停在第 4 页那一段且下载文案变成「下载纯译文 Markdown」。

**验证**：`frontend_check.py` 的 `reader-Markdown视图` 用例改成断言 `data-md-page="3"`（整篇渲染）
且 `title="上一页" / title="下一页"` **必须不存在**（翻页器已删）→ **52/52**；
`vue-tsc` 干净；`pytest tests -q` 全绿。


### 17.18 插图放大：弹窗标题显示**图片标题**（图注），不再是"插图"两个字

用户口径：*点击放大图片时，顶部不显示文字"插图"，而应该显示图片标题（markdown 有图片标题信息）*。

**改法**：放大事件从"只传 URL"变成传一个 `ZoomTarget`（`api/types.ts`）：
`{ url, caption, page, number }` —— 图注本来就在 Markdown 产物里（`media.caption`，
形如 `Figure 3: Evaluation of …`），只是以前没往上带。

- `ReaderPageRows.vue`：`zoomTarget(m, g.page)`（三处：页图模式的行、文本对照的行、页尾兜底图）；
- `ReaderSidePanel.vue`：插图页签的缩略图同样带上 `caption/page/number`；
- `ReaderView.vue`：`zoomImage` 从字符串改成 `ZoomTarget | null`，新增 `zoomTitle` ——
  **有图注就显示图注**（图注本身通常已含"Figure N:"，不再另加前缀），
  没有图注才退回"插图 · 第 N 页"。

**顺带修的排版问题**：图注一句话常有几十上百字（用户截图那张 Figure 3 有 300+ 字），
标题栏是 flex 一行，长标题会把右侧的关闭按钮顶出弹窗。`Modal.vue` 给标题加了
`.modal-title { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap }`
（`min-width: 0` 是关键：flex 项默认不肯收缩），并挂原生 `title` 看全文。

实测：正文插图与侧栏缩略图点开，标题都是那张图的图注（`Figure 1: Distribution of Inner
Product error for TURBOQUANT…`）；塞一条 313 字的超长图注 → 标题在 1024px 处省略号截断
（全文 2146px）、`text-overflow: ellipsis` 生效、标题栏 scrollWidth == clientWidth（不溢出）、
关闭按钮仍在弹窗内。

**验证**：`frontend_check.py` 新增第 ⑥ 节常驻用例（点开第一张插图 → 断言标题来自图注、
有 `<img>`、超长图注一行截断、标题栏不溢出）→ 两篇论文各跑一遍都是 **56/56**；
`vue-tsc` 干净；`pytest tests -q` 全绿。


### 17.19 划词高亮：Markdown 视图（原文 / 译文）也能标，且**两侧分开存**

用户口径：*实现 markdown（原文，译文）的高亮效果（即菜单上提及的高亮效果），分开存储*。

**先说清楚原来的两个问题**（都属于"高亮根本没实现好"）：

1. **一侧高亮会画到另一侧**：一行对照的两侧共用同一个 `block_id`，前端只按 `block_id` 取高亮、
   再把 `start/end` 套到文本上 —— 原文上标的偏移量会原样画到译文上（中英文字数完全对不齐）。
2. **颜色只有黄色**：`htmlWithHighlights` 产出的是 `class="hl yellow"`，而配色只写在**笔记页自己的
   scoped 样式**里（`.hl.yellow` 等），正文里的 `mark.hl` 没有任何样式 → 不管选绿/蓝/粉，
   渲染出来都是浏览器默认的黄色（用户"选了没反应"）。

**改法**

- 表结构：`highlight` 加两列 —— `side`（en/zh：偏移量相对哪一侧的文本）与
  `scope`（row/markdown：在正文行标的还是在 Markdown 视图标的）。默认 `en` / `row`。
  `session.ensure_columns()` 负责给**历史库补列**（`create_all` 不会 ALTER 既有表），
  `init_db()` 里调用 —— 用户的库在我这边已经补好了（实测 `added: highlight.side, highlight.scope`）。
- `annotation_service.create_highlight` 校验并落库这两列（非法值回落 en/row），`reader_service`
  把 side/scope 一起发给前端；`_anchor_page` 认识 `md:p<页号>` 这种锚点。
- 前端 `ReaderRow`：高亮过滤条件加上 `side === 本侧`（老数据没有 side 时按 en 处理）。
- **Markdown 视图**：每页按侧包一层 `<div data-md-side="en|zh">`；新增 `utils/domMarks.ts`
  （把原 `searchMarks.ts` 并进来）提供**按字符偏移量**画/摘标记、以及"选区 → 渲染后文字偏移量"
  的换算。锚点用 `block_id = "md:p<页号>"` + `side` + 偏移量，**偏移量的口径是"渲染后的纯文本"**
  （Markdown 源里的偏移对不上看到的文字）；偏移量对不上时按"去空白后相同"判断、
  再用选中文字精确找一次，都找不到就跳过（宁可少画一条，也不画错位置）。
- 弹窗里也能划词：`SelectionMenu` 新增 `allowNote` —— Markdown 视图**不给「笔记」**，
  因为笔记靠 `block_id` 定位回正文，而 `md:p3` 这种锚点在正文里不存在（存下来就是一条点不动的死笔记）。
  高亮支持"点一下删掉"（弹窗传送到 body，接了独立的 click 处理器）。
- 配色统一到 `base.css`（`mark.hl.<color>`，笔记页里那份重复的删掉）：正文、Markdown 视图、笔记页共用一套。

**实测**（无头浏览器）

| 用例 | 结果 |
|---|---|
| 正文行：原文侧标绿 | 原文 1 条 / 译文 **0** 条（旧代码会两边各 1 条） |
| 同一行的译文侧再标蓝 | 原文 1 条 / 译文 1 条，互不影响；颜色分别是 `rgba(90,220,150,.38)` / `rgba(110,180,255,.38)` |
| Markdown 视图第 3 页：译文侧标绿 | 译文 1 条 / 原文 0 条；菜单是「AI 解释 / 复制 / 4 个色块」，**没有「笔记」** |
| 同上，原文侧标蓝 | 译文 1 / 原文 1；库里两条分别是 `md:p3+zh+green`、`md:p3+en+blue` |
| 整页刷新（无任何点击） | 高亮自己画出来：md 第 3 页 zh1/en1、正文行 en1/zh1，共 4 条 |

**验证**：`frontend_check.py` 新增第 ⑦ 节常驻用例（在同一行两侧各标一条 → 断言"原文侧只画原文 /
两侧各自独立 / 颜色真的生效 / 正文行仍可写笔记"，**跑完自己把探针造的高亮删掉**）→ **60/60**；
新增 `tests/test_highlight_sides.py`（side/scope 默认值与校验、读写往返、补列是否真的加上）→ 全绿；
`vue-tsc` 干净。


### 17.20 高亮支持公式（含跨行）与跨行选区；删除改为菜单里的「禁止」图标

用户口径：*1. 跨行公式要支持高亮 2. 删除高亮的方式改为在菜单上加入禁止符号来删除对应的高亮*；
随后又报：*对照模式下原文/译文两列选区不独立、能选到不符的位置；选中右侧不弹菜单（pdf 对照正常）*。

**① 公式（含 `\begin{aligned}` 那种多行公式）能整体高亮**

根因：老实现把偏移量当**Markdown 源**的偏移量用（`htmlWithHighlights` 往源里插占位符再解析）——
`$…$` 里插标记会把 KaTeX 弄坏，而且选中公式时 `sel.toString()` 得到的是 KaTeX 的字形文字
（还带 MathML 的重复副本），跟源文本永远对不上。上一轮我给这种情况加了"明确拒绝"的提示，
用户看到的就是"公式不能高亮"。

改法：**正文行的高亮也改成 DOM 标记**（与 Markdown 视图同一套），`utils/domMarks.ts` 统一定义坐标系：
- **公式按一个原子单位**参与偏移量（拼成一个占位字符 U+FFFC），画的时候**整块套一层 `<mark>`**
  （不去动 KaTeX 内部的 span —— 它的排版靠定位与负负边距，从中间切开必然错位）；
- `ReaderRow` 不再用 `htmlWithHighlights`，改为"先画高亮、再画搜索命中"（顺序有讲究：
  重画高亮会拍平搜索标记所在的文本）；
- 边界落在**纯空白文本节点**上时（划到段末/换行处很常见）取最近的真实文本边界，
  否则那一行算不出偏移量、整段丢。

**② 跨行选区 = 同组多条 + 菜单删除**

- 选区跨多行时按行切开（`rowSegments`），每行一条记录，共用同一个 `group_id`
  （新增列，`ensure_columns` 补列）；**每段存本段自己的文字**——只存整段选区的话，
  逐行渲染时校验必然不过，一条也画不出来（实测就是这么丢的）。
- 删除走菜单：`SelectionMenu` 新增「禁止」图标（`AppIcon` 新增 `ban`），只在
  选区/点击处**盖住了已有高亮**时出现；点它删除对应高亮，跨行的那种**整组一起删**
  （`delete_highlight` 按 `group_id` 连带删除并回传所有 id）。点高亮本身不再弹原生确认框，
  而是把菜单叫出来（菜单里才有那个禁止图标）。

**③ 两列选区独立（原文列 / 译文列）**

`clampSelection` 原来只区分"pdf / 正文"，对照模式下原文与译文是同一行的两列，
从一列拖到另一列会一路"吞"过去 —— 用户看到的就是"选区不独立、能选到不符的位置"。
现在 `columnOf()` 细化成 `pdf / rows:en / rows:zh`，跨列即夹回。
**顺带修掉一个静默失效**：`lastGood` 里的节点可能已被重新渲染换掉（画高亮会拆文本节点），
对着游离节点调 `setBaseAndExtent` 会抛错、夹取静默失效 → 现在先判 `isConnected`，
不可用就 `collapse` 到锚点并把 `lastGood` 清掉。

**④ 「选中右侧不弹菜单」= 同一处 bug**

PDF 对照（`left=page`）时每行只有一个 `.rr-body`，所以正常；文本对照时一行有两个，
`rowSegments` 只取第一个（永远是原文侧）→ 在译文侧划词一段都取不到 → 菜单不出来。
现在按 `side` 取对应的 `.rrow.side-<side> .rr-body`。

**实测**（无头浏览器 + 自检脚本，探针造的高亮跑完自动按"前后差集"清干净）：

| 用例 | 结果 |
|---|---|
| 选中一整条公式后标黄 | 1 段，`mark.hl > .katex`（整块包住）、KaTeX 结构完好、底色 `rgba(255,214,64,.45)` |
| 译文侧划词 | 菜单正常弹出（修复前不弹） |
| 跨行选择（p001-01 尾 → p001-02 头） | toast「已高亮（跨 2 行）」，库里两条：`p001-01 41..47 "le.com"`、`p001-02 0..6 "Majid"` |
| 点其中一段 → 菜单「删除高亮」 | 两段一起消失（7 段 → 5 段，那两行归零） |
| 原文列 → 译文列拖选 | 选区被夹回 `rows:en / rows:en`（不再跨列） |

**验证**：`frontend_check.py` 第 ⑦ 节扩到 9 项（点色块真能生效 / 两侧独立 / 颜色生效 /
公式整块高亮 / 跨行同组 / 菜单删除整条 / 仍可写笔记 / 跨列被夹回）→ **65/65**；
`tests/test_highlight_sides.py` 增加分组删除与单条删除两条 → pytest 全绿；`vue-tsc` 干净。


### 17.21 高亮返工：Markdown 视图的两面独立 + 公式高亮（用户复报后的第二轮）

用户复报：*1. 两列选区还是不独立 2. 不支持公式高亮*。上一轮我按"正文对照模式"验证通过就收工了，
**漏了 Markdown 视图**这个面 —— 而"两列"在那里指同一页里的**原文面 / 译文面**。

**这一轮真正修掉的三处**

1. **`columnOf` 不认 Markdown 视图的面**：只区分了 `pdf / rows[:side]`，弹窗里的
   `[data-md-side="en"|"zh"]` 落进"未知"，于是那个面的跨列选区**永远不会被夹取**。
   现在返回 `md:en / md:zh`。
2. **夹取条件太窄**：原来是"两边都在内容里、且分属不同列"才夹。拖动时焦点很容易跑到
   工具栏/页号标注上（那是 `''`），这种情况以前直接放行 → 选区把"上下对照 / 原文 / 译文 / 第 N 页"
   这些 UI 文字一起选进来。现在改成：**只要有一边在内容里、另一边不在或不同列，就夹回本列**
   （两边都在内容外则不管：那是弹窗页脚之类的正常选择）。
3. **菜单删除整组时的 404 把界面更新整个跳过**：跨行高亮删第一条时后端已把整组删掉并回传
   `deleted_ids`，再删兄弟 id 就是 404 —— 以前 `for + await` 里一个异常就跳到 catch，
   `highlights.value` 那行永远不执行，表现是"库里删了、界面上的高亮还在"。
   现在逐条删、单条失败忽略，最后统一按并集过滤本地列表。

**另外两处体验补丁**

- 原 PDF 栏（pdf.js 文字层）**没有块锚点**，本来就不能高亮；以前那里只给「AI 解释 / 复制」，
  用户只会觉得"高亮坏了"（"公式不能高亮"很可能就是在左边点的）。现在菜单里明确写一行
  「原 PDF 栏不支持高亮」。
- Markdown 视图划词改成**先与所在面求交集**再算偏移量（`offsetsWithin`）：选区万一跨到另一面，
  至少把锚点这一面覆盖到的部分标上，而不是"菜单压根不出来"。

**验证方式也返工了**：前几轮的探针用"一次性 `addRange` + 合成 click"，
而夹取逻辑靠的是**拖拽过程中反复派发的 `selectionchange`**，所以它测不出真实拖拽的问题；
探针的点还必须落在**滚动容器**的可见区里（页面很长时随手取元素中心会落到工具栏上，测的就不是内容区）。
现在 `frontend_check.py` 有两段"逐点扩展选区"的真实拖拽探针：

| 用例 | 结果 |
|---|---|
| 正文对照：原文列 → 译文列拖拽 | 夹回 `rows:en / rows:en`，菜单正常出现 |
| 正文对照：拖过公式 → 点色块 | 公式整块被 `mark` 包住、KaTeX 结构完好 |
| 跨行选择 → 菜单删除 | 新造的 2 段一起消失（0 残留） |
| Markdown 视图：原文面 → 译文面拖拽 | 夹回 `md:en / md:en` |
| Markdown 视图：拖过公式 → 点色块 | 公式整块包住、结构完好（4 个色块可选） |

`frontend_check.py` 现共 **67/67** 通过；`pytest tests -q` 全绿；`vue-tsc` 干净。


### 17.22 公式高亮"只黄中间一行" + 拖拽夹取跑在异步任务里（用户第二次复报）

用户复报（带两张截图）：*1. 公式高亮 2. 选取不独立（两边都是 markdown 时）*，
并补一句"不是 markdown 视图" —— 也就是说第 2 条指的是**正文对照模式**（原文列 / 译文列都是 markdown）。

**验证手段这一轮彻底换了**：前几轮用合成事件（`setBaseAndExtent` + 派发 `MouseEvent`），
而真实鼠标拖拽的事件时序与它并不一样。这次直接写临时探针用 **CDP 派发真实鼠标事件**
（`Input.dispatchMouseEvent`：mousePressed → 若干 mouseMoved → mouseReleased）驱动本机 Edge，
页面侧只装监听、把**每一步**的选区（哪一列 → 哪一列、长度、所在单元格盒子）记下来。
两张截图的 bug 都用它复现了。

**① 「公式高亮」= `mark` 套错了层（截图逐像素复现）**

`formulaOf()` 原来取最近的 `.katex`。KaTeX 的显示公式是
`.katex-display`（**块级**，`\begin{aligned}` 的每一行都在它里面）> `.katex`（**行内**，高度只有一条行高）。
`<mark>` 套在内层行内元素上，浏览器只把底色画在那一条行高上 —— 三行公式看起来"只有中间一行被标黄"，
和用户截图一模一样（实测 `mark` 高 15px / 公式块高 94px）。

现在 `formulaOf()` 取**最外层**的 `.katex-display`（`.katex-display` 里还可能嵌 `.katex`，一路往上找），
`wrapRange` 给它加 `block` 类，`base.css` 加一条 `mark.hl.block { display: block }`。
实测 `mark` 高 94px = 公式块高 94px，整块都被底色盖住 —— 正文两侧与 Markdown 视图都验过。

**② 「选取不独立」= 夹取跑在异步任务里，浏览器已经画过一帧**

`selectionchange` 是**异步派发**的：真实拖拽时浏览器先把选区跨到对面列，再把事件交给 JS ——
等 `selectionchange` 跑起来，那一帧可能已经画出来了（用户看到的就是"选区还是不独立"）。
快甩时更糟：`mouseup` 直接读到跨列选区，菜单里显示的文字、要存库的偏移量都是跨列的。

现在夹取挂在 **`mousemove`（按住左键时）与 `mouseup`** 上，跟拖拽在**同一个任务**里完成：
浏览器内部确实会短暂跨列（探针的原始快照能看到），但**任何一帧都不会画出来**
（探针按"每一轮 mousemove 处理完、还没画"的状态断言：从不为跨列）。
`user-select: contain` 本来是最干净的解法，但实测 Chromium **没实现**（`CSS.supports` = false、
设上去算出来还是 `auto`），所以只能自己夹。

顺带把"夹到哪儿"做对了：把指针位置**投影**回本列再作为焦点
（左右分栏夹 x、上下分面夹 y；纵向按**滚动容器**夹而不是按行的盒子夹），
并与"最后一个同列选区"（`lastGood`）取**更远**的那个：

- 快甩（一两个 mousemove 就甩到对面列）时 `lastGood` 还停在起点，只有投影够远；
- 鼠标落在文字行上方/行间时投影会被浏览器吸附回锚点（选区直接变空），这时用 `lastGood`。

**③ 顺手修掉的三个夹取缺陷**

- `spanLength` 在**反向选区**（从下往上、从右往左拖）下量出 0：`setStart` 之后 `setEnd` 落在它前面时
  Range 会自己塌缩 —— 于是"更远的候选"永远选不出来、选区被清空（"从译文列往上拖到工具栏"就是这么丢的，
  修后 1939 字仍在译文列内）。
- 焦点跑到内容外（工具栏 / 侧栏 / 页号）时不再直接放弃投影：按**指针相对本列的位置**判夹哪根轴。
- 端点落在 `.cell` 上（在缝里按下或松手）时按 `cell-<side>` 判侧，不再当成"另一列"把合法选区夹掉。

**实测（CDP 真实鼠标，8 种手势；判据是"每一轮 mousemove 之后都不跨列"）**

| 手势 | 结果 |
|---|---|
| 原文列 → 译文列（慢拖 / 快甩） | 夹回 `rows:en`，109 / 59 字，菜单正常 |
| 译文列 → 原文列（起点在译文列最左边） | 停在 `rows:zh`，3 字，菜单正常 |
| 原文列 → 译文列（跨多行） | 夹回 `rows:en`，1441 字 |
| 译文列连续跨两行 | `rows:zh`，1921 字 |
| 从译文列往上拖到工具栏 | `rows:zh`，1939 字（不再丢选区） |
| Markdown 视图：原文面 → 译文面 | 夹回 `md:en`，1920 字 |

**验证**：`frontend_check.py` **71/71**（新增 4 项：`多行公式底色覆盖整块`（正文 / Markdown 各一）、
`拖拽当帧即夹回本列`、`拖拽当帧即夹回同面`）；`pytest tests -q` 138 项（137 通过 / 1 skip）；`vue-tsc` 干净。

**探针口径也修了**：以前按"行内标记总数"断言，库里本来就有别的高亮时会误判
（上一次失败的运行留下两条 → "原文侧只画原文"变成 2 条）→ 改成**只认这一轮自己造的标记**（前后差集）。


### 17.23 「向下拖拽时选区涂到对面列」的真因：两列在 DOM 里是交错的

用户复报（带截图）：*1. 完全移除原 pdf 栏不支持高亮字样 2. 拖拽向下时选区涂到对面列（一直在右侧选取拖拽），
感觉应该通过使用两边使用不同的容器解决*。

**① 移除「原 PDF 栏不支持高亮」字样**（照办）：`SelectionMenu.vue` 里那行 `<span class="no-mark">`
连同它的 `.no-mark` 样式一起删掉。那一栏的行为不变（没有块锚点 → 菜单只给「AI 解释 / 复制」）。

**② 纵向拖拽涂到对面列：这是"连续选区"的必然结果，不是夹取漏了**

两列在**文档顺序**里是交错的：`en1, zh1, en2, zh2, …`（同一个网格行里两列相邻，所以 DOM 也相邻）。
在译文列里**纵向**往下拖，选区必须从中间那些原文单元格**之间穿过** —— 锚点和焦点都还在译文列，
跨列的内容却夹在选区中间。夹取只看两端，永远发现不了（上一轮把这当成"两端跨列"来处理，所以没修掉）。

先验证了两条路，都不通：

| 试过的做法 | 实测结果 |
|---|---|
| 对面列 `user-select: none` | **没用**：拖拽中测得对面列 `computed user-select = none`，选中的 1130 字译文里仍然混进 2458 字原文（`user-select` 只影响"焦点吸附"，不会把中间的内容从连续 Range 里去掉） |
| 多段选区（每行一段，跳过对面列） | **Chromium 不支持**：`addRange` 两次后 `rangeCount` 仍是 1，只能拿到最后一段 |

用户建议的"两边用不同容器"确实是**结构上的正解**（文档顺序一改，Range 自然不穿过对面列），
但它会牵动三样东西：逐行对齐（现在靠"一行两列共用网格行"实现）、通栏插图（`grid-column: 1/-1`）、
通栏表格行（宽表改上下排）—— 属于一次大改，不适合混在这轮里做。所以这轮先做**语义与观感**上的等价修复：

- **不涂对面列**（观感）：拖某一列时给 `<html>` 挂 `pm-col-drag` + `data-pm-col`，
  `base.css` 里把对面列（以及页号、页脚、插图）的 `::selection` 背景设为 `transparent` ——
  它们仍在那个连续 Range 里，只是**不画出来**。
- **文字只取本列**（语义，三处一起改）：
  - 划词菜单里的文字改成按行切开后的**本列**文字（以前是 `sel.toString()`，实测 1130 字的译文选区会读出 3596 字）；
  - `Ctrl+C`：选区确实盖住对面列时改写剪贴板（只放本列文字），单列复制保持原样交给浏览器；
  - 「删除高亮」：候选高亮按**锚点所在侧**过滤（以前纵向跨行会把对面列的高亮也算成"选中的"）。

**实测（CDP 真实鼠标，纵向跨 5 行）**

| 用例 | 结果 |
|---|---|
| 译文列往下拖（拖到一半截图） | 译文列有选区底色、**原文列完全干净**（截图肉眼确认） |
| 原文列往下拖 | 反之亦然（原文列有底色、译文列干净） |
| 菜单里的「复制」 | 译文列那条只拿到中文（601 字，按 600 截断），原文列那条只拿到英文 |
| `Ctrl+C` | 译文列 1130 字（= 本列字符数）、原文列 3532 字；`preventDefault` 生效 |
| 纵向跨 3 行 + 中间行原文有一条 fixture 高亮 → 菜单「删除高亮」 | 本列的删掉、**对面列那条仍在** |

**验证**：`frontend_check.py` **75/75**（新增：`纵向跨行的对面列高亮不算选中`、`复制只带本列文字`，
以及新的一节 ⑨ `原 PDF 栏划词菜单` —— 断言 PDF 栏菜单只有「解释/复制」且**整页不含**「不支持高亮」字样）；
`vue-tsc` 干净。

**给用户的说明**：这一轮是"观感 + 语义"上的等价修复；如果希望**结构上**彻底分离（两列各自成容器），
那是一次牵动对齐 / 通栏图 / 通栏表格的重构，需要单独一轮来做。

**补修（用户复报："拖拽时不会到左侧，但是结束拖拽就会回到左侧"）**

上一版把"选区在哪一列"的标记**在 `mouseup` 时摘掉了** —— 但松手之后那个连续 Range 还在（译文列跨了
好几行），一摘标记，浏览器立刻把整段选区（含中间那些原文单元格）重新涂上底色，于是"一松手就回到左侧"。
现在标记**跟着选区走**：`onSelectionChange` 按**选区锚点**所在列同步、`onmouseup` 之后也保留
（`syncSelectionColumn()`），只有选区折叠（点别处）或离开阅读器时才摘掉。
为这个具体时序加了自检项 `松手后不发白对面列（标记跟着选区）`：断言松手后 `pm-col-sel/zh` 仍在、
清空选区后消失。`frontend_check.py` 现 **76/76**。

（顺带把类名从 `pm-col-drag` 改成 `pm-col-sel`：它表达的是"选区属于哪一列"，不再只是"拖拽中"。）

**补修 2（用户复报："复制的内容和胶囊菜单的位置不对：复制了左侧的内容、胶囊菜单在左侧，但高亮看起来没问题"）**

两处，都是"半对半错"的典型症状（同一次划词里，有的路径判对了侧、有的判歪了）：

1. **胶囊菜单跑到对面列头顶**：菜单位置原来取**整段选区的 bbox**（`sel.getRangeAt(0).getBoundingClientRect()`）。
   纵向跨行时那个 bbox 的左边界是对面列（Range 中间夹着对面列的单元格），实测译文列的选择算出
   `left=316`（原文列 320..933），于是菜单位于 x=316 —— 正好在左边头上。
   现在按**本列那一段**定位：`offsetsWithin()` 顺带回传交集的 `rect`，菜单用它（实测 x=978，落在译文列 957..1570 内；
   原文列的选择则是 320，落在原文列内）。
2. **判侧默认成了原文**：划词里判侧写的是 `closest('[data-side]')?.dataset.side === 'zh' ? 'zh' : 'en'` ——
   端点是**元素**（在单元格空白处按下、程序设置选区、点在行/单元格上）时 `closest('[data-side]')` 拿不到，
   于是**静默变成 `en`**：复制出原文、菜单按 `en` 算位置、高亮也记到原文侧。而夹取那边用的是
   `columnOf()`（有 `.cell-<side>` 兜底）→ 于是出现"选区看着在本列、复制/菜单却在本列对面"的半对半错。

   现在判侧统一走 `rowsSide(node)`（内部就是 `columnOf`），并且**锚点拿不到就看焦点那一端**；
   `onMouseUp` / `onCopy` / 「删除高亮」全部用它（以前这三处各写了一遍判侧）。自检里加了两项：
   `胶囊菜单落在本列（不是对面列）`、`端点是元素时判侧仍正确`（元素锚点 + 点色块 → 新造高亮必须全在译文侧）。

`frontend_check.py` 现 **78/78**（⑦ 节还顺手加了一条诊断：探针抛错时把它打印出来，
否则一抛错后面全是 `None`，只看结果看不出原因）。


### 17.24 选了 PDF 之后译文选不中（在译文里按下要收掉 PDF 选区）

用户报：*解决选中 pdf 后，无法选中译文内容的问题*，随后澄清：*指的是选中译文内容时取消 pdf 的选中状态*。

**真因**：`PdfPage.vue` 为了让"跨页选 PDF"不把中间的译文圈进选区，拖拽期间给 `<html>` 加
`pm-pdf-drag`（`base.css` 让非 PDF 内容 `user-select: none`，Chrome 会把不可选内容从选区里跳过），
而且**松手之后只要选区还在 PDF 上就一直留着**（这是刻意的，见 `endDrag` 的注释）。
问题是：这时用户在**译文列**里按下时，`onDocPointerDown` 发现落点不在 PDF 文字上就 `return` 了 ——
类没摘、选区还罩着，于是译文一直是 `user-select: none`；而 Chrome **不允许在不可选的内容上开始新选区**，
结果就是"选了 PDF 之后译文再也选不中"（不是"选到一半被吃"，而是压根开始不了）。

**改法**：落点不在 PDF 文字上时，先把上一次的 PDF 选区连同 `pm-pdf-drag` 一起收掉
（`clearPdfSelection()`：只在"当前选区确实盖着 PDF 文字层"时才 `removeAllRanges()`，然后摘类）。
按在 PDF 上时行为完全不变（那个类正是跨页选区需要的）。

**实测（CDP 真实鼠标，`left=page`）**

| 步骤 | 结果 |
|---|---|
| 1. 在 PDF 文字上拖一段 | `pm-pdf-drag` 挂上、译文列 `user-select: none`（这就是复现出来的前置状态） |
| 2. **不点别处**，直接去译文列拖一段 | 类已摘、译文列 `user-select: auto`、选区落在 `rows:zh`、划词菜单正常弹出 |
| 3. 再在 PDF 上拖一段 | 类照旧挂上（跨页选区行为未受影响） |

**验证**：`frontend_check.py` **79/79**（⑨ 节新增 `选了 PDF 后译文仍可选`：先造一个 PDF 选区、
断言此时译文 `user-select: none`，再往译文里派发 `pointerdown`，断言类已摘、译文恢复可选、PDF 选区被清空）。


### 17.25 按论文保存"打开状态"，切标签/重开都还原

用户口径：*希望能分别保存论文的打开状态（对照、原 PDF、译文在右），并且支持多标签切换时还原这个状态*。

**原来是什么样**：`reading_progress` 里只存了 `view_mode`（原文/译文/对照）与 `pair_order`（原文在左/译文在左），
**`original_mode`（原文文本 / 原 PDF）根本没存** —— `setOriginalMode()` 既不落库也不进 URL，
于是"切到原 PDF → 切个标签回来"就变回原文文本；只靠 `?left=page` 这种深链才记得住。

**这一轮**：

1. **落库**：`reading_progress` 加 `original_mode`（`models.py` + `session.py` 的 `_ADDED_COLUMNS` 补列，
   启动自动 ALTER），`_progress_payload` 读写它、`save_progress` 只接受 `text|page`（非法值不覆盖已有状态）。
2. **进 URL**：三处开关（视图 / 原文栏呈现 / 左右顺序）改完都调 `syncViewQuery()`，
   用 `router.replace({ path, query })` 把 `view/left/order` 写进当前 query。
   为什么非要有这一步：标签条记住的是**每个标签上次的路由位置（含 query）**，
   点标签切回来时 `tabLocation` 原样跳回去 —— 写进 query 就能**立刻**还原，
   不必等服务端进度那趟往返（快切标签时它可能还没落库）。服务端那份负责"关掉标签/重开浏览器还记得"。
3. `setOriginalMode()` 以前**不落库**，现在和其他两个开关一样立即保存。

**实测（真机 Edge；`frontend_check.py` 新增两处）**

| 用例 | 结果 |
|---|---|
| 进度接口往返 | PUT `bilingual/zh_left/page` → GET 回读一致（`original_mode` 已落库） |
| 不带任何 query 重开 | 左侧就是原 PDF、译文在左（`.pg-pair.image-right` + `.col-image`）→ 从服务端还原 |
| A 标签切成「原 PDF + 译文在左」 | URL 变成 `?nosse=1&view=bilingual&left=page&order=zh_left` |
| 切到 B 标签再切回 A | 状态还在（URL 三个参数都在、`.col-image` 在、工具栏按钮仍是 对照/原 PDF/译文在左） |
| `pytest tests -q` | 新增 `tests/test_reader_progress.py`（纯映射 + 落库往返；不可用 MySQL 自动跳过）全绿 |

**顺手修的自检卫生问题**：这些用例会**真的去点工具栏开关**、也会用 `?left=page` 之类的深链渲染阅读器，
而阅读器在卸载时会把"当前视图状态"存回服务端 —— 于是跑一次自检就把**用户真实的阅读状态**改了
（实测：跑完变成 `bilingual/page/zh_left`）。现在 `main()` 在**任何渲染之前**先读一份进度快照，
全部跑完原样写回；⑩ 节内也保留了它自己的快照/还原（它要显式写测试状态）。


### 17.26 界面状态也按论文记：侧栏开合 / 字号 / 显示参考文献 / 原 PDF 缩放

用户口径：*记录一下侧栏展开状态，字号等信息吧*（承接上一条"按论文保存打开状态"）。

**原来是什么样**：侧栏开合与字号只存在**全局** `localStorage`（`pm.reader.aside` / `pm-reader-text-size`）——
换一篇论文也是同一个值，多标签之间更是没法"各记各的"；`showRefs`（显示参考文献）与 `zoom`（原 PDF 缩放）
**根本没持久化**，切个标签就回默认。

**改法**（和 17.25 同一套两层结构，不再新增一套机制）：

- 后端 `reading_progress` 再加四列：`aside_open`(bool) / `text_size`(sm|md|lg) / `show_refs`(bool) / `zoom`(float)。
  **都允许 NULL**：NULL = 这篇论文还没记过 → 前端回退到本地偏好/默认值
  （所以不给 `NOT NULL DEFAULT`，否则"没记过"和"记成了默认值"就分不出来）。
  `save_progress` 只接受合法值：字号白名单、`zoom` 夹到 0.4~3、布尔必须是真布尔
  （非法值当作"没说"，不覆盖已有记录）。
- 前端：`loadAll` 里**服务端那份优先**，没记过才用 `localStorage`/默认值；
  新增一个 watcher `[asideOpen, textSize, showRefs, zoom]` → 一变就**立刻**存一次
  （`loading` 期间跳过：那一刻正把服务端那份灌进来，回存等于自己写自己）。
  于是 `toggleAside` / `setTextSize` / `v-model:show-refs` / 缩放按钮都不用各自加保存代码。

**定点验证**（按用户要求**没跑全流程自检**，只测这一件事）：

| 步骤 | 结果 |
|---|---|
| PUT `aside_open=false / text_size=lg / show_refs=true / zoom=1.5` | 回读一致 |
| 不带任何 query 打开阅读器 | 侧栏收起（`.reader.no-aside`）、字号按钮「大」且 `.rows-wrap` 实测 `font-size: 17px`（= `TEXT_SIZES.lg`）、「显示参考文献」勾上、缩放标签 `150%` |
| `pytest tests/test_reader_progress.py -q` | 4 项通过（新增两条：默认值必须是 `None`；非法值不覆盖 + `zoom` 超范围夹到上限） |

`frontend_check.py` 的 ⑩ 节也补了对应断言（`界面状态按论文还原（侧栏/字号/参考文献/缩放）`，
判据与上面这次定点验证完全一致），但**这一轮没有跑整套自检**（用户要求），
下次跑 `frontend_check.py` 时会一并覆盖。


### 17.27 对照「原 PDF」模式下的文字列支持 译文 / 原文 切换

用户口径：*对照增加译文/原文模式*，澄清后是 *对照 pdf 时有原文，译文模式* ——
即"**对照 + 原 PDF**"时，右侧文字列能在**译文（默认）/ 原文**之间切换，并且这个选择也要记下来。

**原来**：`ReaderPageRows.pageRowSide` 写死 `'zh'`（注释里还留着"以前跟着 `pairOrder` 走 → 原文出现两次、
译文消失"的教训）—— 想在页图旁边对着原文核对/复制是没有办法的。

**现在**：
- 新增 `pageSide`（`zh` 默认 / `en`）；工具栏只在 `pageLeft`（对照 + 原 PDF）时多一个「译文 / 原文」分段控件
  （`title="页图旁的文字列"`）—— 只有那时"另一列是页图"，文字列放哪一侧才是个选择。
- `pageRowSide` 改成读它，**仍然不跟 `pairOrder` 走**，所以那个"原文出现两次"的老毛病不会回来。
- 按论文记录：`reading_progress.page_side`（`NOT NULL DEFAULT 'zh'`，老记录自动补上默认值）+
  `save_progress` 白名单；同时写进 URL `?pside=`，切标签**立刻**还原（与 17.25 的 view/left/order 同一套两层结构）。

**定点验证**（仍然**没跑全流程自检**）：

| 用例 | 结果 |
|---|---|
| PUT `page_side=en` → 回读 | `en`；PUT 非法值 `xx` 被忽略（仍是 `en`） |
| 不带 query 重开（进度里是 en） | 文字列 266 行 `rrow side-en`、**0 行** `side-zh`，仍是 `.pg-pair`（页图 + 文字） |
| 进度里是 zh（默认） | 266 行 `side-zh`、0 行 `side-en` |
| 深链 `?view=bilingual&left=page&pside=en` | 266 行 `side-en`（切标签还原走的就是 query） |
| 工具栏 | 出现 `title="页图旁的文字列"` 的分段控件，默认「译文」高亮 |
| `pytest tests/test_reader_progress.py -q` | 5 项通过（新增 `page_side` 默认值/往返/非法值） |

### 17.28 阅读器数据加载占位：圆圈旋转动画

用户口径：*增加加载阅读器数据加一个圆圈旋转动画*，随后补一句 *不一定匀速旋转哦*。

**原来**：`<div v-if="loading" class="empty">正在加载阅读器数据…</div>` —— 一行灰字，
而且阅读器是整屏布局（`.app-main.wide`），`.empty` 的 `padding` 会把这行字贴在左上角。

**现在**：
- 占位换成 `.reader-loading`（`ReaderView.vue` 的 scoped 样式）：**圈 + 文案居中**，
  高度 `calc(100vh - var(--header-h))` 与 `.reader` 网格对齐，加载完切正文不会跳一下。
- 圈是 `base.css` 里的通用 `.spinner`（顺手删掉没人用的 `.boot-spinner`，它和 `index.html`
  里的首屏骨架不是一回事）。标记固定为
  `<svg class="spinner" viewBox="0 0 24 24"><circle cx="12" cy="12" r="9" /></svg>`。
- **不做匀速转**（按用户那句补正）：外圈 `spin 1.6s linear` 稳速旋转，弧长本身再用
  `spin-arc 1.4s ease-in-out` 一伸一缩（Material 那套循环进度）。只让圈匀速转的话，
  每一圈都一样快慢，看着像秒针/计时器；弧长伸缩才让"在转"有呼吸感。
  注释里标了"调的时候别把 `spin-arc` 删了"。
- `prefers-reduced-motion` 要单独收尾：全局规则只是把动画时长压到 ~0，弧长会**停在最短那一帧**
  （`dasharray: 1`，几乎看不见），所以这里显式 `animation: none` + `stroke-dasharray: none`，
  静态也留一个完整的环。

**定点验证**（无头 Edge + CDP 采计算样式，不靠截图 —— 转不转截图看不出来）：

| 用例 | 结果 |
|---|---|
| 只放 `.spinner` 标记（引构建产物 CSS）采样 32 次 | 弧长 `1.04 → 42`、32 次里 **16 个不同值** → 弧长在伸缩，不是匀速 |
| 同上，两圈动画 | `spin` 1600ms `linear`（外圈）/ `spin-arc` 1400ms **`ease-in-out`**（弧长）；计算尺寸 22px、描边 `rgb(15,123,138)` |
| 减少动效（`Emulation.setEmulatedMedia`） | 动画数 0、`stroke-dasharray: none` → 静态整环（不是"停在最短帧的隐身圈"） |
| **真实阅读器页面**：Fetch 域扣住 `/reader` 与 `/progress`，让 `loading` 一直挂着采样 | `.reader-loading` 在、文案「正在加载阅读器数据…」、容器 750×433（= 视口 485 − 顶栏 52）、圈心 x=375 = 视口中心；`spin`/`spin-arc` 时钟 5883→6316ms（真在跑）；旧的 `.empty` 占位已不存在 |
| 同一次页面加载里被扣住的请求 | 只有 `GET /papers/…/reader` —— 进度 PUT 没发出去，**这轮验证没写数据库** |

### 17.29 划词胶囊菜单：滚动 / 失焦 / 点到别处就收掉

用户口径：*使胶囊菜单在页面滚动或者失去焦点后消失*（附了一张菜单浮在译文上方、已经和文字脱开的截图）。

**原来**：菜单是 `position: fixed`，位置在**划词那一刻**按选区 rect 算好，之后没人管它 —— 一滚动就与文字脱开，
变成一枚"跟谁都不挨着"的胶囊。更漏的是"点已有高亮"弹出的那个菜单**没有选区**：点别处不会走到
`onMouseUp` 里"选区已折叠 → 收起"那条分支，菜单会一直挂在屏幕上。

**现在**（`ReaderView.vue`，三件事分开写，缺一条就有漏）：

- `window.addEventListener('scroll', onAnyScroll, { capture: true, passive: true })` ——
  滚动事件**不冒泡**，只有捕获能在 window 上收到"栏内滚动"（正文栏、侧栏、Markdown、原 PDF 各自是独立滚动容器）；
- `window.addEventListener('blur', hideMenu)` —— 窗口失焦（切标签页 / 切到别的应用）；
- `document.addEventListener('pointerdown', onDocPointerDown, true)` —— 补"点了别处"。
  **点菜单自己（色块/按钮）必须放过**：mousedown 的默认动作会清掉选区，一旦在这里收掉菜单，
  菜单会在自己的 click 触发前被卸载 —— "点高亮毫无反应"那个老 bug 就回来了（见 `SelectionMenu.vue` 顶部那段）。

**定点验证**（无头 Edge + 真鼠标事件：双击选词，比拖选稳 —— 在已有选区里按下会被浏览器当成"拖动选中的文字"；
`/progress` 交给 Fetch 域扣住，**全程不写库**）：

| 用例 | 结果 |
|---|---|
| 双击选词 → 菜单 | 打开（`AI 解释 笔记 复制`） |
| 点页面别处（左边页图区） | 收掉；选区也清空（selLen 10 → 0） |
| 正文栏里真滚轮滚 260px | 收掉；**选区保留**（selLen 10 不变）—— 菜单没了，但接着还能高亮/复制 |
| 切到另一个标签页（`Target.activateTarget`） | 无头环境里 `document.hasFocus()` 始终为 true（target 激活不产生真 blur），**这条测不出来**；改派发真正的 `blur` 事件 → 收掉，说明监听挂上了 |
| 按在菜单自己的色块上（只按下、松开点在外面，免得真写一条高亮） | 菜单仍在、选区仍在 → "点高亮有反应"没被新逻辑打掉 |

### 17.30 算法块在「对照 / 原文 / 译文」里糊成一坨：块切分把 Markdown 硬换行剥掉了

用户口径：*有些格式在 markdown 视图正常，对照和原文、译文模式下不正常*（贴了同一页的两张图：Markdown 视图里算法 1 的 11 步各自成行，
行视图里粘成一整段「1:输入：维度 d 与位宽 b 2:生成一个随机旋转矩阵 …」）。

**真因**：`split_blocks()`（页 Markdown → 块的唯一入口）里那行 `line = raw.rstrip()` 把**行尾两个空格**一起剥掉了 ——
那正是 Markdown 的**硬换行**标记。原文 md（Markdown 视图读的就是它）里算法每行都以两空格结尾，所以 md 视图正常；
切成块之后的文本只剩单换行，`marked` 按 CommonMark 把**软换行折成空格**，整块就糊成一段。

**修法**（两处，缺一处老产物都不会好）：

- `page_translate.split_blocks`：rstrip 之后**把硬换行标记补回来**（`raw.endswith("  ")` → `line += "  "`；
  代码围栏里是字面量，不补）。块的**数量与类型判定完全不受影响** —— `stripped` 仍然是 strip 过的。
- `reader_service.page_translations`：原来只有 `align == structure` 的页读时重配对，其余页直接用**存下来的行**
  （翻译那一刻切的，标记早被剥掉了）。现在**所有页都读时重算一遍**，只在「重算块数 == 存储块数」时替换 ——
  行 id 是位置编号（`p{page}-{i:02}`），块数一变 id 就挪位，已有高亮 / 笔记会指到别的文字上，宁可那一页保持旧样也不动 id。

**验证**（单元测试 + 无头 Edge 真渲染；`/progress` 交给 Fetch 域扣住 → 全程不写库）：

| 用例 | 结果 |
|---|---|
| 单元 `split_blocks("1: a  \n2: b  \n3: c\n\n下一段")` | 块文本保留两空格标记；单个行尾空格仍旧清掉；代码围栏不补 |
| 单元 `pair_rows`（算法块） | `original` 里 2 处 `  \n` |
| `pytest tests/test_page_translate.py tests/test_outline.py tests/test_reader_progress.py -q -m "not e2e"` | **49 项通过** |
| 真实页面（行视图）`p005-01` 算法块 | `<br>` × **10**、1 个 `<p>`、高 254px（≈11 行）；同页普通段落 `<br>` × **0**（没被牵连） |
| 截图肉眼比对 | 与用户给的 Markdown 视图截图**逐行一致**（1:输入 … 11:输出；行号、`QUANT_mse` / `DEQUANT_mse` 都在自己那行） |
| 全库影响面 | 只有 turboquant 的 **3 行**（`p005-01` / `p006-01` / `p006-02`，两个算法块）文本变化；这些行上**没有高亮、没有笔记**；没有任何页因块数不一致而保持旧样 |
| 开销 | `page_translations` 22 页 266 行：cold 13ms / warm 10ms（可忽略） |

**副作用**：这三行每行多了 2 个字符，若以前在这些行上存过高亮，偏移会挪几个字符 —— 实测库里这些行是空的，不用迁移。

### 17.31 算法块标题和正文之间的空档：空行吃了全局 `.empty` 的 28px 内边距 + 结构对齐把算法块配错

用户口径：*就是页 markdown 多空了几行（之前就有的小问题）*（贴了算法 2 的两张图：一张标题和 `1: 输入` 之间空了一大截，一张是紧的）。

先量了三处，把"到底谁多空"钉死（**页 Markdown 是好的**）：

| 位置 | 结果 |
|---|---|
| Markdown 视图（读的就是页 md） | 算法 2 那段 `<br>` × 11、`<p>` × 10 → **没有多余空行** |
| 单栏「译文」 | `p006-00` 21px → **`p006-01` 56px（内容为空）** → `p006-02` 281px（才是算法步骤）→ 这一格 56px 就是那"多出来的几行" |
| 对照 | `p006-01`：EN 算法步骤 ↔ ZH **空**；`p006-02`：EN 一句正文 ↔ ZH **算法步骤** —— 两侧根本没挨在一起 |

两个独立原因，都在：

**① 空行吃了全局 `.empty` 的内边距（56px 的洞）**：`ReaderRow.vue` 用 `empty: isEmpty` 标"本侧没内容"，
而 `base.css` 里的 `.empty` 是**全局空态工具类**（`padding: 28px 16px`，给"列表空了"那种占位用的）。
名字撞上 → 每个空行被塞进上下各 28px = **56px**，量出来的高度分毫不差。
改成 `is-empty`（只做标记、不带样式），空行高度立刻变 0。

**② 结构对齐把算法块配错了（两侧错位）**：`p006` 是 `align=structure`（EN 比 ZH 多一段正文 —— 定理那句在上一页已译完），
而 `_row_key` 只看**块类型**，算法块和正文段落都是 `para`，`SequenceMatcher` 于是"删掉"先出现的算法块、
让后一段正文去配算法译文。修法是给**编号步骤块**（多行 + ≥2 行以 `1: / 2. / 3、` 开头）一个独立的键
（`_is_step_block`）。这类块两侧的行数与序号天然一一对应，是最可靠的锚点。
**验证过影响面**：全库只有 turboquant 的 p006 一页是 structure，改动只让那一页的配对从错变对，行数不变（13 → 13，id 不挪）。

**验证**（单元测试 + 无头 Edge 真渲染 + 截图肉眼比对；`/progress` 交给 Fetch 域扣住 → 全程不写库）：

| 用例 | 结果 |
|---|---|
| `pytest tests/test_page_translate.py tests/test_outline.py tests/test_reader_progress.py -q -m "not e2e"` | **51 项通过**（新增 `_row_key` 步骤块单测 + 配对预期） |
| 单栏译文 `p006-01` / `p006-02` / `p006-03` | **281px（算法步骤）** / **0px（空行，不再占位）** / 47px |
| 对照 `p006-01` | EN 算法步骤 290px ↔ **ZH 算法步骤 281px**（对齐了） |
| 对照 `p006-02` | EN 那句正文 ↔ ZH 空（这一侧确实没有对应块，符合预期） |
| 截图 | 译文视图里标题下面直接就是 `1: 输入`，与用户给的"紧"的那张一致；对照视图左右逐行对齐 |
| 全库结构页 | 只有 p006 一页；其余 0 页（改动范围仅 structure 页，`exact` 页走按序号配对、根本不看键） |

真页复现条件依赖整页块序列（缩小样本就不复现了），所以**守门单测直接盯 `_row_key`**，另留一条"配对预期"测试记录行为。

### 17.32 删除跨行高亮时日志里那条 404：前端按 id 逐条删，后端一次就把整组删了

用户口径：*其实能正常删除和保存高亮*（附后端日志：`DELETE /highlights/h-84a5… 200` 紧跟一条 `DELETE /highlights/h-8b93… 404`）。

**行为没坏，是噪声**：跨行高亮是"同组多条"，后端 `delete_highlight` 删第一条就按 `group_id` **整组带走**并回传 `deleted_ids`；
前端却对 `hlIds` 里**每个 id 各发一次**（`Promise.all` 并发），于是兄弟 id 必然撞 404 ——
以前靠 `catch {}` 吞掉，功能是对的，但日志里固定出现一条 404，看着像删除失败。

**两处都收干净**（缺一处都还会剩噪声）：

- 前端 `deleteSelectionHighlights`：**同一分组只发一次请求**（按本地列表里的 `group_id` 去重），
  非分组的 id 照旧各发各的；`catch` 保留兜底（另一个标签页先删掉的情况）。
- 后端 `delete_highlight`：**幂等** —— id 不存在时返回 `{deleted_ids: [], count: 0, missing: true}` 而不是抛 404。
  多标签页同开一篇必然撞车，重复删同一条不该算失败。

**验证**（真浏览器 + Fetch 域**假造**高亮列表并**拦下 DELETE 计数**：不写库、真数据一行没动）：

| 用例 | 结果 |
|---|---|
| 假造 3 条同组高亮（盖在真实行 p001-06/08/09 上） | 画上 3 个 `mark.hl`，点开后菜单标题「删除这处高亮（**共 3 段**）」 |
| 点「删除高亮」后发出去的请求 | **只有 1 条** `DELETE /api/v1/highlights/h-stub0`（改之前是 3 条，其中 2 条 404） |
| `pytest tests/test_highlight_sides.py -q` | **7 项通过**（新增"重复删同一条返回 `count: 0` 而不是报错"） |
| 库状态 | 跑完仍是 1 条用户自己的高亮（假高亮只存在于被拦截的响应里） |

### 17.33 第三十一轮 —— RAG 接入层：`qwen3-vl-embedding` + `qwen3-vl-rerank` 真的调通了

用户给了百炼 API Key 与向量模型（Qwen3-VL-Embedding-8B），问重排选哪个。先把**模型事实**用真调用敲死
（不抄文档），再把两层 provider 接进现有 `model_config` 路由，最后用**项目自己的 provider** 跑通
"中文问句 → 向量召回 → 重排 → 跨模态 → 跨文献"。

**实测事实（2026-09 真实调用，探针 `scripts/_probe_dashscope.py`）**：

| 问题 | 实测答案 |
|---|---|
| 模型名 | 百炼上就叫 **`qwen3-vl-embedding`**：`qwen3-vl-embedding-8b` → `Model not exist`（`-8b` 是**开源权重**名，不是 API 模型名），`-2b` → `Access denied` |
| 端点 | **不是** OpenAI 兼容的 `/embeddings`，而是原生 `POST /api/v1/services/embeddings/multimodal-embedding/multimodal-embedding` |
| 维度 | 默认 **2560**，可降到 2048/1536/1024/768/512/256（`parameters.dimension`） |
| 文本 + 图片 | 同一语义空间（返回 `type=vl`），图片用 base64 data URI → **本机公式裁图/插图不需要公网可访问** |
| 上限 | 单请求 contents ≤ 20（provider 按 `parameters.batch` 自动切片，硬上限也夹住） |
| 重排 | `qwen3-vl-rerank` / `qwen3.7-text-rerank` / `qwen3-rerank` / `gte-rerank-v2` **四个都在同一个 native 端点可用**（不必分两条路） |

**重排模型的取舍（结论：默认 `qwen3-vl-rerank`）**：

- 与向量模型同一条 Qwen3-VL 底座，语义空间最一致；
- **唯一支持图片文档**的选项 → 公式裁图 / 插图能与中文问句落在同一次排序里
  （本项目 chunk 本来就有 formula / figure / table 三类带裁图）；
- 单条 8k token、单请求 100 条文本，我们的 chunk（≈900 字）远远够用。
- 纯文本最强是 `qwen3.7-text-rerank`（30k/条、201 语种），`qwen3-rerank`（4k/条）次之；
  `gte-rerank-v2` 官方公告 **2026-05-30 下线**，不选。
- 四者的 `input` / `parameters` 结构一致 → **换模型只是改一行配置**。

**落地的东西**：

- `DashScopeMultimodalEmbedding`：`embed` / `embed_images` / `embed_mixed`；`instruct` 是**查询侧**参数，
  入库不传、检索才传（预设里刻意不写，避免文档侧语义整体偏移）；
- `DashScopeReranker`：`rerank(query, docs, top_n, instruct=, images=, query_image=)`；
  注释写明分数是**本次请求内的相对分**、跨请求不可比（上层不许拿它做绝对阈值）；
- 工厂：`dashscope_multimodal` → 多模态向量；`dashscope` → 重排。
  **没动** 老的 `dashscope` → OpenAI 兼容向量映射（`text-embedding-v3` 不受影响）；
- 预设 `dashscope_rag`；`create_preset` 补上 `reranker` 类型（以前只导 generation/embedding/vision，
  重排预设会被**静默丢掉**）；
- `lancedb_store.upsert_chunks`：**向量维度变了就丢表重建**（LanceDB 向量列定长，512 维 mock 向量
  追加 2560 维会直接失败；向量库是派生数据，规划 §6.2 允许全量重建）；
- 前端 `ModelsView.presetTypes` 补 reranker（否则新预设的徽标少一项）。

**验证**：

| 用例 | 结果 |
|---|---|
| `pytest tests/test_rag_providers.py -q` | **13 项通过**（请求体形状 / 乱序索引重排 / 20 条切片 / data URI / 401 不重试 / 429 限流 / 条数不符报错 / 混模态同质文档 / 工厂接线 / 预设 / 维度重建） |
| `pytest -q -m "not e2e"`（全量离线） | **141 项通过** |
| 真实调用（走项目自己的 provider） | 12 条中文段落 × **2560 维**，嵌入 1.05s / 重排 0.32s；向量 top1 = 0.595 → 重排把"期望失真定义"顶到 **0.734** |
| 跨模态 | "量化误差的期望形式怎么写" → 公式裁图 **0.749**（文本 0.784，同档）；"这一页插图在讲什么" → 插图 **0.767**（第一） |
| 跨文献 | 用 TurboQuant p3 段落找相似技术：同篇 0.845 / 0.807（跨篇未进 top3，符合"几乎同一段话"的预期） |
| `GET /models/presets`（临时 8711） | 预设 7 个；`dashscope_rag` 带 `embedding.provider=dashscope_multimodal` + `reranker=qwen3-vl-rerank`；现有默认仍是 mock-embedding / heuristic（**没动用户库**） |
| 前端 | `vue-tsc` + `vite build` 干净（`ModelsView-C9iUp5nq.js`） |

两个坑（实测撞出来的，不是推的）：

1. **混模态时 `documents` 必须同质**：一旦有图片项，文本项也得写成 `{"text": …}`，
   字符串与字典混着传 → `400 InvalidParameter: Input should be a valid string…`（provider 已统一处理）；
2. 密钥落在 `data/secrets/dashscope.key`（`/data/` 已在 `.gitignore`），**不进 `.env`、不进版本库**。

### 17.34 第三十二轮 —— RAG 语料：同一 chunk 出 zh/en 两行（双语索引）

用户选的口径：**双语各一行**。原来 `build_chunks_for_paper` 只拿**解析产物的英文原文**（`md/full.md`）
入库，中文提问全靠模型的跨语能力；而库里**中文译文是现成的**（页级产物的 `translation_md`，22/22 页译完）——
不用白不用。

**做法（难点只有一个：怎么把"译文行"对到"解析块"上）**

- chunk id 加 `:zh` / `:en` 后缀、`lang` 写进 `metadata_json`，**不动 MySQL 表结构**（省一次 ALTER 迁移）；
- 正文块 / 图注块：译文取自页级产物的行；公式块语言无关（只有 LaTeX），两行同文本；
- 没翻译的块**不出 zh 行**（不拿原文冒充译文，否则索引里会出现"中文行其实是英文"的脏数据）。

**三次迭代（每一版都是被真实数据逼出来的，不是设计出来的）**

| 版本 | 做法 | 真实论文上的正文 zh 覆盖率 |
|---|---|---|
| v1 | 行签名 = 压空白后精确匹配 | **52% / 72%** —— 漏掉的大多不是"没翻译" |
| v2 | 签名只留「小写字母 + 数字 + 汉字」：解析产物与页级产物是同一页的**两次转写**（`#` vs `##`、冒号后空格、LaTeX 空格都会飘） | 升到 **55% / 80%**；诊断出真因是**切分不同**（同一个段落，解析块 140 字符、页级行 180 字符，互不为前缀） |
| v3 | 页内**字符级对齐**（difflib）取重叠最多的行；重叠 ≥ 12 字符且 ≥ 较短一侧的一半才认 | **100%（TurboQuant 56/56）/ 98%（AIAYN 49/50）** |

**v3 带出的新问题（已修）**：解析块比行细时，一个块会把整行译文端走 —— 实测 TurboQuant 有
**21/56** 个中文 chunk 的 token 数是英文侧的 2 倍以上，还有 **6 条内容完全重复**（同一段中文
被塞进多个 chunk）。修法是**按该段在行内的字符占比切片**（译文与原位序一致），切点吸附到句号；
贴边的（≤5%）直接用整段边界 —— 否则会从"向|量化"这种词中间切进去（第一版确实切出来了）。
修完：**重复 0 条**，zh/en token 比中位 **1.47 → 1.23**。

**一并落地**

- LanceDB 加 `lang` 列 + `search(langs=…)` 过滤 + `all_rows()`（下一轮词法检索要整表扫）；
  表结构与新数据不符（缺列 / 向量维度变）自动**丢表重建**（派生数据可重建，规划 §6.2）；
- `/knowledge/chunks` 支持 `lang` 过滤并回传 `lang`；`/knowledge/overview` 加 `chunks_by_lang`
  （按 id 后缀数 `…:zh` / `…:en`，不去解析 JSON）；`index_paper` 结果加 `by_lang`；
- 论文库页：chunk 总数标注为「双语行」，加中文/原文徽标，chunk 表加语言列。

**验证**

| 用例 | 结果 |
|---|---|
| `pytest tests/test_rag_chunks.py -q` | **14 项通过**（签名归一 / 页内对齐 / 切分不同仍配对 / 拒绝乱配 / 切片吸附句号 / 贴边不切 / 变体展开 / 相邻去重 / 公式双语 / 图注取译文 / 短标题保留） |
| `pytest -q -m "not e2e"`（全量离线） | **156 项通过** |
| 真实论文探针（**只读**，不写库、不调模型） | TurboQuant **258 行**（zh 129 / en 129）、覆盖率 **100%**；AIAYN **137 行**（zh 68 / en 69）、**98%**（剩 1 条是图内文字，原文本来就没译）；重复 **0**；id 唯一、最长 ≤ 128 字符 |
| LanceDB 往返（临时目录 + 假向量） | 258 / 137 条写入成功，`langs=["zh"]` 过滤生效，`all_rows()` 条数一致 |
| 前端 | `vue-tsc` + `vite build` 干净 |

**已知边界**：译文行比解析块粗时，zh 侧是按比例**近似切**的（不是人工对齐），跨块拼出的中文
可能与英文侧边界差一句。对检索无影响（嵌入按整段编码），但**不能拿 zh chunk 做逐字引用校验** ——
`/rag/query` 那一轮的答案校验要走原文（en）侧。

### 17.35 第三十三轮 —— 检索内核 + `POST /rag/similar`（向量 + 词法 BM25 → RRF → 重排降级链）

按用户选的口径，先交付**跨文献技术相似度**（任务书功能⑥）。内核一次写成通用的，两条链路共用
（`backend/services/knowledge/retrieval.py`）。

**五段结构**

1. **向量召回**：`qwen3-vl-embedding`，查询侧带 instruct、入库侧不带；LanceDB 用 cosine，距离 → 相似度；
2. **词法 BM25**：整表在内存里打分（本库几百~几千条 chunk），不引外部全文索引 —— 少一层索引就少一处不一致；
3. **RRF 融合**（k=60）：两路分数不可比（余弦 ∈ [-1,1]、BM25 无上界），只按**名次**融合，省掉一套要调的归一化参数；
4. **重排降级链**：`qwen3-vl-rerank` 优先；调用失败或没配模型 → 词法启发式，且**必须标明降级**（不能让启发式看起来像模型打分）；
5. **编排** `search(scope=all|cross|paper)`：`cross` = 排除来源论文（"我这篇的做法，别人怎么做的"）。

**踩到并修掉的三个真问题（都是实测暴露的，不是推出来的）**

| 现象 | 真因 | 修法 |
|---|---|---|
| 同一个 chunk 的 zh / en 两行占掉 top-k 里两个位置（实测查 `QJL 1-bit inner product quantization`，前两名是同一个标题块的两行） | 双语索引里一个逻辑 chunk 有两行，而 RRF 按 chunk_id 去重 | RRF **按 `pair_id` 聚合**：分数求和（两种语言都召回 = 更强的相关信号），展示取**名次最好**的那一行，并回传 `langs` 给前端标注 |
| 查询 `QJL 1-bit inner product quantization`（正文里一字不差）却 `lexical_hits=0` | **用户现有索引没有 `lang` 列**（本轮之前建的）→ `select([... "lang" ...])` 抛错，被"失败即返回空列表"的语义吞掉 | 按 schema **取列交集**再 select；旧表 `lang` 默认 `en`；语言过滤在旧表上忽略并在 `retrieval.notes` 里明说"重建索引后可用" |
| 查询"量化"匹配不上含"向量量化"的段落 | jieba 把"向量量化"切成**一个词** | 中文分词**词 + 单字**都给（"的/在/是"这类高频字噪声由 idf 自然压下去） |

**接口**：`POST /api/v1/rag/similar`

- 请求：`{query? | chunk_id?, scope: all|cross|paper, paper_id?, chunk_types?, langs?, top_k?}`；
- 响应：`{implemented, query, source, scope, items[{paper_title, page, block_id, lang, langs, snippet, scores{vector,lexical,rrf,rerank}, ranks}], retrieval{vector_hits, lexical_hits, fused, returned, rrf_k, vector_model, rerank{model,degraded,reason}, notes[]}}`；
- 错误：空请求 → 400 `VALIDATION_ERROR`；chunk 不存在 → 404 `NOT_FOUND`；非法 scope → 400。

**验证**

| 用例 | 结果 |
|---|---|
| `pytest tests/test_rag_retrieval.py -q` | **18 项通过**（分词与字级兜底 / RRF 聚合与加权与 top_k / BM25 排序与四类过滤 / 重排降级三态 / 编排元信息 / 服务层装饰 / 三个错误码） |
| `pytest -q -m "not e2e"`（全量离线） | **176 项通过** |
| 真实链路（临时向量库 + 真 `qwen3-vl-embedding` 2560 维 395 行 + 真 `qwen3-vl-rerank`） | 「如何在没有校准数据的情况下做在线向量量化」→ 全中量化那篇（rerank 0.844/0.828）；「自注意力为什么除以根号 d」→ 命中 3.2.1 缩放点积注意力（0.812）；术语查询 → 词法 + 向量双路命中 QJL 小节；跨文献（排除 TurboQuant）→ 返回 Attention 的公式/正文块 |
| 用户真实环境（临时 8712 + 现有 139 条旧索引 + 默认 mock/heuristic） | `implemented=true`；**`lexical_hits` 20（修复前是 0）**；`rerank.degraded=true` 且 reason 写"未配置重排模型"；语言过滤给 `notes` 而不是返回空；三种错误码都对 |
| 无头 Edge 截图（`/app/knowledge`） | 「跨文献技术相似检索」卡片渲染正常（分段控件 + 论文下拉 + 描述框 + 找相似按钮）；chunk 表多出语言列 |
| 前端 | `vue-tsc` + `vite build` 干净 |

**一点说明**：重排分数是**请求内的相对分**（官方明确），`scores.rerank` 只用于本次排序、跨查询不可比，
UI 也不拿它做阈值过滤。

### 17.36 第三十四轮 —— RAG 问答 `POST /rag/query`（ContextBuilder → 生成 → AnswerValidator）

三段各自的职责（`backend/services/knowledge/rag_service.py`）：

- **ContextBuilder**：检索结果 → 编号来源 `[S1]`…；每个来源把**原文 + 译文都给**（同一 chunk 的两行
  都在库里，靠 `pair_id` 取到另一语言）——中文作答时才能拿中文做逐字校验；预算 7000 字 / 最多 8 条，
  超出的**显式记 `truncated`**，不悄悄丢。
- **生成**：严格提示词（只用来源、事实后立刻标编号、引原文用「」、答不了就以「【无法回答】」开头）；
  `allow_general_knowledge` 打开时，补充内容必须写成「通用知识：…」并单独成段。
- **AnswerValidator**：来源编号必须存在；引号内容必须来自来源。不通过就把 `grounded` 置 False
  并列出问题 —— 规划 §19.5 的"来源 100% 可校验"宁可不答，也不给"看起来有依据"。

接口：`POST /rag/query`（非流式）与 `POST /rag/query/stream`
（SSE：`rag_meta` → 若干 `rag_token` → `rag_done` / `rag_error`；同步生成器交给 Starlette 在线程池迭代）。
前端「问答（RAG）」卡片翻过来：来源带 `[S1]` 标签、语言徽标、rerank 分数与跳回原文；
校验问题与说明单列；回答带「库内文献不足以回答」徽标。

**四个真问题（全是真跑出来的，不是推的）**

| 现象 | 真因 | 修法 |
|---|---|---|
| 三个问题一律回"没有检索到可用来源" | `_decorate_items` 装饰检索结果时把 `content` / `metadata` 丢了，ContextBuilder 一条来源都拼不出来 | 装饰结果**保留** `content` / `metadata`，对外响应在接口边界用 `public_item()` 裁掉 |
| 一个**诚实**的"库里没有"回答被标 5 处编造 | 模型会**重排 LaTeX**（`\mathbb{E}_Q[\|x-Q^{-1}(Q(x))\|]` → `E_Q[\|x-Q^{-1}(Q(x))\|]`），逐字比对必然误报 | 含公式的引用**单独记账**、不参与 `grounded` 判定 |
| 另一个诚实回答被标编造（`“校准数据”`、`“量子计算”`） | 模型用引号标**词**，还会复述提问里的说法（"来源没有出现'校准数据'一词"） | <8 字的词级引号、以及在提问里出现过的引号，都不做逐字校验（各自记账） |
| 又一处：`「data-oblivious/数据无关、在线适用」` | 模型用「」标**概念名**、还把中英术语拼在一起 | 与来源的**字符重合度 ≥ 70%** 视为有依据（真正编造的整句重合度远低于此） |

外加一条踩过的坑：**不要在服务层写死 `max_tokens`** —— 思考模式下思维链会先把预算吃光、正文返回空串
（项目在翻译那轮已经踩过），所以默认让 provider 用它自己配置的值，只有调用方显式传才覆盖。

**验证**（真模型全链路：临时向量库 + 真 `qwen3-vl-embedding` 2560 维 395 行 + 真 `qwen3-vl-rerank`
+ **用户当前配置的生成模型 `deepseek-flash`**；只读配置、只调用，不改任何设置）

| 用例 | 结果 |
|---|---|
| `pytest tests/test_rag_query.py -q` | **20 项通过** |
| `pytest -m "not e2e"`（全量离线） | **195 项通过** |
| 本篇可答（量化器为什么不需要校准数据 / 优化什么目标） | `grounded=True`、引用 `[S1][S3][S4]`、**0 编造**；上下文 6871 字（截断 2）；prompt 2473 / completion 4523（推理 3962）tok、18.6s |
| 跨篇可答（为什么要除以 √d_k） | `grounded=True`、引用 `[S1][S2]`；上下文 4286 字；prompt 1802 / completion 3642 tok、14.1s |
| 库里没有（问量子计算） | 回答以「【无法回答】」开头、`insufficient=True`、`grounded=True`（引用仍可校验）、0 编造 |
| 流式 | `rag_meta` → 456~652 个 `rag_token` → `rag_done`，末帧带同一份校验结论 |
| 前端 | `vue-tsc` + `vite build` 干净 |

**已知边界**：引号校验只能保证"内容来自我们给出的来源"，不能证明"原文里一定逐字如此"
（中文侧是按比例切的近似译文）；公式引用不做逐字校验（模型必然重排 LaTeX）。












