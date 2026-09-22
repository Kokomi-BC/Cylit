"""页级翻译（**唯一粒度**）——页图 + 页内文本 → 原文 Markdown + 译文 Markdown → 逐块对齐。

设计（与上一版的区别）

上一版是「按页喂文本 → 模型只回译文 → 再用 :mod:`page_aligner` 的锚点 + 保序 DP
把整页译文切回 PDF 抽出的块」，有三个固有毛病：抽取块的边界本来就不可靠、模型
合并/拆分段落时切不准、切不准就要回退块级补译（调用数翻倍）。

本版把「原文」也交给模型产出：同一页、同一次调用里，模型先**忠实转录**这一页
（OCR / 版面还原），再给出**块划分与之逐一对应**的中文译文。于是：

- **对齐是天然的**：两份 Markdown 的块按序号一一对应，不需要 DP、不需要回退补译；
- **不信任抽取结果也没关系**：抽取文本只作为参考塞进 prompt，页图才是事实来源；
- **目录中英双语**：标题块同时带原文与译文，目录直接由标题行生成；
- **只有一种粒度**：一页一次调用。图注不再单独调用，公式不再特殊处理，
  参考文献由模型原样保留（不翻译，不额外调用）。

产物

- ``md/pages.json``：逐页产物（两份 Markdown + 对齐后的行 + 用量），**翻译的唯一事实源**；
- 每页写盘即原子落盘，中断后按页续跑（``pages.json`` 里已有的页不再调用模型）。

完全信任模型：返回的行**不做校验、不做回退**。模型没给出原文/译文时只记一次
``align`` 质量标记（``exact`` / ``structure``），供前端提示，不触发任何补救调用。
"""

from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path

from backend.core.errors import ProviderError
from backend.core.logging import get_logger
from backend.core.utils import (
    json_loads, now_iso, read_json, sha256_text, write_json,
)

log = get_logger(__name__)

# 产物与版本
PAGES_FILENAME = "pages.json"
PAGES_VERSION = 3

# --------------------------------------------------------------------------
# Prompt
# --------------------------------------------------------------------------

SYSTEM_PROMPT = """你是学术论文的「页面转录 + 翻译」专家。每次给你一页论文的**页图**与**解析器抽出的页内文本**，
你要在同一次回复里产出两份互相独立、结构完全对应的 Markdown：先忠实转录原文，再给出中文译文。

【最高原则：一切以页图上**显示出来的内容**为准】
- 页图是唯一事实来源。解析器文本只作参考：它可能有断行、连字、错字、缺字、乱码、公式损坏、顺序错乱、表格串行。
- **两者冲突时，一律以页图上看到的内容为准**，包括文字、数字、单位、符号、编号、大小写与上下标。
- 解析器文本里没有、但页图上有的内容要补上；页图上没有、只在解析器文本里出现的，删掉。
- 看不清的字符写 [?]，禁止猜测、禁止用常识补全。

【original_md：原文转录】
1. 标题保留层级与编号：用 # / ## / ### 表示，编号（1、1.1、A.2）原样保留。
2. 正文按页图分段，段间空行分隔；行内公式写 $...$，独立公式写 $$...$$ 并单独成段。
3. **插图**：图注单独成段，写成 **Figure 1: ...** / **Table 2: ...**，编号必须与页图上实际编号一致
   （页图上是 Figure 2 就写 Figure 2）。图内的子图标题（(a)/(b)…）、坐标轴名、刻度、图例、
   旁边标注的文字，若页图看得清就一并写进图注所在段的下一行，看不清就省略，不要编造。
4. **表格**：用 Markdown 表格输出（`| 列 | 列 |` + `|---|---|`），**逐格照页图抄写数字与文字**：
   行名、列名、单位、每格数值都要与页图一致；页图上的粗体/分组行用 `**...**` 表示。
   页图上表格过大、无法在一屏内看清的，抄下能看清的行列，其余不要臆造。
5. 参考文献条目逐条保留（保留 [12] 这类编号），不要翻译、不要合并。
6. 丢弃页眉、页脚、页码、脚注横线、出版社水印。

【translation_md：中文译文】
1. **块划分必须与 original_md 一一对应**：第 i 块译第 i 块，块的数量、顺序、类型完全一致
   （标题对标题、段落对段落、公式对公式、图注对图注、表格对表格）。
2. 标题保留编号（例：## 3.2.1 子中心弧面损失）。
3. 公式的 LaTeX 逐字符原样保留，不改任何符号。
4. 图注/表注保留编号（例：**图 1：…**）；**表格里的数字、单位、方法名、数据集名原样保留**，
   只翻译行名/列名这类自然语言表头。
5. 参考文献条目**不翻译**：与 original_md 的对应块保持相同文本。
6. 术语按给定术语表统一；学术书面语；不增删任何内容。

【输出格式】只输出一个 JSON 对象，不要解释、不要代码块：
{"original_md": "……", "translation_md": "……"}"""

PAGE_USER_TEMPLATE = """{prefix}

【本页】第 {page} 页 / 共 {page_total} 页{section_line}
【解析器抽出的本页文本（**仅供参考**；与页图冲突时以页图为准）】
<页面文本>
{body}
</页面文本>

【本页输出】按系统提示输出 JSON：original_md 为忠实转录（一切以页图显示的内容为准，
表格逐格照抄、图注编号与页图一致），translation_md 为逐块对应的中文译文。"""

PAGE_SCHEMA = {
    "type": "object",
    "properties": {
        "original_md": {"type": "string"},
        "translation_md": {"type": "string"},
    },
    "required": ["original_md", "translation_md"],
}

# 思考模式（DeepSeek 默认开启）会把 max_tokens 吃光导致正文为空，按阶梯升档。
MAX_TOKENS_LADDER = (8192, 16384, 32768, 65536)
SAFE_MAX_TOKENS = MAX_TOKENS_LADDER[0]

# 单页原文文本送入 prompt 的上限（页图才是事实来源，文本只是辅助）
MAX_PAGE_TEXT_CHARS = 16000


def effective_max_tokens(configured: int | None) -> int:
    if configured and configured > 0:
        return max(int(configured), SAFE_MAX_TOKENS)
    return SAFE_MAX_TOKENS


def next_max_tokens(current: int) -> int | None:
    for candidate in MAX_TOKENS_LADDER:
        if candidate > current:
            return candidate
    return None


def build_stable_prefix(brief, glossary_text: str = "") -> str:
    """整篇字节级不变的前缀（论文简报 + 术语表），放在 prompt 最前以命中前缀缓存。"""
    parts: list[str] = []
    if brief is not None and getattr(brief, "enabled", False):
        block = brief.prompt_block()
        if block:
            parts.append(block)
    if glossary_text:
        parts.append(f"【术语表（必须遵守）】\n{glossary_text}")
    return "\n\n".join(parts)


def prefix_hash(prefix: str) -> str:
    return sha256_text(prefix or "")[:16]


def render_page_prompt(*, page: int, page_total: int, body: str,
                       prefix: str = "", section: str = "") -> str:
    section_line = f"；章节：{section}" if section else ""
    return PAGE_USER_TEMPLATE.format(
        prefix=prefix, page=page, page_total=page_total,
        section_line=section_line, body=body or "（解析器没有抽到文本，请完全依据页图）",
    )


# --------------------------------------------------------------------------
# 调用
# --------------------------------------------------------------------------


@dataclass
class PageCallResult:
    original_md: str
    translation_md: str
    model: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    reasoning_tokens: int = 0
    latency_ms: float = 0.0
    max_tokens: int = 0


def call_page(provider, prompt: str, image: bytes | None, *,
              max_tokens: int) -> PageCallResult:
    """一次页级调用（带页图）。图片不可用时退化为纯文本调用。"""
    try:
        if image and getattr(provider, "supports_vision", False):
            res = provider.generate_vision(prompt, [image], system=SYSTEM_PROMPT,
                                           max_tokens=max_tokens)
        else:
            res = provider.generate(prompt, system=SYSTEM_PROMPT,
                                    temperature=0.2, max_tokens=max_tokens)
    except ProviderError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise ProviderError(f"页级翻译调用失败：{str(exc)[:200]}") from exc

    original, translation = parse_pair(res.text or "")
    if not original.strip() or not translation.strip():
        raise ProviderError(
            "模型未返回完整的 original_md / translation_md（页级调用）",
            retryable=True,
        )
    return PageCallResult(
        original_md=original.strip(), translation_md=translation.strip(),
        model=getattr(res, "model", "") or provider.model_name,
        prompt_tokens=int(getattr(res, "prompt_tokens", 0) or 0),
        completion_tokens=int(getattr(res, "completion_tokens", 0) or 0),
        reasoning_tokens=int(getattr(res, "reasoning_tokens", 0) or 0),
        latency_ms=float(getattr(res, "latency_ms", 0.0) or 0.0),
        max_tokens=max_tokens,
    )


def parse_pair(text: str) -> tuple[str, str]:
    """从模型回复里取 ``original_md`` / ``translation_md``（容错解析）。

    真实调用里可能出现的形状：纯 JSON、带 ```json 围栏、JSON 前后带一句说明。
    都按「先找 JSON 对象，再退回标记切分」处理；仍然失败时返回空串由调用方报错。
    """
    raw = (text or "").strip()
    if not raw:
        return "", ""
    data = _loads_lenient(raw)
    if isinstance(data, dict):
        return str(data.get("original_md") or ""), str(data.get("translation_md") or "")
    # 退路：模型没用 JSON，而是用标记分了两段
    orig = _between(raw, "【原文】", "【译文】") or _between(raw, "<original_md>", "</original_md>")
    trans = _between(raw, "【译文】", None) or _between(raw, "<translation_md>", "</translation_md>")
    return orig or "", trans or ""


def _loads_lenient(raw: str) -> dict | None:
    candidates = [raw]
    fence = re.search(r"```(?:json)?\s*(.*?)```", raw, re.DOTALL)
    if fence:
        candidates.append(fence.group(1).strip())
    start, end = raw.find("{"), raw.rfind("}")
    if 0 <= start < end:
        candidates.append(raw[start:end + 1])
    for cand in candidates:
        try:
            data = json.loads(cand)
        except (ValueError, TypeError):
            continue
        if isinstance(data, dict) and ("original_md" in data or "translation_md" in data):
            return data
    return json_loads(raw, None) if raw.startswith("{") else None


def _between(text: str, start: str, end: str | None) -> str | None:
    i = text.find(start)
    if i < 0:
        return None
    i += len(start)
    if end is None:
        return text[i:].strip()
    j = text.find(end, i)
    return (text[i:j] if j >= 0 else text[i:]).strip()


# --------------------------------------------------------------------------
# Markdown → 块
# --------------------------------------------------------------------------

_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
_NUMBER = re.compile(r"^(\d{1,2}(?:\.\d{1,2}){0,3}|[A-Z](?:\.\d{1,2}){1,3})\b")
_LIST_ITEM = re.compile(r"^\s*(?:[-*+]|\d{1,2}[.)])\s+\S")
_CAPTION = re.compile(r"^\*{0,2}\s*(Figure|Fig\.|Table|Algorithm|Listing|图|表|算法)\s*\.?\s*\d+",
                      re.IGNORECASE)
_REF_ITEM = re.compile(r"^\s*\[?\d{1,3}\]?[.)]?\s+\S")


def split_blocks(md: str) -> list[dict]:
    """把一页 Markdown 切成块（标题 / 段落 / 公式 / 图注 / 列表 / 表格 / 代码）。

    切法是**纯结构**的：空行分段、`$$` 围栏成块、连续列表项并成一块。
    左右两侧用同一套切法，于是"块数相同"就意味着块序号一一对应。
    """
    lines = (md or "").replace("\r\n", "\n").split("\n")
    blocks: list[dict] = []
    buf: list[str] = []
    mode = "para"          # para | formula | code | list | table
    level = 0

    def flush() -> None:
        nonlocal buf, mode, level
        text = "\n".join(buf).strip()
        buf = []
        if not text:
            mode, level = "para", 0
            return
        btype = mode
        if mode == "para":
            if _CAPTION.match(text):
                btype = "caption"
            elif _REF_ITEM.match(text) and text.count("\n") == 0:
                btype = "ref"
        blocks.append({"type": btype, "level": level if btype == "heading" else 0,
                       "number": _number_of(text) if btype == "heading" else "",
                       "text": text})
        mode, level = "para", 0

    for raw in lines:
        line = raw.rstrip()
        stripped = line.strip()

        if mode == "code":
            buf.append(line)
            if stripped.startswith("```"):
                flush()
            continue
        if mode == "formula":
            buf.append(line)
            if stripped.endswith("$$") and len(stripped) >= 2:
                flush()
            continue

        if not stripped:
            flush()
            continue
        if stripped.startswith("```"):
            flush()
            buf, mode = [line], "code"
            continue
        if stripped.startswith("$$"):
            flush()
            buf, mode = [line], "formula"
            if stripped.endswith("$$") and len(stripped) > 2:
                flush()
            continue

        heading = _HEADING.match(stripped)
        if heading:
            flush()
            buf = [stripped]
            mode, level = "heading", len(heading.group(1))
            flush()
            continue

        if stripped.startswith("|") and stripped.endswith("|"):
            if mode != "table":
                flush()
                mode = "table"
            buf.append(line)
            continue
        if _LIST_ITEM.match(stripped):
            if mode != "list":
                flush()
                mode = "list"
            buf.append(line)
            continue

        if mode in ("table", "list"):
            # 表格/列表块被普通段落打断
            flush()
        buf.append(line)

    flush()
    return blocks


def _number_of(text: str) -> str:
    first = (text or "").split("\n", 1)[0]
    first = re.sub(r"^[\s#]+", "", first.strip())
    m = _NUMBER.match(first)
    return m.group(1) if m else ""


# --------------------------------------------------------------------------
# 两份 Markdown → 对齐后的行
# --------------------------------------------------------------------------


def pair_rows(original_md: str, translation_md: str, page: int) -> tuple[list[dict], str]:
    """把两份 Markdown 按块对齐成行。

    块数相同 → 直接按序号配对（``exact``，绝大多数情况）。
    否则用 ``difflib`` 按「类型 + 编号/文本签名」做一次序列对齐（``structure``），
    多出来的块保留为单边行（原文有译文空，或反之），**不丢弃任何内容**。
    """
    left = split_blocks(original_md)
    right = split_blocks(translation_md)
    if not left and not right:
        return [], "empty"
    pairs: list[tuple[dict | None, dict | None]] = []
    if len(left) == len(right):
        pairs = list(zip(left, right))
        quality = "exact"
    else:
        pairs = _sequence_pairs(left, right)
        quality = "structure"
    rows: list[dict] = []
    for i, (a, b) in enumerate(pairs):
        head = a or b or {}
        rows.append({
            "id": f"p{page:03d}-{i:02d}",
            "index": i,
            "type": head.get("type") or "para",
            "level": int(head.get("level") or 0),
            "number": head.get("number") or "",
            "original": (a or {}).get("text") or "",
            "translation": (b or {}).get("text") or "",
        })
    return rows, quality


def _sequence_pairs(left: list[dict], right: list[dict]
                    ) -> list[tuple[dict | None, dict | None]]:
    keys_l = [_row_key(b) for b in left]
    keys_r = [_row_key(b) for b in right]
    matcher = SequenceMatcher(a=keys_l, b=keys_r, autojunk=False)
    out: list[tuple[dict | None, dict | None]] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            out.extend(zip(left[i1:i2], right[j1:j2]))
        elif tag == "replace":
            span = max(i2 - i1, j2 - j1)
            for k in range(span):
                out.append((left[i1 + k] if i1 + k < i2 else None,
                            right[j1 + k] if j1 + k < j2 else None))
        elif tag == "delete":
            out.extend((b, None) for b in left[i1:i2])
        elif tag == "insert":
            out.extend((None, b) for b in right[j1:j2])
    return out


def _row_key(block: dict) -> tuple:
    btype = block.get("type") or "para"
    text = (block.get("text") or "").strip()
    if btype == "heading":
        return ("h", int(block.get("level") or 0), block.get("number") or _norm(text)[:24])
    if btype == "formula":
        return ("f", _norm(text)[:60])
    return (btype, _norm(text)[:40])


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").lower()).strip()


# --------------------------------------------------------------------------
# 页产物
# --------------------------------------------------------------------------


def build_page_payload(*, page: int, page_total: int, result: PageCallResult,
                       source_chars: int = 0, cached: bool = False,
                       align_override: str | None = None) -> dict:
    rows, quality = pair_rows(result.original_md, result.translation_md, page)
    return {
        "page": page,
        "page_total": page_total,
        "original_md": result.original_md,
        "translation_md": result.translation_md,
        "rows": rows,
        "align": align_override or quality,
        "model": result.model,
        "prompt_tokens": result.prompt_tokens,
        "completion_tokens": result.completion_tokens,
        "reasoning_tokens": result.reasoning_tokens,
        "latency_ms": round(result.latency_ms, 1),
        "max_tokens": result.max_tokens,
        "source_chars": source_chars,
        "cached": cached,
        "updated_at": now_iso(),
    }


class PageStore:
    """``md/pages.json`` 的读写（逐页原子落盘 → 中断可续跑）。"""

    def __init__(self, paper_id: str, path: Path | None = None) -> None:
        self.paper_id = paper_id
        self.path = path
        self._lock = threading.RLock()
        self._data: dict | None = None

    # ---------------- 读写 ----------------
    def _load(self) -> dict:
        with self._lock:
            if self._data is not None:
                return self._data
            if self.path is not None and self.path.is_file():
                data = read_json(self.path, {}) or {}
                pages = data.get("pages") if isinstance(data.get("pages"), dict) else {}
                self._data = {
                    "paper_id": data.get("paper_id") or self.paper_id,
                    "version": int(data.get("version") or PAGES_VERSION),
                    "page_total": int(data.get("page_total") or 0),
                    "model": data.get("model") or "",
                    "updated_at": data.get("updated_at"),
                    "pages": {int(k): v for k, v in pages.items()
                              if isinstance(v, dict)},
                }
            else:
                self._data = {"paper_id": self.paper_id, "version": PAGES_VERSION,
                              "page_total": 0, "model": "", "updated_at": None,
                              "pages": {}}
            return self._data

    def flush(self) -> None:
        with self._lock:
            data = self._load()
            data["version"] = PAGES_VERSION
            data["updated_at"] = now_iso()
            if self.path is not None:
                write_json(self.path, {
                    **{k: v for k, v in data.items() if k != "pages"},
                    "pages": {str(k): v for k, v in sorted(data["pages"].items())},
                })

    def put(self, page: int, payload: dict) -> None:
        with self._lock:
            data = self._load()
            data["pages"][int(page)] = payload
            if payload.get("page_total"):
                data["page_total"] = int(payload["page_total"])
            if payload.get("model"):
                data["model"] = str(payload["model"])
            self.flush()

    def clear(self) -> int:
        with self._lock:
            data = self._load()
            n = len(data["pages"])
            data["pages"] = {}
            self.flush()
            return n

    # ---------------- 查询 ----------------
    def page(self, page: int) -> dict | None:
        return self._load()["pages"].get(int(page))

    def all_pages(self) -> dict[int, dict]:
        return dict(self._load()["pages"])

    def done_pages(self) -> list[int]:
        return sorted(p for p, v in self._load()["pages"].items()
                      if (v.get("rows") or v.get("translation_md")))

    def set_page_total(self, total: int) -> None:
        with self._lock:
            data = self._load()
            if int(data.get("page_total") or 0) != int(total):
                data["page_total"] = int(total)
                self.flush()

    def stats(self) -> dict:
        data = self._load()
        pages = data["pages"]
        rows = sum(len(v.get("rows") or []) for v in pages.values())
        return {
            "page_total": int(data.get("page_total") or 0),
            "pages_done": len([p for p, v in pages.items()
                               if (v.get("rows") or v.get("translation_md"))]),
            "pages_failed": len([p for p, v in pages.items() if v.get("error")]),
            "rows": rows,
            "model": data.get("model") or "",
            "updated_at": data.get("updated_at"),
            "prompt_tokens": sum(int(v.get("prompt_tokens") or 0) for v in pages.values()),
            "completion_tokens": sum(int(v.get("completion_tokens") or 0) for v in pages.values()),
            "reasoning_tokens": sum(int(v.get("reasoning_tokens") or 0) for v in pages.values()),
            "align": _align_counts(pages),
        }


def _align_counts(pages: dict) -> dict:
    out: dict[str, int] = {}
    for v in pages.values():
        key = str(v.get("align") or "?")
        out[key] = out.get(key, 0) + 1
    return out


# --------------------------------------------------------------------------
# 页清单 / 页内文本
# --------------------------------------------------------------------------

_PAGE_PNG = re.compile(r"page-(\d+)\.png$")


def list_pages(paper_id: str) -> list[int]:
    """该论文的页号清单（以渲染出来的页图为准；没有页图时退回 IR 的页数）。"""
    from backend.services.paper import paper_service

    paths = paper_service.paper_paths(paper_id)
    pages_dir: Path = paths["pages"]
    nums: list[int] = []
    if pages_dir.is_dir():
        for p in pages_dir.iterdir():
            m = _PAGE_PNG.search(p.name)
            if m:
                nums.append(int(m.group(1)))
    if nums:
        return sorted(set(nums))
    try:
        from backend.parsing.ir.ir_reader import IRReader

        ir = IRReader(paths["ir"])
        if ir.exists:
            return [int(p["page_no"]) for p in ir.pages()]
    except Exception as exc:  # noqa: BLE001
        log.warning("读取页清单失败：%s", exc)
    return []


def page_source_text(paper_id: str, page: int) -> tuple[str, str]:
    """该页的抽取文本 + 章节名（供 prompt 参考）与字符数。"""
    from backend.services.paper import reader_service

    index = reader_service.load_block_index(paper_id)
    rows = [b for b in (index.get("blocks") or []) if int(b.get("page") or 0) == int(page)]
    rows.sort(key=lambda b: b.get("order") or 0)
    section = next((b.get("section") for b in rows if b.get("section")), "") or ""
    parts: list[str] = []
    size = 0
    truncated = False
    for b in rows:
        text = (b.get("text") or "").strip()
        if not text:
            continue
        btype = b.get("type") or "para"
        if btype == "heading" and not text.lstrip().startswith("#"):
            text = f"{'#' * max(1, min(6, int(b.get('heading_level') or 2)))} {text}"
        if size + len(text) > MAX_PAGE_TEXT_CHARS:
            truncated = True
            break
        parts.append(text)
        size += len(text)
    if truncated:
        parts.append("……（本页文本过长，已截断；请完全依据页图）")
    return "\n\n".join(parts), section


def load_page_image(paper_id: str, page: int) -> bytes | None:
    from backend.services.paper import paper_service

    path = paper_service.paper_paths(paper_id)["pages"] / f"page-{page:04d}.png"
    if not path.is_file():
        return None
    try:
        return path.read_bytes()
    except OSError as exc:
        log.warning("读取页图失败 p%d：%s", page, exc)
        return None


def page_rows(paper_id: str, store: PageStore | None = None) -> list[dict]:
    """阅读器用：按页序拼出全部行（含页信息）。"""
    store = store or PageStore(paper_id, _default_store_path(paper_id))
    out: list[dict] = []
    for page, payload in sorted(store.all_pages().items()):
        for row in payload.get("rows") or []:
            out.append({**row, "page": page})
    return out


def _default_store_path(paper_id: str) -> Path:
    from backend.services.paper import paper_service

    return paper_service.paper_paths(paper_id)["md"] / PAGES_FILENAME
