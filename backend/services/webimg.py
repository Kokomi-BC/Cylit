"""站外图片落地：抓一次、存盘、之后由本站 /api/v1/webimg 提供。

为什么要有这个模块（2026-12 收尾）：

- 以前回答里的站外图片是**浏览器直连第三方**：用户 IP / referer 泄露给那个图片站，
  而且第三方一挂，回答里的图就全裂；
- 流式期间原始图片 markdown 还会先推一次给前端（看着像"URL 闪了一下"）。

现在改成：清洗阶段把站外 https 地址改写成 /api/v1/webimg?url=… （见
agent/sanitize.strip_foreign_images），浏览器只跟本站打交道；本站第一次拿到请求时
抓一次落盘，之后命中磁盘缓存。

**SSRF 防线（五层，缺一层都能绕）**

1. 只认 https（http 在清洗阶段就降级成链接了，根本走不到这里）；
2. 抓取前**解析 DNS 并拒私网 / 回环 / 链路本地**，且**每一跳重定向都重新校验** ——
   否则 https://good.com/x 一个 302 就能把我们的请求打到 127.0.0.1（经典 SSRF）；
3. 单次抓取超时 8s、响应体上限 8MB（**流式累计**，超了立刻断开，不把内存吃满）；
4. Content-Type 必须是 image/* **且**与**魔数**对得上（png/jpeg/gif/webp/svg），
   光信响应头等于让攻击者用一句 image/png 拿到任意字节；
5. 落盘后一律以固定 MIME + X-Content-Type-Options: nosniff 返回；SVG 再加
   Content-Security-Policy: sandbox（直接打开那个地址时脚本也执行不了）。

已知不完美（写在代码里而不是留给下一个人猜）：DNS 解析与实际连接是两次动作
（TOCTOU），彻底固定 IP 要自建 transport 并手动处理 SNI / Host；这里用"每一跳重新校验
+ 超时 + 体积上限 + 只回图片字节"把可利用面压到很小。见交付说明第 ⑦ 条。
"""

from __future__ import annotations

import hashlib
import ipaddress
import os
import socket
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from urllib.parse import quote, urljoin, urlsplit

from backend.core.config import get_settings
from backend.core.errors import PaperMindError
from backend.core.logging import get_logger

log = get_logger(__name__)

#: 单次抓取的超时（秒）。图片是"顺带看的东西"，8s 拿不到就当它没写。
TIMEOUT_SECONDS = 8.0
#: 单张图上限（字节）。超过就当它不是配图（多半是下载链接或者攻击载荷）。
MAX_BYTES = 8 * 1024 * 1024
#: 最多跟随几跳重定向（每一跳都重新做 URL + DNS 校验）
MAX_REDIRECTS = 3
#: 缓存目录容量上限（字节）；超了按 mtime 从旧到新删
CACHE_MAX_BYTES = 500 * 1024 * 1024

_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})

_REQUEST_HEADERS = {
    # 老实说明自己是谁：不伪装浏览器（伪装只会让第三方更难排查它自己的流量）
    "User-Agent": "Cylit-WebImage/1.0 (+local)",
    "Accept": "image/*",
}

#: 扩展名 → 回给浏览器的 MIME（**只来自魔数**，不来自上游那个 Content-Type）
_MEDIA_TYPES = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "gif": "image/gif",
    "webp": "image/webp",
    "svg": "image/svg+xml",
}

#: 私网 / 保留地址（IPv4 + IPv6）。命中**任意一条**就拒。
_PRIVATE_NETWORKS = tuple(ipaddress.ip_network(cidr) for cidr in (
    "0.0.0.0/8",          # "本网络"：0.0.0.0 在某些栈上会解析成本机
    "10.0.0.0/8",
    "100.64.0.0/10",      # 运营商级 NAT
    "127.0.0.0/8",
    "169.254.0.0/16",     # 链路本地（云元数据 169.254.169.254 就在这段）
    "172.16.0.0/12",
    "192.0.0.0/24",
    "192.168.0.0/16",
    "198.18.0.0/15",      # 基准测试保留段
    "224.0.0.0/4",        # 组播
    "240.0.0.0/4",        # 保留
    "::/128",             # IPv6 未指定地址
    "::1/128",            # IPv6 回环
    "fc00::/7",           # IPv6 唯一本地（含 fd00::/8）
    "fe80::/10",          # IPv6 链路本地
    "ff00::/8",           # IPv6 组播
))


# --------------------------------------------------------------------------
# 异常：每一种失败给一句**人话**，且各带一个稳定的 reason（日志 / 排查用）
# --------------------------------------------------------------------------


class WebImageError(PaperMindError):
    """站外图片抓取失败。子类各自给出 reason —— 失败原因必须分得清。"""

    code = "WEB_IMAGE_ERROR"
    http_status = 502
    reason = "failed"

    def __init__(self, message: str) -> None:
        super().__init__(message, detail={"reason": self.reason})


class WebImageNotHttps(WebImageError):
    code = "WEB_IMAGE_SCHEME"
    http_status = 400
    reason = "not-https"


class WebImagePrivateAddress(WebImageError):
    code = "WEB_IMAGE_PRIVATE_ADDRESS"
    http_status = 400
    reason = "private-address"


class WebImageNotImage(WebImageError):
    code = "WEB_IMAGE_NOT_IMAGE"
    http_status = 415
    reason = "not-image"


class WebImageTooLarge(WebImageError):
    code = "WEB_IMAGE_TOO_LARGE"
    http_status = 413
    reason = "too-large"


class WebImageTimeout(WebImageError):
    code = "WEB_IMAGE_TIMEOUT"
    http_status = 504
    reason = "timeout"


class WebImageUnreachable(WebImageError):
    code = "WEB_IMAGE_UNREACHABLE"
    http_status = 502
    reason = "unreachable"


# --------------------------------------------------------------------------
# 传输层：默认 httpx，测试替换 _fetcher() / _resolve() 就能完全离线
# --------------------------------------------------------------------------


@dataclass(slots=True)
class RawResponse:
    """一跳的原始响应（**不跟随重定向**：跳数由 local_path 控制，那样才能每跳重校验）。"""

    status: int
    headers: dict[str, str]
    body: bytes = b""


class Fetcher(Protocol):
    def get(self, url: str, *, timeout: float, max_bytes: int) -> RawResponse: ...


class HttpxFetcher:
    """真实的抓取：follow_redirects=False + 流式读（超上限立刻断开）。"""

    def get(self, url: str, *, timeout: float, max_bytes: int) -> RawResponse:
        import httpx

        try:
            with httpx.Client(timeout=timeout, follow_redirects=False) as client:
                with client.stream("GET", url, headers=_REQUEST_HEADERS) as resp:
                    headers = {k.lower(): v for k, v in resp.headers.items()}
                    if resp.status_code != 200:
                        # 重定向只看头，不读体；其它状态码同样不读（省流量）
                        return RawResponse(resp.status_code, headers)
                    chunks: list[bytes] = []
                    total = 0
                    for chunk in resp.iter_bytes():
                        total += len(chunk)
                        if total > max_bytes:
                            raise WebImageTooLarge(
                                "这张图太大了（超过 "
                                + str(max_bytes // 1024 // 1024) + " MB），没有代理。")
                        chunks.append(chunk)
                    return RawResponse(resp.status_code, headers, b"".join(chunks))
        except httpx.TimeoutException as exc:
            raise WebImageTimeout(
                "抓取图片超时（" + str(int(timeout)) + " 秒），这次先不显示这张图。") from exc
        except httpx.HTTPError as exc:
            raise WebImageUnreachable("抓取图片失败（网络不可达或对方拒绝）。") from exc


def _fetcher() -> Fetcher:
    """测试替换这一个函数即可完全离线（与 websearch._transport() 同一形状）。"""
    return HttpxFetcher()


def _resolve(host: str) -> list[str]:
    """默认的 DNS 解析（返回所有 A/AAAA 文本地址）；测试替换这一个函数。"""
    infos = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    return [str(info[4][0]) for info in infos]


# --------------------------------------------------------------------------
# URL / 地址校验（每一跳都过一遍）
# --------------------------------------------------------------------------


def _address_is_blocked(text: str) -> bool:
    try:
        address = ipaddress.ip_address(text)
    except ValueError:
        return False
    # IPv4 映射的 IPv6（::ffff:127.0.0.1）要按它映射到的 v4 判，否则能绕过
    mapped = getattr(address, "ipv4_mapped", None)
    candidates = (address, mapped) if mapped is not None else (address,)
    for candidate in candidates:
        for network in _PRIVATE_NETWORKS:
            if candidate.version != network.version:
                continue
            if candidate in network:
                return True
    return False


def _assert_public_url(url: str) -> str:
    """校验一个 URL 可以抓：只认 https、主机名不能落在私网（含解析结果）。返回原 url。"""
    parts = urlsplit((url or "").strip())
    scheme = parts.scheme.lower()
    if scheme != "https":
        raise WebImageNotHttps("只代理 https 图片地址（http 会降级成普通链接）。")
    host = (parts.hostname or "").strip()
    if not host:
        raise WebImageNotHttps("图片地址里没有主机名。")
    # ① 字面 IP（https://127.0.0.1/x.png）：不用查 DNS，直接判
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        if _address_is_blocked(str(literal)):
            raise WebImagePrivateAddress("拒绝抓取私网 / 回环地址的图片。")
        return url
    # ② 域名：解析后**任一**地址命中私网就拒（解析失败也算失败，不能放行）
    try:
        addresses = _resolve(host)
    except OSError as exc:
        raise WebImageUnreachable("图片域名解析失败。") from exc
    if not addresses:
        raise WebImageUnreachable("图片域名解析失败。")
    for text in addresses:
        if _address_is_blocked(text):
            log.warning("站外图片命中私网地址，拒绝抓取：host=%s addr=%s", host, text)
            raise WebImagePrivateAddress("拒绝抓取私网 / 回环地址的图片。")
    return url


# --------------------------------------------------------------------------
# 魔数嗅探：Content-Type 只当"必要条件"，真正的类型来自字节
# --------------------------------------------------------------------------


def _looks_like_svg(body: bytes) -> bool:
    """SVG 是文本：允许 BOM / 空白 / XML 声明 / 注释，之后必须出现 <svg。"""
    head = body[:4096]
    if head.startswith(b"\xef\xbb\xbf"):
        head = head[3:]
    text = head.decode("utf-8", errors="ignore").lstrip()
    while text.startswith("<?") or text.startswith("<!"):
        end = text.find(">")
        if end < 0:
            return False
        text = text[end + 1:].lstrip()
    return text.startswith("<svg") or text.startswith("<SVG")


def _sniff_extension(body: bytes, content_type: str) -> str:
    """字节 → 扩展名；认不出（或与声明不符）就抛 WebImageNotImage。"""
    head = body[:1024]
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if head.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    if content_type == "image/svg+xml" and _looks_like_svg(body):
        # SVG 只在**声明就是 svg** 时才认：否则一份任意文本（含 HTML）都能被当图存下来
        return "svg"
    raise WebImageNotImage("这个地址返回的内容不是图片（类型与字节不符）。")


# --------------------------------------------------------------------------
# 缓存目录
# --------------------------------------------------------------------------


def cache_dir() -> Path:
    """<data_dir>/cache/webimg（跟着 data_dir 走，不写死路径）。"""
    root = get_settings().webimg_cache_dir
    root.mkdir(parents=True, exist_ok=True)
    return root


def cache_key(url: str) -> str:
    """缓存文件名（不含扩展名）：sha256(url) 的十六进制。"""
    return hashlib.sha256((url or "").strip().encode("utf-8")).hexdigest()


def _lookup(url: str) -> Path | None:
    """已缓存的文件（**不联网**）；没有就 None。"""
    key = cache_key(url)
    root = cache_dir()
    for candidate in root.glob(key + ".*"):
        if candidate.name.endswith(".part"):
            continue
        try:
            if candidate.is_file() and candidate.stat().st_size > 0:
                return candidate
        except OSError:
            continue
    return None


def prune(*, max_bytes: int = CACHE_MAX_BYTES) -> list[str]:
    """目录超过 max_bytes 时按 mtime 从旧到新删文件；返回被删掉的文件名。

    容量控制刻意做得**很笨**（mtime 优先级淘汰，不记访问时间也不做 LFU）：
    这是"别把磁盘吃满"的兜底，不是缓存效率优化。启动后第一次用、以及每次新增缓存后
    各调一次，见 _prune_once / _store。
    """
    root = get_settings().webimg_cache_dir
    if not root.is_dir():
        return []
    entries: list[tuple[float, int, Path]] = []
    total = 0
    for path in root.iterdir():
        try:
            if not path.is_file():
                continue
            stat = path.stat()
        except OSError:
            continue
        entries.append((stat.st_mtime, stat.st_size, path))
        total += stat.st_size
    if total <= max_bytes:
        return []
    removed: list[str] = []
    for _mtime, size, path in sorted(entries, key=lambda item: item[0]):
        if total <= max_bytes:
            break
        try:
            path.unlink()
        except OSError:
            continue
        total -= size
        removed.append(path.name)
    if removed:
        log.info("webimg 缓存超过 %d MB，淘汰了 %d 个文件",
                 max_bytes // 1024 // 1024, len(removed))
    return removed


#: 本进程是否已经做过一次启动容量检查（只做一次）
_pruned_once = False


def _prune_once() -> None:
    global _pruned_once
    if _pruned_once:
        return
    _pruned_once = True
    try:
        prune()
    except OSError as exc:            # 容量检查失败不该连累取图
        log.warning("webimg 缓存容量检查失败（忽略）：%s", exc)


def _store(url: str, body: bytes, ext: str) -> Path:
    """原子落盘（先写 .part 再 rename），顺手做容量淘汰。"""
    target = cache_dir() / (cache_key(url) + "." + ext)
    part = target.with_name(target.name + ".part")
    part.write_bytes(body)
    os.replace(part, target)
    try:
        prune()
    except OSError as exc:
        log.warning("webimg 缓存淘汰失败（忽略）：%s", exc)
    return target


def media_type_of(path: Path) -> str:
    """回给浏览器的 MIME（**按我们嗅出来的扩展名**，不按上游声明）。"""
    return _MEDIA_TYPES.get(path.suffix.lstrip(".").lower(), "application/octet-stream")


# --------------------------------------------------------------------------
# 对外入口
# --------------------------------------------------------------------------


def local_path(url: str, *, max_bytes: int | None = None) -> Path:
    """url → 本地上已有（或刚抓下来）的缓存文件路径；失败抛 WebImageError 子类。"""
    limit = MAX_BYTES if max_bytes is None else max_bytes
    _prune_once()
    requested = (url or "").strip()
    if not requested:
        raise WebImageNotHttps("缺少图片地址。")
    cached = _lookup(requested)
    if cached is not None:
        log.info("webimg 缓存命中：%s", cached.name)
        return cached
    target = requested
    fetcher = _fetcher()
    for _hop in range(MAX_REDIRECTS + 1):
        _assert_public_url(target)
        try:
            response = fetcher.get(target, timeout=TIMEOUT_SECONDS, max_bytes=limit)
        except WebImageError:
            raise
        except Exception as exc:      # noqa: BLE001 —— 传输层的任何意外都收敛成一句人话
            raise WebImageUnreachable("抓取图片失败（网络不可达或对方拒绝）。") from exc
        if response.status in _REDIRECT_STATUSES:
            location = (response.headers.get("location") or "").strip()
            if not location:
                raise WebImageUnreachable("对方的跳转响应里没有目标地址。")
            target = urljoin(target, location)
            continue
        if response.status != 200:
            # 上游的状态码只写进日志，回给前端的是我们自己的一句话
            log.warning("webimg 上游返回非 200：status=%s url=%s",
                        response.status, target[:200])
            raise WebImageUnreachable(
                "图片站返回了 HTTP " + str(response.status) + "，这张图取不到。")
        body = response.body or b""
        if len(body) > limit:
            raise WebImageTooLarge(
                "这张图太大了（超过 " + str(limit // 1024 // 1024) + " MB），没有代理。")
        declared = (response.headers.get("content-type") or "").split(";")[0].strip().lower()
        if not declared.startswith("image/"):
            log.warning("webimg 上游不是图片：content-type=%s url=%s",
                        declared or "-", target[:200])
            raise WebImageNotImage("这个地址返回的不是图片（Content-Type 不是 image/*）。")
        if not body:
            raise WebImageNotImage("这个地址返回了空内容。")
        ext = _sniff_extension(body, declared)
        path = _store(requested, body, ext)
        log.info("webimg 已落盘：%s（%d 字节，来自 %s）", path.name, len(body),
                 urlsplit(target).hostname or "-")
        return path
    raise WebImageUnreachable("重定向超过 " + str(MAX_REDIRECTS) + " 跳，放弃了。")


def proxy_path(url: str, *, prefix: str | None = None) -> str:
    """图片地址 → 本站代理地址（**只拼字符串，不抓取**）。

    为什么单独一个函数：清洗（agent/sanitize.py）与路由必须用**同一套**拼法，
    否则"模型写进去的地址"和"路由认的地址"会分家。safe="" 是关键 ——
    整条 URL 全百分号转义，于是地址里的 ) / 空格 / & 不会把 markdown 括号撑破。
    """
    base = (prefix if prefix is not None else get_settings().api_prefix) or ""
    return base.rstrip("/") + "/webimg?url=" + quote((url or "").strip(), safe="")


#: 给路由用的固定响应头（缓存命中与未命中一致）
CACHE_HEADERS = {
    "Cache-Control": "public, max-age=604800, immutable",
    "X-Content-Type-Options": "nosniff",
}

#: SVG 额外的隔离头：直接打开那个地址时，脚本 / 外链一样跑不起来
SVG_HEADERS = {
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; sandbox",
}
