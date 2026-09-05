# 排查与反馈

## 常见现象

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| Twitter/X 卡片没有热评和互动数字 | X 已不对未登录访问输出带回复数据的页面，属平台行为 | 配置一个 Nitter 实例，见 [Twitter/X → 热评与互动统计需要 Nitter](platforms.md#热评与互动统计需要-nitter) |
| YouTube 只出封面卡片，日志写 `playability=LOGIN_REQUIRED` | 出口 IP 被要求人机验证，或 Cookie 已失效 | 重新导出 Cookie、或换住宅/家宽出口；见 [YouTube → 机器人验证](youtube.md#sign-in-to-confirm-you-are-not-a-bot) |
| YouTube 有完整元数据但「未取到可下载视频流」，`playability=OK` | 该视频只发 SABR 分段流，官方接口拿不到直连地址 | 装上 yt-dlp 兜底，见 [YouTube → 疑难视频的 yt-dlp 兜底](youtube.md#疑难视频的-yt-dlp-兜底) |
| YouTube Cookie 隔几天就失效 | 导出时会话没有冻结，浏览器继续用同一会话导致服务端轮换掉旧凭据 | 按 [让 Cookie 长期不用再管](youtube.md#让-cookie-长期不用再管) 重新导出一次 |
| 只发了信息与封面，没有视频文件 | 预估体积超过生效的发送上限 | 调整 `send_video_max_mb`，见 [视频体积与发送上限](configuration.md#视频体积与发送上限) |
| 发送时报 `[Highway] httpUpload Error ... code 102902` | 协议端富媒体通道拒收大文件，常在传到一半才拒 | 把 `send_video_max_mb` 调到自己部署的实测值以下 |
| 高画质视频没有声音，或合并失败 | DASH 音视频轨需要 `ffmpeg` 合并 | 安装 ffmpeg，见 [ffmpeg](configuration.md#ffmpeg) |
| 图片发不出、B 站高画质拿不到 | 缺少可写缓存目录，无法带 Referer 下载 | 配置缓存目录，见 [缓存目录](configuration.md#缓存目录) |
| B 站画质一直上不去 | 未配置 Cookie 或 Cookie 已失效 | 见 [平台专项 → B 站](platforms.md#b-站) |
| 同一条链接被回复两次 | 与上游插件同时开启了自动解析 | 只保留一个插件的自动解析 |
| 卡片皮肤名和配置里写的对不上 | 旧别名会自动映射到现用名称 | 属正常行为，见 [卡片渲染 → 排版行为](cards.md#排版行为) |

## 看日志

插件按统一分级输出，先看这三类：

- **一行摘要（INFO）**：每次解析结束都会打一条，包含平台、ID、标题、作者、取流方式、清晰度、热评条数和耗时。
- **降级说明（WARNING）**：取不到视频流、热评为空、Cookie 失效等情况会打一条，一行写清尝试过的链路、登录态、代理状态与处理建议。取不到流不算异常，不会抛错。
- **详细降级链（DEBUG）**：逐个客户端 / 数据源的失败原因需要开启「管理与调试 → debug 模式」才会输出。

YouTube 取不到流时的日志字段含义见 [取不到视频流时](youtube.md#取不到视频流时)。

## 反馈问题

反馈时请尽量提供：

- AstrBot 版本、插件版本和运行环境（是否 Docker、宿主系统）
- 目标平台与完整链接类型，**不要公开 Cookie**
- 开启「管理与调试 → debug 模式」后的相关日志
- 是否配置了缓存目录、代理和 ffmpeg

如果同时安装了上游插件，请先确认是否发生了双插件重复解析，再提交问题。
