"""官方提供方目录（模型页「+」里那一排卡片的数据源，**后端唯一真源**）。

用户口径（改造计划 §2.1 / §5 / §7.6，2026-10 二次收敛）：

· 「官方」与否**只能由后端判定** —— 就是本模块 ``PROVIDER_META` 里登记的这几家，
  前端拿到的 ``kind`` 是算出来的，不是它自己传上来的字段；
· 这里同时登记每家的**默认端点**（``base_url``）与**能跑哪些任务类型**（``types``），
  以前这两样是从"一键导入预设"的模板里派生出来的 —— 那套模板已按用户口径整套下线
  （``presets.py`` 已删除：它只是「思考模式开 / 关」这类冗余变体，不是提供方身份）；
· 图标用**固定映射**（``icon`` 键 → 前端 ``assets/model-icons`` 里同一套键），不在前端各页各写一份；
· 这里**不出现任何密钥内容**，也不出现凭据文件名 —— 只给厂商身份、端点、支持的能力。

自定义提供方不在这份目录里：它没有固定的厂商身份，前端用统一占位图标（见 ``custom_provider()``）。
"""

from __future__ import annotations

#: 一家提供方能跑的任务类型（顺序即界面顺序）
TYPES = ("generation", "agent", "embedding", "reranker", "vision")

# 厂商展示名 / 图标键 / 默认端点 / 支持的任务类型。
# 图标键必须与前端 ``assets/model-icons`` 的键一致（aliases 之外的那一层）。
# base_url 与 types 是从原 presets.py 的模板里**逐条抄下来的**（不是我编的），
# 模板下线后这里是唯一出处；用户新建这家时表单一开就带默认端点。
PROVIDER_META: dict[str, dict] = {
    "deepseek": {
        "label": "DeepSeek",
        "icon": "deepseek",
        "note": "官方 OpenAI 兼容端点；支持思考模式",
        "auth": "bearer",
        "base_url": "https://api.deepseek.com/v1",
        "types": ("generation", "agent", "vision"),
    },
    "dashscope": {
        "label": "阿里云百炼（DashScope）",
        "icon": "qwen",
        "note": "Qwen 系列的官方端点",
        "auth": "bearer",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "types": ("generation", "embedding", "reranker"),
    },
    # 这是原来「RAG 检索」那套模板的归类名，**从来不是 provider id**（从它导入的模型
    # provider 全是 dashscope）。目录里每条都必须是"模型行的 provider 真可能取到的值"，
    # 否则用户会对着一个永远配不出模型的厂商发呆 —— 所以 hidden。
    # 元信息留着：它记着"百炼这一家能跑 embedding + reranker"这件事的出处。
    "dashscope_rag": {
        "label": "阿里云 · RAG 检索",
        "icon": "qwen",
        "note": "向量化 + 重排一组（同一个百炼账号）",
        "auth": "bearer",
        "hidden": True,
        # 原预设里这两类模型的 base_url 是空 = 跟随百炼的同一个端点
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "types": ("embedding", "reranker"),
    },
    # 多模态向量（qwen3-vl-embedding）**不再是一家提供方**：它原来是
    # provider=dashscope_multimodal，于是同一家阿里云被拆成两个厂商（密钥、图标、目录
    # 全重复）。现在能力是**模型级协议**：model_config.parameters.protocol =
    # 'dashscope_multimodal'（见 providers/factory.build_embedding_provider），
    # 厂商一律是 dashscope —— 所以这里没有这一条。
    "zhipu": {
        "label": "智谱 GLM",
        "icon": "zhipu",
        "note": "OpenAI 兼容端点",
        "auth": "bearer",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "types": ("generation", "embedding"),
    },
    "openai": {
        "label": "OpenAI · GPT",
        "icon": "openai",
        "note": "官方 OpenAI 兼容端点",
        "auth": "bearer",
        "base_url": "https://api.openai.com/v1",
        "types": ("generation", "agent", "embedding", "vision"),
    },
    "anthropic": {
        "label": "Claude · Anthropic",
        "icon": "claude",
        "note": "原生 Messages 协议（非 OpenAI 兼容）",
        "auth": "bearer",
        "base_url": "https://api.anthropic.com",
        "types": ("generation", "agent"),
    },
    "gemini": {
        "label": "Gemini · Google",
        "icon": "gemini",
        "note": "Google 的 OpenAI 兼容层",
        "auth": "bearer",
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "types": ("generation", "agent"),
    },
    "doubao": {
        "label": "豆包 · 火山方舟（Ark）",
        "icon": "doubao",
        "note": "model 填推理接入点 ID",
        "auth": "bearer",
        "base_url": "https://ark.cn-beijing.volces.com/api/v3",
        "types": ("generation", "agent"),
    },
    "moonshot": {
        "label": "Kimi · 月之暗面",
        "icon": "moonshot",
        "note": "OpenAI 兼容端点",
        "auth": "bearer",
        "base_url": "https://api.moonshot.cn/v1",
        "types": ("generation", "agent"),
    },
    "minimax": {
        "label": "MiniMax",
        "icon": "minimax",
        "note": "OpenAI 兼容端点",
        "auth": "bearer",
        "base_url": "https://api.minimaxi.com/v1",
        "types": ("generation",),
    },
    # 没有品牌图标的本地推理档一律 hidden：图标画不出来就是一个空白方块，
    # 用户口径是「模型列表里没有图标的就不要出现」。本机端点仍可用「自定义提供方」接入。
    "ollama": {
        "label": "Ollama（本机）",
        "icon": "",
        "note": "跑在自己机器上，不需要密钥",
        "auth": "none",
        "hidden": True,
        "base_url": "http://127.0.0.1:11434/v1",
        "types": ("generation", "embedding"),
    },
    # 本地推理没有 HTTP 端点（进程内跑），所以 base_url 留空是事实，不是漏填。
    "local": {
        "label": "本地推理（BGE / sentence-transformers）",
        "icon": "",
        "note": "本机推理，不需要密钥",
        "auth": "none",
        "hidden": True,
        "base_url": "",
        "types": ("embedding",),
    },
    "local_bge": {
        "label": "本地 BGE（向量化）",
        "icon": "",
        "note": "本机推理，不需要密钥",
        "auth": "none",
        "hidden": True,
        "base_url": "",
        "types": ("embedding",),
    },
}


def official_ids() -> set[str]:
    """后端认定的官方提供方 id（**跳过 hidden**）。``provider_store`` 与目录共用这一处。"""
    return {pid for pid, meta in PROVIDER_META.items() if not meta.get("hidden")}


def meta_for(provider_id: str | None) -> dict:
    """某家的元信息（没有就是空 dict，调用方回落）。"""
    return PROVIDER_META.get(str(provider_id or "").strip().lower(), {})


def default_base_url(provider_id: str | None) -> str:
    """这家的默认端点（没有登记就是空串）。"""
    return str(meta_for(provider_id).get("base_url") or "")


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

    遍历的**唯一真源**就是 ``PROVIDER_META``（跳过 hidden）；
    ``configured`` = 库里已经建了几条这家厂商的模型（0 表示还没配过）。
    """
    from backend.services.model import model_service

    configured: dict[str, int] = {}
    for row in model_service.list_models():
        key = str(row.get("provider") or "").strip().lower()
        if key:
            configured[key] = configured.get(key, 0) + 1

    providers = []
    for pid in sorted(PROVIDER_META):
        meta = PROVIDER_META[pid]
        if meta.get("hidden"):
            # 没有品牌图标的本地档不进目录（见 PROVIDER_META 里的说明）
            continue
        providers.append({
            "id": pid,
            "kind": "official",
            "label": meta.get("label") or pid,
            "icon": meta.get("icon", ""),
            "note": meta.get("note", ""),
            "auth": meta.get("auth", "bearer"),
            "base_url": str(meta.get("base_url") or ""),
            "types": [t for t in TYPES if t in tuple(meta.get("types") or ())],
            "configured": configured.get(pid, 0),
        })
    return {"providers": providers, "custom": custom_provider()}
