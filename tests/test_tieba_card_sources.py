from types import SimpleNamespace

import pytest

from nova_core.card import build_model
from test_card_skins import render_probe


@pytest.mark.parametrize("theme_key", ["aurora", "bilibili", "x"])
def test_tieba_card_labels_first_batch_and_keeps_real_floor(theme_key):
    result = SimpleNamespace(
        platform=SimpleNamespace(name="tieba", display_name="贴吧"),
        title="帖子标题", text="主楼正文", url="https://tieba.baidu.com/p/11180655858",
        author=None, contents=[], graphics=[], timestamp=None,
        extra={
            "comment_section_title": "楼层回复",
            "hot_comments": [{
                "username": "回帖用户", "message": "楼层正文", "likes": None,
                "position_text": "2楼", "time": "2026-11-15 12:00:00",
            }],
        },
    )
    model = build_model(result, {})
    probe = render_probe(model, theme_key=theme_key, width=640)
    assert "楼层回复" in probe.joined_text
    assert "2楼" in probe.joined_text
    assert "未知" in probe.joined_text
    if theme_key in {"bilibili", "x"}:
        assert "首批回复" in probe.joined_text
