"""Web 路由聚合。"""
from .api import router as api
from .views import router as views
from .ws import router as ws

__all__ = ["api", "views", "ws"]
