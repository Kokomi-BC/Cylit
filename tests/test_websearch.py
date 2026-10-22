"""联网搜索（DeepSeek 官方 Search / Fetch）的配置、密钥与调用口径（**全离线**）。

为什么要这一组测试：这条链路两头都可能静默失效 ——

1. 密钥侧：一旦某处把明文回了前端或写进日志，问题不会报错，只会等到某天泄露；
2. 参数侧：`count` / `fetch_chars` 这类"写在一个地方、读在另一个地方"的默认值，
   两处不一致就是"界面改了、行为没变"（本项目为同类问题吃过一次亏）；
3. 错误侧：上游 401/429/504 如果不翻译成人话，模型和用户都只会看到一句 HTTP 状态码。

所以这里把"掩码不出明文 / 边界值被拒 / 参数真的透传 / 状态码有对应人话"逐条钉住。
网络全部由 `FakeTransport` 顶替：测试**不发真实请求**，也不需要任何密钥。
"""

from __future__ import annotations

import asyncio
import json

import pytest

from backend.core.config import Settings
from backend.core.errors import ValidationError
from backend.services.agent import websearch as ws

PLAIN_KEY = "sk-deepseek-search-9f2c4d7e1b8a"


class FakeTransport:
    """替掉 `ws._transport()`：记下请求、回一个人造响应。"""

    def __init__(self, status: int = 200, body: dict | None = None,
                 exc: BaseException | None = None) -> None:
        self.status = status
        self.body = body if body is not None else {"results": []}
        self.exc = exc
        self.calls: list[dict] = []

    def post_json(self, url, body, headers, timeout):
        self.calls.append({"url": url, "body": body, "headers": headers,
                           "timeout": timeout})
        if self.exc is not None:
            raise self.exc
        return self.status, self.body


_store: dict = {}


@pytest.fixture
def fake_settings(tmp_path, monkeypatch):
    """Settings 指向临时目录（密钥只写临时文件），两个数据库口换成字典。

    与 `test_mineru_engine.py` 同一套替身：`_stored` 必须把 JSON 文本解回来，
    否则读到的是带引号的形态，与线上不一致。
    """
    _store.clear()
    s = Settings(secrets_dir=tmp_path / "secrets", data_dir=tmp_path / "data")
    s.ensure_dirs()
    monkeypatch.setattr(ws, "get_settings", lambda: s)
    monkeypatch.setattr(ws, "_stored",
                        lambda: {k: json.loads(v) for k, v in _store.items()})
    monkeypatch.setattr(ws, "_write_setting",
                        lambda key, value: _store.__setitem__(key, json.dumps(value)))
    monkeypatch.delenv(ws.KEY_ENV, raising=False)
    return s


@pytest.fixture
def fake_http(monkeypatch):
    """装一个假传输层并返回它（调用点随时能换 status/body/exc）。"""
    def install(status: int = 200, body: dict | None = None,
                exc: BaseException | None = None) -> FakeTransport:
        transport = FakeTransport(status, body, exc)
        monkeypatch.setattr(ws, "_transport", lambda: transport)
        return transport
    return install


def _with_key(fake_settings) -> None:
    ws.write_key(PLAIN_KEY)


# --------------------------------------------------------------------------
# 1. 配置与掩码：只进不出
# --------------------------------------------------------------------------


def test_defaults_when_nothing_is_saved(fake_settings):
    cfg = ws.public_config()
    assert cfg["provider"] == "deepseek"
    assert cfg["count"] == 8
    assert cfg["need_content"] is False
    assert cfg["fetch_chars"] == 6000
    assert cfg["has_key"] is False and cfg["ready"] is False
    assert cfg["key_masked"] == ""
    assert cfg["providers"] == [{"id": "deepseek", "label": "DeepSeek 搜索"}]


def test_saved_key_is_masked_and_never_echoed(fake_settings):
    cfg = ws.save_config({"api_key": PLAIN_KEY})
    assert cfg["has_key"] is True and cfg["ready"] is True
    assert cfg["key_masked"] == "sk-d…1b8a"
    assert PLAIN_KEY not in json.dumps(cfg, ensure_ascii=False)
    assert cfg["applied"] == {"api_key": "saved"}


def test_key_lands_in_the_secret_file(fake_settings):
    ws.save_config({"api_key": PLAIN_KEY})
    path = fake_settings.secrets_dir / "websearch.key"
    assert path.is_file()
    assert path.read_text(encoding="utf-8") == PLAIN_KEY
    # 密钥文件必须是唯一落点：不得同时写进 setting 表
    assert all("sk-deepseek" not in str(v) for v in _store.values())


def test_empty_api_key_means_do_not_touch_it(fake_settings):
    ws.save_config({"api_key": PLAIN_KEY})
    cfg = ws.save_config({"api_key": "", "count": 5})
    assert cfg["applied"] == {"websearch_count": 5}
    assert ws.read_key() == PLAIN_KEY


def test_clear_key_removes_the_file(fake_settings):
    ws.save_config({"api_key": PLAIN_KEY})
    cfg = ws.save_config({"clear_key": True})
    assert cfg["applied"] == {"api_key": "cleared"}
    assert cfg["has_key"] is False
    assert not (fake_settings.secrets_dir / "websearch.key").exists()


def test_env_var_is_the_fallback(fake_settings, monkeypatch):
    monkeypatch.setenv(ws.KEY_ENV, "env-key-1234567890")
    cfg = ws.public_config()
    assert cfg["has_key"] is True
    assert ws.key_source() == "env"
    assert cfg["key_masked"] == "env-…7890"


def test_non_secret_settings_round_trip(fake_settings):
    cfg = ws.save_config({"provider": "deepseek", "count": 3,
                          "need_content": True, "fetch_chars": 1200})
    assert (cfg["count"], cfg["need_content"], cfg["fetch_chars"]) == (3, True, 1200)
    # 读取点只有 effective()：换个调用者（搜索）也必须是同一份值
    assert ws.effective() == {"provider": "deepseek", "count": 3,
                              "need_content": True, "fetch_chars": 1200}


def test_key_with_spaces_is_rejected(fake_settings):
    with pytest.raises(ValidationError, match="空格"):
        ws.save_config({"api_key": "sk-abc def"})


def test_plaintext_key_never_reaches_the_log(fake_settings, caplog):
    with caplog.at_level("INFO"):
        ws.save_config({"api_key": PLAIN_KEY})
    assert PLAIN_KEY not in caplog.text
    assert "sk-d…1b8a" in caplog.text


# --------------------------------------------------------------------------
# 2. 边界值
# --------------------------------------------------------------------------


@pytest.mark.parametrize("value,want", [(1, 1), (50, 50), ("8", 8), (10, 10)])
def test_count_accepts_the_legal_range(value, want):
    assert ws.norm_count(value) == want


@pytest.mark.parametrize("value", [0, 51, -1, "abc", "", None])
def test_count_rejects_out_of_range(value):
    with pytest.raises(ValidationError):
        ws.norm_count(value)


def test_saved_count_out_of_range_is_rejected_and_not_written(fake_settings):
    with pytest.raises(ValidationError, match="1~50"):
        ws.save_config({"count": 51})
    assert "websearch_count" not in _store


@pytest.mark.parametrize("value,want", [(500, 500), (20000, 20000), ("6000", 6000)])
def test_fetch_chars_accepts_the_legal_range(value, want):
    assert ws.norm_fetch_chars(value) == want


@pytest.mark.parametrize("value", [0, 499, 20001, "很多", None])
def test_fetch_chars_rejects_out_of_range(value):
    with pytest.raises(ValidationError):
        ws.norm_fetch_chars(value)


@pytest.mark.parametrize("value", ["bing", "", "DEEPSEEK-V2"])
def test_unknown_provider_is_rejected(value):
    """只注册了 deepseek：别的提供方没有同一套 search/fetch 语义与错误映射，先拒。"""
    with pytest.raises(ValidationError):
        ws.save_config({"provider": value})
    assert "websearch_provider" not in _store


def test_provider_value_is_normalized(fake_settings):
    assert ws.save_config({"provider": " DeepSeek "})["provider"] == "deepseek"


@pytest.mark.parametrize("value,want", [(True, True), ("false", False), (0, False),
                                        ("yes", True)])
def test_need_content_parses_the_common_forms(value, want):
    assert ws.norm_bool(value, field="need_content") is want


def test_need_content_rejects_garbage():
    with pytest.raises(ValidationError, match="true / false"):
        ws.norm_bool("maybe", field="need_content")


# --------------------------------------------------------------------------
# 3. search：参数透传 + 错误映射
# --------------------------------------------------------------------------


def _results(*titles: str) -> dict:
    return {"results": [
        {"rerank_score": 2.4, "title": t, "url": f"https://e.com/{i}",
         "snippet": "短摘要", "summary": "长摘要", "content": "正文",
         "site_name": "Example News", "date": "2026-06-01T00:00:00+00:00",
         "authority": 3}
        for i, t in enumerate(titles)]}


def test_search_passes_query_and_flags_through(fake_settings, fake_http):
    _with_key(fake_settings)
    transport = fake_http(200, _results("第一条"))
    out = ws.search("random rotation", count=3, need_content=True,
                    ori_question="什么是对称随机旋转？")

    call = transport.calls[0]
    assert call["url"] == ws.PROVIDERS["deepseek"]["search_url"]
    assert call["body"] == {"query": "random rotation", "count": 3,
                            "need_content": True,
                            "ori_question": "什么是对称随机旋转？"}
    assert call["headers"]["Authorization"] == f"Bearer {PLAIN_KEY}"
    assert out["provider"] == "deepseek"
    assert out["query"] == "random rotation"
    assert out["results"][0]["title"] == "第一条"
    assert out["results"][0]["site_name"] == "Example News"
    assert set(out) == {"provider", "query", "results"}
    assert set(out["results"][0]) == {"title", "url", "snippet", "summary", "content",
                                      "site_name", "date", "authority", "rerank_score"}


def test_search_falls_back_to_the_saved_settings(fake_settings, fake_http):
    _with_key(fake_settings)
    ws.save_config({"count": 5, "need_content": False})
    transport = fake_http(200, _results("x"))
    ws.search("turboquant")
    body = transport.calls[0]["body"]
    assert body["count"] == 5 and body["need_content"] is False
    # 没给 ori_question / localization 时不该出现在请求体里（"没动这一项"≠"设成空"）
    assert "ori_question" not in body and "localization" not in body


def test_search_without_key_says_what_to_do(fake_settings, fake_http):
    transport = fake_http(200, _results("不该被调用"))
    with pytest.raises(ws.WebSearchAuthError) as err:
        ws.search("turboquant")
    assert "还没配置" in err.value.message and "websearch.key" in err.value.message
    assert transport.calls == []          # 没密钥就不要去打网络


def test_empty_query_is_rejected(fake_settings, fake_http):
    _with_key(fake_settings)
    fake_http(200, _results("x"))
    with pytest.raises(ValidationError, match="搜索词"):
        ws.search("   ")


@pytest.mark.parametrize("status,exc_type,needle", [
    (401, ws.WebSearchAuthError, "密钥无效或已过期"),
    (403, ws.WebSearchAuthError, "密钥无效或已过期"),
    (402, ws.WebSearchQuotaError, "余额"),
    (429, ws.WebSearchQuotaError, "频率"),
    (408, ws.WebSearchTimeoutError, "超时"),
    (504, ws.WebSearchTimeoutError, "超时"),
    (413, ws.WebSearchError, "请求体"),
    (404, ws.WebSearchError, "抓不到"),
    (500, ws.WebSearchError, "内部错误"),
    (418, ws.WebSearchError, "HTTP 418"),
])
def test_status_codes_map_to_human_messages(fake_settings, fake_http,
                                            status, exc_type, needle):
    _with_key(fake_settings)
    fake_http(status, {"error": {"message": "'count' must be between 1 and 50",
                                 "type": "invalid_request_error", "param": None}})
    with pytest.raises(exc_type) as err:
        ws.search("turboquant")
    assert needle in err.value.message
    # 上游原文必须一起带上，绝不吞
    assert "must be between 1 and 50" in err.value.message


def test_network_timeout_becomes_human(fake_settings, fake_http):
    _with_key(fake_settings)
    fake_http(exc=TimeoutError("connect timeout"))
    with pytest.raises(ws.WebSearchTimeoutError, match="超时"):
        ws.search("turboquant")


def test_unknown_transport_error_is_not_swallowed(fake_settings, fake_http):
    _with_key(fake_settings)
    fake_http(exc=RuntimeError("boom"))
    with pytest.raises(ws.WebSearchError, match="boom"):
        ws.search("turboquant")


# --------------------------------------------------------------------------
# 4. fetch：截断与地址校验
# --------------------------------------------------------------------------


def test_fetch_truncates_by_the_saved_limit(fake_settings, fake_http):
    _with_key(fake_settings)
    ws.save_config({"fetch_chars": 500})
    fake_http(200, {"title": "长文", "content": "字" * 900})
    out = ws.fetch("https://e.com/a")
    assert out["title"] == "长文"
    assert len(out["content"]) == 500
    assert out["truncated"] is True


def test_fetch_without_overflow_is_not_truncated(fake_settings, fake_http):
    _with_key(fake_settings)
    fake_http(200, {"title": "短文", "content": "只有一点点"})
    out = ws.fetch("https://e.com/a")
    assert out["content"] == "只有一点点"
    assert out["truncated"] is False
    assert set(out) == {"url", "title", "content", "truncated"}


def test_fetch_max_chars_overrides_the_setting(fake_settings, fake_http):
    _with_key(fake_settings)
    transport = fake_http(200, {"title": "t", "content": "x" * 900})
    out = ws.fetch("https://e.com/a", max_chars=600)
    assert len(out["content"]) == 600 and out["truncated"] is True
    assert transport.calls[0]["body"] == {"url": "https://e.com/a"}


@pytest.mark.parametrize("url", ["", "ftp://e.com/a", "e.com/a",
                                 "https://user:pw@e.com/a", "javascript:alert(1)"])
def test_fetch_rejects_bad_urls(fake_settings, fake_http, url):
    _with_key(fake_settings)
    transport = fake_http(200, {"title": "t", "content": "x"})
    with pytest.raises(ValidationError):
        ws.fetch(url)
    assert transport.calls == []


# --------------------------------------------------------------------------
# 5. verify：HTTP 一律 200，结论在 ok/state 里
# --------------------------------------------------------------------------


def test_verify_without_key_is_no_key(fake_settings, fake_http):
    transport = fake_http(200, _results("x"))
    out = ws.verify()
    assert out["ok"] is False and out["state"] == "no_key"
    assert out["ms"] == 0
    assert transport.calls == []


def test_verify_runs_one_real_search(fake_settings, fake_http):
    _with_key(fake_settings)
    transport = fake_http(200, _results("a", "b"))
    out = ws.verify()
    assert out["ok"] is True and out["state"] == "ok"
    assert isinstance(out["ms"], int)
    assert transport.calls[0]["body"]["count"] == 1
    assert set(out) == {"ok", "state", "detail", "ms"}


@pytest.mark.parametrize("status,state", [(401, "auth"), (429, "quota"),
                                          (504, "timeout"), (500, "error")])
def test_verify_maps_failures_to_states(fake_settings, fake_http, status, state):
    _with_key(fake_settings)
    fake_http(status, {"error": {"message": "上游说明"}})
    out = ws.verify()
    assert out["ok"] is False and out["state"] == state
    assert "上游说明" in out["detail"]


# --------------------------------------------------------------------------
# 6. 接口与工具注册（契约形状，防止路由改名/工具漏挂）
# --------------------------------------------------------------------------


def test_both_web_tools_carry_no_second_default(fake_settings, fake_http):
    """工具的 `count` / `need_content` / `max_chars` 默认值必须全是 None。

    填一个具体值就是第二处默认值：主后端设置里那一项从此再也不可能被读到
    （"界面上能改、代码里没人读"，本仓库明确不允许的状态）。
    """
    import inspect

    from backend.agent import tools as agent_tools

    for func, names in ((agent_tools.web_search, ("count", "need_content")),
                        (agent_tools.web_fetch, ("max_chars",))):
        params = inspect.signature(func).parameters
        for name in names:
            assert params[name].default is None, f"{func.__name__}.{name} 不该有默认值"


def test_routes_are_registered():
    from backend.api import agent as agent_api

    routes = {(r.path, m) for r in agent_api.router.routes
              for m in getattr(r, "methods", set())}
    assert ("/agent/websearch/config", "GET") in routes
    assert ("/agent/websearch/config", "PUT") in routes
    assert ("/agent/websearch/verify", "POST") in routes
    assert ("/agent/websearch/search", "POST") in routes
    assert ("/agent/websearch/fetch", "POST") in routes


def test_both_tools_are_always_mounted():
    from backend.agent import tools as agent_tools

    names = [t.__name__ for t in agent_tools.AGENT_TOOLS]
    assert "web_search" in names and "web_fetch" in names


def test_skill_name_is_legal_and_documented(project_root):
    """自带技能是启动时物化的：名字必须过 `SKILL_NAME_RE`，frontmatter 必须只有两项。"""
    from backend.services.agent.skills_store import SKILL_NAME_RE, parse_frontmatter

    md = project_root / "backend" / "agent" / "skills" / "web-search" / "SKILL.md"
    name, description = parse_frontmatter(md.read_text(encoding="utf-8"))
    assert name == "web-search"
    assert SKILL_NAME_RE.match(name)
    assert description.startswith("需要论文之外的公开信息")

# --------------------------------------------------------------------------
# 7. 工具层：need_content 的三态必须原样透传（主后端的设置才有机会生效）
# --------------------------------------------------------------------------


class FakeHost:
    """记录工具传了什么，再按 host.py 的同一条规则（`v not in (None, "")`）滤成 payload。

    那条过滤就是被测的一环：`None` 不进 payload，主后端 `effective()` 才有机会回落成
    设置里的值。顺带照官方口径模拟"need_content=false 时 content 恒为空串"。
    """

    def __init__(self, results: list[dict]) -> None:
        self.results = results
        self.received: list[dict] = []
        self.payloads: list[dict] = []

    async def web_search(self, query, *, count=None, need_content=None,
                         ori_question=""):
        self.received.append({"query": query, "count": count,
                              "need_content": need_content,
                              "ori_question": ori_question})
        self.payloads.append({
            k: v for k, v in (("query", query), ("count", count),
                              ("need_content", need_content),
                              ("ori_question", ori_question))
            if v not in (None, "")})
        results = []
        for raw in self.results:
            item = dict(raw)
            if need_content is False:
                item["content"] = ""
            results.append(item)
        return {"provider": "deepseek", "query": query, "results": results}


class FakeDeps:
    """工具只碰到 deps 的这三样（host / query_cache / spend_search+note_tool）。"""

    def __init__(self, host) -> None:
        self.host = host
        self.query_cache: dict = {}

    def spend_search(self) -> bool:
        return True

    def note_tool(self, *args, **kwargs) -> None:
        return None


class _Ctx:
    def __init__(self, deps) -> None:
        self.deps = deps


def _call_web_search(ctx, **kwargs):
    from backend.agent import tools as agent_tools

    return asyncio.run(agent_tools.web_search(ctx, "turboquant", **kwargs))


def _run_web_search(host, **kwargs):
    """一次调用 = 一个全新的 deps（缓存本来就是**单轮**作用域）。"""
    return _call_web_search(_Ctx(FakeDeps(host)), **kwargs)


def test_tool_leaves_need_content_unset_so_the_setting_wins():
    host = FakeHost(_results("第一条")["results"])
    out = _run_web_search(host)
    assert host.received[0]["need_content"] is None
    assert "need_content" not in host.payloads[0]
    # 服务端按设置回了正文：工具必须带出来，不能因为"我没传"就丢掉
    assert out["items"][0]["content"]


@pytest.mark.parametrize("flag,has_content", [(False, False), (True, True)])
def test_tool_passes_an_explicit_flag_through(flag, has_content):
    host = FakeHost(_results("第一条")["results"])
    out = _run_web_search(host, need_content=flag)
    assert host.payloads[0]["need_content"] is flag
    assert ("content" in out["items"][0]) is has_content


def test_cache_does_not_mix_the_three_need_content_states():
    host = FakeHost(_results("第一条")["results"])
    ctx = _Ctx(FakeDeps(host))                            # 同一轮：共用一份缓存
    without = _call_web_search(ctx, need_content=False)   # 显式不要正文
    default = _call_web_search(ctx)                       # 不传 → 由设置决定（这里给正文）
    assert len(host.received) == 2                        # 三态不同 → 不能命中对方的缓存
    assert "content" not in without["items"][0]
    assert default["items"][0]["content"]
    # 同一种调用重复一次仍然是缓存（不白扣预算、不再打后端）
    _call_web_search(ctx)
    assert len(host.received) == 2
