"""模型端点地址的安全闸门（SSRF 防护）。

用户口径（2026-12-07 改造计划 §7）：自定义提供方的 Base URL 是**用户输入**，
因此拉模型列表 / 测连通 / 正式调用都必须过同一道校验，不许把它当成可信地址直接请求。

三条规则：

1. **协议与形状**：只允许 http / https；不许带 userinfo（`https://u:p@host`）；
   不许带 query / fragment（Base URL 是前缀，不是完整请求）；host 必须能 IDNA 编码。
2. **云元数据地址永远拒绝**：`169.254.169.254`、阿里云 `100.100.100.200`、
   `metadata.google.internal` 这类能换到实例凭据的目标，无论 allow_private 与否都拦。
3. **内网 / 环回 / 链路本地**：只有**显式放行**才允许。放行口径见 `allow_private_for`：
   本次请求体里现填的地址一律不放行（未保存 = 不可信）；已保存的配置（`model_id`）放行，
   因为 Ollama / 本地 BGE 这类本来就在 127.0.0.1 上跑 —— 那是管理员自己写进去的配置。

域名一旦解析出**任意一个**被拦的地址就整体拒绝：DNS 轮询里混一个内网 IP 也照样是 SSRF。
"""

from __future__ import annotations

import asyncio
import ipaddress
import os
import socket
from urllib.parse import urlsplit

from backend.core.errors import ValidationError

# 允许放行内网目标的环境变量开关（默认关闭；`1` 打开）
ALLOW_PRIVATE_ENV = "CYLIT_ALLOW_PRIVATE_MODEL_ENDPOINT"

# 云厂商元数据服务的地址：命中就拒，与 `allow_private` 无关
_METADATA_IPS = {
    ipaddress.ip_address("169.254.169.254"),   # AWS / GCP / Azure / 阿里云 ECS 共用的链路本地地址
    ipaddress.ip_address("100.100.100.200"),   # 阿里云 ECS 元数据
    ipaddress.ip_address("fd00:ec2::254"),     # AWS IPv6 元数据
}
_METADATA_HOSTS = {
    "metadata", "metadata.google.internal", "metadata.goog", "instance-data",
    "metadata.azure.com", "metadata.tencentyun.com",
}


def normalize_base_url(raw: str | None) -> str:
    """规范化并做**形状**校验；返回去掉尾斜杠的 Base URL。违反规则抛 ValidationError。"""
    text = str(raw or "").strip()
    if not text:
        raise ValidationError("Base URL 不能为空")
    if len(text) > 500:
        raise ValidationError("Base URL 太长（上限 500 字符）")
    parts = urlsplit(text)
    if parts.scheme.lower() not in ("http", "https"):
        raise ValidationError("Base URL 只支持 http / https")
    if not parts.hostname:
        raise ValidationError("Base URL 缺少主机名")
    if parts.username or parts.password or "@" in parts.netloc:
        raise ValidationError("Base URL 不能带用户名或密码，请把密钥填在 API Key 里")
    if parts.query or parts.fragment:
        raise ValidationError("Base URL 不能带查询参数或锚点")
    try:
        host = parts.hostname.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise ValidationError("Base URL 的主机名不合法") from exc
    if host in _METADATA_HOSTS:
        raise ValidationError("这个地址是云厂商的元数据服务，不能作为模型端点")
    port = f":{parts.port}" if parts.port else ""
    path = parts.path.rstrip("/")
    return f"{parts.scheme.lower()}://{host}{port}{path}"


def _ip_is_blocked(ip: ipaddress._BaseAddress, *, allow_private: bool) -> bool:
    if ip in _METADATA_IPS:
        return True
    if ip.is_loopback or ip.is_private or ip.is_link_local or ip.is_reserved or ip.is_multicast:
        return not allow_private
    return False


def _resolve(host: str, port: int) -> list[str]:
    """解析出全部 A / AAAA 记录；解析不出来就抛 ValidationError（宁可拒也不猜）。"""
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except OSError as exc:
        raise ValidationError("域名解析失败，请检查 Base URL") from exc
    addrs: list[str] = []
    for info in infos:
        sockaddr = info[4]
        if sockaddr and sockaddr[0] not in addrs:
            addrs.append(sockaddr[0])
    if not addrs:
        raise ValidationError("域名没有解析到任何地址")
    return addrs


def guard_url_sync(url: str, *, allow_private: bool = False) -> str:
    """校验一个**已规范化**的 URL；不安全就抛 ValidationError，安全则原样返回。"""
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if not host:
        raise ValidationError("Base URL 缺少主机名")
    if host in _METADATA_HOSTS:
        raise ValidationError("这个地址是云厂商的元数据服务，不能作为模型端点")
    port = parts.port or (443 if parts.scheme == "https" else 80)
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        candidates = _resolve(host, port)
    else:
        candidates = [str(literal)]
    for addr in candidates:
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            raise ValidationError("解析出的地址不合法") from None
        if _ip_is_blocked(ip, allow_private=allow_private):
            raise ValidationError(
                "这个地址指向内网 / 环回 / 云元数据，出于安全不能作为模型端点"
                if not allow_private else
                "这个地址是云厂商的元数据服务，不能作为模型端点",
            )
    return url


async def guard_url(url: str, *, allow_private: bool = False) -> str:
    """`guard_url_sync` 的异步壳：DNS 解析放到线程里，别堵事件循环。"""
    return await asyncio.to_thread(guard_url_sync, url, allow_private=allow_private)


def private_allowed_by_env() -> bool:
    """环境开关：显式 `1` 才放行内网（给「我就是连内网模型」的部署用） */"""
    return str(os.environ.get(ALLOW_PRIVATE_ENV, "")).strip() == "1"


def allow_private_for(*, stored_config: bool) -> bool:
    """放行口径（唯一真源）：

    · **已保存的配置**（`model_id` 走查表）：默认放行 —— Ollama / 本地 BGE 就在 127.0.0.1，
      那是管理员自己写进库的地址，不是本次请求现填的；环境变量显式设 `0` 也能关掉。
    · **请求体里现填的地址**：默认不放行，必须 `CYLIT_ALLOW_PRIVATE_MODEL_ENDPOINT=1`。
    """
    flag = os.environ.get(ALLOW_PRIVATE_ENV)
    if stored_config:
        return str(flag).strip() != "0"
    return str(flag).strip() == "1"
