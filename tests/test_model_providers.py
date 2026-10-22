"""模型提供方目录 / 端点守卫 / 远程模型列表（改造计划 §2.1、§7）。

这几条是纯逻辑 + 一个本地假 HTTP 服务，不联网、不依赖 MySQL：

· `endpoint_guard`：协议、userinfo、元数据地址、DNS 解析结果、内网放行口径；
· `providers_catalog`：官方目录的 kind/icon 由后端算出来，且不含任何密钥字段；
· `remote_models`：能解析三种常见形状，错误里不出现密钥。
"""

from __future__ import annotations

import pytest

from backend.core.errors import ValidationError
from backend.services.model import endpoint_guard


def test_normalize_rejects_bad_shapes():
    for bad in ("", "   ", "ftp://x.com/v1", "api.deepseek.com/v1",
                "https://u:p@api.deepseek.com/v1", "https://api.deepseek.com/v1?a=1",
                "https://api.deepseek.com/v1#x"):
        with pytest.raises(ValidationError):
            endpoint_guard.normalize_base_url(bad)


def test_normalize_strips_trailing_slash_and_lowercases_scheme():
    assert endpoint_guard.normalize_base_url("HTTPS://API.DeepSeek.com/v1/") == \
        "https://api.deepseek.com/v1"


def test_guard_blocks_cloud_metadata_even_when_private_allowed():
    for url in ("http://169.254.169.254/latest", "http://100.100.100.200/latest/meta-data"):
        with pytest.raises(ValidationError):
            endpoint_guard.guard_url_sync(url, allow_private=True)


def test_guard_blocks_loopback_unless_allowed():
    with pytest.raises(ValidationError):
        endpoint_guard.guard_url_sync("http://127.0.0.1:11434/v1", allow_private=False)
    assert endpoint_guard.guard_url_sync("http://127.0.0.1:11434/v1", allow_private=True)


def test_guard_blocks_private_ranges():
    for url in ("http://10.0.0.5/v1", "http://192.168.1.9/v1", "http://172.16.3.4/v1"):
        with pytest.raises(ValidationError):
            endpoint_guard.guard_url_sync(url, allow_private=False)


def test_allow_private_for_stored_vs_fresh(monkeypatch):
    monkeypatch.delenv(endpoint_guard.ALLOW_PRIVATE_ENV, raising=False)
    assert endpoint_guard.allow_private_for(stored_config=False) is False
    assert endpoint_guard.allow_private_for(stored_config=True) is True
    monkeypatch.setenv(endpoint_guard.ALLOW_PRIVATE_ENV, "0")
    assert endpoint_guard.allow_private_for(stored_config=True) is False
    monkeypatch.setenv(endpoint_guard.ALLOW_PRIVATE_ENV, "1")
    assert endpoint_guard.allow_private_for(stored_config=False) is True


def test_pick_ids_accepts_three_shapes():
    from backend.services.model import remote_models

    a = remote_models._pick_ids({"data": [{"id": "b"}, {"id": "a"}, {"id": "a"}]})
    assert [x["id"] for x in a] == ["a", "b"]
    b = remote_models._pick_ids({"models": ["x", "y"]})
    assert [x["id"] for x in b] == ["x", "y"]
    c = remote_models._pick_ids([{"id": "m", "owned_by": "deepseek"}])
    assert c == [{"id": "m", "owned_by": "deepseek"}]
    with pytest.raises(ValidationError):
        remote_models._pick_ids({"nope": 1})


def test_provider_catalog_shape_and_no_secret_leak(monkeypatch):
    from backend.services.model import providers_catalog, presets

    # 把预设裁成两条，避免依赖真实配置与数据库
    monkeypatch.setattr(presets, "list_presets", lambda: [
        {"id": "deepseek", "provider": "deepseek",
         "generation": {"model_name": "deepseek-flash",
                        "base_url": "https://api.deepseek.com/v1",
                        "secret_ref": "deepseek.key"}},
        {"id": "ollama", "provider": "ollama",
         "embedding": {"model_name": "bge-m3", "base_url": "http://127.0.0.1:11434/v1"}},
    ])
    data = providers_catalog.catalog()
    ids = [p["id"] for p in data["providers"]]
    # ollama 在 PROVIDER_META 里已标 hidden（没有品牌图标 → 前端只会画个空白方块），
    # 目录按用户口径跳过它 —— 所以这里只剩 deepseek。
    assert ids == ["deepseek"]
    deep = next(p for p in data["providers"] if p["id"] == "deepseek")
    assert deep["kind"] == "official"
    assert deep["icon"] == "deepseek"
    assert deep["types"] == ["generation"]
    assert deep["base_url"].startswith("https://")
    assert data["custom"]["kind"] == "custom"
    blob = repr(data)
    assert "deepseek.key" not in blob          # 凭据文件名不外泄
    assert "sk-" not in blob
