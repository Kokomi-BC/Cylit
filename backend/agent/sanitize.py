"""正文清洗：模型抄进来的内部 id → ``[n]``；**映射不到的一律不动**。

两个真实现象驱动了这个模块（都在真机探测里见过）：

1. 模型会把工具返回里的 id 直接写进正文（``ch-91ac``、``block_id=p01-b03``）——
   用户看到的是天书，前端也点不动；
2. 但**不能按形状删**：`机型 MD-11 与 MD-80` 这类文本会被贪心规则吃成 `机型 与`。
   所以只替换**我们自己登记过**的 id（精确匹配），其余原样保留。

流式路径**不另写一套增量规则**：对累积的原文跑同一套批量清洗，只把"不会再被改动"的
前缀推给前端（留 ``GUARD_CHARS`` 个字符的余量）。增量规则看起来更省，但它必然与
批量规则产生分歧 —— 同一个回答流式和落库后不一样，是最难查的一类 bug。
"""

from __future__ import annotations

import re
from typing import Iterable

from backend.agent.cite_groups import parse_bracket_marks
from backend.agent.citations import RefRegistry
from backend.core.logging import get_logger

log = get_logger(__name__)

#: 尾部保留多少个字符不推（可能还有一个 id 只写了一半）
GUARD_CHARS = 48

#: ``chunk_id: xxx`` / ``block_id = xxx`` / ``page_idx：3`` 这类口癖的壳
_LABEL_RE = re.compile(
    r"(?:`?\b(?:chunk_id|block_id|page_idx|unit_id|parent_chunk_id)\b`?\s*[=:：]\s*)"
    r"(`?)(?P<ident>[^\s`）)】,，。;；]+)(\1)"
)

#: **`[ref=3]` 这类写法要归一成 `[3]`**。提示词里说的是"标 `[ref]`"，模型有时会把
#: `ref` 当字段名照抄成 `[ref=9]`（实测第一轮就中了）。留着它，前端的 `[n]` → 按钮
#: 那套就匹配不上，用户看到的是 `[ref=9]` 这种半成品。
_REF_MARK_BRACKET_RE = re.compile(r"[\[\(]\s*ref\s*[=:：#]?\s*(\d{1,3})\s*[\]\)]",
                                  re.IGNORECASE)
_REF_MARK_BARE_RE = re.compile(r"\bref\s*[=:：]\s*(\d{1,3})\b", re.IGNORECASE)


def normalize_ref_marks(text: str) -> str:
    """``[ref=3]`` / ``(ref: 3)`` / ``ref=3`` → ``[3]``。"""
    if not text:
        return text
    out = _REF_MARK_BRACKET_RE.sub(lambda m: f"[{m.group(1)}]", text)
    return _REF_MARK_BARE_RE.sub(lambda m: f"[{m.group(1)}]", out)


def _ident_table(registry: RefRegistry) -> dict[str, int]:
    """登记过的内部 id → ref。长 id 优先替换，避免短 id 是长 id 前缀时替换错位置。"""
    table = {}
    for citation in registry.all():
        for key in (citation.block_id, citation.row_id):
            if key:
                table[str(key)] = citation.ref
    # chunk_id 只存在 registry 的 id 索引里（它不进 Citation 的公开字段）
    for raw, ref in getattr(registry, "_by_id", {}).items():
        table[str(raw)] = ref
    return dict(sorted(table.items(), key=lambda kv: -len(kv[0])))


def sanitize_answer_text(text: str, registry: RefRegistry) -> str:
    """把正文里出现的内部 id（带壳或不带壳）换成 ``[n]``，并归一 `[ref=3]` 这类写法。"""
    if not text:
        return text
    text = normalize_ref_marks(text)
    table = _ident_table(registry)
    if not table:
        return text

    out = text
    for ident, ref in table.items():
        # ① 带壳的：``chunk_id: p01-b03`` → ``[2]``（壳一起去掉）
        out = _LABEL_RE.sub(
            lambda m, r=ref, i=ident: f"[{r}]" if m.group("ident") == i else m.group(0),
            out)
        # ② 裸 id：直接换成 [n]（精确匹配，不做形状猜测）
        if ident in out:
            out = re.sub(re.escape(ident), f"[{ref}]", out)
    return out


def used_refs(text: str, registry: RefRegistry) -> list[int]:
    r"""正文里实际出现的引用编号（按出现顺序去重，**含多号与区间**）。

    为什么不再用 `\[(\d{1,3})\]` 那条正则：实测正文里写着 `[36, 41]`（区间 `[3-5]` 同理），
    只认单号会让整组漏过去 —— 既不算"引用过"，也不会被改写，那条消息的引用表于是与正文对不上。
    解析统一交给 `cite_groups.parse_bracket_marks`：认不出的方括号本来就不该算引用。
    """
    seen: list[int] = []
    for mark in parse_bracket_marks(text or ""):
        for ref in mark.members:
            if ref not in seen and registry.by_ref(ref) is not None:
                seen.append(ref)
    return seen


def referenced_citations(text: str, registry: RefRegistry) -> list:
    """正文引用到的 citation；**一个都没写时**才退回"这一轮用到的全部证据"。

    为什么要有这个兜底：模型偶尔会整段不写编号（尤其短回答）。全部丢掉会让前端
    引用表空空如也，用户以为没有依据。但只在"一个都没写"时兜底 —— 写了编号却漏几个，
    那是模型的判断，不该由我们补齐（补出来的引用是伪造的）。
    """
    refs = used_refs(text, registry)
    if refs:
        return [registry.by_ref(r) for r in refs]
    return registry.all()


class StreamingAnswerSanitizer:
    """流式清洗：累积原文 → 批量清洗 → 只推安全前缀。"""

    def __init__(self, registry: RefRegistry, *, guard: int = GUARD_CHARS) -> None:
        self._registry = registry
        self._guard = guard
        self._raw = ""
        self._clean = ""
        self._emitted = 0
        self._last_batch = 0
        #: 首批攒得少一点：攒 256 字才吐第一个字，用户会觉得"卡住了"。清洗本身很便宜，
        #: 真正的成本在长回答上（O(n²)），那由 `_reclean` 里的自适应批次兜住。
        self._batch = 64

    # ---------------- 输入 ----------------
    def feed(self, delta: str) -> str:
        """吃一段增量，返回**本次可以安全推出的**文本（可能为空串）。"""
        if not delta:
            return ""
        self._raw += delta
        if len(self._raw) - self._last_batch < self._batch:
            return ""
        self._reclean()
        return self._drain()

    def reset(self) -> None:
        """丢掉**到目前为止**累积的正文，从头开始。

        用途：模型「边想边说」—— 先讲一段（『我先看一下第 3 页』）再调工具，接着才写正式回答。
        调工具之前那些话属于**过程**，不该进最终正文，否则整段落进回答里，用户看到的
        是一段自言自语的草稿（用户口径 2026-11-26）。调用方负责同时通知前端清掉已显示的那段。
        """
        self._raw = ""
        self._clean = ""
        self._emitted = 0
        self._last_batch = 0
        self._batch = 64

    def flush(self) -> str:
        """回答结束：把剩下的（含被 guard 挡住的尾巴）全部推出。"""
        self._reclean()
        tail = self._clean[self._emitted:]
        self._emitted = len(self._clean)
        return tail

    # ---------------- 内部 ----------------
    def _reclean(self) -> None:
        self._clean = sanitize_answer_text(self._raw, self._registry)
        self._last_batch = len(self._raw)
        # 自适应攒批：回答越长，单次清洗越贵 → 拉大批次，避免 O(n²)
        self._batch = max(192, len(self._raw) // 8)

    def _drain(self) -> str:
        safe_upto = max(0, len(self._clean) - self._guard)
        if safe_upto <= self._emitted:
            return ""
        chunk = self._clean[self._emitted:safe_upto]
        self._emitted = safe_upto
        return chunk

    # ---------------- 只读 ----------------
    @property
    def raw(self) -> str:
        return self._raw

    @property
    def clean(self) -> str:
        return sanitize_answer_text(self._raw, self._registry)


def sanitize_many(texts: Iterable[str], registry: RefRegistry) -> list[str]:
    return [sanitize_answer_text(t, registry) for t in texts]


#: Markdown 图片（含 ``![alt](url "title")`` 形式）
_IMAGE_RE = re.compile(r"!\[(?P<alt>[^\]]*)\]\(\s*<?(?P<url>[^)\s>]+)[^)]*\)")
#: 允许的受控地址：**只能是本论文自己的受控图**（region 裁图 / 整页图 / 静态裁图）。
#: 2026-12-07 放宽一次：用户那次配图全被删成「（配图已省略：…）」，而模型写的到底是哪种地址
#: 我看不出来（清洗器把原文吃掉了）。与其猜，不如把"本论文的所有受控图路径"都放行；
#: 站外地址仍然一律拒绝（域名在 `_controlled_image_path` 里已经先丢掉了）。
_ALLOWED_IMAGE_PREFIXES = (
    "/api/v1/papers/{paper_id}/figures/",
    "/api/v1/papers/{paper_id}/pages/",
    "/papers/{paper_id}/",
)
#: 兼容旧名（单条前缀），避免其它引用处炸掉
_ALLOWED_IMAGE = _ALLOWED_IMAGE_PREFIXES[0]
#: 规整图片地址时用来削掉部署前缀的锚点（`/cylit/api/v1/papers/…` → `/api/v1/papers/…`）
_API_PATH_MARK = "/api/v1/papers/"


def _controlled_image_path(url: str) -> str:
    """把模型写出来的图片地址规整成**绝对路径**（非受控地址原样返回，由调用方决定丢弃）。

    为什么需要：模型会把受控地址写歪 —— 实测 `api/v1/papers/<id>/figures/region/7/0?v=…`
    少了开头那个 `/`，于是 `startswith("/api/v1/…")` 判不出来，一张本来完全合法的裁图被当成
    外链删成了「（配图已省略：…）」（用户 2026-11-26 报的正是这个）。只要**路径**对得上就补回 `/`；
    写成整条 `https://…/api/v1/…` 的只取路径部分，域名一律丢掉 —— 不能因为它长得像就放行站外地址。
    """
    raw = (url or "").strip()
    if not raw:
        return ""
    if "://" in raw:
        rest = raw.split("://", 1)[1]
        raw = "/" + (rest.split("/", 1)[1] if "/" in rest else "")
    elif raw.startswith("./"):
        raw = raw[1:]
    if not raw.startswith("/"):
        raw = "/" + raw
    # 部署在子路径下时（本站是 `/cylit/`），模型抄来的绝对地址常常带那层前缀 ——
    # `/cylit/api/v1/papers/…` 以前过不了白名单，一张合法裁图被删成「（配图已省略：…）」
    # （用户 2026-12-07 报的正是这个）。域名已经在上面丢掉了，剩下的就是**本机路径**，
    # 于是把 `/api/v1/papers/` 之前的那截前缀削掉是安全的。
    mark = raw.find(_API_PATH_MARK)
    if mark > 0:
        raw = raw[mark:]
    return raw


def strip_foreign_images(text: str, paper_id: str) -> str:
    """删掉回答里的非受控图片（外链 / ``data:`` / ``blob:`` / 别的论文）。

    为什么必须在服务端删而不是靠提示词：模型会**说实话地**去配一张"它认识的"图
    （实测：讲 Transformer 架构图时嵌了 ``https://arxiv.org/abs/1706.03762`` ——
    那是论文地址，根本不是图片）。前端渲染出来就是一个裂图，而且这条链路会把用户
    引到站外。规则只能有一条：**只允许本论文自己的受控裁图地址**。
    """
    allowed = tuple(p.format(paper_id=paper_id) for p in _ALLOWED_IMAGE_PREFIXES)

    def repl(match: re.Match) -> str:
        alt = (match.group("alt") or "").strip() or "插图"
        path = _controlled_image_path(match.group("url"))
        if path.startswith(allowed):
            # 用**规整后的绝对地址**重写：模型少写一个 `/` 时也能真的加载出来
            return f"![{alt}]({path})"
        # 被删掉的地址**记下来**：上一次排查就是查不到"模型到底写了什么"，只能靠猜
        log.warning("回答里的图片地址不在白名单，换成文字占位：paper=%s url=%s",
                    paper_id, path[:200])
        return f"（配图已省略：{alt}）"

    return _IMAGE_RE.sub(repl, text or "")
