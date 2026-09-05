# YouTube 说明

YouTube 解析走官方 Innertube 接口与 yt-dlp 两条腿，**不依赖任何第三方镜像站或解析服务**。标题、头像、统计与热评始终由 Innertube 提供；可下载的视频流交给哪条腿，由「取流来源」这一项决定。每一步失败只降级、不中断：

```text
① 元数据   oembed ‖ Innertube player   并发
           → 都拿不到 videoDetails 时补跑 TVHTML5_SIMPLY（门禁下它仍然下发完整字段）
           → 仍然没有则抓 watch 页内嵌的 ytInitialPlayerResponse
② 媒体流   Innertube: streamingData → dash → progressive → HLS → 仅视频轨
           yt-dlp:    执行播放器 JS，还原带签名挑战 / SABR 的直链
③ 增强     Innertube next → 作者头像 / 播放量 / 点赞数 / 评论数 / 热评
④ 补位     本轮出流方交不出流时换另一条腿再试；yt-dlp 的 info 顺手
           补齐被门禁吞掉的标题 / 作者 / 时长 / 统计
```

支持的链接形态：`youtube.com/watch?v=`、`youtu.be/`、`/shorts/`、`/live/`、`/embed/`、`/v/`、`music.youtube.com`，以及分享用的 `/attribution_link?u=...`（会自动解包内层地址）。

## 配置项

在「YouTube 设置」中：

| 配置 | 默认 | 说明 |
| --- | --- | --- |
| 画质上限 | 1080 | 按视频高度限制。选「不限制」会尽量取最高画质；挑流时还会叠一层体积预算，放不下就自动降档，见 [视频体积与发送上限](configuration.md#视频体积与发送上限) |
| 允许 dash 分离流 | 开 | 音视频分离流 + ffmpeg 合并，才能拿到 1080P 及以上。没装 ffmpeg 请关闭 |
| 视频流取用来源 | 自动 | 谁负责交出可下载的流，见 [谁来出流](#谁来出流) |
| Innertube 客户端顺序 | `ios,android_vr` | 逗号分隔，按顺序试到拿到可下载的流为止。Cookie 可用时自动在末尾追加 `tv_downgraded,tv,web` |
| 单次解析总时间预算 | 45 秒 | 元数据、媒体流、增强三段**共享**这一个预算，而不是每段各自超时 |
| YouTube Cookie | 空 | 公开视频不需要。填入登录 Cookie 可绕过机器人验证、解析年龄限制内容 |
| Cookie 失效时私聊提醒管理员 | 开 | 检测到 Cookie 掉登录态时向管理员私聊发一条带步骤的提醒（不打扰群聊）。若插件从没见过管理员的私聊会话，会退而发到管理员最近说话的那个会话 |
| Cookie 失效提醒冷却 | 120 分钟 | 两次提醒之间的最短间隔，避免刷屏 |
| 自动跟进 Cookie 轮换 | 开 | 像真实浏览器一样吸收服务端下发的新 Cookie 并落盘，让手工导出的那份长期免维护 |
| Cookie 体检间隔 | 6 小时 | 登录态体检的周期，填 0 连同续期一起关掉。续期本身是独立的固定节奏（20 分钟一轮），见 [让 Cookie 长期不用再管](#让-cookie-长期不用再管) |
| 疑难视频用 yt-dlp 兜底取流 | 开 | 允许 yt-dlp 参与取流。需要额外装三件套，见下文；关掉等于把取流来源锁死在官方接口 |
| yt-dlp 使用的 JS 运行时 | `auto` | `auto` 按 deno → node → bun → quickjs 自动挑一个可用的；也可指定其中之一 |
| yt-dlp 兜底超时 | 60 秒 | 单次 yt-dlp 取流的上限，超时按封面卡片降级。这一项**不占**上面那份总时间预算 |
| PO Token 提供方地址 | 空 | 选填。装了 `bgutil-ytdlp-pot-provider` 后通常留空即可（用它自己的默认位置）。填 `http(s)://` 走 HTTP 服务模式，填路径走脚本模式，见下文 |
| PO Token 取用策略 | 自动 | 自动 / 总是 / 从不。「总是」每次取流都强制生成令牌，多花 1～3 秒 |

热评开关在「消息输出 → 附加内容：热评 → YouTube」，默认开启。

**两条链路都会按体积挑流**：先估算每个候选格式的大小（`contentLength` / `filesize`，缺失时用 `tbr × 时长` 反推），在「可发送视频体积上限」之内取最高画质，全部超限时退回最小的那一档。

## 谁来出流

Innertube 快、只是一次 HTTP 请求；yt-dlp 要拉起 JS 运行时子进程、耗时数秒，但它能还原带签名挑战和 SABR 的直链，也自带 PO Token 与匿名兜底客户端。「视频流取用来源」就是在这两者之间做取舍：

| 取值 | 行为 | 适合 |
| --- | --- | --- |
| **自动**（默认） | 先走 Innertube；**连续 2 次**被机器人门禁挡下就进 **30 分钟**冷却期，冷却期内直接由 yt-dlp 出流，一次成功立刻解除冷却 | 绝大多数部署 |
| **优先官方接口** | 永远先试 Innertube，只在它交不出流时才让 yt-dlp 补位，不做冷却 | 出口 IP 干净，不愿频繁拉起 JS 运行时 |
| **仅 yt-dlp** | 跳过 Innertube 出流，官方接口只负责元数据、头像与热评 | 机房 IP 长期被门禁盯上，且已装好 yt-dlp 三件套（最好配 PO Token 提供方） |

冷却机制的意义在于：门禁看的是出口 IP 的信誉，连续被拦说明这个 IP 短时间内已经进了黑名单，每次再去试一遍只是白等一轮超时。门禁计数只统计官方接口真的下场出流的那几趟，不会被自己的降级结果无限续期。

yt-dlp 还有一次**提前出手**的时机：官方接口连标题都没拿到（门禁会把 `videoDetails` 整块吞掉）时，它的 `info` 就是卡片唯一的信息来源，此时不等出流阶段，直接跑一趟把标题、作者、时长、统计和封面补回来——否则这条链接只能报「元数据获取失败」。

关掉「疑难视频用 yt-dlp 兜底取流」，或者三件套没装齐时，三档取值都退化成纯 Innertube。

## 为什么默认只有两个客户端

实测（对 11 个 Innertube 客户端逐一验证）的结论是：**匿名状态下只有 `ios` 和 `android_vr` 会真正返回可直连的 `adaptiveFormats`**，其余客户端一律 `UNPLAYABLE` 或 `LOGIN_REQUIRED`，连一条流都拿不到。

| 客户端 | 匿名出流 | 支持 Cookie 鉴权 | 用途 |
| --- | --- | --- | --- |
| `ios` | ✅ 最高 2160P，无签名挑战 | ❌ | 默认首选 |
| `android_vr` | ✅ 最高 2160P，无签名挑战 | ❌ | 默认备选 |
| `tv_downgraded` | ❌ | ✅ 只在有 Cookie 时才发 | Cookie 可用时追加，排在鉴权客户端最前：它对 Cookie 鉴权最宽容，也不需要 PO Token |
| `tv` | ❌ | ✅ | Cookie 可用时追加 |
| `web` | ❌ | ✅ | Cookie 可用时追加；同时用于 next / 评论端点 |
| `mweb` | ❌ | ✅ | 仅手动指定时使用 |

默认顺序里因此不放注定失败的客户端——它们只会白白消耗时间预算。这两个原生客户端返回的直链还有一个好处：不带限速挑战参数，也不带 `signatureCipher`，无需在本地执行 YouTube 的播放器 JS 就能全速下载。

带 `signatureCipher` 的流在 Innertube 这一侧会被直接跳过：还原它要下载并运行播放器 JS，这份维护成本交给 yt-dlp 更划算。

三个鉴权客户端只在 Cookie **确实可用**时才挂上链，Cookie 一旦被判失效就整组摘掉，见 [判死之后自动退回匿名](#判死之后自动退回匿名)。

## 「Sign in to confirm you’re not a bot」

一部分视频会被 YouTube 的机器人门禁拦下，`playabilityStatus` 返回 `LOGIN_REQUIRED`。这是**按视频**触发的（同一台机器上有的视频正常、有的被拦），机房 / VPS 出口 IP 上尤其常见。

实测已经排除的无效手段，不必再试：

- 换客户端、换客户端版本号、换 User-Agent（11 个客户端全军覆没）
- `params=8AEB` / `params=CgIQBg` 等播放参数
- `X-Goog-Visitor-Id` + `context.client.visitorData`
- watch 页加 `bpctr=9999999999&has_verified=1`（页面里干脆没有 `adaptiveFormats`）

真正有效的三条路：

1. **让 yt-dlp 出流**。它自带匿名兜底客户端与 PO Token 支持，很多被 Innertube 判 `LOGIN_REQUIRED` 的视频它照样能取到流。默认的「自动」档在连续撞门禁后就会自动切过去。
2. **填 Cookie**。Cookie 必须包含 `SAPISID` 或 `__Secure-3PAPISID`——只发 `Cookie` 头 Innertube 会当匿名请求处理，插件会额外算出 `Authorization: SAPISIDHASH` 才算真正登录。填写后 `tv_downgraded`、`tv`、`web` 会自动追加到尝试链末尾。Cookie 里找不到 `SAPISID` 时会在日志里给出警告并退回匿名。
3. **走住宅代理**（`代理设置 → YouTube`）。门禁很大程度上看出口 IP 的信誉。

插件**自己不实现** PO token 与播放器 JS 签名还原：前者要跑一整套 BotGuard 虚拟机、后者随时被改，两者都会把这个解析器变成需要持续追着上游改的负担。这份维护成本转交给了 yt-dlp；两条腿都取不到流时，插件退化成封面卡片而不是报错。

账号风控提醒：用于填 Cookie 的账号有被限制的风险，建议用小号。

## yt-dlp 这条腿

YouTube 给 Web 端下发的流越来越多是 **SABR**（服务端自适应码率）：`adaptiveFormats` 里既没有 `url` 也没有 `signatureCipher`，唯一的 progressive 流只给 `signatureCipher`，必须真的执行 YouTube 的播放器 JS 才能还原成可用地址。日志里的表现是「有元数据但无可直连媒体流」。

这类流由 **yt-dlp** 负责。复用它而不是自己写签名还原，理由很直接：上游每隔几周就换一次算法，追着改的成本远高于装一个包。

### 需要装三件套

缺件时这条腿只会安静退场并在日志里给出可直接照做的建议，不会报错。在 **AstrBot 所用的那个 Python 环境**里装前两件：

```bash
pip install -U yt-dlp yt-dlp-ejs
```

再装一个 JS 运行时（任选其一）：**node ≥ 22** / **deno ≥ 2.3** / **bun ≥ 1.2.11** / **quickjs ≥ 2023.12.9**。Debian / Ubuntu 上 `apt install nodejs` 给的版本通常太老，用 NodeSource 或 nvm 装 22 以上。

三件缺一不可：`yt-dlp` 把 JS 挑战外包给外部运行时，`yt-dlp-ejs` 提供求解脚本，运行时负责真正跑这段 JS。**只装 yt-dlp 是不够的**，这也是很多人只升级 yt-dlp 却依旧撞「Sign in to confirm you’re not a bot」的原因。

> **一个很隐蔽的坑**：yt-dlp 的 `--js-runtimes` 默认值只有 `deno`，机器上装了 node 也不会被启用（日志里表现为 `JS runtimes: none`）。插件会自己探测并显式声明用哪个运行时，所以不需要手动配；但如果在命令行直接跑 yt-dlp 复现问题，记得加 `--js-runtimes node`。

### Cookie 与 yt-dlp 的关系

yt-dlp 自己带一条**匿名链**（`android_vr` + web 系客户端 + PO Token），不带 Cookie 也常常能出流。真正需要有效 Cookie 的是年龄限制、会员限定、私享这类内容。

关键在于：**给它一份已经失效的 Cookie，比什么都不给更糟。** yt-dlp 只要在 cookie 文件里读到登录凭据就进入 `is_authenticated` 分支，把所有「不支持 Cookie」的客户端整批摘掉——`android_vr` 首当其冲，整条匿名兜底链就这么被自己砍断了。所以插件只在 Cookie 判定可用时才把它写给 yt-dlp，一旦判死就按匿名跑。

插件会把当前登录态（含自动吸收的轮换值）写成一份 Netscape cookies.txt 交给 yt-dlp，文件放在缓存目录下、权限 `0600`，**每次取流前都按运行时的权威 Cookie 重写一遍**。不需要额外为 yt-dlp 准备一份 Cookie。

> 为什么必须每次重写：yt-dlp 收到 `cookiefile` 后，会在收工时把它自己的 cookie 罐**存回同一个文件**。如果这一趟 YouTube 下发过删除指令，`SID` / `SAPISID` / `LOGIN_INFO` 这些登录核心项就会被就地抹掉——之后只要复用这份文件，就永远按匿名跑。插件自己的 `cookie.json` 是权威来源，yt-dlp 那份只是每次现生成的副本。

### 行为细节

- 触发时机由「视频流取用来源」决定：**仅 yt-dlp** 档每次都由它出流，**自动**档在门禁冷却期内由它出流，其余情况只在 Innertube 交不出流时补位
- 补位那趟刻意排在头像 / 热评抓取之后——它要跑数秒并拉起 JS 运行时子进程，先把卡片内容拿到手更稳妥。唯一的例外是官方接口连标题都没拿到，那时它提前出手兼职元数据来源
- 选流偏好与 Innertube 一致：dash 分离流（画质最高）> progressive 单文件 > 仅视频轨；同样受「画质上限」和「允许 dash 分离流」两项配置约束
- 同一时刻只跑一个 yt-dlp 任务，避免多条链接同时拉起多个 JS 运行时把 CPU 打满
- yt-dlp 给出的直链与它取链时用的 User-Agent 绑定，插件会自动改用同一个 UA 下载，否则必定 403
- 顺手回收 `info` 里的作者、简介、时长、播放量、点赞、评论数与封面，只填官方接口留下的空位，已有值一律不动
- yt-dlp 自身的输出全部降到 DEBUG；取流成功在 INFO 留一行摘要（流类型、清晰度、格式、耗时），失败只出一条 WARNING

## 可选增强：PO Token provider

**PO Token** 是 YouTube 的 BotGuard 证明令牌。yt-dlp 自己不生成它，而是留了一层插件接口，交给第三方 provider 去跑 BotGuard。社区的事实标准是 `bgutil-ytdlp-pot-provider`。插件这边同样不实现 BotGuard，只做两件事：**探测**有没有装 provider（结果写进日志摘要，缺了会在降级告警里给出安装方式），以及把地址**透传**给 yt-dlp。

先说结论，免得白折腾：

| 遇到的现象 | PO Token 能不能救 |
| --- | --- |
| 拿到了元数据，但媒体流 403 / 只有 SABR（日志：「有元数据但无可直连媒体流」） | **能**，这正是它的用途 |
| Innertube 在播放器阶段就被拦（日志：`playabilityStatus=LOGIN_REQUIRED`） | 对 Innertube 这条腿**没有帮助**——请求还没走到取流就被挡回，插件也不在官方接口上带令牌。但它能给 yt-dlp 的 web 系客户端多开一条路，所以这类视频值得直接交给 yt-dlp 出流 |

第二种是机房 IP 的常态。真机实测过：provider 装好、日志确认 `Retrieved a player PO Token`（令牌确实生成了），Innertube 侧被拦的那几个视频加不加令牌都还是 `LOGIN_REQUIRED`；换 `web` / `mweb` / `tv_simply` / `web_embedded` / `android` 等客户端、强制每次取令牌、间隔重试，全部一样。对官方接口这条腿来说，这类问题只能靠**住宅／家宽出口代理**或**有效 Cookie**解决；把出流交给 yt-dlp 则是另一条独立的出路。

### 两种模式与安装

provider 有两种运行形态，插件都支持：

- **脚本模式（推荐）**：不常驻进程，需要令牌时现拉起一次 Node 跑完就退，单次约 1～3 秒。省内存，适合小机器。
- **HTTP 服务模式**：常驻一个 Node 服务（默认 `127.0.0.1:4416`），令牌走 HTTP 取，延迟更低但要多养一个进程。

脚本模式装法（在 **AstrBot 所用的那个 Python 环境** 里装 pip 包）：

```bash
pip install -U bgutil-ytdlp-pot-provider

# 生成脚本单独 clone 一份并编译（版本号与上面的 pip 包保持一致）
git clone --depth 1 --branch 1.3.2 \
  https://github.com/Brainicism/bgutil-ytdlp-pot-provider.git \
  ~/bgutil-ytdlp-pot-provider
cd ~/bgutil-ytdlp-pot-provider/server && npm ci && npx tsc
```

需要 **Node ≥ 18**，编译产物约 190 MB（主要是 npm 依赖），运行时零常驻内存。装完可以这样确认 yt-dlp 认到了：

```bash
yt-dlp -v --js-runtimes node "https://www.youtube.com/watch?v=dQw4w9WgXcQ" 2>&1 | grep -i "pot"
# 期望看到 PO Token Providers: ... bgutil:script-node-1.3.2 (external)
```

路径就放在 `~/bgutil-ytdlp-pot-provider/server` 时**插件侧零配置**——那是 provider 自己的默认位置。

### 两个配置项

| 配置项 | 作用 |
| --- | --- |
| `youtube.ytdlp_pot_provider` | 选填。留空即用 provider 默认位置（脚本模式 `~/bgutil-ytdlp-pot-provider/server`，HTTP 模式 `127.0.0.1:4416`）。填 `http://` 或 `https://` 开头的地址走 HTTP 模式，填目录或脚本路径走脚本模式 |
| `youtube.ytdlp_fetch_pot` | 取用策略。**自动**（默认，由 yt-dlp 判断这次要不要令牌）／**总是**（每次都强制生成，多花 1～3 秒，只在确实被 403/SABR 反复挡住时才值得）／**从不**（跳过令牌，用于排查 provider 自身故障） |

两项都留默认时插件不会往 yt-dlp 传任何 `extractor_args`，这是有意的：`auto` 交给 yt-dlp 判断更省时间，地址写死反而会挡掉 provider 的默认值。另外「总是」只在真的探测到 provider 时才生效——没装却要求必须取令牌只会让 yt-dlp 直接报错。

## 让 Cookie 长期不用再管

先说清一件事：**严格意义上不存在「永不过期」的 YouTube Cookie**，Google 不发长期令牌。但只要把下面三层都做对，一份手工导出的 Cookie 可以长期不用回来重新导，实际效果就等同于不过期。

### 为什么 YouTube Cookie 看起来「特别容易失效」

真正的原因不是有效期短，而是**它一直在被服务端轮换**。`__Secure-1PSIDTS` / `__Secure-3PSIDTS` / `SIDCC` 这几项每隔一段时间就会换新值：浏览器静默跟进，所以自己刷 YouTube 永远不用重新登录；而一个只会把静态文本原样发出去的客户端，等于一个**永远不更新凭据的浏览器**，服务端迟早判定这个会话已经过期。

另一个坑是导出方式：产出这份 Cookie 的浏览器会话如果还活着并继续刷新，或者事后点了「登出」，服务端会直接把对应的 `SAPISID` 作废。

### 第一层：用「冻结会话」的方式导出一次

1. 开一个**无痕 / 隐私窗口**，登录准备当小号用的 Google 账号。
2. **在同一个标签页**跳转到 `https://www.youtube.com/robots.txt`。这一步让页面停在一个不会自己发后台请求、也不会刷新令牌的静态页上。
3. 用浏览器扩展 **Get cookies.txt LOCALLY**（注意选「LOCALLY」那个，纯本地导出、不回传服务器）导出当前站点的 Cookie。Edge 用户注意：Edge 加载项商店里没有这个扩展，可以在 Edge 里打开 Chrome 应用店并允许「来自其他商店的扩展」，或者用商店里的 **Cookie-Editor** 代替。
4. 把导出内容整段填进「YouTube 设置 → Cookie」（多行输入框，可拖高）。**三种格式都能直接粘**，插件会自动认出来并转换：
   - `cookies.txt`（Netscape 格式，`Get cookies.txt LOCALLY` 的产物，含制表符的多行文本）
   - 扩展导出的 **JSON 数组**（`Cookie-Editor` / `EditThisCookie` 的默认格式）
   - `a=1; b=2` 形式的 **Cookie 请求头**（浏览器开发者工具里直接抄的那种）

   唯一的硬要求是里面得含有 `SAPISID` 或 `__Secure-3PAPISID`，否则 Innertube 会把请求当匿名处理（日志里会给出警告，并带上识别到的 Cookie 条数，方便判断是不是格式问题）。粘贴时如果换行被输入框吃掉，插件也能靠 `域名 TRUE 路径 TRUE 过期时间` 的字段结构把整份 `cookies.txt` 还原出来。
5. **直接关掉整个无痕窗口，绝对不要点登出。** 点一次登出，刚导出的这份 Cookie 会立刻作废。

### 第二层：让插件自动续期并跟进轮换（默认开启）

插件内置一个 YouTube 登录态运行时，行为对齐真实浏览器：

- **续期**：每 **20 分钟**向 Google 账号服务的 `/RotateCookies` 发一个空 POST（只带账号域凭据），换回新的 `__Secure-1PSIDTS` / `__Secure-3PSIDTS`。这正是浏览器长期不掉登录的原生机制，成本只有一次请求，所以可以跑得很密。
  - 服务端下发了新凭据 → 会话仍然有效；
  - HTTP 401 / 403 → 会话确已被吊销，判死；
  - 其余情况（含网络失败）→ 本次无定论，补一次登录态体检再下结论。
- **体检**：按「Cookie 体检间隔」（默认 6 小时）向 `youtube.com` 首页发一次带登录态的轻量请求，读页面里的 `LOGGED_IN` 字段；被甩到 Google 登录页这件事本身也算「已掉登录态」。插件启动后的第一轮就带体检。
- **吸收**：每次 YouTube 响应里的 `Set-Cookie` 都会被合并回内存中的 Cookie 罐，后续请求发的是服务端最新认可的那份值。4xx 响应也会先吸收再报错——门禁响应同样会带新 Cookie。
- **落盘**：合并结果原子写入缓存目录下的 `runtime_manager/youtube/cookie.json`（权限 `0600`），插件重载、AstrBot 重启后接着用轮换后的新值，而不是回退到配置里那份越来越旧的文本。

几个刻意的设计约束：

- 只吸收**身份类 + 轮换类白名单**里的 Cookie 名，埋点 Cookie 不会把罐子撑大。
- 续期请求只带**账号域**凭据，`VISITOR_INFO1_LIVE` / `YSC` 这类 YouTube 埋点不会被送到账号服务。
- 服务端下发的**删除指令**（`Max-Age=0`、值为 `EXPIRED` / 空）会被忽略，避免一次异常响应就地把登录态清空。
- 配置里换了新 Cookie 时，靠指纹比对自动丢弃旧的运行时状态，不会出现新旧凭据串味。
- 日志里**只出现 Cookie 名，绝不输出取值**；运行时文件在 AstrBot 缓存目录下，不在仓库里。

关掉「自动跟进 Cookie 轮换」会退回「永远原样回放配置里那串静态文本」的行为，也就回到了 Cookie 会慢慢腐烂的状态。「Cookie 体检间隔」填 0 则连后台续期一起停掉，Cookie 只在每次解析时被动跟进轮换。

### 第三层：住宅代理（比 Cookie 更耐用）

论耐用度，**住宅代理往往比 Cookie 更划算**：代理不会「过期」，也不用定期回来重新导出，而机器人门禁本身很大程度上就是在看出口 IP 的信誉。条件允许时优先配 `代理设置 → YouTube`，机房 IP 是撞门禁的主要原因。

### 判死之后自动退回匿名

Cookie 被判死不是终点，也不需要手动去配置里清掉它。健康态一旦翻到「失效」：

- Innertube 客户端链摘掉 `tv_downgraded` / `tv` / `web` 这三个鉴权客户端；
- 解析请求不再带 `Cookie` 头，也不再算 `SAPISIDHASH`；
- yt-dlp 的 cookie 文件里也不写凭据，好让它自己那条匿名 + PO Token 链保持完整。

最后一条尤其关键：一份死 Cookie 会让 yt-dlp 摘掉所有不支持 Cookie 的客户端，等于把本来能成功的匿名链一起拖下水。

**探活请求仍然用完整 Cookie。** 判死后每一轮维护都会强制补一次体检，服务端一确认在线就立刻复活，鉴权客户端自动回到链上。健康态告警只在**翻转的那一次**打，不会每次解析都刷一遍。

日志里那行「Cookie 维护」摘要就是体检报告：续期结果、体检结果、Cookie 条数、是否已鉴权、上次轮换距今多久、当前健康态。

### 其他要点

- yt-dlp 早期那套 OAuth / 设备码登录方案**已被 YouTube 封禁**，没有比 Cookie 更省事的合法登录途径。
- Cookie 真的失效时插件不会闷着：日志出 WARNING，同时（默认开启）私聊管理员一条带上述 5 步指引的提醒，并带冷却避免刷屏。续期或体检发现会话被吊销时走同一条提醒链路。
- 始终用小号。这个账号有被 Google 风控限制的风险。
- 不要把 Cookie 写进公开仓库、截图或聊天记录。

## 代理只有一个开关

`代理设置 → YouTube` 这一个开关同时控制解析请求和媒体下载。googlevideo 直链与取流时的出口 IP 绑定，如果解析走代理、下载走直连（或反过来），下载必定 403。因此这里不像其他平台那样拆成「解析代理」「下载代理」两项。

## 取不到视频流时

机器人验证、会员限定、地区限制、年龄限制、私享视频、正在直播等情况下拿不到可下载的流。**两条腿都失败时**插件不会报错，而是退化成「封面 + 标题 + 作者 + 时长 + 统计 + 热评」的卡片，并在卡片上标明原因（例如「被 YouTube 机器人验证挡下，仅展示封面与信息」）。

**信息卡片是完整的**：门禁会把出流客户端的 `videoDetails` 整块吞掉，插件会自动补跑一个专门的元数据客户端（TVHTML5_SIMPLY），把标题、作者、时长、播放量捞回来；点赞数与评论数从 `next` 端点单独取；这些都没拿到时还有 yt-dlp 的 `info` 兜着。所以被拦下的视频依旧能出「👀 播放 / 👍 点赞 / 💬 评论」齐全的统计行和正确的时长，只是没有视频文件。

体积原因跳过视频时不算失败，也走同一套「封面 + 信息」降级，卡片和消息里会写明实际体积与上限，见 [视频体积与发送上限](configuration.md#视频体积与发送上限)。

日志分层：完整的降级链走 DEBUG，正常解析在 INFO 留一行摘要（视频 ID、流类型、由谁取的流、客户端、热评条数、耗时）。**取不到流时会额外打一条 WARNING**，一行写清全部上下文：本次的取流策略、尝试过的客户端链、当次的登录态（匿名 / Cookie 已鉴权 / Cookie 已判失效）、代理是否启用、门禁返回的状态码与 `playabilityStatus`、yt-dlp 链路的可用性，以及对应的处理建议（缺哪件就说装哪件）。
