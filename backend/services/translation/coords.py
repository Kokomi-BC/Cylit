"""P4b **共享坐标契约**：以"解析器块号"为两侧共同的行格，合并时按块号配对。

## 问题（为什么要这一层）

两路各自按页图分段，块数经常差 1~2 段。`pair_rows` 只能靠"结构类型 + 跨语言标记"做差分对齐，
于是有两类修不掉的毛病：

1. **整页错位**：多出来的那一段该插在哪是**不可判定**的（实测 turbo p1，A 把摘要相邻两句并成
   一块、B 拆成两块，匹配器把插入放到页尾，后面每行都错位）；
2. **页首分歧**：两个同级标题（层级编号都为空）在键上完全一样，分不清谁是谁，
   实测把英文标题配到了中文摘要上（`_row_key` 带上全大写缩略语后仍修不掉）。

根因是**两侧各自分段、没有共同的坐标**。契约就是补上这个坐标。

## 坐标从哪来

解析器块索引（`reader_service.load_block_index`）每页给出一组带稳定 id 的块
（`turboquanton-p01-b01`…，含 `order/type/text/section`），**两路拿到的是同一份**，
且与本轮的两路产物**无关**（它是解析阶段的产物）—— 所以它是天然的共享格。

## 缓存安全的做法（重要）

前文（`history_context`）来自 **store 里已落盘的模型输出**，**不是** prompt 里的页面正文。
所以把 `[bNN]` 印在页面正文里、把契约追加到 `【本页输出】` 尾部，全部落在 `{previous}` **之后**：

- 下一页 prompt 的公共前缀 = `system + 简报 + 骨架 + previous(1..N-1) + 本页输出`，**逐字节不变**；
- **system prompt 一个字都不用改**（两路共用的规则常量保持原样）。

## 输出契约

模型除 `original_md`/`translation_md`/`breaks` 外再给一个 ``src``：**与输出块一一对应、按顺序**，
每项写它覆盖的块号（多个用 `|` 连），例如 ``["b01", "b02|b03", "b04"]``。
校验（任一不满足 → 这一页**退回 `pair_rows`**，不冒险）：

- 项数 == 输出块数；
- 每项里的块号都出现在**本页**清单里；
- 所有块号合起来**恰好覆盖本页每个块一次**（不漏、不重）；
- 出现顺序与块号顺序一致（单调递增）。

## 合并

在两个划分的交上走一遍块号：维护当前行的 (A 块集合, B 块集合)；
某块号只有一侧有主 → 并入当前行；主块已在当前行里 → 并入当前行（**这就是"1:N 合并"**）；
否则开新行。行内容 = 该侧各块的 markdown 依次拼接（**不重复**）。

于是"A 并成一块 / B 拆成两块"变成**一行**（左 = A 那一块，右 = B 那两块拼起来），
既不错位、也不留单侧空栏 —— 这正是 P4b 想要的结果。
"""

from __future__ import annotations

import re

#: `src` 项里出现的块号（`"b03"` / `"b03|b04"`）
_SRC_ITEM_RE = re.compile(r"b\d{2,3}", re.IGNORECASE)

#: 追加在 `【本页输出】` 尾部的契约（**只在页面级尾部**，不动 system prompt / 稳定前缀）
SRC_TAIL = """

【本页坐标（新增，必填）】上面正文每段开头的 `[bNN]` 是**解析器给这一页的块号**。
在本页输出里再加一个 `src` 字段：**与你的输出块一一对应、按顺序**，每项写它覆盖了哪几个块号：

- 一个输出块正好对应一个解析器块 → `"b03"`；
- 你把**相邻两段并进了一个输出块** → `"b03|b04"`（两个都写上，别漏）；
- 你把**一个解析器块拆成了两个输出块** → 两块分别写 `"b05"`、`"b05"`（同一个块号可以出现两次）。

要求：`src` 的项数与输出块数**必须相等**；块号只能取本页清单里的；
合起来要**覆盖本页每个块**，顺序与块号顺序一致。写不出来就给空数组（系统会退回按结构配对）。"""


def annotate_blocks(blocks: list[dict]) -> str:
    """块清单 → 带 `[bNN]` 行首的正文（供 prompt 的 `{body}` 用）。

    块号按 `order` 排出来的**页内序号**（`b01`、`b02`…），与解析器 id 的序号一致。
    """
    parts: list[str] = []
    for i, block in enumerate(blocks, 1):
        text = str(block.get("text") or "").strip()
        if not text:
            continue
        parts.append(f"[b{i:02d}] {text}")
    return "\n\n".join(parts)


def lattice_text(blocks: list[dict], *, head_chars: int = 40) -> str:
    """本页坐标清单（块号 + 类型 + 片段），**追加在 prompt 尾部**给模型对照。

    为什么不直接把 `[bNN]` 印在 `{body}` 里：`body` 还要参与"剪掉上一页已译完的头部"
    这类页面级处理，动它会连带影响那套逻辑；而坐标只要"块号 → 这一段是什么"的对照，
    放在尾部**同样不破坏前缀缓存**（尾部的任何内容都不在下一页 prompt 的公共前缀里）。
    """
    lines = ["【本页解析器块清单（坐标；`src` 只能用这些块号）】"]
    for i, block in enumerate(blocks, 1):
        text = str(block.get("text") or "").strip().replace("\n", " ")
        if not text:
            continue
        lines.append(f"[b{i:02d}] ({block.get('type') or 'para'}) {text[:head_chars]}")
    return "\n".join(lines)


def codes_of(blocks: list[dict]) -> list[str]:
    """本页的块号清单（与 `annotate_blocks` 的编号规则一致）。"""
    return [f"b{i:02d}" for i, block in enumerate(blocks, 1)
            if str(block.get("text") or "").strip()]


def parse_src(text: str) -> list[str]:
    """从模型回复里取 ``src``（宽容：去掉外层 JSON 后再扫数组）。"""
    from backend.core.utils import json_loads

    raw = (text or "").strip()
    for cand in _json_candidates(raw):
        data = json_loads(cand, None)
        if isinstance(data, dict) and "src" in data:
            got = data.get("src")
            if isinstance(got, list):
                return [str(x).strip() for x in got]
    return []


def _json_candidates(raw: str) -> list[str]:
    out = [raw]
    fence = re.search(r"```(?:json)?\s*(.*?)```", raw, re.DOTALL)
    if fence:
        out.append(fence.group(1).strip())
    start, end = raw.find("{"), raw.rfind("}")
    if 0 <= start < end:
        out.append(raw[start:end + 1])
    return out


def split_codes(item: str) -> list[str]:
    """``"b02|b03"`` → ``["b02", "b03"]``（大小写归一）。"""
    return [m.group(0).lower() for m in _SRC_ITEM_RE.finditer(str(item or ""))]


def validate_src(src: list[str], codes: list[str], block_count: int) -> tuple[list[list[str]], str]:
    """校验并展开 `src`；返回 ``(每块覆盖的块号列表, 失败原因)``。

    失败原因非空时调用方**必须退回 `pair_rows`** —— 宁可回到"两端可能错位"，
    也不要拿一个半对半错的坐标去配对（那会把错误扩散到每一行）。
    """
    if not src or len(src) != block_count:
        return [], f"src 项数 {len(src)} != 输出块数 {block_count}"
    valid = set(codes)
    order = {c: i for i, c in enumerate(codes)}
    covered: list[list[str]] = []
    seen: list[str] = []
    last = -1
    for item in src:
        got = split_codes(item)
        if not got:
            return [], f"src 里有空项：{item!r}"
        for code in got:
            if code not in valid:
                return [], f"src 里的块号不在本页：{code}"
            if code in seen:
                # 同一个块号只能出现一次（"一个块拆成两个输出块"由**相邻**两项共用块号表达，
                # 这里按"出现即记录"判断会误杀，所以只在**非相邻**重复时判错）
                if seen[-1] != code:
                    return [], f"src 里块号重复且不相邻：{code}"
            if order[code] < last:
                return [], f"src 顺序与块号顺序不一致：{code}"
            last = order[code]
            seen.append(code)
        covered.append(got)
    if set(seen) != valid:
        missing = sorted(valid - set(seen))
        return [], f"src 没覆盖本页全部块：缺 {missing}"
    return covered, ""


def pair_rows_by_src(md_a: str, src_a: list[str], md_b: str, src_b: list[str],
                     page: int) -> tuple[list[dict], str]:
    """按共享坐标合并两侧输出；返回 ``(rows, 质量)``。

    质量：``coords``（用的是坐标契约）或 ``""``（不可用，调用方退回 `pair_rows`）。
    每个输出块的文本用 `split_blocks` 取（与 `pair_rows` 同一套块切分）。
    """
    from backend.services.translation.page_translate import split_blocks

    blocks_a = split_blocks(md_a or "")
    blocks_b = split_blocks(md_b or "")
    if len(blocks_a) != len(src_a) or len(blocks_b) != len(src_b):
        return [], ""
    if not blocks_a and not blocks_b:
        return [], ""

    # 块号 → 拥有它的输出块下标（取第一次出现；"一个块拆成两块"则由后续 group 合并处理）
    owner_a: dict[str, int] = {}
    owner_b: dict[str, int] = {}
    for i, item in enumerate(src_a):
        for code in split_codes(item):
            owner_a.setdefault(code, i)
    for i, item in enumerate(src_b):
        for code in split_codes(item):
            owner_b.setdefault(code, i)

    lattice: list[str] = []
    for item in list(src_a) + list(src_b):
        for code in split_codes(item):
            if code not in lattice:
                lattice.append(code)
    lattice.sort()

    rows: list[dict] = []
    cur_a: set[int] = set()
    cur_b: set[int] = set()

    def flush() -> None:
        if not cur_a and not cur_b:
            return
        text_en = "\n\n".join(blocks_a[i]["text"].strip()
                              for i in sorted(cur_a) if blocks_a[i]["text"].strip())
        text_zh = "\n\n".join(blocks_b[i]["text"].strip()
                              for i in sorted(cur_b) if blocks_b[i]["text"].strip())
        btype = (blocks_a[sorted(cur_a)[0]]["type"] if cur_a
                 else blocks_b[sorted(cur_b)[0]]["type"])
        # ⚠️ **标题行的 `level` / `number` 必须跟着一起搬过来**。
        # 它们不是给读者看的装饰：`build_outline` 只收 `level` 在 1..4 的标题行，
        # 缺这个字段的标题会被**静默丢掉** —— 走坐标合并的页（实测 turbo 9/22 页）
        # 于是从目录里整段消失（`# B MATHEMATICAL PRELIMINARIES`、`## B.1 SHANNON…`、
        # `## C.1/C.2` 全都没了），下游按"标题行在它之前、层级最深"归节的分块层
        # 也就把 p16 整页算进了 `REFERENCES`（q16 公式专项因此检不到）。
        # 取值口径与 `pair_rows` 逐字一致：**优先 A 侧**（原始 Markdown 的 `#` 深度）。
        head = (blocks_a[sorted(cur_a)[0]] if cur_a else blocks_b[sorted(cur_b)[0]])
        rows.append({"original": text_en, "translation": text_zh, "type": btype,
                     "level": int(head.get("level") or 0),
                     "number": head.get("number") or ""})

    for code in lattice:
        a, b = owner_a.get(code), owner_b.get(code)
        if a is None or b is None:
            # 这一侧没有对应的输出块（模型漏了一段）→ 并进当前行，绝不新开一行去错位
            if a is not None:
                cur_a.add(a)
            if b is not None:
                cur_b.add(b)
            continue
        if a in cur_a or b in cur_b:
            # **1:N / N:1 合并**：任一侧的块已经在当前行里 → 合成同一行
            cur_a.add(a)
            cur_b.add(b)
            continue
        flush()
        cur_a, cur_b = {a}, {b}
    flush()

    for i, row in enumerate(rows):
        row["id"] = f"p{int(page):03d}-{i:02d}"
        row["index"] = i
    return rows, "coords"
