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

安全约定：Cookie 只以名值写入权限 0600 的临时 jar 文件，日志里一律只出
现 Cookie 项数，绝不输出取值。
"""

from __future__ import annotations

import asyncio
import os
import stat
import tempfile
import threading
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from ....logger import logger
from .cookie import parse_cookie_header


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

# yt-dlp 的 protocol 取值里只有这两个是「一个 URL 直接下载完整媒体」；
# http_dash_segments / m3u8 / m3u8_native / sabr 都需要额外的拼装逻辑。
_DIRECT_PROTOCOLS = frozenset({"http", "https"})

_COOKIE_JAR_NAME = "ytdlp_cookies.txt"
# Netscape jar 每行都要一个过期时间。YouTube 的登录凭据由 Cookie 运行时
# 负责轮换，这里统一给远期时间，免得 yt-dlp 把会话 Cookie 当成已过期丢弃。
_JAR_EXPIRY = 2147483647

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


def _probe_ejs() -> bool:
    """yt-dlp-ejs 能 import 就视为求解脚本已就绪。"""
    try:
        import yt_dlp_ejs  # noqa: F401
    except Exception:
        return False
    return True


def _probe_uncached(preference: str) -> YtDlpEnvironment:
    """不走缓存地做一次完整探测。"""
    try:
        import yt_dlp
    except Exception:
        return YtDlpEnvironment(problems=("未安装 yt-dlp",))
    version = str(getattr(yt_dlp, "__version__", "") or "")
    runtime_name, runtime_version, ejs_arch = _probe_js_runtime(preference)
    ejs_available = _probe_ejs() if ejs_arch else False
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


def _format_size(fmt: Dict[str, Any]) -> int:
    return _as_int(fmt.get("filesize") or fmt.get("filesize_approx"))


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
    ):
        self.proxy = (proxy or "").strip() or None
        self.max_height = max(0, _as_int(max_height))
        self.allow_dash = bool(allow_dash)
        self.timeout = max(10.0, float(timeout or 60.0))
        self.js_runtime = (js_runtime or "auto").strip().lower() or "auto"
        self.cookie_dir = (cookie_dir or "").strip()
        self._jar_path = ""
        self._jar_token: Tuple[int, int] = (-1, -1)
        # yt-dlp 一次解析会起子进程并跑 JS，串行化避免并发请求把 CPU 打满。
        self._gate = asyncio.Semaphore(1)

    # ── Cookie jar ───────────────────────────────────────

    def _jar_dir(self) -> str:
        if self.cookie_dir:
            return self.cookie_dir
        return tempfile.gettempdir()

    def _ensure_cookie_jar(self, cookie_header: str, revision: int) -> str:
        """把当前 Cookie 头落成 Netscape jar 供 yt-dlp 使用。

        用 Cookie 运行时的 revision 做缓存键：只有真的发生过轮换才重写文件，
        免得每次解析都做一次磁盘写。
        """
        header = (cookie_header or "").strip()
        if not header:
            return ""
        token = (_as_int(revision), len(header))
        if (
            self._jar_path
            and self._jar_token == token
            and os.path.exists(self._jar_path)
        ):
            return self._jar_path
        cookies = parse_cookie_header(header)
        if not cookies:
            return ""
        lines = [
            "# Netscape HTTP Cookie File",
            "# 由 Nova 插件依当前 YouTube 登录态生成，供 yt-dlp 兜底使用。",
        ]
        for name, value in cookies.items():
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
        text = "\n".join(lines) + "\n"
        path = self._write_jar(text)
        if path:
            self._jar_path = path
            self._jar_token = token
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

    def _best_progressive(
        self, formats: List[Dict[str, Any]]
    ) -> Optional[Dict[str, Any]]:
        best: Optional[Tuple[Tuple[int, int, int], Dict[str, Any]]] = None
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
            if best is None or score > best[0]:
                best = (score, fmt)
        return best[1] if best else None

    def _best_video_only(
        self, formats: List[Dict[str, Any]]
    ) -> Optional[Dict[str, Any]]:
        best: Optional[Tuple[Tuple[int, int, int], Dict[str, Any]]] = None
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
            if best is None or score > best[0]:
                best = (score, fmt)
        return best[1] if best else None

    def _best_audio_only(
        self, formats: List[Dict[str, Any]]
    ) -> Optional[Dict[str, Any]]:
        best: Optional[Tuple[Tuple[int, int], Dict[str, Any]]] = None
        for fmt in formats:
            if _has_video(fmt) or not _has_audio(fmt):
                continue
            score = (
                _codec_rank(str(fmt.get("acodec") or ""), _AUDIO_CODEC_RANK),
                _as_int(fmt.get("tbr")),
            )
            if best is None or score > best[0]:
                best = (score, fmt)
        return best[1] if best else None

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
        if self.allow_dash:
            video = self._best_video_only(formats)
            audio = self._best_audio_only(formats)
            if video and audio:
                return YtDlpStream(
                    url=f"dash:{video['url']}||{audio['url']}",
                    kind="dash",
                    height=_as_int(video.get("height")),
                    user_agent=_format_user_agent(video),
                    filesize=_format_size(video) + _format_size(audio),
                    detail=f"{_format_label(video)}+{_format_label(audio)}",
                )
        progressive = self._best_progressive(formats)
        if progressive:
            return YtDlpStream(
                url=str(progressive.get("url") or ""),
                kind="progressive",
                height=_as_int(progressive.get("height")),
                user_agent=_format_user_agent(progressive),
                filesize=_format_size(progressive),
                detail=_format_label(progressive),
            )
        video = self._best_video_only(formats)
        if video:
            return YtDlpStream(
                url=str(video.get("url") or ""),
                kind="video_only",
                height=_as_int(video.get("height")),
                user_agent=_format_user_agent(video),
                filesize=_format_size(video),
                detail=_format_label(video),
            )
        return None
