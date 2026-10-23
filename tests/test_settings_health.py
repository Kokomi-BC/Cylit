"""GET /settings/health 的 system / disk 两块（设置页「系统信息」）。

钉住的约定：

· 老字段一个都不动（database / vector_store / task_runner / data_dir 仍在）；
· system 只用标准库，os 是人能读的名字，python 形如 3.x；
· disk 单位统一 bytes，另外给一位小数的 percent（前端拿它画占用条）；
· data 目录递归求和带 60 秒缓存 —— 否则大目录每次刷新都扫盘；
· data_dir 不存在 / 扫盘失败 / 查库失败都不许让整个 health 500，对应项给 null。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

API = "/api/v1/settings/health"


@pytest.fixture(scope='module')
def client():
    from fastapi.testclient import TestClient

    import backend.main as main

    return TestClient(main.app)


@pytest.fixture
def settings_api():
    """拿模块本体，并保证每个用例都从「空缓存」起步。"""
    from backend.api import settings as mod

    mod._dir_usage_cache.clear()
    yield mod
    mod._dir_usage_cache.clear()


def _data(resp) -> dict:
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["error"] is None
    return body["data"]


def test_old_fields_untouched(client):
    data = _data(client.get(API))
    assert {"database", "vector_store", "task_runner", "data_dir"} <= set(data)
    assert isinstance(data["data_dir"], str) and data["data_dir"]


def test_system_block_fields_and_types(client):
    system = _data(client.get(API))["system"]
    assert system is not None
    assert isinstance(system["os"], str) and system["os"].strip()
    assert isinstance(system["os_detail"], str) and system["os_detail"].strip()
    assert isinstance(system["kernel"], str) and system["kernel"].strip()
    assert isinstance(system["arch"], str) and system["arch"].strip()
    assert isinstance(system["hostname"], str) and system["hostname"].strip()
    assert re.fullmatch(r"3\.\d+(\.\d+)?", system["python"]), system["python"]
    assert isinstance(system["cpu_count"], int) and system["cpu_count"] >= 1
    assert isinstance(system["uptime_s"], (int, float)) and system["uptime_s"] >= 0


def test_disk_block_units(client):
    disk = _data(client.get(API))["disk"]
    assert disk is not None
    total, used, free = disk["total_bytes"], disk["used_bytes"], disk["free_bytes"]
    assert all(isinstance(v, int) for v in (total, used, free))
    assert total > 0
    # used + free 会略小于 total（文件系统保留块 / 元数据）—— 用不等式与比例断言。
    assert used + free <= total
    assert used + free >= total * 0.9
    assert used > 0
    assert 0.0 <= disk["percent"] <= 100.0
    assert round(disk["percent"], 1) == disk["percent"]  # 一位小数
    assert disk["percent"] == pytest.approx(used * 100.0 / total, abs=0.1)
    assert Path(disk["path"]).is_absolute()
    assert disk["data_dir_bytes"] is None or disk["data_dir_bytes"] >= 0
    assert disk["db_bytes"] is None or disk["db_bytes"] >= 0


def test_missing_data_dir_falls_back_to_parent(client, tmp_path, monkeypatch, settings_api):
    from backend.core.config import Settings

    gone = tmp_path / "__no_such_data_dir__" / "deeper"
    assert not gone.exists()
    monkeypatch.setattr(settings_api, 'get_settings', lambda: Settings(data_dir=gone))

    data = _data(client.get(API))  # 不抛错、仍 200
    assert data["system"] is not None  # 其它块不受影响
    disk = data["disk"]
    assert disk is not None  # 回退到最近的已存在父目录
    assert Path(disk["path"]) == gone.absolute()
    assert disk["total_bytes"] > 0
    assert disk["data_dir_bytes"] is None  # 不存在的目录不扫
    assert 0.0 <= disk["percent"] <= 100.0


def test_data_dir_bytes_hits_cache(client, monkeypatch, settings_api):
    calls: list = []

    def fake_scan(root: Path, deadline: float) -> int:
        calls.append(root)
        return 123456

    monkeypatch.setattr(settings_api, '_scan_dir', fake_scan)

    first = _data(client.get(API))["disk"]["data_dir_bytes"]
    second = _data(client.get(API))["disk"]["data_dir_bytes"]
    assert first == second == 123456
    assert len(calls) == 1  # 第二次命中 60 秒缓存，没有再扫盘


def test_disk_failure_does_not_500(client, monkeypatch, settings_api):
    def boom():
        raise RuntimeError("disk 统计炸了")

    monkeypatch.setattr(settings_api, '_disk_block', boom)

    data = _data(client.get(API))
    assert data["disk"] is None  # 该块降级
    assert data["system"] is not None  # 其它块照旧
