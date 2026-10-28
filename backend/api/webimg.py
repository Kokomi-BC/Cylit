"""站外图片代理：GET /api/v1/webimg?url=<percent-encoded>

为什么走"本站再取一次"这条路（2026-12 收尾）：

- 回答里的站外图以前是**用户浏览器直连第三方**：IP / referer 泄露给图片站，对方一挂图就裂；
- 现在由本站抓一次落盘（backend/services/webimg.py），浏览器只跟本站打交道，
  顺带还拿到了固定 MIME 与 nosniff 这两层护栏。

失败一律走项目的统一错误信封（PaperMindError 子类，见 services/webimg.py 的六个异常）：
上游的状态码只进日志，回给前端的是我们自己的一句人话 —— 上游原文里可能有内网地址、
也可能只是一坨网关 HTML，整段吐给前端既没意义也不安全。
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter
from fastapi.responses import FileResponse

from backend.core.errors import ValidationError
from backend.services import webimg as webimg_service

router = APIRouter(tags=["webimg"])


@router.get("/webimg", response_class=FileResponse)
async def serve_webimg(url: str) -> FileResponse:
    """命中缓存直接回文件；没命中就抓一次（同步 IO 丢进线程池，不堵事件循环）。"""
    target = (url or "").strip()
    if not target:
        raise ValidationError("缺少图片地址（url 参数）。")
    # webimg.local_path 是同步的（httpx.Client + 磁盘写）：绝不能直接在事件循环里跑
    path = await asyncio.to_thread(webimg_service.local_path, target)
    headers = dict(webimg_service.CACHE_HEADERS)
    if path.suffix.lstrip(".").lower() == "svg":
        # SVG 是唯一"能被当成文档执行"的图片格式：名额靠魔数拿，另外再加一道沙箱头，
        # 这样即使用户把 /api/v1/webimg?url=… 直接贴进地址栏，脚本与外部资源也起不来。
        headers.update(webimg_service.SVG_HEADERS)
    return FileResponse(path, media_type=webimg_service.media_type_of(path),
                        headers=headers)
