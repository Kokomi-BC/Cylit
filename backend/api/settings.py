"""设置 API（规划 §20.7）：任务线程数 / RAG 参数 / 允许通用知识 / 数据目录。

注意：**翻译没有并发设置** —— 页级翻译固定串行（见 `core/config.py` 翻译段注释）。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Request

from backend.api.response import envelope
from backend.core.config import get_settings
from backend.core.utils import json_dumps, json_loads
from backend.repositories.mysql.models import Setting
from backend.repositories.mysql.session import session_scope

router = APIRouter(tags=["settings"])

# 允许通过 /settings 覆盖的白名单（避免任意键写入）：键 → 类型。
#
# 键名一律用**裸名**（`unit_expand` 而不是 `kb.unit_expand`）：服务层用
# `retrieval.setting` 解析，它会先试 `kb.<key>`、再回退裸键 —— 全库只有那一条规则。
# 写入时同样把两处都写（见 `_write_values`），这样脚本按文档写 `kb.*` 也能读到。
#
# `kb.*` 这一组以前**不在白名单里**：于是"分块 / 检索参数"只能靠脚本直接写库
# （`scripts/kb_arm_compare.py`），界面上根本调不了（dev-log §17.66）。
#
# 2026-10 收敛：`task_worker_count` / `render_dpi` / `crop_dpi` / `default_generation_model`
# 四项**从这里删掉**（设置页的「常规参数」整页移除）。它们本来就不是"能改的设置" ——
# 各自的读取点读的是 `Settings`（环境变量 / .env），设置表里那份值没有任何代码去读，
# 改了一辈子不生效。留一个"能改但没用"的开关比没有更糟。
EDITABLE: dict[str, type] = {
    "allow_general_knowledge": bool,
    # ---- 问答（agent）：**可改的只有风格那一段**（用户口径 2026-10-15）----
    # 护栏/工作方式在 `agent/prompts.py` 的常驻块里，不入设置表 —— 那是底线，不是配置。
    # 读取点：`services/agent/style.py`（空 = 没设过，回落到 prompts.DEFAULT_STYLE）。
    "agent_style": str,
    # ---- Cyli 解释（划词 / 插图解释）单独一份模型配置 ----
    # 读取点：services/agent/explain.py，消费方是 POST /agent/ask（只在 mode=explain 时用）。
    # 空串 = 没设过 → 模型跟随对话默认、思考强度跟随模型自己的 parameters。
    "explain_model_config_id": str,
    "explain_reasoning_effort": str,
    "rag_top_k_vector": int,
    "rag_top_k_lexical": int,
    "rag_top_k_fused": int,
    "rag_top_n_rerank": int,
    # ---- 知识库：分块与检索（改完必须**重建索引**才生效：chunk 是建出来的）----
    "chunk_target_chars": int,
    "chunk_min_chars": int,
    "chunk_hard_chars": int,
    "chunk_use_breaks": bool,
    "unit_index": bool,
    "unit_expand": int,
    "retrieval_types": str,
}


def _live_default(key: str) -> Any:
    """没有 ``Settings`` 字段的键（分块/检索参数只活在 setting 表里）：问**读取点**要当前生效值。

    为什么不在这儿再抄一份默认值：抄一份就有两处默认值，改了一处另一处静默漂掉 ——
    这个项目刚因为"两处键名不一致"吃过一次亏（dev-log §17.66）。
    """
    try:
        if key == "agent_style":
            from backend.services.agent import style as style_service

            return style_service.default()
        if key in ("explain_model_config_id", "explain_reasoning_effort"):
            # 默认值就是"没设过"：空串让设置页那两个下拉落在「跟随默认」那一项
            return ""

        from backend.services.knowledge import knowledge_service as ks
        from backend.services.knowledge import rag_service, retrieval

        if key in ("chunk_target_chars", "chunk_min_chars", "chunk_hard_chars"):
            target, floor, hard = ks.chunk_params()
            return {"chunk_target_chars": target, "chunk_min_chars": floor,
                    "chunk_hard_chars": hard}[key]
        if key == "chunk_use_breaks":
            return ks.using_breaks()
        if key == "unit_index":
            return ks.unit_index_enabled()
        if key == "unit_expand":
            return rag_service.unit_expand_mode()
        if key == "retrieval_types":
            return ",".join(retrieval.default_search_types())
        # 检索规模：这几个键既没有 `Settings` 字段、也没有服务层常量，默认值就在
        # `retrieval.DEFAULT_K` 里 —— 照样问读取点要，不在这里抄。
        # 注意这里要的是**当前生效值**（setting 表优先），只服务 `values`；
        # `defaults` 要的是"没改过时会是什么"，它另走 `retrieval.DEFAULT_K`（见 `_defaults`）。
        if key == "rag_top_n_rerank":
            return retrieval.rag_k("rerank")
        if key.startswith("rag_top_k_"):
            return retrieval.rag_k(key[len("rag_top_k_"):])
    except Exception:  # noqa: BLE001 —— 只服务展示，读不到不影响设置接口
        pass
    return None


def _effective() -> dict:
    s = get_settings()
    stored = _stored()
    out = {}
    for key in EDITABLE:
        if key in stored:
            out[key] = stored[key]
        elif hasattr(s, key):
            out[key] = getattr(s, key)
        else:
            out[key] = _live_default(key)
    out.update({
        "data_dir": str(s.data_dir),
        "db_backend": s.db_backend,
        "api_prefix": s.api_prefix,
        "secrets_dir": str(s.secrets_dir),
        "user_id": s.user_id,
    })
    return out


def _stored() -> dict:
    with session_scope() as sess:
        rows = sess.query(Setting).all()
        return {r.key: json_loads(r.value, r.value) for r in rows}


#: `defaults` 里的"检索规模"键 → `retrieval.DEFAULT_K` 里的名字。
#: 这四个键既没有 `Settings` 字段、也没有服务层常量，**默认值的唯一来源**就是那个字典。
RAG_K_KEYS: dict[str, str] = {
    "rag_top_k_vector": "vector",
    "rag_top_k_lexical": "lexical",
    "rag_top_k_fused": "fused",
    "rag_top_n_rerank": "rerank",
}


def _defaults() -> dict:
    """设置页要显示的**代码默认值**（值被改过之后，用户要能对比与恢复）。

    ⚠️ 与 `values` 是**两种口径**，不能共用同一个来源：

    - `values`（`_effective`）要**当前生效值** —— 存过就取存的那份，没存过才回落到默认；
      所以那条路径向读取点要（`_live_default` / `get_settings()`）是对的；
    - `defaults` 要**没改过时会是什么** —— 一律取代码里的默认，**不看 setting 表**。

    检索规模那四项因此**不能**走 `_live_default`：它对这四个键返回 `retrieval.rag_k(...)`，
    而 `rag_k` 读的正是 setting 表（含界面上的覆盖）。用户把 20 改成 30，`defaults` 会跟着
    变成 30，「恢复默认」点下去只是把表单填回它现在的值 —— 一个空操作。
    默认值取 `retrieval.DEFAULT_K`（数字与名字都在那一处，这里不复写数字）。
    """
    from backend.services.knowledge import retrieval

    out = {key: int(retrieval.DEFAULT_K[name]) for key, name in RAG_K_KEYS.items()}
    from backend.services.agent import style as style_service

    out["agent_style"] = style_service.default()
    # 这一项**有** `Settings` 字段（环境变量），默认值直接取设置对象
    out["allow_general_knowledge"] = get_settings().allow_general_knowledge
    return out


@router.get("/settings")
async def get_settings_api(request: Request):
    """设置页的读取口。

    ``defaults`` 给"默认值不在 `Settings` 字段上"的那些键 —— 值被改过之后，设置页仍然要能
    显示"默认是多少 / 默认是哪一段"，用户才能对比与恢复：

    - ``agent_style``：代码里的默认风格（`services/agent/style.py`）；
    - ``rag_top_k_vector`` / ``rag_top_k_lexical`` / ``rag_top_k_fused`` / ``rag_top_n_rerank``：
      检索规模，取 `retrieval.DEFAULT_K`（**代码默认**，不是 `retrieval.rag_k` 的当前生效值）；
    - ``allow_general_knowledge``：`Settings` 里的环境变量默认值。

    `defaults` 与 `values` 口径**不同**：`values` 是当前生效值（存过就取存的那份），
    `defaults` 是"没改过时会是什么"。后者必须取代码默认 —— 问读取点会得到当前值，
    「恢复默认」就成了空操作。这个接口不放字面数字（前端也不抄一份，读这里）。
    """
    return envelope({"values": _effective(), "stored": _stored(),
                     "editable": list(EDITABLE), "defaults": _defaults()},
                    request=request)


@router.put("/settings")
async def put_settings(request: Request, payload: dict = Body(...)):
    """更新设置：白名单校验后写 setting 表（重启后仍生效）。

    ⚠️ 写入时顺手**删掉同名 `kb.<key>` 那一行**。原因：服务层解析是"`kb.` 前缀优先、
    裸键回退"，如果库里已经有一行 `kb.unit_expand`（脚本按文档写法写进去的），
    界面这次写裸键 `unit_expand` 就会被前缀优先的那一行**盖住** —— 表现为"在界面上改了，
    值也回显了，但行为没变"。这是最难查的一类静默失效，所以在写入侧直接消灭歧义：
    每个设置**只有一行权威值**；谁想用 `kb.*` 写法就自己写那一行（它仍然优先）。
    """
    applied: dict = {}
    rejected: dict = {}
    with session_scope() as sess:
        for key, value in (payload or {}).items():
            caster = EDITABLE.get(key)
            if caster is None:
                rejected[key] = "不允许修改"
                continue
            # **空 = 恢复默认**（只对"没设过就用代码里的默认值"的键，当前是风格）：
            # 删掉那一行而不是存一段空字符串 —— 存空串会让模型失去身份设定，
            # 而"删掉"才与"从没设过"是同一种状态（读取点自然回落到代码里的默认）。
            if key == "agent_style" and not str(value or "").strip():
                for name in (key, f"kb.{key}"):
                    row = sess.get(Setting, name)
                    if row is not None:
                        sess.delete(row)
                applied[key] = ""
                continue
            try:
                if caster is bool:
                    value = value if isinstance(value, bool) else \
                        str(value).lower() in ("1", "true", "yes", "on")
                else:
                    value = caster(value)
            except (TypeError, ValueError):
                rejected[key] = f"类型错误，应为 {caster.__name__}"
                continue
            row = sess.get(Setting, key)
            if row is None:
                row = Setting(key=key)
                sess.add(row)
            row.value = json_dumps(value)
            stale = sess.get(Setting, f"kb.{key}")
            if stale is not None:
                sess.delete(stale)
            applied[key] = value
    return envelope({"applied": applied, "rejected": rejected,
                     "values": _effective()}, request=request)


@router.get("/settings/health")
async def settings_health(request: Request):
    from backend.repositories.mysql.session import healthcheck
    from backend.repositories.vector.lancedb_store import vector_store
    from backend.services.task.task_service import task_runner

    return envelope({
        "database": healthcheck(),
        "vector_store": vector_store.stats(),
        "task_runner": {"workers": task_runner.workers, "queues": task_runner.queues,
                        "running": task_runner.running_tasks},
        "data_dir": str(get_settings().data_dir),
    }, request=request)
