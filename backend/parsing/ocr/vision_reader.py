"""扫描件论文的兜底：整页交视觉模型「读」出正文（规划 §13.4）。

**为什么需要它**：扫描件（影印 PDF）里根本没有文本层，PyMuPDF 抽不出一个 span，
公式矢量层、字号、行距全部缺失 —— 现有解析链路（行聚类 / 段落切分 / 公式恢复）
整套失效。既然内容只存在于像素里，**正文与译文就只能都来自模型**：

```
页图（200dpi）→ 视觉模型 → 该页 Markdown 原文（不翻译）
                              ↓
                       切块（heading / para / formula / caption）
                              ↓
                   走现有的页级翻译链路 → 中文译文
```

关键约束：

1. **只识别不翻译**：这一步的输出是"原文事实源"，翻译是下一步的事；
   让一次调用同时产出两种语言会让对照阅读失去原文、也让校验无从下手。
2. **格式必须可切块**：提示里明确要求标题用 ``#``、展示公式用独立 ``$$…$$`` 段落、
   图注用 ``**Figure N**``，这样切块器能稳定识别（与文本层论文的块类型一一对应）。
3. **宁可留 ``[?]`` 也不编**：看不清的字要求写 ``[?]``，不要靠语义补全——
   这一步的错误会被翻译放大，且无法通过公式校验发现。
"""

from __future__ import annotations

import base64
import re
from pathlib import Path

from backend.core.errors import ProviderError
from backend.core.logging import get_logger
from backend.parsing.models import Block, Rect

log = get_logger(__name__)


# --------------------------------------------------------------------------
# 判定：这一页是不是扫描件
# --------------------------------------------------------------------------

# 低于这个字符数就认为"没有可用文本层"。扫描件通常是 0，
# 但有些影印件带了极少量 OCR 残渣（页眉页码），所以不能用 == 0。
MIN_TEXT_CHARS = 40


def page_text_chars(page) -> int:
    """该页文本层的有效字符数（不含空白）。"""
    try:
        text = page.get_text("text") or ""
    except Exception:  # noqa: BLE001 - 坏页不该让整篇解析失败
        return 0
    return len(re.sub(r"\s+", "", text))


def looks_scanned(page, min_chars: int = MIN_TEXT_CHARS) -> bool:
    """文本层几乎为空 → 当作扫描件（需要视觉模型读页）。"""
    if page_text_chars(page) >= min_chars:
        return False
    # 必须真的有整页图像，否则是"空白页"而不是"扫描页"
    try:
        return bool(page.get_images(full=True))
    except Exception:  # noqa: BLE001
        return False


# --------------------------------------------------------------------------
# 读页：页图 → Markdown 原文
# --------------------------------------------------------------------------

PAGE_READ_SYSTEM = """你是论文 OCR/版面还原专家。把论文页面图片**逐字转录**为 Markdown。

规则（必须严格遵守）：
1. **只转录，不翻译**：保持论文原语言（通常是英文），一个字都不要译成中文。
2. 标题**只写一层** `#`（按层级用 `##`/`###`），后面直接接标题文字，
   不要写成 `## ## 标题`，保留编号（如 `## 3.2 Method`）。
3. 展示公式**单独成段**并用 `$$ … $$` 包裹；行内公式用 `$ … $`，全部用 LaTeX。
   看不清的符号用 `[?]`，**不要靠语义猜测补全**。
4. 图注/表注用 `**Figure N**: …` / `**Table N**: …`，保留编号。
5. 表格用 Markdown 表格；参考文献条目一行一条，以 `- ` 开头。
6. 页眉页脚、页码、水印**不要转录**（本系统会单独处理）。
7. 只输出 Markdown 正文，不要输出解释、不要加代码块围栏、不要写"以下是转录"之类的话。
8. 如果这一页的大部分内容看不清，只输出一行 `<!-- unreadable -->`。"""

PAGE_READ_TEMPLATE = """请转录第 {page} 页 / 共 {page_total} 页的内容。

要求回顾：只转录不翻译；标题用 #；公式用 $…$/$$…$$（LaTeX）；图注加粗；
不要页眉页脚与页码；只输出 Markdown 正文。"""


def read_page_markdown(image: bytes | str | Path, provider, *,
                       page_no: int = 1, page_total: int = 1,
                       max_tokens: int | None = None) -> tuple[str, dict]:
    """调视觉模型读一页，返回 ``(markdown, 用量信息)``。

    ``image`` 可以是 PNG 字节，也可以是图片路径。
    """
    data = image if isinstance(image, bytes) else Path(image).read_bytes()
    if not data:
        return "", {"error": "页图为空"}
    prompt = PAGE_READ_TEMPLATE.format(page=page_no, page_total=page_total)
    try:
        res = provider.generate_vision(prompt, [data], system=PAGE_READ_SYSTEM,
                                       max_tokens=max_tokens or 8192)
    except ProviderError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise ProviderError(f"扫描件读页失败（第 {page_no} 页）：{str(exc)[:200]}") from exc
    text = _strip_fence(res.text or "")
    if "<!-- unreadable -->" in text:
        log.warning("第 %d 页被模型判定为不可读，跳过", page_no)
        text = ""
    usage = {
        "page": page_no, "chars": len(text),
        "prompt_tokens": res.prompt_tokens, "completion_tokens": res.completion_tokens,
        "reasoning_tokens": int(getattr(res, "reasoning_tokens", 0) or 0),
        "latency_ms": res.latency_ms,
    }
    return text, usage


def read_page_from_file(path: str | Path, provider, *, page_no: int = 1,
                        page_total: int = 1, max_tokens: int | None = None
                        ) -> tuple[str, dict]:
    """``render_page_png`` 写出的页图 → Markdown。"""
    return read_page_markdown(path, provider, page_no=page_no,
                              page_total=page_total, max_tokens=max_tokens)


# --------------------------------------------------------------------------
# 页 Markdown → 块
#
# 这里**不复用**文本层那套行聚类（没有字号/基线可用），而是复用"页级翻译"
# 那一侧已经验证过的 Markdown 切段规则：空行分段、`#` 成标题、`$$…$$` 成公式。
# 好处是：扫描件论文产出的块类型与普通论文**完全一致**，下游（翻译/阅读器/对账）
# 一行都不用改。
# --------------------------------------------------------------------------

_HEADING = re.compile(r"^(#{1,6})\s+(?P<title>.+?)\s*$")
_HEADING_HASH = re.compile(r"^[\s#]+")
_DISPLAY_MATH = re.compile(r"^\$\$(?P<body>.+?)\$\$$", re.DOTALL)
_CAPTION = re.compile(r"^\*\*(?:Figure|Table|图|表)\s*\d+[^*]*\*\*", re.IGNORECASE)
_TABLE_ROW = re.compile(r"^\s*\|.*\|\s*$")
_REF_ITEM = re.compile(r"^\s*[-*]\s+\[\d+\]")
_FENCE = re.compile(r"^\s*```")


def split_page_markdown(markdown: str) -> list[tuple[str, str]]:
    """把一页 Markdown 切成 ``[(kind, text)]``（保序）。

    kind ∈ heading / para / formula / caption / table / ref；
    块 id 由调用方按页序分配，所以这里只负责"切"和"分类"。
    """
    text = (markdown or "").replace("\r\n", "\n").strip()
    if not text:
        return []
    text = _strip_fence(text)

    out: list[tuple[str, str]] = []
    buf: list[str] = []
    in_math = False
    math_buf: list[str] = []

    def flush_para() -> None:
        nonlocal buf
        body = "\n".join(buf).strip()
        buf = []
        if body:
            out.append(_classify(body))

    for raw in text.split("\n"):
        line = raw.rstrip()
        stripped = line.strip()

        # 跨行公式优先判断：单独的 ``$$`` 既可能是开也可能是闭
        if in_math:
            if stripped.endswith("$$"):
                head = stripped[:-2]
                if head.strip():
                    math_buf.append(head)
                in_math = False
                out.append(("formula", _math_body(math_buf)))
                math_buf = []
            else:
                math_buf.append(stripped)
            continue

        if stripped.startswith("$$"):
            inner = stripped[2:]
            if inner.rstrip().endswith("$$") and len(inner.strip()) > 2:
                flush_para()
                out.append(("formula", inner.rstrip()[:-2].strip()))
            else:
                flush_para()
                in_math = True
                math_buf = [inner] if inner.strip() else []
            continue

        if not stripped:
            flush_para()
            continue
        buf.append(line)
    flush_para()
    if in_math and math_buf:
        out.append(("formula", _math_body(math_buf)))
    return out


def _math_body(lines: list[str]) -> str:
    return "\n".join(lines).strip().strip("$").strip()


def _classify(body: str) -> tuple[str, str]:
    first = body.split("\n", 1)[0].strip()
    heading = _HEADING.match(first)
    if heading and len(body.split("\n")) == 1:
        # 规整标题：实测视觉模型会写出 ``## ## 0.2 PRIOR WORK``（叠了两层），
        # 这里统一成"一层标记 + 标题"，否则标记会一路漏进译文与阅读器。
        title = _HEADING_HASH.sub("", heading.group("title")).strip()
        level = len(heading.group(1))
        return "heading", f"{'#' * level} {title}" if title else body
    if all(_TABLE_ROW.match(ln) for ln in body.split("\n") if ln.strip()):
        return "table", body
    if _ref_item_lines(body):
        return "ref", body
    if _CAPTION.match(body):
        return "caption", body
    return "para", body


def _ref_item_lines(body: str) -> bool:
    lines = [ln for ln in body.split("\n") if ln.strip()]
    return bool(lines) and sum(1 for ln in lines if _REF_ITEM.match(ln)) >= max(
        1, len(lines) - 1)


def blocks_from_page_markdown(markdown: str, *, page_no: int,
                              paper_id: str = "",
                              page_width: float = 0.0,
                              page_height: float = 0.0,
                              source: str = "vision") -> list[Block]:
    """一页 Markdown → ``Block`` 列表（供解析流水线直接使用）。

    块 id / order_global 由流水线后续统一分配（``assign_ids_in_order`` /
    ``_renumber_reading_order``），这里只填内容与类型。

    **公式必须按全系统的约定写成 ``$$ … $$``**：文本层论文里
    ``formula_markdown`` 就是这么写的，下游的 ``formula_shapes``（对齐用的
    LaTeX 锚点）也只认带 ``$`` 的文本。实测不包 ``$$`` 时，
    公式块在页级对齐里拿不到任何证据分 → 每块都要多花一次块级调用（整页 3 块全回退）。

    **bbox 的处理**：扫描件没有矢量层，坐标不可用；这里给每块一条**递增的虚拟
    横带**（``y0`` 逐块下移），因为 ``_renumber_reading_order`` 按
    ``(page, y0, x0)`` 排序——全给同一个矩形会让排序退化成"碰运气"。
    """
    blocks: list[Block] = []
    items = split_page_markdown(markdown)
    n = max(1, len(items))
    band = (page_height or 1.0) / n
    for i, (kind, body) in enumerate(items):
        text = body.strip()
        if not text:
            continue
        if kind == "formula":
            text = _wrap_display_math(text)
        y0 = band * i
        rect = Rect(0.0, y0, page_width or 0.0, y0 + band)
        blocks.append(_make_block(kind, text, page_no, rect, source, paper_id))
    return blocks


def _wrap_display_math(tex: str) -> str:
    """把裸 LaTeX 包成块级公式 ``$$ … $$``（与文本层论文的写法一致）。"""
    body = (tex or "").strip()
    if not body:
        return body
    if body.startswith("$$") and body.endswith("$$"):
        return body
    return f"$$\n{body}\n$$" if "\n" in body else f"$$ {body} $$"


def _make_block(kind: str, text: str, page_no: int, rect: Rect, source: str,
                paper_id: str) -> Block:
    """构造块。

    **文本约定必须与文本层论文一致**（``md/generator.block_body_text`` 的契约）：
    ``Block.text_clean`` 存**裸文本**，Markdown 标记由生成器统一加 ——
    标题不加 ``#``（生成器按 ``heading_level`` 加），图注不加 ``**``。
    实测踩过的坑：这里存了带标记的文本，生成 Markdown 时会再加一层，
    于是事实源里出现 ``## ## 0.2 PRIOR WORK``、阅读器与翻译都跟着错。
    """
    meta: dict = {"source": source, "ocr": True}
    if kind == "heading":
        first = text.split("\n", 1)[0].strip()
        m = _HEADING.match(first)
        level = len(m.group(1)) if m else 2
        title = (m.group("title") if m else first).strip()
        text = title
        meta["heading_level"] = level
        meta["section_title"] = title[:200]
    elif kind == "caption":
        text = text.replace("**", "").strip()
    b = Block(
        id="", paper_id=paper_id, page_no=page_no, order_in_page=0,
        order_global=0, type=kind, col=0, rect=rect,
        text=text, text_clean=text, source_text=text, source_kind=source,
        font_names=[], font_size=0.0,
        line_count=len([ln for ln in text.split("\n") if ln.strip()]),
        meta=meta,
    )
    return b.finalize()


def title_from_markdown(markdown: str, max_len: int = 200) -> str:
    """从第一页识别结果里取标题（扫描件没有文本层，元信息也来自模型）。

    **只认标题行**（提示里已要求标题用 ``#``）：不能拿第一段正文当标题——
    实测从论文中间某页开始扫描时，首页第一段是上一页的续句，
    拿它当标题会把一整段正文塞进 ``paper.title``。取不到就返回空串，
    让调用方保留原有的（文件名）兜底。
    """
    best: tuple[int, str] | None = None
    for kind, body in split_page_markdown(markdown):
        if kind != "heading":
            continue
        first = body.split("\n", 1)[0].strip()
        m = _HEADING.match(first)
        level = len(m.group(1)) if m else 2
        title = (m.group("title") if m else body).strip()
        if not title or len(title) > max_len:
            continue
        # 优先最浅的层级：真正的论文标题是 `#`，章节标题是 `##`/`###`
        if best is None or level < best[0]:
            best = (level, title)
    return best[1] if best else ""


def _strip_fence(text: str) -> str:
    t = (text or "").strip()
    if not t.startswith("```"):
        return t
    lines = t.split("\n")
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip().startswith("```"):
        lines = lines[:-1]
    return "\n".join(lines).strip()


def image_to_b64(path: str | Path) -> str:
    """页面图 → base64（给需要自己组请求的调用方）。"""
    return base64.b64encode(Path(path).read_bytes()).decode("ascii")
