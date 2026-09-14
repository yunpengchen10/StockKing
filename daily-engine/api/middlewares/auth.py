# -*- coding: utf-8 -*-
"""
Auth middleware: protect /api/v1/* when admin auth is enabled.
"""

from __future__ import annotations

import logging
import os
import secrets
from typing import Callable

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from src.auth import COOKIE_NAME, is_auth_enabled, verify_session

logger = logging.getLogger(__name__)

EXEMPT_PATHS = frozenset({
    "/api/v1/auth/login",
    "/api/v1/auth/status",
    "/api/health",
    "/api/v1/health",
    "/health",
    "/docs",
    "/redoc",
    "/openapi.json",
})


def _path_exempt(path: str) -> bool:
    """Check if path is exempt from auth."""
    normalized = path.rstrip("/") or "/"
    return normalized in EXEMPT_PATHS


class AuthMiddleware(BaseHTTPMiddleware):
    """Require valid session for /api/v1/* when auth is enabled."""

    async def dispatch(
        self,
        request: Request,
        call_next: Callable,
    ):
        path = request.url.path

        # The embedded Stock King sidecar is reachable only on loopback and is
        # additionally protected by a fresh per-process bearer value. Check it
        # before the optional Daily admin session so Vue never receives this
        # credential and cannot bypass the Wails bridge.
        sidecar_token = os.environ.get("STOCK_KING_SIDECAR_TOKEN", "").strip()
        if sidecar_token and path.startswith("/api/v1/"):
            supplied = request.headers.get("X-Stock-King-Token", "")
            if not supplied or not secrets.compare_digest(supplied, sidecar_token):
                return JSONResponse(
                    status_code=401,
                    content={
                        "error": "invalid_sidecar_token",
                        "message": "A valid Stock King sidecar token is required",
                    },
                )

        if not is_auth_enabled():
            return await call_next(request)

        if _path_exempt(path):
            return await call_next(request)

        if not path.startswith("/api/v1/"):
            return await call_next(request)

        cookie_val = request.cookies.get(COOKIE_NAME)
        if not cookie_val or not verify_session(cookie_val):
            return JSONResponse(
                status_code=401,
                content={
                    "error": "unauthorized",
                    "message": "Login required",
                },
            )

        return await call_next(request)


def add_auth_middleware(app):
    """Add auth middleware to protect API routes.

    The middleware is always registered; whether auth is enforced is determined
    at request time by is_auth_enabled() so the decision stays consistent across
    any runtime configuration reload.
    """
    app.add_middleware(AuthMiddleware)
