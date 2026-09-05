"""YouTube yt-dlp 兜底运行时：Innertube 取不到流时借 yt-dlp 解出直链。

为什么需要这一层：YouTube 已对 Web 端全面改用 SABR（服务端自适应码率）
分发，`streamingData.adaptiveFormats` 里既没有 `url` 也没有
`signatureCipher`，唯一的 progressive 流又只给 `signatureCipher`——必须真
的执行播放器 JS 才能还原签名。自行实现签名还原与 PO Token 的维护成本极
高（上游每隔几周就换一次算法），所以这一层直接复用 yt-dlp 的现成能力，
把维护成本转移给上游。

代价与取舍：一次 `extract_info` 需要数秒并会拉起一个 JS 运行时子进程，
因此它只作为兜底——常规视频仍走轻量的 Innertube 直取路径，只有取不到流
时才付这份代价。

环境要求（缺一不可；缺件时本模块只是安静降级并给出可操作建议，不抛错）：

* `yt-dlp` >= 2026.08.19：新架构把 JS 挑战交给外部运行时求解；
* `yt-dlp-ejs`：挑战求解脚本的分发包；
* 一个 JS 运行时：deno >= 2.3 / node >= 22 / bun >= 1.2.11 / quickjs。

一个极隐蔽的坑：yt-dlp 的 `--js-runtimes` 默认值只有 `deno`，机器上装了
node 也不会被启用（日志里表现为 "JS runtimes: none"）。所以本模块总是把
探测到的运行时显式写进 `js_runtimes` 选项。

PO Token 提供方（可选件）：YouTube 对部分请求要求 BotGuard 令牌（PO
Token）。yt-dlp 自己不产生这类令牌，而是留了一层插件接口，由第三方
provider（如 bgutil-ytdlp-pot-provider）去跑 BotGuard。本模块的立场不变
——不自己实现 BotGuard，只做两件事：探测当前 Python 环境里有没有装
provider 插件，以及把用户配置的地址/路径透传给 yt-dlp 的 extractor_args。
装不装都不影响兜底链路可用（缺 provider 不计入 problems）。

实测边界（别抱错期待）：PO Token 能救的是 SABR / gvs 403 那一类「有元数
据但拿不到媒体流」的情况；如果 YouTube 在播放器响应阶段就回
`playabilityStatus=LOGIN_REQUIRED`（典型是机房 IP 信誉太差被要求人机验
证），令牌根本没有介入的机会，此时只能靠住宅出口代理或有效 Cookie。

安全约定：Cookie 只以名值写入权限 0600 的临时 jar 文件，日志里一律只出
现 Cookie 项数，绝不输出取值。
"""

from __future__ import annotations

import asyncio
import os
import pkgutil
import re
import stat
import tempfile
import threading
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from ....logger import logger
from .cookie import normalize_cookie_input, parse_cookie_header


__all__ = [
    "JS_RUNTIME_PREFERENCE",
    "YtDlpEnvironment",
    "YtDlpStream",
    "YtDlpStreamResolver",
    "probe_ytdlp_environment",
    "reset_ytdlp_environment_cache",
]


# yt-dlp 官方的运行时优先级；最低版本仅用于生成安装建议文案。
JS_RUNTIME_PREFERENCE: Tuple[str, ...] = ("deno", "node", "bun", "quickjs")
_RUNTIME_HINT = "node>=22 / deno>=2.3 / bun>=1.2.11 / quickjs>=2023.12.9"

# PO Token 提供方相关。地址以 http(s):// 开头视为 HTTP 服务模式，否则当作
# 脚本模式的 server 目录/脚本路径。
_POT_URL_RE = re.compile(r"^https?://", re.I)
_FETCH_POT_CHOICES: Tuple[str, ...] = ("auto", "always", "never")
_POT_PLUGIN_HINT = (
    "pip install -U bgutil-ytdlp-pot-provider，"
    "并按其 README 准备生成脚本或 HTTP 服务"
)
# provider 插件模块名的常见前缀，剥掉后更适合放进日志摘要。
_POT_NAME_PREFIXES: Tuple[str, ...] = ("getpot_", "get_pot_", "pot_")

# yt-dlp 的 protocol 取值里只有这两个是「一个 URL 直接下载完整媒体」；
# http_dash_segments / m3u8 / m3u8_native / sabr 都需要额外的拼装逻辑。
_DIRECT_PROTOCOLS = frozenset({"http", "https"})

_COOKIE_JAR_NAME = "ytdlp_cookies.txt"
# Netscape jar 每行都要一个过期时间。YouTube 的登录凭据由 Cookie 运行时
# 负责轮换，这里统一给远期时间，免得 yt-dlp 把会话 Cookie 当成已过期丢弃。
_JAR_EXPIRY = 2147483647
# Netscape jar 是制表符分隔的行式格式：名或值里混进空白或分号会直接把
# 该行结构撕开，yt-dlp 会整行丢弃（日志里只有一句 invalid length）。
_JAR_UNSAFE = re.compile(r"[\s;]")

# 编解码器优先级与 Innertube 侧保持一致：avc1/mp4a 兼容性最好，ffmpeg 能
# 直接 copy 合流。
_VIDEO_CODEC_RANK = (("avc1", 3), ("avc3", 3), ("vp9", 2), ("vp09", 2), ("av01", 1))
_AUDIO_CODEC_RANK = (("mp4a", 3), ("opus", 2), ("vorbis", 1), ("ec-3", 1))

_PROBE_LOCK = threading.Lock()
_PROBE_CACHE: Dict[str, "YtDlpEnvironment"] = {}
# 缺件告警只出一次，避免每条链接都刷一遍相同的安装建议。
_WARNED_PROBLEMS: set = set()


def _as_int(value: Any) -> int:
    """尽力把任意值读成非负整数，读不出算 0。"""
    try:
        return max(0, int(float(value)))
    except (TypeError, ValueError):
        return 0


def _codec_rank(text: str, table: Tuple[Tuple[str, int], ...]) -> int:
    """按编解码器给出优先级分值，未知编码得 0。"""
    lowered = (text or "").lower()
    for token, rank in table:
        if token in lowered:
            return rank
    return 0


class _SilentLogger:
    """把 yt-dlp 的输出全部转进 debug，避免污染用户可见日志。"""

    @staticmethod
    def debug(msg: Any) -> None:
        logger.debug(f"[youtube][yt-dlp] {msg}")

    @staticmethod
    def info(msg: Any) -> None:
        logger.debug(f"[youtube][yt-dlp] {msg}")

    @staticmethod
    def warning(msg: Any) -> None:
        logger.debug(f"[youtube][yt-dlp] {msg}")

    @staticmethod
    def error(msg: Any) -> None:
        logger.debug(f"[youtube][yt-dlp] {msg}")


# ── 环境探测 ──────────────────────────────────────────────

@dataclass(frozen=True)
class YtDlpEnvironment:
    """一次 yt-dlp 兜底链路可用性探测的结果快照。"""

    available: bool = False
    version: str = ""
    # 新架构（>= 2026.08）把 JS 挑战外包给 yt-dlp-ejs + 外部运行时；更老的
    # 版本自带 jsinterp，不需要这两件，所以必须分开判断而不是比版本号。
    needs_js_runtime: bool = False
    ejs_available: bool = False
    runtime_name: str = ""
    runtime_version: str = ""
    # 已安装的 PO Token 提供方插件模块名。属于可选增强件，故意不进
    # problems——缺它不影响兜底链路可用。
    pot_providers: Tuple[str, ...] = ()
    problems: Tuple[str, ...] = ()

    @property
    def ready(self) -> bool:
        """三件套是否齐全，可以真正发起兜底解析。"""
        return bool(self.available and not self.problems)

    def summary(self) -> str:
        """给日志用的一行环境摘要。"""
        if not self.available:
            return "未安装 yt-dlp"
        parts = [f"yt-dlp {self.version or '未知版本'}"]
        if self.needs_js_runtime:
            parts.append("yt-dlp-ejs " + ("就绪" if self.ejs_available else "缺失"))
            if self.runtime_name:
                label = self.runtime_name
                if self.runtime_version:
                    label = f"{label} {self.runtime_version}"
                parts.append(f"JS 运行时 {label}")
            else:
                parts.append("无可用 JS 运行时")
        else:
            parts.append("使用内置 jsinterp")
        if self.pot_providers:
            parts.append(f"POT 提供方 {_pot_label(self.pot_providers)}")
        else:
            parts.append("无 POT 提供方")
        return "，".join(parts)

    def advice(self) -> str:
        """针对缺件给出可直接照做的处理建议；齐全时返回空串。"""
        steps: List[str] = []
        if not self.available:
            steps.append(
                "在 AstrBot 所用的 Python 环境执行 "
                "pip install -U yt-dlp yt-dlp-ejs"
            )
        else:
            if self.needs_js_runtime and not self.ejs_available:
                steps.append("执行 pip install -U yt-dlp-ejs")
            if self.needs_js_runtime and not self.runtime_name:
                steps.append(f"安装一个 JS 运行时（{_RUNTIME_HINT}）")
        if not steps:
            return ""
        return "；处理建议: " + "；".join(steps)

    def pot_advice(self) -> str:
        """没装 PO Token 提供方时给出安装提示；装了则返回空串。

        与 `advice()` 分开是因为性质不同：那边是"缺了就不能跑"，这边是
        "装了可能更稳"，不该混进同一条告警里误导人。
        """
        if self.pot_providers:
            return ""
        return (
            "；可选增强: 未检测到 PO Token 提供方，"
            "若遇到取到元数据但媒体流 403/SABR 受阻，可 "
            f"{_POT_PLUGIN_HINT}"
        )


def _probe_js_runtime(preference: str = "") -> Tuple[str, str, bool]:
    """挑一个可用的 JS 运行时。

    Returns:
        (运行时名, 版本, 是否为 EJS 架构)。第三项区分「yt-dlp 太老、根本没
        有运行时注册表」与「有注册表但一个可用的都没有」两种情况。
    """
    try:
        # 运行时是在 yt_dlp 包 import 期间注册的，必须先 import 再取注册表。
        import yt_dlp  # noqa: F401
        from yt_dlp.globals import supported_js_runtimes
    except Exception:
        return "", "", False
    try:
        registry = dict(supported_js_runtimes.value or {})
    except Exception:
        return "", "", False
    if not registry:
        return "", "", False
    order: List[str] = []
    wanted = (preference or "").strip().lower()
    if wanted and wanted not in ("auto", "any", ""):
        order.append(wanted)
    for name in JS_RUNTIME_PREFERENCE:
        if name not in order:
            order.append(name)
    for name in registry:
        if name not in order:
            order.append(name)
    for name in order:
        factory = registry.get(name)
        if factory is None:
            continue
        try:
            info = factory().info
        except Exception:
            continue
        if info is None or not getattr(info, "supported", False):
            continue
        tuple_version = getattr(info, "version_tuple", None) or ()
        version = ".".join(str(part) for part in tuple_version)
        if not version:
            version = str(getattr(info, "version", "") or "")
        return name, version, True
    return "", "", True


def _ytdlp_version(module: Any) -> str:
    """读出 yt-dlp 版本号。

    2026.08 起包顶层不再导出 `__version__`，只有 `yt_dlp.version` 子模块里
    还有；只 getattr 顶层会拿到空串，日志里表现为「yt-dlp 未知版本」。
    """
    version = str(getattr(module, "__version__", "") or "").strip()
    if version:
        return version
    try:
        from yt_dlp.version import __version__ as packaged
    except Exception:
        return ""
    return str(packaged or "").strip()


def _probe_ejs() -> bool:
    """yt-dlp-ejs 能 import 就视为求解脚本已就绪。"""
    try:
        import yt_dlp_ejs  # noqa: F401
    except Exception:
        return False
    return True


def _probe_pot_providers() -> Tuple[str, ...]:
    """列出已安装的 PO Token 提供方插件模块名。

    只扫 `yt_dlp_plugins.extractor` 命名空间包下的模块名，不 import 任何一
    个实现。理由：provider 插件在 import 期就会做环境检查（找 node、探
    HTTP 服务），import 一遍既慢又可能抛错，而我们这里只需要知道"装了没"。
    做法保持通用，不写死某个 provider 的包名。

    Returns:
        排序后的模块名元组；探测失败或一个都没有时返回空元组。
    """
    try:
        import yt_dlp_plugins.extractor as extractor_ns

        paths = list(getattr(extractor_ns, "__path__", []) or [])
    except Exception:
        return ()
    if not paths:
        return ()
    found: List[str] = []
    try:
        for module in pkgutil.iter_modules(paths):
            name = str(getattr(module, "name", "") or "")
            if "pot" in name.lower() and name not in found:
                found.append(name)
    except Exception:
        return ()
    return tuple(sorted(found))


def _pot_label(names: Tuple[str, ...]) -> str:
    """把 provider 模块名整理成适合写进日志的短标签。"""
    labels: List[str] = []
    for name in names:
        short = name
        for prefix in _POT_NAME_PREFIXES:
            if short.startswith(prefix):
                short = short[len(prefix):]
                break
        short = short.strip("_") or name
        if short not in labels:
            labels.append(short)
    return "/".join(labels)


def _probe_uncached(preference: str) -> YtDlpEnvironment:
    """不走缓存地做一次完整探测。"""
    try:
        import yt_dlp
    except Exception:
        return YtDlpEnvironment(problems=("未安装 yt-dlp",))
    version = _ytdlp_version(yt_dlp)
    runtime_name, runtime_version, ejs_arch = _probe_js_runtime(preference)
    ejs_available = _probe_ejs() if ejs_arch else False
    pot_providers = _probe_pot_providers()
    problems: List[str] = []
    if ejs_arch:
        if not ejs_available:
            problems.append("缺少 yt-dlp-ejs")
        if not runtime_name:
            problems.append(f"没有可用的 JS 运行时（需要 {_RUNTIME_HINT}）")
    return YtDlpEnvironment(
        available=True,
        version=version,
        needs_js_runtime=ejs_arch,
        ejs_available=ejs_available,
        runtime_name=runtime_name,
        runtime_version=runtime_version,
        pot_providers=pot_providers,
        problems=tuple(problems),
    )


def probe_ytdlp_environment(preference: str = "") -> YtDlpEnvironment:
    """探测兜底链路是否齐全；结果按运行时偏好缓存，避免反复起子进程。"""
    key = (preference or "").strip().lower()
    with _PROBE_LOCK:
        cached = _PROBE_CACHE.get(key)
        if cached is None:
            cached = _probe_uncached(key)
            _PROBE_CACHE[key] = cached
    return cached


def reset_ytdlp_environment_cache() -> None:
    """清空探测缓存，供测试与「装完包再复查」使用。"""
    with _PROBE_LOCK:
        _PROBE_CACHE.clear()
        _WARNED_PROBLEMS.clear()


# ── 取流 ──────────────────────────────────────────────────

@dataclass(frozen=True)
class YtDlpStream:
    """yt-dlp 选出的一路可直连媒体。"""

    url: str
    kind: str
    height: int = 0
    # yt-dlp 给出的直链与它请求时用的 UA 绑定，下游下载必须复用，否则 403。
    user_agent: str = ""
    filesize: int = 0
    detail: str = ""


def _is_direct(fmt: Any) -> bool:
    """只接受可直连的 http(s) 单流。

    顺带排除三类不可直连的东西：SABR（protocol=sabr）、DASH/HLS 清单，以及
    storyboard 缩略图轨（ext=mhtml）。protocol 必须精确匹配白名单——
    `http_dash_segments` 同样以 http 开头，但它指向的是需要逐段拼装的清单，
    直接丢给下游下载器只会拿到一个 .mpd。
    """
    if not isinstance(fmt, dict):
        return False
    url = fmt.get("url")
    if not isinstance(url, str) or not url.startswith(("http://", "https://")):
        return False
    if str(fmt.get("protocol") or "").lower() not in _DIRECT_PROTOCOLS:
        return False
    if str(fmt.get("ext") or "").lower() == "mhtml":
        return False
    return True


def _has_video(fmt: Dict[str, Any]) -> bool:
    return str(fmt.get("vcodec") or "none").lower() != "none"


def _has_audio(fmt: Dict[str, Any]) -> bool:
    return str(fmt.get("acodec") or "none").lower() != "none"


def _format_user_agent(fmt: Dict[str, Any]) -> str:
    """取出该路流对应的 UA。"""
    headers = fmt.get("http_headers")
    if isinstance(headers, dict):
        for name, value in headers.items():
            if str(name).lower() == "user-agent" and isinstance(value, str):
                return value
    return ""


def _format_size(fmt: Dict[str, Any], duration_seconds: int = 0) -> int:
    """取该路流的体积（字节）；yt-dlp 没给时用 tbr×时长折算，估不出返回 0。"""
    declared = _as_int(fmt.get("filesize") or fmt.get("filesize_approx"))
    if declared > 0:
        return declared
    tbr = _as_int(fmt.get("tbr"))
    duration = _as_int(fmt.get("duration")) or max(0, _as_int(duration_seconds))
    if tbr <= 0 or duration <= 0:
        return 0
    # tbr 单位是 kbit/s。
    return int(tbr * 1000 * duration / 8)


def _fits_budget(size_bytes: int, max_bytes: int) -> bool:
    """无预算或体积未知时都算放得下。"""
    return max_bytes <= 0 or size_bytes <= 0 or size_bytes <= max_bytes


def _format_label(fmt: Dict[str, Any]) -> str:
    """给日志用的一路流的简短标签。"""
    bits = [str(fmt.get("format_id") or "?")]
    if _has_video(fmt):
        bits.append(f"{_as_int(fmt.get('height'))}p")
    bits.append(str(fmt.get("ext") or ""))
    return "/".join(bit for bit in bits if bit)


class YtDlpStreamResolver:
    """用 yt-dlp 兜底解析一条 YouTube 视频的可直连媒体流。"""

    def __init__(
        self,
        proxy: Optional[str] = None,
        max_height: int = 1080,
        allow_dash: bool = True,
        timeout: float = 60.0,
        js_runtime: str = "auto",
        cookie_dir: str = "",
        pot_provider: str = "",
        fetch_pot: str = "auto",
        max_bytes: int = 0,
    ):
        self.proxy = (proxy or "").strip() or None
        self.max_height = max(0, _as_int(max_height))
        # 可发送体积预算（字节）：选流时优先挑塞得进去的那一路，0 表示不限。
        self.max_bytes = max(0, _as_int(max_bytes))
        self.allow_dash = bool(allow_dash)
        self.timeout = max(10.0, float(timeout or 60.0))
        self.js_runtime = (js_runtime or "auto").strip().lower() or "auto"
        self.cookie_dir = (cookie_dir or "").strip()
        self.pot_provider = (pot_provider or "").strip()
        mode = (fetch_pot or "auto").strip().lower()
        self.fetch_pot = mode if mode in _FETCH_POT_CHOICES else "auto"
        self._jar_path = ""
        # yt-dlp 一次解析会起子进程并跑 JS，串行化避免并发请求把 CPU 打满。
        self._gate = asyncio.Semaphore(1)

    # ── Cookie jar ───────────────────────────────────────

    def _jar_dir(self) -> str:
        if self.cookie_dir:
            return self.cookie_dir
        return tempfile.gettempdir()

    def _ensure_cookie_jar(self, cookie_header: str, revision: int) -> str:
        """把当前 Cookie 头落成 Netscape jar 供 yt-dlp 使用。

        每次调用都按运行时的权威 Cookie 重写文件，不做任何复用。原因：
        yt-dlp 拿到 ``cookiefile`` 后会在收工时把它自己的 jar 存回同一个
        路径，服务端下发过删除指令的条目（SID / SAPISID / LOGIN_INFO 等
        登录核心）会被就地抹掉。一旦复用这份被削过的文件，兜底解析就会
        永久按匿名跑。相比一次网络请求，写 2KB 磁盘的开销可以忽略。

        ``revision`` 只为兼容调用方保留，不再参与任何缓存判定。
        """
        # 调用方通常给的是已规范化的 Cookie 头，但配置里也可能是整段
        # cookies.txt（WebUI 会把换行压成空格）。这里再兜一次，避免把一整
        # 段文本当成单个 Cookie 名写进 jar。
        header = normalize_cookie_input(cookie_header)
        if not header:
            return ""
        cookies = parse_cookie_header(header)
        if not cookies:
            return ""
        lines = [
            "# Netscape HTTP Cookie File",
            "# 由 Nova 插件依当前 YouTube 登录态生成，供 yt-dlp 兜底使用。",
        ]
        dropped: List[str] = []
        for name, value in cookies.items():
            if _JAR_UNSAFE.search(name) or _JAR_UNSAFE.search(value):
                dropped.append(name)
                continue
            lines.append(
                "\t".join(
                    [
                        ".youtube.com",
                        "TRUE",
                        "/",
                        "TRUE",
                        str(_JAR_EXPIRY),
                        name,
                        value,
                    ]
                )
            )
        if len(lines) <= 2:
            logger.debug("[youtube] yt-dlp Cookie jar 无可用条目，按匿名处理")
            return ""
        if dropped:
            logger.debug(
                "[youtube] yt-dlp Cookie jar 跳过 "
                f"{len(dropped)} 项含空白/分号的条目: {', '.join(dropped)}"
            )
        text = "\n".join(lines) + "\n"
        path = self._write_jar(text)
        if path:
            self._jar_path = path
        return path

    def _write_jar(self, text: str) -> str:
        """原子写入 jar 文件并把权限收敛到 0600。"""
        directory = self._jar_dir()
        try:
            os.makedirs(directory, exist_ok=True)
            path = os.path.join(directory, _COOKIE_JAR_NAME)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(text)
            try:
                os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)
            except OSError:
                pass
            os.replace(tmp, path)
            return path
        except OSError as exc:
            logger.debug(f"[youtube] yt-dlp Cookie jar 写入失败: {exc}")
            return ""

    # ── 选项与调用 ───────────────────────────────────────

    def _extractor_args(self, env: YtDlpEnvironment) -> Dict[str, Any]:
        """组装与 PO Token 相关的 extractor_args。

        默认（fetch_pot=auto、未填地址）什么都不传，这是有意为之：

        * `auto` 交给 yt-dlp 自己判断何时需要令牌。强制 `always` 的代价是
          每次兜底都要拉起一个 node 子进程跑 BotGuard，实测 1~3 秒，而多
          数视频根本不需要令牌；
        * 地址留空时 provider 自己有合理默认值——脚本模式默认找
          `~/bgutil-ytdlp-pot-provider/server`，HTTP 模式默认
          `127.0.0.1:4416`。这里写死反而会把这些默认值挡掉。

        另外 `always` 只在真的探测到 provider 时才传：没有提供方却要求必须
        取令牌，只会让 yt-dlp 直接报错，比不传更糟。
        """
        args: Dict[str, Any] = {}
        if self.fetch_pot == "never" or (
            self.fetch_pot == "always" and env.pot_providers
        ):
            args["youtube"] = {"fetch_pot": [self.fetch_pot]}
        if self.pot_provider:
            if _POT_URL_RE.match(self.pot_provider):
                args["youtubepot-bgutilhttp"] = {
                    "base_url": [self.pot_provider]
                }
            else:
                args["youtubepot-bgutilscript"] = {
                    "server_home": [self.pot_provider]
                }
        return args

    def build_options(self, jar_path: str = "") -> Dict[str, Any]:
        """组装 yt-dlp 选项（只取元数据，不下载）。"""
        env = probe_ytdlp_environment(self.js_runtime)
        options: Dict[str, Any] = {
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
            "no_color": True,
            "skip_download": True,
            "noplaylist": True,
            "retries": 1,
            "extractor_retries": 1,
            "socket_timeout": max(5.0, min(30.0, self.timeout)),
            "logger": _SilentLogger(),
        }
        if env.needs_js_runtime and env.runtime_name:
            # 关键：--js-runtimes 默认只有 deno，装了 node 也不会被启用，
            # 必须显式声明；旧版 yt-dlp 不认识这个键，会被安全忽略。
            options["js_runtimes"] = {env.runtime_name: {"path": None}}
        if jar_path:
            options["cookiefile"] = jar_path
        if self.proxy:
            options["proxy"] = self.proxy
        extractor_args = self._extractor_args(env)
        if extractor_args:
            options["extractor_args"] = extractor_args
        return options

    @staticmethod
    def _extract_sync(video_id: str, options: Dict[str, Any]) -> Dict[str, Any]:
        """阻塞地跑一次 yt-dlp 元数据提取，必须在线程里调用。"""
        import yt_dlp

        url = f"https://www.youtube.com/watch?v={video_id}"
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(url, download=False)
        return info if isinstance(info, dict) else {}

    def _warn_unready(self, env: YtDlpEnvironment) -> None:
        """缺件只提醒一次，附上可直接照做的建议。"""
        signature = "|".join(env.problems)
        if signature in _WARNED_PROBLEMS:
            logger.debug(f"[youtube] yt-dlp 兜底不可用: {env.summary()}")
            return
        _WARNED_PROBLEMS.add(signature)
        logger.warning(
            f"[youtube] yt-dlp 兜底暂不可用（{env.summary()}），"
            f"疑难视频只能出封面卡片{env.advice()}"
        )

    async def resolve(
        self,
        video_id: str,
        cookie_header: str = "",
        cookie_revision: int = 0,
    ) -> Optional[YtDlpStream]:
        """解析一条视频；任何失败都只返回 None，由调用方继续降级。"""
        video_id = (video_id or "").strip()
        if not video_id:
            return None
        env = probe_ytdlp_environment(self.js_runtime)
        if not env.ready:
            self._warn_unready(env)
            return None
        jar = self._ensure_cookie_jar(cookie_header, cookie_revision)
        options = self.build_options(jar)
        try:
            async with self._gate:
                info = await asyncio.wait_for(
                    asyncio.to_thread(self._extract_sync, video_id, options),
                    timeout=self.timeout,
                )
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            logger.warning(
                f"[youtube] yt-dlp 兜底超时（上限 {self.timeout:.0f}s）: "
                f"video_id={video_id}"
            )
            return None
        except Exception as exc:
            logger.warning(
                f"[youtube] yt-dlp 兜底解析失败: video_id={video_id}; "
                f"{type(exc).__name__}: {exc}"
            )
            return None
        stream = self.select(info)
        if stream is None:
            logger.warning(
                f"[youtube] yt-dlp 兜底未挑到可直连流: video_id={video_id}"
            )
        return stream

    # ── 选流 ─────────────────────────────────────────────

    def _within_cap(self, fmt: Dict[str, Any]) -> bool:
        if self.max_height <= 0:
            return True
        height = _as_int(fmt.get("height"))
        return height <= self.max_height if height else True

    @staticmethod
    def _resolve(
        candidates: List[Tuple[Tuple[int, ...], Dict[str, Any], int]],
        max_bytes: int,
    ) -> Optional[Dict[str, Any]]:
        """预算内挑评分最高的一路；全都超预算时退让为体积最小的那一路。"""
        if not candidates:
            return None
        within = [
            item for item in candidates if _fits_budget(item[2], max_bytes)
        ]
        if within:
            return max(within, key=lambda item: item[0])[1]
        return min(candidates, key=lambda item: item[2])[1]

    def _best_progressive(
        self,
        formats: List[Dict[str, Any]],
        max_bytes: int = 0,
        duration_seconds: int = 0,
    ) -> Optional[Dict[str, Any]]:
        candidates: List[Tuple[Tuple[int, ...], Dict[str, Any], int]] = []
        for fmt in formats:
            if not (_has_video(fmt) and _has_audio(fmt)):
                continue
            if not self._within_cap(fmt):
                continue
            score = (
                _codec_rank(str(fmt.get("vcodec") or ""), _VIDEO_CODEC_RANK),
                _as_int(fmt.get("height")),
                _as_int(fmt.get("tbr")),
            )
            candidates.append(
                (score, fmt, _format_size(fmt, duration_seconds))
            )
        return self._resolve(candidates, max_bytes)

    def _best_video_only(
        self,
        formats: List[Dict[str, Any]],
        max_bytes: int = 0,
        duration_seconds: int = 0,
    ) -> Optional[Dict[str, Any]]:
        candidates: List[Tuple[Tuple[int, ...], Dict[str, Any], int]] = []
        for fmt in formats:
            if not _has_video(fmt) or _has_audio(fmt):
                continue
            if not self._within_cap(fmt):
                continue
            score = (
                _as_int(fmt.get("height")),
                _codec_rank(str(fmt.get("vcodec") or ""), _VIDEO_CODEC_RANK),
                _as_int(fmt.get("tbr")),
            )
            candidates.append(
                (score, fmt, _format_size(fmt, duration_seconds))
            )
        return self._resolve(candidates, max_bytes)

    def _best_audio_only(
        self,
        formats: List[Dict[str, Any]],
        max_bytes: int = 0,
        duration_seconds: int = 0,
    ) -> Optional[Dict[str, Any]]:
        candidates: List[Tuple[Tuple[int, ...], Dict[str, Any], int]] = []
        for fmt in formats:
            if _has_video(fmt) or not _has_audio(fmt):
                continue
            score = (
                _codec_rank(str(fmt.get("acodec") or ""), _AUDIO_CODEC_RANK),
                _as_int(fmt.get("tbr")),
            )
            candidates.append(
                (score, fmt, _format_size(fmt, duration_seconds))
            )
        return self._resolve(candidates, max_bytes)

    def select(self, info: Any) -> Optional[YtDlpStream]:
        """从 extract_info 的结果里挑一路最合适的流。

        偏好与 Innertube 侧一致：dash 分离流（画质最高）> progressive 单文件
        > 纯视频流兜底。
        """
        formats: List[Dict[str, Any]] = []
        if isinstance(info, dict) and isinstance(info.get("formats"), list):
            formats = [fmt for fmt in info["formats"] if _is_direct(fmt)]
        if not formats:
            return None
        duration = 0
        if isinstance(info, dict):
            duration = max(0, _as_int(info.get("duration")))
        budget = self.max_bytes
        if self.allow_dash:
            audio = self._best_audio_only(formats, budget, duration)
            audio_size = _format_size(audio, duration) if audio else 0
            # 音轨先占位，余下的预算才留给视频轨。
            video_budget = max(1, budget - audio_size) if budget > 0 else 0
            video = self._best_video_only(formats, video_budget, duration)
            if video and audio:
                video_size = _format_size(video, duration)
                return YtDlpStream(
                    url=f"dash:{video['url']}||{audio['url']}",
                    kind="dash",
                    height=_as_int(video.get("height")),
                    user_agent=_format_user_agent(video),
                    filesize=(video_size + audio_size) if video_size else 0,
                    detail=f"{_format_label(video)}+{_format_label(audio)}",
                )
        progressive = self._best_progressive(formats, budget, duration)
        if progressive:
            return YtDlpStream(
                url=str(progressive.get("url") or ""),
                kind="progressive",
                height=_as_int(progressive.get("height")),
                user_agent=_format_user_agent(progressive),
                filesize=_format_size(progressive, duration),
                detail=_format_label(progressive),
            )
        video = self._best_video_only(formats, budget, duration)
        if video:
            return YtDlpStream(
                url=str(video.get("url") or ""),
                kind="video_only",
                height=_as_int(video.get("height")),
                user_agent=_format_user_agent(video),
                filesize=_format_size(video, duration),
                detail=_format_label(video),
            )
        return None
