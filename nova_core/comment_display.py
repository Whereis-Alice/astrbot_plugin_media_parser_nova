"""评论来源、互动数字和位置的统一展示规则。"""

from typing import Any, Dict


def comment_section_title(metadata: Dict[str, Any], default: str = "热评") -> str:
    title = str(metadata.get("comment_section_title") or "").strip()
    return " ".join(title.split())[:32] or default


def comment_likes_text(comment: Dict[str, Any]) -> str:
    explicit = str(comment.get("likes_text") or "").strip()
    if explicit:
        return " ".join(explicit.split())[:24]
    value = comment.get("likes")
    if value is None or isinstance(value, bool):
        return "未知"
    if isinstance(value, float) and not value.is_integer():
        return "未知"
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        return "未知"
    if number < 0:
        return "未知"
    return str(number)


def comment_likes_label(comment: Dict[str, Any]) -> str:
    value = str(comment.get("likes_label") or "点赞").strip()
    return " ".join(value.split())[:16] or "点赞"
