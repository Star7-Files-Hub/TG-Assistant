"""Web 依赖注入。"""
from __future__ import annotations

from typing import Any

from fastapi import Depends, Request


def get_state(request: Request) -> Any:
    return request.app.state


def get_settings(request: Request) -> Any:
    return request.app.state.settings


def get_paths(request: Request) -> Any:
    return request.app.state.paths


def get_store(request: Request) -> Any:
    return request.app.state.store


def get_runtime(request: Request) -> Any:
    return request.app.state.runtime


def get_web_settings(request: Request) -> Any:
    return request.app.state.web_settings


__all__ = [
    "get_state",
    "get_settings",
    "get_paths",
    "get_store",
    "get_runtime",
    "get_web_settings",
]
