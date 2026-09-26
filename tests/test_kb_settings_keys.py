"""设置键解析：全库只有一条规则 —— ``kb.<key>`` 优先、裸键回退。

## 为什么专门给这件事写测试

这个 bug 的形状是**静默失效**：文档与测试都写 `kb.retrieval_types` / `kb.unit_expand`，
代码读的却是裸键 `retrieval_types` / `unit_expand` —— 于是"用户在文档里看到可以调的
两个开关"实际上一次都没被读过，而**没有任何报错**：它只是悄悄用了默认值。
所以这里把三件事都钉死：

1. `setting()` 的优先级（`kb.` 优先 → 裸键 → 默认值）；
2. 两个具体读取点（`retrieval.default_search_types` / `rag_service.unit_expand_mode`）
   真的走这条规则 —— 只测 `setting()` 是不够的，当初的 bug 就在"读取点写错了键名"；
3. 写入侧不留歧义：`PUT /settings` 会删掉同名 `kb.<key>` 行（否则界面改的值会被
   前缀优先的旧行盖住 —— 同一个 bug 的另一种表现）。

全部离线：`retrieval._setting_raw` 是唯一的数据库入口，把它替成字典即可。
"""
from __future__ import annotations

import pytest

from backend.services.knowledge import knowledge_service as ks
from backend.services.knowledge import rag_service, retrieval


def _fake_store(monkeypatch, values: dict) -> None:
    """把唯一的数据库入口 `_setting_raw` 换成字典查询（键不存在 → `_MISSING`）。"""
    monkeypatch.setattr(retrieval, "_setting_raw",
                        lambda key: values.get(key, retrieval._MISSING))


# --------------------------------------------------------------------------
# 1. 解析规则
# --------------------------------------------------------------------------


def test_kb_prefix_wins_over_bare_key(monkeypatch):
    _fake_store(monkeypatch, {"kb.unit_expand": 2, "unit_expand": 0})
    assert retrieval.setting("unit_expand", 1) == 2


def test_bare_key_is_a_fallback(monkeypatch):
    """老库里可能是脚本写的裸键 —— 只认一种写法会让老行静默失效。"""
    _fake_store(monkeypatch, {"unit_expand": 0})
    assert retrieval.setting("unit_expand", 1) == 0


def test_default_when_neither_spelling_exists(monkeypatch):
    _fake_store(monkeypatch, {})
    assert retrieval.setting("unit_expand", 1) == 1


def test_value_none_still_counts_as_set(monkeypatch):
    """值是 None 也要算"读到了"：否则回退链会在第一层停住，裸键永远读不到。"""
    _fake_store(monkeypatch, {"kb.unit_expand": None, "unit_expand": 2})
    assert retrieval.setting("unit_expand", 1) is None


def test_setting_int_falls_back_on_bad_value(monkeypatch):
    _fake_store(monkeypatch, {"kb.chunk_min_chars": "不是数字"})
    assert retrieval.setting_int("chunk_min_chars", 200) == 200
    _fake_store(monkeypatch, {"kb.chunk_min_chars": "1600"})
    assert retrieval.setting_int("chunk_min_chars", 200) == 1600


@pytest.mark.parametrize("raw,expected", [
    (True, True), (False, False),
    ("1", True), ("true", True), ("yes", True), ("on", True),
    ("0", False), ("false", False), ("no", False), ("off", False),
    ('"1"', True),                       # 脚本写的是带引号的 JSON 字符串
])
def test_setting_flag_accepts_both_write_paths(monkeypatch, raw, expected):
    """`PUT /settings` 写 JSON（读出来是 bool），脚本/手改写文本（读出来是字符串）。"""
    _fake_store(monkeypatch, {"kb.unit_index": raw})
    assert retrieval.setting_flag("unit_index", not expected) is expected


def test_setting_flag_bad_value_uses_default(monkeypatch):
    _fake_store(monkeypatch, {"kb.unit_index": "maybe"})
    assert retrieval.setting_flag("unit_index", True) is True


# --------------------------------------------------------------------------
# 2. 读取点（bug 原本就在这里：键名写错，规则再对也没用）
# --------------------------------------------------------------------------


def test_default_search_types_reads_documented_key(monkeypatch):
    _fake_store(monkeypatch, {"kb.retrieval_types": "unit,text"})
    assert retrieval.default_search_types() == ["unit", "text"]


def test_default_search_types_empty_means_no_type_filter(monkeypatch):
    """空串 = 不限类型（改造前行为）；不写这一行时用默认候选集。"""
    _fake_store(monkeypatch, {"kb.retrieval_types": ""})
    assert retrieval.default_search_types() == list(retrieval.DEFAULT_SEARCH_TYPES)


def test_unit_expand_reads_documented_key(monkeypatch):
    _fake_store(monkeypatch, {"kb.unit_expand": 0})
    assert rag_service.unit_expand_mode() == 0
    _fake_store(monkeypatch, {"kb.unit_expand": 2})
    assert rag_service.unit_expand_mode() == 2
    _fake_store(monkeypatch, {"kb.unit_expand": 9})       # 越界夹回 2
    assert rag_service.unit_expand_mode() == 2
    _fake_store(monkeypatch, {"unit_expand": "-3"})        # 裸键 + 字符串
    assert rag_service.unit_expand_mode() == 0


def test_using_breaks_and_unit_index_read_documented_keys(monkeypatch):
    _fake_store(monkeypatch, {"kb.chunk_use_breaks": "0", "kb.unit_index": "false"})
    assert ks.using_breaks() is False
    assert ks.unit_index_enabled() is False
    _fake_store(monkeypatch, {})
    assert ks.using_breaks() is True      # 默认开
    assert ks.unit_index_enabled() is True


def test_chunk_params_keys_are_bare_and_prefixed_both_work(monkeypatch):
    _fake_store(monkeypatch, {"kb.chunk_target_chars": 1600})
    target, _, _ = ks.chunk_params()
    assert target == 1600
    _fake_store(monkeypatch, {"chunk_target_chars": 1200})
    target, _, _ = ks.chunk_params()
    assert target == 1200


# --------------------------------------------------------------------------
# 3. 枚举与白名单（不然"能调"这件事在界面上根本不存在）
# --------------------------------------------------------------------------


def test_chunk_types_includes_unit():
    """`unit` 是真实在用、且是检索默认候选的类型，不该漏在规范枚举外。"""
    assert "unit" in ks.CHUNK_TYPES
    assert set(ks.CHUNK_TYPES) == {"text", "unit", "formula", "figure", "table"}


def test_editable_whitelist_covers_knowledge_keys():
    """`kb.*` 那一组以前不在白名单里 → 只能靠脚本直接写库。"""
    from backend.api import settings as settings_api

    for key in ("chunk_target_chars", "chunk_min_chars", "chunk_hard_chars",
                "chunk_use_breaks", "unit_index", "unit_expand", "retrieval_types"):
        assert key in settings_api.EDITABLE, f"{key} 不在 /settings 白名单里"


def test_editable_keys_are_bare_names():
    """白名单里的键必须是**裸名**：带 `kb.` 前缀会和 `setting()` 的前缀拼成 `kb.kb.*`。"""
    from backend.api import settings as settings_api

    bad = [k for k in settings_api.EDITABLE if k.startswith(retrieval.SETTING_PREFIX)]
    assert bad == [], f"白名单里不该出现带前缀的键：{bad}"
