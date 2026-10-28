"""站外图片代理 + 磁盘缓存（**全离线**：DNS 与传输层都换掉，不发任何真实请求）。

这一组钉住四件事，任何一件失效都会变成安全事故或者"图全裂"：

1. **缓存**：同一个地址只抓一次，第二次必须命中磁盘（FakeFetcher.calls 不再变长）；
2. **SSRF**：私网 / 回环 / 云元数据地址，以及**重定向到**这些地址，一律拒 —— 而且拒在
   抓取**之前**（calls 为空），不能"先连上去再判断"；
3. **类型**：Content-Type 不是 image/* 拒；声明对了但**魔数**不对也拒；
4. **失败分得清**：https / 私网 / 非图片 / 超大 / 超时各有各的异常（前端看到的是不同的一句人话）。
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import time
from pathlib import Path
from urllib.parse import quote

import pytest

from backend.core.config import Settings
from backend.services import webimg as w

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
JPG = b"\xff\xd8\xff\xe0" + b"\x00" * 64
SVG = (b'<?xml version="1.0"?>' +
       b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>')
REMOTE = "https://cdn.example.com/a.png"


# --------------------------------------------------------------------------
# 替身：配置目录、DNS、传输层
# --------------------------------------------------------------------------


@pytest.fixture
def settings(tmp_path, monkeypatch):
    """Settings 指向临时目录（缓存只写临时文件）；启动容量检查标记成"已做过"。"""
    s = Settings(data_dir=tmp_path / "data", secrets_dir=tmp_path / "secrets")
    monkeypatch.setattr(w, "get_settings", lambda: s)
    monkeypatch.setattr(w, "_pruned_once", True)
    return s


@pytest.fixture
def resolver(monkeypatch):
    """替掉 w._resolve：默认所有域名都解析到回环（**未知域名一律当危险**）。"""
    table = {
        "cdn.example.com": ["93.184.216.34"],
        "img.example.com": ["93.184.216.34"],
        "evil.example.com": ["10.0.0.5"],
    }

    def fake(host: str) -> list[str]:
        return table.get(host, ["127.0.0.1"])

    monkeypatch.setattr(w, "_resolve", fake)
    return table


class FakeFetcher:
    """替掉 w._fetcher()：按 url 回放预置响应，并记下每一次真实抓取。"""

    def __init__(self, routes: dict[str, object]) -> None:
        self.routes = routes
        self.calls: list[str] = []

    def get(self, url: str, *, timeout: float, max_bytes: int) -> object:
        self.calls.append(url)
        if url not in self.routes:
            raise AssertionError("测试没有预置这次抓取：" + url)
        value = self.routes[url]
        if isinstance(value, BaseException):    # 预置的异常照样"抛出去"
            raise value
        return value


@pytest.fixture
def fetcher(monkeypatch):
    def install(routes: dict[str, object]) -> FakeFetcher:
        fake = FakeFetcher(routes)
        monkeypatch.setattr(w, "_fetcher", lambda: fake)
        return fake

    return install


def png_response() -> w.RawResponse:
    return w.RawResponse(200, {"content-type": "image/png"}, PNG)


# --------------------------------------------------------------------------
# ① 正常抓取 + 落盘 + 缓存命中
# --------------------------------------------------------------------------


def test_fetches_https_image_once_then_serves_from_disk(settings, resolver, fetcher):
    fake = fetcher({REMOTE: png_response()})

    first = w.local_path(REMOTE)
    assert first.is_file()
    assert first.read_bytes() == PNG
    assert first.parent == settings.webimg_cache_dir
    # 文件名 = sha256(url) + 按**魔数**定的扩展名（不信上游声明）
    assert first.name == hashlib.sha256(REMOTE.encode("utf-8")).hexdigest() + ".png"
    assert fake.calls == [REMOTE]

    second = w.local_path(REMOTE)
    assert second == first
    # ② 命中缓存：**一个请求都没再发**
    assert fake.calls == [REMOTE]


def test_cache_dir_follows_data_dir_not_hardcoded(settings):
    assert settings.webimg_cache_dir == settings.data_dir / "cache" / "webimg"
    assert w.cache_dir() == settings.webimg_cache_dir


def test_extensions_come_from_magic_bytes(settings, resolver, fetcher):
    fetcher({"https://cdn.example.com/b.jpg": w.RawResponse(
        200, {"content-type": "image/jpeg"}, JPG)})
    assert w.local_path("https://cdn.example.com/b.jpg").suffix == ".jpg"


def test_svg_is_accepted_only_when_declared(settings, resolver, fetcher):
    fetcher({"https://cdn.example.com/c.svg": w.RawResponse(
        200, {"content-type": "image/svg+xml"}, SVG)})
    path = w.local_path("https://cdn.example.com/c.svg")
    assert path.suffix == ".svg"
    assert w.media_type_of(path) == "image/svg+xml"


# --------------------------------------------------------------------------
# ② SSRF：私网 / 回环 / 元数据 / 重定向
# --------------------------------------------------------------------------


@pytest.mark.parametrize("host", [
    "127.0.0.1", "127.9.9.9", "10.1.2.3", "172.16.0.5", "172.31.255.254",
    "192.168.1.9", "169.254.169.254", "0.0.0.0", "[::1]", "[fd00::1]", "[fe80::1]",
])
def test_rejects_private_loopback_and_metadata(settings, resolver, fetcher, host):
    fake = fetcher({})
    with pytest.raises(w.WebImagePrivateAddress):
        w.local_path("https://" + host + "/x.png")
    # 关键：拒在**抓取之前**（不是"连上了再判断"）
    assert fake.calls == []


def test_rejects_hostname_that_resolves_to_a_private_address(settings, resolver, fetcher):
    fake = fetcher({})
    with pytest.raises(w.WebImagePrivateAddress):
        w.local_path("https://evil.example.com/x.png")
    assert fake.calls == []


def test_rejects_redirect_to_a_private_address(settings, resolver, fetcher):
    """第一跳是合法公网地址，302 把我们引到内网 —— 每一跳都重新校验才拦得住。"""
    fake = fetcher({
        "https://cdn.example.com/r.png": w.RawResponse(
            302, {"location": "https://evil.example.com/x.png"}),
    })
    with pytest.raises(w.WebImagePrivateAddress):
        w.local_path("https://cdn.example.com/r.png")
    # 第二跳**根本没发出去**
    assert fake.calls == ["https://cdn.example.com/r.png"]


def test_rejects_redirect_to_the_metadata_endpoint(settings, resolver, fetcher):
    fake = fetcher({
        "https://cdn.example.com/r2.png": w.RawResponse(
            301, {"location": "https://169.254.169.254/latest/meta-data/"}),
    })
    with pytest.raises(w.WebImagePrivateAddress):
        w.local_path("https://cdn.example.com/r2.png")
    assert fake.calls == ["https://cdn.example.com/r2.png"]


def test_follows_at_most_three_redirects(settings, resolver, fetcher):
    fake = fetcher({
        "https://cdn.example.com/a1.png": w.RawResponse(
            302, {"location": "https://cdn.example.com/a2.png"}),
        "https://cdn.example.com/a2.png": w.RawResponse(
            302, {"location": "https://cdn.example.com/a3.png"}),
        "https://cdn.example.com/a3.png": w.RawResponse(
            302, {"location": "https://cdn.example.com/a4.png"}),
        "https://cdn.example.com/a4.png": w.RawResponse(
            302, {"location": "https://cdn.example.com/a5.png"}),
    })
    with pytest.raises(w.WebImageUnreachable):
        w.local_path("https://cdn.example.com/a1.png")
    assert fake.calls == [
        "https://cdn.example.com/a1.png",
        "https://cdn.example.com/a2.png",
        "https://cdn.example.com/a3.png",
        "https://cdn.example.com/a4.png",
    ]


def test_accepts_three_redirects(settings, resolver, fetcher):
    fake = fetcher({
        "https://cdn.example.com/r3.png": w.RawResponse(
            302, {"location": "/r4.png"}),
        "https://cdn.example.com/r4.png": w.RawResponse(
            307, {"location": "https://img.example.com/r5.png"}),
        "https://img.example.com/r5.png": png_response(),
    })
    path = w.local_path("https://cdn.example.com/r3.png")
    assert path.read_bytes() == PNG
    assert len(fake.calls) == 3


# --------------------------------------------------------------------------
# ③ 类型 / 体量 / scheme
# --------------------------------------------------------------------------


def test_rejects_http_scheme(settings, resolver, fetcher):
    fake = fetcher({})
    with pytest.raises(w.WebImageNotHttps):
        w.local_path("http://cdn.example.com/a.png")
    assert fake.calls == []


def test_rejects_non_image_content_type(settings, resolver, fetcher):
    fetcher({REMOTE: w.RawResponse(200, {"content-type": "text/html"},
                                  b"<html>nope</html>")})
    with pytest.raises(w.WebImageNotImage):
        w.local_path(REMOTE)


def test_rejects_content_type_that_lies_about_the_bytes(settings, resolver, fetcher):
    """声明 image/png、字节是 HTML —— 只信响应头的实现在这里就漏了。"""
    fetcher({REMOTE: w.RawResponse(200, {"content-type": "image/png"},
                                  b"<html>not a png</html>")})
    with pytest.raises(w.WebImageNotImage):
        w.local_path(REMOTE)


def test_rejects_oversized_body(settings, resolver, fetcher, monkeypatch):
    monkeypatch.setattr(w, "MAX_BYTES", 32)
    fetcher({REMOTE: png_response()})
    with pytest.raises(w.WebImageTooLarge):
        w.local_path(REMOTE)


def test_rejects_timeout(settings, resolver, fetcher):
    fake = fetcher({REMOTE: w.WebImageTimeout("抓取图片超时（8 秒），这次先不显示这张图。")})
    with pytest.raises(w.WebImageTimeout):
        w.local_path(REMOTE)
    assert fake.calls == [REMOTE]


def test_each_failure_has_its_own_reason_and_status(settings):
    """失败要**分得清**：reason 与 HTTP 状态码都不一样。"""
    pairs = {
        w.WebImageNotHttps: ("not-https", 400),
        w.WebImagePrivateAddress: ("private-address", 400),
        w.WebImageNotImage: ("not-image", 415),
        w.WebImageTooLarge: ("too-large", 413),
        w.WebImageTimeout: ("timeout", 504),
        w.WebImageUnreachable: ("unreachable", 502),
    }
    for cls, (reason, status) in pairs.items():
        exc = cls("一句话")
        assert exc.reason == reason and exc.http_status == status
        assert exc.to_dict()["detail"]["reason"] == reason


# --------------------------------------------------------------------------
# ④ 容量控制 / 代理地址拼法 / 路由响应头
# --------------------------------------------------------------------------


def test_prune_deletes_the_oldest_files_first(settings):
    root = w.cache_dir()
    old = root / ("a" * 64 + ".png")
    new = root / ("b" * 64 + ".png")
    old.write_bytes(b"x" * 100)
    new.write_bytes(b"x" * 100)
    os.utime(old, (time.time() - 1000, time.time() - 1000))
    removed = w.prune(max_bytes=150)
    assert removed == [old.name]
    assert not old.exists() and new.exists()


def test_prune_is_a_noop_under_the_limit(settings):
    root = w.cache_dir()
    (root / ("c" * 64 + ".png")).write_bytes(b"x" * 10)
    assert w.prune(max_bytes=1024) == []


def test_proxy_path_encodes_the_whole_url(settings):
    url = "https://cdn.example.com/a b.png?x=1&y=2"
    out = w.proxy_path(url)
    assert out == "/api/v1/webimg?url=" + quote(url, safe="")
    # 空格 / & / 括号全被转义，塞进 markdown 的 () 里不会撑破
    assert " " not in out and "&y=" not in out


def test_route_serves_the_cached_file_with_hard_cache_headers(settings, resolver, fetcher):
    fetcher({REMOTE: png_response()})
    from backend.api.webimg import serve_webimg

    response = asyncio.run(serve_webimg(REMOTE))
    assert Path(response.path) == w.local_path(REMOTE)
    assert response.headers["cache-control"] == "public, max-age=604800, immutable"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.media_type == "image/png"


def test_route_gives_svg_a_sandbox_csp(settings, resolver, fetcher):
    fetcher({"https://cdn.example.com/c.svg": w.RawResponse(
        200, {"content-type": "image/svg+xml"}, SVG)})
    from backend.api.webimg import serve_webimg

    response = asyncio.run(serve_webimg("https://cdn.example.com/c.svg"))
    assert "sandbox" in response.headers["content-security-policy"]
    assert response.headers["x-content-type-options"] == "nosniff"
