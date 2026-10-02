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
    kind: str                                   # page | block | chunk | figure
    page: int | None = None
    section: str | None = None
    block_id: str | None = None
    row_id: str | None = None
    snippet: str = ""
    image_urls: list[str] = field(default_factory=list)
    #: 人话定位串（``第 3 页 §3.1 · 公式``），给模型指位置用，也顺手当引用表标题
    label: str = ""

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
        return {
            "ref": self.ref, "paper_id": self.paper_id, "kind": self.kind,
            "page": self.page, "section": self.section,
            "block_id": self.block_id, "row_id": self.row_id,
            "snippet": self.snippet, "image_urls": list(self.image_urls),
            "label": self.label or self._locate(),
        }


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
               ids: tuple[str, ...] = ()) -> Citation:
        """登记一条证据；同一个 ``key`` 重复登记会复用原编号。

        复用很重要：模型很爱把同一页读两遍（真机探测里 `paper_search` 连发两次），
        不复用的话引用表会出一堆重复项，编号还跳。
        """
        existing = self._by_key.get(key)
        if existing is not None:
            return self._by_ref[existing]
        ref = len(self._by_ref) + 1
        citation = Citation(ref=ref, paper_id=self.paper_id, kind=kind, page=page,
                            section=section, block_id=block_id, row_id=row_id,
                            snippet=snippet, image_urls=list(image_urls or []),
                            label=label)
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
