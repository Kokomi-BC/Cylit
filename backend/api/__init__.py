"""API 层（薄路由：校验、依赖注入、统一响应；不写业务逻辑，规划 §6.1）。"""

from backend.api.agent import router as agent_router
from backend.api.annotations import router as annotations_router
from backend.api.home import router as home_router
from backend.api.knowledge import router as knowledge_router
from backend.api.mineru import router as mineru_router
from backend.api.models import router as models_router
from backend.api.papers import router as papers_router
from backend.api.reader import router as reader_router
from backend.api.settings import router as settings_router
from backend.api.tasks import router as tasks_router

__all__ = [
    "agent_router", "papers_router", "reader_router", "tasks_router", "models_router",
    "annotations_router", "knowledge_router", "settings_router", "mineru_router",
    "home_router",
]
