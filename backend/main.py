"""FastAPI 应用入口（规划 §6 架构、§20 API）。

单进程：挂载 REST + SSE + 静态资源 + TaskRunner 启停。
"""

from __future__ import annotations

import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from backend.api import (
    agent_router, annotations_router, home_router, knowledge_router, mineru_router,
    models_router, papers_router, reader_router, settings_router, tasks_router,
)
from backend.api.folders import router as folders_router
from backend.core.config import get_settings
from backend.core.errors import NotFoundError, PaperMindError, provider_detail_of
from backend.core.logging import get_logger, setup_logging
from backend.core.utils import json_dumps

#: 前端 SPA 的挂载前缀。**必须与 `frontend/vite.config.ts` 的 `base` 逐字一致** ——
#: 两处不一致时浏览器会去根路径拿 assets，表现是"白屏 + 一堆 404"。
#: 2026-10-15 由 `/app` 改成 `/cylit`（用项目名，也和门户首页的跳转目标对齐）。
UI_BASE = "/cylit"

log = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    s = get_settings()
    setup_logging()
    s.ensure_dirs()
    log.info("启动 %s v%s（data=%s）", s.app_name, s.app_version, s.data_dir)

    # 技能库：`ensure_dirs()` 已经把自带技能**幂等**物化进 `<data_dir>/skills`
    # （内容哈希没变就什么都不做；用户改过的副本不覆盖）。这里只把结果说清楚 ——
    # 失败只记日志，技能只影响问答效果，不该连累主后端起不来。
    from backend.services.agent import skills_store

    try:
        skills = skills_store.list_skills(s)
        log.info("技能库 %s：%d 个（自带 %d）", s.agent_skills_dir, len(skills),
                 sum(1 for item in skills if item["builtin"]))
    except Exception as exc:  # noqa: BLE001
        log.warning("技能库概览读取失败（忽略）：%s", exc)

    # 数据库就绪 + 任务系统
    from backend.repositories.mysql.session import init_db

    try:
        info = init_db()
        log.info("业务库就绪：%s", info["backend"])
    except Exception as exc:  # noqa: BLE001
        log.error("数据库初始化失败：%s", exc)

    # 提供方 / 密钥迁移：把老库里散落的密钥文件名归到 provider 名下（幂等，只建不改）。
    # 必须在 init_db 之后跑（新表就是它建出来的）；失败只记日志 —— 迁移没跑通时
    # 解析链会退回 legacy 回落，模型照样能用，不该连累后端起不来。
    from backend.services.model import key_store

    try:
        migrated = key_store.ensure_keys()
        log.info("模型密钥迁移：%s", migrated)
    except Exception as exc:  # noqa: BLE001
        log.warning("模型密钥迁移失败（忽略，解析链会走历史回落）：%s", exc)

    from backend.services.task.handlers import register_all
    from backend.services.task.task_service import task_runner

    # 问答 agent 的内部令牌：启动时**生成一次**（缺失时）。
    # 不生成的话 agent 服务起不来（它启动就要校验令牌），而用户没有任何理由手动造这个文件。
    if s.agent_enabled:
        s.agent_token(create=True)
        log.info("问答 agent：http://%s:%s（%s）", s.agent_host, s.agent_port,
                 "令牌已就绪" if s.agent_token() else "令牌缺失")

    register_all()
    task_runner.start()
    try:
        yield
    finally:
        task_runner.stop()
        log.info("已停止 %s", s.app_name)


def create_app() -> FastAPI:
    s = get_settings()
    setup_logging()
    app = FastAPI(
        title=f"{s.app_name} API",
        version=s.app_version,
        description="智能论文阅读与知识管理系统 —— 后端（解析 / 逐段翻译 / 任务 / 模型）",
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],          # 本地单用户应用
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # ---------------- 统一响应 {data, error, trace_id}（规划 §20 约定）---------
    @app.middleware("http")
    async def envelope(request: Request, call_next):
        trace_id = uuid.uuid4().hex[:12]
        request.state.trace_id = trace_id
        t0 = time.perf_counter()
        try:
            response = await call_next(request)
        except PaperMindError as exc:
            return _error_response(exc, trace_id)
        response.headers["x-trace-id"] = trace_id
        response.headers["x-elapsed-ms"] = f"{(time.perf_counter() - t0) * 1000:.1f}"
        return response

    @app.exception_handler(PaperMindError)
    async def _handle_papermind(request: Request, exc: PaperMindError):
        return _error_response(exc, getattr(request.state, "trace_id", ""))

    @app.exception_handler(RequestValidationError)
    async def _handle_validation(request: Request, exc: RequestValidationError):
        return JSONResponse(status_code=422, content={
            "data": None,
            "error": {"code": "VALIDATION_ERROR", "message": "请求参数校验失败",
                      # provider 恒为 null：参数校验没有供应商语义，但字段要在 ——
                      # 前端只按 error.detail.provider 一种读法取分类，缺键会掉进兜底
                      "detail": {"errors": exc.errors()[:10], "provider": None}},
            "trace_id": getattr(request.state, "trace_id", ""),
        })

    @app.exception_handler(Exception)
    async def _handle_unexpected(request: Request, exc: Exception):
        log.exception("未处理异常：%s", exc)
        return JSONResponse(status_code=500, content={
            "data": None,
            "error": {"code": "INTERNAL_ERROR",
                      "message": f"{type(exc).__name__}: {str(exc)[:300]}",
                      "detail": {}},
            "trace_id": getattr(request.state, "trace_id", ""),
        })

    # ---------------- 路由 ----------------
    app.include_router(papers_router, prefix=s.api_prefix)
    # 论文自定义分类（文件夹）
    app.include_router(folders_router, prefix=s.api_prefix)
    app.include_router(reader_router, prefix=s.api_prefix)
    app.include_router(tasks_router, prefix=s.api_prefix)
    app.include_router(models_router, prefix=s.api_prefix)
    app.include_router(annotations_router, prefix=s.api_prefix)
    app.include_router(knowledge_router, prefix=s.api_prefix)
    # 首页刊头的数据 rail：几条 COUNT，只读
    app.include_router(home_router, prefix=s.api_prefix)
    app.include_router(settings_router, prefix=s.api_prefix)
    # 问答 agent：对外 /agent/ask（SSE 转发）、/agent/conversations；对内 /agent/papers/... 只读投影
    app.include_router(agent_router, prefix=s.api_prefix)
    # 在线解析（MinerU）的配置/校验：只挂带前缀的那一份 —— 它没有"直接打开的资源链接"，
    # 不像 /papers 那样需要无前缀别名
    app.include_router(mineru_router, prefix=s.api_prefix)

    # 兼容：同时挂一份到 /papers 等无前缀路径（便于直接打开资源链接）
    app.include_router(reader_router)
    app.include_router(papers_router)

    @app.get("/health", tags=["system"])
    async def health():
        from backend.repositories.mysql.session import healthcheck
        from backend.services.task.task_service import task_runner

        return {
            "data": {
                "ok": True, "app": s.app_name, "version": s.app_version,
                "database": healthcheck(),
                "task_runner": {"workers": task_runner.workers,
                                "queues": task_runner.queues,
                                "running": task_runner.running_tasks},
                "data_dir": str(s.data_dir),
            },
            "error": None, "trace_id": "",
        }

    _mount_static(app)
    return app


def _error_response(exc: PaperMindError, trace_id: str) -> JSONResponse:
    """统一错误信封 {data, error, trace_id}。

    error.detail.provider 与 SSE error 帧共用 provider_errors 的同一份字段：
    to_dict() 已经带上了，这里只兜住"子类覆写了 to_dict"的情况 —— 缺了它前端
    就得回去猜 code，两套读法立刻分叉。
    """
    body = exc.to_dict()
    detail = body.get("detail")
    if isinstance(detail, dict) and "provider" not in detail:
        provider = provider_detail_of(exc)
        if provider is not None:
            detail["provider"] = provider
    return JSONResponse(status_code=exc.http_status, content={
        "data": None, "error": body, "trace_id": trace_id,
    })


def _mount_static(app: FastAPI) -> None:
    """受控静态路由：前端构建产物 + data/ 下论文资源（规划 §6.2 第 3 条硬约束）。

    前端是 Vite 构建的 SPA（history 路由，`base=UI_BASE`）：
    - `<UI_BASE>/<file>` 命中 `frontend/dist/` 里的真实文件就直接返回（assets/*.js|css、字体…）；
    - 否则回退 `index.html`，让前端路由自己解析（`/cylit/reader/xxx` 刷新不会 404）；
    - 未构建时给出**明确的构建指引页**（503），不静默 404、也不暴露 `node_modules`。

    `UI_BASE` 必须与 `frontend/vite.config.ts` 的 `base` **逐字一致**：
    改了这里没改那里，浏览器会去根路径拿 assets，全是 404。
    """
    s = get_settings()
    papers_dir = s.papers_dir
    papers_dir.mkdir(parents=True, exist_ok=True)
    app.mount("/papers", StaticFiles(directory=str(papers_dir), html=False),
              name="papers")

    dist = Path(s.project_root) / "frontend" / "dist"
    index_file = dist / "index.html"

    @app.get("/", include_in_schema=False)
    async def _root_redirect():
        return RedirectResponse(UI_BASE + "/")

    @app.get(UI_BASE, include_in_schema=False)
    async def _frontend_redirect():
        return RedirectResponse(UI_BASE + "/")

    if not index_file.is_file():
        @app.get(UI_BASE + "/{path:path}", include_in_schema=False)
        async def _frontend_not_built(path: str):  # noqa: ARG001
            return HTMLResponse(_BUILD_HINT_HTML, status_code=503)
        return

    dist_root = dist.resolve()

    # 缓存策略：**index.html 一律回源校验**（`no-cache` + ETag，命中就是 304，很便宜），
    # `assets/` 下的文件名带内容哈希，可以长缓存 `immutable`。
    # 这不是性能优化而是**正确性**：以前 index.html 一条 Cache-Control 都没有，浏览器按
    # 启发式规则把它缓存住，于是"前端改完、用户刷新看到的还是旧界面"真实发生过
    # （用户截图里的首页还是旧构建，同一个问题被反复报）。文件名带哈希的 assets 一旦和
    # index.html 的引用错位，用户就会一直跑旧 bundle，且没有任何报错。
    _NO_CACHE = {"Cache-Control": "no-cache"}
    _IMMUTABLE = {"Cache-Control": "public, max-age=31536000, immutable"}

    def _dist_file(file: Path, path: str = "") -> FileResponse:
        return FileResponse(file, headers=_IMMUTABLE if path.startswith("assets/") else _NO_CACHE)

    @app.get(UI_BASE + "/{path:path}", include_in_schema=False)
    async def _frontend_spa(path: str):
        if path:
            candidate = (dist / path).resolve()
            try:
                candidate.relative_to(dist_root)
            except ValueError:                      # 路径穿越：一律回退到入口
                return _dist_file(index_file)
            if candidate.is_file():
                return _dist_file(candidate, path)
            # 带扩展名的资源（assets/*.js、字体、图片）缺失时应当 404：
            # 返回 index.html 会让浏览器把 HTML 当 JS/CSS 解析，报出更难懂的 MIME 错误。
            if "." in Path(path).name:
                raise NotFoundError(f"静态资源不存在：{path}")
        return _dist_file(index_file)


_BUILD_HINT_HTML = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>前端尚未构建</title>
<style>body{font-family:system-ui,"Microsoft YaHei",sans-serif;max-width:720px;margin:80px auto;line-height:1.7;color:#1c2027}
code{background:#eef0f4;padding:2px 6px;border-radius:4px}pre{background:#eef0f4;padding:12px;border-radius:8px;overflow:auto}</style>
</head><body>
<h2>前端尚未构建（API 正常可用）</h2>
<p>后端已启动，但没有找到 <code>frontend/dist/index.html</code>。请先构建前端：</p>
<pre>cd frontend
npm install        # 首次；本机 npm 已配置 npmmirror 镜像
npm run build      # 产物输出到 frontend/dist</pre>
<p>然后刷新本页。<code>pr.py serve</code> 会自动托管 <code>frontend/dist</code>。</p>
<p>开发模式（热更新）也可以：<code>cd frontend &amp;&amp; npm run dev</code>，访问 dev server 打印的地址
（已把 <code>/api</code>、<code>/papers</code> 代理到 8710）。</p>
<p>接口文档：<a href="/docs">/docs</a> ｜ 健康检查：<a href="/health">/health</a></p>
</body></html>
"""


app = create_app()
