# Agent 接入规划（Cylit）

**状态：** 可执行规划（选型已定、关键假设已实测）
**范围：** 在 Cylit 现有检索/理解能力之上接入一个带 tool calling 的论文问答 Agent
**非范围：** 前端视觉改造、翻译流水线、MinerU 解析链路、**脚本执行（未规划）**

配套阅读：`docs/论文理解系统-设计.md`、`docs/语义单元索引-设计.md`、`docs/API.md`；
参考实现：`../retain-pdf/backend/ai/`。

---

## 0. 结论

**做一个独立的 agent 服务（独立进程 + 端口，与主仓共用 `scenemind` 环境），内核用
`pydantic-ai-slim[openai]==2.53.0` + `pydantic-ai-harness[skills]==0.53.0`，
把 Cylit 的能力包成 13 个工具（12 读 + 1 写），新功能以 `skills/<name>/SKILL.md` 的形式加。**

| 项 | 状态 |
|---|---|
| 框架选型 | **已定 A**（理由见 §5.1） |
| 依赖 | **已装进 `scenemind`**：新增 8 个包、`openai` 3.17→3.23.0（patch）、零降级、`pip check` 干净 |
| Skills | **已实测可用**（Windows 上跑通发现 + 解析 + include 过滤） |
| 待验 | DeepSeek 的 function calling 与 `OpenAIProvider(base_url=…)` 直连（需真实 key，未做） |
| 部署形态 | 未定（容器 / WSL2 / Windows 原生），**P0 第一步再定** |

---

## 1. 目标与非目标

### 1.1 目标

| # | 目标 | 可验收的现象 |
|---|---|---|
| G1 | 问"这篇的核心贡献是什么"能给出**带引用**的回答 | 回答里有 `[1][2]`，点引用跳到阅读器对应段落并高亮 |
| G2 | **没有证据就不答** | 检索无命中时返回护栏文案，而不是一段看起来正常的常识回答 |
| G3 | 加新功能 = 写一个 skill 目录 | 新增 `skills/xxx/SKILL.md` 重启即生效，Python 零改动 |
| G4 | 工具调用可控 | 轮数、超时、证据条数、视觉输入量都是设置项 |
| G5 | 模型能自主决定"读哪一页" | 检索先给位置（chunk + 页码），模型再决定要不要读全文/看版面 |

### 1.2 非目标

- 不做 PDF 修改类操作（旋转/删除/重排页面）——Cylit 是阅读与理解产品，
  retain-pdf 的 operation runtime（create → run → commit + 确认闸）整套不引入。
- 不做多 provider 抽象（Claude/Gemini），先用 OpenAI 兼容端点。
- **不做论文复核**（审稿式评价不是本项目的工作）。
- **不做脚本执行 / Shell**（未规划，见 §6.4 未来展望）。
- 不做跨用户协作/权限体系（单用户本地，`Settings.user_id = 1`）。

---

## 2. 现状与缺口

### 2.1 已有能力（全部已核对到函数/接口）

| 能力 | 实现 | 对 agent 的价值 |
|---|---|---|
| 混合检索 | `retrieval.search()`：向量 + jieba BM25 + RRF(k=60) + 重排（失败降级 HeuristicReranker） | 就是 retain-pdf 的 `search_fulltext`，而且更强。`scope="paper"/"all"/"cross"` 已支持 |
| 检索锚点 | item 白名单含 `chunk_id / paper_id / block_id / page / section / chunk_type / lang / snippet / image_url` | 引用编号所需锚点**已经齐了** |
| 结构化读取 | IR（`parsing/ir/`）+ `anchor_map.build_block_row_map()`（block → 原文行） | 比 retain-pdf 更细：它能跳到"页 + 块"，我们能到"行" |
| 论文理解 | `understanding_service`：A 路英文 / B 路中文，语义单元 + 公式 + 图表清单 | retain-pdf **没有**这层 |
| **论文档案** | `translation/brief.py` 的 `DocumentBrief`：每篇一次调用、结构化产出、落 `<paper>/md/translation_brief.json`、可人工编辑 | 见 §9，**已经存在，不用新建** |
| 页图 / 裁图 | `resolve_page_image`（`render_dpi=150`）、`figure_regions.ensure_region_crop`（600dpi 按需渲染落盘） | 视觉路径的原料齐了 |
| 生成 | `OpenAICompatibleGeneration`：`generate / stream / structured / generate_vision` | 缺 tool calling，由引入 pydantic-ai 解决 |
| 模型配置 | `model_config` 表 + `data/secrets/` + `model_manager` | 密钥单一真源，agent 复用 |

### 2.2 缺口

1. **无多轮会话**：没有 conversation/message 表，没有树与 head。
2. **无引用编号与清洗**：`[n]` 分配、内部 id 剥离、流式一致性都没有。
3. **无证据闸**：`validate()` 判"答案是否被证据支持"，但没有"没证据不许答"的硬闸。
4. **前端无问答 UI**：`/rag/query`、`/rag/query/stream` 前端零调用方。
5. **页面内容只读**：`GET /papers/{id}/markdown` 有，**没有任何写入接口**（写工具要新增）。
6. **页图 dpi 不够看公式**：`resolve_page_image` 走 150dpi，需要按需高 dpi。

---

## 3. 从 retain-pdf 抄的 8 条机制

| # | 机制 | 落在我们哪里 |
|---|---|---|
| M1 | **证据闸**：有文档 scope 却没检索就作答 → 那段正文不推流，先塞一句"必须先检索"；仍不搜 → 返回护栏文案 | §7.6 |
| M2 | **scope 由宿主强制**：`scope_tool_arguments()` 覆写工具参数里的 document/job id（它踩过"模型自带 job_id 读别的文档"的真实漏洞） | §4.3、§7.6 |
| M3 | **引用编号宿主分配**：给每个带 `(document_id, block_id)` 的条目贴递增 `ref`，登记的 snippet 取译文 | §8 |
| M4 | **给模型的证据要瘦、给前端的引用要全**：白名单投影，剥 bbox/长度/重复字段；前端跳转用投影前的原始结果 | §7.6 |
| M5 | **清洗与流式一致**：内部 id 映射成 `[n]`，**映射不到的不动**；流式路径对累积原文批量清洗、只推安全前缀（guard 48 字符） | §8 |
| M6 | **引用表只回正文真正引用过的**；区分"忘了标"（fallback）与"编造了"（不 fallback——凭空造依据比没依据更糟） | §8 |
| M7 | **预算分离**：检索预算与计算预算分开扣；超预算硬收尾要带 `incomplete_reason` | §7.6 |
| M8 | **落库顺序**：`done` 在落库之后发；**落库之后不再查 deadline**（否则已成功的 turn 被误报超时，用户重试就多一条）；追问建议排在落库后且失败不影响本轮 | §11 |

---

## 4. 架构

### 4.1 拓扑

```
浏览器
  │  POST /app/api/agent/ask   (SSE)
  ▼
Cylit 主后端  FastAPI :8710        ← 既有进程，不新增依赖
  │  鉴权 / 会话读写 / SSE 字节级转发（不解释事件）
  │  POST http://127.0.0.1:8711/v1/ask   (X-Internal-Token)
  ▼
Agent 服务  FastAPI :8711           ← 新进程；与主仓共用 conda 环境 scenemind
  ├─ pydantic-ai-slim[openai] + pydantic-ai-harness[skills]
  ├─ skills/ 目录（SKILL.md 按需加载）
  └─ tools/ 13 个工具 → 回调主后端 /api/v1/*
```

**为什么独立进程**：① 崩溃隔离——agent 跑飞不该拖垮解析/翻译调度器；
② 可重启——改一个 skill 只需重启 agent，不影响正在跑的解析任务与 SSE 连接；
③ 对齐参考实现（Rust API + Python AI 服务两个进程，边界已被验证）。
**依赖不隔离**（按你的要求共用环境），实测只新增 8 个包，代价可接受。

### 4.2 边界原则

- **Agent 服务不直连 MySQL / LanceDB / 产物目录**，只经主后端 HTTP 拿数据——
  "什么能被 agent 看到"只有一个审批点。
- **Agent 服务不持有模型密钥**：模型配置由主后端给（`GET /api/v1/agent/model-config`，
  只回 provider/base_url/model 与已解析的 key，仅回环 + internal token 可读），
  设置页仍是密钥唯一入口。
- **主后端不解释 agent 事件**，只做字节转发（同 retain-pdf 的 Rust ai_proxy）。

### 4.3 接口契约

Agent 服务对外（主后端调用）：

| 方法 | 路径 | 用途 |
|---|---|---|
| POST | `/v1/ask` | 一轮问答（`stream=true` 返 SSE） |
| GET | `/v1/skills` | 已加载 skill 清单（名/描述/来源） |
| POST | `/v1/skills/reload` | 热加载（开发期） |
| GET | `/readyz` | 自检（含 skills 目录可读、模型配置可取） |

主后端对外（前端调用）：`POST /api/v1/agent/ask`（转发 + SSE 直通）、
`/api/v1/agent/conversations...`（会话 CRUD，**落主后端 MySQL**）、`GET /api/v1/agent/skills`。

**会话存主后端**的理由：消息要带 `citations` 与 `paper_id`，主后端已经在管这些字段；
放 agent 侧会出现两处都存、迁移动两边。

---

## 5. 选型与落地约束

### 5.1 为什么是 A

`pydantic-ai-slim[openai]` + `pydantic-ai-harness[skills]`：

1. **Skills 是官方能力**：`Skills(directories, *, include, exclude, workspace)`，
   指一个目录、`SKILL.md` 按需加载（渐进式披露），发现/注入/加载三层不用自己写；
2. **依赖小**：实测 31 个包（对比 deepagents 的 59 个，后者拖 langchain + langgraph +
   langsmith + langchain-anthropic + langchain-google-genai）。Electron 打包时
   langchain 的动态导入是 PyInstaller 的老大难，8 个包 vs 50 个包不是洁癖，是构建风险；
3. **同栈**：pydantic 原生（`Agent(output_type=Model)`、工具入参就是 pydantic 模型），
   跟仓库风格一致；
4. **最小权限**：B 的便利来自默认给 agent 可写文件系统 + 可执行 shell
   （`LocalShellBackend` 的 docstring 自己写着 "NO sandboxing or isolation"）。
   我们只要只读问答 + 一个受控写工具，自己那 60 行只读 backend 反而是**构造上只读**。

**B（deepagents）的翻盘条件**：如果以后想让 agent 在你机器上真正做事（写文件、跑脚本），
或者彻底不想写那 60 行——B 在 Windows 上零适配可用。技能目录格式两边通用，切换成本隔离在工具层。

⚠️ **装法**：必须装 `pydantic-ai-slim[openai]`，**不要装 `pydantic-ai`**——元包的 extras
会把 anthropic / google-genai / logfire / mcp / cli / evals 全套拉进来（101 个包）。
另：本机 pip 走阿里云镜像，**它滞后**，装最新版要显式 `--index-url https://pypi.org/simple`。

### 5.2 已验 / 未验

| 项 | 结论 |
|---|---|
| Skills 发现与解析 | ✅ Windows 上跑通：坏 skill 告警跳过不影响启动，`include` 过滤生效，中文 description 正常 |
| 版本耦合 | ✅ 真的：harness 0.53.0 钉 `pydantic-ai-slim==2.53.0`，必须一起锁 |
| Windows workspace | ⚠️ 框架自带的不可用，**必须自己补**（§5.3） |
| skill 级工具白名单 | ⚠️ **框架不提供**（§5.4） |
| DeepSeek function calling | ⏳ 需真实 key，未做 |
| `OpenAIProvider(base_url=…)` 直连 | ⏳ 同上；API 形状已确认 |

### 5.3 硬约束一：Windows 上没有可用 workspace

`LocalWorkspaceBackend`（及 `LocalWorkspace` capability）在 Windows 直接抛
`NotImplementedError: only supports POSIX platforms`；而 `Skills` 依赖 workspace
（官方 README：*"A run without a workspace fails at its start"*）。

三条出路：① 服务跑容器/WSL2（`LocalWorkspace` 直接可用）；② **自写只读 backend**
（约 60 行，已验证可行）；③ 远端 workspace（harness 的 `ssh_workspace`）。

**② 的关键坑（已踩）**：`working_dir()` 必须返回**虚拟 POSIX 根**（如 `/workspace`），
不能返回 `C:/...`。因为框架的 `Workspace.resolve()` 是 `posixpath.join(base, path)`，
POSIX 语义下 `join("/workspace", "/workspace/x") == "/workspace/x"`（幂等），
而 `C:/...` 在 POSIX 眼里是**相对路径**，会被拼成 `C:/ws/C:/ws/skills` 然后 `WinError 123`。

其余要点：`WorkspaceRef(provider=…, id=…)`；`working_dir` 是**协程**；
写操作一律抛 `WorkspaceReadOnlyError`；路径比较要同时容忍 `\` 与 `/`。

### 5.4 硬约束二：框架不提供的两件事

1. **`Skills` 不读 skill 目录里的 `reference.md`，也不跑 `scripts/`**（官方 README 明说）。
   要做到"读参考文档 / 跑脚本"必须**另外挂** `FileSystem` / `Shell`。
   我们本轮只用 L1（目录里的 `description`）+ L2（`SKILL.md` 正文）。
2. **没有 skill 级工具白名单**：`allowed-tools` 被"接受但忽略"
   （实测 `ignored_behavioral_fields=('allowed-tools',)`）。
   → 我们自己按激活的 skill 过滤工具集，或退到"只读 agent / 可写 agent 两个实例"。
   **写 skill 时一律显式写 `name`**（deepagents 强制、harness 可省但写了更稳）。

---

## 6. Skill 规范

### 6.1 目录与格式

```
Cylit/
  agent/
    app.py                  # FastAPI 入口 + SSE
    tools/                  # 13 个工具（薄封装，回调主后端）
    runtime/                # 模型装配、预算、清洗
    skills/                 # ★ 加功能只动这里
      paper-qa/SKILL.md
      formula-fix/SKILL.md
```

```markdown
---
name: paper-qa
description: 论文事实问答的默认流程。用户问某段内容、某个数字、某个方法怎么做的时用我。
---

# 论文问答

1. 先 `paper_search` 定位（只看返回的 chunk 与页码，不要凭记忆）。
2. 命中后 `page_markdown_get` / `block_get` 读那一页或那一块，拿能引用的原文。
3. 公式/表格/插图这类 OCR 不可靠的内容，用 `page_image_get` 看版面。
4. 每个来自论文的结论都写 `[n]`；论文里没有的就直说没有。
```

- `name`（与目录名一致）与 `description` **必填**；`description` 是模型唯一看到的字段，
  要写"**什么时候用我**"，不是"我是什么"。
- 正文是 L2，只在模型判断相关时加载。

### 6.2 三层渐进式披露

| 层 | 内容 | 进上下文时机 | 谁做 |
|---|---|---|---|
| L1 | `name` + `description` | 常驻（每 skill 一行） | 框架；建议 skill 总数 ≤ 15 |
| L2 | `SKILL.md` 正文 | 模型判断相关时 | 框架（`load_capability`） |
| L3 | `reference.md` / `scripts/` | 正文要求时 | **框架不做**，要另挂 `FileSystem`（本轮不用） |

### 6.3 权限分级

| 级 | 能力 | 组件 | 状态 |
|---|---|---|---|
| P-a | 只读：检索、读页/块、页图、插图、公式 | 仅 tools | **P0** |
| P-b | skill 读自己目录下的参考文档 | `FileSystem(root_dir='agent/skills', read_only=True)` | P1 |
| P-d | 子 agent 委派 | `SubAgents(agents=[…], max_depth=3, inherit_tools=False)` | 未规划（见 §6.4） |
| **P-写** | 写回论文内容（目前只有"修正某块公式"） | `page_markdown_edit` + `requires_approval=True`，**默认不挂** | P2 |
| ~~P-c~~ | ~~脚本执行~~ | `Shell` 能力记录在案，**本规划不含** | 未规划 |

### 6.4 skill 清单

| skill | 用途 | 依赖工具 | 级 | 阶段 |
|---|---|---|---|---|
| `paper-qa` | 事实问答默认流程：先检索定位、再读那一页/块、只用 `[n]` | `paper_search` `page_markdown_get` `block_get` | P-a | P0 |
| `paper-overview` | 回答"讲了什么 / 核心贡献 / 局限"——读预注入的档案 | 通常**不需要工具** | P-a | P0 |
| `figure-reading` | 图表解读：看图注 + 看图本身，需要时引用图片 | `figure_list` `figure_image_get` | P-a | P0 |
| `page-visual-check` | 页面级视觉复核：OCR/公式可疑时把整页渲染再看一遍 | `page_image_get`（高 dpi） | P-a | P0 |
| `formula-explain` | 公式解释 | `formula_get` `block_get` | P-a | P1 |
| `term-glossary` | 术语中英对照 + 本文用法 | `paper_search`（unit 型）+ 档案术语表 | P-a | P1 |
| `translate-check` | 译文质检（原文/译文对齐） | `page_markdown_get` `translate_get` `block_get` | P-a | P1 |
| `formula-fix` ⚠️ | 公式纠错：先读、再提议、等用户确认 | `page_markdown_get` + `page_markdown_edit`（写，需审批） | P-写 | P2 |

**未来展望（最低优先级，本轮不纳入）**：`cross-paper-compare`（跨论文对比，会让 scope
强制覆写复杂一档）、`survey-map`（领域地图，是另一种产品形态）、脚本执行 / Shell。

**已明确移出**：`review-notes`（论文复核不是本项目的工作）、`math-reproduce`（依赖 shell）。

### 6.5 反模式

1. **把 skill 当系统提示写**——几百行的通用规则该进 system prompt；skill 是"某个具体任务的做法"。
2. **在 skill 里内联检索逻辑**——skill 只说"先查什么、再读什么、怎么算够了"，工具改了就撒谎。
3. **skill 之间互相调用**——会形成不可见依赖图；需要组合就在正文写清顺序。
4. **`description` 写成功能罗列**——模型按场景匹配，要写"用户说 X 的时候用我"。

**诚实的边界**：只有"流程/知识"类新功能能靠写 skill 零代码（如"按 IMRaD 结构总结"、
"术语中英对照"）。**"新数据能力"仍然要加一个 tool**（如"查某作者的所有论文"）。
不写清这条，"写 skill 就加功能"会在第一次遇到新数据源时变成失望。

---

## 7. 工具面

**原则**：工具是宿主能力的白名单；每个工具一个 pydantic 入参模型 + 一个**瘦投影**返回；
`paper_id` 由宿主覆写（M2）；工具名用动宾结构，描述里写"什么时候用"。

### 7.0 工具总览（13 个）

| # | 工具 | 读写 | 一句话 | 落点 | 阶段 |
|---|---|---|---|---|---|
| 1 | `paper_search` | 读 | **在本论文内检索**：返回 chunk + 页码 + 总页数 + 短片段，**不发全文** | `retrieval.search(scope="paper", mode="qa")` | P0 |
| 2 | `page_markdown_get` | 读 | 某一页的 Markdown / 按块取 | `paper_service.get_markdown`、`reader_service.load_markdown_map`（**需加按页参数**） | P0 |
| 3 | `block_get` | 读 | 单个块的原文 + 译文 + 行锚点 | `GET /papers/{id}/blocks/{block_id}` | P0 |
| 4 | `page_image_get` | 读 | 整页渲染成图喂视觉模型（**需支持高 dpi**） | `paper_service.resolve_page_image` | P0 |
| 5 | `figure_list` | 读 | 该页插图清单（编号/段号/图注/URL） | `figure_regions.media_of_page` | P0 |
| 6 | `figure_image_get` | 读 | 单张插图裁图（600dpi 按需渲染） | `figure_regions.ensure_region_crop` | P0 |
| 7 | `kb_search` | 读 | 跨知识库检索（"我库里哪篇讲过 X"） | 同上，`scope="all"` | P1 |
| 8 | `formula_get` | 读 | 公式 LaTeX / 编号 / 出处 | `GET /papers/{id}/formulas`、`GET /knowledge/chunk` | P1 |
| 9 | `translate_get` | 读 | 译文（页级 / 全文） | `GET /papers/{id}/markdown/translated` | P1 |
| 10 | `library_list` | 读 | 书架检索 | `GET /papers` | P1 |
| 11 | `notes_search` | 读 | 笔记 / 高亮 / 标注 | `GET /notes`、`/highlights`、`/annotations` | P1 |
| 12 | `related_concepts` | 读 | 理解产物（概念与关联） | `GET /papers/{id}/related` | P2 |
| 13 | `page_markdown_edit` | **写** | 按块修正文本（CAS + 审批） | **需新增接口** | P2 |

**只有第 13 个需要写新接口**，其余 12 个挂在现有 API 上（第 2、4 个要加个小参数）。

**`paper_search` vs `kb_search` 是两种问题，不是一个工具加参数**：
"这篇论文里怎么说的"（P0）vs "我的库里还有谁讲过这个"（P1）。
合并会让模型在 P0 就拿到全库视野，scope 语义跟着糊。

### 7.1 搜索是为了"决定读哪页"，不是给答案

模型自主性的关键：**检索工具返回"候选位置"，不返回内容**。

| 层 | 工具 | 返回什么 | 目的 |
|---|---|---|---|
| 找位置 | `paper_search` | `chunk_id` + `page` + `page_total` + `section` + `chunk_type` + **短片段（≤200 字）** | 判断"这页值不值得读" |
| 读内容 | `page_markdown_get` / `block_get` | 完整文本 | 拿到**能引用**的原文 |
| 看版面 | `page_image_get` / `figure_image_get` | 图片 | 公式/表格/插图这类 OCR 不可靠的东西 |

四条实现要求：

1. **必须带 `page` 与 `page_total`**：页码让模型决定"去读第 7 页"，总页数让它知道规模、
   不会去要第 999 页（`page_count` 在论文详情里已有）。
2. **短片段要短，且标注是节选**：给全文等于把"读哪页"的决定权收走，
   且 Top-10 × 全文会瞬间吃光上下文（M4）。
3. **必须传 `mode="qa"`** ⚠️：`retrieval.query_vector()` 里只有 `mode == "qa"` 才用
   `QUERY_INSTRUCT`，否则用 `SIMILAR_INSTRUCT`（那是"找相似技术"那个用法的口径）。
   默认值 `similar` 用在问答上，查询编码口径不对，**白丢一截召回且看不出来**。
   注意 `mode` **不是"语义/关键词"开关**，别把前端筛选 UI 的语义套过来。
4. **保留自动兜底**：类型过滤后一条都没有时 `retrieval.search` 会放开全类型重搜一次
   （老索引/没建语义单元靠它）。

### 7.2 三个必须写清的细节

**① 页图 dpi 是个真问题。** `render_dpi = 150` 是阅读器口径，给视觉模型看公式上下标常常不够；
仓库里 `crop_dpi = 600`（"公式高倍裁图，4× 于 150dpi"）就是为这个准备的。
`page_image_get` **要支持 `dpi`**，高 dpi 走按需渲染 + 落盘缓存——**不要放大 150dpi 的图**
（放大只是插值，信息量没变）。→ P0 拿一篇公式密集的论文对比 150 vs 400 dpi 的识别正确率。

**② 图片 URL 一律用仓库存好的，不要自己拼。** `media_of_page` 拼 URL 时带了
`v=<区域哈希>`：裁图接口响应头是 `Cache-Control: public, max-age=604800`，而 URL 只由
"页 + 序号"决定——区域算法一变，浏览器还会拿缓存里的旧图。

**③ 论文档案不做成工具，是每轮预注入的上下文**（§9）。做成工具只会让模型"有时忘调"，
而这恰恰最该稳定。

### 7.3 "引用图片"不是工具，是四条规则

`figure_regions.markdown_refs(paper_id, page_no, prefix="/api/v1")` 已能产出
"图号 → Markdown 图片行"，并顺手确保裁图落盘。落实在四处：

| 位置 | 规则 |
|---|---|
| 工具 | `figure_list` / `figure_image_get` 返回的 `url` 就是可嵌入的受控路径 |
| 提示词 | 只能用工具返回的 URL **逐字**嵌入 `![简短说明](url)`；不得改写/拼接/用外站图；没有相关受控图就不配图 |
| 前端 | 图片走**鉴权 hydration**（fetch → blob URL） |
| 安全 | 只允许 `/api/v1/papers/<当前 paper_id>/figures/region/...`，其它一律拒绝 |

### 7.4 写工具（整份规划里唯一的写能力）

```
page_markdown_edit(block_id 必填, expected_text 必填(CAS), new_text, reason)
```

**先读后写：这是硬约束，不是流程建议。** 块就是分块的边界——chunk 全部由 block 派生
（`chunking.py` 的语义单元、`_row_formula_chunks` 的公式行、`_row_structure_chunks` 的图表），
`anchor_map.build_block_row_map()` 还把 block 映射回原文行。一旦"改写"变成**页面级自由文本
重写**，就可能合并/拆开块——block_id ↔ chunk 的对应、行锚点、阅读器高亮会**同时失效，
而且看不出来**：问答照样能答，只是引用位置开始漂。

三条写死：

1. **按 `block_id` 粒度编辑，不接受"整页 markdown 覆盖"**。改多个块就多次调用。
2. **先读后写 + CAS**：`expected_text` 与库里不一致就**拒绝并返回当前值**，让模型重读再改。
3. **只重算受影响的那一个块**，并**断言该论文 chunk 总数与各块边界未变**；
   变了就回滚报错（说明这次编辑跨了块边界）。

另外五件事必须一起做对：

1. **权威源不是 markdown 文件**：`GET /markdown` 是**派生物**，真正被检索、被阅读器渲染的
   是 MySQL 里的 block + IR。新接口要**改 block**（markdown 由 block 重新生成），
   否则问答引用新公式、阅读器显示旧公式。
2. **必须重建该论文的知识库索引**（`knowledge_service.reconcile(paper_id=…, fix=True)` /
   `POST /knowledge/index`）。**写完没重建比不改更糟**——下次问答还在引用错公式。
   返回值里要带"索引是否已重建"。
3. **人工修改标来源**：`source=manual / edited_at / edited_by`。论文**重跑解析会重建 block**，
   人工修改会被覆盖——要和术语表一个套路（`source=model|manual`，人工优先）。
4. **必须审批**：`Agent.tool(requires_approval=True)` + 前端确认卡；模型不能自己决定写。
5. **必须能撤销**：保留 `old_text`。

**并且 P0/P1 默认不挂**：不装 `formula-fix` 这个 skill，模型根本不知道有这个能力。

### 7.5 返回值必须瘦

以 `paper_search` 为例，**不要**发 `retrieval` 元数据（`vector_hits / fused / rrf_k /
rerank.degraded`）——那是给我们排查用的；也不发 `scores`（模型用不上，要排序说明就写
"已按相关度排序"）。`page_markdown_get` / `block_get` **不发 bbox**（模型没法用像素坐标
回答，48 块就白占 2.4K 字符）；bbox 与 `row_id` 只在 `to_citation()` 里给前端跳转用。

### 7.6 证据闸放在编排层

```
hits = paper_search(...)          # 宿主发起
if not hits: return 护栏("没有找到证据，无法可靠回答")
agent.run(...)                     # 只有有证据才进模型
```

比 retain-pdf 简单：它的闸要处理"模型该不该调工具"，我们的检索是宿主主动的。

---

## 8. 引用链（四段，缺一段就会出现"点不动"或"内部 id 泄漏"）

1. **分配**（宿主）：工具结果落地后给每个带 `(paper_id, block_id)` 的条目贴 `ref`，
   登记 `Citation(ref, paper_id, block_id, page, section, snippet /* 优先译文 */, image_urls, row_id)`。
2. **投影**：`to_model_payload()` 剥内部 id 与 bbox；`to_citation()` 保留全部（M4）。
3. **清洗**：模型漏出的 `chunk_id` / `block_id=` / `page_idx=` 口癖映射成 `[n]`，
   **映射不到的不动**（否则 `机型 MD-11 与 MD-80` 会被吃成 `机型 与`）；
   流式路径**不另写增量规则**，而是对累积原文跑批量清洗、只推"不会再被改动"的前缀
   （guard 48 字符，按总长自适应攒批避免 O(n²)）。
4. **渲染**（前端）：`[n]` 装饰成锚点（负向前瞻 `(?<!!)` 避开 Markdown 图片、跳过代码块）
   → 换成按钮（tooltip = snippet + 页码）→ 气泡下方"来源"列表（默认 ≤5 条）
   → 点击按 `paper_id + page + row_id/block_id` 定位并高亮。

**引用表只回正文真正引用过的**，并区分两种情况：一个标记都没写 → fallback 按页去重取 3 条；
写了但全是编造的（正文 `[42]` 而 citations 只有 1–4）→ **不 fallback**。
正文里那个 `[42]` 不动——学术文档的 `[42]` 很可能是原文自己的参考文献编号（M6）。

**我们比 retain-pdf 强的地方**：`anchor_map` 能把 block 映射到原文行，引用可以高亮到**行**。

---

## 9. 论文档案（已存在，只需补两个字段）

`translation/brief.py` 的 **`DocumentBrief`** 就是"论文总信息"，**不用新建**：

| 维度 | 现状 |
|---|---|
| 生成 | **每篇只调一次**（抽样：标题 + 摘要 + 各章节开头，上限 6000 字符），`provider.structured()` 出 JSON |
| 字段 | `domain`（领域与核心方法 ≤40 字）、`glossary`（8~20 条统一译法）、`keep_as_is`、`style`、`notes`、`meta`（venue/year/tags/**abstract_zh**） |
| 存储 | `<paper>/md/translation_brief.json`，**可人工编辑后重跑**（人工术语优先） |
| 消费 | 逐页翻译注入每个 page prompt（统一术语）；`understanding_service`；顺手回填元数据 |
| 接口 | `GET /papers/{id}/brief`、`POST /papers/{id}/brief` 已有 |

**要做的三件小事**：

1. **补两个字段**：`outline`（把已有的 `page_translate.build_outline_skeleton()` 并进来）与
   `key_claims`（核心主张 3~6 条，**加进 `BRIEF_SCHEMA` 即可，同一次调用、零额外成本**）。
   有了 `key_claims`，"这篇的核心贡献是什么"直接读档案答，**一次检索都不用**。
2. **给 agent 一个只读入口**（`GET /papers/{id}/archive`）。单篇问答不必落 MySQL 表——
   文件已是权威源，加表只会多一份要同步的状态。
3. ⚠️ **绝对不要改 `prompt_block()`**。其 docstring 明写"**必须字节级稳定**"——靠字节不变
   命中 provider 的 prompt prefix cache（DeepSeek 命中部分约 0.1× 计费），它刻意把 `meta`
   排除在外就是为此。给 agent 用的档案**另开一个 `archive_block()`**（含 outline / key_claims），
   别往翻译前缀里塞新字段——那会把省下来的钱又花回去。

**注入位置**：system prompt 之后的"当前论文档案"块（不是 user 消息——那要留给用户的话）。

---

## 10. 会话与记忆

### 10.1 表结构（MySQL，新增）

```sql
conversation(
  conversation_id CHAR(36) PK, paper_id VARCHAR(64) NULL, title VARCHAR(255),
  head_id CHAR(36) NULL, created_at, updated_at)

conversation_message(
  message_id CHAR(36) PK, conversation_id CHAR(36) INDEX,
  parent_id CHAR(36) NULL, seq INT,
  role ENUM('user','assistant','summary','tool'),
  content MEDIUMTEXT, citations_json JSON, tool_trace_json JSON,
  finish_reason VARCHAR(32), created_at)      -- stop / rounds_exhausted / cancelled / error
```

**为什么是树**：`regenerate` 与"编辑提问"都是在同一父节点下挂兄弟节点；线性数组做不到，
事后加会很痛。可见分支 = 从 `head_id` 沿 `parent_id` 回溯再反转（旧数据无 `parent_id`
时退化为按 `seq` 串联）。

### 10.2 记忆策略

| 项 | 值 | 理由 |
|---|---|---|
| 常驻窗口 | 最近 6 轮 | 论文问答通常在同几节内 |
| 压缩触发 | > 12 轮 | 低于此值压缩收益小于损失 |
| 摘要方式 | **抽取式**（不调模型） | 省一次往返，且不会把事实改写 |
| 摘要内容 | 用户关注点 + **带 `[n]` 的结论行** + 证据清单 `[ref] p.N block_id snippet` | 保留可回溯性 |
| history 上限 | 24000 字符 | 与档案（~1.5k token）一起算总预算 |

### 10.3 追问建议

只在**有证据**时生成（有 citations 或成功的 tool_trace）；排在落库**之后**；
任何异常吞掉返回 `[]`；deadline 剩不足 5s 不跑（M8）。做成设置项，默认开。

---

## 11. SSE 事件契约

| type | 载荷 | 前端行为 |
|---|---|---|
| `stage` | `{stage: routing\|retrieving\|reading, message}` | 进度文案（**首个事件必须是它**） |
| `session` | `{conversation_id, paper_id, runtime, skills[], content_source}` | 记录会话 id、显示已激活 skill |
| `tool` | `{round, tool, status, args_digest}` | 过程行（**不发原始 JSON**） |
| `answer_delta` | `{text}` | 追加正文（**增量，不是累积全文**） |
| `citation` | `Citation` | 引用表增量 |
| `compress` | `{summary_id, turns}` | 提示"历史已压缩" |
| `done` | `{answer, citations[], tool_trace[], rounds, incomplete_reason, persisted, conversation_id, followups[]}` | 权威结果，覆盖流式内容 |
| `error` | `{code, message, retryable}` | 错误终态 |
| `cancelled` | `{}` | 取消终态 |
| `heartbeat` | `{elapsed_ms}` | 忽略（保连接） |

**硬规则**：① 每次流必须且只能以 `done` / `error` / `cancelled` 之一结束；
② `done` 在落库**之后**发，带 `persisted`；③ **落库之后不再检查 deadline**（M8）；
④ 心跳 5s、总 deadline 120s（Cylit 检索比 Rust FTS 慢，给宽点；做成设置项）；
⑤ `answer_delta` 推的必须是**清洗后**的增量。

---

## 12. 安全边界

| # | 规则 |
|---|---|
| 1 | 用户消息、历史、摘要、论文正文、工具结果**都是不可信数据**，不能提升权限 |
| 2 | agent 服务不持有 MySQL/向量库凭据；数据只经主后端 HTTP 取 |
| 3 | agent 服务不持有任何可写文件/命令能力。**本规划不含脚本执行** |
| 4 | 工具参数里的 `paper_id` 由宿主覆写（M2 的真实漏洞） |
| 5 | 图片只允许 `/api/v1/papers/<当前 paper_id>/figures/region/...`，拒绝外链 / `data:` / `blob:` / 其它 paper |
| 6 | 模型不能决定"已完成"：只能声称工具明确返回成功的动作 |
| 7 | 所有轮次/超时/证据条数有上限，且可在设置页改 |
| 8 | 审计：每轮落 `tool_trace_json`（工具名、轮次、参数摘要、耗时、状态） |
| 9 | **写工具默认不挂**，且 `requires_approval=True`（§7.4） |
| 10 | 写操作必须"改权威源 + 重建索引 + 标 `source=manual`"三件一起做 |
| 11 | 视觉输入字节量设上限（张数 × 分辨率），超了拒绝而不是静默降质 |

---

## 13. 分阶段落地

| 阶段 | 内容 | 出口标准 | 估时 |
|---|---|---|---|
| **P0** | 服务骨架；**只读工具 6 个**（`paper_search` `page_markdown_get` `block_get` `page_image_get` `figure_list` `figure_image_get`）；**档案预注入**（`archive_block()` 含 `key_claims`/`outline`）；skills：`paper-qa` `paper-overview` `figure-reading` `page-visual-check`；SSE + 引用链（**不含多轮**） | ① 问"核心贡献"→ 带 `[n]` 的回答 → 点引用跳阅读器并高亮；② 问某张图 → 回答嵌入受控图片且前端能显示；③ 问一个 OCR 可疑的公式页 → 模型先 `paper_search` 定位、再 `page_image_get` 复核；④ 检索无命中 → 护栏文案 | 3~5 天 |
| **P1** | agent 循环（轮次/预算/证据闸）+ skills 目录 + 会话落库（树 + head）+ 追问建议 + 设置页入口 + `kb_search` + `formula_get` `translate_get` `library_list` `notes_search` | 多轮追问能记住上文；`/v1/skills` 能列出；加一个 skill 目录即生效 | 3~5 天 |
| **P2** | **写工具 `page_markdown_edit` + `formula-fix` skill**（按块 + CAS + 审批 + 索引重建 + 人工优先 + 可撤销）+ 评估回归 | 修正一处公式后：阅读器/Markdown 显示新值、**chunk 总数与各块边界未变**、旧 chunk 被替换、重跑解析不丢人工修改；回归集（`tests/retrieval_regression/questions.json`）通过率不低于单轮 RAG | 5~8 天 |

**P0 刻意不含多轮**：先把"证据 → 引用 → 跳转"打通并被自检覆盖，再加循环。
否则多轮一旦引入脏状态，定位"是检索错了还是循环错了"会很贵。
**P0 刻意不含写工具**：它牵扯三条链（权威源 / 索引重建 / 重解析覆盖），
放进 P0 等于第一版就带一个能改数据、又没被自检覆盖的入口。

---

## 14. 风险

| # | 风险 | 缓解 |
|---|---|---|
| R1 | harness 是 0.x 且与 slim 强耦合（0.53.0 钉 2.53.0），**又共用主环境没有 venv 兜底** | `requirements.txt` 里 `==` 钉死这两行 + 一个冒烟测试（换机重装必跑） |
| R2 | 框架不提供 skill 级工具白名单 | 自己按激活 skill 过滤工具集，或"只读 agent / 可写 agent 两个实例" |
| R3 | Windows 无可用 workspace | 容器/WSL2，或自写只读 backend（虚拟 POSIX 根，已验证） |
| R4 | DeepSeek function calling 稳定性（未验） | 退路：模型输出 JSON 动作由宿主执行（伪 tool call），代价是流式体验变差 |
| R5 | 成本失控（轮数 × 证据 × 档案 × 视觉输入） | 预算全部进设置页；每轮记 token 用量（已有 `tiktoken`） |
| R6 | 论文正文的 `[12]` 与我们的引用编号撞车 | 抄 M6：不按形状删；前端只在能匹配到 citation 时才把 `[n]` 变按钮 |
| R7 | 主后端多一层转发，排查链路变长 | 两侧日志都带 `conversation_id` + `round` |

---

## 15. 自检（`scripts/agent_check.py`，中文 `[PASS]/[FAIL]`，报告落 `data/logs/agent_check.json`）

| # | 断言 | 手段 |
|---|---|---|
| A1 | 服务健康 | `GET /readyz` |
| A2 | 无证据必护栏 | 用论文里不存在的词提问 → 命中护栏文案且 `citations == []` |
| A3 | 引用编号连续可解析 | `ref` 从 1 递增无洞；正文 `[n]` 都能在 citations 找到 |
| A4 | 内部 id 不泄漏 | 正文不含 `chunk_id` 形态串与 `block_id=` 口癖 |
| A5 | 轮数上限生效 | 需要 ≥4 轮的问题 → `rounds <= max` 且 `incomplete_reason == "rounds_exhausted"` |
| A6 | skill 生效 | 加临时 skill 目录 → `/v1/skills` 出现该名；触发场景 → `tool_trace` 出现对应工具序 |
| A7 | SSE 单终态 | 整条流消费完 → 终态事件恰好 1 个 |
| A8 | 落库先于 done | 收到 `persisted=true` 后立刻查会话 → 消息已存在 |
| A9 | scope 覆写 | 构造模型传错 `paper_id` 的用例 → 实际检索仍是宿主 scope |
| A10 | 引用跳转 | 前端探针（复用 `frontend_check.py` 的 `dom_probe`）：点引用 → 定位到页且高亮行存在 |
| A11 | 档案预注入 | 抓第一轮实际 messages → 有档案块且含 `domain/glossary/outline/key_claims`；问"核心贡献"时**不调检索工具**即可答 |
| A12 | 回答里的图片可用 | 问"图 3 画了什么" → 正文含受控图片 URL；fetch 该 URL → 200 且 `image/png` |
| A13 | 搜索决定读哪页 | 问需翻页的问题 → `tool_trace` 里 `paper_search` 在前、`page_markdown_get` 在后，且返回含 `page_total` |
| A14 | 写工具默认不可见 | P0/P1：`/v1/skills` 不含 `formula-fix`，`tool_trace` 从未出现 `page_markdown_edit` |
| A15 | 写工具三件套（P2） | 修正一次 → ① markdown 与 block 都是新值；② 检索命中新内容；③ 该块带 `source=manual`；④ 撤销回旧值；⑤ chunk 总数与边界未变 |

---

## 16. 关键事实备忘（实现时速查）

**版本**（已装进 `scenemind`，官方 PyPI 索引）

```
pydantic-ai-slim==2.53.0   pydantic-ai-harness==0.53.0   pydantic-graph==2.53.0
openai==3.23.0             pydantic==2.13.4             httpx==0.28.1
```

- 安装：`pip install --index-url https://pypi.org/simple "pydantic-ai-slim[openai]" "pydantic-ai-harness[skills]"`
  （阿里云镜像滞后，必须显式给官方索引）
- 冒烟脚本：`scripts/_agent_env_check.py` 的同类断言（9 项）应在换机重装后重跑

**框架 API**（实测）

```python
from pydantic_ai import Agent
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_ai_harness import Skills, FileSystem, Shell, SubAgents

Agent(model, output_type=…, instructions=…, tools=[…], capabilities=[…])
Agent.tool(name=…, description=…, requires_approval=True)     # 写工具用
agent.run / run_sync / run_stream / run_stream_events         # SSE 用 run_stream_events
OpenAIChatModel("deepseek-chat",
                provider=OpenAIProvider(base_url=…, api_key=…))
Skills(directories, *, include=None, exclude=frozenset(), workspace=None)
```

**主后端端点**（工具落点）

```
POST /api/v1/rag/similar              {query|chunk_id, scope: all|cross|paper, paper_id?, chunk_types?, langs?, top_k?}
GET  /api/v1/papers/{id}/markdown      全文 Markdown（派生物）
GET  /api/v1/papers/{id}/blocks/{bid}  块详情
GET  /api/v1/papers/{id}/pages/{n}     页图 PNG（render_dpi=150）
GET  /api/v1/papers/{id}/figures/region/{page}/{index}?card=1   插图裁图 600dpi
GET  /api/v1/papers/{id}/brief         论文档案（DocumentBrief）
GET  /api/v1/papers/{id}/outline | /formulas | /related
GET  /api/v1/papers/{id}/markdown/translated
GET  /api/v1/papers                    书架
GET  /api/v1/notes | /highlights | /annotations
POST /api/v1/knowledge/index           重建索引（写工具后用）
```

**检索要点**：`retrieval.search(query, mode="qa", scope="paper", paper_id=…)`；
默认候选类型 `unit/formula/figure/table`（正文 chunk 不进候选，避免同内容两条命中）；
类型过滤后为空会自动放开全类型重搜一次。
