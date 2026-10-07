"""Agent 的高亮 / 笔记手脚（《修改计划》第六步）。

**复用** `annotation_service`（同一套表、同一套增删改查、同一套 ID 前缀）—— 这里只补两件
"只有 agent 才需要"的事：

1. **按文字定位偏移量**。模型只会说"把这段文字标成高亮"，它算不出 DOM 偏移量 —— 而
   `Highlight` 存的是"某一面渲染文字里的起止偏移"。所以这里把**文字 → 偏移**这一步做掉。
2. **面感知的定位信息**。正文行 / Markdown 视图 / 原 PDF 是三套锚点（见 `docs/API.md` §8），
   "这条笔记挂在哪、点它能跳到哪"要按面分别回答；PDF 那一面服务端**看不到** pdf.js 文字层，
   所以只能查不能建（建了也没有偏移量可用）。

⚠️ 坐标系的唯一事实源在**前端**（`utils/domMarks.renderedText`：Markdown 标记不算字、
公式整体算 1 个 `U+FFFC`、只有空白的文本节点跳过、段内软换行按中/英分别接回）。
这里的 `render_like_dom()` 是它的**近似**：偏移量按近似值算，`selected_text` 一起存下来 ——
前端画的时候若发现偏移量对不上，会**按文字重新找位置**（`markRanges` 的兜底），
那才是"两边不会错位"的真正保证。
"""

from __future__ import annotations

import re

from backend.core.errors import NotFoundError, ValidationError
from backend.core.logging import get_logger
from backend.services.annotation import annotation_service as svc

log = get_logger(__name__)

#: 公式在前端坐标系里占的字符（`domMarks.ATOM`）
ATOM = "\ufffc"
#: 中文/全角：判断软换行接回时要不要补空格（与前端 `useMarkdown.CJK_OR_FULL` 同一类字符）
_CJK_OR_FULL = re.compile(r"[\u3400-\u9fff\u3000-\u303f\uff01-\uff60\uffe0-\uffe6]")
_BLOCK_START = re.compile(r"^\s{0,3}(?:#{1,6}\s|[-*+]\s|\d{1,9}[.)]\s|>|\||```|~~~|\$\$)")
_HARD_BREAK = re.compile(r"(?:\s{2,}|\\)$")
_DISPLAY_MATH = re.compile(r"\$\$(.+?)\$\$", re.DOTALL)
_INLINE_MATH = re.compile(r"(?<!\$)\$(?!\$)([^$\n]+?)\$(?!\$)")
_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
_HTML_TAG = re.compile(r"</?[a-zA-Z][^>]*>")
_LIST_MARK = re.compile(r"^\s{0,3}(?:[-*+]\s+|\d{1,9}[.)]\s+)")
_QUOTE_MARK = re.compile(r"^\s{0,3}>\s?")
_HEAD_MARK = re.compile(r"^\s{0,3}#{1,6}\s+")
_EMPHASIS = re.compile(r"(\*\*|__|\*|_|~~|`)")


def _join_soft_breaks(md: str) -> str:
    """段内软换行接回（与前端 `useMarkdown.normalizeSoftBreaks` 同一套规则）。

    分块的行文本里几乎没有这种换行，但 Markdown 视图那一面（整页 markdown）会有 ——
    不接回的话"文字对得上、偏移量差几个字节"，前端就得靠兜底找位置。
    """
    lines = md.split("\n")
    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        nxt = lines[i + 1] if i + 1 < len(lines) else None
        joinable = (nxt is not None and line.strip() and nxt.strip()
                    and not _BLOCK_START.match(line) and not _HARD_BREAK.search(line)
                    and not _BLOCK_START.match(nxt))
        if not joinable:
            out.append(line)
            i += 1
            continue
        left, right = line.rstrip(), nxt.lstrip()
        if (_CJK_OR_FULL.search(left[-1:]) or _CJK_OR_FULL.search(right[:1])):
            out.append(left + right)
        else:
            out.append(f"{left} {right}")
        i += 2
    return "\n".join(out)


def render_like_dom(md: str) -> str:
    """Markdown → 前端坐标系里的那串文字（**近似**，够用来算偏移量）。"""
    text = _COMMENT.sub("", md or "")
    text = _IMAGE.sub("", text)                  # `<img>` 不产生文字
    text = _LINK.sub(r"\1", text)
    text = _DISPLAY_MATH.sub(ATOM, text)         # 公式整体算 1 个字符
    text = _INLINE_MATH.sub(ATOM, text)
    text = _join_soft_breaks(text)
    text = _HTML_TAG.sub("", text)
    out: list[str] = []
    for raw in text.split("\n"):
        line = _QUOTE_MARK.sub("", raw)
        line = _HEAD_MARK.sub("", line)
        line = _LIST_MARK.sub("", line)
        out.append(_EMPHASIS.sub("", line))
    return "\n".join(out)


def find_span(text: str, probe: str, *, occurrence: int = 1) -> tuple[int, int] | None:
    """``probe`` 在 ``text`` 里的第 ``occurrence`` 次出现 → ``(start, end)``；找不到返回 None。"""
    probe = (probe or "").strip()
    if not probe:
        return None
    start = -1
    for _ in range(max(1, int(occurrence or 1))):
        start = text.find(probe, start + 1)
        if start < 0:
            return None
    return start, start + len(probe)


def near_misses(text: str, probe: str, *, limit: int = 3, width: int = 60) -> list[str]:
    """找不到时给模型几句"最像的原文"，让它照着改一版（与产物纠错工具同一套路）。"""
    probe = (probe or "").strip()
    if not probe or not text:
        return []
    head = probe[:8]
    out: list[str] = []
    at = text.find(head)
    if at < 0:
        # 头 8 个字也不在：退化成"按最长的公共片段找一行"
        best: tuple[int, int] = (0, -1)
        for line in text.split("\n"):
            common = 0
            for a, b in zip(line.strip(), probe):
                if a != b:
                    break
                common += 1
            if common > best[0]:
                best = (common, text.find(line.strip()[:20]))
        at = best[1] if best[1] >= 0 else -1
    if at < 0:
        return []
    out.append(text[max(0, at - 10):at + width].strip())
    return out[:limit]


def anchor_kind(anchor: str) -> str:
    """锚点属于哪一面：``row`` / ``markdown`` / ``pdf``（认不出来当 ``row``）。"""
    text = str(anchor or "").strip()
    if text.startswith("md:p"):
        return "markdown"
    if text.startswith("pdf:p"):
        return "pdf"
    return "row"


def anchor_page_no(anchor: str) -> int:
    """``md:p9`` / ``pdf:p3`` → 页号；**行锚点解不出来**（回 0，调用方只在 Markdown 分支用它）。"""
    text = str(anchor or "").strip()
    if not (text.startswith("md:p") or text.startswith("pdf:p")):
        return 0
    digits = "".join(ch for ch in text.split(":", 1)[1] if ch.isdigit())
    return int(digits) if digits else 0


_CJK_RE = re.compile(r"[\u3400-\u9fff]")


def side_of_text(text: str) -> str:
    """引用的文字是中文还是英文 → ``"zh"`` / ``"en"``。

    为什么要按文字定这一面（用户口径 2026-10-07）：高亮**只画在它那一面**上 ——
    界面按"译文"读时（`pside=zh`，读者常这么看）只渲染中文那一列，标在原文那一面的高亮
    **根本画不出来**（用户报过"未产生高亮"：锚点、文字都对，就是看不见）。
    所以 `create()` 在调用方没指定面时按文字判断：引中文 → 译文面。
    """
    return "zh" if _CJK_RE.search(str(text or "")) else "en"


def locate_in_rows(paper_id: str, page: int, text: str, *, side: str = "en",
                   occurrence: int = 1) -> tuple[str, tuple[int, int], str] | None:
    """在这一页的**正文行**里找这段文字 → ``(行 id, (start, end), 渲染后的行文本)``。

    为什么 Markdown 锚点要往这里退一步（用户口径 2026-10-07）：高亮画在**哪一面**决定了
    用户能不能看见它 —— 阅读器默认是"对照 / 译文"（正文行那一面），而 `md:pN` 的高亮只在
    Markdown 视图里出现。agent 从 `page_markdown_get` 拿到的锚点恰好是 `md:pN`，
    于是"帮我给这一段加笔记"标出来的高亮在用户眼前**根本不存在**（用户报"未产生高亮"）。
    能在行里找到就落在行上（默认那一面），找不到才留在 Markdown 面。
    """
    if not page:
        return None
    for payload in _page_payloads(paper_id):
        if int(payload.get("page") or 0) != int(page):
            continue
        for row in payload.get("rows") or []:
            raw = str(row.get("translation") if side == "zh" else row.get("original") or "")
            if not raw.strip():
                continue
            rendered = render_like_dom(raw)
            span = find_span(rendered, text, occurrence=occurrence)
            if span is not None:
                return str(row.get("id") or ""), span, rendered
        return None                      # 这一页的行里没有（别去别的页找：锚点本来就指定了页）
    return None


def _page_payloads(paper_id: str) -> list[dict]:
    from backend.services.paper import reader_service

    return list(reader_service.page_translations(paper_id) or [])


def anchor_text(paper_id: str, anchor: str, *, side: str = "en") -> str | None:
    """锚点那一面的**原始行文本 / 页 Markdown**（还没过 `render_like_dom`）。

    - 正文行：`p004-02` → 页产物里那一行的 ``original`` / ``translation``；
    - Markdown 视图：`md:p4` → 第 4 页的 ``original_md`` / ``translation_md``（md 视图渲染的就是它）；
    - 原 PDF：`pdf:p4` → **None**（文字层在前端的 pdf.js 里，服务端看不到）。
    """
    kind = anchor_kind(anchor)
    text = str(anchor or "").strip()
    payloads = _page_payloads(paper_id)
    if kind == "pdf":
        return None
    if kind == "markdown":
        try:
            page = int(text.split(":", 1)[1].lstrip("p"))
        except (IndexError, ValueError):
            return None
        for payload in payloads:
            if int(payload.get("page") or 0) == page:
                key = "translation_md" if side == "zh" else "original_md"
                return str(payload.get(key) or "")
        return None
    for payload in payloads:
        for row in payload.get("rows") or []:
            if str(row.get("id") or "") == text:
                key = "translation" if side == "zh" else "original"
                return str(row.get(key) or "")
    return None


def create(paper_id: str, *, anchor: str, text: str, color: str = "yellow",
           side: str = "", occurrence: int = 1) -> dict:
    """按**文字**建一条高亮（agent 的入口）。返回创建结果或 ``{"ok": False, "error": ...}``。

    借用 `annotation_service.create_highlight` 落库（同一套表与校验），这里只负责
    "把文字翻成偏移量"这一件事。
    """
    anchor = str(anchor or "").strip()
    if not anchor:
        raise ValidationError("anchor 必填（正文行 id / `md:p<页号>` / `pdf:p<页号>`）")
    # 面没指定（或给了个不认识的值）→ 按**引用的文字**判：引中文就落在译文面
    # （界面按译文读时，原文那一面的高亮根本画不出来，见 `side_of_text`）。
    if side not in ("en", "zh"):
        side = side_of_text(text)
    kind = anchor_kind(anchor)
    if kind == "pdf":
        return {"ok": False, "error": "原 PDF 那面的文字层只存在于阅读器里，服务端算不出偏移量："
                                      "请让用户在 PDF 页上自己划词高亮（或者改标正文行 / Markdown 视图）。"}
    if kind == "markdown":
        # Markdown 锚点的文字能在**正文行**里找到 → 落在行上（用户默认读的就是那一面，
        # 高亮才看得见；见 `locate_in_rows`）。找不到才留在 Markdown 面。
        hit = locate_in_rows(paper_id, anchor_page_no(anchor), text,
                             side=side, occurrence=occurrence)
        if hit:
            log.info("高亮锚点从 Markdown 挪到正文行（用户读的是对照 / 译文）：%s → %s",
                     anchor, hit[0])
            anchor, kind = hit[0], "row"
    raw = anchor_text(paper_id, anchor, side=side)
    if raw is None:
        return {"ok": False, "error": f"找不到这个锚点对应的文字：{anchor}（行号可能在重译后变了）"}
    rendered = render_like_dom(raw)
    span = find_span(rendered, text, occurrence=occurrence)
    if span is None:
        near = near_misses(rendered, text)
        return {"ok": False,
                "error": f"这段文字在 {anchor} 里找不到（要逐字一致，Markdown 记号不算字）。",
                "near_miss": near}
    start, end = span
    created = svc.create_highlight({
        "paper_id": paper_id, "block_id": anchor,
        "selected_text": rendered[start:end],       # 存**坐标系里的那一段**（前端画标记要靠它校验）
        "start_offset": start, "end_offset": end,
        "color": color, "side": side, "scope": kind,
    })
    created["page"] = svc.anchor_page(anchor)
    log.info("agent 建高亮 %s %s[%d:%d] %r", paper_id, anchor, start, end, rendered[start:end][:30])
    return {"ok": True, "highlight": created, "page": created["page"], "scope": kind}


def list_items(paper_id: str, *, kind: str = "highlight", query: str | None = None) -> dict:
    """列这一篇的高亮 / 笔记（都带 `page` + `scope`，笔记还带它挂靠的高亮原文）。"""
    if kind == "note":
        notes = svc.list_notes(paper_id)["items"]
        by_id = {h["id"]: h for h in svc.list_highlights(paper_id)["items"]}
        for note in notes:
            hl = by_id.get(str(note.get("highlight_id") or ""))
            note["highlight_text"] = (hl or {}).get("text")
        if query:
            notes = [n for n in notes if query in str(n.get("content_md") or "")]
        return {"items": notes, "count": len(notes)}
    items = svc.list_highlights(paper_id)["items"]
    if query:
        items = [h for h in items if query in str(h.get("text") or "")]
    return {"items": items, "count": len(items)}


def locate(paper_id: str, *, highlight_id: str = "", note_id: str = "") -> dict:
    """一条高亮 / 笔记"挂在哪、点它能跳到哪"（定位信息，供模型说清位置）。

    笔记会给它挂靠的那条高亮（用户口径：`Highlight └── Note`，高亮是笔记的位置锚点）。
    """
    if note_id:
        note = next((n for n in svc.list_notes(paper_id)["items"] if n["id"] == note_id), None)
        if note is None:
            raise NotFoundError(f"笔记不存在：{note_id}")
        hl_id = str(note.get("highlight_id") or "")
        hl = next((h for h in svc.list_highlights(paper_id)["items"] if h["id"] == hl_id), None)
        return {"ok": True, "kind": "note", "note": note,
                "highlight": hl, "anchor": note.get("block_id"),
                "page": note.get("page"), "scope": note.get("scope"),
                "hint": _locate_hint(note.get("scope"), note.get("page"), hl)}
    if not highlight_id:
        raise ValidationError("highlight_id 或 note_id 必填")
    hl = next((h for h in svc.list_highlights(paper_id)["items"] if h["id"] == highlight_id), None)
    if hl is None:
        raise NotFoundError(f"高亮不存在：{highlight_id}")
    note = next((n for n in svc.list_notes(paper_id)["items"]
                 if str(n.get("highlight_id") or "") == highlight_id), None)
    return {"ok": True, "kind": "highlight", "highlight": hl, "note": note,
            "anchor": hl.get("block_id"), "page": hl.get("page"), "scope": hl.get("scope"),
            "hint": _locate_hint(hl.get("scope"), hl.get("page"), None)}


def _locate_hint(scope, page, highlight) -> str:
    """一句人话的定位说明（模型照着说给用户听）。"""
    where = {"row": "正文行", "markdown": "Markdown 视图", "pdf": "原 PDF 页图"}.get(
        str(scope or "row"), "正文行")
    text = (highlight or {}).get("text") or ""
    if page:
        return f"第 {page} 页的{where}" + (f"，原文是「{text[:40]}」" if text else "")
    return f"{where}" + (f"，原文是「{text[:40]}」" if text else "")


def create_note(paper_id: str, *, highlight_id: str, content: str) -> dict:
    """给一条**已存在的高亮**加笔记（用户口径：笔记必须挂在高亮上，不做孤儿笔记）。"""
    highlight_id = str(highlight_id or "").strip()
    if not highlight_id:
        raise ValidationError("highlight_id 必填：笔记要挂在一处高亮上（先 highlight op=create）")
    hl = next((h for h in svc.list_highlights(paper_id)["items"] if h["id"] == highlight_id), None)
    if hl is None:
        raise NotFoundError(f"高亮不存在：{highlight_id}")
    return svc.create_note({
        "paper_id": paper_id, "block_id": hl.get("block_id"), "highlight_id": highlight_id,
        "selected_text": hl.get("text"), "start_offset": hl.get("start"),
        "end_offset": hl.get("end"), "content_md": content,
    })
