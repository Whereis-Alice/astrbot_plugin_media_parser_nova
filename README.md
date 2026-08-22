# Nova 流媒体解析

`astrbot_plugin_media_parser_nova` 是一个面向 AstrBot 的多平台媒体解析插件。它会自动识别消息中的平台链接，提取标题、作者、正文、图片和视频，并按配置发送文本摘要或媒体内容。

项目地址：[Whereis-Alice/astrbot_plugin_media_parser_nova](https://github.com/Whereis-Alice/astrbot_plugin_media_parser_nova)

本项目是一个**独立命名的维护版本**：插件 ID、Python 内部包名、日志名、缓存标记和 ZIP 归档目录均使用 Nova 专属标识，因此不会覆盖原插件的配置和缓存。插件功能基于上游公开源码继续维护，并在此基础上修复问题和补充防御性处理。

> 注意：虽然两个插件可以安装在同一个 AstrBot 中，但如果两者都开启自动解析，同一条链接可能会被回复两次。建议只启用一个插件的自动解析功能。

## 功能概览

- 自动提取消息、回复消息和部分 QQ/平台卡片中的链接
- 支持视频、图片、纯文本和部分平台热评
- 每个平台独立设置为：关闭、全部发送、仅文本、仅富媒体
- 支持不聚合、全部聚合、按条件聚合三种消息发送方式
- 支持卡片渲染、标题/作者/时间/正文等文本字段开关
- 支持可选的大模型翻译
- 支持 B 站 Cookie、高画质解析和管理员协助扫码更新
- 支持媒体缓存、媒体中转和 ZIP 归档
- 对下载大小、解析频率、缓存清理和公网 URL 做了限制与兜底

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
| 小黑盒 | 视频、图片、文本 | `xiaoheihe.cn/app/topic`、`xiaoheihe.cn/app/bbs/link` |
| Twitter/X | 视频、图片、文本 | `twitter.com/.../status/...`、`x.com/.../status/...` |
| Pixiv | 插画、漫画多页图片、文本 | `pixiv.net/artworks/...`、`pixiv.net/i/...` |

平台页面结构、登录状态、地区限制和风控策略会变化，因此“支持平台”不代表每条链接在任何网络环境下都一定可访问。

## 安装

### WebUI 安装

插件发布到市场后，在 AstrBot WebUI 中搜索：

```text
astrbot_plugin_media_parser_nova
```

### 手动安装

1. 将本项目目录放入 AstrBot 的插件目录。
2. 确认目录名为 `astrbot_plugin_media_parser_nova`。
3. 安装 `requirements.txt` 中的依赖，或让 AstrBot 在加载插件时自动安装。
4. 重启 AstrBot，在插件配置页完成设置。

依赖包括 `aiohttp`、`cryptography`、`qrcode[pil]` 和 `pillow`。Python 版本建议为 3.10 或更高，AstrBot 版本要求为 4.25 或更高。

## 基础配置

插件默认开启各平台的“全部发送”，安装后即可尝试发送链接。常用配置如下：

### 解析器与输出模式

每个平台可以单独选择：

- **关闭**：不提取、不解析该平台链接
- **全部发送**：发送文本摘要和图片/视频
- **仅文本**：只发送标题、作者、正文等摘要，不下载媒体
- **仅富媒体**：只发送图片/视频，不发送文本摘要

### 触发方式

- **自动解析链接**：消息中出现支持的链接时自动处理
- **回复触发解析**：引用包含链接的消息，再发送配置关键词
- **手动触发关键词**：关闭自动解析时使用，例如 `视频解析`

### 文本字段

在“消息输出 → 文本元数据”中，可以分别控制标题、作者、发布时间、原始链接和正文是否展示。关闭字段只影响消息展示，也会同步减少提交给翻译模型的内容。

### 消息聚合

- **不聚合**：逐条发送
- **全部聚合**：尽量使用合并转发，超大视频仍可能单独发送
- **按条件聚合**：图片、视频或节点数量达到阈值时才聚合

## 缓存、代理与 ffmpeg

### 缓存目录

建议为插件配置可写的缓存目录。缓存目录可用时，插件可以先下载媒体再发送，能够携带必要的 Referer、Cookie 和请求头，通常比直接发送远程 URL 更稳定。

以下能力通常需要缓存目录：

- 图片下载和发送
- B 站 Cookie 解锁后的 DASH 音视频合并
- 微博视频下载
- 小黑盒视频、BBS 媒体和 M3U8
- Twitter/X 视频缓存
- Pixiv 图片缓存

非 Docker 环境下，插件优先使用 AstrBot 数据目录中的：

```text
plugin_data/astrbot_plugin_media_parser_nova/cache
```

Docker 环境请确保该目录对 AstrBot 和协议端的权限、挂载关系符合部署方式。

### 代理

TikTok、Twitter/X、Pixiv、小黑盒等平台可能受到地区或网络限制。可以在“代理设置”中填写 HTTP 或 SOCKS5 代理，并按平台开启解析或媒体下载代理。

### ffmpeg

以下功能可能需要系统安装 `ffmpeg`：

- DASH 音视频合并
- M3U8 封装
- 视频仅发送封面时截取首帧

没有 ffmpeg 时，插件仍会尽量使用原始封面或普通直链路径；无法完成的媒体会在摘要中说明原因。

## B 站 Cookie

在“B 站增强”中开启 Cookie 解析并填写浏览器请求中的 Cookie，可以解锁账号允许的更高画质。Cookie 失效后，可以开启“管理员协助登录”，由插件私聊管理员完成扫码更新。

建议：

- 不要把 Cookie 写入公开仓库、截图或聊天记录
- 管理员 ID 只填写可信账号
- Cookie 失效时先确认缓存目录和 ffmpeg 可用

## Twitter/X 说明

Twitter/X 默认优先使用 FxTwitter 接口，服务不可用时回退到 Guest GraphQL。图片和视频 CDN 可能需要代理或缓存下载。

本版本特别修复了一个会导致 X 链接解析失败的问题：原代码在构造 Twitter 元数据时直接引用未定义的 `avatar_url`，从而出现 `NameError: name 'avatar_url' is not defined`。现在会从解析结果读取头像并提供空值兜底，即使头像缺失也不会阻断整条推文的解析。

同时，X 链接提取改为保留消息中的出现顺序，并增强了视频候选项的容错处理。

## ZIP 归档

可在“消息输出 → 导出行为 → ZIP 归档”中设置归档命令。使用方式：

1. 引用一条包含可解析链接的消息。
2. 发送只包含归档命令的消息。
3. 插件生成 ZIP，并写入解析摘要、结构化详情和已成功下载的媒体。

Nova 版本的归档根目录为 `media_parser_nova/`，不会与原插件的归档目录混用。归档总大小可配置，无法下载的媒体会在 `metadata.txt` 中保留原链接和失败原因。

## 安全与隐私

- Cookie、请求头、Token 和本地文件路径不会写入 ZIP 的公开详情字段
- 媒体请求默认使用公网地址校验，避免误访问本地或内网地址
- 下载失败不会静默伪装成成功，摘要会显示跳过或失败原因
- 解析频率限制可以按链接和用户分别配置
- 请遵守目标平台的服务条款、版权要求和当地法律法规

## 与上游的关系

本项目参考并基于上游插件：

- [astrbot_plugin_media_parser_yaya](https://github.com/xiaoxi2760/astrbot_plugin_media_parser_yaya)

感谢上游作者 `xiaoxi2760` 及其贡献者提供的解析器、下载器、配置和 AstrBot 集成实现。本项目保留原项目许可与必要的第三方许可文件，并通过独立的插件标识维护自己的配置、缓存和日志命名空间。

当前 `metadata.yaml` 已指向本项目的公开仓库；上游仓库仅作为来源和参考项目保留在致谢部分。

## 其他致谢

- [astrbot_plugin_media_parser](https://github.com/drdon1234/astrbot_plugin_media_parser)：早期解析器实现与平台适配参考
- [astrbot_plugin_rika_share](https://github.com/iris1598/astrbot_plugin_rika_share)：卡片渲染参考（MIT License）
- [nonebot-plugin-parser](https://github.com/maoxig/nonebot-plugin-parser)：CommonRenderer 参考
- [FxEmbed](https://github.com/FxEmbed/FxEmbed)：Twitter/X 解析服务参考
- [bilibili-API-collect](https://github.com/SocialSisterYi/bilibili-API-collect)：B 站接口资料
- [Johnserf-Seed/f2](https://github.com/Johnserf-Seed/f2)：抖音 `a_bogus` 签名实现参考

## 许可证

本项目主体遵循 [GNU Affero General Public License v3.0](LICENSE)。仓库中的 `LICENSES/` 目录包含部分第三方代码对应的许可文本，请在再分发时一并保留。

## 问题反馈

反馈问题时请尽量提供：

- AstrBot 版本、插件版本和运行环境
- 目标平台与完整链接类型，不要公开 Cookie
- 开启“管理与调试 → debug 模式”后的相关日志
- 是否配置了缓存目录、代理和 ffmpeg

如果同时安装了原插件，请先确认是否发生了双插件重复解析，再提交问题。
