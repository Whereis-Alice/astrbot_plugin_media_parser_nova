# YouTube 说明

YouTube 解析主路走官方 Innertube 接口，**不依赖任何第三方镜像站或解析服务**。整条链路分四层，每层失败只降级、不中断：

```text
① 元数据   oembed ‖ Innertube player   并发
           → 都拿不到 videoDetails 时补跑 TVHTML5_SIMPLY（门禁下它仍然下发完整字段）
           → 仍然没有则抓 watch 页内嵌的 ytInitialPlayerResponse
② 媒体流   streamingData → dash → progressive → HLS → 仅视频轨
③ 增强     Innertube next → 作者头像 / 播放量 / 点赞数 / 评论数 / 热评
④ 兜底     ②③ 都没拿到流时交给 yt-dlp 执行播放器 JS 解出直链（可关）
```

前三层是轻量 HTTP 请求，绝大多数视频到这里就结束了。第 ④ 层是给「Innertube 只下发 SABR 流」的疑难视频准备的后手，要拉起一个 JS 运行时、耗时数秒，所以只在前面全都失败时才付这份代价。

支持的链接形态：`youtube.com/watch?v=`、`youtu.be/`、`/shorts/`、`/live/`、`/embed/`、`/v/`、`music.youtube.com`，以及分享用的 `/attribution_link?u=...`（会自动解包内层地址）。

## 配置项

在「YouTube 设置」中：

| 配置 | 默认 | 说明 |
| --- | --- | --- |
| 画质上限 | 1080 | 按视频高度限制。选「不限制」会尽量取最高画质；挑流时还会叠一层体积预算，放不下就自动降档，见 [视频体积与发送上限](configuration.md#视频体积与发送上限) |
| 允许 dash 分离流 | 开 | 音视频分离流 + ffmpeg 合并，才能拿到 1080P 及以上。没装 ffmpeg 请关闭 |
| Innertube 客户端顺序 | `ios,android_vr` | 逗号分隔，按顺序试到拿到可下载的流为止 |
| 单次解析总时间预算 | 45 秒 | 前三层**共享**这一个预算，而不是每层各自超时 |
| Cookie | 空 | 公开视频不需要。填入登录 Cookie 可绕过机器人验证、解析年龄限制内容 |
| Cookie 失效时私聊管理员 | 开 | 检测到 Cookie 掉登录态时向管理员私聊发一条带步骤的提醒（不打扰群聊）。若插件从没见过管理员的私聊会话，会退而发到管理员最近说话的那个会话 |
| Cookie 失效提醒冷却 | 120 分钟 | 两次提醒之间的最短间隔，避免刷屏 |
| 自动跟进 Cookie 轮换 | 开 | 像真实浏览器一样吸收服务端下发的新 Cookie 并落盘，让手工导出的那份长期免维护 |
| Cookie 保鲜间隔 | 6 小时 | 长期没有解析请求时主动触发一次轮换并体检登录态；填 0 关闭 |
| yt-dlp 兜底取流 | 开 | Innertube 一路都没取到流时，借 yt-dlp 执行播放器 JS 解出直链。需要额外装三件套，见下文 |
| yt-dlp 的 JS 运行时 | `auto` | `auto` 按 deno → node → bun → quickjs 自动挑一个可用的；也可指定其中之一 |
| yt-dlp 兜底超时 | 60 秒 | 单次兜底的上限，超时按封面卡片降级。这一项**不占**上面那份总时间预算 |
| PO Token 提供方地址 | 空 | 选填。装了 `bgutil-ytdlp-pot-provider` 后通常留空即可（用它自己的默认位置）。填 `http(s)://` 走 HTTP 服务模式，填路径走脚本模式，见下文 |
| PO Token 取用策略 | 自动 | 自动 / 总是 / 从不。「总是」每次兜底都强制生成令牌，多花 1～3 秒 |

热评开关在「消息输出 → 附加内容：热评 → YouTube」，默认开启。

**InnerTube 与 yt-dlp 两条链路都会按体积挑流**：先估算每个候选格式的大小（`contentLength` / `filesize`，缺失时用 `tbr × 时长` 反推），在「可发送视频体积上限」之内取最高画质，全部超限时退回最小的那一档。

## 为什么默认只有两个客户端

实测（对 11 个 Innertube 客户端逐一验证）的结论是：**匿名状态下只有 `ios` 和 `android_vr` 会真正返回可直连的 `adaptiveFormats`**，其余客户端一律 `UNPLAYABLE` 或 `LOGIN_REQUIRED`，连一条流都拿不到。

| 客户端 | 匿名出流 | 支持 Cookie 鉴权 | 用途 |
| --- | --- | --- | --- |
| `ios` | ✅ 最高 2160P，无签名挑战 | ❌ | 默认首选 |
| `android_vr` | ✅ 最高 2160P，无签名挑战 | ❌ | 默认备选 |
| `tv` | ❌ | ✅ | 配了 Cookie 后自动追加 |
| `web` | ❌ | ✅ | 配了 Cookie 后自动追加；同时用于 next / 评论端点 |
| `mweb` | ❌ | ✅ | 仅手动指定时使用 |

默认顺序里因此不放注定失败的客户端——它们只会白白消耗时间预算。这两个原生客户端返回的直链还有一个好处：不带限速挑战参数，也不带 `signatureCipher`，无需在本地执行 YouTube 的播放器 JS 就能全速下载。

带 `signatureCipher` 的流会被直接跳过：还原它需要下载并运行播放器 JS，维护成本高且随时会被改，而靠客户端选择已经能拿到干净直链。

## 「Sign in to confirm you are not a bot」

一部分视频会被 YouTube 的机器人门禁拦下，`playabilityStatus` 返回 `LOGIN_REQUIRED`。这是**按视频**触发的（同一台机器上有的视频正常、有的被拦），机房 / VPS 出口 IP 上尤其常见。

实测已经排除的无效手段，不必再试：

- 换客户端、换客户端版本号、换 User-Agent（11 个客户端全军覆没）
- `params=8AEB` / `params=CgIQBg` 等播放参数
- `X-Goog-Visitor-Id` + `context.client.visitorData`
- watch 页加 `bpctr=9999999999&has_verified=1`（页面里干脆没有 `adaptiveFormats`）

真正有效的两条路：

1. **填 Cookie**。Cookie 必须包含 `SAPISID` 或 `__Secure-3PAPISID`——只发 `Cookie` 头 Innertube 会当匿名请求处理，插件会额外算出 `Authorization: SAPISIDHASH` 才算真正登录。填写后会自动把 `tv`、`web` 这两个支持鉴权的客户端追加到尝试链末尾。Cookie 里找不到 `SAPISID` 时会在日志里给出警告并退回匿名。
2. **走住宅代理**（`代理设置 → YouTube`）。门禁很大程度上看出口 IP 的信誉。

插件**自己不实现** PO token 与播放器 JS 签名还原：前者要跑一整套 BotGuard 虚拟机、后者随时被改，两者都会把这个解析器变成需要持续追着上游改的负担。这份维护成本转交给了 yt-dlp——需要执行播放器 JS 才能拿到的直链由第 ④ 层兜底解决；连兜底也失败时，插件退化成封面卡片而不是报错。

账号风控提醒：用于填 Cookie 的账号有被限制的风险，建议用小号。

## 疑难视频的 yt-dlp 兜底

有一类视频即使 Cookie 已经正确鉴权，Innertube 也交不出可下载的直链——它下发的是 **SABR**（服务端自适应码率）流：`adaptiveFormats` 里既没有 `url` 也没有 `signatureCipher`，唯一的 progressive 流只给 `signatureCipher`，必须真的执行 YouTube 的播放器 JS 才能还原成可用地址。日志里的表现是「有元数据但无可直连媒体流」。

这种情况下插件调用 **yt-dlp** 兜底。选择复用 yt-dlp 而不是自己写签名还原，理由很直接：上游每隔几周就换一次算法，追着改的成本远高于装一个包。

### 需要装三件套

兜底默认开启，但缺件时它只会安静降级并在日志里给出可直接照做的建议，不会报错。在 **AstrBot 所用的那个 Python 环境**里装前两件：

```bash
pip install -U yt-dlp yt-dlp-ejs
```

再装一个 JS 运行时（任选其一）：**node ≥ 22** / **deno ≥ 2.3** / **bun ≥ 1.2.11** / **quickjs ≥ 2023.12.9**。Debian / Ubuntu 上 `apt install nodejs` 给的版本通常太老，用 NodeSource 或 nvm 装 22 以上。

三件缺一不可：`yt-dlp` 把 JS 挑战外包给外部运行时，`yt-dlp-ejs` 提供求解脚本，运行时负责真正跑这段 JS。**只装 yt-dlp 是不够的**，这也是很多人只升级 yt-dlp 却依旧撞「Sign in to confirm you’re not a bot」的原因。

> **一个很隐蔽的坑**：yt-dlp 的 `--js-runtimes` 默认值只有 `deno`，机器上装了 node 也不会被启用（日志里表现为 `JS runtimes: none`）。插件会自己探测并显式声明用哪个运行时，所以不需要手动配；但如果在命令行直接跑 yt-dlp 复现问题，记得加 `--js-runtimes node`。

### 仍然需要 Cookie

实测：装齐三件套但不带 Cookie，被门禁的视频依旧是 `Sign in to confirm you’re not a bot`。兜底解决的是「拿不到直链」，不是「进不了门」——门还是得靠 Cookie 或住宅代理开。两者配齐才是完整方案。

插件会把当前登录态（含自动吸收的轮换值）写成一份 Netscape cookies.txt 交给 yt-dlp，文件放在缓存目录下、权限 `0600`，**每次兜底前都按运行时的权威 Cookie 重写一遍**。不需要额外为 yt-dlp 准备一份 Cookie。

> 为什么必须每次重写：yt-dlp 收到 `cookiefile` 后，会在收工时把它自己的 cookie 罐**存回同一个文件**。如果这一趟 YouTube 下发过删除指令，`SID` / `SAPISID` / `LOGIN_INFO` 这些登录核心项就会被就地抹掉——之后只要复用这份文件，兜底就永远按匿名跑。插件自己的 `cookie.json` 是权威来源，yt-dlp 那份只是每次现生成的副本。

### 行为细节

- 只在前面几层**一路都没取到流**时才触发，正常视频完全不受影响，也不会多耗时间
- 刻意排在头像 / 热评抓取之后，所以即使兜底最终失败，卡片内容也已经是齐的
- 选流偏好与主路一致：dash 分离流（画质最高）> progressive 单文件 > 仅视频轨；同样受「画质上限」和「允许 dash 分离流」两项配置约束
- 同一时刻只跑一个兜底任务，避免多条链接同时拉起多个 JS 运行时把 CPU 打满
- yt-dlp 给出的直链与它取链时用的 User-Agent 绑定，插件会自动改用同一个 UA 下载，否则必定 403
- yt-dlp 自身的输出全部降到 DEBUG；兜底成功在 INFO 留一行摘要（流类型、清晰度、格式、耗时），失败只出一条 WARNING
- 不想用可以直接关掉「yt-dlp 兜底取流」，行为回到纯 Innertube

## 可选增强：PO Token provider

**PO Token** 是 YouTube 的 BotGuard 证明令牌。yt-dlp 自己不生成它，而是留了一层插件接口，交给第三方 provider 去跑 BotGuard。社区的事实标准是 `bgutil-ytdlp-pot-provider`。插件这边同样不实现 BotGuard，只做两件事：**探测**有没有装 provider（结果写进日志摘要，缺了会在降级告警里给出安装方式），以及把地址**透传**给 yt-dlp。

先说结论，免得白折腾：

| 遇到的现象 | PO Token 能不能救 |
| --- | --- |
| 拿到了元数据，但媒体流 403 / 只有 SABR（日志：「有元数据但无可直连媒体流」） | **能**，这正是它的用途 |
| 播放器阶段就被拦（日志：`playabilityStatus=LOGIN_REQUIRED`、`Sign in to confirm you’re not a bot`） | **不能**。请求还没走到取流就被挡回，令牌根本没有介入的机会 |

第二种是机房 IP 的常态。真机实测过：provider 装好、日志确认 `Retrieved a player PO Token`（令牌确实生成了），但被拦的那几个视频加不加令牌都还是 `LOGIN_REQUIRED`；换 `web` / `mweb` / `tv_simply` / `web_embedded` / `android` 等客户端、强制每次取令牌、间隔重试，全部一样。这类只能靠**住宅／家宽出口代理**或**有效 Cookie**解决。

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
| `youtube.ytdlp_fetch_pot` | 取用策略。**自动**（默认，由 yt-dlp 判断这次要不要令牌）／**总是**（每次兜底都强制生成，多花 1～3 秒，只在确实被 403/SABR 反复挡住时才值得）／**从不**（跳过令牌，用于排查 provider 自身故障） |

两项都留默认时插件不会往 yt-dlp 传任何 `extractor_args`，这是有意的：`auto` 交给 yt-dlp 判断更省时间，地址写死反而会挡掉 provider 的默认值。另外「总是」只在真的探测到 provider 时才生效——没装却要求必须取令牌只会让 yt-dlp 直接报错。

## 让 Cookie 长期不用再管

先说清一件事：**严格意义上不存在「永不过期」的 YouTube Cookie**，Google 不发长期令牌。但只要把下面三层都做对，一份手工导出的 Cookie 可以长期不用回来重新导，实际效果就等同于不过期。

### 为什么 YouTube Cookie 看起来「特别容易失效」

真正的原因不是有效期短，而是**它一直在被服务端轮换**。`__Secure-1PSIDTS` / `__Secure-3PSIDTS` / `SIDCC` 这几项每隔一段时间就会通过 `Set-Cookie` 下发新值：浏览器静默跟进，所以自己刷 YouTube 永远不用重新登录；而插件如果只把配置里那串静态文本一直原样发出去，就等于一个**永远不更新凭据的浏览器**，服务端迟早判定这个会话已经过期。

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

### 第二层：让插件自动跟进轮换（默认开启）

插件内置了一个 YouTube 登录态运行时，行为对齐真实浏览器：

- **吸收**：每次 YouTube 响应里的 `Set-Cookie` 都会被合并回内存中的 Cookie 罐，后续请求发的是服务端最新认可的那份值。4xx 响应也会先吸收再报错——门禁响应同样会带新 Cookie。
- **落盘**：合并结果原子写入缓存目录下的 `runtime_manager/youtube/cookie.json`（权限 `0600`），插件重载、AstrBot 重启后接着用轮换后的新值，而不是回退到配置里那份越来越旧的文本。
- **保鲜**：默认每 6 小时主动向 `youtube.com` 首页发一次带登录态的轻量请求，主动触发一次轮换，顺带体检登录态：页面里的 `LOGGED_IN` 字段、或者被甩到 Google 登录页这件事本身，都会被判成「已掉登录态」并触发提醒。

几个刻意的设计约束：

- 只吸收**身份类 + 轮换类白名单**里的 Cookie 名，埋点 Cookie 不会把罐子撑大。
- 服务端下发的**删除指令**（`Max-Age=0`、值为 `EXPIRED` / 空）会被忽略，避免一次异常响应就地把登录态清空。
- 配置里换了新 Cookie 时，靠指纹比对自动丢弃旧的运行时状态，不会出现新旧凭据串味。
- 日志里**只出现 Cookie 名，绝不输出取值**；运行时文件在 AstrBot 缓存目录下，不在仓库里。

关掉「自动跟进 Cookie 轮换」会退回「永远原样回放配置里那串静态文本」的行为——同时也就回到了 Cookie 会慢慢腐烂的状态。

### 第三层：住宅代理（比 Cookie 更耐用）

论耐用度，**住宅代理往往比 Cookie 更划算**：代理不会「过期」，也不用定期回来重新导出，而机器人门禁本身很大程度上就是在看出口 IP 的信誉。条件允许时优先配 `代理设置 → YouTube`，机房 IP 是撞门禁的主要原因。

### 其他要点

- yt-dlp 早期那套 OAuth / 设备码登录方案**已被 YouTube 封禁**，没有比 Cookie 更省事的合法登录途径。第 ④ 层的 yt-dlp 兜底同样要靠这份 Cookie 才能过门禁。
- Cookie 真的失效时插件不会闷着：日志出 WARNING，同时（默认开启）私聊管理员一条带上述 5 步指引的提醒，并带冷却避免刷屏。保鲜请求发现服务端判定未登录时也会走同一条提醒链路。
- 始终用小号。这个账号有被 Google 风控限制的风险。
- 不要把 Cookie 写进公开仓库、截图或聊天记录。

## 代理只有一个开关

`代理设置 → YouTube` 这一个开关同时控制解析请求和媒体下载。googlevideo 直链与取流时的出口 IP 绑定，如果解析走代理、下载走直连（或反过来），下载必定 403。因此这里不像其他平台那样拆成「解析代理」「下载代理」两项。

## 取不到视频流时

机器人验证、会员限定、地区限制、年龄限制、私享视频、正在直播等情况下拿不到可下载的流。**连 yt-dlp 兜底也失败时**插件不会报错，而是退化成「封面 + 标题 + 作者 + 时长 + 统计 + 热评」的卡片，并在卡片上标明原因（例如「被 YouTube 机器人验证挡下，仅展示封面与信息」）。

**信息卡片是完整的**：门禁会把出流客户端的 `videoDetails` 整块吞掉，插件会自动补跑一个专门的元数据客户端（TVHTML5_SIMPLY），把标题、作者、时长、播放量捞回来；点赞数与评论数从 `next` 端点单独取。所以被拦下的视频依旧能出「👀 播放 / 👍 点赞 / 💬 评论」齐全的统计行和正确的时长，只是没有视频文件。

体积原因跳过视频时不算失败，也走同一套「封面 + 信息」降级，卡片和消息里会写明实际体积与上限，见 [视频体积与发送上限](configuration.md#视频体积与发送上限)。

日志分层：完整的降级链走 DEBUG，正常解析在 INFO 留一行摘要（视频 ID、流类型、客户端、热评条数、耗时）。**取不到流时会额外打一条 WARNING**，一行写清全部上下文：尝试过的客户端链、当次的登录态（匿名 / Cookie 已鉴权 / Cookie 已失效）、代理是否启用、门禁返回的状态码与 `playabilityStatus`、yt-dlp 兜底链路的可用性，以及对应的处理建议（缺哪件就说装哪件）。
