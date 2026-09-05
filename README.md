# Nova 流媒体解析

面向 AstrBot 的多平台媒体解析插件。自动识别消息中的平台链接，提取标题、作者、正文、图片和视频，按配置发送文本摘要、媒体文件或渲染成卡片。

项目地址：[Whereis-Alice/astrbot_plugin_media_parser_nova](https://github.com/Whereis-Alice/astrbot_plugin_media_parser_nova)

插件 ID、Python 包名、日志名、缓存标记和 ZIP 归档目录都使用 Nova 专属标识，与上游插件的配置和缓存互不覆盖。

> 两个插件可以装在同一个 AstrBot 里，但如果都开启自动解析，同一条链接会被回复两次。建议只启用一个插件的自动解析。

## 功能概览

- 自动提取消息、回复消息和部分 QQ/平台卡片中的链接
- 支持视频、图片、纯文本和部分平台热评
- 每个平台独立设置为：关闭、全部发送、仅文本、仅富媒体
- 卡片渲染：8 套皮肤 × 深浅配色 × 4 种布局，皮肤与界面家具可跟随链接所属平台
- 标题、正文和热评可翻译；译文可以只用于卡片，也可以同时用于普通文本
- 热评可直接合并到同一张卡片中
- B 站支持 Cookie 解析、高画质与管理员协助扫码更新
- YouTube 走官方 Innertube 接口多层降级，疑难视频可选交给 yt-dlp 兜底，不依赖第三方镜像站
- 视频按聊天平台能收下的体积挑流，放不下先自动压缩，压不进去才改发封面，发送失败会在会话里说明原因
- 媒体缓存、媒体中转、ZIP 归档，以及对下载体积、解析频率、缓存清理和公网 URL 的兜底

## 支持平台

| 平台 | 支持内容 | 常见链接 |
| --- | --- | --- |
| B 站 | 视频、图片、动态、番剧、文本、热评 | `b23.tv`、`bilibili.com/video`、`bilibili.com/opus` |
| 抖音 | 视频、图集、文本 | `v.douyin.com`、`douyin.com/video`、`douyin.com/note` |
| TikTok | 视频、图集、文本 | `vm.tiktok.com`、`tiktok.com/@.../video` |
| 快手 | 视频、图片、文本 | `v.kuaishou.com`、`kuaishou.com`、`gifshow.com` |
| 微博 | 视频、图片、文本、热评 | `weibo.com`、`m.weibo.cn`、`video.weibo.com` |
| 小红书 | 视频、图片、文本、热评 | `xhslink.com`、`xiaohongshu.com/explore` |
| 闲鱼 | 商品视频、图片、文本 | `m.tb.cn`、`goofish.com/item` |
| 今日头条 | 视频、图片、文章、微头条 | `toutiao.com/article`、`toutiao.com/video`、`toutiao.com/w` |
| 小黑盒 | 视频、图片、文本；BBS 帖子热评 | `xiaoheihe.cn/app/topic`、`xiaoheihe.cn/app/bbs/link` |
| Twitter/X | 视频、图片、文本；热评与互动统计需配置 Nitter | `twitter.com/.../status/...`、`x.com/.../status/...` |
| Pixiv | 插画、漫画多页图片、文本 | `pixiv.net/artworks/...`、`pixiv.net/i/...` |
| YouTube | 视频、封面、文本、热评与统计 | `youtu.be/...`、`youtube.com/watch`、`youtube.com/shorts/...` |

平台页面结构、登录状态、地区限制和风控策略会变化，「支持平台」不代表每条链接在任何网络环境下都一定可访问。

## 安装

在 AstrBot WebUI 的插件市场搜索：

```text
astrbot_plugin_media_parser_nova
```

手动安装：把本项目目录放进 AstrBot 插件目录，确认目录名为 `astrbot_plugin_media_parser_nova`，安装 `requirements.txt` 里的依赖（或交给 AstrBot 自动安装），重启后在插件配置页完成设置。

依赖 `aiohttp`、`cryptography`、`qrcode[pil]`、`pillow`。Python 3.10 或更高，AstrBot 4.25 或更高。

## 快速开始

装好即用：各平台默认为「全部发送」，直接在聊天里发一条支持的链接就会解析。

三种触发方式：自动解析链接、引用消息后发关键词、手动关键词（默认含 `媒体解析`）。全部配置项见 [配置说明](docs/configuration.md)。

## 卡片渲染

开启卡片渲染后，解析结果会画成一张 PNG。五套通用皮肤（极光 / 报章 / 测控 / 展陈 / 夜曲）走自己的设计语言，三套仿站皮肤（哔哩哔哩 / X / YouTube）复刻对应平台手机端的详情页；配色分深浅，布局有标准 / 杂志 / 沉浸式 / 信息流四种。

皮肤选「跟随平台」时，B 站链接用哔哩哔哩皮肤、X 用 X 皮肤、YouTube 用 YouTube 皮肤；页签条、操作栏、评论输入框这些界面家具也按链接来源换件。

![皮肤跟随平台](docs/card-skins/platform-skin.png)

全部皮肤与布局的截图、家具对照表和排版规则见 [卡片渲染](docs/cards.md)。

## 文档

| 文档 | 内容 |
| --- | --- |
| [配置说明](docs/configuration.md) | 输出模式、触发方式、聚合、视频体积与超限压缩、翻译、缓存、代理、ffmpeg、ZIP 归档 |
| [卡片渲染](docs/cards.md) | 八套皮肤、四种布局、家具跟随平台、热评合并，含全部截图 |
| [YouTube 说明](docs/youtube.md) | 四层解析链路、Cookie 保鲜、yt-dlp 兜底、PO Token provider、代理 |
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
