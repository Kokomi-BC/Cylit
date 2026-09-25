"""Mock Provider（离线可跑，规划 §1.2 完全离线原则）。

用途：
1. **不配任何模型时系统仍可完整运行**：导入 → 解析 → 阅读原文 → 复核 → 笔记；
2. 作为**测试替身**：可注入 429 / 持续失败，用于验证自适应并发（阶段 08 验收）；
3. 逐段翻译的"占位译文"让三视图与配对逻辑可被端到端验证。

Mock 输出形状与真实 Provider **完全一致**（含 token 统计与 JSON schema），
因此上层代码不需要分支判断。
"""

from __future__ import annotations

import hashlib
import json
import random
import re
import time
from typing import Any, Iterator

from backend.core.errors import ProviderError, RateLimitError
from backend.providers.base import (
    EmbeddingProvider, GenerationProvider, GenerationResult, ModelInfo,
)


class MockGeneration(GenerationProvider):
    """确定性假生成器：可配置失败率/限流率，用于测试调度器。"""

    def __init__(self, config: dict | None = None, api_key: str | None = None) -> None:
        super().__init__(config, api_key)
        p = dict(self.config.get("parameters") or {})
        self.fail_every: int = int(p.get("fail_every") or 0)   # 每 N 次失败一次
        self.rate_limit_every: int = int(p.get("rate_limit_every") or 0)
        self.error_streak: int = int(p.get("error_streak") or 0)  # 连续失败次数
        self.fail_first: int = int(p.get("fail_first") or 0)      # 前 N 次失败
        self.delay_ms: int = int(p.get("delay_ms") or 0)
        self.jitter: float = float(p.get("jitter") or 0.0)
        self.seed: int = int(p.get("seed") or 20260917)
        self._calls = 0
        self._rng = random.Random(self.seed)

    # ------------------------------------------------------------------
    def _maybe_fail(self) -> None:
        self._calls += 1
        if self.fail_first and self._calls <= self.fail_first:
            raise ProviderError(f"mock 注入失败（第 {self._calls} 次）", retryable=True)
        if self.error_streak and self._calls <= self.error_streak:
            raise ProviderError(f"mock 连续失败（第 {self._calls} 次）", retryable=True)
        if self.rate_limit_every and self._calls % self.rate_limit_every == 0:
            raise RateLimitError("mock 注入 429", retry_after=1.5)
        if self.fail_every and self._calls % self.fail_every == 0:
            raise ProviderError("mock 注入失败", retryable=True)

    def generate(self, prompt: str, *, system: str | None = None,
                 temperature: float | None = None, max_tokens: int | None = None,
                 **kwargs: Any) -> GenerationResult:
        t0 = time.perf_counter()
        self._maybe_fail()
        if self.delay_ms:
            delay = self.delay_ms / 1000.0
            if self.jitter:
                delay *= 1 + self._rng.uniform(-self.jitter, self.jitter)
            time.sleep(delay)
        text = _mock_text(prompt, system)
        pt = _approx_tokens((system or "") + prompt)
        ct = _approx_tokens(text)
        return GenerationResult(
            text=text, model="mock-generation",
            prompt_tokens=pt, completion_tokens=ct,
            latency_ms=round((time.perf_counter() - t0) * 1000, 1),
            finish_reason="stop",
        )

    def stream(self, prompt: str, *, system: str | None = None,
               **kwargs: Any) -> Iterator[str]:
        text = self.generate(prompt, system=system, **kwargs).text
        for i in range(0, len(text), 12):
            yield text[i:i + 12]

    def structured(self, prompt: str, schema: dict, *, system: str | None = None,
                   **kwargs: Any) -> dict:
        self._maybe_fail()
        return _mock_structured(prompt, schema, system)

    def generate_vision(self, prompt: str, images: list[bytes], *,
                        system: str | None = None,
                        max_tokens: int | None = None,
                        **kwargs: Any) -> GenerationResult:
        """扫描件读页 / 页级翻译的离线替身。

        形状必须与真实视觉模型一致：页级翻译要的是 ``{original_md, translation_md}``
        两份逐块对应的 Markdown（由 :func:`_mock_page_pair` 产出），
        扫描件读页要的是一整页 Markdown（:func:`_mock_scanned_page`）。
        """
        t0 = time.perf_counter()
        self._maybe_fail()
        if "original_md" in prompt:
            payload = _mock_page_pair(prompt)
            text = json.dumps(payload, ensure_ascii=False)
        else:
            text = _mock_scanned_page(len(images))
        pt = _approx_tokens((system or "") + prompt) + 384 * max(1, len(images))
        return GenerationResult(
            text=text, model="mock-vision",
            prompt_tokens=pt, completion_tokens=_approx_tokens(text),
            latency_ms=round((time.perf_counter() - t0) * 1000, 1),
            finish_reason="stop",
        )

    def test(self) -> ModelInfo:
        t0 = time.perf_counter()
        try:
            self.generate("ping", max_tokens=4)
            return ModelInfo(ok=True, model_name="mock-generation",
                             latency_ms=round((time.perf_counter() - t0) * 1000, 1),
                             detail="mock provider 正常（离线模式）")
        except ProviderError as exc:
            return ModelInfo(ok=False, detail=exc.message)


class MockEmbedding(EmbeddingProvider):
    """确定性哈希向量：离线也能跑通检索链路（质量有限，仅作降级）。"""

    def __init__(self, config: dict | None = None, api_key: str | None = None) -> None:
        super().__init__(config, api_key)
        p = dict(self.config.get("parameters") or {})
        self._dim = int(p.get("dim") or 512)

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._vec(t) for t in texts]

    def _vec(self, text: str) -> list[float]:
        import math

        vec = [0.0] * self._dim
        tokens = _tokens(text)
        for tok in tokens:
            h = hashlib.blake2b(tok.encode("utf-8"), digest_size=8).digest()
            idx = int.from_bytes(h[:4], "big") % self._dim
            sign = 1.0 if h[4] % 2 == 0 else -1.0
            vec[idx] += sign
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        return [round(v / norm, 6) for v in vec]

    @property
    def dim(self) -> int:
        return self._dim

    def test(self) -> ModelInfo:
        t0 = time.perf_counter()
        v = self.embed(["ping"])[0]
        return ModelInfo(ok=True, dimension=len(v), model_name="mock-embedding",
                         latency_ms=round((time.perf_counter() - t0) * 1000, 1),
                         detail="确定性哈希向量（离线降级）")


# --------------------------------------------------------------------------
# Mock 文本生成规则
# --------------------------------------------------------------------------

_MOCK_NOTICE = (
    "[MOCK 译文] 当前未接入真实模型（ModelManager 未找到可用的 generation 模型），"
    "以下为占位内容，用于验证「逐段流式 + 三视图配对 + 断点续跑」链路。\n\n"
)


def _mock_text(prompt: str, system: str | None) -> str:
    """按调用场景返回不同形状的占位输出。

    判据必须精确：翻译 prompt 的结尾也会出现 ``translation_md`` 字段名，
    而公式修复 prompt 的 ``output_schema`` 里也会出现 ``repaired_latex``，
    因此用「页面标记 / 带引号的 JSON 键」与 system 提示语区分。
    """
    sys_text = system or ""
    if "original_md" in prompt:
        # 页级翻译（当前产线 A）：同一次调用里给「原文转录 + 译文」
        return json.dumps(_mock_page_pair(prompt), ensure_ascii=False)
    if "本页译文（Markdown）" in prompt:
        return _mock_page_translation(prompt)
    if "<公式>" in prompt:
        return json.dumps(_mock_formula_translation(prompt), ensure_ascii=False)
    if "repaired_latex" in prompt or '"task": "repair_formula"' in prompt:
        return json.dumps(_mock_formula_repair(prompt), ensure_ascii=False)
    if "翻译" in sys_text or "translate" in sys_text.lower():
        return json.dumps(_mock_translation(prompt), ensure_ascii=False)
    if "简报" in sys_text or "术语负责人" in sys_text:
        return json.dumps(_mock_brief(prompt), ensure_ascii=False)
    if "解释" in sys_text or "explain" in sys_text.lower():
        return (_MOCK_NOTICE + "（AI 解释占位）该段落在论述量化误差与内积估计的关系，"
                "关键点：随机旋转 → 已知分布 → 最优标量量化。")
    body = [b for b in prompt.strip().split("\n") if b.strip()][:3]
    return _MOCK_NOTICE + " / ".join(b[:120] for b in body)


# --------------------------------------------------------------------------
# 页级翻译的 Mock：**保持块结构**，便于验证"整页 Markdown → 按块切分"
# --------------------------------------------------------------------------

_BLOCK_TEXT = re.compile(r"【本页原文（Markdown，按块顺序）】\s*\n(.*?)\n\s*【本页译文（Markdown）】",
                         re.DOTALL)
# 页级翻译（当前产线 A）prompt 里的页内文本
_PAGE_TEXT = re.compile(r"<页面文本>\s*(.*?)\s*</页面文本>", re.DOTALL)
_HEADING_LINE = re.compile(r"^(#{1,6})\s+(.*)$")
_NUM_PREFIX = re.compile(r"^(\d{1,2}(?:\.\d{1,2}){0,3})\s+(.*)$")

# 极简术语表：只为让 Mock 译文"看起来像中文"，不追求语义正确
_MOCK_TERMS = [
    ("TurboQuant", "TurboQuant"), ("quantization", "量化"), ("quantizer", "量化器"),
    ("distortion", "失真"), ("distortion rate", "失真率"), ("inner product", "内积"),
    ("random rotation", "随机旋转"), ("residual", "残差"), ("bit-width", "位宽"),
    ("scalar", "标量"), ("error", "误差"), ("bound", "上界"), ("Theorem", "定理"),
    ("Lemma", "引理"), ("Figure", "图"), ("Table", "表"), ("Section", "节"),
    ("vector", "向量"), ("vectors", "向量"), ("nearest neighbor", "最近邻"),
    ("MSE", "MSE"), ("QJL", "QJL"), ("KV cache", "KV 缓存"), ("transformer", "Transformer"),
    ("encoder", "编码器"), ("decoder", "解码器"), ("optimal", "最优"),
    ("unbiased", "无偏"), ("estimator", "估计量"), ("proof", "证明"),
    ("appendix", "附录"), ("Algorithm", "算法"), ("bits", "比特"), ("bit", "比特"),
    ("dimension", "维度"), ("quantize", "量化"), ("compression", "压缩"),
    ("memory", "内存"), ("accuracy", "精度"), ("baseline", "基线"),
    ("experiment", "实验"), ("results", "结果"), ("method", "方法"),
]


def _page_body_of(prompt: str) -> str:
    """取 prompt 里的页内文本（新版 ``<页面文本>``；兼容旧版标记）。"""
    m = _PAGE_TEXT.search(prompt)
    if m:
        body = m.group(1).strip()
        # 解析器没抽到文本时 prompt 里是一句说明：换成一段结构完整的假页
        if body and not body.startswith("（解析器没有抽到文本"):
            return body
    m = _BLOCK_TEXT.search(prompt)
    if m:
        return m.group(1).strip()
    return _mock_scanned_page(1)


def _mock_page_pair(prompt: str) -> dict:
    """页级翻译的占位产出：原文=页内文本（转录），译文=逐块对应的中文占位。

    块结构**刻意保持一致**（标题保编号、公式原样、空行分段），
    这样离线也能验证"两份 Markdown → 行对齐 → 前端逐段对照"的完整链路。
    """
    body = _page_body_of(prompt)
    return {
        "original_md": body,
        "translation_md": _mock_page_translation(prompt),
    }


def _mock_page_translation(prompt: str) -> str:
    """按块结构产出占位译文：保留标题编号、图注加粗、公式原样。"""
    body = _page_body_of(prompt)
    out: list[str] = []

    for raw_line in body.split("\n"):
        line = raw_line.rstrip()
        stripped = line.strip()
        if not stripped:
            if out and out[-1] != "":
                out.append("")
            continue
        # 展示公式：原样保留（模型不该改写公式）
        if stripped.startswith("$$"):
            out.append(stripped)
            continue
        # 图注/表注：保留标签并加粗
        caption = re.match(r"^\*\*(.+?)\*\*$", stripped)
        if caption:
            out.append(f"**{_mock_zh(caption.group(1))}**")
            continue
        # 标题：保留编号层级
        heading = _HEADING_LINE.match(stripped)
        if heading:
            level, text = heading.group(1), heading.group(2)
            num = _NUM_PREFIX.match(text)
            if num:
                zh = f"{num.group(1)} {_mock_zh(num.group(2))}"
            else:
                zh = _mock_zh(text)
            out.append(f"{level} {zh}")
            continue
        out.append(_mock_zh(stripped))

    text = "\n".join(out).strip()
    return text or _MOCK_NOTICE


def _mock_zh(text: str) -> str:
    """确定性占位翻译：术语替换 + 编号/公式保留 + 中文语序包装。

    刻意**保持与英文相近的信息密度**（数字、公式、编号一个不少），
    这样页级对齐器面对的输入形态与真实模型相似，回退率才有参考价值。
    """
    t = (text or "").strip()
    if not t:
        return ""
    # 抽出所有行内公式与数字，先占位再还原，避免被替换破坏
    holds: list[str] = []

    def hold(m: re.Match) -> str:
        holds.append(m.group(0))
        return f"\x00{len(holds) - 1}\x00"

    t = re.sub(r"\$[^$]+\$", hold, t)
    t = re.sub(r"\d+(?:\.\d+)?", hold, t)

    for en, zh in sorted(_MOCK_TERMS, key=lambda x: -len(x[0])):
        t = re.sub(rf"\b{re.escape(en)}\b", zh, t, flags=re.IGNORECASE)

    # 去掉纯英文残留（含单字母变量以外的词），保留术语与标点
    t = re.sub(r"[A-Za-z]{2,}", "该项", t)
    t = re.sub(r"\b([A-Za-z])\b", r"\1", t)
    # 规范化标点：英文句点后接中文句号会变成 "。。"
    t = re.sub(r"\s*([,;:])\s*", "，", t)
    t = re.sub(r"\s*\.\s*", "。", t)
    t = re.sub(r"。+", "。", t)
    t = re.sub(r"\s{2,}", " ", t).strip()
    if not t.endswith(("。", "！", "？", "：")):
        t += "。"

    for i, h in enumerate(holds):
        t = t.replace(f"\x00{i}\x00", h)
    return t


def _mock_brief(prompt: str) -> dict:
    """文档简报的 Mock：从抽样文本里挑出高频术语作为统一译法。"""
    lower = (prompt or "").lower()
    glossary = []
    for en, zh in _MOCK_TERMS:
        if en.lower() in lower and len(glossary) < 12:
            glossary.append({"en": en, "zh": zh})
    return {
        "domain": "高维向量量化与近似最近邻检索（占位简报）",
        "glossary": glossary,
        "keep_as_is": ["TurboQuant", "QJL", "MSE", "Transformer"],
        "style": "学术论文正文，简洁书面语，公式与符号保留",
        "notes": ["符号 d/b 保持原样", "缩写首次出现可保留英文"],
        # 元数据与术语同一次产出：mock 只给标签，会议/年份/摘要留给离线抽取
        "meta": {"venue": "", "year": 0, "tags": ["向量量化", "近似最近邻", "占位标签"],
                 "abstract_zh": ""},
    }


def _mock_translation(prompt: str) -> dict:
    """从 prompt 里解析出原文，返回符合 translation schema 的占位译文。"""
    src = _extract_between(prompt, "<原文>", "</原文>") or prompt
    src = src.strip()
    head = src[:60].replace("\n", " ")
    translated = f"{_MOCK_NOTICE}【占位译文】{head}{'…' if len(src) > 60 else ''}"
    return {
        "translation_md": translated,
        "terms_used": [],
        "uncertain": [],
        "confidence": 0.1,
        "notes": "mock provider：未接入真实模型",
    }


_MOCK_SCANNED_PAGE = """# {title}

We study quantization error bounds for high-dimensional vectors. Our method
applies a random rotation so that each coordinate follows a Beta distribution.

## 2 Method

Let $x \\in \\mathbb{{R}}^d$ be a unit-norm vector and let $b$ be the bit-width.
The MSE distortion is defined as

$$ D_{{mse}} := \\mathbb{{E}}\\|x - Q(x)\\|_2^2. $$

For any $b \\geq 1$ the distortion is bounded by {bound}.

**Figure 1**: MSE distortion of TurboQuant compared with PQ and QJL.

<!-- layout {{"figures":[{{"number":"1","bbox":[0.18,0.20,0.82,0.55],"caption_bbox":[0.18,0.565,0.72,0.585]}}]}} -->
"""


def _mock_scanned_page(n_images: int) -> str:
    """扫描件页的假识别结果（占位，用于离线验证"扫描件 → 插图裁剪"整条链路）。

    形状与真实视觉模型一致：正文 Markdown + **末尾一行版面块**（插图粗定位）。
    带版面块才能离线跑到 ``pipeline._scan_figures``，否则扫描页永远是 0 张图。
    """
    return _MOCK_SCANNED_PAGE.format(title="[MOCK 扫描件] Scanned Paper",
                                     bound="2.7207")


def _mock_formula_translation(prompt: str) -> dict:
    """Mock 的公式块输出（仅 ``block`` 粒度 / 回退补译用到）：**原样返回**源 LaTeX。

    这正是保守且正确的行为（能渲染的公式不该被改写）；页级翻译里公式直接
    混在整页正文中，走 :func:`_mock_page_translation` 的 ``$$`` 分支。
    """
    src = (_extract_between(prompt, "<公式>", "</公式>") or "").strip()
    return {
        "translation_md": src,
        "terms_used": [],
        "uncertain": [] if src else ["mock：未从提示中解析到公式"],
        "confidence": 0.95 if src else 0.1,
        "notes": "mock provider：公式原样返回（不修改 LaTeX）",
    }


def _mock_formula_repair(prompt: str) -> dict:
    """Mock 的公式修复：不改变结构，只把候选原样返回并标注不确定性。

    ——这恰好是**正确的保守行为**（规划 §14.4 规则：证据不足必须返回 uncertainties），
    因此 mock 下的分级结果（C 级保留）与真实模型并不矛盾。
    """
    evidence = _extract_between(prompt, '"evidence":', '"output_schema"') or ""
    raw = _extract_between(prompt, '"raw_text":', ',\n') or ""
    candidate = _extract_between(prompt, '"chosen_candidate":', '}') or ""
    candidate = candidate.strip().strip('"').strip()
    notes = []
    if "radical_bars" in evidence:
        notes.append("矢量层存在根号横杠，但覆盖范围需人工确认")
    if "fraction_lines" in evidence:
        notes.append("存在分式线，分子/分母边界需人工确认")
    if not notes:
        notes.append("mock provider 未做修复，保留原候选")
    return {
        "repaired_latex": candidate,
        "reason": f"mock 基于证据 {raw[:40]} 保留候选（未接入真实模型）",
        "evidence_used": ["vector_audit"],
        "uncertainties": notes,
    }


def _mock_structured(prompt: str, schema: dict, system: str | None) -> dict:
    # 兼容两种 schema 写法：标准 JSON Schema（含 properties）与扁平字段映射
    props = (schema or {}).get("properties") or schema or {}
    # 页级翻译走非结构化通道
    if "本页译文（Markdown）" in prompt:
        return {"translation_md": _mock_page_translation(prompt)}
    # 公式块（校验/修正）与普通翻译都是 translation_md 契约，必须先分流
    if "<公式>" in prompt:
        return _mock_formula_translation(prompt)
    text = _mock_text(prompt, system)
    if "translation_md" in props:
        return _mock_translation(prompt)
    if "repaired_latex" in props:
        return _mock_formula_repair(prompt)
    if "domain" in props:
        return _mock_brief(prompt)
    if "answer" in props:
        return {"answer": "（mock）未接入模型，无法基于检索结果作答。", "used_sources": []}
    if not props:
        # schema 为空时无法判定契约：返回通用结构，避免调用方静默拿到空 dict
        return {"text": text[:200], "note": "mock：未提供 output_schema"}
    out: dict[str, Any] = {}
    for key, spec in props.items():
        t = (spec or {}).get("type")
        out[key] = {"string": text[:200], "array": [], "object": {},
                    "number": 0, "integer": 0, "boolean": False}.get(t, None)
    return out


def _tokens(text: str) -> list[str]:
    out, buf = [], ""
    for ch in (text or "").lower():
        if ch.isalnum():
            buf += ch
        else:
            if buf:
                out.append(buf)
                buf = ""
            if ch.strip():
                out.append(ch)
    if buf:
        out.append(buf)
    return out


def _approx_tokens(text: str) -> int:
    """粗略 token 估算（英文 ~4 字符/token，中文 ~1.5 字符/token）。"""
    t = text or ""
    cjk = sum(1 for c in t if "\u4e00" <= c <= "\u9fff")
    other = len(t) - cjk
    return int(cjk / 1.5 + other / 4) + 1


def _extract_between(text: str, start: str, end: str) -> str | None:
    i = text.find(start)
    if i < 0:
        return None
    i += len(start)
    j = text.find(end, i) if end else -1
    return text[i:j if j >= 0 else len(text)]
