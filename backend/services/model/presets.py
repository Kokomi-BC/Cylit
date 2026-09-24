"""常见模型 Provider 预设（方便"填个 key 就能用"）。

不硬编码到业务代码里（规划 §12.2）：预设只是**配置模板**，
由用户在 /models 页一键导入后仍存于 MySQL ``model_config``。
"""

from __future__ import annotations

from backend.core.errors import ValidationError
from backend.services.model import model_service

PRESETS: dict[str, dict] = {
    # ---- 默认：思考模式开启（质量优先，规划 §14.3 的候选修复/择优需要推理）----
    "deepseek": {
        "label": "DeepSeek V4.1 Flash · 思考模式（默认）",
        "provider": "deepseek",
        # 实测（2026-09，官方文档 + 真实调用）：
        #  · 平台只接受 deepseek-flash / deepseek-v4-pro 两个模型名
        #    （deepseek-chat / deepseek-v4-flash / deepseek-reasoner 均可调通，
        #     但 model 字段都返回 deepseek-flash）；
        #  · 思考模式**默认开启**、effort 默认 high，思维链计入 completion_tokens
        #    → max_tokens 必须给足（这里 16384），否则正文会是空串（finish=length）；
        #  · 思考模式下 temperature 无效；top_p 生效且下限 0.95。
        "generation": {
            "model_name": "deepseek-flash",
            "base_url": "https://api.deepseek.com/v1",
            "secret_ref": "deepseek.key",
            "parameters": {
                "thinking": "enabled",
                "reasoning_effort": "high",
                "max_tokens": 16384,
                "top_p": 0.95,
            },
        },
        "embedding": None,
        "vision": {
            "model_name": "deepseek-v4-flash-vision-exp",
            "base_url": "https://api.deepseek.com/v1",
            "secret_ref": "deepseek.key",
            "parameters": {"thinking": "enabled", "max_tokens": 8192, "top_p": 0.95},
        },
    },
    # ---- 高速档：关闭思考（便宜、快、temperature 可用）----
    "deepseek-fast": {
        "label": "DeepSeek V4.1 Flash · 关闭思考（高速档）",
        "provider": "deepseek",
        # 批量翻译这类"输入输出都既定"的任务用这个：实测 860ms vs 1860ms，
        # 且 completion_tokens 从 200 降到 35（无思维链计费），temperature 生效。
        "generation": {
            "model_name": "deepseek-flash",
            "base_url": "https://api.deepseek.com/v1",
            "secret_ref": "deepseek.key",
            "parameters": {
                "thinking": "disabled",
                "max_tokens": 8192,
                "temperature": 0.3,
            },
        },
        "embedding": None,
        "vision": {
            "model_name": "deepseek-v4-flash-vision-exp",
            "base_url": "https://api.deepseek.com/v1",
            "secret_ref": "deepseek.key",
            "parameters": {"thinking": "disabled", "max_tokens": 4096,
                           "temperature": 0.2},
        },
    },
    "dashscope": {
        "label": "通义千问 / DashScope（OpenAI 兼容）",
        "provider": "dashscope",
        "generation": {"model_name": "qwen-plus",
                       "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
                       "secret_ref": "dashscope.key",
                       "parameters": {"temperature": 0.3, "max_tokens": 8192}},
        "embedding": {"model_name": "text-embedding-v3",
                      "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
                      "secret_ref": "dashscope.key",
                      "parameters": {"dim": 1024}},
    },
    "zhipu": {
        "label": "智谱 GLM（OpenAI 兼容）",
        "provider": "zhipu",
        "generation": {"model_name": "glm-4-plus",
                       "base_url": "https://open.bigmodel.cn/api/paas/v4",
                       "secret_ref": "zhipu.key",
                       "parameters": {"temperature": 0.3, "max_tokens": 8192}},
        "embedding": {"model_name": "embedding-3",
                      "base_url": "https://open.bigmodel.cn/api/paas/v4",
                      "secret_ref": "zhipu.key",
                      "parameters": {"dim": 2048}},
    },
    "ollama": {
        "label": "Ollama 本地（完全离线）",
        "provider": "ollama",
        "generation": {"model_name": "qwen2.5:7b",
                       "base_url": "http://127.0.0.1:11434/v1",
                       "secret_ref": None,
                       "parameters": {"temperature": 0.3, "max_tokens": 8192}},
        "embedding": {"model_name": "bge-m3",
                      "base_url": "http://127.0.0.1:11434/v1",
                      "secret_ref": None,
                      "parameters": {"dim": 1024}},
    },
    "local_bge": {
        "label": "本地 BGE（sentence-transformers，可选安装）",
        "provider": "local",
        "generation": None,
        "embedding": {"model_name": "BAAI/bge-small-zh-v1.5",
                      "base_url": None, "secret_ref": None,
                      "parameters": {"dim": 512, "batch": 32}},
    },
    # ---- RAG 检索档：多模态向量 + 重排（2026-09 实测选定）----
    "dashscope_rag": {
        "label": "通义千问 · RAG 检索（qwen3-vl-embedding + qwen3-vl-rerank）",
        "provider": "dashscope",
        "generation": None,
        # qwen3-vl-embedding：2560 维，文本与图片同一空间。
        # 不在这里写 instruct —— instruct 是**查询侧**参数，入库时传了就会让
        # 文档侧语义整体偏移；由检索服务在 query 时才带上。
        "embedding": {
            "provider": "dashscope_multimodal",
            "model_name": "qwen3-vl-embedding",
            "base_url": None, "secret_ref": "dashscope.key",
            "parameters": {"dim": 2560, "batch": 10},
        },
        # qwen3-vl-rerank：与向量模型同一条 Qwen3-VL 底座，文本/图片都能排。
        # 其余候选：qwen3.7-text-rerank（纯文本最强、单条 30k token）、
        # qwen3-rerank（4k token/条）；gte-rerank-v2 官方公告 2026-05-30 下线，不选。
        "reranker": {
            "provider": "dashscope",
            "model_name": "qwen3-vl-rerank",
            "base_url": None, "secret_ref": "dashscope.key",
            "parameters": {},
        },
    },
}


def list_presets() -> list[dict]:
    return [{"id": k, "label": v["label"], "provider": v["provider"],
             "generation": v.get("generation"), "embedding": v.get("embedding"),
             "reranker": v.get("reranker"), "vision": v.get("vision")}
            for k, v in PRESETS.items()]


def create_preset(name: str, *, base_url: str | None = None,
                  model_name: str | None = None,
                  include_vision: bool = True) -> dict:
    preset = PRESETS.get(name)
    if preset is None:
        raise ValidationError(f"未知预设：{name}（可选：{list(PRESETS)}）")
    created = []
    for mtype in ("generation", "embedding", "reranker", "vision"):
        spec = preset.get(mtype)
        if not spec:
            continue
        if mtype == "vision" and not include_vision:
            continue
        payload = {
            "name": f"{preset['label']} · {mtype}",
            "provider": spec.get("provider") or preset["provider"],
            "model_type": mtype,
            "model_name": model_name or spec["model_name"],
            "base_url": base_url or spec.get("base_url"),
            "secret_ref": spec.get("secret_ref"),
            "parameters": spec.get("parameters") or {},
        }
        try:
            created.append(model_service.create_model(payload))
        except Exception as exc:  # noqa: BLE001
            created.append({"error": str(exc)[:200], "payload": payload})
    return {"preset": name, "created": created}
