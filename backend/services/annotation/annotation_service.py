"""批注 / 笔记 / 高亮服务（规划 §10 ⑤⑥⑦）。

- 高亮锚定 ``block_id + 字符偏移``（规划 §10 ⑤），重译后按 ``anchor_hash`` 重定位
  —— 哈希**pid 优先**（``row_pid`` 取得就 pid+偏移+侧，取不到才退回 block_id 的旧形式，
  见 ``anchor_hash`` 与 ``docs/身份模型.md`` §8）；
- **划词解释已改由问答 agent 负责**（`/api/v1/agent/ask` + `selection`），本模块只负责
  把 agent 给出的解释**存成批注**（`source=selection`）—— 旧的那条"自己拼 prompt 调
  generation provider"的链路已删除，见 dev-log 第二十轮。
"""

from __future__ import annotations

import re

from backend.core.errors import NotFoundError, ValidationError
from backend.core.events import EVENT_ANNOTATION, bus
from backend.core.logging import get_logger
from backend.core.utils import (
    iso_utc, json_dumps, json_loads, new_id, sha256_text,
)
from backend.repositories.mysql.models import (
    HIGHLIGHT_SCOPES, HIGHLIGHT_SIDES, Annotation, Block, Highlight, Note, Paper,
)
from backend.repositories.mysql.session import session_scope

log = get_logger(__name__)

#: 高亮色板：**色名 → 落库色值**（唯一真相）。
#:
#: 口径（2026-12-10）：库里**只存 hex**。色名只是"输入别名"，进门由 `_norm_color` 换成 hex ——
#: 于是"同一个颜色"在库里不会再出现两种写法（前端还在传色名，所以两种都得收）。
#:
#: 这一份与 `frontend/src/utils/highlightColors.ts` 的 `HL_PALETTE` 是**同一组色值**
#: （那边写 `rgba(...)`，这边落成 `#rrggbb`；`tests/test_highlight_color.py` 会比对两边）。
#: **这份是唯一真相，前端那份要与它一致**。
#:
#: **alpha 只影响渲染浓度，不进口径**：`rgba(230, 180, 20, .95)` 里的 `.95` 说的是"画多浓"，
#: 不是颜色本身，所以 hex 不带 alpha（前端 hex 分支自己按 40% 铺 `--hl-bg`）。
#: `pink` / `purple` 是历史名，留着是为了**存量高亮**仍能通过校验、仍能渲染；新界面不再提供它们。
HIGHLIGHT_PALETTE: dict[str, str] = {
    # —— 多巴胺色板（2026-12 用户口径：**所有菜单的色板都换成这一套**）——
    #
    # ⚠️ 历史那九个色名（red / orange / yellow / green / blue / indigo / violet / pink /
    # purple）已从色板里删掉（用户口径 2026-12）：界面不再提供、没人再往里存，留着只会让
    # 「哪个颜色算合法」永远多一档。它们的落库值本来就是 hex，库里的存量高亮一个字节都不用改；
    # 只有「用户嘴上说黄的却被当成没听见」这件事不能接受，所以旧名与中文色词另外走一张
    # RETIRED_COLOR_ALIASES 翻译成新 id（见下），既不进色板也不进菜单。
    # 八个高饱和色，铺 40% 在纸面上正好是「亮而不糊」那档：以前那批色名混着低饱和的
    # 靛/紫，一片标下来容易显脏。id 用英文是为了进 URL / 日志 / 模型入参时不带转义。
    "strawberry": "#ff4d6d",  # 草莓红
    "tangerine": "#ff8a3d",   # 橘子橙
    "lemon": "#ffc93c",       # 柠檬黄
    "mint": "#3ddc84",        # 薄荷绿
    "lagoon": "#22d3ee",      # 湖蓝
    "vega": "#4d7cff",        # 电光蓝
    "grape": "#a855f7",       # 葡萄紫
    "sakura": "#ff5cc8",      # 樱花粉
}

#: 色板顺序（= 菜单上色点的顺序）。
HIGHLIGHT_COLORS = tuple(HIGHLIGHT_PALETTE)

#: **旧色名 / 中文色词 → 新色板 id** 的兜底（不是色板，不进菜单）。
#: 为什么还留着：用户会说「标成黄的」、模型会照着说 `yellow`；把这类词当「没指定」会变成
#: 随机配色，比多一档别名更让人意外。这里只做**翻译**，落库仍然是 hex。
RETIRED_COLOR_ALIASES: dict[str, str] = {
    # 英文旧名（2026-12 之前界面上那九个）
    "red": "strawberry", "orange": "tangerine", "yellow": "lemon",
    "green": "mint", "blue": "lagoon", "indigo": "vega",
    "violet": "grape", "purple": "grape", "pink": "sakura",
    # 中文色词（用户原话怎么说的就怎么认）
    "红": "strawberry", "红色": "strawberry",
    "橙": "tangerine", "橙色": "tangerine", "橘": "tangerine", "橘色": "tangerine",
    "黄": "lemon", "黄色": "lemon", "金": "lemon", "金色": "lemon",
    "绿": "mint", "绿色": "mint",
    "青": "lagoon", "青色": "lagoon", "蓝": "lagoon", "蓝色": "lagoon",
    "靛": "vega", "靛蓝": "vega", "电光蓝": "vega",
    "紫": "grape", "紫色": "grape", "葡萄紫": "grape",
    "粉": "sakura", "粉色": "sakura", "樱花粉": "sakura",
}


# --------------------------------------------------------------------------
# 高亮（规划 §10 ⑤）
# --------------------------------------------------------------------------


def _norm_color(raw: object) -> str:
    """色名 / 任意色 → **落库色值**（色名一律换成 `HIGHLIGHT_PALETTE` 里的 hex）。

    为什么要把色名换成 hex：同一个颜色以前能存成 `yellow` 或 `#e6b414` 两种写法，
    统计、去重、前端比较都得判两遍。色名仍然**接受**（前端还在传），但输出只有 hex 一种 ——
    `_norm_color("yellow") == _norm_color("#e6b414")`。

    为什么后端必须放行自定义色（用户口径 2026-10-10「添加自定义颜色」）：以前未知色会被
    **静默退回一个固定色** —— 取色器选的颜色存进去变成别的色，用户只会当成"取色器坏了"。
    """
    c = str(raw or "").strip().lower()
    if c in HIGHLIGHT_PALETTE:
        return HIGHLIGHT_PALETTE[c]
    # 旧色名 / 中文色词：翻译成新 id 再取 hex（`yellow` → lemon、`黄` → lemon）——
    # 删掉老名字不等于「用户说黄的就当没听见」，这一步只翻译、不落名字。
    alias = RETIRED_COLOR_ALIASES.get(c)
    if alias:
        return HIGHLIGHT_PALETTE[alias]
    # 任意色（用户口径 2026-11-26「允许 agent 自定义任意颜色」）：hex 3/6/8 位、rgb()/hsl() 一律放行，
    # 只有真的认不出来的写法才退回**色板第一个**（静默改色最坑，见上面的说明；退回前打一条 warning，
    # 至少日志里查得到是谁传了个什么）。
    if re.fullmatch(r"#(?:[0-9a-f]{3}|[0-9a-f]{6}|[0-9a-f]{8})", c):
        return c
    if re.fullmatch(r"(?:rgb|rgba|hsl|hsla)\([0-9a-f.,%\s/]+\)", c):
        return c
    fallback = HIGHLIGHT_PALETTE[HIGHLIGHT_COLORS[0]]
    log.warning("认不出的颜色写法 %r：退回色板第一个 %s", raw, fallback)
    return fallback


#: 正文行 id（行 id = 位置编号，``p015-06``）：只有这种锚点才可能有行身份 pid
_ROW_ID_RE = re.compile(r"^p(\d{1,5})-\d+$")


def row_pid(paper_id: str | None, block_id: str | None) -> str | None:
    """锚点（正文行 id）→ 这一行的稳定身份 pid；取不到给 ``None``（**不猜**）。

    pid **只从页产物取**（``reader_service._finalize_page`` 是唯一写 ``row["pid"]`` 的地方），
    所以这里问 ``merged_page``（单页就够：高亮一定落在某一页的行上，没必要整篇重算）——
    自己按 block_id 推出来的 pid 与阅读器下发的对不上，存下来就是错的。
    ``md:p3`` / ``pdf:p1`` 这类面没有行身份，老锚点（解析块 id）也没有，一律 None，
    由调用方退回旧口径（见 ``anchor_hash``）。
    """
    text = str(block_id or "").strip()
    match = _ROW_ID_RE.match(text)
    if not paper_id or not match:
        return None
    try:
        from backend.services.paper import reader_service

        page = reader_service.merged_page(str(paper_id), int(match.group(1)))
        for row in (page or {}).get("rows") or []:
            if isinstance(row, dict) and str(row.get("id") or "") == text:
                return str(row.get("pid") or "").strip() or None
    except Exception as exc:  # noqa: BLE001 —— 取不到 pid 只是退回旧哈希，不许挡住"存高亮"
        log.warning("取行身份失败（这条高亮按旧口径存）：%s/%s: %s", paper_id, text, exc)
    return None


def anchor_hash(selected_text: object, block_id: str, side: str,
                start: object = 0, end: object = 0, *, pid: str | None = None) -> str:
    """高亮的重定位键 —— "重译后按它找回同一条"（规划 §10 ⑤）。

    **pid 优先**（阶段 2）：pid 是行的稳定身份（生成一次、重建时按指纹继承），而
    ``block_id`` 只是位置编号（``p015-06``，页内插一行后面全换号）。旧实现把 block_id
    拼进哈希，于是"id 一变哈希就变"，``docs/身份模型.md`` §8 记的"重译后按 anchor_hash
    重定位从未生效"就是这一条。拿得到 pid 时用 ``pid + start + end + side``（冒号拼，
    免得 a|b 与 ab| 撞在一起；side 是同一行的第二维 —— 原文/译文共用 block_id，
    不加它两侧的哈希会撞）。

    拿不到 pid（markdown / pdf 面、老锚点、注册表还没生成）时**逐字节**退回旧形式
    ``selected_text + block_id + side``：存量高亮的哈希不变，向后兼容。
    """
    if pid:
        return sha256_text(f"{pid}:{start}:{end}:{side}")[:64]
    return sha256_text(str(selected_text or "") + str(block_id) + side)[:64]


#: 定位不到时给用户看的那句话：要能直接显示（不出现 0/0、不出现栈帧）
_SPAN_NOT_FOUND = ("这段文字在当前页找不到，可能换了译本或跨了行 —— "
                   "请在正文里重新划一次")


def _quote_hint(text: object) -> str:
    """错误回执里的引文（截断：一条超长选区不该把消息撑满）。"""
    body = re.sub(r"\s+", " ", str(text or "")).strip()
    return f"「{body[:24]}{'…' if len(body) > 24 else ''}」"


def _offset(value: object) -> int:
    """偏移量：非数字 / 负数一律当 0（脏值交给定位去纠正，不许变成 500）。"""
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def side_for_text(raw: object, text: object = "") -> str:
    """这条高亮该落**哪一面**（`en` / `zh`）。

    与 agent 那条路同一口径（`agent.annotations.create`）：

    - 请求里给了面就尊重它（`original` / `translation` 这类别名也认）；
    - 没给、或给了个认不出的写法 → 按**引用的文字**判；
    - **文字里有中文就只能是译文面**：中文引文存进 `en` 面，用户切到译文时那条高亮
      根本画不出来（agent 路径 2026-12-07 实测过同一个坑，用户 2026-12 又复报一次）。
    """
    from backend.services.agent.annotations import normalize_side, side_of_text

    detected = side_of_text(str(text or ""))
    if detected == "zh":
        return "zh"
    return normalize_side(raw) or detected


def _block_page_no(block_id: str) -> int:
    """行锚点 → 页码：**先问 block 表**（`page_no` 是解析时定的），解不出才从行 id 里读。"""
    text = str(block_id or "").strip()
    try:
        with session_scope() as sess:
            row = sess.get(Block, text)
            if row is not None and int(row.page_no or 0) > 0:
                return int(row.page_no)
    except Exception as exc:  # noqa: BLE001 —— 只是"去哪一页找"，取不到退回行 id
        log.warning("取块页码失败（按行 id 解页号）：%s: %s", text, exc)
    return int(anchor_page(text) or 0)


def _row_side_text(paper_id: str, block_id: str, side: str) -> str | None:
    """这一行在 `side` 那一面的**渲染文字**（与存库的偏移量同一坐标系）；取不到给 None。"""
    from backend.services.agent.annotations import anchor_text, render_like_dom

    try:
        raw = anchor_text(paper_id, block_id, side=side)
    except Exception as exc:  # noqa: BLE001 —— 核对不了偏移量就别挡住"存高亮"
        log.warning("取行文本失败（这条高亮的偏移量无法核对）：%s/%s: %s",
                    paper_id, block_id, exc)
        return None
    return None if raw is None else render_like_dom(raw)


def _same_span_text(left: object, right: object) -> bool:
    """两段文字是不是同一段（**空白不算差异**：浏览器/模型抄回来的空白常不一样）。"""
    return re.sub(r"\s+", "", str(left or "")) == re.sub(r"\s+", "", str(right or ""))


def _located_span(paper_id: str, block_id: str, text: str, side: str,
                  start: int, end: int) -> tuple[int, int]:
    """**按文字定位**偏移量（正文行那一面）；定位不到就是 `ValidationError`。

    只在两处动：`end <= start`（0/0 的死高亮）或"给定区间上取出来的字与 `selected_text`
    对不上"（换了译本 / 换了坐标系）。两者都**不许原样落库**：0/0 在 `markRanges` 里永远
    画不出来（它只画 `end > start`），用户看到的就是"高亮没挂上"，比一句报错更糟。
    """
    from backend.services.agent.annotations import locate_in_rows, looks_like_formula

    rendered = _row_side_text(paper_id, block_id, side)
    if end > start:
        # 区间是真的就先认：公式那一路存的是 **LaTeX**（不是坐标系里那一段），按文字比必然
        # 对不上，重新定位还可能挪到别的行上（见 `annotations.create` 的整式快路）。
        if (rendered is None or looks_like_formula(text)
                or _same_span_text(rendered[start:end], text)):
            return start, end
    elif rendered is None:
        # 0/0 又没有页产物可查：核不了也不许存（存下去就是一条永远画不出来的高亮）
        raise ValidationError(f"{_SPAN_NOT_FOUND}（{_quote_hint(text)}）")
    page = _block_page_no(block_id)
    hit = locate_in_rows(paper_id, page, text, side=side) if page else None
    if hit is None:
        raise ValidationError(f"{_SPAN_NOT_FOUND}（{_quote_hint(text)}）")
    row_id, (found_start, found_end) = str(hit[0] or ""), hit[1]
    if row_id and row_id != str(block_id):
        # 找到了、但在**别的行**上：硬写下去就是把高亮挂错地方（宁可让调用方换锚点）
        raise ValidationError(
            f"这段文字不在 {block_id} 这一行里（它在同一页的 {row_id} 上）："
            f"请把锚点换成那一行再标，或者重新划一次")
    return int(found_start), int(found_end)


def create_highlight(payload: dict, user_id: int = 1) -> dict:
    """存一条高亮。

    **原文 / 译文分开存**（`side`）：一行对照里两侧共用同一个 `block_id`，
    只存偏移量的话原文的高亮会按同样的偏移量画到译文上。
    `scope` 区分在哪标的：正文行（`block_id` = 行 id）还是 Markdown 视图（`md:p<页号>`）。
    `group_id`：一次划词跨了多行时，每行一条记录共用同一个分组 id（删除时整组一起删）。

    **面与偏移量都不再原样照抄请求**（用户 2026-12 复报"高亮没挂上"就是这两条叠加）：

    - 面按**文字语言**判（`side_for_text`）—— 以前漏带 `side` 一律当 `en`，中文引文
      于是落在原文面，用户看译文时那条高亮永远画不出来；
    - 偏移量按**文字**在当前页的行文本里定位（`_located_span`）—— 以前 0/0 也照存，
      `markRanges` 只画 `end > start`，库里有一条、正文上永远看不见。

    `scope="markdown"`（`md:pN`）那一支的偏移量语义不动：它的坐标系是那一面渲染出来的
    Markdown，与行文本无关，前端算得很准。
    """
    paper_id = payload.get("paper_id")
    block_id = payload.get("block_id")
    if not paper_id or not block_id:
        raise ValidationError("paper_id 与 block_id 必填")
    color = _norm_color(payload.get("color"))
    scope = str(payload.get("scope") or "row").lower()
    if scope not in HIGHLIGHT_SCOPES:
        scope = "row"
    text = str(payload.get("selected_text") or "")
    side = side_for_text(payload.get("side"), text)
    group_id = str(payload.get("group_id") or "").strip()[:32] or None
    start = _offset(payload.get("start_offset"))
    end = _offset(payload.get("end_offset"))
    if scope == "row" and str(block_id).strip() and text.strip():
        start, end = _located_span(str(paper_id), str(block_id), text, side, start, end)
    # 行身份**先**算：它在页产物那边（reader_service）有自己的 session，不在这个 DB 会话里开
    pid = row_pid(paper_id, block_id)
    with session_scope() as sess:
        row = Highlight(
            id=new_id("h"), user_id=user_id, paper_id=paper_id, block_id=block_id,
            selected_text=payload.get("selected_text"),
            start_offset=start, end_offset=end,
            color=color, side=side, scope=scope, group_id=group_id,
            anchor_hash=anchor_hash(payload.get("selected_text"), block_id, side,
                                    start, end, pid=pid),
        )
        sess.add(row)
        sess.flush()
        return _highlight_dict(row)


def update_highlight(highlight_id: str, payload: dict) -> dict:
    """改一条高亮：颜色 / 引用文字，**以及换位置**（`block_id` + 偏移量 + `side` + `scope`）。

    换位置这条是 2026-12-07 补的（用户口径：标错一行时，删了重建会**连带删掉笔记**，
    成本太高）。语义是"把这一条挪过去"，不是"新建一条"：**id 不变**，挂在它上面的笔记
    跟着走（笔记里的 `block_id` / 偏移 / 引用文字是冗余副本，这里一并同步）。
    偏移量变了就得重算 `anchor_hash`，否则重译后按哈希重定位会把它找回旧位置。
    """
    with session_scope() as sess:
        row = sess.get(Highlight, highlight_id)
        if row is None:
            raise NotFoundError(f"高亮不存在：{highlight_id}")
        color = payload.get("color")
        if color is not None:
            row.color = _norm_color(color)
        moved = False
        new_text = payload.get("selected_text")
        #: 引文变了也算「动过」：挂在它上面的笔记要跟着换文字（笔记里是冗余副本）。
        if new_text is not None and str(new_text) != str(row.selected_text or ""):
            row.selected_text = str(new_text)
            moved = True
        if str(payload.get("block_id") or "").strip():
            row.block_id = str(payload["block_id"]).strip()
            moved = True
        scope = str(payload.get("scope") or "").lower()
        if scope in HIGHLIGHT_SCOPES and scope != row.scope:
            row.scope = scope
            moved = True
        side = str(payload.get("side") or "").lower()
        if side in HIGHLIGHT_SIDES and side != row.side:
            row.side = side
            moved = True
        for field in ("start_offset", "end_offset"):
            if payload.get(field) is not None and int(payload[field]) != int(getattr(row, field) or 0):
                setattr(row, field, int(payload[field]))
                moved = True
        if moved:
            row.anchor_hash = anchor_hash(row.selected_text, row.block_id, row.side,
                                          row.start_offset, row.end_offset,
                                          pid=row_pid(row.paper_id, row.block_id))
            # 笔记是挂在这条高亮上的附注：位置副本一起搬，否则点笔记会跳到旧地方。
            for note in sess.query(Note).filter(Note.highlight_id == highlight_id).all():
                note.block_id = row.block_id
                note.selected_text = row.selected_text
                note.start_offset = row.start_offset
                note.end_offset = row.end_offset
        sess.flush()
        return _highlight_dict(row)


def list_highlights(paper_id: str, user_id: int = 1,
                    q: str | None = None) -> dict:
    """某篇论文的高亮；`q` 按**选中文本**过滤（与 `list_notes` 的 `q` 同一写法）。

    `q` 是 2026-12 补的：库级笔记检索（`project.notes`）带关键词时，高亮那一支以前
    **没有**这个参数 —— 查询被静默丢掉，于是 `kind=all` + `query` 回来一批与关键词无关的
    高亮、把真正命中的笔记挤到后面（用户看到的就是"笔记正文永远拿不到"）。
    大小写不敏感靠列的排序规则（与 `list_notes` 对 `content_md` 的做法逐字一致）。
    """
    with session_scope() as sess:
        query = (sess.query(Highlight)
                 .filter(Highlight.paper_id == paper_id, Highlight.user_id == user_id))
        if q:
            query = query.filter(Highlight.selected_text.like(f"%{q}%"))
        rows = query.order_by(Highlight.created_at.asc()).all()
        return {"items": [_highlight_dict(r) for r in rows], "count": len(rows)}


def delete_highlight(highlight_id: str) -> dict:
    """删一条高亮；如果它属于某个跨行分组，**整组一起删**（用户眼里那是一条高亮）。

    返回被删掉的 id 列表：前端据此把本地那几条一起摘掉，不用重新拉一遍列表。

    **级联删笔记**（`Highlight └── Note`，见 `docs/开发日志` §73）：笔记的位置就是它挂的高亮，
    高亮没了笔记就成了"没有原文可看的一条"。级联在**这一层**做（返回里给出 `deleted_notes` 的 id），
    因为走 `delete_highlight` 的调用方不止一个（前端删除、agent 工具、自检清理）；
    以前不级联 —— 前端 `deleteHighlightIds` 按"后端会级联"写的（本地过滤掉 `highlight_id` 已删的
    笔记），于是库里留下的孤儿笔记谁也看不见，隔一阵冒出来就是用户说的"莫名其妙的笔记"。

    **幂等**：id 不存在时返回空列表，而不是 404。因为"整组删"这件事天然会带来重复请求 ——
    前端点一次删除可能对同组的几条各发一次（多标签页同开一篇也会撞车），
    重复删同一条不该算失败；以前会往日志里丢 404，看着像删除坏了（用户报过）。
    """
    with session_scope() as sess:
        row = sess.get(Highlight, highlight_id)
        if row is None:
            return {"deleted": highlight_id, "deleted_ids": [], "count": 0,
                    "deleted_notes": [], "missing": True}
        rows = [row]
        if row.group_id:
            rows = (sess.query(Highlight)
                    .filter(Highlight.group_id == row.group_id,
                            Highlight.paper_id == row.paper_id,
                            Highlight.user_id == row.user_id).all())
        ids = [r.id for r in rows]
        # 先查出"挂在这些高亮上的笔记"，删完把 id 一起回给调用方（前端据此把本地那几条摘掉）
        note_ids: list[str] = []
        if ids:
            note_ids = [str(x[0]) for x in
                        sess.query(Note.id)
                        .filter(Note.highlight_id.in_(ids),
                                Note.paper_id == row.paper_id,
                                Note.user_id == row.user_id).all()]
            if note_ids:
                (sess.query(Note).filter(Note.id.in_(note_ids))
                 .delete(synchronize_session=False))
        for r in rows:
            sess.delete(r)
    return {"deleted": highlight_id, "deleted_ids": ids, "count": len(ids),
            "deleted_notes": note_ids}


def anchor_page(block_id: str | None) -> int | None:
    """锚点 → 页码（**三种面都能解析**，因为页码本来就编码在锚点里）。

    - 正文行 id：`p004-02` → 第 4 页（行 id 就是"位置编号"）；
    - Markdown 视图：`md:p8` → 第 8 页；
    - 原 PDF 页图：`pdf:p3` → 第 3 页。

    为什么放在这里而不是只靠 `reader_service` 那份映射：侧栏列表（`/notes`、`/highlights`）也要
    "点了跳到哪一页"，而那条链路上没有行/块映射表。老锚点（`attentionisa-p04-b06` 这种解析块 id）
    解不出来就返回 None，由前端退回"只按页提示"。
    """
    if not block_id:
        return None
    text = str(block_id).strip()
    if (m := re.match(r"^(?:md|pdf):p(\d+)$", text)):
        return int(m.group(1))
    if (m := re.match(r"^p(\d{1,5})-\d+$", text)):
        return int(m.group(1))
    return None


def _highlight_dict(row: Highlight) -> dict:
    return {"id": row.id, "paper_id": row.paper_id, "block_id": row.block_id,
            "text": row.selected_text, "start": row.start_offset,
            "end": row.end_offset, "color": row.color,
            "side": row.side or "en", "scope": row.scope or "row",
            "group_id": row.group_id,
            "page": anchor_page(row.block_id),
            "created_at": iso_utc(row.created_at)}



# --------------------------------------------------------------------------
# 笔记（规划 §10 ⑥）
# --------------------------------------------------------------------------


def create_note(payload: dict, user_id: int = 1) -> dict:
    if not payload.get("paper_id"):
        raise ValidationError("paper_id 必填")
    with session_scope() as sess:
        row = Note(
            id=new_id("n"), user_id=user_id, paper_id=payload["paper_id"],
            block_id=payload.get("block_id"),
            highlight_id=payload.get("highlight_id"),
            selected_text=payload.get("selected_text"),
            start_offset=payload.get("start_offset"),
            end_offset=payload.get("end_offset"),
            content_md=payload.get("content_md") or "",
            tags=",".join(payload.get("tags") or []) if isinstance(
                payload.get("tags"), list) else payload.get("tags"),
        )
        sess.add(row)
        sess.flush()
        return _note_dict(row)


def update_note(note_id: str, payload: dict) -> dict:
    with session_scope() as sess:
        row = sess.get(Note, note_id)
        if row is None:
            raise NotFoundError(f"笔记不存在：{note_id}")
        for field in ("content_md", "selected_text", "block_id"):
            if field in payload and payload[field] is not None:
                setattr(row, field, payload[field])
        if "tags" in payload and payload["tags"] is not None:
            tags = payload["tags"]
            row.tags = ",".join(tags) if isinstance(tags, list) else tags
        sess.flush()
        return _note_dict(row)


def list_notes(paper_id: str | None = None, user_id: int = 1,
               q: str | None = None) -> dict:
    with session_scope() as sess:
        query = sess.query(Note).filter(Note.user_id == user_id)
        if paper_id:
            query = query.filter(Note.paper_id == paper_id)
        if q:
            query = query.filter(Note.content_md.like(f"%{q}%"))
        rows = query.order_by(Note.updated_at.desc()).all()
        return {"items": [_note_dict(r) for r in rows], "count": len(rows)}


def delete_note(note_id: str) -> dict:
    with session_scope() as sess:
        row = sess.get(Note, note_id)
        if row is None:
            raise NotFoundError(f"笔记不存在：{note_id}")
        sess.delete(row)
    return {"deleted": note_id}


def note_scope(block_id: str | None) -> str:
    """锚点 → 挂在哪一面：``"pdf"`` / ``"markdown"`` / 其余 = 正文行（``"row"``）。

    **只此一处**：`/notes`、`/highlights` 与**阅读器的 payload** 都用它。以前阅读器那份
    payload 自己又写了一遍（而且漏了 `highlight_id`），侧栏的归并规则（`utils/notesFeed`）
    因此配不上"高亮 ↔ 笔记"，同一条内容被渲染成两条（一条「待写」+ 一条「笔记」）——
    用户报的"产生了两条莫名的笔记"就是这个。
    """
    text = str(block_id or "")
    if text.startswith("pdf:"):
        return "pdf"
    if text.startswith("md:"):
        return "markdown"
    return "row"


def _note_dict(row: Note) -> dict:
    return {"id": row.id, "paper_id": row.paper_id, "block_id": row.block_id,
            "highlight_id": row.highlight_id, "selected_text": row.selected_text,
            "content_md": row.content_md,
            # 页码与"面"都给出来：侧栏列表要显示"这条笔记挂在哪、点它能跳到哪"
            "page": anchor_page(row.block_id),
            "scope": note_scope(row.block_id),
            "tags": [t for t in (row.tags or "").split(",") if t],
            "created_at": iso_utc(row.created_at),

            "updated_at": iso_utc(row.updated_at)}



# --------------------------------------------------------------------------
# AI 批注（规划 §10 ⑦、§21.9、§21.10）
# --------------------------------------------------------------------------


def list_annotations(paper_id: str, *, type_: str | None = None,
                     user_id: int = 1, q: str | None = None) -> dict:
    """某篇论文的 AI 批注；`q` 按**批注正文**（`content_md`）过滤。

    `q` 与 `list_notes` / `list_highlights` 的 `q` 同一写法（模糊匹配、大小写不敏感）。
    为什么补：库级笔记检索（`project.notes`）带关键词时，批注那一支以前**没有**这个参数 ——
    查询被静默丢掉，于是问「我关于 KL 的批注」回来的是**全部批注**，模型把无关条目当命中引用。
    """
    with session_scope() as sess:
        query = sess.query(Annotation).filter(Annotation.paper_id == paper_id,
                                              Annotation.user_id == user_id,
                                              Annotation.status == "active")
        if type_:
            query = query.filter(Annotation.type == type_)
        if q:
            query = query.filter(Annotation.content_md.like(f"%{q}%"))
        rows = query.order_by(Annotation.created_at.asc()).all()
        return {"items": [_annotation_dict(r) for r in rows], "count": len(rows)}


def create_annotation(payload: dict, user_id: int = 1) -> dict:
    if not payload.get("paper_id"):
        raise ValidationError("paper_id 必填")
    with session_scope() as sess:
        row = Annotation(
            id=new_id("a"), user_id=user_id, paper_id=payload["paper_id"],
            block_id=payload.get("block_id"),
            type=payload.get("type") or "explanation",
            content_md=payload.get("content_md") or "",
            source=payload.get("source") or "manual",
            sources_json=json_dumps(payload.get("sources") or []),
            model_id=payload.get("model_id"),
            confidence=payload.get("confidence"),
            task_id=payload.get("task_id"),
            status="active",
        )
        sess.add(row)
        sess.flush()
        data = _annotation_dict(row)
    bus.publish("papers", EVENT_ANNOTATION, data)
    return data


def update_annotation(annotation_id: str, payload: dict) -> dict:
    with session_scope() as sess:
        row = sess.get(Annotation, annotation_id)
        if row is None:
            raise NotFoundError(f"批注不存在：{annotation_id}")
        if payload.get("content_md") is not None:
            row.content_md = payload["content_md"]
        if payload.get("status") is not None:
            row.status = payload["status"]
        sess.flush()
        return _annotation_dict(row)


def delete_annotation(annotation_id: str) -> dict:
    with session_scope() as sess:
        row = sess.get(Annotation, annotation_id)
        if row is None:
            raise NotFoundError(f"批注不存在：{annotation_id}")
        row.status = "deleted"
    return {"deleted": annotation_id}


def _annotation_dict(row: Annotation) -> dict:
    return {"id": row.id, "paper_id": row.paper_id, "block_id": row.block_id,
            "type": row.type, "content_md": row.content_md, "source": row.source,
            "sources": json_loads(row.sources_json, []) or [],
            "model_id": row.model_id, "confidence": row.confidence,
            "status": row.status,
            "created_at": iso_utc(row.created_at)}



# --------------------------------------------------------------------------
# 划词解释的**落库**（解释本身由问答 agent 产出：`POST /api/v1/agent/ask` + `selection`）
#
# 旧实现 `explain_selection` 已删除：它自己拼 prompt（段落原文 + 译文 + 邻段 + 选区）
# 直接调 generation provider，产出物不带引用、不能跳转，与问答那条链路的口径也不一致。
# 现在划词走问答 agent —— 它会用 `block_get` 展开选区所在段落、按需读相邻页，答案带 `[n]`
# 引用可跳转，而且**作为一轮对话留在会话里**（这就是「划词解释历史」的数据来源）。
# --------------------------------------------------------------------------


def save_explanation_as_annotation(payload: dict, user_id: int = 1) -> dict:
    """把 agent 给出的划词解释存成批注（`source=selection`）。

    这里**不调用任何模型**：内容来自问答 agent 那一轮，本函数只负责"把这段话记下来"。
    所以它同时是前端「划词解释历史」的写入端。
    """
    data = dict(payload)
    data["type"] = payload.get("type") or "qa"
    data["source"] = "selection"
    data["content_md"] = payload.get("content_md") or payload.get("explanation") or ""
    return create_annotation(data, user_id)


def notes_overview(user_id: int = 1) -> dict:
    """跨论文笔记/高亮/批注聚合（规划 §22 /notes）。"""
    with session_scope() as sess:
        notes = (sess.query(Note).filter(Note.user_id == user_id)
                 .order_by(Note.updated_at.desc()).limit(200).all())
        highlights = (sess.query(Highlight).filter(Highlight.user_id == user_id)
                      .order_by(Highlight.created_at.desc()).limit(200).all())
        annotations = (sess.query(Annotation)
                       .filter(Annotation.user_id == user_id,
                               Annotation.status == "active")
                       .order_by(Annotation.created_at.desc()).limit(200).all())
        titles = {p.id: p.title for p in sess.query(Paper).all()}
    return {
        "notes": [{**_note_dict(n), "paper_title": titles.get(n.paper_id)} for n in notes],
        "highlights": [{**_highlight_dict(h), "paper_title": titles.get(h.paper_id)}
                       for h in highlights],
        "annotations": [{**_annotation_dict(a), "paper_title": titles.get(a.paper_id)}
                        for a in annotations],
    }
