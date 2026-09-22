"""通用工具：ID 生成、内容哈希、原子写盘、文本规范化、时间。"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# --------------------------------------------------------------------------
# 时间
# --------------------------------------------------------------------------


def now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def now_iso() -> str:
    return now().isoformat(timespec="seconds")


def ts_compact() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


# --------------------------------------------------------------------------
# 哈希
# --------------------------------------------------------------------------


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: str | Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def short_hash(text: str, length: int = 16) -> str:
    return sha256_text(text)[:length]


# --------------------------------------------------------------------------
# ID
# --------------------------------------------------------------------------

_ALNUM = re.compile(r"[^a-z0-9]+")


def slugify(text: str, fallback: str = "item", max_len: int = 48) -> str:
    s = unicodedata.normalize("NFKD", text or "")
    s = _ALNUM.sub("-", s.lower()).strip("-")
    return (s[:max_len].strip("-") or fallback)


def new_id(prefix: str = "id") -> str:
    return f"{prefix}-{os.urandom(8).hex()}"


def block_id(page_no: int, order_in_page: int) -> str:
    """稳定人类可读块 ID：``p07-b12``（规划 §9.2，全系统锚点）。"""
    return f"p{page_no:02d}-b{order_in_page:02d}"


# --------------------------------------------------------------------------
# JSON
# --------------------------------------------------------------------------


def json_dumps(obj: Any, *, indent: int | None = None) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=indent, default=_json_default)


def _json_default(o: Any) -> Any:
    if isinstance(o, datetime):
        return o.isoformat(timespec="seconds")
    if isinstance(o, Path):
        return str(o)
    if hasattr(o, "value"):  # Enum
        return o.value
    if hasattr(o, "to_dict"):
        return o.to_dict()
    if hasattr(o, "__dict__"):
        return {k: v for k, v in o.__dict__.items() if not k.startswith("_")}
    return str(o)


def json_loads(text: str | None, default: Any = None) -> Any:
    if not text:
        return default
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return default


def write_json(path: str | Path, obj: Any, *, indent: int | None = 2) -> Path:
    return write_text_atomic(path, json_dumps(obj, indent=indent))


def read_json(path: str | Path, default: Any = None) -> Any:
    p = Path(path)
    if not p.is_file():
        return default
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return default


# --------------------------------------------------------------------------
# 原子写盘（避免任务中断留下半个文件）
# --------------------------------------------------------------------------


def write_text_atomic(path: str | Path, text: str, *, retries: int = 4) -> Path:
    """原子写文本。

    Windows 上 ``os.replace`` 会偶发 ``WinError 5 / 32``（杀软、索引器、
    其他进程短暂持有句柄），因此带重试 + 直接覆盖兜底（规划风险 R15 同类问题）。
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    last_exc: Exception | None = None
    for attempt in range(max(1, retries)):
        fd, tmp = tempfile.mkstemp(dir=str(p.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
                f.write(text)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, p)
            return p
        except OSError as exc:
            last_exc = exc
            Path(tmp).unlink(missing_ok=True)
            time.sleep(0.08 * (attempt + 1))
    # 兜底：直接覆盖写（非原子，但保证内容落地）
    try:
        with open(p, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        return p
    except OSError:
        raise last_exc if last_exc else OSError(f"写入失败：{p}")


def write_bytes_atomic(path: str | Path, data: bytes, *, retries: int = 4) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    last_exc: Exception | None = None
    for attempt in range(max(1, retries)):
        fd, tmp = tempfile.mkstemp(dir=str(p.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, p)
            return p
        except OSError as exc:
            last_exc = exc
            Path(tmp).unlink(missing_ok=True)
            time.sleep(0.08 * (attempt + 1))
    try:
        with open(p, "wb") as f:
            f.write(data)
        return p
    except OSError:
        raise last_exc if last_exc else OSError(f"写入失败：{p}")


# --------------------------------------------------------------------------
# 文本
# --------------------------------------------------------------------------

_WS = re.compile(r"[ \t\u00a0\u2000-\u200a\u3000]+")
_HYPHEN_BREAK = re.compile(r"(\w)[-\u2010\u2011]\n(\w)")
_MULTI_NL = re.compile(r"\n{3,}")

# 句末标点（判断段落是否续行，规划 §13.2）
SENT_END = ".!?:;。！？：；"
_END_CHARS = set(SENT_END + "\"')]”’）】》")


def normalize_space(text: str) -> str:
    return _WS.sub(" ", text or "").strip()


def normalize_text(text: str) -> str:
    """文本层规范化：去软连字符、合并空白，但保留段落内换行语义。"""
    t = (text or "").replace("\u00ad", "").replace("\ufb00", "ff")
    t = _WS.sub(" ", t)
    t = _MULTI_NL.sub("\n\n", t)
    return t.strip()


def dehyphenate(text: str) -> str:
    """行尾连字符粘连还原：``distribu-\\ntion`` → ``distribution``。"""
    return _HYPHEN_BREAK.sub(r"\1\2", text or "")


def join_lines(lines: list[str]) -> str:
    """把同一段落的行合并为一行（英文空格连接，CJK 直接连接）。"""
    out = ""
    for raw in lines:
        line = normalize_space(raw)
        if not line:
            continue
        if not out:
            out = line
            continue
        prev = out[-1]
        nxt = line[0]
        if _is_cjk(prev) or _is_cjk(nxt):
            out += line
        elif prev == "-" and out[-2:-1].isalnum() and nxt.isalnum():
            out = out[:-1] + line
        else:
            out += " " + line
    return dehyphenate(out)


def _is_cjk(ch: str) -> bool:
    if not ch:
        return False
    cp = ord(ch)
    return (
        0x4E00 <= cp <= 0x9FFF or 0x3400 <= cp <= 0x4DBF
        or 0x3040 <= cp <= 0x30FF or 0xAC00 <= cp <= 0xD7AF
        or 0xF900 <= cp <= 0xFAFF or 0x3000 <= cp <= 0x303F
        or 0xFF00 <= cp <= 0xFFEF
    )


def is_cjk_text(text: str, threshold: float = 0.2) -> bool:
    t = [c for c in (text or "") if not c.isspace()]
    if not t:
        return False
    return sum(1 for c in t if _is_cjk(c)) / len(t) >= threshold


def ends_sentence(text: str) -> bool:
    t = normalize_space(text)
    if not t:
        return False
    i = len(t) - 1
    while i >= 0 and t[i] in "\"')]”’）】》":
        i -= 1
    return i >= 0 and t[i] in SENT_END


def is_upper_ratio(text: str, threshold: float = 0.8) -> bool:
    letters = [c for c in (text or "") if c.isalpha()]
    if len(letters) < 3:
        return False
    return sum(1 for c in letters if c.isupper()) / len(letters) >= threshold


def truncate(text: str, limit: int, ellipsis: str = "…") -> str:
    t = text or ""
    return t if len(t) <= limit else t[: max(0, limit - 1)] + ellipsis


def safe_filename(name: str, fallback: str = "file") -> str:
    base = Path(name or "").name
    base = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", base).strip(" .")
    return base or fallback


# --------------------------------------------------------------------------
# 计时
# --------------------------------------------------------------------------


class Timer:
    """简易计时器：``with Timer() as t: ...; t.ms``。"""

    def __init__(self) -> None:
        self.start = 0.0
        self.elapsed = 0.0

    def __enter__(self) -> "Timer":
        self.start = time.perf_counter()
        return self

    def __exit__(self, *exc: object) -> None:
        self.elapsed = time.perf_counter() - self.start

    @property
    def ms(self) -> float:
        return round(self.elapsed * 1000, 2)


def chunked(items: list, size: int):
    for i in range(0, len(items), max(1, size)):
        yield items[i:i + max(1, size)]
