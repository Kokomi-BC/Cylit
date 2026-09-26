"""两路并发的任务编排（离线）：按路建任务 / 按路幂等 / 融合模式互斥 / 产物只写自己那一侧。

## 为什么这些测试必须是离线的

真实的并发验证要花视觉调用（两路各一次一页 ≈ 12s + 27s），那是 P4 全量重跑时做的。
这里用 monkeypatch 把 `task_service` 那层替掉，钉住**编排逻辑**本身：

1. `paths=["a","b"]` 建**两个**任务、两个不同任务类型（并发的前提是两条独立流水线）；
2. 幂等**按路**判定：同一路重复触发复用、不同路互不影响；
3. `fused` 不能和 `a`/`b` 混着触发（它俩写的是同一个文件，同时跑必然互相覆盖）；
4. 一路的产物只写自己那一侧的字段（A 件不可能有 `translation_md`，反之亦然）——
   这是"两路各自独立 chunk"的地基：谁都不会顺手替对方产出内容。
"""
from __future__ import annotations

import pytest

from backend.core.errors import ValidationError
from backend.services.translation import translation_service as ts
from backend.services.translation.page_translate import (
    PATH_VERSION, PageCallResult, PageStore, build_path_payload,
)


# --------------------------------------------------------------------------
# 1. 路 → 任务类型
# --------------------------------------------------------------------------


def test_task_types_per_path() -> None:
    assert ts.PATH_TASK_TYPES == {None: "TRANSLATE_MD", "a": "TRANSCRIBE_EN",
                                  "b": "TRANSLATE_ZH"}


@pytest.mark.parametrize("raw,expected", [
    ("a", "a"), ("A", "a"), ("b", "b"),
    ("fused", None), ("FUSED", None), (None, None), ("", None),
])
def test_normalize_path(raw, expected) -> None:
    assert ts.normalize_path(raw) == expected


def test_normalize_path_rejects_unknown() -> None:
    with pytest.raises(ValidationError):
        ts.normalize_path("c")


def test_page_store_uses_per_path_files(monkeypatch, tmp_path) -> None:
    md = tmp_path / "md"
    md.mkdir()
    fake = {"md": md, "path_a": md / "path_a.json", "path_b": md / "path_b.json",
            "page_products": md / "pages.json"}
    monkeypatch.setattr(ts.paper_service, "paper_paths", lambda pid: fake)
    assert ts.page_store("p").path == md / "pages.json"
    assert ts.page_store("p", "a").path == md / "path_a.json"
    assert ts.page_store("p", "b").path == md / "path_b.json"
    # 单路产物用单路版本号（与旧合并格式分开）
    assert ts.page_store("p", "b").version == PATH_VERSION


# --------------------------------------------------------------------------
# 2. 一路的产物只写自己那一侧
# --------------------------------------------------------------------------


def _result() -> PageCallResult:
    return PageCallResult(original_md="# 1 Method\n\nEN body.",
                          translation_md="# 1 方法\n\n中文正文。",
                          carry_md="body.", breaks=[
                              {"start": "# 1 Method", "type": "section"}],
                          model="m", prompt_tokens=10, completion_tokens=20,
                          reasoning_tokens=5, latency_ms=12.34, max_tokens=8192)


def test_path_a_payload_has_only_the_english_side() -> None:
    payload = build_path_payload(path="a", page=1, page_total=1, result=_result())
    assert payload["path"] == "a"
    assert payload["original_md"] == "# 1 Method\n\nEN body."
    assert "translation_md" not in payload, "A 件不该出现译文（那会让两路各产出一份内容）"
    assert "carry_md" not in payload, "carry_md 是翻译侧独有的机制"
    assert "rows" not in payload, "单路不存行（行必须由两路合并产生）"
    assert "align" not in payload
    assert payload["breaks"] == [{"start": "# 1 Method", "type": "section"}]


def test_path_b_payload_has_only_the_chinese_side() -> None:
    payload = build_path_payload(path="b", page=1, page_total=1, result=_result())
    assert payload["path"] == "b"
    assert payload["translation_md"] == "# 1 方法\n\n中文正文。"
    assert payload["carry_md"] == "body."
    assert "original_md" not in payload
    assert "rows" not in payload


def test_build_path_payload_rejects_unknown_path() -> None:
    with pytest.raises(ValidationError):
        build_path_payload(path="c", page=1, page_total=1, result=_result())


def test_path_store_roundtrip_and_done_pages(tmp_path) -> None:
    """A 件只有 `original_md`，`done_pages()` 也必须认它 —— 否则"已转录的页"会被反复重跑。"""
    path = tmp_path / "path_a.json"
    store = PageStore("p", path, version=PATH_VERSION)
    store.set_page_total(3)
    store.put(2, build_path_payload(path="a", page=2, page_total=3, result=_result()))
    assert store.done_pages() == [2]
    stats = store.stats()
    assert stats["pages_done"] == 1 and stats["page_total"] == 3
    # 重新打开（走真实读盘路径）
    again = PageStore("p", path, version=PATH_VERSION)
    assert again.page(2)["original_md"] == "# 1 Method\n\nEN body."
    assert again.page(2)["path"] == "a"


# --------------------------------------------------------------------------
# 3. 编排：按路建任务 + 按路幂等 + 融合互斥
# --------------------------------------------------------------------------


class _FakeTaskService:
    """替掉 `task_service`：只记下建了哪些任务、有哪些活动任务。"""

    def __init__(self) -> None:
        self.created: list[tuple[str, dict]] = []
        self.active: dict[str, dict] = {}
        self.finished: list[tuple[str, str]] = []

    def find_active(self, type_, paper_id=None, queue=None):
        return self.active.get(type_)

    def create(self, type_, queue, paper_id=None, params=None, total_items=0,
               message=None):
        self.created.append((type_, dict(params or {})))
        task = {"id": f"t-{type_}", "type": type_, "status": "pending",
                "params": dict(params or {}), "total_items": total_items,
                "message": message, "cancel_requested": False}
        self.active[type_] = task
        return task

    def finish(self, task_id, status, *, message=None):
        self.finished.append((task_id, status))
        for type_, t in list(self.active.items()):
            if t["id"] == task_id:
                self.active.pop(type_)


@pytest.fixture
def fake_tasks(monkeypatch, tmp_path):
    """把任务层与页面图都替掉：让 `start_translation` 能纯逻辑地跑。"""
    fake = _FakeTaskService()
    monkeypatch.setattr(ts, "_task_service", lambda: fake, raising=False)
    import backend.services.task.task_service as task_mod

    monkeypatch.setattr(task_mod, "task_service", fake)
    monkeypatch.setattr(ts.paper_service, "get_paper", lambda pid: {"id": pid})
    monkeypatch.setattr(ts, "list_pages", lambda pid: [1, 2, 3])
    md = tmp_path / "md"
    md.mkdir()
    monkeypatch.setattr(ts.paper_service, "paper_paths", lambda pid: {
        "md": md, "path_a": md / "path_a.json", "path_b": md / "path_b.json",
        "page_products": md / "pages.json"})
    return fake


def test_two_paths_create_two_tasks(fake_tasks) -> None:
    out = ts.start_translation("p", paths=["a", "b"])
    assert out["paths"] == ["a", "b"]
    assert [t for t, _ in fake_tasks.created] == ["TRANSCRIBE_EN", "TRANSLATE_ZH"]
    assert len(out["task_ids"]) == 2
    assert out["created"] == 2 and out["reused"] is False
    # 顶层 `id` 给"翻译"那一路（前端与管理脚本的历史用法）
    assert out["id"] == "t-TRANSLATE_ZH"


def test_fused_is_its_own_task(fake_tasks) -> None:
    out = ts.start_translation("p", paths=["fused"])
    assert [t for t, _ in fake_tasks.created] == ["TRANSLATE_MD"]
    assert out["paths"] == ["fused"]


def test_fused_cannot_run_alongside_paths(fake_tasks) -> None:
    """融合模式写 `pages.json`，两路写 `path_*.json`；同时跑会让人分不清读哪一份。"""
    with pytest.raises(ValidationError):
        ts.start_translation("p", paths=["a", "fused"])


def test_idempotency_is_per_path(fake_tasks) -> None:
    first = ts.start_translation("p", paths=["a", "b"])
    assert first["reused"] is False
    # 再触发一次：两路都已有活动任务 → 全复用，不再新建
    again = ts.start_translation("p", paths=["a", "b"])
    assert again["reused"] is True and again["created"] == 0
    assert len(fake_tasks.created) == 2
    assert again["task_ids"] == first["task_ids"]
    # 只请求 B 路：A 路的活动任务不影响它被复用
    only_b = ts.start_translation("p", paths=["b"])
    assert only_b["reused"] is True and only_b["created"] == 0


def test_zombie_task_is_dropped_and_recreated(fake_tasks) -> None:
    """已请求取消的老任务不能挡住新任务（否则前端点「继续」拿到一个永不执行的任务）。"""
    fake_tasks.active["TRANSLATE_ZH"] = {
        "id": "t-old", "type": "TRANSLATE_ZH", "cancel_requested": True,
        "updated_at": None}
    out = ts.start_translation("p", paths=["b"])
    assert fake_tasks.finished == [("t-old", "cancelled")]
    assert out["created"] == 1 and out["reused"] is False


def test_requested_tasks_carry_mode_and_pages(fake_tasks) -> None:
    ts.start_translation("p", paths=["a"], mode="retranslate", force=True,
                         brief=False, pages=[2])
    _, params = fake_tasks.created[0]
    assert params == {"mode": "retranslate", "force": True, "brief": False,
                      "pages": [2]}


def test_total_items_is_the_scoped_page_count(fake_tasks) -> None:
    """任务面板的分母 = 本次任务负责的页数（不是全篇 3 页）。"""
    ts.start_translation("p", paths=["a"], pages=[2])
    task = fake_tasks.active["TRANSCRIBE_EN"]
    assert task["total_items"] == 1
