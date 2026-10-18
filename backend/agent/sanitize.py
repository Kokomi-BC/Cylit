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

from backend.agent.citations import RefRegistry

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
    """正文里实际出现的引用编号（按出现顺序去重）。"""
    seen: list[int] = []
    for m in re.finditer(r"\[(\d{1,3})\]", text or ""):
        ref = int(m.group(1))
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
#: 允许的受控地址：**只能是本论文自己的 region 裁图**
_ALLOWED_IMAGE = "/api/v1/papers/{paper_id}/figures/region/"


def strip_foreign_images(text: str, paper_id: str) -> str:
    """删掉回答里的非受控图片（外链 / ``data:`` / ``blob:`` / 别的论文）。

    为什么必须在服务端删而不是靠提示词：模型会**说实话地**去配一张"它认识的"图
    （实测：讲 Transformer 架构图时嵌了 ``https://arxiv.org/abs/1706.03762`` ——
    那是论文地址，根本不是图片）。前端渲染出来就是一个裂图，而且这条链路会把用户
    引到站外。规则只能有一条：**只允许本论文自己的受控裁图地址**。
    """
    allowed = _ALLOWED_IMAGE.format(paper_id=paper_id)

    def repl(match: re.Match) -> str:
        url = match.group("url")
        if url.startswith(allowed):
            return match.group(0)
        alt = (match.group("alt") or "").strip() or "插图"
        return f"（配图已省略：{alt}）"

    return _IMAGE_RE.sub(repl, text or "")
