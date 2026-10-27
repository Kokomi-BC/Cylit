"""引用分组：**一个标号 = 一处连续的材料**。

为什么需要这个模块（实测根因）：`citations.py` 按"工具返回顺序"发号，模型只用其中一小撮；
`runtime._finalize` 收尾时把用到的号压成 1..M 并改写正文 —— 但改写与 `sanitize.used_refs`
**只认单号** `[n]`。实测某条回答正文里写着 `[36, 41]`，而那条消息的引用表只有 1/2/3 号：
多号方括号既没被改写、也没被计入引用；前端又把"紧挨着的一串方括号"并成一枚按钮，
点一下同时亮三处不相邻的地方 —— 用户看到的"编号很奇怪"就是这么来的。

用户口径（2026-12 定稿）：**一个标号 = 一组连续引用**（同页且行号相邻，或同一块跨页的两半）。
不相邻的必须拆成多个标号。正文里只允许两种形态：

- `[n]`：单词条；
- `[a–b]`：连续组（连接号是 U+2013 EN DASH，a < b）。

绝不出现 `[a,b]` / `[a] [b]` 这种"一枚按钮含多号"的写法。

引用表这一侧（与前端渲染是一份契约，改一边就得改另一边）：

- 一条 Citation 新增**可选**键 `members: [int...]` = 这个标号覆盖的编号（按文档顺序）；
  **单词条不写这个键**（老形状一个字不变），组的 ref = members[0]；
- 组的 pids = 成员 pids 的并集（文档顺序、去重），row_id / block_id / page / label / snippet
  取第一个成员，latex 任一成员有就保留；
- 编号按**正文首次出现顺序**压成 1..M 连续无洞。

编号的"身份"是**材料本身**，不是显示文本（用户 2026-12 补充）：一个编号对应的行/块集合
（pids 优先，取不到就用"页 + 行号"）在全文里必须复用同一个号；**不同集合（哪怕只差一行、
或是子集）各给各的号**。所以同一处材料写两遍不会一次 3 一次 36，而 {3,4} 与 {3,4,5}
也各是各的标号，绝不并成一个区间。

本模块是**纯函数**（不碰网络 / DB / DOM），所以能单测；调用方只负责把 Citation 喂进来。
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

#: 区间的连接号：U+2013 EN DASH（前端 parseRefGroup 认得它）
EN_DASH = "\u2013"

#: 区间宽度上限：一页正文里也就几个引用，宽到这个程度必然是年份 / 页码（与前端同一口径）
MAX_SPAN = 20

#: 一个方括号（不许跨行、不许嵌套、长度有界 —— 认不出就整段原样留着，绝不吞字）
_MARK_RE = re.compile(r"\[([^\[\]\n]{1,64})\]")
#: 括号内的分隔符：半角逗号 / 全角逗号 / 顿号（模型三种都写过）
_SEP_RE = re.compile(r"[,，、]")
#: 区间：3-5 / 3"+"\u2013"+"5 / 3\u20145 / 3~5（与前端 [-"+"\u2013"+"\u2014~] 同一口径）
_SPAN_RE = re.compile(r"(\d{1,3})\s*[-\u2013\u2014~]\s*(\d{1,3})\Z")
_NUM_RE = re.compile(r"\d{1,3}\Z")
#: row_id 形如 p005-02（页 + 页内行号）；p01-b03 那种块 id 不算行号
_ROW_ID_RE = re.compile(r"[pP]\s*(\d{1,4})\s*[-_]\s*(\d{1,4})")


@dataclass(frozen=True)
class BracketMark:
    """正文里的一个**数字方括号**：位置 + 它写了哪几个号（区间已展开）。"""

    start: int
    end: int
    raw: str
    members: tuple[int, ...]


def _members_of(inner: str) -> tuple[int, ...] | None:
    """括号内容 → 编号列表；**认不出就返回 None**（调用方原样保留这段文本）。"""
    text = inner.strip()
    if not text:
        return None
    out: list[int] = []
    for seg in _SEP_RE.split(text):
        seg = seg.strip()
        if not seg:
            return None
        span = _SPAN_RE.match(seg)
        if span:
            lo, hi = int(span.group(1)), int(span.group(2))
            # 契约要求 a<b；写反了 / 写成一个数 / 区间过宽 —— 一律不猜
            if lo >= hi or hi - lo > MAX_SPAN:
                return None
            out.extend(range(lo, hi + 1))
            continue
        if not _NUM_RE.match(seg):
            return None
        out.append(int(seg))
    return tuple(out) if out else None


def parse_bracket_marks(text: str) -> list[BracketMark]:
    """扫出正文里**认得出**的全部数字方括号（[3] [3,7] [3、7] [3-5] [3-5, 9] 这些形态）。

    区间过宽（hi-lo > MAX_SPAN）或含非数字 token 的方括号**直接不返回**：
    它可能是论文自己的编号 / 页码，原样留着才不会把正文改坏。
    """
    out: list[BracketMark] = []
    for match in _MARK_RE.finditer(text or ""):
        members = _members_of(match.group(1))
        if members:
            out.append(BracketMark(start=match.start(), end=match.end(),
                                   raw=match.group(0), members=members))
    return out


def split_runs(text: str, marks: Sequence[BracketMark] | None = None) -> list[list[BracketMark]]:
    """把方括号切成**极大连续段**：段内两个方括号之间只有空白。

    为什么按 run 而不是按整段正文：[3][7] 是模型"分两处引用"的写法，**中间没有正文**
    才说明它们是紧挨着写的；[3]，[7] 中间隔着标点，本来就是两处不同的引用。
    """
    items = list(parse_bracket_marks(text) if marks is None else marks)
    runs: list[list[BracketMark]] = []
    cur: list[BracketMark] = []
    for mark in items:
        if cur and text[cur[-1].end:mark.start].strip():
            runs.append(cur)
            cur = []
        cur.append(mark)
    if cur:
        runs.append(cur)
    return runs

# --------------------------------------------------------------------------
# 几何：这条证据落在哪一行
# --------------------------------------------------------------------------


def _get(item: Any, key: str, default: Any = None) -> Any:
    """对象 / 字典都能读（单测与"从 citations_json 复算"两条路都要用）。"""
    if isinstance(item, Mapping):
        return item.get(key, default)
    return getattr(item, key, default)


def row_pair(citation: Any) -> tuple[int, int] | None:
    """row_id（p005-02）→ (页, 页内行号)；认不出就是 None。"""
    match = _ROW_ID_RE.search(str(_get(citation, "row_id") or ""))
    if not match:
        return None
    return int(match.group(1)), int(match.group(2))


def pid_set(citation: Any) -> set[str]:
    """这条证据记住的行身份（chunk 的 metadata.pids，可能是空的）。"""
    raw = _get(citation, "pids") or []
    return {str(pid) for pid in raw if str(pid).strip()}


def identity_key(citation: Any) -> tuple[str, ...]:
    """一条证据的**身份**：它覆盖的行 / 块集合（既不是 ref，也不是显示文本）。

    优先 pids（跨重建索引仍认得出同一行），取不到退"页 + 行号"；两样都没有就用它自己的
    编号 —— **认不出就单独算一处**，不猜。
    """
    pids = pid_set(citation)
    if pids:
        return tuple("pid:" + pid for pid in sorted(pids))
    row = row_pair(citation)
    if row:
        return (f"row:{row[0]}:{row[1]}",)
    return (f"ref:{_get(citation, 'ref')}",)


# --------------------------------------------------------------------------
# 分组
# --------------------------------------------------------------------------


def _adjacent(group: Sequence[Any], nxt: Any,
              pid_rows: Mapping[str, Sequence[int]] | None) -> bool:
    """nxt 是不是紧接在这个组**后面**（同一处材料的下一块）？判不出就 False。

    ① 同页且行号相邻（差 0 也算：同一行的 block 证据与它的 chunk 证据本来就是同一处）；
    ② pids 有交集（同一块，跨页的两半也在这里命中）；
    ③ 两边的 pids 并起来是同页连续行（块跨页被切成两半时 pids 不相交但首尾相接）——
       这一步要 pid → (页, 行号) 的解析表；没有表就判不出来，**不猜**（宁可拆成两个标号）。
    """
    mine, theirs = row_pair(group[-1]), row_pair(nxt)
    if mine and theirs and mine[0] == theirs[0] and abs(mine[1] - theirs[1]) <= 1:
        return True
    left, right = pid_set(group[-1]), pid_set(nxt)
    if left and right and (left & right):
        return True
    if not pid_rows:
        return False
    pages: set[int] = set()
    rows: set[int] = set()
    for citation in (*group, nxt):
        pids = pid_set(citation)
        if not pids:
            return False
        for pid in pids:
            hit = pid_rows.get(pid)
            if not hit:
                return False          # 认不全就不猜
            pages.add(int(hit[0]))
            rows.add(int(hit[1]))
    if len(pages) != 1 or len(rows) < 2:
        return False
    return max(rows) - min(rows) + 1 == len(rows)


def group_run(members: Sequence[Any],
              *, pid_rows: Mapping[str, Sequence[int]] | None = None) -> list[list[Any]]:
    """把一个 run 内的证据切成**极大连续子序列**（每个子序列 = 一个标号）。

    pid_rows：pid → (页, 页内行号)，只有"同一块跨页的两半"那条规则需要它；调用方拿不到
    就传 None，那条规则退化成"不合并"（认不出几何信息的证据单独成组，绝不猜）。
    """
    uniq: list[Any] = []
    seen: set[tuple[str, ...]] = set()
    for citation in members or []:
        key = identity_key(citation)
        if key in seen:
            continue          # 同一处材料被写了两遍：一个标号就够
        seen.add(key)
        uniq.append(citation)

    groups: list[list[Any]] = []
    for citation in uniq:
        if groups and _adjacent(groups[-1], citation, pid_rows):
            groups[-1].append(citation)
        else:
            groups.append([citation])
    return groups

# --------------------------------------------------------------------------
# 编号与改写
# --------------------------------------------------------------------------


def _contiguous(nums: Sequence[int]) -> bool:
    """是不是一段**升序连续**的号（能写成 [a-b] 区间的前提）。"""
    return bool(nums) and list(nums) == list(range(nums[0], nums[0] + len(nums)))


def _render(nums: Sequence[int]) -> str:
    """一组编号 → 正文写法：单词条 [n]、连续组 [a-b]；号乱序（极少见）退回逐个单号。"""
    if len(nums) == 1:
        return f"[{nums[0]}]"
    if _contiguous(nums):
        return f"[{nums[0]}{EN_DASH}{nums[-1]}]"
    return "".join(f"[{n}]" for n in nums)


def _as_dict(citation: Any) -> dict:
    to_dict = getattr(citation, "to_dict", None)
    if callable(to_dict):
        return dict(to_dict())
    if isinstance(citation, Mapping):
        return dict(citation)
    raise TypeError(f"不认识的 citation 形状：{type(citation)!r}")


class CitePlan:
    """一次收尾的**编号表**：先由正文建表，再用同一份表改写正文与轨迹里的每一段。

    为什么要分成"建表 / 改写"两步：state.log.steps 里的正文步骤必须与最终正文**同一套
    编号**（流式看到的 = 落库的）。编号只由"材料身份 + 正文首次出现顺序"决定，所以同一份
    表可以反复渲染任意段落。
    """

    def __init__(self, citations: Iterable[Any],
                 *, pid_rows: Mapping[str, Sequence[int]] | None = None) -> None:
        self._citations = [c for c in (citations or []) if _get(c, "ref") is not None]
        self._pid_rows = dict(pid_rows) if pid_rows else None
        self._by_ref: dict[int, Any] = {}
        for citation in self._citations:
            self._by_ref.setdefault(int(_get(citation, "ref")), citation)
        #: 身份 → 新号（1..M，按正文首次出现顺序）
        self._number: dict[tuple[str, ...], int] = {}
        #: 身份 → 第一次见到的那个 Citation（payload 的字段都从它取）
        self._first: dict[tuple[str, ...], Any] = {}
        #: 组首号 → 成员号（按文档顺序）。只有"连续组"才登记，单词条不写 members。
        self._members: dict[int, list[int]] = {}
        #: 身份 → 这条证据的行身份并集 / 第一份非空 latex。同一处材料可能被登记成好几条
        #: （同一行的块证据 + 它里面那枚公式），合并成一个号时字段不能因此丢 —— 尤其 latex：
        #: 丢了就只能退化成整行闪。
        self._pids_of: dict[tuple[str, ...], list[str]] = {}
        self._latex_of: dict[tuple[str, ...], str] = {}
        self._next = 1

    # ---------------- 建表 ----------------
    def assign(self, text: str) -> "CitePlan":
        """扫正文建表：按**首次出现顺序**给每一处材料发号，并登记连续组。"""
        for run in split_runs(text):
            for segment in self._segments(run, self._known):
                for group in self._groups(segment):
                    self._register(group)
        # 正文一个标号都没写的兜底（短回答常见）：引用到的证据照样要有号，顺序按传入顺序。
        for citation in self._citations:
            self._number_of(citation)
        return self

    def _known(self, mark: BracketMark) -> bool:
        return all(ref in self._by_ref for ref in mark.members)

    def _numbered(self, mark: BracketMark) -> bool:
        if not all(ref in self._by_ref for ref in mark.members):
            return False
        return all(identity_key(self._by_ref[ref]) in self._number for ref in mark.members)

    def _segments(self, run: Sequence[BracketMark], pred) -> list[list[BracketMark]]:
        """run 内**认得出**的极大连续段；认不出的方括号把段切断（它原样留在正文里）。"""
        out: list[list[BracketMark]] = []
        cur: list[BracketMark] = []
        for mark in run:
            if not pred(mark):
                if cur:
                    out.append(cur)
                    cur = []
                continue
            cur.append(mark)
        if cur:
            out.append(cur)
        return out

    def _groups(self, segment: Sequence[BracketMark]) -> list[list[Any]]:
        members: list[Any] = []
        for mark in segment:
            for ref in mark.members:
                members.append(self._by_ref[int(ref)])
        return group_run(members, pid_rows=self._pid_rows)

    def _number_of(self, citation: Any) -> int:
        key = identity_key(citation)
        self._absorb(key, citation)
        num = self._number.get(key)
        if num is None:
            num = self._next
            self._next += 1
            self._number[key] = num
            self._first[key] = citation
        return num

    def _absorb(self, key: tuple[str, ...], citation: Any) -> None:
        """把这条证据的信息并进它那个身份：pids 按先来后到去重，latex 空的不覆盖非空的。"""
        pids = self._pids_of.setdefault(key, [])
        for raw in (_get(citation, "pids") or []):
            pid = str(raw)
            if pid and pid not in pids:
                pids.append(pid)
        latex = str(_get(citation, "latex") or "")
        if latex and not self._latex_of.get(key):
            self._latex_of[key] = latex

    def _register(self, group: Sequence[Any]) -> None:
        """登记一个**连续组**：首号带 members（payload 里组的 pids 并集 / latex 在那边算）。

        单词条什么都不做 —— 老形状一个字不变。组的成员号在这里已经按文档顺序算好了一整段
        （因为 `_number_of` 按调用顺序发号），所以 `a < b` 天然成立。
        """
        nums = [self._number_of(c) for c in group]
        if len(nums) < 2 or not _contiguous(nums):
            return
        leader = nums[0]
        if leader in self._members:
            # 同一个首号只能带一份 members：同一处材料先后被写成不同组合时，先到的那份算准
            return
        self._members[leader] = nums

    # ---------------- 改写 ----------------
    def render(self, text: str) -> str:
        """用**已经定好的编号**改写一段正文（不新发号；认不出的方括号原样保留）。"""
        text = text or ""
        repls: list[tuple[int, int, str]] = []
        for run in split_runs(text):
            for segment in self._segments(run, self._numbered):
                pieces: list[str] = []
                for group in self._groups(segment):
                    nums = [self._number[identity_key(c)] for c in group]
                    pieces.append(_render(nums))
                # run 内相邻的组直接相接：绝不留出"[1] [2]"那种带空格的写法
                repls.append((segment[0].start, segment[-1].end, "".join(pieces)))
        out = text
        for start, end, piece in reversed(repls):
            out = out[:start] + piece + out[end:]
        return out

    def payload(self) -> list[dict]:
        """引用表：一条 = 一处材料（组的首条带 members），按新号升序 = 1..M 连续无洞。

        组的字段口径：`ref` / `row_id` / `block_id` / `page` / `label` / `snippet` 取第一个成员
        （`_first` 就是它），`pids` 取成员并集（文档顺序、去重），`latex` 任一成员有就保留。
        """
        key_of_num = {num: key for key, num in self._number.items()}
        out: list[dict] = []
        for key, num in sorted(self._number.items(), key=lambda kv: kv[1]):
            data = _as_dict(self._first[key])
            data["ref"] = num
            members = self._members.get(num)
            pids = list(self._pids_of.get(key, []))
            latex = self._latex_of.get(key, "")
            if members:
                data["members"] = list(members)
                for extra in members:
                    other = key_of_num.get(extra)
                    if other is None or other == key:
                        continue
                    for pid in self._pids_of.get(other, []):
                        if pid not in pids:
                            pids.append(pid)
                    if not latex:
                        latex = self._latex_of.get(other, "")
            if pids:
                data["pids"] = pids
            else:
                data.pop("pids", None)
            if latex:
                data["latex"] = latex
            out.append(data)
        return out


def plan_cite_groups(text: str, citations: Iterable[Any],
                     *, pid_rows: Mapping[str, Sequence[int]] | None = None) -> CitePlan:
    """按正文建一份编号表（正文与轨迹步骤共用同一份）。"""
    return CitePlan(citations, pid_rows=pid_rows).assign(text)


def apply_cite_groups(text: str, citations: Iterable[Any],
                      *, pid_rows: Mapping[str, Sequence[int]] | None = None
                      ) -> tuple[str, list[dict]]:
    """正文 → (改写后的正文, 引用表)。这就是 runtime._finalize 要的那一步。"""
    plan = plan_cite_groups(text, citations, pid_rows=pid_rows)
    return plan.render(text), plan.payload()
