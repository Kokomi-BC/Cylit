"""图片批注文档（大图查看器里的手写笔迹 + 图上的笔记）。

**为什么另起一套坐标**：正文高亮存的是"某一面渲染文字里的字符偏移"，而这里存的是
**图片自身归一化坐标**（0..1，左上为原点）—— 图片会被缩放、旋转、换设备（平板 → 电脑），
只有归一化坐标能跟着图片走。所以两份东西不能混在同一张表里。

**为什么整份文档一起存**：撤销 / 橡皮 / 加一条笔记，都是"这一份批注"的变化，
拆成一条条记录反而要处理"哪条属于哪次撤销"。所以 `(paper_id, key)` 一行、一份 JSON，
前端编辑完防抖 PUT 覆盖 —— 用户口径是"默认保存"，没有保存按钮。

归一化值的来源与渲染在 `frontend/src/utils/imageInk.ts`（同一套 clamp 与上限）。
"""

from __future__ import annotations

import re

from backend.core.errors import ValidationError
from backend.core.logging import get_logger
from backend.core.utils import iso_utc, json_dumps, json_loads, new_id
from backend.repositories.mysql.models import ImageMark
from backend.repositories.mysql.session import session_scope

log = get_logger(__name__)

#: 一份文档的 JSON 上限（400KB）：笔迹是"点数组"，一页密密麻麻也就几十 KB，
#: 超过这个量级基本是前端出了循环 bug —— 宁可报错也不要往库里灌。
DOC_MAX_BYTES = 400_000
#: 单份文档最多多少笔 / 多少条笔记
STROKE_MAX = 400
NOTE_MAX = 200
#: 一笔最多几个点（一笔画 4000 点足够写满一页；再多是长按抖动）
POINT_MAX = 4000
NOTE_MAX_CHARS = 4000

TOOLS = ("pen", "highlighter")
#: 图片锚点：`media:<sha1(url)>` / `page:<页号>` / `rel:<相对路径哈希>`
KEY_RE = re.compile(r"^(?:media|page|rel):[A-Za-z0-9._-]{1,120}$")
_COLOR_RE = re.compile(r"^#[0-9a-f]{6}$")

#: 笔迹粗细（**图片宽度的比例**）：0.002 ≈ 一张 1000px 宽的图上 2px
W_MIN, W_MAX = 0.0005, 0.06
#: 橡皮半径上限同上
ERASE_MAX = 0.2


def empty_doc() -> dict:
    return {"v": 1, "strokes": [], "notes": []}


def check_key(key: object) -> str:
    """校验图片锚点。`key` 是**图**的身份，不是 URL —— 见模块头。"""
    text = str(key or "").strip()
    if not text or len(text) > 160 or not KEY_RE.match(text):
        raise ValidationError("图片锚点不合法（应为 media:/page:/rel: 前缀 + 短哈希）",
                              detail={"key": text[:80]})
    return text


def _num(raw: object, lo: float, hi: float, default: float | None = None) -> float:
    try:
        val = float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        if default is None:
            raise ValidationError("坐标/粗细必须是数字") from None
        return default
    if val != val or val in (float("inf"), float("-inf")):   # NaN / inf
        return default if default is not None else lo
    return min(hi, max(lo, val))


def _points(raw: object) -> list[list[float]]:
    if not isinstance(raw, list):
        return []
    out: list[list[float]] = []
    for item in raw[:POINT_MAX]:
        if not isinstance(item, (list, tuple)) or len(item) < 2:
            continue
        out.append([round(_num(item[0], 0.0, 1.0), 5),
                    round(_num(item[1], 0.0, 1.0), 5)])
    return out


def _stroke(raw: object) -> dict | None:
    if not isinstance(raw, dict):
        return None
    pts = _points(raw.get("pts"))
    if len(pts) < 2:      # 一个点画不出线（误触/点一下）
        return None
    tool = str(raw.get("tool") or "pen")
    if tool not in TOOLS:
        tool = "pen"
    color = str(raw.get("color") or "#111111").lower()
    if not _COLOR_RE.match(color):
        color = "#111111"
    return {
        "id": str(raw.get("id") or new_id("s"))[:32],
        "tool": tool,
        "color": color,
        "w": round(_num(raw.get("w"), W_MIN, W_MAX, 0.004), 5),
        "pts": pts,
    }


def _rect(raw: object) -> list[float] | None:
    if not isinstance(raw, (list, tuple)) or len(raw) < 4:
        return None
    x0 = _num(raw[0], 0.0, 1.0)
    y0 = _num(raw[1], 0.0, 1.0)
    x1 = _num(raw[2], 0.0, 1.0)
    y1 = _num(raw[3], 0.0, 1.0)
    left, right = min(x0, x1), max(x0, x1)
    top, bottom = min(y0, y1), max(y0, y1)
    if right - left < 0.004 or bottom - top < 0.004:   # 退化成一个点 → 补成小块
        cx, cy = (left + right) / 2, (top + bottom) / 2
        left, right = max(0.0, cx - 0.02), min(1.0, cx + 0.02)
        top, bottom = max(0.0, cy - 0.02), min(1.0, cy + 0.02)
    return [round(left, 5), round(top, 5), round(right, 5), round(bottom, 5)]


def _note(raw: object) -> dict | None:
    if not isinstance(raw, dict):
        return None
    text = str(raw.get("text") or "").strip()[:NOTE_MAX_CHARS]
    if not text:      # 空笔记 = 用户点了"取消"，不落库
        return None
    rect = _rect(raw.get("rect"))
    if rect is None:
        return None
    block_id = raw.get("block_id")
    return {
        "id": str(raw.get("id") or new_id("n"))[:32],
        "rect": rect,
        "text": text,
        "block_id": (str(block_id)[:32] if block_id else None),
        "at": str(raw.get("at") or "")[:32] or None,
    }


def normalize_doc(raw: object) -> dict:
    """把前端 POST 上来的文档洗成**可信形状**（越界值截断、脏项丢弃）。

    为什么服务端也要洗：这是一份"随便什么形状都可能"的 JSON blob，
    直接存下去等于把前端的 bug 永久留在库里 —— 渲染时再炸就更难查了。
    """
    if raw is None:
        return empty_doc()
    if not isinstance(raw, dict):
        raise ValidationError("批注文档必须是对象")
    strokes_raw = raw.get("strokes", [])
    notes_raw = raw.get("notes", [])
    if not isinstance(strokes_raw, list) or not isinstance(notes_raw, list):
        raise ValidationError("strokes / notes 必须是数组")
    strokes = [s for s in (_stroke(item) for item in strokes_raw[:STROKE_MAX]) if s]
    notes = [n for n in (_note(item) for item in notes_raw[:NOTE_MAX]) if n]
    doc = {"v": 1, "strokes": strokes, "notes": notes}
    if len(json_dumps(doc)) > DOC_MAX_BYTES:
        raise ValidationError("这批批注太大了（超过 400KB），请先撤销一部分再试")
    return doc


def load(paper_id: str, key: str, user_id: int = 1) -> dict:
    """取一份批注文档。没存过就回空文档（前端不需要区分"没画过"和"存了个空的"）。"""
    pid = str(paper_id or "").strip()
    if not pid:
        raise ValidationError("缺少 paper_id", detail={"paper_id": pid})
    anchor = check_key(key)
    with session_scope() as sess:
        row = (sess.query(ImageMark)
               .filter(ImageMark.user_id == user_id, ImageMark.paper_id == pid,
                       ImageMark.key == anchor)
               .one_or_none())
        if row is None:
            return {"key": anchor, "doc": empty_doc(), "exists": False,
                    "updated_at": None}
        return {"key": anchor, "doc": normalize_doc(json_loads(row.doc_json, {})),
                "exists": True, "updated_at": iso_utc(row.updated_at)}


def save(paper_id: str, key: str, doc: object, user_id: int = 1) -> dict:
    """覆盖写一份批注文档。**空文档 = 删行**（撤销到干净状态后不留垃圾行）。"""
    pid = str(paper_id or "").strip()
    if not pid:
        raise ValidationError("缺少 paper_id", detail={"paper_id": pid})
    anchor = check_key(key)
    clean = normalize_doc(doc)
    if not clean["strokes"] and not clean["notes"]:
        with session_scope() as sess:
            (sess.query(ImageMark)
             .filter(ImageMark.user_id == user_id, ImageMark.paper_id == pid,
                     ImageMark.key == anchor)
             .delete(synchronize_session=False))
        return {"key": anchor, "saved": True, "empty": True, "bytes": 0,
                "updated_at": None}

    text = json_dumps(clean)
    with session_scope() as sess:
        row = (sess.query(ImageMark)
               .filter(ImageMark.user_id == user_id, ImageMark.paper_id == pid,
                       ImageMark.key == anchor)
               .one_or_none())
        if row is None:
            row = ImageMark(id=new_id("imk"), user_id=user_id, paper_id=pid,
                            key=anchor, doc_json=text)
            sess.add(row)
        else:
            row.doc_json = text
        sess.flush()
        stamps = {"key": anchor, "saved": True, "empty": False,
                  "bytes": len(text.encode("utf-8")),
                  "updated_at": iso_utc(row.updated_at)}
    log.debug("批注已存：paper=%s key=%s bytes=%s", pid, anchor, stamps["bytes"])
    return stamps
