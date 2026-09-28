"""解析引擎的接线：API 参数 → 服务 → 任务参数 → 任务处理器分派（离线）。

默认不跑（``@pytest.mark.online``）：``pytest --run-online``。

这一层最容易出的错是**静默回退**：用户在导入弹窗选了"在线解析"，但某一处把 ``engine``
丢了，任务照样跑 —— 只是悄悄用了本地解析，用户要到很久以后才发现（这个项目已经因为
"两处键名不一致导致设置静默失效"吃过一次亏）。所以这里从 `_fill_parse_options` 一路
钉到 `handle_parse` 的分派。
"""

from __future__ import annotations

import json

import pytest

from backend.core.config import Settings
from backend.core.errors import ValidationError
from backend.services.paper import figure_regions, mineru_service, paper_service

pytestmark = pytest.mark.online


_store: dict = {}


@pytest.fixture
def fake_settings(tmp_path, monkeypatch):
    """把 mineru_service 看到的 Settings 指向临时目录（token 只写临时文件）。

    同时把唯一的数据库入口 `_stored` 换成字典 —— 与 `test_kb_settings_keys.py` 同一条
    思路：离线测"规则"，不依赖 MySQL。
    """
    _store.clear()
    s = Settings(secrets_dir=tmp_path / "secrets", data_dir=tmp_path / "data")
    s.ensure_dirs()
    monkeypatch.setattr(mineru_service, "get_settings", lambda: s)
    # 读写两个口都换成字典：真实实现里 `_stored` 会把库里的 JSON 文本解回来，
    # 替身必须照做，否则取到的是 `"mineru"`（带引号）这种形态，跟线上不一致。
    monkeypatch.setattr(mineru_service, "_stored",
                        lambda: {k: json.loads(v) for k, v in _store.items()})
    monkeypatch.setattr(mineru_service, "_write_setting",
                        lambda key, value: _store.__setitem__(key, json.dumps(value)))
    return s


# --------------------------------------------------------------------------
# 1. 任务参数补齐：engine 与其他开关同一条优先级链
# --------------------------------------------------------------------------


def _fake_parse_options(monkeypatch, last: dict) -> None:
    monkeypatch.setattr(figure_regions, "parse_options", lambda pid: dict(last))


def test_engine_defaults_to_the_settings_value(monkeypatch, fake_settings):
    _fake_parse_options(monkeypatch, {})
    monkeypatch.setattr(mineru_service, "effective",
                        lambda: {"engine_default": "mineru"})
    opts = paper_service._fill_parse_options("p1", {})
    assert opts["engine"] == "mineru"
    assert opts["figure_caption"] is False and opts["deep_figure_search"] is False


def test_explicit_engine_wins(monkeypatch, fake_settings):
    """显式给了 engine 就不该被改写（`_fill_parse_options` 只补"没给"的键）。"""
    _fake_parse_options(monkeypatch, {"engine": "mineru"})
    monkeypatch.setattr(mineru_service, "effective",
                        lambda: {"engine_default": "mineru"})
    opts = paper_service._fill_parse_options("p1", {"engine": "local"})
    assert "engine" not in opts
    assert opts["figure_caption"] is False


def test_reparse_keeps_the_engine_this_paper_was_parsed_with(monkeypatch, fake_settings):
    """用户点「重新解析」不该因为面板默认值变了就把论文从在线切回本地。"""
    _fake_parse_options(monkeypatch, {"engine": "mineru", "figure_caption": True})
    monkeypatch.setattr(mineru_service, "effective",
                        lambda: {"engine_default": "local"})
    opts = paper_service._fill_parse_options("p1", {})
    assert opts["engine"] == "mineru" and opts["figure_caption"] is True


def _capture_task(monkeypatch) -> dict:
    """把 task_service 换成替身，捕捉 create_parse_task 真正落下去的参数。"""
    from backend.services.task import task_service as ts

    captured: dict = {}
    monkeypatch.setattr(paper_service, "get_paper", lambda pid: {"id": pid})
    monkeypatch.setattr(ts.task_service, "find_active", lambda *a, **k: None)

    def fake_create(task_type, queue, paper_id, **kw):
        captured.update(task_type=task_type, queue=queue, paper_id=paper_id, **kw)
        return {"id": "t1"}

    monkeypatch.setattr(ts.task_service, "create", fake_create)
    return captured


def test_bad_engine_value_is_rejected(monkeypatch, fake_settings):
    _fake_parse_options(monkeypatch, {})
    _capture_task(monkeypatch)
    with pytest.raises(ValidationError) as err:
        paper_service.create_parse_task("p1", force=True, params={"engine": "cloud"})
    assert "local" in str(err.value) and "mineru" in str(err.value)


def test_auto_engine_means_use_the_default(monkeypatch, fake_settings):
    _fake_parse_options(monkeypatch, {})
    monkeypatch.setattr(mineru_service, "effective",
                        lambda: {"engine_default": "mineru"})
    captured = _capture_task(monkeypatch)
    paper_service.create_parse_task("p1", force=True, params={"engine": "auto"})
    assert captured["params"]["engine"] == "mineru"


def test_create_parse_task_passes_the_engine_through(monkeypatch, fake_settings):
    """create_parse_task 是导入与重解析的公共入口，engine 必须落到任务参数里。

    用替身把 `task_service` 与 `get_paper` 换掉，不必起数据库 —— 这条测的是**接线**。
    """
    _fake_parse_options(monkeypatch, {})
    monkeypatch.setattr(mineru_service, "effective", lambda: {"engine_default": "local"})
    captured = _capture_task(monkeypatch)

    paper_service.create_parse_task("p1", force=True, params={"engine": "mineru"})
    assert captured["task_type"] == "PARSE"
    assert captured["params"]["engine"] == "mineru"
    assert captured["params"]["force"] is True



# --------------------------------------------------------------------------
# 2. 在线解析的参数：模型 / OCR / 公式 / 表格 / 页码范围
# --------------------------------------------------------------------------


@pytest.mark.parametrize("raw,want", [
    ("1-10", "1-10"),
    ("2,4-6", "2,4-6"),
    ("2--2", "2--2"),                 # `-2` = 倒数第二页，不能把 `--` 压成一个 `-`
    (" 3 , 5-7 ", "3,5-7"),
    ("7", "7"),
    ("", ""),                          # 留空 = 全部页
])
def test_page_ranges_accepts_the_official_forms(raw, want):
    assert mineru_service.norm_page_ranges(raw) == want


@pytest.mark.parametrize("bad", ["abc", "1-", "-2", "1..3", "1-2-3", "1,a", ","])
def test_page_ranges_rejects_bad_input(bad):
    """写错页码要给**人话**：服务端只会回 `-500 参数错误`，用户猜不到是页码。"""
    with pytest.raises(ValidationError) as err:
        mineru_service.norm_page_ranges(bad)
    assert "页码范围" in str(err.value) and "1-10" in str(err.value)


def test_normalize_params_keeps_only_explicit_keys():
    """没传的键**不能**出现在结果里 —— 否则"用户没动这一项"会变成"设成了默认值"。"""
    out = mineru_service.normalize_params({
        "mineru_model_version": "vlm",
        "mineru_is_ocr": "true",
        "mineru_page_ranges": "1-3",
        "mineru_language": None,
        "mineru_enable_table": "",
    })
    assert out == {"mineru_model_version": "vlm", "mineru_is_ocr": True,
                   "mineru_page_ranges": "1-3"}


def test_normalize_params_accepts_form_strings_for_bools():
    """multipart 表单里一切都是字符串，`"false"` 必须真的读成 False。"""
    out = mineru_service.normalize_params({
        "mineru_enable_formula": "false",
        "mineru_enable_table": "1",
    })
    assert out == {"mineru_enable_formula": False, "mineru_enable_table": True}


def test_normalize_params_rejects_garbage_bools_and_values():
    from backend.core.errors import ValidationError as VE

    with pytest.raises(VE):
        mineru_service.normalize_params({"mineru_is_ocr": "maybe"})
    with pytest.raises(VE):
        mineru_service.normalize_params({"mineru_model_version": "gpt-4o"})
    with pytest.raises(VE):
        mineru_service.normalize_params({"mineru_language": "zh-CN"})


def test_effective_params_prefers_task_params_then_settings(fake_settings):
    mineru_service.save_config({"model_version": "vlm", "language": "ch"})
    got = mineru_service.effective_params({"mineru_model_version": "pipeline",
                                           "mineru_page_ranges": "1-2"})
    assert got["model_version"] == "pipeline"        # 任务参数赢
    assert got["language"] == "ch"                   # 没传 → 设置默认值
    assert got["page_ranges"] == "1-2"
    assert got["enable_formula"] is True and got["enable_table"] is True
    assert got["is_ocr"] is False


def test_effective_params_falls_back_to_official_defaults(fake_settings):
    got = mineru_service.effective_params({})
    assert got == {"model_version": "pipeline", "language": "en",
                   "enable_formula": True, "enable_table": True,
                   "is_ocr": False, "page_ranges": ""}


def test_parse_online_forwards_every_param_to_the_client(fake_settings, monkeypatch,
                                                        tmp_path):
    """把参数一路钉到客户端调用：中间任何一处按默认值硬编码都会在这里现形。"""
    from backend.services.paper import paper_service as ps

    seen: dict = {}

    class FakeClient:
        def parse_pdf(self, pdf, dest, **kw):
            seen.update(kw)
            return {"batch_id": "B", "raw_dir": str(tmp_path), "content_list": None,
                    "model_version": kw["model_version"]}

    built: dict = {}

    def fake_build(pdf, paper_id, root, bundle, **kw):
        built.update(kw)
        return "RESULT"

    monkeypatch.setattr(mineru_service, "read_token", lambda: "tok")
    monkeypatch.setattr(mineru_service, "client", lambda token=None: FakeClient())
    monkeypatch.setattr(mineru_service, "build_from_bundle", fake_build)
    monkeypatch.setattr(ps, "get_paper", lambda pid: {"id": pid, "page_count": 5})
    monkeypatch.setattr(ps, "paper_paths", lambda pid: {"root": tmp_path})
    monkeypatch.setattr(ps, "resolve_pdf", lambda pid: tmp_path / "x.pdf")
    monkeypatch.setattr(mineru_service, "_clear_dir", lambda p: None)

    result, _bundle = mineru_service.parse_online("p1", params={
        "mineru_model_version": "vlm", "mineru_language": "ch",
        "mineru_is_ocr": True, "mineru_enable_formula": False,
        "mineru_enable_table": False, "mineru_page_ranges": "1-3",
        "figure_caption": True,
    })
    assert result == "RESULT"
    assert seen["model_version"] == "vlm" and seen["language"] == "ch"
    assert seen["is_ocr"] is True
    assert seen["enable_formula"] is False and seen["enable_table"] is False
    assert seen["page_ranges"] == "1-3"
    # 解析产物里要留痕：事后只能靠 IR 里的 params 回答"这篇按什么参数解析的"
    assert built["mineru_params"]["model_version"] == "vlm"
    assert built["mineru_params"]["page_ranges"] == "1-3"
    # 插图开关与在线参数互不干扰
    assert built["figure_caption"] is True


# --------------------------------------------------------------------------
# 3. 配置读写（引擎/模型版本/语言）
# --------------------------------------------------------------------------


def test_save_config_writes_and_reads_back(fake_settings):
    cfg = mineru_service.save_config({"engine_default": "mineru",
                                      "model_version": "vlm",
                                      "language": "ch"})
    assert cfg["engine_default"] == "mineru"
    assert cfg["model_version"] == "vlm"
    assert cfg["language"] == "ch"
    assert _store["mineru_engine_default"] == '"mineru"'


def test_save_config_only_touches_the_fields_given(fake_settings):
    mineru_service.save_config({"engine_default": "mineru"})
    mineru_service.save_config({"model_version": "vlm"})
    assert _store["mineru_engine_default"] == '"mineru"'
    assert _store["mineru_model_version"] == '"vlm"'
    assert "mineru_language" not in _store


@pytest.mark.parametrize("payload,needle", [
    ({"engine_default": "cloud"}, "解析引擎"),
    ({"model_version": "gpt-5"}, "模型版本"),
    ({"language": "zh-CN"}, "OCR 语言"),
])
def test_save_config_rejects_bad_values(fake_settings, payload, needle):
    with pytest.raises(ValidationError) as err:
        mineru_service.save_config(payload)
    assert needle in str(err.value)


def test_token_roundtrip_is_masked_but_stored_in_full(fake_settings):
    cfg = mineru_service.save_config({"token": "abcdefghijklmnop"})
    assert cfg["has_token"] is True and cfg["online_ready"] is True
    assert cfg["token_masked"] == "abcd…mnop"
    assert cfg["token_source"] == "file"
    assert fake_settings.mineru_token_file.read_text(encoding="utf-8") == "abcdefghijklmnop"


def test_token_with_whitespace_is_rejected(fake_settings):
    with pytest.raises(ValidationError):
        mineru_service.save_config({"token": "abc def"})


def test_clear_token_removes_the_file(fake_settings):
    mineru_service.save_config({"token": "abcdefghijklmnop"})
    cfg = mineru_service.save_config({"clear_token": True})
    assert cfg["has_token"] is False and cfg["token_masked"] is None
    assert not fake_settings.mineru_token_file.exists()


def test_verify_without_token_does_not_touch_the_network(fake_settings, monkeypatch):
    called = {"n": 0}

    class Boom:
        def verify(self):                       # pragma: no cover —— 不该被调到
            called["n"] += 1
            raise AssertionError("没有 token 时不该发请求")

    monkeypatch.setattr(mineru_service, "client", lambda token=None: Boom())
    res = mineru_service.verify()
    assert res["ok"] is False and res["state"] == "missing"
    assert called["n"] == 0
    assert _store["mineru_last_verify"]          # 结论记了一笔，设置页刷新后还能看到


def test_verify_records_the_result(fake_settings, monkeypatch):
    mineru_service.save_config({"token": "abcdefghijklmnop"})

    class Fake:
        def verify(self):
            return {"ok": True, "state": "valid", "message": "Token 有效", "latency_ms": 12}

    monkeypatch.setattr(mineru_service, "client", lambda token=None: Fake())
    res = mineru_service.verify()
    assert res["ok"] is True
    stored = json.loads(_store["mineru_last_verify"])
    assert stored["state"] == "valid" and stored["at"]


def test_effective_falls_back_to_settings_defaults(fake_settings):
    cfg = mineru_service.effective()
    assert cfg["engine_default"] == "local"
    assert cfg["model_version"] == "pipeline"
    assert cfg["language"] == "en"
    assert cfg["base_url"] == "https://mineru.net/api/v4"
    assert cfg["online_ready"] is False


def test_parse_options_engine_is_a_closed_set(monkeypatch, tmp_path):
    """IR 里存了脏引擎名（比如手改的 JSON）时回退默认，而不是原样传到分派端。"""
    from backend.services.paper import paper_service as ps

    ir = tmp_path / "ir.sqlite"
    import sqlite3

    conn = sqlite3.connect(str(ir))
    conn.execute("CREATE TABLE paper (paper_id TEXT PRIMARY KEY, stats_json TEXT)")
    conn.execute("INSERT INTO paper VALUES (?, ?)",
                 ("p1", json.dumps({"parse_options": {"engine": "cloud"}})))
    conn.commit()
    conn.close()
    monkeypatch.setattr(ps, "paper_paths", lambda pid: {"ir": ir})
    figure_regions.invalidate_cache()
    assert figure_regions.parse_options("p1")["engine"] == "local"


# --------------------------------------------------------------------------
# 3. 区域层的"精确来源"判据
# --------------------------------------------------------------------------


def test_precise_bbox_sources():
    online = {"audit_json": json.dumps({"source": "online"})}
    scan = {"audit_json": json.dumps({"source": "scan"})}
    local = {"audit_json": json.dumps({"issues": []})}
    assert figure_regions._has_precise_bbox(online) is True
    assert figure_regions._has_precise_bbox(scan) is True
    assert figure_regions._has_precise_bbox(local) is False
    assert figure_regions._has_precise_bbox({}) is False
    # 老名字（扫描件专用）语义不变，既有调用点/单测不受影响
    assert figure_regions._is_scan_region(scan) is True
    assert figure_regions._is_scan_region(online) is False


# --------------------------------------------------------------------------
# 4. 任务处理器分派
# --------------------------------------------------------------------------


def _ctx(params: dict):
    from backend.services.task.task_service import TaskContext

    return TaskContext(task_id="t", task_type="PARSE", queue="system",
                       paper_id="p1", params=params, session=None)


def test_handler_dispatches_to_the_online_engine(monkeypatch):
    from backend.services.task import handlers

    calls: list[str] = []
    monkeypatch.setattr(handlers, "_handle_parse_online",
                        lambda ctx: calls.append("mineru") or {"engine": "mineru"})
    monkeypatch.setattr(handlers, "_handle_parse_local",
                        lambda ctx: calls.append("local") or {"engine": "local"})
    assert handlers.handle_parse(_ctx({"engine": "mineru"}))["engine"] == "mineru"
    assert calls == ["mineru"]


def test_handler_defaults_to_the_local_engine(monkeypatch):
    from backend.services.task import handlers

    calls: list[str] = []
    monkeypatch.setattr(handlers, "_handle_parse_local",
                        lambda ctx: calls.append("local") or {"engine": "local"})
    monkeypatch.setattr(handlers, "_handle_parse_online",
                        lambda ctx: calls.append("mineru") or {"engine": "mineru"})
    # 没传 engine（老任务 / 手工建的 PARSE 任务）→ 本地解析，不能按在线跑
    assert handlers.handle_parse(_ctx({}))["engine"] == "local"
    assert handlers.handle_parse(_ctx({"engine": ""}))["engine"] == "local"
    assert handlers.handle_parse(_ctx({"engine": "MINERU"}))["engine"] == "mineru"
    assert calls == ["local", "local", "mineru"]


def test_online_progress_labels_cover_every_stage():
    from backend.services.task import handlers

    assert set(handlers.MINERU_STAGE_LABELS) == set(handlers.MINERU_STAGES)
    # 后四段与本地解析同名：两条引擎在任务面板上的"后半程"说法一致
    assert set(handlers.MINERU_STAGES) & set(handlers.PARSE_STAGES) == {
        "render", "structure", "persist"}


# --------------------------------------------------------------------------
# 5. HTTP 层接线（不起数据库：把 setting 入口换成字典）
# --------------------------------------------------------------------------


def test_mineru_api_endpoints(fake_settings, monkeypatch):
    """HTTP 接线：字段名/掩码/200-而非报错 的约定。

    不起 lifespan（不用数据库），也不发真请求：`client()` 换成替身。
    """
    from fastapi.testclient import TestClient

    import backend.main as main

    class FakeClient:
        def verify(self):
            return {"ok": True, "state": "valid", "http_status": 200, "code": -60012,
                    "message": "Token 有效", "latency_ms": 9,
                    "token_masked": "abcd…mnop"}

    monkeypatch.setattr(mineru_service, "client", lambda token=None: FakeClient())
    client = TestClient(main.app)

    got = client.get("/api/v1/mineru/config")
    assert got.status_code == 200
    body = got.json()["data"]
    assert body["engine_default"] == "local" and body["has_token"] is False
    assert body["model_versions"] == ["pipeline", "vlm"]
    assert any(x["value"] == "en" for x in body["languages"])

    saved = client.put("/api/v1/mineru/config",
                       json={"token": "abcdefghijklmnop",
                             "engine_default": "mineru"})
    assert saved.status_code == 200
    data = saved.json()["data"]
    assert data["has_token"] is True and data["engine_default"] == "mineru"
    assert data["token_masked"] == "abcd…mnop"
    # token 本体**不进响应**（只回掩码）
    assert "abcdefghijklmnop" not in saved.text

    verified = client.post("/api/v1/mineru/verify", json={})
    assert verified.status_code == 200
    # 结论走 data.ok（HTTP 仍是 200）：前端要能同时拿到结论 + 原因 + 延迟
    assert verified.json()["data"]["ok"] is True
    assert "有效" in verified.json()["data"]["message"]

    bad = client.put("/api/v1/mineru/config", json={"engine_default": "cloud"})
    assert bad.status_code == 400
