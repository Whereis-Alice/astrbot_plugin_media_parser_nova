"""B站 CDN 镜像换源工具。

playurl 返回的媒体直链由 CDN 主机 + 签名路径组成。签名（``upsig`` /
``uparams``）只覆盖路径与查询参数，同一资源可通过更换主机名在多个
UPOS 镜像之间平移。境外出口 IP 常被路由到对境外限流（532 / 中途断流）
的镜像，此时保持路径与查询不变、只替换主机名即可恢复下载。
"""

from typing import List
from urllib.parse import urlsplit, urlunsplit


# 仅 B站 CDN 主机才参与换源，避免影响其它平台直链
_BILIBILI_CDN_SUFFIXES = (".bilivideo.com", ".bilivideo.cn", ".akamaized.net")

# 换源候选主机：Akamai 全球节点对境外出口最稳，其余为国内多线镜像备用
_FALLBACK_MIRROR_HOSTS = (
    "upos-hz-mirrorakam.akamaized.net",
    "upos-sz-mirrorali.bilivideo.com",
    "upos-sz-mirrorcos.bilivideo.com",
    "upos-sz-mirrorhw.bilivideo.com",
)

DEFAULT_MAX_MIRROR_CANDIDATES = 5


def is_bilibili_cdn_url(url: str) -> bool:
    """判断是否为 B站 CDN 直链（UPOS / PCDN 主机）。"""
    if not url or not isinstance(url, str):
        return False
    try:
        netloc = urlsplit(url).netloc
    except ValueError:
        return False
    if not netloc:
        return False
    host = netloc.rsplit("@", 1)[-1].split(":", 1)[0].lower()
    return host.endswith(_BILIBILI_CDN_SUFFIXES)


def swap_url_host(url: str, host: str) -> str:
    """保持 scheme/path/query/fragment 不变，替换 URL 主机。"""
    if not host:
        return url
    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    if not parts.netloc or not parts.hostname:
        return url
    return urlunsplit((parts.scheme, host, parts.path, parts.query, parts.fragment))


def build_mirror_candidates(
    url: str, max_candidates: int = DEFAULT_MAX_MIRROR_CANDIDATES
) -> List[str]:
    """为一个媒体直链生成按优先级排列的候选源，原始链接永远排第一。

    非 B站 CDN 直链只返回原链接，调用方的多候选重试逻辑对其完全无感。
    """
    if not url:
        return [url]
    try:
        limit = max(1, int(max_candidates))
    except (TypeError, ValueError):
        limit = DEFAULT_MAX_MIRROR_CANDIDATES
    candidates = [url]
    if not is_bilibili_cdn_url(url):
        return candidates
    try:
        current_host = (urlsplit(url).hostname or "").lower()
    except ValueError:
        return candidates
    for host in _FALLBACK_MIRROR_HOSTS:
        if host == current_host:
            continue
        candidate = swap_url_host(url, host)
        if candidate not in candidates:
            candidates.append(candidate)
        if len(candidates) >= limit:
            break
    return candidates
