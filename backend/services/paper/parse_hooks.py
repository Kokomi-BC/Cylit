"""解析阶段的两个"模型钩子"（parsing 层不依赖 services，钩子在这里注入）。

- **公式修复**（规划 §14.4）：证据包 → 修复后的 LaTeX，必须基于证据、不足要报不确定；
- **扫描件读页**（规划 §13.4）：页图 → 该页 Markdown 原文。

两者都放在服务层，因为要用 ``model_manager``；``parsing.pipeline`` 只接收可调用对象，
任务处理器与 CLI 共用同一份实现，避免"CLI 能跑但服务里少了个钩子"这类不一致。
"""

from __future__ import annotations

from backend.core.logging import get_logger
from backend.core.utils import json_dumps
from backend.services.model.usage_stats import usage_scope

log = get_logger(__name__)

REPAIR_SYSTEM = ("你是公式排版修复专家。只能依据 evidence 中的信息修复 LaTeX，"
                 "不得引入证据中未出现的符号或数值；证据不足必须返回 uncertainties。"
                 "只输出 JSON。")


def build_repair_fn():
    """公式 LLM 修复钩子。未配置真实生成模型时返回 ``None``（不调模型）。"""
    from backend.services.model.model_manager import model_manager

    if model_manager.is_mock("generation"):
        return None

    def repair(evidence: dict) -> dict:
        with usage_scope("formula"):
            provider = model_manager.get_generation()
            return provider.structured(json_dumps(evidence, indent=2),
                                       evidence.get("output_schema", {}),
                                       system=REPAIR_SYSTEM)

    return repair


def build_vision_fn(params: dict | None = None, *, page_total: int = 0):
    """扫描件读页钩子：没有文本层的页 → 视觉模型整页识别。

    返回 ``None`` 表示"不做扫描件兜底"（未配置视觉模型，或调用方显式关闭
    ``params["vision"] = False``）。mock 视觉也能兜底：离线跑一遍即可验证
    "扫描页 → 块 → 翻译"整条链路，只是识别结果是占位文本。
    """
    from backend.services.paper import parse_prefs

    params = params or {}
    # 开关的优先级：本次任务的 params["vision"] > 设置页存的值 > core/config 的默认
    vision_on = params.get("vision")
    if vision_on is None:
        vision_on = parse_prefs.defaults()["parse_vision_enabled"]
    if vision_on is False:
        return None
    from backend.parsing.ocr.vision_reader import read_page_markdown
    from backend.services.model.model_manager import model_manager

    try:
        provider = model_manager.get_vision(params.get("vision_model"))
    except Exception as exc:  # noqa: BLE001 - 未注册视觉模型时静默降级
        log.info("未配置视觉模型，扫描件兜底关闭：%s", str(exc)[:120])
        return None
    if not getattr(provider, "supports_vision", False):
        log.warning("视觉模型 %s 不支持图片输入，扫描件兜底关闭",
                    provider.model_name or provider.provider_name)
        return None

    total = int(params.get("page_total") or page_total or 0)

    def read(page_no: int, image: bytes) -> str:
        with usage_scope("vision"):
            markdown, usage = read_page_markdown(
                image, provider, page_no=page_no, page_total=total or page_no)
        log.info("扫描页 p%d 识别完成：%d 字符，%d+%d token，%.0fms",
                 page_no, usage.get("chars", 0), usage.get("prompt_tokens", 0),
                 usage.get("completion_tokens", 0), usage.get("latency_ms", 0.0))
        return markdown

    return read
