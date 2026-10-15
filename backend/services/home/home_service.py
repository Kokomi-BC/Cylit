"""首页概览服务：**只读**的几条 COUNT，给首页刊头的数据 rail 用。

为什么单开一个口子，而不是复用现成的两个 overview：首页右侧那行数字分属 `paper` /
`reading_progress` / `note` / `highlight` / `understanding` 五张表，而现有接口里没有一个是
"只数个数"的 ——

- `/notes/overview` 会把最多 200 条笔记 + 200 条高亮 + 200 条批注**整包**返回：首页只用得上
  一个数，却要收三份正文；而且它带 `limit(200)`，拿 `len()` 当数字会**数不准**；
- `/knowledge/overview` 要走向量库、算语种/路号分布，比"数个数"重得多。

这里只做 COUNT，全部命中主键或普通索引，不返回任何正文。

口径（一处事实源，要改就改这里）：
- `papers`：未删除的论文数；
- `readings`：`reading_progress` 里有行的论文数 = "翻开过"的篇数；
- `notes`：当前用户的笔记数；
- `highlights`：当前用户的高亮**处**数 —— 一次划词跨了多行会拆成多条共用 `group_id`
  （见 `models.Highlight` 的注释），所以按 `coalesce(group_id, id)` 去重，
  否则"标了一处"会被数成两三处；
- `understandings`：已生成解读的**篇**数（主键是 `{paper_id}:{path}`，按 `paper_id` 去重）。
"""

from __future__ import annotations

from sqlalchemy import func

from backend.repositories.mysql.models import (
    Highlight, Note, Paper, ReadingProgress, Understanding,
)
from backend.repositories.mysql.session import session_scope


def summary(user_id: int = 1) -> dict:
    """首页要用的几个数；单个用户库，`user_id` 与其它服务保持同一个默认值。"""
    with session_scope() as sess:
        papers = int(sess.query(func.count(Paper.id))
                     .filter(Paper.deleted_at.is_(None)).scalar() or 0)
        readings = int(sess.query(func.count(ReadingProgress.paper_id))
                       .filter(ReadingProgress.user_id == user_id).scalar() or 0)
        notes = int(sess.query(func.count(Note.id))
                    .filter(Note.user_id == user_id).scalar() or 0)
        highlights = int(sess.query(func.count(
            func.distinct(func.coalesce(Highlight.group_id, Highlight.id))))
            .filter(Highlight.user_id == user_id).scalar() or 0)
        understandings = int(sess.query(func.count(
            func.distinct(Understanding.paper_id))).scalar() or 0)
    return {
        "papers": papers,
        "readings": readings,
        "notes": notes,
        "highlights": highlights,
        "understandings": understandings,
    }
