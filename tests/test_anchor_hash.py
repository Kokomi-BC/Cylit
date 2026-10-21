"""高亮的重定位键（``Highlight.anchor_hash``）：**pid 优先**，拿不到 pid 逐字节退回旧形式。

背景（``docs/身份模型.md`` §8 记录的既有缺陷）：``anchor_hash`` 以前是
``sha256(selected_text + block_id + side)`` —— block_id 是**位置编号**（``p015-06``），
页内插一行后面全换号，于是"重译后按 anchor_hash 重定位"从未生效。pid 是稳定身份
（生成一次、重建时按指纹继承），所以现在能拿到 pid 时用它。

这里钉住两件事，且**不连库**（session 用构造对象）：

1. 同一行文本不变、pid 不变 → 哈希不变（哪怕 block_id 换号）；
2. 拿不到 pid（markdown / pdf 面、老锚点、注册表还没生成）→ 与旧版**逐字节**一致
   （存量高亮的哈希不能因为这次改动失效）。
"""

from __future__ import annotations

import hashlib

from backend.services.annotation import annotation_service as ann


def _legacy_hash(selected_text: str, block_id: str, side: str) -> str:
    """改造前那一行（``sha256_text(text + block_id + side)[:64]``）的逐字节复刻。"""
    return hashlib.sha256((selected_text + block_id + side).encode("utf-8")).hexdigest()[:64]


def test_pid_hash_is_stable_when_block_id_changes():
    """行文本不变、pid 不变 → 哈希不变（即便 block_id 从 p015-06 换成了 p016-02）。"""
    old = ann.anchor_hash("向量量化", "p015-06", "en", 4, 10, pid="r-abc123")
    new = ann.anchor_hash("向量量化", "p016-02", "en", 4, 10, pid="r-abc123")

    assert old == new
    assert old != _legacy_hash("向量量化", "p015-06", "en"), "pid 优先时不该再拼 block_id"


def test_pid_hash_separates_sides_and_offsets():
    """同一行两侧（原文/译文）与不同偏移必须是不同的键（否则重定位会认错那一条）。"""
    base = ann.anchor_hash("注意力", "p003-01", "en", 0, 3, pid="r-1")

    assert base != ann.anchor_hash("注意力", "p003-01", "zh", 0, 3, pid="r-1")
    assert base != ann.anchor_hash("注意力", "p003-01", "en", 0, 4, pid="r-1")


def test_fallback_is_byte_identical_to_legacy():
    """拿不到 pid → 与旧版逐字节一致（存量高亮、markdown/pdf 面靠这条不失效）。"""
    for text, block_id, side in (("先有高亮", "p003-02", "en"),
                                 ("向量量化", "md:p3", "zh"),
                                 ("", "pdf:p1", "en")):
        assert ann.anchor_hash(text, block_id, side) == _legacy_hash(text, block_id, side)


def test_row_pid_has_no_identity_for_non_row_anchors():
    """只有正文行 id 才可能有 pid：markdown / pdf 面、解析块 id 一律给 None（不猜）。"""
    assert ann.row_pid("p", "md:p3") is None
    assert ann.row_pid("p", "pdf:p1") is None
    assert ann.row_pid("p", "attentionisa-p04-b06") is None
    assert ann.row_pid("", "p003-02") is None      # 没论文 id 就没处查


# --------------------------------------------------------------------------
# 服务层同一条口径：create_highlight 用的是这两个函数的结果
# --------------------------------------------------------------------------


class _Session:
    def __init__(self) -> None:
        self.added: list = []

    def add(self, row) -> None:
        self.added.append(row)

    def flush(self) -> None:
        pass


class _SessionCtx:
    def __init__(self, sess: _Session) -> None:
        self.sess = sess

    def __enter__(self) -> _Session:
        return self.sess

    def __exit__(self, *args) -> bool:
        return False


def _create(monkeypatch, payload: dict, pid: str | None):
    """用假 session 存一条高亮；pid 由 monkeypatch 的 row_pid 决定（不碰页产物）。"""
    sess = _Session()
    monkeypatch.setattr(ann, "session_scope", lambda: _SessionCtx(sess))
    monkeypatch.setattr(ann, "row_pid", lambda paper_id, block_id: pid)
    ann.create_highlight(payload)
    return sess.added[0]


def test_create_highlight_uses_pid_hash(monkeypatch):
    """能取到 pid → anchor_hash 用 pid 形式（block_id 换号不影响它）。"""
    payload = {"paper_id": "p", "block_id": "p015-06", "selected_text": "向量量化",
               "start_offset": 4, "end_offset": 9, "side": "en"}
    row = _create(monkeypatch, payload, pid="r-abc123")

    assert row.anchor_hash == ann.anchor_hash("向量量化", "p015-06", "en", 4, 9,
                                              pid="r-abc123")


def test_create_highlight_falls_back_to_legacy_hash(monkeypatch):
    """取不到 pid（md/pdf 面、老数据）→ 落库的就是旧版哈希（逐字节）。"""
    payload = {"paper_id": "p", "block_id": "md:p3", "selected_text": "向量量化",
               "start_offset": 0, "end_offset": 4, "side": "zh"}
    row = _create(monkeypatch, payload, pid=None)

    assert row.anchor_hash == _legacy_hash("向量量化", "md:p3", "zh")
