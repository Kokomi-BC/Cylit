"""论文自定义分类（文件夹）的服务层：建/改名/删 + 批量归类 + 计数。

单用户系统（库里没有 user 表），所以分类**全局唯一**；一篇论文最多属于一个分类，
只有一层、不做嵌套。删分类只解绑论文（``folder_id`` 置 NULL），论文本身不删、不软删。

为什么没有 repository 类：仓库里 ``paper_service`` / ``annotation_service`` 都是"服务层直接开
``session_scope()``"，这里照抄同一写法；多一层转发只是把 SQL 挪个地方。
"""

from __future__ import annotations

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError

from backend.core.errors import ConflictError, NotFoundError, ValidationError
from backend.core.utils import new_id
from backend.repositories.mysql.models import Paper, PaperFolder
from backend.repositories.mysql.session import session_scope

#: 分类名长度上限（trim 之后按**字符**数算，中文一个字算一个）
NAME_MAX = 32


def _clean_name(raw) -> str:
    """校验并归一分类名：trim 后为空 / 超长一律 400。"""
    name = (raw or "").strip() if isinstance(raw, str) else ""
    if not name:
        raise ValidationError("分类名不能为空")
    if len(name) > NAME_MAX:
        raise ValidationError(f"分类名最多 {NAME_MAX} 个字符")
    return name


def _folder_dict(row: PaperFolder, paper_count: int | None = None) -> dict:
    data = {"id": row.id, "name": row.name, "sort": int(row.sort or 0)}
    if paper_count is not None:
        data["paper_count"] = paper_count
    return data


def _counts(sess) -> dict:
    """``folder_id -> 论文数``（键 None = 未分类，不含软删的论文）。"""
    rows = (sess.query(Paper.folder_id, func.count(Paper.id))
            .filter(Paper.deleted_at.is_(None))
            .group_by(Paper.folder_id).all())
    return {key: int(num) for key, num in rows}


def list_folders() -> dict:
    """分类列表 + 每类的论文数 + 未分类论文数。

    计数的实现**只在这里有一份**（api 层直接透传）：让路由层再数一遍，
    两个口径迟早会漂成"列表显示 3 篇、筛出来 4 篇"。
    """
    with session_scope() as sess:
        counts = _counts(sess)
        rows = (sess.query(PaperFolder)
                .order_by(PaperFolder.sort.asc(), PaperFolder.created_at.asc()).all())
        items = [_folder_dict(r, counts.get(r.id, 0)) for r in rows]
        unclassified = counts.get(None, 0)
    return {"items": items, "total": len(items), "unclassified": unclassified}


def create_folder(payload: dict) -> dict:
    name = _clean_name(payload.get("name"))
    with session_scope() as sess:
        if sess.query(PaperFolder.id).filter(PaperFolder.name == name).first():
            raise ConflictError(f"分类「{name}」已存在")
        # 新分类排在最后：sort 取当前最大值 +1（用户没提手工排序，先按创建顺序给个数）
        newest = int(sess.query(func.max(PaperFolder.sort)).scalar() or 0)
        row = PaperFolder(id=new_id("folder"), name=name, sort=newest + 1)
        sess.add(row)
        try:
            sess.flush()
        except IntegrityError as exc:  # 并发同名：靠唯一索引兜底，不能漏成 500
            raise ConflictError(f"分类「{name}」已存在") from exc
        return _folder_dict(row, 0)


def rename_folder(folder_id: str, payload: dict) -> dict:
    name = _clean_name(payload.get("name"))
    with session_scope() as sess:
        row = sess.get(PaperFolder, folder_id)
        if row is None:
            raise NotFoundError(f"分类不存在：{folder_id}")
        dup = (sess.query(PaperFolder.id)
               .filter(PaperFolder.name == name, PaperFolder.id != folder_id).first())
        if dup:
            raise ConflictError(f"分类「{name}」已存在")
        row.name = name
        try:
            sess.flush()
        except IntegrityError as exc:
            raise ConflictError(f"分类「{name}」已存在") from exc
        return _folder_dict(row, _counts(sess).get(row.id, 0))


def delete_folder(folder_id: str) -> dict:
    """删分类：里面的论文先**解绑**（folder_id 置 NULL），论文不删、不软删。

    按 ``folder_id`` 整表更新（不只看未删的论文）：软删在回收站里的论文也得解绑，
    否则它被恢复后又"属于"一个已经不存在的分类。
    """
    with session_scope() as sess:
        row = sess.get(PaperFolder, folder_id)
        if row is None:
            raise NotFoundError(f"分类不存在：{folder_id}")
        moved = (sess.query(Paper)
                 .filter(Paper.folder_id == folder_id)
                 .update({Paper.folder_id: None}, synchronize_session=False))
        sess.delete(row)
        sess.flush()
        return {"moved": int(moved or 0)}


def assign_papers(paper_ids: list | None, folder_id: str | None) -> dict:
    """批量归类；``folder_id=None`` = 移回未分类。返回真正被改到的篇数。

    分类不存在直接 404（而不是"把论文挂到一个不存在的分类"）——那种数据在列表里
    既不属于任何分类、又不在未分类里，等于凭空消失。
    """
    ids = [str(pid).strip() for pid in (paper_ids or []) if str(pid).strip()]
    if not ids:
        raise ValidationError("paper_ids 不能为空")
    with session_scope() as sess:
        if folder_id is not None and sess.get(PaperFolder, folder_id) is None:
            raise NotFoundError(f"分类不存在：{folder_id}")
        updated = (sess.query(Paper)
                   .filter(Paper.id.in_(ids), Paper.deleted_at.is_(None))
                   .update({Paper.folder_id: folder_id}, synchronize_session=False))
        return {"updated": int(updated or 0)}
