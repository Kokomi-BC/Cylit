"""引用登记表：**宿主分配** `[n]`，模型只负责写编号。

为什么编号必须由宿主给（抄 retain-pdf 的 M1）：模型自己编号会出现"同一段证据两轮拿到
不同号"和"号跳空"，前端点不动；而让模型抄内部 id 又会把 `chunk_id` 泄漏进正文
（真机探测里已经复现过：DeepSeek 在流式回答里写了 ``` `ch-91ac` ```）。

所以两侧看到的东西是**两份投影**：

- 给模型：``ref`` + 人话定位（``第 3 页 §3.1``）+ 短片段，**没有任何内部 id**；
- 给前端：完整的 `Citation`（含 `block_id` / `row_id`），跳转与高亮要用。
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Citation:
    ref: int
    paper_id: str
    kind: str                                   # page | block | chunk | figure | table
    page: int | None = None
    section: str | None = None
    block_id: str | None = None
    row_id: str | None = None
    snippet: str = ""
    image_urls: list[str] = field(default_factory=list)
    #: 这条证据记住的**行身份**（chunk 的 ``metadata.pids``）。阶段 2b 只是读时导出：
    #: 存量 chunk 还没有这个字段，非 chunk 证据（页图 / 公式块）也没有 ——
    #: 空列表时 ``to_dict`` **不写这个键**，所以前端拿到的形状与改造前一致。
    pids: list[str] = field(default_factory=list)
    #: 人话定位串（``第 3 页 §3.1 · 公式``），给模型指位置用，也顺手当引用表标题
    label: str = ""
    #: 这条证据的**完整 LaTeX**（公式引用才有）。为什么非要它：前端跳转要靠它认出那一行里
    #: 的**哪一枚** KaTeX（`findFormula` 比签名）。只有 `snippet` 时那是截到 160 字的残片，
    #: 长公式认不出来就退化成整行闪 —— 与解读面板 / 笔记卡片的精度对不齐。
    latex: str = ""

    def model_view(self) -> dict:
        """给模型的**瘦投影**：编号 + 定位 + 片段（无内部 id、无 bbox、无分数）。"""
        item: dict = {"ref": self.ref, "locate": self.label or self._locate()}
        if self.snippet:
            item["snippet"] = self.snippet
        if self.image_urls:
            item["image_url"] = self.image_urls[0]
        return item

    def _locate(self) -> str:
        bits = []
        if self.page:
            bits.append(f"第 {self.page} 页")
        if self.section:
            bits.append(self.section)
        return " · ".join(bits) or self.kind

    def to_dict(self) -> dict:
        item = {
            "ref": self.ref, "paper_id": self.paper_id, "kind": self.kind,
            "page": self.page, "section": self.section,
            "block_id": self.block_id, "row_id": self.row_id,
            "snippet": self.snippet, "image_urls": list(self.image_urls),
            "label": self.label or self._locate(),
        }
        # 只在真有身份时才出现：老索引（chunk 还没有 pids）与非 chunk 证据的形状一个字不变。
        if self.pids:
            item["pids"] = list(self.pids)
        # 同 pids 口径：非公式引用（以及还没登 latex 的老路径）不写这个键，形状不变。
        if self.latex:
            item["latex"] = self.latex
        return item


def _merge_pids(citation: Citation, pids: list[str] | None) -> None:
    """把新拿到的行身份并进已有引用：按先来后到去重，**绝不覆盖成空**。"""
    for raw in (pids or []):
        pid = str(raw or "")
        if pid and pid not in citation.pids:
            citation.pids.append(pid)


class RefRegistry:
    """一次问答内的引用表（**每轮一问一份**，不跨请求共享）。"""

    def __init__(self, paper_id: str) -> None:
        self.paper_id = paper_id
        self._by_ref: dict[int, Citation] = {}
        self._by_key: dict[str, int] = {}
        #: 内部 id（chunk_id / block_id）→ ref，清洗阶段要用
        self._by_id: dict[str, int] = {}

    # ---------------- 分配 ----------------
    def assign(self, *, key: str, kind: str, snippet: str = "",
               page: int | None = None, section: str | None = None,
               block_id: str | None = None, row_id: str | None = None,
               image_urls: list[str] | None = None, label: str = "",
               ids: tuple[str, ...] = (), paper_id: str | None = None,
               pids: list[str] | None = None, latex: str = "") -> Citation:
        """登记一条证据；同一个 ``key`` 重复登记会复用原编号。

        复用很重要：模型很爱把同一页读两遍（真机探测里 `paper_search` 连发两次），
        不复用的话引用表会出一堆重复项，编号还跳。

        ``paper_id`` 只在**跨库检索**（`kb_search`）时才显式传：那条命中不属于当前论文，
        引用必须带上它自己那篇的 id，否则前端会跳到当前论文的同一页 —— 一个不相干的段落。

        ``pids`` 是这条证据记住的**行身份**（阶段 2b，读时导出）：同一 ``key`` 复用编号时
        与已有的 pids **合并**，所以先到的那份不会因为后到的命中没带而丢。

        ``latex`` 只有公式引用会带：复用同一个号时**空的不覆盖非空的**（同一枚公式第二次
        命中可能只带 snippet 或被切成片段，覆盖回去前端就只剩残片可认了）。
        """
        existing = self._by_key.get(key)
        if existing is not None:
            # 复用编号时**合并**行身份，不是覆盖：同一处证据的第二条命中（A 路 / B 路）
            # 可能只带着自己那一份 pids，覆盖就会把先到的那份抹掉。
            _merge_pids(self._by_ref[existing], pids)
            # 先到的那份可能没带 latex（老路径只登 snippet），补上但绝不覆盖成空
            if latex and not self._by_ref[existing].latex:
                self._by_ref[existing].latex = latex
            # 复用一个号时，**新出现的内部 id 也要登记**：同一处证据的两条命中（A 路 / B 路
            # 各自的 chunk id）共用一个号，清洗阶段仍要认得出这两个 id 都属于它。
            for raw in (block_id, *ids):
                if raw:
                    self._by_id.setdefault(str(raw), existing)
            return self._by_ref[existing]
        ref = len(self._by_ref) + 1
        citation = Citation(ref=ref, paper_id=paper_id or self.paper_id, kind=kind,
                            page=page, section=section, block_id=block_id,
                            row_id=row_id, snippet=snippet,
                            image_urls=list(image_urls or []), label=label,
                            pids=[str(p) for p in (pids or []) if p], latex=latex)
        self._by_ref[ref] = citation
        self._by_key[key] = ref
        for raw in (block_id, *ids):
            if raw:
                self._by_id.setdefault(str(raw), ref)
        return citation

    # ---------------- 查询 ----------------
    def by_ref(self, ref: int) -> Citation | None:
        return self._by_ref.get(int(ref))

    def ref_of_id(self, raw: str) -> int | None:
        return self._by_id.get(str(raw))

    def all(self) -> list[Citation]:
        return [self._by_ref[r] for r in sorted(self._by_ref)]

    def has_evidence(self) -> bool:
        return bool(self._by_ref)

    def model_payload(self) -> list[dict]:
        return [c.model_view() for c in self.all()]

    def to_list(self) -> list[dict]:
        return [c.to_dict() for c in self.all()]

    def __len__(self) -> int:
        return len(self._by_ref)
