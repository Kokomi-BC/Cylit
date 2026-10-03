"""一轮问答的编排：档案预注入 → 证据闸 → agent 循环 → 引用分配 → 清洗 → SSE 事件。

抄 retain-pdf 的机制落在这里（`docs/agent-接入规划.md` §3）：

- **M2 宿主覆写**：`paper_id` 只从请求里来，工具拿不到、模型改不了；
- **M4 返回瘦投影**：`RefRegistry.model_payload()` 剥内部 id，`to_list()` 给前端全量；
- **M6 清洗**：`StreamingAnswerSanitizer` 对累积原文批量清洗，只推安全前缀；
- **M8 deadline**：总时限只在"还能省事"的位置检查，**落库之后不再检查**。

比参考实现简单的一处：它的证据闸要判断"模型该不该去检索"，我们的检索是**宿主主动**跑的
（`_presearch`），所以闸门就是一次函数调用 —— 没有证据就不进模型，直接给护栏文案。
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

from pydantic_ai import Agent, BinaryContent, RunContext, UsageLimits
from pydantic_ai.exceptions import UsageLimitExceeded
from pydantic_ai.messages import (
    ModelRequest, ModelResponse, SystemPromptPart, TextPart, UserPromptPart,
)
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_ai_harness import Skills

from backend.agent import prompts
from backend.agent.citations import RefRegistry
from backend.agent.config import AgentConfig
from backend.agent.deps import Deps
from backend.agent.host import HostClient, HostError
from backend.agent.sanitize import (
    StreamingAnswerSanitizer, referenced_citations, strip_foreign_images,
)
from backend.agent.tools import AGENT_TOOLS
from backend.agent.trace import StepLog
from backend.agent.workspace import ReadOnlyWorkspace
from backend.core.logging import get_logger

log = get_logger(__name__)

#: 检索完全没有命中、档案也没有实质内容 → 不进模型，直接给这句话（规划 §7.6）
GUARDRAIL_NO_EVIDENCE = (
    "这篇论文里没有检索到与这个问题相关的内容，我无法基于它可靠回答。"
    "可以换一个更贴近论文用词的说法（比如直接用它自己的术语）再问一次。"
)

#: 低相关度提醒（软信号，给模型看的）
EVIDENCE_NOTE = (
    "【本轮检索提示】检索回来的材料相关度偏低（最高 {score:.2f}，参考线 {floor:.2f}）。"
    "它们很可能答不了这个问题 —— 请先判断材料里到底有没有依据：有就只依据它回答，"
    "没有就直说这篇论文里没有相关内容，不要用这些不相关片段凑答案。"
)

#: 压缩摘要在历史里挂的抬头（见 `_history`）
SUMMARY_LABEL = "【之前对话的压缩摘要（更早的内容都在这里）】\n"

#: 模型配置缓存时长（密钥轮换后最多这么久生效，省掉每轮一次 HTTP）
MODEL_CONFIG_TTL = 60.0


@dataclass
class AskRequest:
    paper_id: str
    question: str = ""
    conversation_id: str = ""
    parent_id: str | None = None
    #: 可见分支的历史（主后端从 MySQL 取好传进来，agent 不碰数据库）
    history: list[dict] = field(default_factory=list)
    model_id: str | None = None
    #: 划词解释：``{text, block_id?, page?}``。有它时 `question` 可以为空（默认"解释这段内容"）
    selection: dict | None = None
    #: 用户上传/粘贴的图片（``[{attachment_id, name, mime, bytes}]``，主后端已落库）。
    #: **直接多模态喂给问答模型**（实测 deepseek-flash 吃图）：所以这里传的是元数据，
    #: 真正的字节在 `_user_prompt` 里按 id 取 —— agent 仍然不碰文件系统。
    attachments: list[dict] = field(default_factory=list)


@dataclass
class RunState:
    """一次运行的收尾状态（由 `_stream_model` 填充，`_finalize` 消费）。"""

    text: str = ""
    rounds: int = 0
    incomplete_reason: str | None = None
    usage: dict = field(default_factory=dict)
    error: str | None = None
    #: 思考模式的思维链（**单独一条通道**，永远不进正文、也不进下一轮历史）。
    #: 为什么不丢掉：模型想什么对用户是有用的上下文；为什么不能混进正文：
    #: `ThinkingPartDelta` 与 `TextPartDelta` 都有 `content_delta`，混了就是把推理当答案发出去。
    thinking: str = ""
    #: 思考耗时（毫秒）：从**发起模型调用**到第一段正文出现。
    #: 注意它包含工具往返（模型会"想 → 调工具 → 接着想"），因为用户等的就是这一段；
    #: 只统计纯粹"在想"的片段既算不准（工具等待与思考在流里没有干净的分界），
    #: 也会给出一个和体感不符的小数字。没有正文（轮数用尽/报错）时取到流结束为止。
    thinking_ms: int = 0
    #: 模型调用开始的时刻（`_translate` 用它算上面那个毫秒数）
    model_t0: float = 0.0
    #: 已经开始输出正文（思考阶段结束的判据）
    answer_started: bool = False
    #: 这一轮的**过程轨迹**（多轮思考 + 工具调用，按发生顺序；见 `agent/trace.py`）。
    #: 它是"这一轮到底干了什么"的唯一形状：SSE 的 `done` 整份下发，同时落库。
    log: StepLog = field(default_factory=StepLog)
    #: 整轮耗时（毫秒，从进 `ask()` 到出 `done`）：前端抬头那句"已完成，用时 …"
    elapsed_ms: int = 0


# --------------------------------------------------------------------------
# 模型配置 → pydantic-ai
# --------------------------------------------------------------------------


def model_settings_from(parameters: dict) -> dict:
    """把主后端存的 ``parameters`` 翻译成 pydantic-ai 的 model_settings。

    与 `providers/generation/openai_compatible.py` 的口径保持一致（那是翻译链路验证过的）：
    DeepSeek 的厂商参数走 ``extra_body``；思考模式下 ``temperature`` 恒不生效要剥掉，
    ``top_p`` 生效但下限 0.95；关闭思考时 ``top_p`` 恒为 1.0，同样剥掉 ——
    留着只会让人以为"设了就在生效"。
    """
    p = dict(parameters or {})
    extra = dict(p.get("extra_body") or {})
    thinking = p.get("thinking")
    if thinking in ("enabled", "disabled") and "thinking" not in extra:
        extra["thinking"] = {"type": thinking}
    disabled = (extra.get("thinking") or {}).get("type") == "disabled"

    out: dict = {}
    if p.get("max_tokens"):
        out["max_tokens"] = int(p["max_tokens"])
    if extra:
        out["extra_body"] = extra
    if p.get("reasoning_effort") and not disabled:
        out["openai_reasoning_effort"] = str(p["reasoning_effort"])
    if disabled:
        if p.get("temperature") is not None:
            out["temperature"] = float(p["temperature"])
    elif p.get("top_p") is not None:
        out["top_p"] = max(0.95, min(1.0, float(p["top_p"])))
    return out


def build_model(model_cfg: dict):
    """``{provider, model_name, base_url, api_key}`` → pydantic-ai 模型对象。

    目前只走 OpenAI 兼容协议（DeepSeek / 通义 / 智谱 / Ollama / vLLM 都是这一条），
    这也是本仓 `factory.py` 已有的口径；要接 Anthropic 原生协议时在这里加一条分支。
    """
    model_name = (model_cfg.get("model_name") or "").strip()
    if not model_name:
        raise HostError("主后端没有给出可用的问答模型（model_name 为空）")
    base_url = (model_cfg.get("base_url") or "").strip() or None
    api_key = (model_cfg.get("api_key") or "").strip() or None
    provider = OpenAIProvider(base_url=base_url, api_key=api_key or "not-needed")
    return OpenAIChatModel(model_name, provider=provider)


def instructions_for(ctx: RunContext[Deps]) -> str:
    """动态指令：pydantic-ai 传进来的是 **RunContext**，deps 在 ``ctx.deps`` 上。

    （踩过一次：直接写成 ``instructions=prompts.build_instructions`` 会拿 Deps 当上下文，
    运行期报 ``'RunContext' object has no attribute 'archive_text'``；注解写清楚
    `RunContext[Deps]` 才能让框架认出这是"要上下文的指令函数"。）
    """
    return prompts.build_instructions(ctx.deps)


# --------------------------------------------------------------------------
# 运行时
# --------------------------------------------------------------------------


class AgentRuntime:
    """进程级单例：workspace / Skills / 模型配置缓存都挂在这里。"""

    def __init__(self, cfg: AgentConfig, host: HostClient) -> None:
        self.cfg = cfg
        self.host = host
        self._workspace = ReadOnlyWorkspace(cfg.skills_dir)
        self._skills = Skills(str(cfg.skills_dir), workspace=self._workspace)
        self._model_cfg: dict = {}
        self._model_at = 0.0
        self._model_lock = asyncio.Lock()

    # ---------------- 资源 ----------------
    def workspace(self) -> ReadOnlyWorkspace:
        return self._workspace

    def skills(self) -> Skills:
        return self._skills

    def reload_skills(self) -> None:
        """重建 Skills 能力。

        P0 每轮问答都会重新构造 Agent（见 `_build_agent`），所以 skill 目录的改动
        **下一轮就生效**；这个接口是给"想立刻确认已加载"的场景用的。
        """
        self._skills = Skills(str(self.cfg.skills_dir), workspace=self._workspace)

    async def model_config(self, force: bool = False) -> dict:
        if not force and self._model_cfg and (time.time() - self._model_at) < MODEL_CONFIG_TTL:
            return self._model_cfg
        async with self._model_lock:
            if not force and self._model_cfg and (time.time() - self._model_at) < MODEL_CONFIG_TTL:
                return self._model_cfg
            self._model_cfg = await self.host.model_config()
            self._model_at = time.time()
            return self._model_cfg

    async def skill_list(self) -> list[dict]:
        """已加载的 skill（名 / 描述 / 正文长度 / 来源）。

        走框架自己的加载器取"事实"（它才是真正往提示词里注入的那一份），失败时退回自己扫目录
        —— 诊断接口不该因为框架版本变了就整个 500。

        ⚠️ `load_skill_libraries` 收的是**框架的 `Workspace` 包装**，不是裸 backend
        （它内部会调 `workspace.resolve(...)`；直接传裸 backend 会 AttributeError ——
        那正是"看着有 4 个 skill，其实是退化成自己扫目录"的原因）。
        `source` 字段就是给自检看的：必须是 `framework`。
        """
        try:
            from pydantic_ai.workspaces import Workspace
            from pydantic_ai_harness.skills._loader import load_skill_libraries

            defs = await load_skill_libraries(Workspace(self._workspace),
                                              [str(self.cfg.skills_dir)],
                                              include=None, exclude=frozenset())
            return [{"name": d.name, "description": (d.description or "").strip(),
                     "chars": len(d.body or ""), "source": "framework"} for d in defs]
        except Exception as exc:  # noqa: BLE001
            log.warning("框架加载器不可用，退回自扫目录：%s", exc)
            return self._scan_skills()

    def _scan_skills(self) -> list[dict]:
        out: list[dict] = []
        root = self.cfg.skills_dir
        if not root.is_dir():
            return out
        for child in sorted(root.iterdir()):
            skill_md = child / "SKILL.md"
            if not (child.is_dir() and skill_md.is_file()):
                continue
            text = skill_md.read_text(encoding="utf-8", errors="ignore")
            name, desc = child.name, ""
            if text.startswith("---"):
                head = text.split("---", 2)[1]
                for line in head.splitlines():
                    key, _, value = line.partition(":")
                    if key.strip() == "name":
                        name = value.strip() or name
                    elif key.strip() == "description":
                        desc = value.strip()
            out.append({"name": name, "description": desc, "chars": len(text),
                        "source": "scan"})
        return out

    # ---------------- 组装 agent ----------------
    def _build_agent(self, model_cfg: dict) -> Agent[Deps]:
        return Agent(
            build_model(model_cfg),
            deps_type=Deps,
            instructions=instructions_for,
            tools=list(AGENT_TOOLS),
            capabilities=[self._skills],
        )

    @staticmethod
    def _history(req: AskRequest) -> list:
        """把主后端给的历史转成 pydantic-ai 消息（P0 通常为空）。"""
        msgs: list = []
        for item in req.history or []:
            content = str(item.get("content") or "").strip()
            if not content:
                continue
            role = item.get("role")
            if role == "summary":
                # 压缩摘要（`api/agent.py` 的 compress → `role="summary"`）是"压缩点之前所有对话"
                # 的等价物。这里以前只有 user/assistant 两支，摘要被静默丢掉 —— 用户一点压缩，
                # 模型就再也看不到摘要，记忆直接断在压缩点（"不认上面的对话"最硬的一条）。
                # 用 SystemPromptPart 而不是 UserPromptPart：摘要是模型写的要点，不是用户说的话。
                msgs.append(ModelRequest(parts=[SystemPromptPart(SUMMARY_LABEL + content)]))
            elif role == "user":
                msgs.append(ModelRequest(parts=[UserPromptPart(content)]))
            elif role == "assistant":
                msgs.append(ModelResponse(parts=[TextPart(content)]))
        return msgs

    # ---------------- 主流程 ----------------
    async def ask(self, req: AskRequest) -> AsyncIterator[dict]:
        """把一轮问答变成 SSE 事件流（**不含** SSE 帧格式，见 `service.py`）。"""
        t0 = time.perf_counter()
        deadline = t0 + self.cfg.deadline_seconds
        deps = Deps(cfg=self.cfg, host=self.host, paper_id=req.paper_id,
                    registry=RefRegistry(req.paper_id))
        # 图片张数写进 deps：`prompts.image_block` 把它渲染进**本轮备注**（拼在用户消息里）。
        # 为什么不能只写进 system：这一轮的张数每轮都可能变，混进 system 会把
        # "system + 全部历史"整段前缀缓存打掉（见 `prompts.build_instructions`）；
        # 而"图片是用户材料、别当论文正文"那类**规则**仍常驻 system 的 IMAGE_RULES。
        if req.attachments:
            from backend.core.config import get_settings

            inline = max(1, int(get_settings().agent_attachment_inline_limit))
            deps.attachments = len(req.attachments)
            deps.attachments_shown = min(len(req.attachments), inline)
        state = RunState()
        try:
            yield {"type": "stage", "stage": "routing", "message": "正在准备论文档案…"}
            state.log.stage("正在准备论文档案…")
            archive = await self.host.archive(req.paper_id)
            deps.title = str(archive.get("title") or "")
            deps.page_total = int(archive.get("page_total") or 0)
            deps.archive_text = prompts.archive_block(
                self.cfg.archive_chars, archive.get("brief") or {},
                archive.get("outline") or [], title=deps.title,
                page_total=deps.page_total)

            yield {"type": "stage", "stage": "retrieving", "message": "正在这篇论文里检索…"}
            state.log.stage("正在这篇论文里检索…")
            found = await self._presearch(req, deps, state)
            # 划词提问：把选区所在的块登记成 ref，模型就能自己展开上下文（见 prompts.SELECTION）
            if req.selection:
                await self._prepare_selection(req, deps)
            # 档案算不算"证据"：只有**实质内容**（领域/主要论断）才算。章节大纲不算 ——
            # 光有一串小标题答不了"核心贡献是什么"，拿它当依据等于给模型一个错觉。
            brief = archive.get("brief") or {}
            archive_evidence = bool(brief.get("domain") or brief.get("key_claims"))

            yield {"type": "session", "conversation_id": req.conversation_id,
                   "paper_id": req.paper_id, "runtime": "pydantic-ai",
                   "skills": [s["name"] for s in await self.skill_list()],
                   "content_source": ("selection" if deps.selection
                                      else ("image" if req.attachments
                                            else ("search" if found
                                                  else ("archive" if archive_evidence
                                                        else "none"))))}

            # 划词选区本身就是证据（用户指着这段问的），图片同理（用户自己给的材料），
            # 都不能因为检索没命中就挡回去
            if (not found and not archive_evidence and not deps.selection
                    and not req.attachments):
                state.text = GUARDRAIL_NO_EVIDENCE
                state.incomplete_reason = "no_evidence"
                state.elapsed_ms = int((time.perf_counter() - t0) * 1000)
                async for ev in self._finalize(req, deps, state):
                    yield ev
                return

            yield {"type": "stage", "stage": "reading", "message": "正在阅读并组织回答…"}
            state.log.stage("正在阅读并组织回答…")
            async for ev in self._stream_model(req, deps, state, deadline, t0):
                yield ev
            state.elapsed_ms = int((time.perf_counter() - t0) * 1000)
            async for ev in self._finalize(req, deps, state):
                yield ev
        except HostError as exc:
            yield {"type": "error", "code": exc.code or "HOST_ERROR",
                   "message": exc.message, "retryable": True}
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.exception("问答失败：%s", exc)
            yield {"type": "error", "code": "INTERNAL",
                   "message": f"{type(exc).__name__}: {exc}"[:300], "retryable": False}

    # ---------------- 划词选区 ----------------
    @staticmethod
    def _page_of_row(row_id: str) -> int:
        """行 id（``p004-02``）里的页号；不是这个形状就 0。"""
        parts = str(row_id).split("-")
        if len(parts) < 2 or not parts[0].startswith("p"):
            return 0
        try:
            return int(parts[0][1:])
        except ValueError:
            return 0

    async def _block_of_row(self, paper_id: str, row_id: str) -> str:
        """把**正文行 id**（``p004-02``）解析成**解析块 id**（老路径，只对没转录的页有效）。

        为什么要这一步：阅读器划词给的是**行 id**，而解析块是另一套命名空间
        （``attentionisa-p04-b02``）。行 id 里带着页号，拿那一页的块列表按 `row_id` 反查即可
        —— 与前端"引用跳转"用的是同一份映射（`anchor_map` 算出来的那个）。

        ⚠️ **转录过的页查不到**：那一页的投影现在是页级产物的行（`id` 就是行 id、没有
        `block_id`）。所以调用方要**先试 `_selection_row`**，这条只是兜底。
        """
        page = self._page_of_row(row_id)
        if not page:
            return ""
        try:
            data = await self.host.pages(paper_id, str(page))
        except HostError:
            return ""
        for page_payload in data.get("pages") or []:
            for block in page_payload.get("blocks") or []:
                if (str(block.get("row_id") or "") == str(row_id)
                        and str(block.get("source") or "") != "transcript"):
                    return str(block.get("id") or "")
        return ""

    async def _selection_row(self, paper_id: str, row_id: str,
                             page: int | None = None) -> dict:
        """选区落在页级产物（转录/翻译）的某一行上时，取回**这一行的整段**。

        ``page`` 优先用前端给的（PDF 划词没有行锚点），其次从行 id 里解出页号。
        """
        page_no = int(page or 0) or self._page_of_row(row_id)
        if not page_no or not row_id:
            return {}
        try:
            return await self.host.row(paper_id, page_no, str(row_id))
        except HostError:
            return {}

    async def _prepare_selection(self, req: AskRequest, deps: Deps) -> None:
        """把选区落在的段落登记成 ref，模型据此展开上下文。

        为什么值得为它多一次 HTTP：选区经常是半句话（"这个 $d_k$ 是干什么的"），
        只把选区文字塞给模型，它看不到所在段落，解释就会飘。登记成 ref 之后
        `block_get` 那套现成的能力直接复用，不用给划词单开一条链路。

        两条路（2026-10-03）：**转录行**（页级产物的行 id，现在读页给的就是它）按行取整段；
        没转录的页仍是解析块（行 id → block id → 块接口）。
        """
        from backend.agent.tools import _clip, _locate

        selection = req.selection or {}
        text = str(selection.get("text") or "").strip()
        if not text:
            return
        deps.selection = text
        deps.selection_question = str(selection.get("question") or "").strip()
        row_id = str(selection.get("row_id") or "")
        block_id = str(selection.get("block_id") or "")
        page_hint = int(selection.get("page") or 0)
        # 位置先记下：原 PDF 划词没有块锚点（拿不到 ref），但**页号本身就是位置线索**，
        # 提示词里要能说出来（见 `prompts.selection_block`）。
        deps.selection_page = page_hint or None

        if row_id:
            row = await self._selection_row(req.paper_id, row_id, page_hint)
            if row:
                page = int(row.get("page") or page_hint or 0) or None
                body = str(row.get("text") or "").strip()
                citation = deps.registry.assign(
                    key=f"row:{row_id}", kind="block", page=page, row_id=row_id,
                    snippet=_clip(body or text),
                    label=_locate(page, None, row.get("type") or ""), ids=(row_id,))
                deps.selection_ref = citation.ref
                deps.selection_page = citation.page
                return

        if not block_id and row_id:
            block_id = await self._block_of_row(req.paper_id, row_id)
        if not block_id:
            return
        try:
            data = await self.host.block(req.paper_id, block_id)
        except HostError as exc:
            log.warning("划词取块失败（解释仍可继续，只是少了上下文）：%s", exc.message)
            return
        page = int(data.get("page") or page_hint or 0) or None
        citation = deps.registry.assign(
            key=f"block:{block_id}", kind="block", page=page,
            section=data.get("section") or None, block_id=block_id,
            snippet=_clip(str(data.get("text") or text)),
            label=_locate(page, data.get("section"), data.get("type") or ""),
            ids=(block_id,), row_id=row_id or None)
        deps.selection_ref = citation.ref
        deps.selection_page = citation.page

    # ---------------- 证据闸 ----------------
    async def _presearch(self, req: AskRequest, deps: Deps, state: RunState) -> bool:
        """宿主主动检索一次，把命中登记进引用表（这是"证据"的定义之一）。

        为什么要宿主来跑而不是等模型调 `paper_search`：证据闸要能在**进模型之前**判断
        "这次有没有依据"。交给模型就变成"模型自己决定要不要有依据"，护栏文案就没地方放了。

        **阈值只在真重排模型上生效**：向量检索永远返回 top-k，裸看"有没有命中"等于没闸；
        而启发式降级那套是词重合度，量纲与重排分完全不同，拿它比阈值会把问答全挡掉。
        """
        try:
            data = await self.host.search(req.paper_id, req.question,
                                          top_k=self.cfg.search_top_k)
        except HostError as exc:
            log.warning("预检索失败（不算致命，档案仍可用）：%s", exc.message)
            return False
        items = list(data.get("items") or [])[:self.cfg.evidence_limit]
        deps.page_total = deps.page_total or int(data.get("page_total") or 0)
        top_score = float(data.get("top_score") or 0.0)
        degraded = bool(data.get("rerank_degraded", True))
        deps.trace.append({
            "tool": "__presearch__", "status": "ok", "ms": 0, "detail": "",
            "args": {"returned": len(items), "top_score": round(top_score, 4),
                     "rerank_degraded": degraded, "floor": self.cfg.evidence_floor},
        })
        # 过程轨迹里也留一条：用户看到的"它一上来就检索了"就是这一步（工具步骤 + 命中条数）
        step = state.log.tool_start("__presearch__", round=0,
                                    digest=f"命中 {len(items)} 条")
        step["status"] = "done"
        step.pop("_t0", None)
        step["ms"] = 0
        if not items:
            return False
        if not degraded and top_score < self.cfg.evidence_floor:
            # ⚠️ **不在这里拦**：实测这个阈值不可靠（同一个无关问题，问句写长一点
            # 重排分就从 0.44 涨到 0.68，而真问题最低也只有 0.70）—— 拿它当硬闸会把
            # 真问题误杀。所以只把信号交给模型，由它判断"材料里到底有没有依据"。
            deps.evidence_note = EVIDENCE_NOTE.format(score=top_score,
                                                     floor=self.cfg.evidence_floor)
            log.info("检索相关度偏低（%.3f < %.2f），已提示模型自行判断",
                     top_score, self.cfg.evidence_floor)
        for raw in items:
            cit = _register_chunk_plain(deps, raw)
            log.debug("预检索命中 ref=%s page=%s", cit.ref, cit.page)
        # 宿主这次预检索也占一格检索预算（它是真打了一次后端）
        deps.search_calls += 1
        return True

    # ---------------- 模型流 ----------------
    async def _stream_model(self, req: AskRequest, deps: Deps, state: RunState,
                            deadline: float, t0: float) -> AsyncIterator[dict]:
        model_cfg = await self.model_config()
        if not model_cfg.get("usable"):
            # 明确失败，不要让 mock 模型编一段像样的答案（那比报错更坏）
            which = "generation（回落）" if model_cfg.get("fallback_to_generation") else "agent"
            yield {"type": "error", "code": "MODEL_NOT_CONFIGURED",
                   "message": f"问答模型没有配好（当前用的是 {which}，provider="
                              f"{model_cfg.get('provider')}）。请到设置页给「问答」配一个"
                              f"支持工具调用的模型并填好 API Key。",
                   "retryable": False}
            return
        agent = self._build_agent(model_cfg)
        settings = model_settings_from(model_cfg.get("parameters") or {})
        sanitizer = StreamingAnswerSanitizer(deps.registry)
        limits = UsageLimits(request_limit=self.cfg.max_rounds)
        sent_refs: set[int] = set()
        prompt = await self._user_prompt(req, deps)
        # 思考耗时的起点：**模型调用**发起那一刻（档案预注入与检索不算思考，
        # 它们在前端有各自的过程提示，混进来会让"思考 8 秒"变成"检索 6 秒 + 思考 2 秒"）
        state.model_t0 = time.perf_counter()

        try:
            async with agent.run_stream_events(
                    prompt, deps=deps, message_history=self._history(req) or None,
                    model_settings=settings, usage_limits=limits,
                    workspace=self._workspace) as events:
                iterator = events.__aiter__()
                while True:
                    try:
                        event = await asyncio.wait_for(iterator.__anext__(),
                                                      timeout=self.cfg.heartbeat_seconds)
                    except StopAsyncIteration:
                        break
                    except asyncio.TimeoutError:
                        if time.perf_counter() > deadline:
                            state.incomplete_reason = "deadline"
                            break
                        yield {"type": "heartbeat",
                               "elapsed_ms": int((time.perf_counter() - t0) * 1000)}
                        continue
                    for out in self._translate(event, deps, state, sanitizer, sent_refs):
                        yield out
                    if time.perf_counter() > deadline:
                        state.incomplete_reason = "deadline"
                        break
        except UsageLimitExceeded as exc:
            # 轮数超限是**正常终止**（规划 §11 的 rounds_exhausted），不是错误
            log.info("轮数超限终止：%s", exc)
            state.incomplete_reason = "rounds_exhausted"
            state.rounds = self.cfg.max_rounds
        except HostError as exc:
            state.incomplete_reason = "error"
            state.error = exc.message
            yield {"type": "error", "code": exc.code or "HOST_ERROR",
                   "message": exc.message, "retryable": True}
            return
        finally:
            tail = sanitizer.flush()
            if tail:
                yield {"type": "answer_delta", "text": tail}
                # ⚠️ 清洗器**留着一段尾巴**（怕半个引用标记被推出去），收尾时它才吐出来。
                # 这段也必须进轨迹：前端是按轨迹的正文段渲染回答的，漏了它回答就少了最后一段
                # （用户 2026-10-03 报的"正文被截断"就是这个）。
                state.log.text(tail, round=deps.model_calls + 1)
            state.text = sanitizer.clean
            # 只想不答（轮数用尽 / 报错）：耗时算到流结束，别留个 0 看起来像没思考
            if state.thinking and not state.thinking_ms:
                state.thinking_ms = int((time.perf_counter() - state.model_t0) * 1000)

    def _thinking_text(self, text: str | None, state: RunState, deps: Deps) -> list[dict]:
        """思维链增量（思考模式）。"""
        if not text:
            return []
        state.thinking += text
        # 过程轨迹：同一轮的思考**并成一条**（一轮几百个增量块，一块一条会刷屏）
        state.log.thinking(text, round=deps.model_calls + 1)
        return [{"type": "thinking_delta", "text": text,
                 "ms": int((time.perf_counter() - state.model_t0) * 1000)}]

    def _answer_text(self, text: str | None, state: RunState, deps: Deps,
                     sanitizer: StreamingAnswerSanitizer) -> list[dict]:
        """正文增量：首次进正文时把思考阶段的跑表停下，其余交给清洗器。"""
        if not text:
            return []
        out: list[dict] = []
        if not state.answer_started:
            state.answer_started = True
            if state.thinking:
                # 思考阶段到此为止：把**权威耗时**推给前端（让它把跑表停下），
                # 前端自己那份计时只是"看得见的跑表"，以这条为准
                state.thinking_ms = int((time.perf_counter() - state.model_t0) * 1000)
                out.append({"type": "thinking", "status": "done",
                            "ms": state.thinking_ms})
        clean = sanitizer.feed(text)
        if clean:
            # 过程轨迹：正文也按轮切开（思考 → 说一句 → 调工具 → 接着说，是交织的）
            state.log.text(clean, round=deps.model_calls + 1)
            out.append({"type": "answer_delta", "text": clean})
        return out

    def _translate(self, event: Any, deps: Deps, state: RunState,
                   sanitizer: StreamingAnswerSanitizer,
                   sent_refs: set[int]) -> list[dict]:
        """pydantic-ai 事件 → 我们的 SSE 事件（一个事件可能产出 0 条或多条）。"""
        name = type(event).__name__
        # ⚠️ **新段的第一块内容装在 `PartStartEvent.part` 里**（不是 Delta）：框架建新
        # `TextPart` 时直接 `TextPart(content=…)` 再 yield PartStartEvent，只认 Delta
        # 事件就会把这整块吞掉 —— 表现是"工具调用之后那段回答缺开头"
        # （实测把「表 1 里 TURBOQUANT…」吞成「 1 里 TURBOQUANT…」，另一轮吞掉「44」）。
        # 思考段同理（`ThinkingPart(content=…)`）。
        if name == "PartStartEvent":
            part = getattr(event, "part", None)
            kind = type(part).__name__
            if kind == "ThinkingPart":
                return self._thinking_text(getattr(part, "content", None), state, deps)
            if kind == "TextPart":
                return self._answer_text(getattr(part, "content", None), state, deps, sanitizer)
            return []
        if name == "PartDeltaEvent":
            delta = getattr(event, "delta", None)
            kind = type(delta).__name__
            # ⚠️ `ThinkingPartDelta` **也有** `content_delta` 字段：不判类型就会把思考模式下的
            # 思维链当正文推给用户（实测：DeepSeek 思考模式的英文推理整段出现在回答开头）。
            # 所以这里**分两支**：思考走 `thinking_delta`（前端折叠显示），正文走 `answer_delta`。
            if kind == "ThinkingPartDelta":
                return self._thinking_text(getattr(delta, "content_delta", None), state, deps)
            if kind != "TextPartDelta":
                return []
            return self._answer_text(getattr(delta, "content_delta", None), state, deps, sanitizer)
        if name == "FunctionToolCallEvent":
            part = getattr(event, "part", None)
            deps.note_model_call()
            tool = getattr(part, "tool_name", "?")
            digest = _digest(getattr(part, "args", None))
            state.log.tool_start(tool, round=deps.model_calls, digest=digest)
            return [{"type": "tool", "round": deps.model_calls,
                     "tool": tool, "status": "start", "args_digest": digest}]
        if name == "FunctionToolResultEvent":
            tool = getattr(getattr(event, "part", None), "tool_name", "?")
            step = state.log.tool_done(tool, round=deps.model_calls)
            out: list[dict] = [{"type": "tool", "round": deps.model_calls,
                                "tool": tool, "status": "done", "args_digest": "",
                                "ms": step.get("ms")}]
            for cit in deps.registry.all():
                if cit.ref not in sent_refs:
                    sent_refs.add(cit.ref)
                    out.append({"type": "citation", **cit.to_dict()})
            return out
        if name == "AgentRunResultEvent":
            # 轮数与用量以框架的快照为准（`deps.model_calls` 只数了工具轮，会少 1）
            result = getattr(event, "result", None)
            usage = getattr(result, "usage", None)
            try:
                snapshot = usage() if callable(usage) else usage
                state.rounds = int(getattr(snapshot, "requests", 0) or 0) or state.rounds
                state.usage = {
                    "requests": state.rounds,
                    "input_tokens": int(getattr(snapshot, "input_tokens", 0) or 0),
                    "output_tokens": int(getattr(snapshot, "output_tokens", 0) or 0),
                    # provider 的前缀缓存命中量（DeepSeek 命中部分约 0.1× 计费）。
                    # 问答这条链以前没有这个数，"缓存到底有没有命中"只能靠猜 —— 现在它跟着
                    # `__usage__` 一起进 `tool_trace`，可以在会话消息里直接对账。
                    "cache_read_tokens": int(getattr(snapshot, "cache_read_tokens", 0) or 0),
                }
                deps.trace.append({"tool": "__usage__", "args": state.usage,
                                   "status": "ok", "ms": 0, "detail": ""})
            except Exception:  # noqa: BLE001
                pass
        return []

    async def _user_prompt(self, req: AskRequest, deps: Deps):
        """本轮的 user prompt：纯文本，或``[文本, 图片…]``（多模态）。

        **"本轮备注"拼在这里**（选区编号/原文、图片张数、检索相关度）：它们每轮都可能不一样，
        放进 system 会把"system + 全部历史"这段前缀整段打掉（见 `prompts.build_instructions`），
        拼在最后一条用户消息里则前面那一大段照旧命中。

        图片走 ``BinaryContent`` —— pydantic-ai 的 OpenAI 适配层会把它编成
        ``image_url`` 的 data URI，与手写多模态请求等价（实测问答模型读得懂）。

        为什么只送前 ``agent_attachment_inline_limit`` 张：用户要求"张数不限"是指
        **能传多少**，不是说一次请求塞 50 张图 —— 那会被厂商按体积直接拒掉。
        多出来的图仍留在历史里（下一轮还能问），只是这一轮不进去，并在备注里说明。
        """
        from backend.core.config import get_settings

        question = (req.question or "").strip()
        if not question and req.selection:
            # 划词但用户没打字：默认就是"解释这段"。有追问时用追问当 prompt。
            question = "请解释我选中的这段内容。"
        notes = prompts.turn_notes(deps)
        if notes:
            question = f"{notes}\n\n【用户这一轮的问题】\n{question}"
        if not req.attachments:
            return question

        limit = max(1, int(get_settings().agent_attachment_inline_limit))
        shown = req.attachments[:limit]
        text = question or "请看这些图片。"
        parts: list = [text]
        for meta in shown:
            try:
                data, mime = await self.host.attachment(str(meta.get("attachment_id") or ""))
            except HostError as exc:
                log.warning("取附件失败（这一张跳过）：%s", exc.message)
                continue
            if data:
                parts.append(BinaryContent(data=data, media_type=mime))
        return parts

    # ---------------- 收尾 ----------------
    @staticmethod
    def _fallback_answer(deps: Deps, state: RunState) -> str:
        """轮数用满 / 模型一个字都没吐时的兜底：把**已经找到的证据**列出来。

        为什么要有它：实测跨库问题会出现"轮数用满、正文为空"，用户拿到的是
        「模型没有产出任何正文」—— 明明检索到了不少东西，却什么都没看见。
        兜底回答虽然不如模型写的，但它给出**已经找到什么**，用户可以据此把问题问小一点。
        没有证据时返回空串（那就该走真正的 error，不要编）。
        """
        citations = deps.registry.all()
        if not citations:
            return ""
        reason = {"rounds_exhausted": "轮数用满", "deadline": "超时"}.get(
            state.incomplete_reason or "", "未能完成")
        lines = [f"这轮没能在限定预算内整理出完整回答（{reason}，问题可能太大了）。"
                 f"以下是已经找到的相关位置，可以作为线索："]
        for c in citations[:6]:
            where = c.label or (f"第 {c.page} 页" if c.page else "相关片段")
            lines.append(f"- [{c.ref}] {where}：{(c.snippet or '')[:120]}")
        lines.append("把问题拆小一点（比如指定某一节、某个概念）再问一次通常能答出来。")
        return "\n".join(lines)

    @staticmethod
    def _renumber(text: str, citations: list) -> tuple[str, list[dict]]:
        """把引用编号压成 ``1..M``，并同步改写正文里的标记。

        为什么不留着 registry 的原始编号：编号是按"工具返回顺序"发的，模型只用了其中一小撮，
        用户看到的引用表就成了 ``[63][49][33]`` 这种带洞的号 —— 表里 4 条、正文写着 ``[63]``，
        谁也读不懂。压成 1..M 之后"正文 ``[n]`` ↔ 引用表第 n 条"是一一对应的。

        ⚠️ 已知代价：论文自己的方括号编号（``as in [12]``）**只在该号恰好是本次用到的
        ref 时**才会被改写 —— 这正是规划 R6 说的撞车，前端只把能匹配到 citation 的 ``[n]``
        变按钮，改不掉的按普通文本留着。
        """
        order = [c.ref for c in citations]
        mapping = {old: i for i, old in enumerate(order, start=1)}

        def repl(match: "re.Match[str]") -> str:
            new = mapping.get(int(match.group(1)))
            return f"[{new}]" if new else match.group(0)

        new_text = re.sub(r"\[(\d{1,3})\]", repl, text) if mapping else text
        out: list[dict] = []
        for old in order:
            data = next(c for c in citations if c.ref == old).to_dict()
            data["ref"] = mapping[old]
            out.append(data)
        return new_text, out

    async def _finalize(self, req: AskRequest, deps: Deps,
                        state: RunState) -> AsyncIterator[dict]:
        # ⚠️ 不做 strip：`done.answer` 必须与流式增量的拼接**逐字一致**（自检里就是这么断言的），
        # 少一个首尾空格就会让"思维链有没有漏进正文"这条检查变成噪声。
        text = state.text or ""
        if not text.strip():
            text = self._fallback_answer(deps, state)
            if text:
                # 兜底正文也要走一遍 answer_delta：否则"流式看到的"和 `done` 的不一致，
                # 前端靠 `done` 覆盖才能显示它 —— 那中间会有一段什么都不显示的空白。
                yield {"type": "answer_delta", "text": text}
                state.log.text(text, round=deps.model_calls + 1)   # 轨迹里也要有它
        if not text.strip():
            yield {"type": "error", "code": "EMPTY_ANSWER",
                   "message": "模型没有产出任何正文，请重试（可换一个更具体的问题）。",
                   "retryable": True}
            return
        citations = referenced_citations(text, deps.registry)
        # 图片先过闸：只留本论文自己的受控裁图地址（§12 规则 5）
        text = strip_foreign_images(text, deps.paper_id)
        text, payload = self._renumber(text, citations)
        # 正文步骤里的 `[n]` 要用**同一套映射**改写一遍：映射只由 citations 决定（不看正文），
        # 所以逐段跑一遍得到的号与整篇跑一遍完全一致；不跑的话轨迹里的引用点下去会跳错。
        for step in state.log.steps:
            if step.get("kind") == "text" and step.get("text"):
                step["text"], _ = self._renumber(step["text"], citations)
        # 过程轨迹收尾：整轮耗时与轮数（前端抬头那句"已完成，用时 …"与自检都读它）
        state.log.elapsed_ms = int(state.elapsed_ms or 0)
        state.log.rounds = int(state.rounds or deps.model_calls or 0)
        run = state.log.payload()
        # 落库在 done **之前**（§11 硬规则②）；落库之后不再检查 deadline（硬规则③）
        persisted = False
        try:
            res = await self.host.persist_message({
                "conversation_id": req.conversation_id,
                "parent_id": req.parent_id,
                "role": "assistant",
                "content": text,
                "citations": payload,
                "tool_trace": deps.trace,
                # 多轮思考 + 工具调用的**有序轨迹**：重开会话时前端照着它重画
                "run": run,
                # 思维链**随消息一起落库**：前端在 `done` 之后会重拉一次会话（`done` 是权威结果），
                # 不落的话思考块会在回答落地的那一刻凭空消失。
                "thinking": state.thinking,
                "thinking_ms": state.thinking_ms,
                "finish_reason": state.incomplete_reason or "stop",
            })
            persisted = bool(res.get("persisted"))
        except HostError as exc:
            log.warning("助手消息落库失败（回答照发，persisted=false）：%s", exc.message)
        yield {"type": "done", "answer": text, "citations": payload,
               "tool_trace": deps.trace, "rounds": state.rounds,
               "incomplete_reason": state.incomplete_reason,
               "thinking": state.thinking, "thinking_ms": state.thinking_ms,
               # 这一轮改过的产物（`page_markdown_edit`）：前端据此提示"正文已更新"，
               # 也让"模型说改了、其实没改"这种话在对不上时看得出来（自检断言它）。
               "edits": deps.edits,
               # 过程轨迹（多轮思考 + 工具调用）：前端收到后用它覆盖自己拼的那份
               "run": run,
               "persisted": persisted, "conversation_id": req.conversation_id}


# --------------------------------------------------------------------------
# 小工具
# --------------------------------------------------------------------------


def _digest(args: Any) -> str:
    """工具入参摘要：**不发原始 JSON**（参数里可能有整篇论文，前端过程行会爆）。"""
    if args is None:
        return ""
    if isinstance(args, dict):
        bits = []
        for key, value in list(args.items())[:4]:
            text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
            bits.append(f"{key}={str(text)[:40]}")
        return "、".join(bits)[:120]
    return str(args)[:120]


def _register_chunk_plain(deps: Deps, item: dict):
    """不依赖 RunContext 的命中登记（预检索在工具之外跑，拿不到 ctx）。"""
    from backend.agent.tools import _clip, _locate

    chunk_id = str(item.get("chunk_id") or "")
    section = item.get("section") or None
    page = int(item.get("page_no") or item.get("page") or 0) or None
    return deps.registry.assign(
        key=f"chunk:{chunk_id}", kind="chunk", page=page, section=section,
        block_id=item.get("block_id"),
        snippet=_clip(item.get("translation") or item.get("content") or ""),
        label=_locate(page, section, item.get("chunk_type") or ""),
        ids=(chunk_id,) if chunk_id else ())


def format_sse(event: dict) -> str:
    """SSE 帧（``event: <type>`` + ``data: <json>``）。"""
    payload = {k: v for k, v in event.items() if k != "type"}
    return f"event: {event.get('type')}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
