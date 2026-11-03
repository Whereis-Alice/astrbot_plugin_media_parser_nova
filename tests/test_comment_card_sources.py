from types import SimpleNamespace

import pytest

from nova_core.card import build_model
from nova_core.card.model import normalize_comments
from test_card_skins import render_probe


def result(title="弹幕摘选", label="Nicoる"):
    return SimpleNamespace(
        platform=SimpleNamespace(name="niconico", display_name="niconico"),
        title="视频标题", text="简介", url="https://www.nicovideo.jp/watch/sm9", author=None,
        contents=[], graphics=[], timestamp=None,
        extra={"comment_section_title": title, "hot_comments": [{"username": "弹幕用户", "message": "弹幕正文", "likes": 2, "likes_label": label, "position_text": "弹幕位置 00:32"}]},
    )


@pytest.mark.parametrize("theme_key", ["aurora", "bilibili", "x"])
def test_comment_source_and_real_metric_survive_card_render(theme_key):
    model = build_model(result(), {}, watermark="Nova", show_watermark=True, show_url=True)
    probe = render_probe(model, theme_key=theme_key, width=640)
    assert "弹幕摘选" in probe.joined_text
    assert "Nicoる: 2" in probe.joined_text
    assert "弹幕位置 00:32" in probe.joined_text
    if theme_key in {"bilibili", "x"}:
        assert "近期弹幕" in probe.joined_text


def test_unknown_likes_remain_unknown_and_legacy_labels_are_preserved():
    data = result()
    data.extra["hot_comments"][0]["likes"] = None
    assert normalize_comments(data, 180)[0].likes == "未知"
    data.extra["hot_comments"][0]["likes_text"] = "1.1K"
    assert normalize_comments(data, 180)[0].likes == "1.1K"
