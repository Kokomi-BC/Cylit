"""论文自定义分类（文件夹）API（薄路由：校验在服务层，这里只管收参和统一响应）。"""

from __future__ import annotations

from fastapi import APIRouter, Body, Request

from backend.api.response import envelope
from backend.services.paper import folder_service

router = APIRouter(tags=["folders"])


@router.get("/folders")
async def list_folders(request: Request):
    """全部分类 + 未分类论文数（计数的唯一实现在 folder_service，这里不重数）。"""
    return envelope(folder_service.list_folders(), request=request)


@router.post("/folders")
async def create_folder(request: Request, payload: dict = Body(...)):
    return envelope(folder_service.create_folder(payload),
                    message="分类已创建", request=request)


@router.patch("/folders/{folder_id}")
async def rename_folder(folder_id: str, request: Request, payload: dict = Body(...)):
    return envelope(folder_service.rename_folder(folder_id, payload),
                    message="分类已改名", request=request)


@router.delete("/folders/{folder_id}")
async def delete_folder(folder_id: str, request: Request):
    """删分类；里面的论文回到未分类（返回 ``moved`` = 解绑了几篇）。"""
    return envelope(folder_service.delete_folder(folder_id),
                    message="分类已删除", request=request)
