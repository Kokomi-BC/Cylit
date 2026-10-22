"""官方提供方目录（模型页「+」里那一排卡片的数据源）。

用户口径（改造计划 §2.1 / §5 / §7.6）：

· 「官方」与否**只能由后端判定** —— 目录就是 `presets.py` 里真实存在的那几家，
  前端拿到的 `kind` 是算出来的，不是它自己传上来的字段；
· 图标用**固定映射**（`icon` 键 → 前端 `assets/model-icons` 里同一套键），不在前端各页各写一份；
· 这里**不出现任何密钥内容**，也不出现凭据文件名 —— 只给厂商身份、端点、支持的能力。

自定义提供方不在这份目录里：它没有固定的厂商身份，前端用统一占位图标（见 `custom_provider()`）。
"""

from __future__ import annotations

# 厂商展示名与图标键：图标键必须与前端 `assets/model-icons` 的键一致（aliases 之外的那一层）。
PROVIDER_META: dict[str, dict] = {
    "deepseek": {
        "label": "DeepSeek",
        "icon": "deepseek",
        "note": "官方 OpenAI 兼容端点；支持思考模式",
        "auth": "bearer",
    },
    "dashscope": {
        "label": "阿里云百炼（DashScope）",
        "icon": "qwen",
        "note": "Qwen 系列的官方端点",
        "auth": "bearer",
    },
    "dashscope_rag": {
        "label": "阿里云 · RAG 检索",
        "icon": "qwen",
        "note": "向量化 + 重排一组（同一个百炼账号）",
        "auth": "bearer",
    },
    "zhipu": {
        "label": "智谱 GLM",
        "icon": "zhipu",
        "note": "OpenAI 兼容端点",
        "auth": "bearer",
    },
    "openai": {"label": "OpenAI · GPT", "icon": "openai",
               "note": "官方 OpenAI 兼容端点", "auth": "bearer"},
    "anthropic": {"label": "Claude · Anthropic", "icon": "claude",
                  "note": "原生 Messages 协议（非 OpenAI 兼容）", "auth": "bearer"},
    "gemini": {"label": "Gemini · Google", "icon": "gemini",
               "note": "Google 的 OpenAI 兼容层", "auth": "bearer"},
    "doubao": {"label": "豆包 · 火山方舟（Ark）", "icon": "doubao",
               "note": "model 填推理接入点 ID", "auth": "bearer"},
    "moonshot": {"label": "Kimi · 月之暗面", "icon": "moonshot",
                 "note": "OpenAI 兼容端点", "auth": "bearer"},
    "minimax": {"label": "MiniMax", "icon": "minimax",
                "note": "OpenAI 兼容端点", "auth": "bearer"},
    "ollama": {
        "label": "Ollama（本机）",
        "icon": "",
        "note": "跑在自己机器上，不需要密钥",
        "auth": "none",
    },
    "local_bge": {
        "label": "本地 BGE（向量化）",
        "icon": "",
        "note": "本机推理，不需要密钥",
        "auth": "none",
    },
}

# 预设里可能出现的任务类型（顺序即界面顺序）
TYPES = ("generation", "agent", "embedding", "reranker", "vision")


def custom_provider() -> dict:
    """自定义提供方的固定描述：没有品牌身份，图标交给前端用统一占位。"""
    return {
        "id": "custom",
        "kind": "custom",
        "label": "自定义提供方",
        "icon": "",
        "note": "填名称 / Base URL / API Key / Model ID 四项即可",
        "auth": "bearer",
        "types": [],
    }


def catalog() -> dict:
    """官方提供方目录 + 自定义入口。

    `presets` = 这家有几套现成模板（前端可显示「2 套预设」）；
    `configured` = 库里已经建了几条这家厂商的模型（0 表示还没配过）。
    """
    from backend.services.model import presets, model_service

    rows = model_service.list_models()
    configured: dict[str, int] = {}
    for row in rows:
        key = str(row.get("provider") or "").strip().lower()
        if key:
            configured[key] = configured.get(key, 0) + 1

    by_provider: dict[str, dict] = {}
    for preset in presets.list_presets():
        provider = str(preset.get("provider") or "").strip().lower()
        if not provider:
            continue
        meta = PROVIDER_META.get(provider, {})
        entry = by_provider.setdefault(provider, {
            "id": provider,
            "kind": "official",
            "label": meta.get("label") or provider,
            "icon": meta.get("icon", ""),
            "note": meta.get("note", ""),
            "auth": meta.get("auth", "bearer"),
            "base_url": "",
            "types": [],
            "presets": 0,
            "configured": configured.get(provider, 0),
        })
        entry["presets"] += 1
        for mtype in TYPES:
            spec = preset.get(mtype)
            if not isinstance(spec, dict):
                continue
            if mtype not in entry["types"]:
                entry["types"].append(mtype)
            if not entry["base_url"] and spec.get("base_url"):
                entry["base_url"] = str(spec["base_url"])

    providers = sorted(by_provider.values(), key=lambda p: (-p["presets"], p["id"]))
    return {"providers": providers, "custom": custom_provider()}
