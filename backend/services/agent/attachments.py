"""对话里的图片附件（用户上传 / 粘贴）。

**为什么不进数据库**：图片是二进制，MySQL 里存 BLOB 会把备份、迁移、只读投影
一起拖下水；而这些东西只有一个消费方（当前这篇论文的这条会话）。所以走
``data/attachments/<日期>/<id>.<ext>`` 落盘，消息行上只留一份元数据 JSON
（``attachments_json``）—— 与"论文产物放 data/papers"是同一个口径。

**为什么元数据要落一份旁挂 JSON**：重放历史时前端要显示文件名与类型，而这两样
只在**上传那一刻**知道（内容类型还能嗅探出来，文件名嗅不出来）。写一个
``<id>.json`` 比给附件单开一张表便宜得多。

**为什么按日期分目录**：附件只增不减，单目录堆到几万个之后 Windows 的
``os.listdir``/杀软扫描会明显拖慢；按天分桶是最省事的分片。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from pathlib import Path

from backend.core.config import get_settings
from backend.core.errors import ValidationError
from backend.core.logging import get_logger
from backend.core.utils import json_dumps, json_loads, truncate

log = get_logger(__name__)

#: 扩展名（落盘用）。与 `agent_attachment_mimes` 一一对应。
EXTENSIONS = {"image/png": ".png", "image/jpeg": ".jpg",
              "image/webp": ".webp", "image/gif": ".gif"}

#: 魔数嗅探：**以内容为准**，不看 `UploadFile.content_type`
#: （浏览器对粘贴进来的图片给的类型并不可靠，甚至有给 application/octet-stream 的）。
_MAGIC: tuple[tuple[bytes, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
)


def _sniff(data: bytes) -> str | None:
    for magic, mime in _MAGIC:
        if data.startswith(magic):
            return mime
    # WebP：`RIFF....WEBP`
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def _verifiable(data: bytes) -> bool:
    """图片**真的能解码吗**（魔数只说明开头 8 个字节像 PNG）。

    为什么不能只信魔数：一个被截断的 PNG 照样通过嗅探、照样落盘，然后在**模型端**
    变成 `ModelHTTPError: 400 ... You have uploaded an unsupported image` ——
    用户看到的是"模型报错"，而不是"这张图坏了"。实测（自检里那张手搓的坏 PNG）就是这条路径。
    在这里挡掉，错误信息才落在正确的地方，也不必为它花一次 API 调用。
    """
    try:
        import io

        from PIL import Image

        with Image.open(io.BytesIO(data)) as image:
            image.verify()
        return True
    except Exception:  # noqa: BLE001 —— 任何解码失败都算"不是有效图片"
        return False


def attachments_dir() -> Path:
    return get_settings().attachments_dir


def save(data: bytes, *, filename: str = "", mime: str = "") -> dict:
    """存一张图，返回它的元数据（含给前端的 url）。"""
    s = get_settings()
    if not data:
        raise ValidationError(f"图片「{filename or '未命名'}」是空的")
    limit = int(s.agent_attachment_max_bytes)
    if len(data) > limit:
        raise ValidationError(
            f"图片「{filename or '未命名'}」有 {len(data) / 1048576:.1f} MB，"
            f"超过单张 {limit / 1048576:.0f} MB 的上限")
    sniffed = _sniff(data) or (mime or "").strip().lower()
    if sniffed not in s.agent_attachment_mimes:
        raise ValidationError(
            f"不支持的图片格式（{filename or '未命名'}）：只接受 PNG / JPEG / WebP / GIF")
    if not _verifiable(data):
        raise ValidationError(
            f"图片「{filename or '未命名'}」读不出来（文件可能损坏或没传完），请重新上传")
    attachment_id = uuid.uuid4().hex
    folder = attachments_dir() / datetime.now().strftime("%Y%m%d")
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{attachment_id}{EXTENSIONS[sniffed]}").write_bytes(data)
    meta = {
        "attachment_id": attachment_id,
        "name": truncate((filename or "image").strip(), 120),
        "mime": sniffed, "bytes": len(data),
        "url": f"{s.api_prefix}/agent/attachments/{attachment_id}",
    }
    (folder / f"{attachment_id}.json").write_text(json_dumps(meta), encoding="utf-8")
    return meta


def locate(attachment_id: str) -> tuple[Path, dict] | None:
    """按 id 找文件（含旁挂元数据）。id 非法 / 文件不在 → ``None``。

    **只认 32 位十六进制**：id 是路径的一部分，不做白名单就等于把任意路径读了出来
    （``../secrets/agent.token`` 这种）。
    """
    aid = (attachment_id or "").strip()
    if len(aid) != 32 or any(c not in "0123456789abcdef" for c in aid.lower()):
        return None
    root = attachments_dir()
    if not root.exists():
        return None
    for folder in sorted(root.iterdir(), reverse=True):
        if not folder.is_dir():
            continue
        meta_path = folder / f"{aid}.json"
        if not meta_path.exists():
            continue
        meta = json_loads(meta_path.read_text(encoding="utf-8"), {}) or {}
        for ext in EXTENSIONS.values():
            path = folder / f"{aid}{ext}"
            if path.exists():
                return path, dict(meta)
    return None


def describe(attachment_ids: list[str]) -> list[dict]:
    """把 id 列表补成完整元数据（落库 / 回放历史时用）；找不到的**丢掉**。"""
    out: list[dict] = []
    for aid in attachment_ids or []:
        found = locate(str(aid))
        if found:
            out.append(found[1])
    return out


def read_bytes(attachment_id: str) -> tuple[bytes, str]:
    """给 agent 取图用：``(字节, mime)``。找不到抛 `ValidationError`。"""
    found = locate(attachment_id)
    if found is None:
        raise ValidationError(f"图片附件不存在：{attachment_id}")
    path, meta = found
    return path.read_bytes(), str(meta.get("mime") or "image/png")
