"""设置 API（规划 §20.7）：任务线程数 / RAG 参数 / 允许通用知识 / 数据目录。

注意：**翻译没有并发设置** —— 页级翻译固定串行（见 `core/config.py` 翻译段注释）。
"""

from __future__ import annotations

from fastapi import APIRouter, Body, Request

from backend.api.response import envelope
from backend.core.config import get_settings
from backend.core.utils import json_dumps, json_loads
from backend.repositories.mysql.models import Setting
from backend.repositories.mysql.session import session_scope

router = APIRouter(tags=["settings"])

# 允许通过 /settings 覆盖的白名单（避免任意键写入）
EDITABLE = {
    "task_worker_count": int,
    "render_dpi": int,
    "crop_dpi": int,
    "allow_general_knowledge": bool,
    "default_generation_model": str,
    "rag_top_k_vector": int,
    "rag_top_k_lexical": int,
    "rag_top_k_fused": int,
    "rag_top_n_rerank": int,
}


def _effective() -> dict:
    s = get_settings()
    stored = _stored()
    out = {}
    for key in EDITABLE:
        value = stored.get(key, getattr(s, key, None))
        out[key] = value
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


@router.get("/settings")
async def get_settings_api(request: Request):
    return envelope({"values": _effective(), "stored": _stored(),
                     "editable": list(EDITABLE)}, request=request)


@router.put("/settings")
async def put_settings(request: Request, payload: dict = Body(...)):
    """更新设置：白名单校验后写 setting 表（重启后仍生效）。"""
    applied: dict = {}
    rejected: dict = {}
    with session_scope() as sess:
        for key, value in (payload or {}).items():
            caster = EDITABLE.get(key)
            if caster is None:
                rejected[key] = "不允许修改"
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
