# Agent 接入规划（Cylit）

**状态：** **P0 已落地并自检通过（46/46）**；P1/P2 待做
**范围：** 在 Cylit 现有检索/理解能力之上接入一个带 tool calling 的论文问答 Agent
**非范围：** 前端视觉改造、翻译流水线、MinerU 解析链路、**脚本执行（未规划）**

配套阅读：`docs/论文理解系统-设计.md`、`docs/语义单元索引-设计.md`、`docs/API.md`；
参考实现：`../retain-pdf/backend/ai/`。

**P0 落地清单**（跑起来只需要三条命令）：

```
python cli/pr.py serve      # :8710 主后端（首次启动会生成 data/secrets/agent.token）
python cli/pr.py agent      # :8711 Agent 服务
python scripts/agent_check.py        # 自检：默认不调用模型；--model 才跑真问答（跑完自删会话）
```

| 位置 | 内容 |
|---|---|
| `backend/agent/` | Agent 服务：`service.py`（HTTP/SSE）、`runtime.py`（编排+引用+清洗）、`tools.py`（6 个只读工具）、`workspace.py`（Windows 只读 backend）、`prompts.py`、`host.py`、`skills/`（4 个） |
| `backend/services/agent/` | 主后端侧：`project.py`（只读投影）、`conversations.py`（会话落库）、`model_config.py`（模型配置下发）、`bridge.py`（SSE 字节转发） |
| `backend/api/agent.py` | 24 条路由（对前端 13 条 + 对 agent 11 条，后者只认内部令牌） |
| `scripts/` | `agent_check.py`（自检）、`agent_env_check.py`（依赖冒烟）、`probe_agent_tools.py`（function calling 真机探测） |

---

## 0. 结论

**做一个独立的 agent 服务（独立进程 + 端口，与主仓共用 `scenemind` 环境），内核用
`pydantic-ai-slim[openai]==2.53.0` + `pydantic-ai-harness[skills]==0.53.0`，
把 Cylit 的能力包成 13 个工具（12 读 + 1 写），新功能以 `skills/<name>/SKILL.md` 的形式加。**

| 项 | 状态 |
|---|---|
| 框架选型 | **已定 A**（理由见 §5.1） |
| 依赖 | **已装进 `scenemind`**：新增 8 个包、`openai` 3.17→3.23.0（patch）、零降级、`pip check` 干净；两行已钉进 `requirements.txt` |
| Skills | **已实测可用**（Windows 上跑通发现 + 解析 + include 过滤，正文真的进了提示词） |
| DeepSeek function calling | **已实测可用**（`scripts/probe_agent_tools.py` 11/11：非流式、流式事件、思考模式三条路都调得动工具） |
| 部署形态 | 已定**Windows 原生 + 自写只读 workspace**（§5.3），为将来 Electron 打包留路 |
| P0 实现 | ✅ 服务骨架 / 6 读工具 / 档案预注入 / 证据闸 / 引用链 / SSE / 单轮落库 |
| P0 未含 | 写工具（P2）、追问建议（**已决定不做**，见 §10.3） |

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

### 2.2 缺口（✅ = P0 已补）

1. ✅ **无多轮会话** → 表已建（`conversation` / `conversation_message`，树形 `parent_id` + `head_id`），
   单轮已落库；**多轮压缩、树分叉、图片多模态**都已在 P1 落地（见 §13）。
2. ✅ **无引用编号与清洗** → `RefRegistry` + `StreamingAnswerSanitizer`（§8）。
3. ✅ **无证据闸** → 编排层的硬护栏 + 低相关度软信号（§7.6）。
4. **前端无问答 UI**：`/rag/query`、`/rag/query/stream` 前端零调用方（Agent 的 `/agent/ask` 也一样）→ P1。
5. **页面内容只读**：`GET /papers/{id}/markdown` 有，**没有任何写入接口**（写工具要新增）→ P2。
6. ✅ **页图 dpi 不够看公式** → `resolve_page_image(paper_id, page_no, dpi)` 按需渲染 + 落盘缓存，
   `GET /papers/{id}/pages/{n}?dpi=300`。

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
| M8 | **落库顺序**：`done` 在落库之后发；**落库之后不再查 deadline**（否则已成功的 turn 被误报超时，用户重试就多一条） | §11 |

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
`/api/v1/agent/conversations...`（会话 CRUD，**落主后端 MySQL**）、
`POST /api/v1/agent/conversations/batch-delete`（一级页的批量管理）、
`POST /api/v1/agent/conversations/{id}/branch`（切可见分支）、
`POST /api/v1/agent/attachments` + `GET /api/v1/agent/attachments/{id}`（图片，张数不限）、
`GET /api/v1/agent/skills`、`GET /api/v1/agent/status`（服务在不在 + 用哪个模型 + 运行限额，
**不回密钥明文**）。

**主后端给 agent 的只读投影**（都只认 `X-Internal-Token`，12 条）：

```
GET  /api/v1/agent/ping                          连通性
GET  /api/v1/agent/model-config                  模型配置（含已解析的密钥）
GET  /api/v1/agent/papers/{id}/archive           档案 + 大纲 + 规模
GET  /api/v1/agent/papers/{id}/pages?pages=3-8   块级正文 + 行锚点（`&lang=zh` 只要译文）
POST /api/v1/agent/papers/{id}/search            本论文检索（固定 mode=qa）
POST /api/v1/agent/kb/search                     跨库检索（带论文标题与归属）
GET  /api/v1/agent/papers/{id}/formulas?page=    公式清单（同阅读器口径）
GET  /api/v1/agent/papers/{id}/figures?page=3    插图清单（受控 url）
GET  /api/v1/agent/papers/{id}/pages/{n}/image?dpi=300   页图（按需渲染）
GET  /api/v1/agent/library?q=                    书架检索
GET  /api/v1/agent/notes?query=&kind=            笔记 / 高亮 / 批注
POST /api/v1/agent/internal/messages             agent 落助手消息（发 done 之前）
```

**为什么投影单独开一套而不是复用既有接口**：一是它们要"瘦"（剥 bbox、内部 id、检索元数据），
二是要按页/按范围取（`pages=3-8`），三是**只有它们是 agent 的可见边界** ——
加一个工具就要在这里加一条受控投影，不存在"agent 顺手读了张表"。

**会话存主后端**的理由：消息要带 `citations` 与 `paper_id`，主后端已经在管这些字段；
放 agent 侧会出现两处都存、迁移动两边，还要为此给 agent 数据库凭据。

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
| Skills 正文真的进提示词 | ✅ `/v1/skills` 的 `source=framework` + `chars` 非零（**踩过**：加载器要收框架的 `Workspace` 包装，传裸 backend 会 `AttributeError` 然后静默退化成"自己扫目录"，看着有 4 个 skill 其实没走框架） |
| 版本耦合 | ✅ 真的：harness 0.53.0 钉 `pydantic-ai-slim==2.53.0`，必须一起锁 |
| Windows workspace | ✅ 自写只读 backend 已落地（§5.3），`working_dir()` 返回虚拟 POSIX 根 |
| skill 级工具白名单 | ⚠️ **框架不提供**（§5.4）；P0 用"根本不挂写工具"绕过 |
| DeepSeek function calling | ✅ 11/11（非流式 / `run_stream_events` / 思考模式三条路都会发 tool call，入参按类型解析） |
| `OpenAIProvider(base_url=…)` 直连 | ✅ 同上，DeepSeek 走的就是这条 |

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
| `formula-explain` | 公式解释 | `formula_get` `block_get` | P-a | P1 ✅ |
| `term-glossary` | 术语中英对照 + 本文用法 | `paper_search`（unit 型）+ 档案术语表 | P-a | P1 ✅ |
| `translate-check` | 译文质检（原文/译文对齐） | `page_markdown_get` `translate_get` `block_get` | P-a | P1 ✅ |
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
| 1 | `paper_search` | 读 | **在本论文内检索**：返回 `ref` + 页码 + 总页数 + 章节 + 短片段，**不发全文** | `retrieval.search(scope="paper", mode="qa")` | P0 ✅ |
| 2 | `page_markdown_get` | 读 | 按 `pages` 取块级正文（`"5"` / `"3-8"` / `"all"`，一次 ≤20 页） | 新投影 `GET /agent/papers/{id}/pages` | P0 ✅ |
| 3 | `block_get` | 读 | **按 `ref`** 展开单个块的原文 + 译文 + 公式 LaTeX | `GET /papers/{id}/blocks/{bid}`（ref→block_id 由宿主查表） | P0 ✅ |
| 4 | `page_image_get` | 读 | 整页渲染成图喂视觉模型（`pages` + `dpi`，一次 ≤3 页） | `GET /agent/papers/{id}/pages/{n}/image?dpi=` | P0 ✅ |
| 5 | `figure_list` | 读 | 该页**图形区域**清单：插图 + **表格**（`kind`/`index`/编号/图注/受控 URL/`ref`） | `figure_regions.media_of_page`（`figures_only=False`） | P0 ✅ |
| 6 | `figure_image_get` | 读 | 单个区域的裁图（600dpi 按需渲染）—— **表里的数字只能走这条** | `figure_regions.ensure_region_crop` | P0 ✅ |
| 7 | `kb_search` | 读 | 跨知识库检索（"我库里哪篇讲过 X"）：每条带 `paper_title` + `is_current_paper` | 新投影 `POST /agent/kb/search`（`scope="all"`） | P1 ✅ |
| 8 | `formula_get` | 读 | 公式 LaTeX / 所在页 / 行锚点 / 上下文（可按页或关键词筛） | 新投影 `GET /agent/papers/{id}/formulas`（**与阅读器公式页签同一口径**） | P1 ✅ |
| 9 | `translate_get` | 读 | 按页取中文译文（逐块带 ref） | 复用 `/agent/papers/{id}/pages?lang=zh` | P1 ✅ |
| 10 | `library_list` | 读 | 书架检索（标题/作者模糊匹配），只回元数据 | 新投影 `GET /agent/library?q=` | P1 ✅ |
| 11 | `notes_search` | 读 | 笔记 / 高亮 / 批注（可按当前论文或全库） | 新投影 `GET /agent/notes?query=&paper_id=&kind=` | P1 ✅ |
| 12 | `related_concepts` | 读 | 理解产物（概念与关联） | `GET /papers/{id}/related` | P2 |
| 13 | `page_markdown_edit` | **写** | 按块修正文本（CAS + 审批） | **需新增接口** | P2 |

**参数口径：模型只认 `ref`，不认内部 id**（P0 的落地取舍）。检索与读页返回的每一项都带
宿主分配的 `ref`；`block_get` / 将来的写工具都收 `ref`，由宿主查回 `block_id`。
这样模型**没有机会**抄错 id，也不需要知道 `chunk_id` / `block_id` 长什么样 ——
M4 的"瘦投影"因此是从"清洗兜底"变成"结构上不可能"。

**只有第 13 个需要写新接口**，其余 12 个挂在现有只读能力上（第 2、4 个加了小参数，
第 7~11 个各加了一条瘦投影，都已落地）。

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
page_markdown_edit(ref 必填, expected_text 必填(CAS), new_text, reason)
```

**先读后写：这是硬约束，不是流程建议。** 块就是分块的边界——chunk 全部由 block 派生
（`chunking.py` 的语义单元、`_row_formula_chunks` 的公式行、`_row_structure_chunks` 的图表），
`anchor_map.build_block_row_map()` 还把 block 映射回原文行。一旦"改写"变成**页面级自由文本
重写**，就可能合并/拆开块——block_id ↔ chunk 的对应、行锚点、阅读器高亮会**同时失效，
而且看不出来**：问答照样能答，只是引用位置开始漂。

三条写死：

1. **按 `ref`（= 单个块）粒度编辑，不接受"整页 markdown 覆盖"**。改多个块就多次调用。
   `ref` 而不是 `block_id`：模型根本看不到 block_id（见 §7.0 的参数口径）。
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
回答，48 块就白占 2.4K 字符）；bbox 与 `row_id` 只在 `to_dict()` 里给前端跳转用。

### 7.6 证据闸放在编排层

```
hits, archive = 宿主取（预检索 + 档案）        # 宿主发起，不是等模型调工具
if not hits and not 档案有实质内容:  return 护栏("没有检索到相关内容…")   # 不进模型
agent.run(...)                                  # 有材料才进模型
```

比 retain-pdf 简单：它的闸要判断"模型该不该调工具"，我们的检索是宿主主动的。

**⚠️ 实测纠正：相关度阈值不能当硬闸。** 最初的设想是"重排最高分 < 阈值就拦住"，实测不成立：

| 查询 | 重排最高分（qwen3-vl-rerank） |
|---|---|
| 真问题"multi-head attention 是怎么做的" | 0.873 |
| 真问题"为什么需要位置编码" | 0.863 |
| 无关问题"量子色动力学中的胶子禁闭…" | 0.588 |
| **同一个无关问题，问句写长一点**（"这篇论文里…是怎么做的？"） | **0.678** |

问句一长，无关问题的分数就跨过了真问题的下限 —— 硬闸会误杀真问题，而向量检索**永远**
返回 top-k，"有没有命中"本身也不携带信息。所以现在的口径是：

- **硬护栏**只挡"确实什么都没有"：检索 0 条 **且** 档案没有实质内容
  （`domain` / `key_claims` 都空；**光有章节大纲不算** —— 一串小标题答不了"核心贡献是什么"）；
- **软信号**：真重排模型给出的最高分低于 `agent_evidence_floor`（默认 0.65）时，
  在提示词里加一句"这批材料相关度偏低，没有依据就直说"，**由模型判断**。
  启发式降级（词重合度量纲不同）不参与比较。

实测这个软信号 + 模型自述的效果是对的：问无关问题时模型自己回"论文里没有…的内容"，
没有编造（自检 ③ 覆盖）。

---

## 8. 引用链（四段，缺一段就会出现"点不动"或"内部 id 泄漏"）

1. **分配**（宿主）：工具结果落地后给每个带 `(paper_id, block_id)` 的条目贴 `ref`，
   登记 `Citation(ref, paper_id, block_id, page, section, snippet /* 优先译文 */, image_urls, row_id)`。
2. **投影**：`model_view()` 剥内部 id 与 bbox；`to_dict()` 保留全部（M4）。
3. **清洗**：模型漏出的 `chunk_id` / `block_id=` / `page_idx=` 口癖映射成 `[n]`，
   **映射不到的不动**（否则 `机型 MD-11 与 MD-80` 会被吃成 `机型 与`）；
   流式路径**不另写增量规则**，而是对累积原文跑批量清洗、只推"不会再被改动"的前缀
   （guard 48 字符，按总长自适应攒批避免 O(n²)；首批只攒 64 字符，否则要等 256 字才吐第一个字）。
4. **`done` 时两处改写**（流式阶段做不到，前端在收到 `done` 后覆盖正文）：
   - **编号压成 1..M**：registry 的号是按"工具返回顺序"发的，模型只用了其中一小撮，
     不压的话用户看到的是 `[63][49][33]` 这种带洞的号 —— 表里 4 条、正文写 `[63]`，没人看得懂。
     压缩时同步改写正文里的标记（只改**用到的**那些号）。代价见 R6。
   - **删掉非受控图片**：只允许 `/api/v1/papers/<当前 paper_id>/figures/region/...`。
     实测模型会"诚实地"去配一张它认识的图（讲 Transformer 架构图时嵌了
     `https://arxiv.org/abs/1706.03762`），提示词管不住，必须在服务端删。
5. **渲染**（前端）：`[n]` 装饰成锚点（负向前瞻 `(?<!!)` 避开 Markdown 图片、跳过代码块）
   → 换成按钮（tooltip = snippet + 页码）→ 气泡下方"来源"列表（默认 ≤5 条）
   → 点击按 `paper_id + page + row_id/block_id` 定位并高亮。

**引用表只回正文真正引用过的**，并区分两种情况：一个标记都没写 → fallback 取这一轮用到的全部证据；
写了但全是编造的（正文 `[42]` 而 citations 只有 1–4）→ **不 fallback**。
正文里那个 `[42]` 不动——学术文档的 `[42]` 很可能是原文自己的参考文献编号（M6）。

**我们比 retain-pdf 强的地方**：`anchor_map` 能把 block 映射到原文行，引用可以高亮到**行**
（实测 30/31 个块拿到了 `row_id`）。

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

**三件小事（已落地）**：

1. **补字段**：`key_claims`（核心主张 3~6 条）**已加进 `BRIEF_SCHEMA`，同一次调用、零额外成本**
   （实测 Attention 那篇一次生成 6 条实质论断）。有了它，"这篇的核心贡献是什么"直接读档案答。
   `outline` **不落进 brief**，而是在 `GET /agent/papers/{id}/archive` 时用
   `reader_service.build_outline()` 现算并入 —— 它是纯结构计算，存进文件反而会在重跑解析后变旧。
   ⚠️ 存量论文的 brief 里**没有** `key_claims`：`POST /papers/{id}/brief` 重跑一次就有。
2. **给 agent 一个只读入口**（`GET /agent/papers/{id}/archive`）。单篇问答不必落 MySQL 表——
   文件已是权威源，加表只会多一份要同步的状态。
3. ⚠️ **绝对不要改 `prompt_block()`**。其 docstring 明写"**必须字节级稳定**"——靠字节不变
   命中 provider 的 prompt prefix cache（DeepSeek 命中部分约 0.1× 计费），它刻意把 `meta`
   排除在外就是为此。给 agent 用的档案**另开 `archive_block()`**（`backend/agent/prompts.py`，
   含 outline / key_claims），别往翻译前缀里塞新字段——那会把省下来的钱又花回去。

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
  attachments_json JSON,                       -- 用户上传/粘贴的图片（P1 追加）
  thinking LONGTEXT, thinking_ms INT,          -- 思维链与思考耗时（只给前端折叠展示）
  finish_reason VARCHAR(32), created_at)      -- stop / rounds_exhausted / cancelled / error
```

**为什么是树**：`regenerate` 与"编辑提问"都是在同一父节点下挂兄弟节点；线性数组做不到，
事后加会很痛。可见分支 = 从 `head_id` 沿 `parent_id` 回溯再反转（旧数据无 `parent_id`
时退化为按 `seq` 串联）。

**附件为什么不进库**：图片是二进制，存 BLOB 会把备份 / 迁移 / 只读投影一起拖下水。
图落 `data/attachments/<日期>/<id>.<ext>`，消息行上只留一份元数据 JSON（名字 / 类型 / 字节数 / url）。
元数据另存一份旁挂 `<id>.json` —— 重放历史要显示文件名，而文件名只在**上传那一刻**知道。
id 只认 32 位十六进制：它是路径的一部分，不做白名单等于把任意路径读了出来。

### 10.2 记忆策略

**前提（用户 2026-10-02 口径）：问答模型也是百万上下文。** 所以策略从"尽量少塞历史"
改成"**按 token 预算塞，够用就不压**"——省的是压缩那一次额外调用与它带来的失真风险，
而不是上下文（1M 之下几轮问答的历史根本不占地方）。

| 项 | 值 | 理由 |
|---|---|---|
| 常驻窗口 | **预算内全给**（不按轮数截断） | 1M 上下文下没必要抠；以前固定 12 轮，聊久了模型会"看不见开头那几轮却不知道自己被截断"（用户口径 2026-10-03：不限制轮数） |
| 压缩触发 | **按预算**（历史 + 档案 + 本轮证据 > 200k tokens）而非按轮数 | 轮数不是成本，token 才是；1M 上下文下正常会话根本到不了 |
| 摘要方式 | **调一次模型**（不带工具的裸 agent，`output_type=str`） | 抽取式只能拿到句子，问答里的"结论"往往要跨段归纳；这一次调用的收益比它贵 |
| 摘要内容 | 用户关注点 + 结论（**带「第 N 页 §X.X」定位、不带 `[n]`**）+ 尚未解决的点 | `[n]` 跨轮无效，写进摘要会让后续引用点错地方（出口还会 `strip_ref_marks` 兜一道） |
| 自动压缩 | 上下文到 `agent_context_budget` 的 **90%** 时，下一轮提问**先自动压一次** | 不能指望用户盯着圆环；放在提问前而不是回答后，用户不会"先看它冲到 100 再归零"。**这是唯一的记忆收缩点**（不再有轮数截断） |
| 单轮工具预算 | 证据 ≤12 条 / ≤24000 字符（`agent_evidence_*`） | 与上下文无关的闸：防止一次 `pages=all` 把整篇灌进来，也压住成本 |
| 读页粒度 | `page_markdown_get` 支持 `"3-8"` / `"all"`（一次 ≤20 页） | 百万上下文下的直接收益：少几轮 tool call，而不是多塞几页 |

### 10.3 追问建议 —— **决定不做**（用户 2026-10-03 口径）

原本 P1 计划里有一条"回答完之后给 3 条接着可以问什么"。实现过一版又**整块删掉**了，
理由是用户点出来的两条，都成立：

1. **打不进缓存**：建议那一轮用的是**另一套 instructions**（不想让主对话的引用规则污染它），
   与主对话的缓存前缀毫无重叠 —— 每轮都是一次冷启动，输入还要带上回答正文（≤6000 字）。
   主对话本身靠自动前缀缓存很省，这笔钱花在最不该花的地方。
2. **可以被连续对话取代**：深挖的入口本来就存在（接着问、目录、引用跳转）。追问 chip 更像
   "教用户怎么提问"，属于锦上添花，不值得每轮一次 API。

删掉的范围：`backend/agent/suggest.py`、`/v1/suggest`、主后端回答后的那一段、
`conversation_message.suggestions_json`（连同已迁移的列一起 DROP）、前端建议 chips。
**别再往回流**：要重新做的话，先回答"它凭什么比直接接着问更好"。

---

## 11. SSE 事件契约

| type | 载荷 | 前端行为 |
|---|---|---|
| `stage` | `{stage: routing\|retrieving\|reading, message}` | 进度文案（**首个事件必须是它**） |
| `session` | `{conversation_id, paper_id, runtime, skills[], content_source}` | 记录会话 id、显示已激活 skill |
| `tool` | `{round, tool, status, args_digest}` | 过程行（**不发原始 JSON**） |
| `answer_delta` | `{text}` | 追加正文（**增量，不是累积全文**） |
| `thinking_delta` | `{text, ms}` | 追加思维链（**另一条通道**，前端折叠展示；`ms` 是服务端跑表，前端照它显示"思考 N 秒"） |
| `thinking` | `{status: "done", ms}` | 思考阶段结束（第一段正文出现时推一次），`ms` 为**权威耗时** |
| `citation` | `Citation` | 引用表增量 || `compress` | `{summary_id, turns}` | 提示"历史已压缩" |
| `done` | `{answer, citations[], tool_trace[], rounds, incomplete_reason, thinking, thinking_ms, persisted, conversation_id}` | 权威结果，覆盖流式内容 |
| `error` | `{code, message, retryable}` | 错误终态 |
| `cancelled` | `{}` | 取消终态 |
| `heartbeat` | `{elapsed_ms}` | 忽略（保连接） |

**硬规则**：① 每次流必须且只能以 `done` / `error` / `cancelled` 之一结束；
② `done` 在落库**之后**发，带 `persisted`；③ **落库之后不再检查 deadline**（M8）；
④ 心跳 5s、总 deadline 120s（Cylit 检索比 Rust FTS 慢，给宽点；做成设置项）；
⑤ `answer_delta` 推的必须是**清洗后**的增量。

**为什么 thinking 必须单独一条通道**：`ThinkingPartDelta` 与 `TextPartDelta` **都有**
`content_delta` 字段 —— 不判类型就会把思考模式的思维链当正文推给用户（P0 首轮实测：整段英文推理
出现在回答开头）。所以在 `runtime._translate` 里分两支：思考 → `thinking_delta`，正文 → `answer_delta`。
思维链**不做引用清洗**（它是草稿，正文才是要交付的东西），前端也**按纯文本渲染**（不走 markdown，
否则它看起来就跟答案一样了）。

**思考耗时**从**发起模型调用**算到第一段正文出现（`state.model_t0`），**包含工具往返**
（模型会"想 → 调工具 → 接着想"，用户等的就是这一段）。只统计纯粹"在想"的片段既算不准
（工具等待与思考在流里没有干净的分界），也会给出一个和体感不符的小数字。没有正文
（轮数用尽 / 报错）时算到流结束 —— 否则这个数字会永远停在 0，看起来像"没思考"。

**思考随消息落库**（`conversation_message.thinking` / `thinking_ms`）：前端在 `done` 之后会
重拉一次会话（`done` 是权威结果），不落的话思考块会在回答落地的那一刻凭空消失。它**不进正文、
不进下一轮历史**（`history_from_chain` 只发 `role` 与 `content`）。

**⚠️ 前端必须知道的一件事**：流式期间推的 `citation.ref` 是 registry 的**原始编号**
（可能到 60+），而 `done` 里的 `citations` 是压成 `1..M` 之后的那一份，正文里的标记
也跟着改了（§8 第 4 条）。这两个数字**对不上是正常的** —— 收到 `done` 时必须用它**整体替换**
流式期间累积的引用表与正文，不能只做增量合并。这也解释了为什么 `done` 是权威结果。

---

## 12. 安全边界

| # | 规则 |
|---|---|
| 1 | 用户消息、历史、摘要、论文正文、工具结果**都是不可信数据**，不能提升权限 |
| 2 | agent 服务不持有 MySQL/向量库凭据；数据只经主后端 HTTP 取 |
| 3 | agent 服务不持有任何可写文件/命令能力。**本规划不含脚本执行** |
| 4 | 工具参数里**没有** `paper_id`（M2 的加固版）：论文由宿主经 deps 注入，模型没有机会传错；检索固定 `scope="paper"` |
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
| **P0 ✅ 已完成** | 服务骨架；**只读工具 6 个**（`paper_search` `page_markdown_get` `block_get` `page_image_get` `figure_list` `figure_image_get`）；**档案预注入**（`archive_block()` 含 `key_claims`/`outline`）；skills：`paper-qa` `paper-overview` `figure-reading` `page-visual-check`；SSE + 引用链（**含单轮落库**，不含多轮压缩） | ✅ 全部达成，`scripts/agent_check.py` **46/46**：① 问"核心贡献"→ 带 `[n]` 的回答 → 引用带 `block_id`+`row_id` 可跳行；② 问某张图 → 回答嵌受控图片且 URL 可取回真图（426KB PNG）；③ 问公式页 → 先 `paper_search` 定位再读页；④ 无索引论文 → 护栏且**一次模型调用都没有** | 3~5 天 |
| **P1 ✅ 已完成** | ~~P1 工具面~~ + ~~前端入口（侧栏「对话」面板 / 划词解释 / 圆环压缩）~~ + ~~两级会话面板（搜索 / 分类 / 批量管理）~~ + ~~图片上传·粘贴（多模态直喂）~~ + ~~就地编辑与开分支（`branch=true`）~~ + ~~设置页「论文问答」卡片~~ + ~~超预算自动压缩~~ + ~~深度思考折叠与思考耗时~~ + ~~正文内引用锚~~；追问建议**决定不做**（§10.3） | ✅ 自检重写为「默认不花钱、不留痕」（§15）：`agent_check.py` **21/21**（免费）+ **30/30**（`--model`）、`frontend_check.py` **18/18**（免费）+ **30/30**（`--model`） | 完成 |
| **P2** | **写工具 `page_markdown_edit` + `formula-fix` skill**（按块 + CAS + 审批 + 索引重建 + 人工优先 + 可撤销）+ 评估回归 | 修正一处公式后：阅读器/Markdown 显示新值、**chunk 总数与各块边界未变**、旧 chunk 被替换、重跑解析不丢人工修改；回归集（`tests/retrieval_regression/questions.json`）通过率不低于单轮 RAG | 5~8 天 |

**P0 刻意不含多轮压缩**：先把"证据 → 引用 → 跳转"打通并被自检覆盖，再加压缩。
不过 `history` 的管道**已经接通**（主后端把可见分支的历史传给 agent，agent 侧转成
pydantic-ai 消息），所以"接着问"现在就能用；P1 补上了压缩、树分叉与图片。
**P0 刻意不含写工具**：它牵扯三条链（权威源 / 索引重建 / 重解析覆盖），
放进 P0 等于第一版就带一个能改数据、又没被自检覆盖的入口。

---

## 14. 风险

| # | 风险 | 缓解 |
|---|---|---|
| R1 | harness 是 0.x 且与 slim 强耦合（0.53.0 钉 2.53.0），**又共用主环境没有 venv 兜底** | `requirements.txt` 里 `==` 钉死这两行 + 一个冒烟测试（换机重装必跑） |
| R2 | 框架不提供 skill 级工具白名单 | 自己按激活 skill 过滤工具集，或"只读 agent / 可写 agent 两个实例" |
| R3 | Windows 无可用 workspace | 容器/WSL2，或自写只读 backend（虚拟 POSIX 根，已验证） |
| R4 | ~~DeepSeek function calling 稳定性~~ **已排除**：11/11 探测通过（非流式/流式/思考模式） | 保留 `scripts/probe_agent_tools.py`，换模型后重跑 |
| R5 | 成本失控（轮数 × 证据 × 档案 × 视觉输入） | 预算全部进设置页；每轮记 token 用量（已有 `tiktoken`） |
| R6 | 论文正文的 `[12]` 与我们的引用编号撞车 | 抄 M6：不按形状删；前端只在能匹配到 citation 时才把 `[n]` 变按钮。⚠️ P0 把编号压成 `1..M`（§8 第 4 条）后，**小号撞车概率变高**（论文自己的 `[3]` 更容易撞上我们的第 3 条）——目前只改写"用到的号"，观察后续是否需要在引用表里加视觉区分 |
| R7 | 主后端多一层转发，排查链路变长 | 两侧日志都带 `conversation_id` + `round` |

---

## 15. 自检（**默认不花钱、不留痕**）

用户口径（2026-10-03）："大幅简化检查代码，避免在历史记录留下内容以及避免额外的模型花费
（默认检查不调用模型）"。所以两个脚本都重写过一次，只保留"值得每次提交都跑"的东西：

| 脚本 | 行数（重写前 → 后） | 默认跑什么 | 加 `--model` 跑什么 |
|---|---|---|---|
| `scripts/agent_check.py` | 978 → 446 | 服务 / 技能 / 模型配置 · 六个只读投影 · 图片附件与守卫 · 会话列表分类搜索与批量删除 —— **不打模型** | 真问答一轮（可选 `--image` 验多模态、`--deep` 再跑压缩/就地改/开分支） |
| `scripts/frontend_check.py` | 4316 → 541 | 阅读器（正文 / 公式 / 胶囊）· 对话面板（一级页 / 二级页 / 输入区 / 确认框）—— **一条消息都不发** | 一条流式问答（回答 / 引用锚 / 思考块 / 删除确认框） |

两条纪律写在两个脚本的文件头里，别退化：

1. **默认零成本**：凡是要打模型（LLM 或重排模型）的都收在 `--model` 后面，跑之前会打印提示；
2. **跑完不留痕**：`--model` 造的会话在结束前 `batch-delete`（按探针自己的问题前缀找），
   上传的测试图片也从 `data/attachments` 删掉 —— 跑完 check，应用里的"对话历史"应该和跑之前一样。

**数据相关的断言现在会 `[SKIP]` 而不是假失败**：比如"当前论文没有历史会话"时，
点不到删除按钮、也没有消息可以看操作条 —— 以前那种"没数据就 FAIL"的写法会把真正的失败淹掉。

保留的关键断言（都是真出过问题的地方）：

- **附件守卫**：坏图（魔数像 PNG、内容断了）在上传口就得 400（否则会在模型端变成
  `400 unsupported image`）；穿越 id 必须 404；非图片必须拒。
- **真问答一轮**（`--model`）：SSE 骨架与**单终态**、回答带引用、引用带 `row_id`/`block_id`、
  正文不外泄内部 id / 不嵌外链图、**流式增量拼接 == `done.answer`**（抹掉编号与图片两处
  已知改写后逐字比）、落库先于 `done`、思考走独立通道且不进正文。
- **就地改 / 开分支**（`--deep`）：默认就地（改写这条 + 作废它之后的问答），
  `branch=true` 才是挂兄弟；切分支只挪 `head`。
- **前端**：胶囊三层（无描边 + 选中项白 pill + 阴影）、工具条胶囊内按钮不投影、
  顶部工具栏只有一个按钮、空态 0 按钮、输入区是一个圆角容器且模型/圆环在框外、
  小圆环无内容、输入框无边框无焦点环、多行自动增高、**正文里的 `[n]` 是引用锚且底部不再列
  chunk 号**、消息操作条在气泡下面、删除确认框无分隔线且取消不删。

**探针的三条铁律**（都踩过，写在 `frontend_check.py` 文件头）：虚拟时间下 `Date.now()` 是
虚拟时钟（忙等要用**计数**，否则死循环）、`sleep()` 不为 XHR 停顿（等就绪要"烧真 CPU + 放手
一拍"）、Python 字符串里的换行转义必须双写（否则 JS 语法错误、整个脚本不执行还看不出原因）。

---

## 16. 前端接入（P1 已落地）

**侧栏第二个页签就是「对话」**（紧跟「目录」，用户口径）。点它会把左栏放宽到 420px
（默认 300 会把消息挤成竖排）。面板是**两级**的：

- **一级 = 会话列表**（进面板先看到它）：**只列当前这篇论文的对话**（跨论文检索交给书架与知识库）、
  搜索框（标题 + **消息正文**，`q` 是数据库侧条件）、分类胶囊（全部 / 划词 / 常规）、
  管理模式（多选 / 全选 / 批量删除）、行内重命名与删除；顶部工具栏**只有「＋ 新对话」一个按钮**，
  会话页顶部连文字都不要（标题已经写着会话名了，只留一枚图标钮）。
- **二级 = 会话正文**：消息流 / 输入区。

**列表的几条口径**（都是用户口径，别再改回去）：

| 项 | 做法 |
|---|---|
| 范围 | **只有当前论文**：不提供"全部论文"切换（混在一起每一条都要先认"这是哪篇的"） |
| 条数 | 一次 30 条（`LIST_LIMIT`）；更早的用搜索找 —— 80 条时首屏 DOM 明显拖慢，也没人翻得完 |
| 标题 | 按**行内 markdown** 渲染（`inlineHtml`）：标题就是用户问的第一句，常常带 `$…$` 公式 |
| 分类 | 后端按"会话里有没有 `> 引用` 开头的用户消息"**自动打标**（`has_selection`），不引入要用户维护的标签 |
| 复选框 | **两个状态两个图标**（`checkbox` / `checkbox-off`）：只换颜色等于"只有一个状态图标" |

| 能力 | 做法 |
|---|---|
| **分类（划词 / 常规）** | 后端按"会话里有没有 `> 引用` 开头的用户消息"**自动打标**（`has_selection`），不引入用户要维护的标签体系 |
| **流式** | `api/stream.ts` 的 `postSse()`（fetch + ReadableStream 手解 SSE 帧）。**不用 `EventSource`**：它只能 GET，而问题/选区/会话 id 都在 body 里 |
| **引用** | 回答下方是可点 chip（`[n] p3`）；点击跳阅读器那一行。**流式期间的 `citation` 事件只作过程展示**，`done` 之后重拉一次会话（编号在 `done` 阶段被压成 1..M，见 §8 第 4 条），不做增量合并 |
| **图片** | 输入区左侧加号 / 直接**粘贴** / 拖进面板，三种入口同一个上传口（`POST /agent/attachments`，**张数不限**）。缩略图带删除；发出的那一轮图片留在气泡上，可点开原图 |
| **分叉** | 用户消息 hover → 复制 / 编辑（编辑 = 新提问挂到原提问的**父**下面）；助手消息 hover → 复制 / 重新生成。有兄弟时消息上方出现 `◀ 2/3 ▶`，点它 `POST /conversations/{id}/branch` 只挪 `head`，一条消息都不删 |
| **圆环 = 压缩** | 圆环显示 `usage.percent`（token 估算 / `agent_context_budget`），点它 = `POST /agent/conversations/{id}/compress`；到 85% 时后端会**自动**先压一次。**手动压缩要先确认**（站内确认框：说清"会压成摘要、原文不删、之后模型只看得到摘要和后面几轮"） |
| **深度思考** | 回答上方一个折叠块：思考中显示「正在深度思考 · 3.4 秒」（跑表取服务端推的 `ms`），第一段正文出现时**自动收起**并停在权威耗时上；点抬头可再展开。历史里的思考块**默认折叠**，等宽纯文本渲染（它是模型的草稿，不是正文） |
| **空态文案** | 从「与大肥鱼一起讨论《论文名》」/「… XX 的实现」/「… XX 为什么有效」/… 里**随机取一条**（XX 由标题与标签现抽关键词），点一下填进输入框，旁边有「换一条」 |
| **划词解释** | 浮动卡片；**由 agent 产出**（`/agent/ask` + `selection`，流式），卡片里是一个小对话线程：可以就这段继续追问（同一 conversation_id），也能「在对话里继续」把选区带进侧栏面板（面板会**自动切到二级页**——一级页没有输入框，留在那儿用户会以为没反应） |
| **划词历史** | 「历史解释（N）」= `source=selection` 的批注（「存为批注」存下来的那些），点一条即可回看 |

**输入框文案就是「请输入问题」**，加号按钮用主题色（`--accent`，自检里拿 `--accent`
的计算值直接比）。文字行一律走 `mdToHtml`，划词消息的 `> 引用` 单独排版成左侧竖条。

**输入区是"一个圆角容器"**（2026-10-03 重做）：容器里只有 textarea、左下角加号、右下角发送；
**模型切换与上下文圆环在容器外**（一行低调明细）。textarea 无边框、无自己的焦点环
（`base.css` 的 `textarea:focus { box-shadow: var(--ring) }` 要显式压掉，否则"框里套框"），
自动增高到 160px 后框内滚；焦点反馈由容器的 `:focus-within` 给。圆环只有 16px 且
**里面不放数字/图标**（数值进 tooltip）。

**所有弹窗走站内组件，已无原生 `confirm()`**：`Modal.vue`（新样式：无分隔线、只有留白层级）
+ `ConfirmModal.vue` + 全局 `askConfirm()`（`stores/confirm.ts` + `components/ConfirmHost.vue`
在 `App.vue` 挂一次）。用模块级单例而不是 provide/inject：调用点包括 `utils/understanding.ts`
这种没有组件上下文的地方。

**图片是"多模态直喂"**：`runtime._user_prompt()` 把附件取成 `BinaryContent` 拼进
user prompt（pydantic-ai 的 OpenAI 适配层会编成 `image_url` 的 data URI）。
实测 `deepseek-flash` 读得懂图。**张数不限**指的是上传不限，单轮进模型的张数有闸
（`agent_attachment_inline_limit`，默认 12）：超出的图仍留在会话里，下一轮还能用。

**图片说明写在 system 侧，不是 user prompt 里**：踩过一次 —— 把"这几张图是用户给的、
别当成论文正文"写在 user prompt 的括号里，模型会**把这句说明原样抄进回答**。
放进 system 侧就没有这个问题（那里的文字是规则，不是"用户说的话"）。

**坏图在上传口就挡掉**：魔数像 PNG、内容却是断的图，能过嗅探、能落盘，然后在**模型端**
变成 `ModelHTTPError 400 ... unsupported image` —— 用户看到的是"模型报错"，而不是"这张图坏了"。
所以 `attachments.save()` 除了嗅探还会用 PIL `verify()` 真解一次。

**划词的锚点是 `row_id` 不是 `block_id`**：阅读器划词拿到的是**正文行 id**（`p004-02`），
而块是解析产物那套命名空间。agent 侧 `runtime._block_of_row()` 按"行 id 里的页号 → 那一页的块列表"
反查出真正的块（与前端引用跳转用同一份映射），查不到就少一层上下文、**不报错**。

**旧链路已删除**：`/api/v1/annotations/from-selection`（后端自己拼 prompt 调 generation provider）
与 `annotation_service.explain_selection` 一起移除；`/annotations/save` 保留 —— 它现在是"把 agent
给出的解释记成批注"的写入端，也就是划词历史的来源。

**压缩 = 多出来的一轮会话**（用户点圆环触发，或超预算自动触发）：

1. 主后端取可见分支的对话（**含上一次的摘要**，压缩是可累加的），调 agent `POST /v1/compress`
   （一个不带工具的裸 agent，`output_type=str`）；
2. 摘要存成一条 `role=summary` 的消息 → 会话里看得见"已压缩"那一轮，原文一条没删；
3. `history_for_agent()` 从**最近一条 summary** 开始算 → 模型不再逐条看到更早的对话；
   `conversation_usage()` 同样只统计摘要之后的部分 → 圆环归零。

摘要提示词要求**保留出处**（写成「第 N 页 §X.X」）但**不许带 `[n]` 编号** —— 编号只对原来那一轮有效，
写进摘要会让后续引用点到错的地方。提示词说了模型也会偶发不遵守（自检抓到过一次 `[1]`），
所以出口还有 `compress.strip_ref_marks()` 兜一道。

**设置页「论文问答」卡片**（`components/settings/AgentModelPanel.vue`）：服务状态、
问答模型（模型名 / provider / 密钥 / 参数摘要 / **是否回落到 generation**）、视觉模型
（说明"对话图片走问答模型、不走它"）、运行限额（轮数 / 检索预算 / 上下文预算 /
自动压缩触发线 / 单轮送图上限）。这一区**只读 + 指路** —— 真正的编辑入口在「模型」页
（密钥、测试连接、预设都在那儿），复制一份表单过来只会让两处状态打架。
`ModelsView` 的类型表也补了 `agent`（"论文问答"），否则用户没法给问答单独注册模型。

---

## 17. 关键事实备忘（实现时速查）

**版本**（已装进 `scenemind`，官方 PyPI 索引）

```
pydantic-ai-slim==2.53.0   pydantic-ai-harness==0.53.0   pydantic-graph==2.53.0
openai==3.23.0             pydantic==2.13.4             httpx==0.28.1
```

- 安装：`pip install --index-url https://pypi.org/simple "pydantic-ai-slim[openai]" "pydantic-ai-harness[skills]"`
  （阿里云镜像滞后，必须显式给官方索引）
- 冒烟脚本：`scripts/agent_env_check.py`（9 项，换机重装必跑，不花钱）

**框架 API**（实测签名）

```python
from pydantic_ai import Agent, RunContext, UsageLimits
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_ai_harness import Skills
from pydantic_ai.messages import BinaryContent, ToolReturn

Agent(model, deps_type=Deps, instructions=f, tools=[…], capabilities=[Skills(…)])
agent.run_stream_events(prompt, deps=…, message_history=…, model_settings=…,
                        usage_limits=UsageLimits(request_limit=6), workspace=…)
Agent.tool(name=…, requires_approval=True, defer_loading=True)   # 写工具 / 延迟加载用
OpenAIChatModel("deepseek-flash", provider=OpenAIProvider(base_url=…, api_key=…))
Skills(directories, *, include=None, exclude=frozenset(), workspace=backend)
ToolReturn(return_value=text, content=[text, BinaryContent(data=…, media_type=…)])
```

**踩过的四个框架坑**（都在代码注释里标了，别改回去）

1. **`ThinkingPartDelta` 也有 `content_delta`** —— 事件里只按"有没有 content_delta"取文本，
   会把思考模式的思维链当正文推给用户（实测 DeepSeek 的英文推理整段出现在回答开头）。
   取文本前必须判 `type(delta).__name__ == "TextPartDelta"`。
2. **`load_skill_libraries` 收的是框架的 `Workspace` 包装，不是裸 backend**
   （内部要调 `workspace.resolve()`）。传裸 backend 会 `AttributeError` → 静默退化成
   "自己扫目录"，症状是"看着有 4 个 skill，其实框架那套没跑"。裸 backend 只能给
   `Skills(workspace=…)`（它内部会 `secondary_workspace()` 包一层）。
3. **`instructions=` 的可调用对象收到的是 `RunContext`**，deps 在 `ctx.deps` 上。
   签名要写 `def f(ctx: RunContext[Deps]) -> str`，写错会在运行期报
   `'RunContext' object has no attribute …`。
4. **`dependencies.usage()`**：轮数的权威数字是 `AgentRunResult.usage().requests`
   （`deps.model_calls` 只数了工具轮，会少 1）。

**主后端端点**（既有 + 为 agent 新增的投影）

```
POST /api/v1/rag/similar              {query|chunk_id, scope: all|cross|paper, paper_id?, chunk_types?, langs?, top_k?}
GET  /api/v1/papers/{id}/markdown      全文 Markdown（派生物）
GET  /api/v1/papers/{id}/blocks/{bid}  块详情（含 bbox，agent 侧会剥掉）
GET  /api/v1/papers/{id}/pages/{n}?dpi= 页图 PNG（不传 dpi = 解析期那张，150dpi）
                                        ★ 新增 dpi 参数：按需渲染并落盘缓存（agent 看公式用 300~400）
GET  /api/v1/papers/{id}/figures/region/{page}/{index}?card=1   插图裁图 600dpi
GET  /api/v1/papers/{id}/brief         论文档案（DocumentBrief，已含 key_claims）
GET  /api/v1/papers/{id}/outline | /formulas | /related
GET  /api/v1/papers/{id}/markdown/translated
GET  /api/v1/papers                    书架
GET  /api/v1/notes | /highlights | /annotations
POST /api/v1/knowledge/index           重建索引（写工具后用）
/api/v1/agent/...                      见 §4.3（对前端 6 条 + 对 agent 12 条）
```

**P1 新增的四条内部投影**（都只认内部 token）：

```
POST /api/v1/agent/kb/search          {query, top_k}  跨库检索（带论文标题）
GET  /api/v1/agent/papers/{id}/formulas?page=&query=  公式清单（同阅读器口径）
GET  /api/v1/agent/papers/{id}/pages?lang=zh          只要译文
GET  /api/v1/agent/library?q=&status=                 书架检索
GET  /api/v1/agent/notes?query=&paper_id=&kind=       笔记/高亮/批注
```

**检索要点**：`retrieval.search(query, mode="qa", scope="paper", paper_id=…)`；
默认候选类型 `unit/formula/figure/table`（正文 chunk 不进候选，避免同内容两条命中）；
类型过滤后为空会自动放开全类型重搜一次。

**预算三条闸**（P1 补了第二条）：

| 闸 | 值 | 管什么 |
|---|---|---|
| 轮数 | `agent_max_rounds=8` | 模型请求次数（`UsageLimits(request_limit=…)`，超限 → `rounds_exhausted`） |
| **检索次数** | `agent_search_budget=5` | **模拟实测发现必须单独设**：跨库问题模型会换着说法连发 10+ 次 `kb_search`（每次都真能捞到新片段，所以"没有新依据"的提示也不拦它），最后轮数用满、正文一个字没写。用尽后检索工具**不打后端**直接回"请基于已有材料作答" |
| 证据量 | `agent_evidence_limit=12` / `agent_evidence_chars=24000` | 进模型的材料总量 |

**轮数用满还没出正文时的兜底**（`runtime._fallback_answer`）：把已登记的证据列成
"以下是已经找到的相关位置"，而不是回「模型没有产出任何正文」—— 明明检索到了不少东西，
让用户什么都看不见是最差的结果。兜底正文也走一遍 `answer_delta`，免得前端有一段空白。

**跨库引用必须带它自己那篇的 `paper_id`**：`RefRegistry.assign(paper_id=…)` 为此留了口子。
不这么做的话前端会跳到**当前论文**的同一页（一个完全不相干的段落）。自检里有一条
"跨库引用的 paper_id 指向正确"专门守它。

**公式没有"编号"字段**：`formulas_payload` 抽出来的是 LaTeX + 上下文，论文自己的 `(3)`
编号在 `context` 里。所以 `formula_get` 只有 `page` / `query` 两个筛选参数，
按编号取只能 `query="(3)"` —— 这一点写进了工具描述，别再加一个假的 `number` 参数。

**"块 → 正文行"锚点**：`anchor_map.build_block_row_map(blocks, rows_by_page)`，
入参块必须是 `{"id","page","text"}` —— **`text` 要与给模型看的那一份一致**。
踩过：传了 `preview` 字段名，映射整片为空，引用退化成"只能跳到页、不能高亮行"。
行数据用 `reader_service.merged_page(paper_id, page)` 单页取，别为主这调全量 `page_products`。

**中文孪生片段**：chunk id 是 `{paper}:{road}:{type}:{块范围}:{lang}`，中文那一份
**road 也从 `a` 变 `b`**（`…:a:unit:p005-01-p005-02:en` ↔ `…:b:unit:p005-01-p005-02:zh`），
所以不能靠替换 `:en`→`:zh` 猜，要按 `(paper_id, block_id, chunk_type)` 回库查，
并且**查不到就不给**（贴错预览比没有预览更坏）。

**输出预算与耗时实测**（Attention 那篇，DeepSeek 思考模式）：
问"核心贡献"≈11s / 4 轮 / 输入 20k tokens；首字约 6~8s（两轮工具调用之后才开始出字）。
