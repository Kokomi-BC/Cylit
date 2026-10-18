"""**Cyli 解释**用的模型配置（设置页「论文问答 → Cyli 解释」）。

为什么要单独一份：划词解释 / 插图解释与侧栏对话是两种负载 ——
解释是"一锤子买卖"（读一段、讲清楚、结束），对话要长上下文与工具编排。
共用一个模型时只能二选一：要么解释很贵（对话那档的大模型），要么对话很笨（解释那档的小模型）。
所以这里让用户单独指定：**模型**（空 = 跟随对话默认）+ **思考强度**（空 = 跟随模型自己的设置）。

口径与 `style.py` 一致：存 setting 表裸键，读法走 `retrieval.setting`
（它先试 `kb.` 前缀那一行、再回退裸键）；**空串 = 没设过**，读取方据此回落默认。
"""

from __future__ import annotations

#: 设置表里的两个键（裸键）
KEY_MODEL = "explain_model_config_id"
KEY_EFFORT = "explain_reasoning_effort"


def model_id() -> str:
    """用户给 Cyli 解释指定的模型配置 id；空 = 跟随对话默认。"""
    from backend.services.knowledge import retrieval

    return str(retrieval.setting(KEY_MODEL, "") or "").strip()


def reasoning_effort() -> str:
    """思考强度（off / low / medium / high / max）；空 = 用模型自己 parameters 里那份。"""
    from backend.services.knowledge import retrieval

    return str(retrieval.setting(KEY_EFFORT, "") or "").strip()


def pick() -> tuple[str, str]:
    """（模型 id, 思考强度）—— 两个空串都表示"没配，跟随默认"。"""
    return model_id(), reasoning_effort()