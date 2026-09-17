"""视频压缩：把超过可发送体积的视频重新编码到能发出去的大小。"""

import asyncio
import json
import math
import os
import re
import time
from dataclasses import dataclass
from typing import Any, List, Optional, Tuple

from ..constants import Config
from ..logger import logger
from ..storage import cleanup_file
from .fileio import run_blocking

# 没有 ffprobe 时从 ffmpeg 的输入信息里兜底抓时长、分辨率与帧率。
_DURATION_PATTERN = re.compile(r"Duration:\s*(\d+):(\d{2}):(\d{2}(?:\.\d+)?)")
_RESOLUTION_PATTERN = re.compile(r",\s*(\d{2,5})x(\d{2,5})\b")
_FPS_PATTERN = re.compile(r"([\d.]+)\s*fps")

# 容器开销与码率控制误差的留白，避免压完刚好卡在上限上方。
_SIZE_SAFETY_RATIO = 0.94
# 音轨码率候选，从高到低取第一个不至于挤占画面的档位。
_AUDIO_KBPS_LADDER = (128, 96, 64)
# 画面码率低于这个值时已经没有观看价值，直接放弃压缩。
_MIN_VIDEO_KBPS = 120
# 码率紧张时先把帧率压到 30，比继续降分辨率更划算。
_FPS_CAP = 30.0
# 码率不足以撑住原始分辨率时，按 (视频码率下限 kbps, 目标高度) 逐档缩放。
_BITRATE_HEIGHT_LADDER = (
    (3500, 1080),
    (1800, 720),
    (900, 540),
    (450, 480),
    (0, 360),
)
# 首轮按目标体积编码，产物仍超限时按实际偏差再修一轮。
_MAX_ATTEMPTS = 2
# 留给收尾的时间，剩余预算不足时不再开新一轮编码。
_MIN_ATTEMPT_SECONDS = 5.0


@dataclass
class VideoProbe:
    """压缩决策需要的视频基本信息。"""

    duration: float = 0.0
    width: int = 0
    height: int = 0
    fps: float = 0.0
    has_audio: bool = False

    @property
    def short_edge(self) -> int:
        """短边像素数。720p/1080p 这类档位说的都是短边，横竖屏都适用。"""
        if self.width > 0 and self.height > 0:
            return min(self.width, self.height)
        return max(self.width, self.height)

    @property
    def is_portrait(self) -> bool:
        """竖屏视频（高大于宽），缩放时要换一个方向限制边长。"""
        return self.width > 0 and self.height > self.width


@dataclass
class TranscodePlan:
    """一轮压缩使用的编码参数。height/fps_cap 为 0 表示不做限制。"""

    video_kbps: int
    audio_kbps: int
    height: int = 0
    fps_cap: float = 0.0
    video_codec: str = "libx264"
    preset: str = "veryfast"
    crf: int = 0
    extra_args: Tuple[str, ...] = ()


@dataclass(frozen=True)
class TranscodeOptions:
    """管理员可调的编码参数；0 表示沿用按目标体积自动规划。"""

    video_codec: str = "libx264"
    preset: str = "veryfast"
    max_height: int = 0
    max_fps: float = 0.0
    video_bitrate_kbps: int = 0
    audio_bitrate_kbps: int = 0
    crf: int = 0
    max_attempts: int = _MAX_ATTEMPTS
    extra_args: Tuple[str, ...] = ()


@dataclass
class TranscodeResult:
    """压缩结果。成功时给出新文件与体积，失败时只有 error 有值。"""

    file_path: Optional[str] = None
    size_mb: Optional[float] = None
    error: Optional[str] = None
    summary: str = ""
    note: str = ""


def _describe_os_error(exc: OSError) -> str:
    """OSError 的 str() 会把路径按 repr 转义，只取系统消息更适合给人看。"""
    return getattr(exc, "strerror", None) or str(exc)


def _parse_float(value: Any) -> float:
    """解析为有限正浮点，失败或非正一律返回 0.0。"""
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return 0.0
    return parsed if math.isfinite(parsed) and parsed > 0 else 0.0


def _parse_fraction(value: Any) -> float:
    """解析 ffprobe 的 30000/1001 式帧率表达。"""
    if value is None:
        return 0.0
    text = str(value).strip()
    if not text or text in ("0/0", "N/A"):
        return 0.0
    if "/" not in text:
        return _parse_float(text)
    numerator, _, denominator = text.partition("/")
    divisor = _parse_float(denominator)
    if divisor <= 0:
        return 0.0
    return _parse_float(numerator) / divisor


async def _terminate_process(process, label: str) -> None:
    """取消或超时时终止并回收子进程。"""
    if process is None:
        return
    try:
        if process.returncode is None:
            process.kill()
    except ProcessLookupError:
        pass
    except Exception as e:
        logger.warning(f"{label} 进程终止失败: {e}")
    try:
        await process.communicate()
    except Exception as e:
        logger.warning(f"{label} 进程回收失败: {e}")


async def _run_capture(
    args: List[str], timeout: float
) -> Tuple[Optional[int], str, str]:
    """执行外部命令并回收输出，超时返回退出码 None。"""
    process = None
    try:
        process = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(
            process.communicate(), timeout=timeout
        )
        return (
            process.returncode,
            stdout.decode("utf-8", errors="ignore") if stdout else "",
            stderr.decode("utf-8", errors="ignore") if stderr else "",
        )
    except asyncio.TimeoutError:
        await _terminate_process(process, args[0])
        return None, "", ""
    except asyncio.CancelledError:
        await _terminate_process(process, args[0])
        raise


async def _probe_with_ffmpeg(file_path: str) -> Optional[VideoProbe]:
    """没有 ffprobe 时用 ffmpeg 打印的输入信息兜底解析。"""
    try:
        _, _, stderr = await _run_capture(
            ["ffmpeg", "-hide_banner", "-nostdin", "-i", file_path],
            timeout=Config.DEFAULT_TIMEOUT,
        )
    except FileNotFoundError:
        logger.warning("ffmpeg 未找到，无法读取视频信息")
        return None
    if not stderr:
        return None

    probe = VideoProbe()
    duration_match = _DURATION_PATTERN.search(stderr)
    if duration_match:
        hours, minutes, seconds = duration_match.groups()
        probe.duration = int(hours) * 3600 + int(minutes) * 60 + float(seconds)
    for line in stderr.splitlines():
        if "Audio:" in line:
            probe.has_audio = True
        if "Video:" not in line or probe.width:
            continue
        resolution = _RESOLUTION_PATTERN.search(line)
        if resolution:
            probe.width = int(resolution.group(1))
            probe.height = int(resolution.group(2))
        fps_match = _FPS_PATTERN.search(line)
        if fps_match:
            probe.fps = _parse_float(fps_match.group(1))
    return probe if probe.duration > 0 else None


async def probe_video(file_path: str) -> Optional[VideoProbe]:
    """读取时长、分辨率、帧率与音轨情况，ffprobe 不可用时退回 ffmpeg。"""
    try:
        returncode, stdout, _ = await _run_capture(
            [
                "ffprobe",
                "-v",
                "error",
                "-print_format",
                "json",
                "-show_format",
                "-show_streams",
                file_path,
            ],
            timeout=Config.DEFAULT_TIMEOUT,
        )
    except FileNotFoundError:
        return await _probe_with_ffmpeg(file_path)
    if returncode != 0 or not stdout:
        return await _probe_with_ffmpeg(file_path)

    try:
        payload = json.loads(stdout)
    except (TypeError, ValueError):
        return await _probe_with_ffmpeg(file_path)
    if not isinstance(payload, dict):
        return await _probe_with_ffmpeg(file_path)

    probe = VideoProbe()
    container = payload.get("format")
    if isinstance(container, dict):
        probe.duration = _parse_float(container.get("duration"))
    for stream in payload.get("streams") or []:
        if not isinstance(stream, dict):
            continue
        codec_type = stream.get("codec_type")
        if codec_type == "audio":
            probe.has_audio = True
            continue
        if codec_type != "video" or probe.width:
            continue
        probe.width = int(_parse_float(stream.get("width")))
        probe.height = int(_parse_float(stream.get("height")))
        probe.fps = _parse_fraction(
            stream.get("avg_frame_rate") or stream.get("r_frame_rate")
        )
        if probe.duration <= 0:
            probe.duration = _parse_float(stream.get("duration"))
    if probe.duration <= 0:
        return await _probe_with_ffmpeg(file_path)
    return probe


def _format_mb(size_bytes: float) -> str:
    """体积文案：小体积多留一位小数，避免出现没有信息量的 0MB。"""
    size_mb = size_bytes / 1024 / 1024
    return f"{size_mb:.1f}MB" if size_mb < 10 else f"{size_mb:.0f}MB"


def _format_duration(seconds: float) -> str:
    """时长文案：秒 / 分钟 / 小时三档，短视频不会被写成 0 分钟。"""
    if seconds < 60:
        return f"{seconds:.0f} 秒"
    if seconds < 3600:
        return f"{seconds / 60:.0f} 分钟"
    return f"{seconds / 3600:.1f} 小时"


def plan_transcode(
    probe: Optional[VideoProbe],
    target_bytes: int,
    *,
    options: Optional[TranscodeOptions] = None,
) -> Tuple[Optional[TranscodePlan], str]:
    """按目标体积推算码率与分辨率，压不出可看画质时返回放弃原因。"""
    if probe is None or probe.duration <= 0:
        return None, "读不出视频时长"
    if target_bytes <= 0:
        return None, "目标体积无效"

    options = options or TranscodeOptions()
    total_kbps = target_bytes * _SIZE_SAFETY_RATIO * 8 / probe.duration / 1000
    audio_kbps = 0
    if probe.has_audio:
        if options.audio_bitrate_kbps > 0:
            audio_kbps = options.audio_bitrate_kbps
        else:
            audio_kbps = _AUDIO_KBPS_LADDER[-1]
            for candidate in _AUDIO_KBPS_LADDER:
                if candidate <= total_kbps * 0.25:
                    audio_kbps = candidate
                    break

    video_kbps = (
        options.video_bitrate_kbps
        if options.video_bitrate_kbps > 0
        else int(total_kbps - audio_kbps)
    )
    if video_kbps < _MIN_VIDEO_KBPS:
        # 说清"至少要多大"，比只说压不动更容易判断该调哪个上限。
        floor_bytes = (
            (_MIN_VIDEO_KBPS + audio_kbps)
            * 1000
            * probe.duration
            / 8
            / _SIZE_SAFETY_RATIO
        )
        return None, (
            f"{_format_duration(probe.duration)}的视频压到 "
            f"{_format_mb(target_bytes)} 后画质无法接受"
            f"（至少需要 {_format_mb(floor_bytes)}）"
        )

    height = _BITRATE_HEIGHT_LADDER[-1][1]
    for floor_kbps, ladder_height in _BITRATE_HEIGHT_LADDER:
        if video_kbps >= floor_kbps:
            height = ladder_height
            break
    if 0 < probe.short_edge <= height:
        height = 0
    if options.max_height > 0 and probe.short_edge > options.max_height:
        if height <= 0 or height > options.max_height:
            height = options.max_height

    fps_cap = _FPS_CAP if video_kbps < 3000 else 0.0
    if options.max_fps > 0:
        fps_cap = min(fps_cap, options.max_fps) if fps_cap > 0 else options.max_fps

    return (
        TranscodePlan(
            video_kbps=video_kbps,
            audio_kbps=audio_kbps,
            height=height,
            fps_cap=fps_cap,
            video_codec=options.video_codec or "libx264",
            preset=options.preset or "veryfast",
            crf=options.crf if 0 < options.crf <= 51 else 0,
            extra_args=tuple(options.extra_args or ()),
        ),
        "",
    )


def _resolve_output_path(input_path: str) -> str:
    """在源文件同目录生成压缩产物路径，直接沿用缓存目录的过期清理。"""
    base, _ = os.path.splitext(input_path)
    candidate = f"{base}_fit.mp4"
    suffix = 1
    while candidate == input_path or os.path.exists(candidate):
        candidate = f"{base}_fit{suffix}.mp4"
        suffix += 1
    return candidate


def _build_ffmpeg_args(
    input_path: str,
    output_path: str,
    plan: TranscodePlan,
    probe: VideoProbe,
) -> List[str]:
    """组装一轮压缩的 ffmpeg 参数。"""
    args = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-nostdin",
        "-loglevel",
        "error",
        "-i",
        input_path,
        "-map",
        "0:v:0",
    ]
    if plan.audio_kbps > 0:
        args += ["-map", "0:a:0"]
    if plan.height > 0:
        # 档位按短边算，竖屏要限制宽度，否则 720p 竖屏会被压成 405x720。
        args += [
            "-vf",
            f"scale={plan.height}:-2" if probe.is_portrait else f"scale=-2:{plan.height}",
        ]
    elif probe.width % 2 or probe.height % 2:
        # libx264 的 yuv420p 要求宽高为偶数，原始尺寸是奇数时补一次对齐。
        args += ["-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2"]
    if plan.fps_cap > 0 and probe.fps > plan.fps_cap:
        args += ["-r", f"{plan.fps_cap:g}"]
    args += ["-c:v", plan.video_codec]
    if plan.preset:
        args += ["-preset", plan.preset]
    if plan.video_codec == "libx264":
        args += ["-profile:v", "high"]
    args += ["-pix_fmt", "yuv420p"]
    if plan.crf > 0:
        args += ["-crf", str(plan.crf)]
    else:
        args += [
            "-b:v",
            f"{plan.video_kbps}k",
            "-maxrate",
            f"{int(plan.video_kbps * 1.45)}k",
            "-bufsize",
            f"{int(plan.video_kbps * 2.5)}k",
        ]
    if plan.audio_kbps > 0:
        args += ["-c:a", "aac", "-b:a", f"{plan.audio_kbps}k", "-ac", "2"]
    else:
        args.append("-an")
    args += [
        "-max_muxing_queue_size",
        "1024",
        "-movflags",
        "+faststart",
    ]
    args.extend(plan.extra_args)
    args.append(output_path)
    return args


def _describe_attempt(attempt: int, plan: TranscodePlan, actual_bytes: int) -> str:
    """把一轮压缩的参数与结果压成一句日志。"""
    scale_text = f"{plan.height}p" if plan.height else "原尺寸"
    audio_text = f"{plan.audio_kbps}kbps音频" if plan.audio_kbps else "无音轨"
    rate_text = f"CRF {plan.crf}" if plan.crf > 0 else f"{plan.video_kbps}kbps"
    return (
        f"第{attempt}轮 {rate_text}/{scale_text}/{audio_text}"
        f" -> {actual_bytes / 1024 / 1024:.1f}MB"
    )


def _describe_note(
    source_bytes: int, result_bytes: int, plan: TranscodePlan, probe: VideoProbe
) -> str:
    """给用户看的一行压缩说明。"""
    note = (
        f"{source_bytes / 1024 / 1024:.1f}MB → "
        f"{result_bytes / 1024 / 1024:.1f}MB"
    )
    if plan.height > 0 and probe.short_edge > 0:
        return f"{note}（{probe.short_edge}p → {plan.height}p）"
    if probe.short_edge > 0:
        return f"{note}（保持 {probe.short_edge}p）"
    return note


async def transcode_video_to_size(
    input_path: str,
    target_bytes: int,
    *,
    timeout_seconds: int = Config.DEFAULT_TRANSCODE_TIMEOUT_SECONDS,
    options: Optional[TranscodeOptions] = None,
) -> TranscodeResult:
    """把视频重编码到 target_bytes 以内，成功时返回新文件路径与体积。"""
    if target_bytes <= 0:
        return TranscodeResult(error="目标体积无效")

    try:
        source_bytes = await run_blocking(os.path.getsize, input_path)
    except OSError as e:
        return TranscodeResult(error=f"读取源视频失败: {_describe_os_error(e)}")

    probe = await probe_video(input_path)
    if probe is None:
        return TranscodeResult(
            error="读不出视频信息（缺少 ffmpeg/ffprobe 或文件损坏）"
        )

    output_path = await run_blocking(_resolve_output_path, input_path)
    temp_output = f"{output_path}.part.mp4"
    timeout_text = f"压缩超时（超过 {timeout_seconds}s）"
    started = time.monotonic()
    budget_bytes = target_bytes
    attempts: List[str] = []
    last_error = "压缩后体积仍然超限"

    options = options or TranscodeOptions()
    custom_rate_control = options.crf > 0 or options.video_bitrate_kbps > 0
    attempt_limit = 1 if custom_rate_control else max(1, min(3, options.max_attempts))

    try:
        for attempt in range(1, attempt_limit + 1):
            plan, reject = plan_transcode(
                probe,
                budget_bytes,
                options=options,
            )
            if plan is None:
                return TranscodeResult(error=reject or "无法规划压缩参数")

            remaining = timeout_seconds - (time.monotonic() - started)
            if remaining <= _MIN_ATTEMPT_SECONDS:
                # 已经压过一轮只是没压够时，说清体积差距比只说超时更有用。
                return TranscodeResult(
                    error=last_error if attempts else timeout_text
                )

            args = _build_ffmpeg_args(input_path, temp_output, plan, probe)
            try:
                returncode, _, stderr = await _run_capture(args, timeout=remaining)
            except FileNotFoundError:
                return TranscodeResult(error="ffmpeg 未找到，无法压缩超限视频")
            if returncode is None:
                return TranscodeResult(error=timeout_text)
            if returncode != 0:
                lines = [line for line in (stderr or "").splitlines() if line.strip()]
                detail = lines[-1][:160] if lines else f"退出码 {returncode}"
                return TranscodeResult(error=f"ffmpeg 压缩失败: {detail}")

            try:
                actual_bytes = await run_blocking(os.path.getsize, temp_output)
            except OSError as e:
                return TranscodeResult(
                    error=f"读取压缩产物失败: {_describe_os_error(e)}"
                )
            attempts.append(_describe_attempt(attempt, plan, actual_bytes))

            if actual_bytes <= target_bytes:
                await run_blocking(os.replace, temp_output, output_path)
                summary = (
                    f"{source_bytes / 1024 / 1024:.1f}MB -> "
                    f"{actual_bytes / 1024 / 1024:.1f}MB，"
                    f"源 {probe.width}x{probe.height}/{probe.duration:.0f}s，"
                    f"耗时 {time.monotonic() - started:.1f}s，"
                ) + " | ".join(attempts)
                return TranscodeResult(
                    file_path=output_path,
                    size_mb=actual_bytes / 1024 / 1024,
                    summary=summary,
                    note=_describe_note(source_bytes, actual_bytes, plan, probe),
                )

            last_error = (
                f"压缩后仍有 {actual_bytes / 1024 / 1024:.1f}MB，"
                f"超过 {target_bytes / 1024 / 1024:.1f}MB"
            )
            budget_bytes = int(budget_bytes * target_bytes / actual_bytes * 0.92)
        return TranscodeResult(error=last_error)
    except asyncio.CancelledError:
        raise
    finally:
        cleanup_file(temp_output)
