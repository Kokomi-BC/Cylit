"""解析引擎的**可调默认值**：本地（PyMuPDF）与 MinerU 各一套，存在 setting 表里。

为什么单独一个模块：这些值同时被三处读 —— 任务处理器（真正开跑时取默认值）、
MinerU 客户端（本次参数里的三项开关）、扫描页读页钩子（要不要调视觉模型）。
散在三处各写一遍迟早会漂，所以只留一个读取点。

取值优先级（与 `mineru_service.effective_params` 一致）：
**本次任务参数 > 这里的默认值 > 代码/官方默认值**。
空值不写进库（`save` 只处理显式给出的键），所以"没动过"和"设成默认值"不会混为一谈。

校验复用 `mineru_service` 的既有函数（`as_bool` / `norm_page_ranges`）——
函数内 import，避免与本模块形成环。
"""
from __future__ import annotations

import logging
from typing import Any

from backend.core.errors import ValidationError

log = logging.getLogger(__name__)

#: MinerU 每次解析的三个开关 + 页码范围（作为**导入默认值**，导入时仍可逐篇改）
MINERU_KEYS = (
    "mineru_is_ocr",
    "mineru_enable_formula",
    "mineru_enable_table",
    "mineru_page_ranges",
)
#: 本地解析：读页是否调视觉模型、读页 dpi、插图两个开关
LOCAL_KEYS = (
    "parse_vision_enabled",
    "parse_vision_dpi",
    "parse_figure_caption",
    "parse_deep_figure_search",
)
ALL_KEYS = MINERU_KEYS + LOCAL_KEYS

#: 布尔键：统一走 as_bool，接受 true/false/1/0/on/off
BOOL_KEYS = (
    "mineru_is_ocr", "mineru_enable_formula", "mineru_enable_table",
    "parse_vision_enabled", "parse_figure_caption", "parse_deep_figure_search",
)

VISION_DPI_MIN, VISION_DPI_MAX = 72, 600


def _read_stored() -> dict[str, Any]:
    """复用 `mineru_service` 的读写口（同一个 setting 表、同一个测试注入口）。"""
    from backend.services.paper import mineru_service

    stored = mineru_service._stored()          # noqa: SLF001 - 同包姐妹模块的唯一读写口
    return {k: v for k, v in stored.items() if k in ALL_KEYS}


def _write(key: str, value: Any) -> None:
    from backend.services.paper import mineru_service

    mineru_service._write_setting(key, value)  # noqa: SLF001


def _core_defaults() -> dict[str, Any]:
    """代码里的默认值（`core/config.py` 的 parse_* 与官方默认值）。"""
    from backend.core.config import get_settings

    s = get_settings()
    return {
        "mineru_is_ocr": False,
        "mineru_enable_formula": True,
        "mineru_enable_table": True,
        "mineru_page_ranges": "",
        "parse_vision_enabled": bool(s.parse_vision_enabled),
        "parse_vision_dpi": int(s.parse_vision_dpi),
        "parse_figure_caption": False,
        "parse_deep_figure_search": False,
    }


def defaults() -> dict[str, Any]:
    """当前生效的默认值（库里的覆盖代码默认）。字段永远齐全，读不到就用代码默认。"""
    out = _core_defaults()
    stored = _read_stored()
    for key, value in stored.items():
        if value in (None, ""):
            continue                      # 空值等于"没设过"，别把默认值盖成空
        out[key] = value
    out["parse_vision_dpi"] = _clamp_dpi(out.get("parse_vision_dpi"))
    return out


def _clamp_dpi(value: Any) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        n = 200
    return max(VISION_DPI_MIN, min(VISION_DPI_MAX, n))


def mineru_defaults() -> dict[str, Any]:
    """给 `effective_params` 用的四项（键名与任务参数一致）。"""
    cfg = defaults()
    return {k: cfg[k] for k in MINERU_KEYS}


def local_defaults() -> dict[str, Any]:
    """给任务处理器 / 读页钩子用的四项。"""
    cfg = defaults()
    return {k: cfg[k] for k in LOCAL_KEYS}


def public() -> dict[str, Any]:
    """接口回给前端的形状（附带两个范围的元信息，前端不用再写死）。"""
    cfg = defaults()
    return {
        **cfg,
        "vision_dpi_min": VISION_DPI_MIN,
        "vision_dpi_max": VISION_DPI_MAX,
    }


def save(payload: dict | None) -> dict[str, Any]:
    """保存显式给出的键（省略 = 不动），返回新的完整配置。"""
    from backend.services.paper.mineru_service import as_bool, norm_page_ranges

    payload = payload or {}
    applied: dict[str, Any] = {}

    for key in BOOL_KEYS:
        if key in payload and payload[key] is not None and payload[key] != "":
            applied[key] = as_bool(payload[key], field=_LABELS[key])

    # 页码范围**允许显式清空**（空串 = 全篇）。所以这里判的是"键在不在"，不是"值非空"：
    # 否则设过一次 1-5 就再也回不到全篇（用户口径 2026-12：默认值要能调回去）。
    if "mineru_page_ranges" in payload and payload["mineru_page_ranges"] is not None:
        raw = str(payload["mineru_page_ranges"]).strip()
        applied["mineru_page_ranges"] = norm_page_ranges(raw) if raw else ""

    if payload.get("parse_vision_dpi") not in (None, ""):
        try:
            dpi = int(payload["parse_vision_dpi"])
        except (TypeError, ValueError) as exc:
            raise ValidationError(
                f"读页 dpi 只能是一个整数（收到 {payload.get('parse_vision_dpi')!r}）") from exc
        if not VISION_DPI_MIN <= dpi <= VISION_DPI_MAX:
            raise ValidationError(
                f"读页 dpi 要在 {VISION_DPI_MIN}–{VISION_DPI_MAX} 之间（收到 {dpi}）")
        applied["parse_vision_dpi"] = dpi

    for key, value in applied.items():
        _write(key, value)
    if applied:
        log.info("解析默认值已更新：%s", ", ".join(sorted(applied)))

    cfg = public()
    cfg["applied"] = applied
    return cfg


_LABELS = {
    "mineru_is_ocr": "强制 OCR",
    "mineru_enable_formula": "识别公式",
    "mineru_enable_table": "识别表格",
    "parse_vision_enabled": "视觉读页",
    "parse_figure_caption": "插图带图注",
    "parse_deep_figure_search": "深度找图",
}

__all__ = [
    "ALL_KEYS", "BOOL_KEYS", "LOCAL_KEYS", "MINERU_KEYS",
    "VISION_DPI_MAX", "VISION_DPI_MIN",
    "defaults", "local_defaults", "mineru_defaults", "public", "save",
]
