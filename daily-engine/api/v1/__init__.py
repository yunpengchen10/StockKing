# -*- coding: utf-8 -*-
"""
===================================
API v1 模块初始化
===================================

职责：
1. 导出 v1 版本 API 的路由
"""

__all__ = ["api_v1_router"]


def __getattr__(name: str):
    """Avoid loading every optional endpoint when one lightweight module is imported."""
    if name == "api_v1_router":
        from api.v1.router import router

        return router
    raise AttributeError(name)
