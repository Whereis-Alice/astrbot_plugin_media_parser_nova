"""Tieba parser boundaries and reduced real official JSON fixtures.

Fixtures were read anonymously from Baidu on 2026-11-15. The three threads
exercise text/image, image-only main floor, and actual type-5 MP4 video.
Tests use no network or local research files.
"""

import asyncio
import copy
import hashlib
import json
import unittest
from unittest.mock import AsyncMock, patch

from nova_core.parser.platform.tieba import TIEBA_API, TiebaParser

REAL_RESPONSES = {
    "11180655858": {
        "error_code": 0,
        "error_msg": "",
        "thread": {
            "id": 11180655858,
            "title": "正在尝试复活",
            "post_id": 154123049678,
            "create_time": 1794203153,
            "agree": {
                "has_agree": 0,
                "agree_type": 0,
                "disagree_num": 0,
                "diff_agree_num": 4,
                "agree_num": 4,
            },
            "reply_num": 2,
            "author": {
                "id": 595970344,
                "name": "Adnini983",
                "name_show": "Adnini983",
                "portrait": "tb.1.f917ef71.CR_0LkesgHiMaWgJRB5mOw?t=1794740357",
            },
        },
        "forum": {"id": 89278, "name": "星之卡比"},
        "post_list": [
            {
                "id": 154123049678,
                "floor": 1,
                "time": 1794203153,
                "author_id": 595970344,
                "agree": {
                    "agree_num": 4,
                    "has_agree": 0,
                    "agree_type": 0,
                    "disagree_num": 0,
                    "diff_agree_num": 4,
                },
                "need_log": 0,
                "is_post_visible": 0,
                "content": [
                    {
                        "type": 0,
                        "text": "是个10年前的一个同人游戏 玩法就是答题 但它在2021年下线了\n"
                        "不过我正在努力抢救它 希望把它恢复成能玩的状态 到时候再发出来给大家玩",
                    },
                    {"type": 2, "text": "image_emoticon24", "c": "太开心"},
                    {
                        "type": 3,
                        "origin_src": "https://tiebapic.baidu.com/forum/pic/item/3cd7a71ea8d3fd1f1434cd32764e251f95ca5f51.jpg?tbpicau=2026-11-26-05_7978e28e3ef0a124dd1acf4ac440a936",
                        "big_cdn_src": "https://tiebapic.baidu.com/forum/w%3D960%3Bq%3D100/sign=e6dec6ba9758ccbf1bbcb93c29e3cd03/3cd7a71ea8d3fd1f1434cd32764e251f95ca5f51.jpg?tbpicau=2026-11-26-05_c6b740763078ccd7d41ad80e407cd426",
                    },
                ],
            },
            {
                "id": 154123207801,
                "floor": 2,
                "time": 1794206291,
                "author_id": 139452280,
                "agree": {
                    "agree_type": 0,
                    "disagree_num": 0,
                    "diff_agree_num": 0,
                    "agree_num": 0,
                    "has_agree": 0,
                },
                "need_log": 0,
                "is_post_visible": 0,
                "content": [
                    {"type": 2, "text": "image_emoticon13", "c": "真棒"},
                    {"type": 0, "text": "抢救失传媒体好评"},
                ],
            },
        ],
        "user_list": [
            {
                "id": 595970344,
                "name": "Adnini983",
                "name_show": "Adnini983",
                "portrait": "tb.1.f917ef71.CR_0LkesgHiMaWgJRB5mOw?t=1794740357",
            },
            {
                "id": 139452280,
                "name": "孤独的滑膛枪",
                "name_show": "孤独的滑膛枪",
                "portrait": "tb.1.610e3440.A7VMYZKRhDTaqRImQLkw2Q?t=1794740357",
            },
            {"id": 20005959584503, "portrait": "tb.1.ce876919.laSe5jSNEvbNu1yvpwoJhw"},
        ],
    },
    "7379611390": {
        "error_code": 0,
        "error_msg": "",
        "thread": {
            "id": 7379611390,
            "title": "求助！！！镜之迷宫，这个们要怎么开？",
            "post_id": 139552681040,
            "create_time": 1622267038,
            "agree": {
                "agree_num": 2,
                "has_agree": 0,
                "agree_type": 0,
                "disagree_num": 0,
                "diff_agree_num": 2,
            },
            "reply_num": 5,
            "author": {
                "id": 564739004,
                "name": "飞翔只为梦想",
                "name_show": "飞翔只为梦想",
                "portrait": "tb.1.2ffb4421.vKGKJ0UjQ5ICuygq4icfBw?t=1794740356",
            },
        },
        "forum": {"id": 89278, "name": "星之卡比"},
        "post_list": [
            {
                "id": 139552681040,
                "floor": 1,
                "time": 1622267038,
                "author_id": 564739004,
                "agree": {
                    "agree_type": 0,
                    "disagree_num": 0,
                    "diff_agree_num": 2,
                    "agree_num": 2,
                    "has_agree": 0,
                },
                "need_log": 0,
                "is_post_visible": 0,
                "content": [
                    {
                        "type": 3,
                        "origin_src": "https://tiebapic.baidu.com/forum/pic/item/200d0eb30f2442a749924b17c643ad4bd0130244.jpg?tbpicau=2026-11-26-05_cf84ce59aeb76c6cd6bf80b0e5195b1c",
                        "big_cdn_src": "https://tiebapic.baidu.com/forum/w%3D1920%3Bq%3D100/sign=89793fbca63533faf5b697279ae3c66e/200d0eb30f2442a749924b17c643ad4bd0130244.jpg?tbpicau=2026-11-26-05_bd8edc0ba831e8f073c4226c01c09dcd",
                    }
                ],
            },
            {
                "id": 139561537689,
                "floor": 2,
                "time": 1622302706,
                "author_id": 3892174370,
                "agree": {
                    "disagree_num": 0,
                    "diff_agree_num": 0,
                    "agree_num": 0,
                    "has_agree": 0,
                    "agree_type": 0,
                },
                "need_log": 0,
                "is_post_visible": 0,
                "content": [
                    {"type": 0, "text": "按我图中顺序走就能到了"},
                    {
                        "type": 3,
                        "origin_src": "https://tiebapic.baidu.com/forum/pic/item/8ba6e7cd7b899e518d9681a655a7d933c9950dc0.jpg?tbpicau=2026-11-26-05_84d90ed7ab066be15fd0708a86c1d160",
                        "big_cdn_src": "https://tiebapic.baidu.com/forum/w%3D1920%3Bq%3D100/sign=4e9ae203970a19d8cb03800c01cab9fa/8ba6e7cd7b899e518d9681a655a7d933c9950dc0.jpg?tbpicau=2026-11-26-05_ba4116b074401303ee3b08517d267992",
                    },
                    {
                        "type": 3,
                        "origin_src": "https://tiebapic.baidu.com/forum/pic/item/2073060828381f305e01361bbe014c086f06f0c1.jpg?tbpicau=2026-11-26-05_0bef69813fd3141227c51198a06abd40",
                        "big_cdn_src": "https://tiebapic.baidu.com/forum/w%3D1920%3Bq%3D100/sign=be01259a802bd40742c7d7f449b9a520/2073060828381f305e01361bbe014c086f06f0c1.jpg?tbpicau=2026-11-26-05_5ef41d16d32a9aa989c808e06d419e20",
                    },
                    {
                        "type": 3,
                        "origin_src": "https://tiebapic.baidu.com/forum/pic/item/ef61279759ee3d6d84de3c7954166d224e4adec1.jpg?tbpicau=2026-11-26-05_3eef94b11cabe39686ddbfd60e66be87",
                        "big_cdn_src": "https://tiebapic.baidu.com/forum/w%3D1920%3Bq%3D100/sign=7ee6f7b31d55b3199cf9867c7199b957/ef61279759ee3d6d84de3c7954166d224e4adec1.jpg?tbpicau=2026-11-26-05_032177fbaceb8deece1d95f1e24525ba",
                    },
                    {
                        "type": 3,
                        "origin_src": "https://tiebapic.baidu.com/forum/pic/item/a410500fd9f9d72a8c7cabafc32a2834359bbbc1.jpg?tbpicau=2026-11-26-05_f7a3ec6cdbdfbe9eabf652b870b81a1d",
                        "big_cdn_src": "https://tiebapic.baidu.com/forum/w%3D1920%3Bq%3D100/sign=6b242f0990d6277fe91236311a082441/a410500fd9f9d72a8c7cabafc32a2834359bbbc1.jpg?tbpicau=2026-11-26-05_f3f2bdcad86f86ff76ec640e43860ad8",
                    },
                    {
                        "type": 3,
                        "origin_src": "https://tiebapic.baidu.com/forum/pic/item/84dbe850352ac65ced1dddc1ecf2b21192138ac1.jpg?tbpicau=2026-11-26-05_5e2c60da7446f0fd2fcb9fa94335d1e9",
                        "big_cdn_src": "https://tiebapic.baidu.com/forum/w%3D1920%3Bq%3D100/sign=f7f4ec5be5d3572c66e298d5b8235856/84dbe850352ac65ced1dddc1ecf2b21192138ac1.jpg?tbpicau=2026-11-26-05_97fcc48ba43c89996be7d1cac5f49950",
                    },
                ],
            },
        ],
        "user_list": [
            {
                "id": 564739004,
                "name": "飞翔只为梦想",
                "name_show": "飞翔只为梦想",
                "portrait": "tb.1.2ffb4421.vKGKJ0UjQ5ICuygq4icfBw?t=1794740356",
            },
            {
                "id": 3892174370,
                "name": "凌夜之鋒",
                "name_show": "🍂Akiha🍁",
                "portrait": "tb.1.3b274f60.edzifT5WcSKejIR56txyZQ?t=1794740356",
            },
            {"id": 20006581941844, "portrait": "tb.1.84e6843.xvQf34wkaS40DAg4qeknbw"},
        ],
    },
    "11127878360": {
        "error_code": 0,
        "error_msg": "",
        "thread": {
            "id": 11127878360,
            "title": "【汉化发布】GB 星之卡比 日版完全汉化 【Monado汉化组】",
            "post_id": 154060356240,
            "create_time": 1792677922,
            "agree": {
                "agree_num": 12,
                "has_agree": 0,
                "agree_type": 0,
                "disagree_num": 0,
                "diff_agree_num": 12,
            },
            "reply_num": 4,
            "author": {
                "id": 309404641,
                "name": "无敌阿尔宙斯",
                "name_show": "无敌阿尔宙斯",
                "portrait": "tb.1.5af74da7.EmPWMIJQE9YI4DkdUM0ZaQ?t=1794740516",
            },
            "video_info": {
                "video_url": "http://tb-video.bdstatic.com/tieba-smallvideo-transcode-crf/45105966_81cdd6bd28d93a4b500f07f03c084cb7_5ec722720905_0.mp4?vt=1&pt=0&ver=12.64.1.1&cr=2&cd=0&sid=&ft=8&tbau=2026-11-16_e699cc42fb6b550d7a3e623a517837abc1bffbe08585f954adaa7999410fa4a2&ptid=11127878360",
                "video_desc": [
                    {
                        "video_id": 0,
                        "video_url": "http://tb-video.bdstatic.com/tieba-smallvideo-transcode-crf/45105966_81cdd6bd28d93a4b500f07f03c084cb7_5ec722720905_0.mp4?vt=1&pt=0&ver=12.64.1.1&cr=2&cd=0&sid=&ft=8&tbau=2026-11-16_e699cc42fb6b550d7a3e623a517837abc1bffbe08585f954adaa7999410fa4a2&ptid=11127878360",
                        "video_width": "1280",
                        "video_height": "720",
                    }
                ],
                "thumbnail_url": "https://tiebapic.baidu.com/forum/whcrop%3D406%2C406/sign=4ec5bf3ef5de9c82a630afcd03f1bd3e/cf1b9d16fdfaaf512f43dab8ca5494eef01f7a11.jpg?tbpicau=2026-11-26-05_9797f6c85d0effbc6c0ead318e3ae230",
                "first_frame_thumbnail": "http://tb-video.bdstatic.com/tieba-video-frame/45105966_81cdd6bd28d93a4b500f07f03c084cb7_5ec722720905_001.jpg",
                "video_duration": 290,
            },
        },
        "forum": {"id": 89278, "name": "星之卡比"},
        "post_list": [
            {
                "id": 154060356240,
                "floor": 1,
                "time": 1792677922,
                "author_id": 309404641,
                "agree": {
                    "agree_num": 9,
                    "has_agree": 0,
                    "agree_type": 0,
                    "disagree_num": 0,
                    "diff_agree_num": 9,
                },
                "need_log": 0,
                "is_post_visible": 0,
                "content": [
                    {"type": 0, "text": "\n"},
                    {
                        "type": 5,
                        "text": "https://tieba.baidu.com/mo/q/movideo/page?thumbnail=a7efce1b9d16fdfa73cc9de9f28f8c5494ee7b11&video=45105966_81cdd6bd28d93a4b500f07f03c084cb7_5ec722720905&product=tieba-movideo&thread_id=11127878360",
                        "link": "http://tb-video.bdstatic.com/tieba-smallvideo-transcode-crf/45105966_81cdd6bd28d93a4b500f07f03c084cb7_5ec722720905_0.mp4?vt=1&pt=0&ver=12.64.1.1&cr=2&cd=0&sid=&ft=8&tbau=2026-11-16_e699cc42fb6b550d7a3e623a517837abc1bffbe08585f954adaa7999410fa4a2&ptid=11127878360",
                        "src": "https://tiebapic.baidu.com/forum/w%3D960%3Bq%3D80/sign=42b2693f6e381f309e1981af993a3d35/cf1b9d16fdfaaf512f43dab8ca5494eef01f7a11.jpg?tbpicau=2026-11-26-05_872189c9b9f4b7e35d15b3a984a6e64b",
                        "during_time": 290,
                    },
                ],
            },
            {
                "id": 154060660536,
                "floor": 2,
                "time": 1792683522,
                "author_id": 309404641,
                "agree": {
                    "disagree_num": 0,
                    "diff_agree_num": 2,
                    "agree_num": 2,
                    "has_agree": 0,
                    "agree_type": 0,
                },
                "need_log": 0,
                "is_post_visible": 0,
                "content": [
                    {"type": 0, "text": "获取: "},
                    {
                        "type": 1,
                        "text": "网盘链接 ",
                        "link": "https://tieba.baidu.com/mo/q/checkurl?url=https%3A%2F%2Fpan.baidu.com%2Fs%2F1VI1opk8B3QNp0L1utWEOPQ%3Fpwd%3Dfm88&urlrefer=5558b65f22b269c15d80ee864f7c9928",
                    },
                    {"type": 0, "text": "提取码: fm88"},
                ],
            },
            {
                "id": 154060661522,
                "floor": 3,
                "time": 1792683540,
                "author_id": 309404641,
                "agree": {
                    "agree_type": 0,
                    "disagree_num": 0,
                    "diff_agree_num": 1,
                    "agree_num": 1,
                    "has_agree": 0,
                },
                "need_log": 0,
                "is_post_visible": 0,
                "content": [
                    {
                        "type": 0,
                        "text": "游戏介绍\n"
                        "可以吸入任何东西，长得圆滚滚的卡比的出道作品。\n"
                        "本作品发售于1992年，是Game "
                        "Boy上的动作游戏。圆滚滚的主人公卡比擅长的技能是用大大的嘴巴将任何东西吸进去，将吸入的敌人变成星星，并用其去撞击其他敌人，还可以吸入空气并让自己在空中飞行。卡比为了从自己所居住的国家“噗噗噗之国”中,那位自称为大王的“帝帝帝大王”那里夺回噗噗噗之国所有的食物和秘宝“Twinkle "
                        "Stars”，卡比前往了帝帝帝大王的城堡。\n"
                        "人员名单\n"
                        "Monado汉化组\n"
                        "破解：扭TYPE-小光、洗涤子\n"
                        "翻译：无敌阿尔宙斯\n"
                        "美工：扭TYPE-小光\n"
                        "cia生成：库帕二世\n"
                        "项目说明\n"
                        "本ROM使用日版修订版作为基础，以Codex与Deepseek "
                        "Harness作为破解工具，配合人工翻译、修图、测试完成汉化。\n"
                        "未汉化制作人员名单，尽请见谅。\n"
                        "二周目“额外游戏”模式通关后，出现的怪物名单中部分怪物至今未公布官方中文名，因此采取暂译，不代表官方正式翻译，如引起误解与混淆，敬请见谅。汉化字体使用Z工坊像素黑体 "
                        "12px与缝合像素字体 8px。\n"
                        "【汉化感想】\n"
                        "扭TYPE-小光：\n"
                        "科学的力量真伟大，像我这样的小白也能参与汉化工作了\n"
                        "洗涤子：\n"
                        "提前庆祝《星之卡比》系列35周年！另外信男愿一生信奉海利亚女神以求塞尔达40周年音乐会二次抽选中选（\n"
                        "科学的力量真伟大啊，在大G老师（GPT-6 "
                        "Astra）与大肥鱼（DeepSeek-V4.1-Flash-1017）的帮助下，我完成了第一次对GB平台的汉化。\n"
                        "本次汉化项目由库帕老师发起，最初由小光老师驾驶Astra神（甚至是Ultra）完成绝大部分内容的汉化破解，并由创大师进行人工翻译，我则负责审计AI对ROM的改动面、细节收尾与发布的相关工作。\n"
                        "这么远古的硬件，自然是存在诸多限制的，简直就是戴着镣铐跳舞，那个时代的程序员太强了！（此外，据说本作程序员SUNDAY "
                        "RAIN是岩田聪先生的化名）\n"
                        "反观如今，我们已经基本告别古法编程了，甚至大部分人根本不会去阅读AI编写的代码，Vibe "
                        "Coding的时代究竟是好时代还是坏时代呢？",
                    }
                ],
            },
        ],
        "user_list": [
            {
                "id": 309404641,
                "name": "无敌阿尔宙斯",
                "name_show": "无敌阿尔宙斯",
                "portrait": "tb.1.5af74da7.EmPWMIJQE9YI4DkdUM0ZaQ?t=1794740516",
            },
            {"id": 20007124834485, "portrait": "tb.1.7ba22ecb.Ni3KZ9wdhn3KMeoQhe-xSQ"},
        ],
    },
}


class _Content:
    def __init__(self, payload):
        self.payload = payload

    async def iter_chunked(self, size):
        for index in range(0, len(self.payload), size):
            yield self.payload[index : index + size]


class _Response:
    def __init__(self, data=None, *, raw=None, status=200):
        payload = raw if raw is not None else json.dumps(data).encode()
        self.content = _Content(payload)
        self.status = status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    def raise_for_status(self):
        if self.status >= 400:
            raise RuntimeError(f"HTTP {self.status}")


class _Session:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return next(self.responses)


class TiebaParserTests(unittest.TestCase):
    def setUp(self):
        self.parser = TiebaParser(hot_comment_count=3)

    def parse_fixture(self, tid="11180655858", parser=None, response=None):
        parser = parser or self.parser
        data = copy.deepcopy(REAL_RESPONSES[tid]) if response is None else response
        parser._fetch_json = AsyncMock(return_value=data)
        return asyncio.run(parser.parse(None, f"https://tieba.baidu.com/p/{tid}"))

    def test_thread_and_verified_share_identities(self):
        urls = (
            "https://tieba.baidu.com/p/11180655858",
            "http://www.tieba.baidu.com:80/p/11180655858/?share=9105#123",
            "https://tieba.baidu.com/mo/q/m?kz=11180655858&from=share",
            "https://tieba.baidu.com/mo/q/m/?tid=11180655858",
            "https://tieba.baidu.com/mo/q/m?tn=bdPList&z=11180655858&pn=0",
            "https://tieba.baidu.com/mo/q/movideo/page?product=tieba-movideo&thread_id=11180655858",
        )
        for url in urls:
            with self.subTest(url=url):
                self.assertTrue(self.parser.can_parse(url))
                self.assertEqual(self.parser._thread_id(url), "11180655858")

    def test_untrusted_and_ambiguous_links_are_rejected(self):
        urls = (
            "https://tieba.baidu.com.evil.test/p/11180655858",
            "https://tieba.baidu.com@evil.test/p/11180655858",
            "https://user:private@tieba.baidu.com/p/11180655858",
            "https://tieba.baidu.com:444/p/11180655858",
            "https://tieba.baidu.com/p/11180655858/another",
            "https://tieba.baidu.com/p/0",
            "https://tieba.baidu.com/f?kw=test",
            "https://tieba.baidu.com/mo/q/m?kz=1&tid=2",
            "https://tieba.baidu.com/mo/q/m?kz=1&kz=2",
            "https://tieba.baidu.com/mo/q/m?kz=",
            "https://tieba.baidu.com/mo/q/m?z=11180655858",
            "https://tieba.baidu.com/mo/q/m?tn=bdL&z=11180655858",
            "https://tieba.baidu.com/mo/q/m?tn=bdPList&z=1&kz=2",
            "file://tieba.baidu.com/p/11180655858",
            "https://tieba.baidu.com/p/11180655858\n",
        )
        for url in urls:
            with self.subTest(url=url):
                self.assertFalse(self.parser.can_parse(url))

    def test_extract_links_deduplicates_same_thread_and_trims_punctuation(self):
        text = (
            "看看 https://tieba.baidu.com/p/11180655858。 "
            "https://tieba.baidu.com/mo/q/m?kz=11180655858 "
            "https://tieba.baidu.com/p/7379611390），"
        )
        self.assertEqual(
            self.parser.extract_links(text),
            [
                "https://tieba.baidu.com/p/11180655858",
                "https://tieba.baidu.com/p/7379611390",
            ],
        )

    def test_standard_uppercase_glued_and_mixed_platform_links(self):
        url = "https://tieba.baidu.com/p/1234567890"
        self.assertEqual(self.parser.extract_links(url), [url])
        self.assertEqual(self.parser.extract_links(f"前文{url}后文"), [url])
        upper = "HTTPS://TIEBA.BAIDU.COM/p/1234567890"
        self.assertEqual(self.parser.extract_links(upper), [upper])
        text = (
            "https://www.bilibili.com/video/BV1test "
            f"贴吧{url}中文 "
            "https://tieba.baidu.com/p/7379611390 "
            "https://www.zhihu.com/answer/123 "
            "https://tieba.baidu.com/mo/q/m?kz=11180655858"
        )
        self.assertEqual(
            self.parser.extract_links(text),
            [
                url,
                "https://tieba.baidu.com/p/7379611390",
                "https://tieba.baidu.com/mo/q/m?kz=11180655858",
            ],
        )

    def test_real_text_image_thread_and_actual_floor_reply(self):
        result = self.parse_fixture()
        self.assertEqual(result["title"], "正在尝试复活")
        self.assertEqual(result["author"], "Adnini983")
        self.assertEqual(result["forum_name"], "星之卡比")
        self.assertEqual(result["stats_line"], "👍 4 💬 2")
        self.assertIn("2021年下线了", result["desc"])
        self.assertTrue(result["desc"].endswith("[太开心]"))
        self.assertEqual(len(result["image_urls"]), 1)
        self.assertIn("/forum/pic/item/", result["image_urls"][0][0])
        self.assertIn("?tbpicau=", result["image_urls"][0][0])
        self.assertTrue(result["timestamp"].endswith("+00:00"))
        reply = result["hot_comments"][0]
        self.assertEqual(reply["username"], "孤独的滑膛枪")
        self.assertEqual(reply["uid"], "139452280")
        self.assertEqual(reply["message"], "[真棒]抢救失传媒体好评")
        self.assertEqual(reply["position_text"], "第 2 楼")
        self.assertEqual(reply["likes"], 0)
        self.assertEqual(result["comment_section_title"], "楼层回复")

    def test_real_image_only_main_does_not_merge_reply_images(self):
        result = self.parse_fixture("7379611390")
        self.assertEqual(result["desc"], "")
        self.assertEqual(len(result["image_urls"]), 1)
        self.assertIn(
            "200d0eb30f2442a749924b17c643ad4bd0130244.jpg", result["image_urls"][0][0]
        )
        self.assertNotIn("8ba6e7cd", str(result["image_urls"]))
        reply = result["hot_comments"][0]
        self.assertIn("按我图中顺序走就能到了", reply["message"])
        self.assertIn("【图片回复：5张】", reply["message"])
        self.assertNotIn("image_urls", reply)

    def test_real_video_type_5_and_thread_video_are_deduplicated(self):
        result = self.parse_fixture("11127878360")
        self.assertEqual(result["desc"], "")
        self.assertEqual(result["image_urls"], [])
        self.assertEqual(len(result["video_urls"]), 1)
        video = result["video_urls"][0][0]
        self.assertTrue(video.startswith("https://tb-video.bdstatic.com/"))
        self.assertIn("&tbau=2026-11-16_", video)
        self.assertIn("&ptid=11127878360", video)
        self.assertEqual(result["timelength_ms"], 290000)
        self.assertEqual(
            result["video_cover_urls"][0][0],
            REAL_RESPONSES["11127878360"]["thread"]["video_info"]["thumbnail_url"],
        )
        self.assertIn("?tbpicau=", result["video_cover_urls"][0][0])
        self.assertNotIn("游戏介绍", result["desc"])
        self.assertEqual(self.parser._fetch_json.await_count, 1)

    def test_main_type_5_video_works_without_thread_video_info(self):
        data = copy.deepcopy(REAL_RESPONSES["11127878360"])
        del data["thread"]["video_info"]
        result = self.parse_fixture("11127878360", response=data)
        self.assertEqual(len(result["video_urls"]), 1)
        self.assertEqual(result["timelength_ms"], 290000)
        self.assertTrue(result["video_cover_urls"][0])

    def test_plain_text_main_only_is_successful(self):
        data = copy.deepcopy(REAL_RESPONSES["11180655858"])
        data["post_list"] = [data["post_list"][0]]
        data["post_list"][0]["content"] = [
            {"type": 0, "text": "正文 <example> &amp; 示例"}
        ]
        result = self.parse_fixture(response=data)
        self.assertEqual(result["desc"], "正文 <example> & 示例")
        self.assertEqual(result["image_urls"], [])
        self.assertNotIn("hot_comments", result)

    def test_text_only_still_accepts_pure_image_or_video_threads(self):
        for tid in ("7379611390", "11127878360"):
            with self.subTest(tid=tid):
                parser = TiebaParser(include_media=False, hot_comment_count=2)
                result = self.parse_fixture(tid, parser=parser)
                self.assertTrue(result["title"])
                self.assertEqual(result["image_urls"], [])
                self.assertEqual(result["video_urls"], [])
                self.assertEqual(result["video_cover_urls"], [])
                self.assertTrue(result["cover_url"])
                self.assertTrue(result["hot_comments"])
                self.assertEqual(parser._fetch_json.await_count, 1)

    def test_zero_comments_skips_reply_extraction(self):
        parser = TiebaParser(hot_comment_count=0)
        parser._replies = AsyncMock()
        result = self.parse_fixture(parser=parser)
        parser._replies.assert_not_called()
        self.assertNotIn("hot_comments", result)
        self.assertEqual(TiebaParser._replies([], {}, 0), [])

    def test_reply_order_uses_floor_and_preserves_unknown_likes(self):
        data = copy.deepcopy(REAL_RESPONSES["11180655858"])
        reply = copy.deepcopy(data["post_list"][1])
        reply["id"] = 100003
        reply["floor"] = 3
        reply.pop("agree")
        reply["content"] = [{"type": 0, "text": "一样的正文"}]
        data["post_list"][1]["content"] = [{"type": 0, "text": "一样的正文"}]
        data["post_list"][1]["agree"]["agree_num"] = 999
        data["post_list"] = [reply, data["post_list"][0], data["post_list"][1], reply]
        result = self.parse_fixture(response=data)
        self.assertEqual([r["floor"] for r in result["hot_comments"]], [2, 3])
        self.assertEqual([r["likes"] for r in result["hot_comments"]], [999, None])
        self.assertEqual(len(result["image_urls"]), 1)

    def test_ad_deleted_invalid_and_main_floor_are_not_replies(self):
        data = copy.deepcopy(REAL_RESPONSES["11180655858"])
        reply = data["post_list"][1]
        ads = []
        for key, value in [
            ("is_ad", 1),
            ("is_ad_post", True),
            ("ad_info", {"title": "推广"}),
            ("is_deleted", 1),
        ]:
            ad = copy.deepcopy(reply)
            ad.update(id=100000 + len(ads), floor=3 + len(ads))
            ad[key] = value
            ads.append(ad)
        data["post_list"].extend([*ads, None, {}, {"floor": 8, "content": None}])
        result = self.parse_fixture(response=data)
        self.assertEqual([r["floor"] for r in result["hot_comments"]], [2])

    def test_pure_media_reply_has_real_summary_without_media_leak(self):
        data = copy.deepcopy(REAL_RESPONSES["11180655858"])
        data["post_list"][1]["content"] = [
            {"type": 3, "origin_src": "https://tiebapic.baidu.com/reply.jpg"},
            {"type": 3, "origin_src": "https://tiebapic.baidu.com/reply2.jpg"},
            {"type": 5, "link": "https://tb-video.bdstatic.com/reply.mp4"},
        ]
        result = self.parse_fixture(response=data)
        self.assertEqual(
            result["hot_comments"][0]["message"], "【图片回复：2张】\n【视频回复】"
        )
        self.assertNotIn("reply.jpg", str(result["image_urls"]))
        self.assertEqual(result["video_urls"], [])

    def test_missing_or_wrong_main_floor_cannot_send_a_reply_as_body(self):
        for mode in ("missing", "wrong_post_id"):
            with self.subTest(mode=mode):
                data = copy.deepcopy(REAL_RESPONSES["11180655858"])
                if mode == "missing":
                    data["post_list"] = data["post_list"][1:]
                else:
                    data["thread"]["post_id"] = 777
                with self.assertRaisesRegex(ValueError, "主楼"):
                    self.parse_fixture(response=data)

    def test_wrong_thread_and_empty_body_are_failures(self):
        data = copy.deepcopy(REAL_RESPONSES["11180655858"])
        data["thread"]["id"] = 777
        with self.assertRaisesRegex(ValueError, "请求的帖子"):
            self.parse_fixture(response=data)
        data["thread"]["id"] = 11180655858
        data["post_list"][0]["content"] = []
        with self.assertRaisesRegex(ValueError, "可访问"):
            self.parse_fixture(response=data)

    def test_api_nonzero_codes_cannot_be_mistaken_for_success(self):
        examples = (
            (350008, "该贴已被删除，请浏览其他贴子", "删除"),
            (4, "需要登录", "登录"),
            ("4", "百度安全验证", "安全验证"),
            (1989002, "加载数据失败", "1989002"),
            (None, "", "未知"),
        )
        for code, message, expected in examples:
            with self.subTest(code=code):
                data = copy.deepcopy(REAL_RESPONSES["11180655858"])
                data.update(error_code=code, error_msg=message)
                with self.assertRaisesRegex(ValueError, expected):
                    self.parse_fixture(response=data)

    def test_malformed_optional_values_do_not_break_metadata(self):
        data = copy.deepcopy(REAL_RESPONSES["11180655858"])
        data.update(user_list=[None, 1, []], forum=[])
        data["thread"].update(author=[], agree=[], reply_num="invalid")
        data["post_list"][0].update(time="invalid", author=[])
        data["post_list"][0]["content"].extend(
            [None, [], {"type": True, "text": "bad"}, {"type": 0, "text": []}]
        )
        result = self.parse_fixture(response=data)
        self.assertEqual(result["author"], "")
        self.assertEqual(result["timestamp"], "")
        self.assertNotIn("stats", result)
        self.assertNotIn("stats_line", result)
        self.assertNotIn("bad", result["desc"])

    def test_untrusted_media_is_rejected_and_signed_query_preserved(self):
        for url in (
            "https://evil.test/photo.jpg",
            "https://tiebapic.baidu.com.evil.test/photo.jpg",
            "https://private@tiebapic.baidu.com/photo.jpg",
            "https://tiebapic.baidu.com:444/photo.jpg",
            "file://tiebapic.baidu.com/photo.jpg",
        ):
            with self.subTest(url=url):
                self.assertEqual(self.parser._media_url(url), "")
        self.assertEqual(
            self.parser._media_url(
                "http://tb-video.bdstatic.com:80/x.mp4?vt=1&tbau=abc&ptid=123#part",
                video=True,
            ),
            "https://tb-video.bdstatic.com/x.mp4?vt=1&tbau=abc&ptid=123",
        )
        self.assertEqual(
            self.parser._media_url("https://tiebapic.baidu.com/image.jpg", video=True),
            "",
        )

    def test_original_fallbacks_remain_signed_and_ordered(self):
        item = REAL_RESPONSES["11180655858"]["post_list"][0]["content"][-1]
        _, images, _, _ = self.parser._content([item, item])
        self.assertEqual(len(images), 1)
        self.assertEqual(images[0][0], item["origin_src"])
        self.assertEqual(images[0][1], item["big_cdn_src"])
        bad_original = {**item, "origin_src": "https://evil.test/photo.jpg"}
        _, images, _, _ = self.parser._content([bad_original])
        self.assertEqual(images[0][0], item["big_cdn_src"])

    def test_avatar_uses_author_portrait_and_keeps_timestamp_query(self):
        user = REAL_RESPONSES["11180655858"]["thread"]["author"]
        self.assertEqual(
            self.parser._avatar(user),
            "https://himg.bdimg.com/sys/portrait/item/" + user["portrait"],
        )
        self.assertEqual(self.parser._avatar({"portrait": "../secret?other=bad"}), "")
        shown = {
            "portrait": "../bad",
            "user_show_info": {
                "feed_head": {
                    "image_data": {
                        "img_url": "http://tb.himg.baidu.com/sys/portrait/item/token?t=1"
                    }
                }
            },
        }
        self.assertEqual(
            self.parser._avatar(shown),
            "https://himg.bdimg.com/sys/portrait/item/token?t=1",
        )

    def test_cookie_proxy_and_media_headers_stay_separate(self):
        parser = TiebaParser(
            cookie="BDUSS=private", use_proxy=True, proxy_url="http://127.0.0.1:7890"
        )
        result = self.parse_fixture(parser=parser)
        self.assertTrue(result["use_image_proxy"])
        self.assertTrue(result["use_video_proxy"])
        self.assertNotIn("Cookie", result["image_headers"])
        self.assertNotIn("Cookie", result["video_headers"])
        self.assertEqual(result["proxy_url"], "http://127.0.0.1:7890")
        self.assertEqual(TiebaParser(cookie="BDUSS=a\nInjected: header").cookie, "")

    def test_optional_reply_failure_preserves_main_and_cancellation_propagates(self):
        self.parser._replies = unittest.mock.Mock(
            side_effect=RuntimeError("unexpected")
        )
        result = self.parse_fixture()
        self.assertEqual(result["title"], "正在尝试复活")
        self.assertNotIn("hot_comments", result)
        self.parser._replies = unittest.mock.Mock(side_effect=asyncio.CancelledError())
        with self.assertRaises(asyncio.CancelledError):
            self.parse_fixture()
        self.parser._fetch_json = AsyncMock(side_effect=asyncio.CancelledError())
        with self.assertRaises(asyncio.CancelledError):
            asyncio.run(
                self.parser.parse(None, "https://tieba.baidu.com/p/11180655858")
            )

    def test_request_is_signed_fixed_https_bounded_and_uses_proxy_cookie(self):
        parser = TiebaParser(
            cookie="BDUSS=private",
            hot_comment_count=3,
            use_proxy=True,
            proxy_url="http://127.0.0.1:7890",
        )
        session = _Session([_Response(REAL_RESPONSES["11180655858"])])
        result = asyncio.run(parser._fetch_json(session, "11180655858"))
        self.assertEqual(result["error_code"], 0)
        url, request = session.calls[0]
        self.assertEqual(url, TIEBA_API)
        self.assertTrue(url.startswith("https://"))
        self.assertFalse(request["allow_redirects"])
        self.assertEqual(request["headers"]["Cookie"], "BDUSS=private")
        self.assertEqual(request["proxy"], "http://127.0.0.1:7890")
        self.assertGreater(request["timeout"].total, 0)
        params = dict(request["data"])
        self.assertEqual(params["rn"], "4")
        signature = params.pop("sign")
        source = (
            "".join(k + "=" + v for k, v in sorted(params.items())) + "tiebaclient!!!"
        )
        self.assertEqual(signature, hashlib.md5(source.encode()).hexdigest().upper())
        self.assertEqual(params["pn"], "1")
        self.assertEqual(params["with_floor"], "0")
        self.assertNotIn("ssl", request)

    def test_zero_comments_requests_only_main_floor(self):
        session = _Session([_Response(REAL_RESPONSES["11180655858"])])
        asyncio.run(TiebaParser()._fetch_json(session, "11180655858"))
        self.assertEqual(session.calls[0][1]["data"]["rn"], "1")
        self.assertEqual(len(session.calls), 1)

    def test_redirects_and_http_failures_are_not_followed(self):
        for status, message in ((302, "重定向"), (429, "频繁"), (500, "HTTP 500")):
            with self.subTest(status=status):
                session = _Session([_Response({}, status=status)])
                with self.assertRaisesRegex((ValueError, RuntimeError), message):
                    asyncio.run(self.parser._fetch_json(session, "11180655858"))
                self.assertEqual(len(session.calls), 1)

    def test_non_json_verification_and_invalid_json_shapes_are_failures(self):
        examples = (
            (
                _Response(raw="<html>百度安全验证</html>".encode(), status=403),
                "安全验证",
            ),
            (_Response(raw=b"not json", status=401), "登录"),
            (_Response(raw=b"not json"), "JSON"),
            (_Response([]), "格式无效"),
            (_Response({"error_code": 4, "error_msg": "需要登录"}, status=403), "登录"),
            (_Response({"error_code": 0}, status=403), "被拒绝"),
        )
        for response, expected in examples:
            with self.subTest(expected=expected):
                session = _Session([response])
                with self.assertRaisesRegex((ValueError, TypeError), expected):
                    asyncio.run(self.parser._fetch_json(session, "11180655858"))

    def test_oversized_response_is_rejected_during_read(self):
        session = _Session([_Response(raw=b"x" * 1000)])
        with (
            patch("nova_core.parser.platform.tieba.MAX_RESPONSE_BYTES", 128),
            self.assertRaisesRegex(ValueError, "大小限制"),
        ):
            asyncio.run(self.parser._fetch_json(session, "11180655858"))


if __name__ == "__main__":
    unittest.main()
