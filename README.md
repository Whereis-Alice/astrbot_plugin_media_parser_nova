# Nova 流媒体解析

面向 AstrBot 的多平台媒体解析插件。自动识别消息中的平台链接，提取标题、作者、正文、图片和视频，按配置发送文本摘要、媒体文件或渲染成卡片。

项目地址：[Whereis-Alice/astrbot_plugin_media_parser_nova](https://github.com/Whereis-Alice/astrbot_plugin_media_parser_nova)

插件 ID、Python 包名、日志名、缓存标记和 ZIP 归档目录都使用 Nova 专属标识，与上游插件的配置和缓存互不覆盖。

> 两个插件可以装在同一个 AstrBot 里，但如果都开启自动解析，同一条链接会被回复两次。建议只启用一个插件的自动解析。

## 功能概览

- 自动提取消息、回复消息和部分 QQ/平台卡片中的链接
- 支持视频、图片、纯文本和部分平台热评
- 每个平台独立设置为：关闭、全部发送、仅文本、仅富媒体
- 卡片渲染：7 套可选皮肤 × 深浅配色 × 4 种布局，皮肤与界面家具可跟随链接所属平台
- 标题、正文和热评可翻译；译文可以只用于卡片，也可以同时用于普通文本
- 热评可直接合并到同一张卡片中
- B 站支持 Cookie 解析、高画质与管理员协助扫码更新
- 视频下载硬上限、普通发送预算、独立可调压缩策略，以及可选 QQ 群聊/私聊文件投递
- 超限视频可直接以原文件发送，跳过所有压缩设置
- LLM 可调用 `nova_fetch_content` 主动获取正文、评论和媒体地址，再按用户需求处理
- 媒体缓存、媒体中转、ZIP 归档，以及对下载体积、解析频率、缓存清理和公网 URL 的兜底

## 支持平台

| 平台 | 支持内容 | 常见链接 |
| --- | --- | --- |
| B 站 | 视频、图片、动态、番剧、文本、热评 | `b23.tv`、`bilibili.com/video`、`bilibili.com/opus` |
| 抖音 | 视频、图集、文本 | `v.douyin.com`、`douyin.com/video`、`douyin.com/note` |
| TikTok | 视频、图集、文本、热评 | `vm.tiktok.com`、`tiktok.com/@.../video` |
| 快手 | 视频、图片、文本 | `v.kuaishou.com`、`kuaishou.com`、`gifshow.com` |
| 微博 | 视频、图片、文本、热评 | `weibo.com`、`m.weibo.cn`、`video.weibo.com` |
| 小红书 | 视频、图片、文本、热评 | `xhslink.com`、`xiaohongshu.com/explore` |
| 闲鱼 | 商品视频、图片、文本 | `m.tb.cn`、`goofish.com/item` |
| 今日头条 | 视频、图片、文章、微头条 | `toutiao.com/article`、`toutiao.com/video`、`toutiao.com/w` |
| 小黑盒 | 视频、图片、文本；BBS 帖子热评 | `xiaoheihe.cn/app/topic`、`xiaoheihe.cn/app/bbs/link` |
| Twitter/X | 视频、图片、文本；热评与互动统计需配置 Nitter | `twitter.com/.../status/...`、`x.com/.../status/...` |
| Pixiv | 插画、漫画多页图片、文本 | `pixiv.net/artworks/...`、`pixiv.net/i/...` |
| niconico | 视频、封面、标题、作者、简介、弹幕摘选 | `nicovideo.jp/watch/sm...`、`nico.ms/sm...` |
| Steam | 游戏商店信息、截图、宣传视频、玩家评测 | `store.steampowered.com/app/...` |
| 知乎 | 回答、问题、专栏文章、图片、视频、评论 | `zhihu.com/question/.../answer/...`、`zhuanlan.zhihu.com/p/...` |
| 贴吧 | 帖子主楼、图片、可获取的视频、楼层回复 | `tieba.baidu.com/p/...`、`tieba.baidu.com/mo/q/m?...` |

平台页面结构、登录状态、地区限制和风控策略会变化，「支持平台」不代表每条链接在任何网络环境下都一定可访问。

## 安装

在 AstrBot WebUI 的插件市场搜索：

```text
astrbot_plugin_media_parser_nova
```

手动安装：把本项目目录放进 AstrBot 插件目录，确认目录名为 `astrbot_plugin_media_parser_nova`，安装 `requirements.txt` 里的依赖（或交给 AstrBot 自动安装），重启后在插件配置页完成设置。

依赖 `aiohttp`、`cryptography`、`qrcode[pil]`、`pillow`。Python 3.10 或更高，AstrBot 4.25 或更高。

v1.21.1 起兼容 Pillow 13，具体版本沿用 AstrBot 核心的依赖约束。遇到日志中的 Pillow 版本保护冲突时，更新插件到新版即可。

## 快速开始

装好即用：各平台默认为「全部发送」，直接在聊天里发一条支持的链接就会解析。

niconico、TikTok、Steam、微博、知乎也可在「解析器与输出模式」中独立关闭。微博、知乎和 TikTok 遇到登录或风控限制时，可在对应平台设置中填写浏览器 Cookie，并按需开启代理。Steam 默认最多发送 6 张截图、1 条宣传视频。

贴吧也有独立输出开关，主楼正文和媒体按配置发送；楼层回复沿用评论条数及卡片设置。接口返回的首批回复会标为“楼层回复”，缺失的点赞数显示未知。访问受限时可在「贴吧」设置中填写 Cookie，并按需开启解析代理。

评论设置沿用 B 站的方式：在「消息输出 → 附加内容：热评」把条数设为大于 0，并开启对应平台。需要在卡片里展示时，再开启「热评合并到卡片」；是否发送独立评论文本由「在普通文本中显示热评」控制。Steam 的内容标为“玩家评测”，niconico 标为“弹幕摘选”。知乎、微博评论可能需要 Cookie；获取失败时继续发送原内容。

要让大视频直接以群文件发送：将「下载设置 → 超限视频投递方式」设为「超限直接上传原文件（不压缩）」。超过普通视频发送上限后，QQ 群聊上传群文件，私聊上传私聊文件；这个选项优先于所有压缩设置，仍遵守下载硬上限。需要可用缓存和支持文件上传的 OneBot v11 协议端。niconico 和 Steam 的 HLS 视频需要 ffmpeg。

三种触发方式：自动解析链接、引用消息后发关键词、手动关键词（默认含 `媒体解析`）。全部指令默认值见 [指令一览](docs/configuration.md#指令一览)，其余配置项见 [配置说明](docs/configuration.md)。

## LLM 内容抓取工具

启用支持函数调用的聊天模型后，可以直接对机器人说：

- “读一下这个知乎回答，总结作者的主要观点：链接”
- “抓取这两个 Steam 游戏的介绍和玩家评测，比较优缺点：链接1 链接2”
- “看看这个贴吧帖子主楼和楼层回复，整理大家的意见：链接”
- “获取这个 niconico 视频的简介和弹幕摘选，翻译成中文：链接”

模型可调用 `nova_fetch_content`，拿到结构化内容后自由回答。工具默认开启；还需在 AstrBot 的工具列表和当前人格中允许该工具，并使用支持工具调用的模型。它只抓取资料，不主动发送卡片或视频。

默认每次最多 3 个链接、返回最多 24000 字符；在「LLM 内容抓取工具」中调整或关闭。每条链接的评论条数由模型通过 `max_comments` 自行决定，可取 0–20，省略参数默认 5 条。评论条数独立于聊天评论条数，沿用各平台评论开关；实际返回数量取决于平台可用内容。平台设为「关闭」后工具也不能抓取；「仅文本/仅富媒体」只控制聊天展示。

返回正文、来源、作者、时间、统计、可用评论及媒体地址。Steam 评论是玩家评测，niconico 是弹幕摘选，贴吧是首批楼层回复。媒体地址不表示模型已观看视频或读取图片；本工具没有视频转录功能。访问受限、摘要和截断会保留提示。参数与返回格式见 [LLM 工具说明](docs/llm-tool.md)。

## YouTube 已独立

YouTube 解析、yt-dlp、PO Token、Cookie 轮换和 YouTube 仿站卡片已经迁移到独立插件 [YouTube Nova](https://github.com/Whereis-Alice/astrbot_plugin_youtube_nova)。本插件不再识别 YouTube 链接，两个插件可以同时安装且不会重复响应。

## 卡片渲染

开启卡片渲染后，解析结果会画成一张 PNG。五套通用皮肤（极光 / 报章 / 测控 / 展陈 / 夜曲）走自己的设计语言，两套仿站皮肤（哔哩哔哩 / X）复刻对应平台手机端的详情页；配色分深浅，布局有标准 / 杂志 / 沉浸式 / 信息流四种。

皮肤选「跟随平台」时，B 站链接用哔哩哔哩皮肤、X 用 X 皮肤；页签条、操作栏、评论输入框这些界面家具也按链接来源换件。

全部皮肤与布局的截图、家具对照表和排版规则见 [卡片渲染](docs/cards.md)。

## 文档

| 文档 | 内容 |
| --- | --- |
| [配置说明](docs/configuration.md) | 输出模式、触发方式、聚合、视频体积、压缩与文件投递、翻译、缓存、代理、ffmpeg、ZIP 归档 |
| [卡片渲染](docs/cards.md) | 七套可选皮肤、四种布局、家具跟随平台、热评合并，含截图 |
| [平台专项说明](docs/platforms.md) | B 站 Cookie、Twitter/X 的 Nitter、小黑盒热评 |
| [排查与反馈](docs/troubleshooting.md) | 常见现象对照表、日志分级、反馈须知 |
| [模块结构](docs/ARCHITECTURE.md) | 目录结构与执行链 |
| [解析思路备忘](docs/PARSER_METHOD_MEMO.md) | 各平台取数方式与已知坑位 |
| [更新日志](CHANGELOG.md) | 版本变更记录 |

## 安全与隐私

- Cookie、请求头、Token 和本地文件路径不会写入 ZIP 的公开详情字段
- 第三方返回的媒体地址只允许公网，避免误访问本地或内网地址
- 下载失败不会静默伪装成成功，摘要会显示跳过或失败原因
- 解析频率限制可以按链接和用户分别配置
- 请遵守目标平台的服务条款、版权要求和当地法律法规

## 致谢与许可

本项目基于上游插件 [astrbot_plugin_media_parser_yaya](https://github.com/xiaoxi2760/astrbot_plugin_media_parser_yaya) 继续维护，感谢作者 `xiaoxi2760` 及其贡献者提供的解析器、下载器、配置与 AstrBot 集成实现。

同时感谢：

- [astrbot_plugin_media_parser](https://github.com/drdon1234/astrbot_plugin_media_parser)：早期解析器实现与平台适配参考
- [astrbot_plugin_rika_share](https://github.com/iris1598/astrbot_plugin_rika_share)：卡片渲染参考（MIT License）
- [nonebot-plugin-parser](https://github.com/maoxig/nonebot-plugin-parser)：CommonRenderer 参考
- [FxEmbed](https://github.com/FxEmbed/FxEmbed)：Twitter/X 解析服务参考
- [bilibili-API-collect](https://github.com/SocialSisterYi/bilibili-API-collect)：B 站接口资料
- [Johnserf-Seed/f2](https://github.com/Johnserf-Seed/f2)：抖音 `a_bogus` 签名实现参考

主体遵循 [GNU Affero General Public License v3.0](LICENSE)。`LICENSES/` 目录包含部分第三方代码对应的许可文本，再分发时请一并保留。
