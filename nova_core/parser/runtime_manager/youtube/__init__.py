"""YouTube 运行时管理入口。"""
from .cookie import (
    IDENTITY_COOKIE_NAMES,
    ROTATING_COOKIE_NAMES,
    SAPISID_COOKIE_NAMES,
    YOUTUBE_ORIGIN,
    YouTubeCookieRuntime,
    build_sapisid_authorization,
    collect_set_cookie_headers,
    parse_cookie_header,
)

__all__ = [
    "IDENTITY_COOKIE_NAMES",
    "ROTATING_COOKIE_NAMES",
    "SAPISID_COOKIE_NAMES",
    "YOUTUBE_ORIGIN",
    "YouTubeCookieRuntime",
    "build_sapisid_authorization",
    "collect_set_cookie_headers",
    "parse_cookie_header",
]
