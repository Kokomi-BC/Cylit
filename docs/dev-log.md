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



