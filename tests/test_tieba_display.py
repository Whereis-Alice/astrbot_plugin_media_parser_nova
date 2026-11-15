from nova_core.card import build_model
from nova_core.message_adapter.node_builder import build_text_node
from nova_core.rika_render.adapter import build_parse_result


def metadata():
    return {
        "platform": "tieba", "title": "帖子标题", "author": "主楼作者",
        "desc": "原始主楼正文", "forum_name": "星之卡比", "stats_line": "👍9 💬2",
    }


def test_forum_is_visible_without_becoming_author_or_changing_original_body():
    data = metadata()
    node = build_text_node(data)
    model = build_model(build_parse_result(data), {})
    assert "贴吧：星之卡比吧" in node.text
    assert "作者：主楼作者" in node.text
    assert model.author_name == "主楼作者"
    assert model.body == "贴吧：星之卡比吧\n\n原始主楼正文"
    assert data["desc"] == "原始主楼正文"
    assert ("点赞", "9") in model.stats
    assert ("评论", "2") in model.stats


def test_forum_follows_author_visibility_and_is_specific_to_tieba():
    data = metadata()
    data["_text_metadata_fields"] = {"author": False}
    assert "星之卡比" not in build_text_node(data).text
    assert "星之卡比" not in build_model(build_parse_result(data), {}).body
    data.pop("_text_metadata_fields")
    data["platform"] = "steam"
    assert "星之卡比" not in build_text_node(data).text
    assert "星之卡比" not in build_model(build_parse_result(data), {}).body
