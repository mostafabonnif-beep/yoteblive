#!/usr/bin/env python3
import argparse
import hmac
import importlib
import json
import logging
import os
import platform
import queue
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from collections import deque
from dataclasses import asdict, dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qs, urlparse

APP_DIR = Path(__file__).resolve().parent
DATA_DIR = APP_DIR / "data"
CONFIG_PATH = DATA_DIR / "config.json"
DEFAULT_SOURCE = "https://www.youtube.com/channel/UCAeyha98k-qVLZkbnwaTB9w/live"
DEFAULT_RTMP_BASE = "rtmp://a.rtmp.youtube.com/live2"
DEFAULTS = {
    "source_url": DEFAULT_SOURCE,
    "backup_sources": [],
    "rtmp_base": DEFAULT_RTMP_BASE,
    "stream_keys": [],
    "resolution": "1280x720",
    "fps": 30,
    "preset": "veryfast",
    "video_bitrate": "3000k",
    "audio_bitrate": "160k",
    "reconnect_delay": 10,
    "max_reconnect_delay": 60,
    "health_timeout": 45,
    "stall_timeout": 15,
    "max_session_minutes": 120,
    "cookies_from_browser": "",
    "cookiefile": "",
    "proxy": "",
    # ---- طبقة الرسوم (v9) ----
    "logo_path": "",
    "logo_position": "br",
    "logo_width": 0,
    "logo_opacity": 1.0,
    "logo_mode": "off",
    "logo_show": 12,
    "logo_hide": 40,
    "pip_slots": [],
    "break_enabled": True,
    "break_image": "",
    "break_text": "سنعود قريباً",
    "break_audio": "",
    "auto_start": False,
    "notify_webhook": "",
    "panel_token": "",
    "log_level": "INFO",
}


def ensure_module(module_name: str, package_name: str):
    try:
        return importlib.import_module(module_name)
    except ImportError:
        logging.info("تثبيت %s...", package_name)
        subprocess.check_call([sys.executable, "-m", "pip", "install", "--upgrade", package_name])
        return importlib.import_module(module_name)


try:
    yt_dlp = ensure_module("yt_dlp", "yt-dlp")
    imageio_ffmpeg = ensure_module("imageio_ffmpeg", "imageio-ffmpeg")
except Exception as exc:
    raise SystemExit(f"تعذر تجهيز المكتبات المطلوبة: {exc}") from exc


class StopRequested(Exception):
    pass


@dataclass
class RelayConfig:
    source_url: str = DEFAULT_SOURCE
    backup_sources: list[str] = field(default_factory=list)
    rtmp_base: str = DEFAULT_RTMP_BASE
    stream_keys: list[str] = field(default_factory=list)
    resolution: str = "1280x720"
    fps: int = 30
    preset: str = "veryfast"
    video_bitrate: str = "3000k"
    audio_bitrate: str = "160k"
    reconnect_delay: int = 10
    max_reconnect_delay: int = 60
    health_timeout: int = 45
    stall_timeout: int = 15
    max_session_minutes: int = 120
    cookies_from_browser: str = ""
    cookiefile: str = ""
    proxy: str = ""
    logo_path: str = ""
    logo_position: str = "br"
    logo_width: int = 0
    logo_opacity: float = 1.0
    logo_mode: str = "off"
    logo_show: int = 12
    logo_hide: int = 40
    pip_slots: list[dict[str, Any]] = field(default_factory=list)
    break_enabled: bool = True
    break_image: str = ""
    break_text: str = "سنعود قريباً"
    break_audio: str = ""
    auto_start: bool = False
    notify_webhook: str = ""
    panel_token: str = ""
    log_level: str = "INFO"

    def validate(self) -> list[str]:
        """أخطاء الإعدادات القاتلة — تُستخدم عند الحفظ وفي وضع --check."""
        errors: list[str] = []
        if self.source_url:
            scheme = urlparse(self.source_url).scheme.lower()
            if scheme not in {"http", "https"}:
                errors.append("رابط المصدر يجب أن يبدأ بـ http:// أو https://")
        else:
            errors.append("رابط المصدر فارغ")
        for url in self.backup_sources:
            if urlparse(url).scheme.lower() not in {"http", "https"}:
                errors.append(f"مصدر احتياطي غير صالح: {url}")
        if self.rtmp_base:
            scheme = urlparse(self.rtmp_base).scheme.lower()
            if scheme not in {"rtmp", "rtmps", "srt"}:
                errors.append("عنوان RTMP يجب أن يبدأ بـ rtmp:// أو rtmps:// أو srt://")
        else:
            errors.append("عنوان RTMP فارغ")
        if self.notify_webhook and urlparse(self.notify_webhook).scheme.lower() not in {"http", "https"}:
            errors.append("رابط الإشعارات يجب أن يبدأ بـ http:// أو https://")
        return errors

    @classmethod
    def load(cls) -> "RelayConfig":
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        data: dict[str, Any] = {}
        if CONFIG_PATH.exists():
            try:
                loaded = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    data = loaded
            except Exception as exc:
                logging.warning("ملف الإعدادات غير صالح: %s", exc)
        merged = dict(DEFAULTS)
        merged.update({key: value for key, value in data.items() if key in merged})
        if not merged["stream_keys"]:
            env_keys = [item.strip() for item in os.environ.get("STREAM_KEYS", "").split(",") if item.strip()]
            merged["stream_keys"] = env_keys
        return cls.from_dict(merged)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RelayConfig":
        source_url = str(data.get("source_url", DEFAULT_SOURCE)).strip()
        backup_sources = data.get("backup_sources", [])
        if isinstance(backup_sources, str):
            backup_sources = re.split(r"[\n,]+", backup_sources)
        backup_sources = [str(url).strip() for url in backup_sources if str(url).strip() and str(url).strip() != source_url]
        backup_sources = list(dict.fromkeys(backup_sources))
        rtmp_base = str(data.get("rtmp_base", DEFAULT_RTMP_BASE)).strip()
        keys = data.get("stream_keys", [])
        if isinstance(keys, str):
            keys = re.split(r"[\n,]+", keys)
        keys = [str(key).strip() for key in keys if str(key).strip()]
        keys = list(dict.fromkeys(keys))
        resolution = str(data.get("resolution", "1280x720")).lower().replace(" ", "")
        if not re.fullmatch(r"\d{3,5}x\d{3,5}", resolution):
            resolution = "1280x720"
        try:
            fps = max(1, min(120, int(data.get("fps", 30))))
        except (TypeError, ValueError):
            fps = 30
        preset = str(data.get("preset", "veryfast")).strip().lower()
        allowed_presets = {"ultrafast", "superfast", "veryfast", "faster", "fast", "medium", "slow", "slower"}
        if preset not in allowed_presets:
            preset = "veryfast"
        try:
            reconnect_delay = max(3, min(300, int(data.get("reconnect_delay", 10))))
        except (TypeError, ValueError):
            reconnect_delay = 10
        try:
            max_reconnect_delay = max(reconnect_delay, min(900, int(data.get("max_reconnect_delay", 60))))
        except (TypeError, ValueError):
            max_reconnect_delay = max(reconnect_delay, 60)
        try:
            health_timeout = max(15, min(600, int(data.get("health_timeout", 45))))
        except (TypeError, ValueError):
            health_timeout = 45
        try:
            stall_timeout = max(10, min(300, int(data.get("stall_timeout", 15))))
        except (TypeError, ValueError):
            stall_timeout = 15
        try:
            max_session_minutes = max(0, min(720, int(data.get("max_session_minutes", 120))))
        except (TypeError, ValueError):
            max_session_minutes = 120
        return cls(
            source_url=source_url,
            backup_sources=backup_sources,
            rtmp_base=rtmp_base,
            stream_keys=keys,
            resolution=resolution,
            fps=fps,
            preset=preset,
            video_bitrate=str(data.get("video_bitrate", "3000k")).strip() or "3000k",
            audio_bitrate=str(data.get("audio_bitrate", "160k")).strip() or "160k",
            reconnect_delay=reconnect_delay,
            max_reconnect_delay=max_reconnect_delay,
            health_timeout=health_timeout,
            stall_timeout=stall_timeout,
            max_session_minutes=max_session_minutes,
            cookies_from_browser=str(data.get("cookies_from_browser", "")).strip(),
            cookiefile=str(data.get("cookiefile", "")).strip(),
            proxy=parse_proxy(data.get("proxy", "")),
            logo_path=str(data.get("logo_path", "")).strip(),
            logo_position=sanitize_position(data.get("logo_position", "br"), "br"),
            logo_width=max(0, min(2000, int(number(data.get("logo_width", 0))))),
            logo_opacity=max(0.0, min(1.0, float(number(data.get("logo_opacity", 1.0), 1.0)))),
            logo_mode=sanitize_mode(data.get("logo_mode", "off")),
            logo_show=max(1, min(3600, int(number(data.get("logo_show", 12), 12)))),
            logo_hide=max(1, min(3600, int(number(data.get("logo_hide", 40), 40)))),
            pip_slots=sanitize_pip_slots(data.get("pip_slots", [])),
            break_enabled=bool(data.get("break_enabled", True)),
            break_image=str(data.get("break_image", "")).strip(),
            break_text=str(data.get("break_text", "سنعود قريباً")).strip() or "سنعود قريباً",
            break_audio=str(data.get("break_audio", "")).strip(),
            auto_start=bool(data.get("auto_start", False)),
            notify_webhook=str(data.get("notify_webhook", "")).strip(),
            panel_token=str(data.get("panel_token", "")).strip(),
            log_level=str(data.get("log_level", "INFO")).strip().upper() if str(data.get("log_level", "INFO")).strip().upper() in {"DEBUG", "INFO", "WARNING", "ERROR"} else "INFO",
        )

    def save(self) -> None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        temp_path = CONFIG_PATH.with_suffix(".tmp")
        temp_path.write_text(json.dumps(asdict(self), ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temp_path, CONFIG_PATH)
        try:
            os.chmod(CONFIG_PATH, 0o600)
        except OSError:
            pass


@dataclass
class SourceSelection:
    mode: str
    video_url: str
    audio_url: Optional[str] = None
    video_headers: dict[str, str] = field(default_factory=dict)
    audio_headers: dict[str, str] = field(default_factory=dict)
    video_codec: str = ""
    audio_codec: str = ""
    title: str = ""
    is_live: bool = False
    source_url: str = ""


@dataclass
class WorkerStatus:
    name: str
    output_url: str
    state: str = "stopped"
    message: str = ""
    attempts: int = 0
    consecutive_failures: int = 0
    pid: Optional[int] = None
    started_at: Optional[float] = None
    first_frame_at: Optional[float] = None
    last_exit_code: Optional[int] = None
    last_source: str = ""
    source_url: str = ""
    last_output_at: Optional[float] = None
    last_advance_at: Optional[float] = None
    stats_fps: str = ""
    stats_bitrate: str = ""
    stats_speed: str = ""
    stats_frame: int = 0
    stats_out_time: str = ""
    http_403_count: int = 0
    fallback_running: bool = False


def proxy_env(config: "RelayConfig") -> Optional[dict[str, str]]:
    """بيئة فرعية لـ ffmpeg عند ضبط بروكسي — تؤثر على مداخل HTTP/HTTPS فقط
    (مخرج RTMP اتصال TCP خام لا يتأثر بمتغيرات البروكسي)."""
    if not config.proxy:
        return None
    env = dict(os.environ)
    for key in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY"):
        env[key] = config.proxy
    env.setdefault("no_proxy", "localhost,127.0.0.1")
    env.setdefault("NO_PROXY", "localhost,127.0.0.1")
    return env


class MemoryLogHandler(logging.Handler):
    def __init__(self, limit: int = 600):
        super().__init__()
        self.lines: deque[str] = deque(maxlen=limit)
        self.lock = threading.RLock()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = self.format(record)
            with self.lock:
                self.lines.append(message)
        except Exception:
            pass

    def snapshot(self, limit: int = 150) -> list[str]:
        with self.lock:
            return list(self.lines)[-limit:]


LOG_BUFFER = MemoryLogHandler()
LOGGER = logging.getLogger("youtube-live-relay")
LOGGER.setLevel(logging.INFO)
LOGGER.propagate = False
if not LOGGER.handlers:
    log_formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(log_formatter)
    LOGGER.addHandler(stream_handler)
    LOG_BUFFER.setFormatter(log_formatter)
    LOGGER.addHandler(LOG_BUFFER)
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(DATA_DIR / "relay.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8")
        file_handler.setFormatter(log_formatter)
        LOGGER.addHandler(file_handler)
    except OSError:
        pass


FFMPEG: Optional[str] = None
PANEL_TOKEN: str = os.environ.get("PANEL_TOKEN", "").strip()


def resolve_ffmpeg() -> str:
    configured = os.environ.get("FFMPEG", "").strip()
    candidates: list[Optional[str]] = []
    if configured:
        candidates.extend([configured, shutil.which(configured)])
        if os.path.isdir(configured):
            candidates.extend([os.path.join(configured, "ffmpeg"), os.path.join(configured, "ffmpeg.exe")])
    candidates.extend([shutil.which("ffmpeg"), shutil.which("ffmpeg.exe")])
    try:
        bundled = imageio_ffmpeg.get_ffmpeg_exe()
        candidates.append(bundled)
    except Exception:
        pass
    if platform.system() == "Windows":
        candidates.extend([r"C:\ffmpeg\bin\ffmpeg.exe", r"C:\Program Files\ffmpeg\bin\ffmpeg.exe"])
    for candidate in candidates:
        if candidate and os.path.isfile(candidate):
            return os.path.abspath(candidate)
    raise RuntimeError("لم أجد ffmpeg. ثبّت ffmpeg أو اضبط المتغير FFMPEG على مساره")


def parse_browser_cookie_source(raw: str) -> Optional[tuple[str, ...]]:
    value = raw.strip()
    if not value:
        return None
    pieces = tuple(part.strip() for part in value.split(":") if part.strip())
    return pieces or None


def parse_proxy(raw: Any) -> str:
    """يقبل بروكسي HTTP/HTTPS فقط (مع أو بدون بيانات اعتماد) ويتجاهل البقية."""
    value = str(raw or "").strip()
    if not value:
        return ""
    parsed = urlparse(value)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        return ""
    return value


def mask_proxy(proxy: str) -> str:
    """يخفي بيانات اعتماد البروكسي عند عرضه في الواجهة."""
    if not proxy:
        return ""
    parsed = urlparse(proxy)
    if not parsed.username:
        return proxy
    host = parsed.hostname or ""
    if parsed.port:
        host += f":{parsed.port}"
    return f"{parsed.scheme}://•••:•••@{host}"


def build_ydl_options(config: RelayConfig) -> dict[str, Any]:
    opts: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": False,
        "socket_timeout": 25,
        "retries": 5,
        "fragment_retries": 5,
        "skip_download": True,
        "live_from_start": False,
        "extract_flat": False,
        "geo_bypass": True,
    }
    browser = parse_browser_cookie_source(config.cookies_from_browser)
    if browser:
        opts["cookiesfrombrowser"] = browser
    if config.cookiefile:
        opts["cookiefile"] = config.cookiefile
    if config.proxy:
        opts["proxy"] = config.proxy
    return opts


def is_live_info(info: dict[str, Any]) -> bool:
    status = str(info.get("live_status") or "").lower()
    return bool(info.get("is_live")) or status in {"is_live", "post_live"}


def extract_live_info(config: RelayConfig, source_url: Optional[str] = None) -> Optional[dict[str, Any]]:
    target_url = (source_url or config.source_url).strip()
    if not target_url:
        return None
    try:
        with yt_dlp.YoutubeDL(build_ydl_options(config)) as ydl:
            info = ydl.extract_info(target_url, download=False)
            if not info:
                return None
            if info.get("formats") and is_live_info(info):
                return info
            entries = [entry for entry in (info.get("entries") or []) if entry]
            live_entry = next((entry for entry in entries if is_live_info(entry)), None)
            if live_entry is None and len(entries) == 1:
                live_entry = entries[0]
            if not live_entry:
                return None
            candidate = live_entry.get("webpage_url") or live_entry.get("original_url") or live_entry.get("url")
            if not candidate:
                return None
            resolved = ydl.extract_info(candidate, download=False)
            return resolved if resolved and resolved.get("formats") else None
    except Exception as exc:
        LOGGER.warning("yt-dlp (%s): %s", target_url, exc)
        return None


def number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value or default)
    except (TypeError, ValueError):
        return default


def format_protocol_score(fmt: dict[str, Any]) -> float:
    protocol = str(fmt.get("protocol") or "").lower()
    if "m3u8" in protocol:
        return 1000
    if protocol.startswith("http"):
        return 450
    return 100


def score_video(fmt: dict[str, Any], target_height: int) -> float:
    height = number(fmt.get("height"))
    target_bonus = 220 if height <= target_height + 180 else -max(0, height - target_height) * 0.25
    return format_protocol_score(fmt) + target_bonus + height + number(fmt.get("fps")) * 2 + number(fmt.get("tbr")) / 10


def score_audio(fmt: dict[str, Any]) -> float:
    return format_protocol_score(fmt) + number(fmt.get("abr")) * 2 + number(fmt.get("tbr")) / 10


def format_kinds(fmt: dict[str, Any]) -> tuple[bool, bool]:
    """يكتشف إن كانت الصيغة تحمل فيديو و/أو صوتاً فعلياً.

    ملاحظة جوهرية: في بث يوتيوب المباشر، yt-dlp يعرض صيغ الصوت المنفصلة
    (itag 233/234 مثلاً) بقيمة acodec=None بدلاً من "none"، كما يعرض صيغ
    الفيديو المنفصلة بقيمة acodec="none". الاعتماد على الكوديك وحده كان
    يتجاهل مسار الصوت كلياً ويحوّل البث إلى وضع "فيديو بلا صوت". لذلك نعتمد
    على video_ext/audio_ext كدليل احتياطي عندما يكون الكوديك غير مذكور.
    """
    vcodec = str(fmt.get("vcodec") or "none").strip().lower()
    acodec = str(fmt.get("acodec") or "none").strip().lower()
    video_ext = str(fmt.get("video_ext") or "none").strip().lower()
    audio_ext = str(fmt.get("audio_ext") or "none").strip().lower()
    has_video = vcodec != "none"
    has_audio = acodec != "none"
    if audio_ext not in {"none", ""} and not has_audio:
        # مسار صوت حقيقي (أو تنسيق مدمج) دون كوديك مذكور
        has_audio = True
    if video_ext not in {"none", ""} and not has_video:
        has_video = True
    return has_video, has_audio


def is_muxed(fmt: dict[str, Any]) -> bool:
    has_video, has_audio = format_kinds(fmt)
    return has_video and has_audio


def source_candidates(config: RelayConfig) -> list[str]:
    return list(dict.fromkeys([config.source_url, *config.backup_sources]))


def pick_best_video(videos: list[dict[str, Any]], target_height: int) -> dict[str, Any]:
    """يختار مصدر الفيديو الأنسب للترميز.

    التفضيل: أعلى دقة ≤ الدقة المستهدفة (لتخفيف حمل المعالجة بدل سحب
    1080p وتحويله إلى 720p — وهو سبب شائع لتأخر الترميز عن الزمن الحقيقي
    وبالتالي رسالة «البيانات غير كافية» في يوتيوب). إذا لم تتوفر دقة
    ≤ الهدف، نأخذ الأدنى فوقه (الأقرب والأخف على المعالج).
    """
    below = [fmt for fmt in videos if number(fmt.get("height")) <= target_height]
    if below:
        return max(below, key=lambda fmt: score_video(fmt, target_height))
    return min(videos, key=lambda fmt: (number(fmt.get("height")), -score_video(fmt, target_height)))


def choose_source(formats: list[dict[str, Any]], info: dict[str, Any], config: RelayConfig, allow_muxed: bool = True) -> Optional[SourceSelection]:
    """يحول قائمة الصيغ إلى اختيار نهائي (نقي — قابل للاختبار دون شبكة).

    ترتيب التفضيل:
    1) تنسيق مدمج واحد يحمل فيديو+صوتاً حقيقيين (مزامنة مثالية) — إن سمح allow_muxed.
    2) مسارا فيديو وصوت منفصلان (حالة يوتيوب المباشر المعتادة) — صوت مضمون.
    3) فيديو فقط → صوت صامت يُضاف (تحذير واضح) — ملاذ أخير فقط.
    """
    try:
        target_height = int(config.resolution.split("x", 1)[1])
    except (ValueError, IndexError):
        target_height = 720
    muxed = [fmt for fmt in formats if fmt.get("url") and not fmt.get("has_drm") and is_muxed(fmt)]
    if muxed and allow_muxed:
        best = max(muxed, key=lambda fmt: score_video(fmt, target_height) + score_audio(fmt))
        return SourceSelection(
            mode="muxed",
            video_url=str(best["url"]),
            video_headers=dict(best.get("http_headers") or {}),
            video_codec=str(best.get("vcodec") or ""),
            audio_codec=str(best.get("acodec") or ""),
            title=str(info.get("title") or ""),
            is_live=is_live_info(info),
            source_url=str(info.get("original_url") or info.get("webpage_url") or ""),
        )
    videos = [fmt for fmt in formats if fmt.get("url") and not fmt.get("has_drm") and not format_kinds(fmt)[1] and format_kinds(fmt)[0]]
    audios = [fmt for fmt in formats if fmt.get("url") and not fmt.get("has_drm") and format_kinds(fmt)[1] and not format_kinds(fmt)[0]]
    if not videos and not audios and muxed:
        # مدمج موجود لكنه معطّل مؤقتاً بعد فشل متكرر — نستخدمه رغماً عنا
        best = max(muxed, key=lambda fmt: score_video(fmt, target_height) + score_audio(fmt))
        return SourceSelection(
            mode="muxed",
            video_url=str(best["url"]),
            video_headers=dict(best.get("http_headers") or {}),
            video_codec=str(best.get("vcodec") or ""),
            audio_codec=str(best.get("acodec") or ""),
            title=str(info.get("title") or ""),
            is_live=is_live_info(info),
            source_url=str(info.get("original_url") or info.get("webpage_url") or ""),
        )
    if videos:
        best_video = pick_best_video(videos, target_height)
        if audios:
            # عند تساوي النقاط (كأن تكون abr/tbr غائبة) نفضل الصيغة الأعلى جودةً إن وُسمت
            def _audio_key(fmt: dict[str, Any]) -> tuple[float, int]:
                note = str(fmt.get("format_note") or "").lower().replace(",", " ").split()
                return score_audio(fmt), 1 if "high" in note else 0
            best_audio = max(audios, key=_audio_key)
        else:
            best_audio = None
        if best_audio:
            return SourceSelection(
                mode="split",
                video_url=str(best_video["url"]),
                audio_url=str(best_audio["url"]),
                video_headers=dict(best_video.get("http_headers") or {}),
                audio_headers=dict(best_audio.get("http_headers") or {}) if best_audio else {},
                video_codec=str(best_video.get("vcodec") or ""),
                audio_codec=str(best_audio.get("acodec") or ""),
                title=str(info.get("title") or ""),
                is_live=is_live_info(info),
                source_url=str(info.get("original_url") or info.get("webpage_url") or ""),
            )
        LOGGER.warning("المصدر لا يوفر مسار صوت منفصل — سيُضاف صوت صامت (قد لا يسمع المشاهدون شيئاً حتى يتوفر مصدر بصوت)")
        return SourceSelection(
            mode="video-only",
            video_url=str(best_video["url"]),
            video_headers=dict(best_video.get("http_headers") or {}),
            video_codec=str(best_video.get("vcodec") or ""),
            title=str(info.get("title") or ""),
            is_live=is_live_info(info),
            source_url=str(info.get("original_url") or info.get("webpage_url") or ""),
        )
    return None


def select_source(config: RelayConfig, allow_muxed: bool = True) -> Optional[SourceSelection]:
    for candidate_url in source_candidates(config):
        info = extract_live_info(config, candidate_url)
        if not info:
            continue
        formats = [fmt for fmt in (info.get("formats") or []) if fmt.get("url") and not fmt.get("has_drm")]
        if not formats and info.get("url"):
            formats = [info]
        if not formats:
            continue
        selection = choose_source(formats, info, config, allow_muxed=allow_muxed)
        if selection:
            selection.source_url = candidate_url
            return selection
    return None


class SourceResolver:
    """كاش قصير العمر لحل رابط المصدر حتى لا يتكرر استدعاء yt-dlp لكل وجهة."""

    def __init__(self, ttl: int = 90):
        self.ttl = ttl
        self.lock = threading.Lock()
        self.cached: Optional[tuple[float, SourceSelection, bool]] = None

    def resolve(self, config: RelayConfig, allow_muxed: bool = True) -> Optional[SourceSelection]:
        with self.lock:
            if self.cached and time.time() - self.cached[0] < self.ttl and self.cached[2] == allow_muxed:
                return self.cached[1]
            if allow_muxed:
                source = select_source(config)
            else:
                source = select_source(config, allow_muxed=False)
            if source:
                self.cached = (time.time(), source, allow_muxed)
            return source

    def invalidate(self) -> None:
        with self.lock:
            self.cached = None


class Notifier:
    """إرسال إشعارات غير حاجبة إلى Webhook (Discord / Slack / ntfy / أي نقطة JSON)."""

    DEDUP_SECONDS = 600

    def __init__(self):
        self._queue: queue.Queue[tuple[str, str, str]] = queue.Queue()
        self._recent: dict[tuple[str, str], float] = {}
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._worker, name="notifier", daemon=True)
        self._thread.start()

    def notify(self, event: str, message: str, url: str) -> None:
        if not url:
            return
        key = (event, message)
        with self._lock:
            last = self._recent.get(key, 0.0)
            if time.time() - last < self.DEDUP_SECONDS:
                return
            self._recent[key] = time.time()
        self._queue.put((event, message, url))

    def _worker(self) -> None:
        while True:
            event, message, url = self._queue.get()
            payload = json.dumps({"text": message, "content": message, "event": event, "app": "youtube-live-relay"}).encode("utf-8")
            for attempt in (1, 2):
                try:
                    request = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json", "User-Agent": "live-relay/3.0"})
                    with urllib.request.urlopen(request, timeout=8) as response:
                        response.read()
                    break
                except Exception as exc:
                    if attempt == 2:
                        LOGGER.warning("تعذر إرسال الإشعار: %s", exc)
                    else:
                        time.sleep(2)


NOTIFIER = Notifier()


POSITIONS = {"tl", "tr", "bl", "br", "center"}
MODES = {"off", "always", "periodic"}


def sanitize_position(raw: Any, default: str = "br") -> str:
    value = str(raw or "").strip().lower()
    return value if value in POSITIONS else default


def sanitize_mode(raw: Any, default: str = "off") -> str:
    value = str(raw or "").strip().lower()
    return value if value in MODES else default


def sanitize_pip_slots(raw: Any) -> list[dict[str, Any]]:
    """يعيد قائمة منافذ PiP نظيفة: الاسم/المسار/الموضع/العرض/الوضع/المدد."""
    slots: list[dict[str, Any]] = []
    if not isinstance(raw, list):
        return slots
    for item in raw[:3]:
        if not isinstance(item, dict):
            continue
        path_value = str(item.get("path") or "").strip()
        if not path_value:
            continue
        slots.append({
            "name": str(item.get("name") or "عرض").strip()[:30] or "عرض",
            "path": path_value,
            "position": sanitize_position(item.get("position"), "br"),
            "width": max(120, min(1600, int(number(item.get("width", 480), 480)))),
            "mode": sanitize_mode(item.get("mode"), "off"),
            "show": max(1, min(3600, int(number(item.get("show", 20), 20)))),
            "hide": max(1, min(3600, int(number(item.get("hide", 30), 30)))),
        })
    return slots


def headers_value(headers: dict[str, str]) -> str:
    allowed = {"user-agent", "referer", "origin", "cookie", "accept-language"}
    result = []
    for key, value in headers.items():
        if key.lower() in allowed and value:
            result.append(f"{key}: {value}\r\n")
    return "".join(result)


def input_options(url: str, headers: dict[str, str]) -> list[str]:
    args = [
        "-thread_queue_size",
        "2048",
        # مهلات قصيرة: مقطع ميت لا يجب أن يجمّد الإرسال 20+ ثانية
        "-rw_timeout",
        "10000000",
        "-timeout",
        "10000000",
        "-reconnect",
        "1",
        "-reconnect_streamed",
        "1",
        "-reconnect_delay_max",
        "5",
    ]
    if ".m3u8" in url or "hls_playlist" in url:
        # ابدأ قرب حافة البث المباشر لتفادي قراءة مقاطع قديمة أو الانتظار الطويل
        args.extend(["-live_start_index", "-3"])
    header_text = headers_value(headers)
    if header_text:
        args.extend(["-headers", header_text])
    args.extend(["-i", url])
    return args


# ============================= طبقة الرسوم v9 =============================
MEDIA_DIR = APP_DIR / "media"
FONT_DIR = MEDIA_DIR / "fonts"
ARABIC_FONT = FONT_DIR / "NotoSansArabic-Bold.ttf"

POS_XY = {
    "tl": ("10", "10"),
    "tr": ("W-w-10", "10"),
    "bl": ("10", "H-h-10"),
    "br": ("W-w-10", "H-h-10"),
    "center": ("(W-w)/2", "(H-h)/2"),
}


def overlay_enable(mode: str, show: int, hide: int) -> str:
    """تعليمة enable للطبقة: off/always/periodic (تظهر show ثانية كل show+hide)."""
    if mode == "periodic":
        total = max(2, show + hide)
        return f"enable='lt(mod(t,{total}),{show})'"
    return ""


def file_input_args(path: str, is_video: bool, loop: bool = True) -> list[str]:
    args: list[str] = []
    if not is_video:
        args.extend(["-loop", "1", "-framerate", "30"])
    elif loop:
        args.extend(["-stream_loop", "-1", "-re"])
    args.extend(["-i", path])
    return args


def graphics_pipeline(config: RelayConfig, source: SourceSelection, width: int, height: int, fps: int) -> Optional[tuple[list[str], str]]:
    """يبني مدخلات إضافية + filter_complex لطبقة الشعار ومنافذ PiP.

    يعيد None إذا لم تكن هناك أي طبقة مفعلة (يبقى المسار القديم البسيط).
    المدخلات: 0 = الفيديو (و1 = الصوت في split/video-only) ثم تُلحق مدخلات الرسوم.
    """
    extra: list[str] = []
    parts: list[str] = []
    # مدخلات الأساس: 0=فيديو (+1=صوت في split/video-only)؛ الرسوم تُلحق بعدها
    slot_idx = 2 if source.mode in {"split", "video-only"} else 1
    label = "bg"
    base_first = True

    logo_on = config.logo_path and config.logo_mode != "off"
    pips = [p for p in config.pip_slots if p.get("path") and p.get("mode") != "off"]
    if not logo_on and not pips:
        return None

    # الأساس: نفس التحجيم القديم
    scale_base = (f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
                  f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black,fps={fps},format=yuv420p")
    if logo_on:
        idx = slot_idx
        slot_idx += 1
        extra.extend(file_input_args(config.logo_path, is_video=False))
        lw = config.logo_width or 220
        pos_x, pos_y = POS_XY.get(config.logo_position, POS_XY["br"])
        parts.append(f"[{idx}:v]scale='min({lw}\\,iw)':-2,format=rgba,fps={fps}[logo]")
        enable = overlay_enable(config.logo_mode, config.logo_show, config.logo_hide)
        if enable:
            parts.append(f"[{label}][logo]overlay={pos_x}:{pos_y}:{enable}[l1]")
            label = "l1"
        else:
            parts.append(f"[{label}][logo]overlay={pos_x}:{pos_y}[l1]")
            label = "l1"
        base_first = False

    for pip in pips:
        idx = slot_idx
        slot_idx += 1
        is_video = pip["path"].lower().endswith((".mp4", ".mkv", ".webm", ".mov", ".ts", ".avi"))
        extra.extend(file_input_args(pip["path"], is_video=is_video))
        pv = f"[{idx}:v]scale='min({pip['width']}\\,iw)':-2,format=rgba,fps={fps}[p{idx}]"
        parts.append(pv)
        pos_x, pos_y = POS_XY.get(pip.get("position"), POS_XY["br"])
        enable = overlay_enable(pip.get("mode", "off"), int(pip.get("show", 20)), int(pip.get("hide", 30)))
        name = label if not base_first else label
        out_label = f"p{idx}"
        if enable:
            parts.append(f"[{name}][p{idx}]overlay={pos_x}:{pos_y}:{enable}[{out_label}]")
        else:
            parts.append(f"[{name}][p{idx}]overlay={pos_x}:{pos_y}[{out_label}]")
        label = out_label
        base_first = False

    graph_parts = [f"[0:v]{scale_base}[bg]"] + parts
    graph_parts.append(f"[{label}]format=yuv420p[vout]")
    return extra, ";".join(graph_parts)


def build_break_command(config: RelayConfig, output_url: str) -> list[str]:
    """شاشة «سنعود قريباً» — محتوى محلي بالكامل (خلفية + صورة + نص عربي + شعار + صوت).

    تعمل حتى لو كان IP الخادم محجوباً لدى يوتيوب لأنها لا تجلب شيئاً من الإنترنت.
    ترتيب المدخلات: 0=خلفية ملونة، 1=صورة اختيارية، 2=شعار اختياري، ثم الصوت أخيراً.
    """
    if not FFMPEG:
        raise RuntimeError("ffmpeg غير جاهز")
    width, height = (int(part) for part in config.resolution.split("x", 1))
    fps = config.fps
    cmd: list[str] = [FFMPEG, "-hide_banner", "-loglevel", "warning", "-nostdin", "-nostats"]
    cmd.extend(["-f", "lavfi", "-i", f"color=c=0x0c1322:s={width}x{height}:r={fps}"])
    next_input = 1
    graph: list[str] = [f"[0:v]format=yuv420p[base]"]
    cur = "base"

    img = config.break_image
    if img and Path(img).exists():
        cmd.extend(["-loop", "1", "-framerate", str(fps), "-i", img])
        graph.append(f"[{next_input}:v]scale='min({width}\\,iw)':-2,format=rgb24[bgi]")
        graph.append(f"[{cur}][bgi]overlay=(W-w)/2:(H-h)/2-170[b1]")
        cur = "b1"
        next_input += 1

    if Path(ARABIC_FONT).exists():
        text = config.break_text or "سنعود قريباً"
        graph.append(
            f"[{cur}]drawtext=fontfile='{ARABIC_FONT}':text='{text}':"
            f"fontcolor=white@0.95:fontsize=52:x=(w-text_w)/2:y=(h-text_h)/2+60:"
            f"shadowcolor=black@0.6:shadowx=2:shadowy=2[txt]"
        )
        cur = "txt"
    else:
        LOGGER.warning("الخط العربي غير موجود في media/fonts — شاشة الاستراحة بلا نص")

    if config.logo_path and Path(config.logo_path).exists():
        cmd.extend(["-loop", "1", "-framerate", str(fps), "-i", config.logo_path])
        graph.append(f"[{next_input}:v]scale='min(150\\,iw)':-2,format=rgba[lgb]")
        graph.append(f"[{cur}][lgb]overlay=W-w-24:H-h-24[l2]")
        cur = "l2"
        next_input += 1

    graph.append(f"[{cur}]format=yuv420p[vout]")

    if config.break_audio and Path(config.break_audio).exists():
        cmd.extend(["-stream_loop", "-1", "-i", config.break_audio])
    else:
        cmd.extend(["-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100"])
    # كل خيارات الخرج بعد جميع المدخلات (ترتيب ffmpeg الصحيح)
    cmd.extend(["-filter_complex", ";".join(graph), "-map", "[vout]", "-map", f"{next_input}:a:0"])
    cmd.extend([
        "-c:v", "libx264", "-preset", config.preset, "-tune", "zerolatency",
        "-profile:v", "main", "-b:v", config.video_bitrate,
        "-maxrate", config.video_bitrate,
        "-bufsize", str(max(2000, int(number(re.sub(r"[^0-9.]", "", config.video_bitrate), 3000) * 2))) + "k",
        "-pix_fmt", "yuv420p", "-r", str(fps),
        "-g", str(fps * 2), "-keyint_min", str(fps * 2), "-sc_threshold", "0",
        "-c:a", "aac", "-b:a", config.audio_bitrate, "-ar", "44100", "-ac", "2",
        "-max_muxing_queue_size", "4096", "-flvflags", "no_duration_filesize",
        "-f", "flv", output_url,
    ])
    return cmd


def build_ffmpeg_command(source: SourceSelection, output_url: str, config: RelayConfig) -> list[str]:
    if not FFMPEG:
        raise RuntimeError("ffmpeg غير جاهز")
    width, height = (int(part) for part in config.resolution.split("x", 1))
    fps = config.fps
    scale = f"scale={width}:{height}:force_original_aspect_ratio=decrease,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black,fps={fps}"
    command: list[str] = [FFMPEG, "-hide_banner", "-loglevel", "warning", "-nostdin", "-nostats", "-progress", "pipe:1", "-stats_period", "2"]
    if source.mode == "split":
        command.extend(input_options(source.video_url, source.video_headers))
        command.extend(input_options(source.audio_url or "", source.audio_headers))
        video_index, audio_index = "0:v:0", "1:a:0"
    else:
        command.extend(input_options(source.video_url, source.video_headers))
        video_index = "0:v:0"
        if source.mode == "video-only":
            command.extend(["-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100"])
            audio_index = "1:a:0"
        else:
            # تنسيق مدمج: الصوت إلزامي إن كان المصدر يصرح بوجوده — لا صمت صامت.
            # إذا لم يوجد الصوت فعلاً في الحاوية، يخرج ffmpeg بخطأ وتُعاد المحاولة
            # تلقائياً بمسار صوت منفصل بدل إرسال بث بلا صوت بصمت.
            audio_index = "0:a:0"
    # ---- طبقة الرسوم v9: شعار + منافذ PiP (عند تفعيل أي منها) ----
    gfx = graphics_pipeline(config, source, width, height, fps)
    if gfx is not None:
        extra_inputs, filter_graph = gfx
        command.extend(extra_inputs)
        command.extend(["-filter_complex", filter_graph, "-map", "[vout]"])
    else:
        command.extend(["-map", video_index, "-vf", scale])
    command.extend(
        [
            "-map",
            audio_index,
            "-c:v",
            "libx264",
            "-preset",
            config.preset,
            "-tune",
            "zerolatency",
            "-profile:v",
            "main",
            "-b:v",
            config.video_bitrate,
            "-maxrate",
            config.video_bitrate,
            "-bufsize",
            str(max(2000, int(number(re.sub(r"[^0-9.]", "", config.video_bitrate), 3000) * 2))) + "k",
            "-pix_fmt",
            "yuv420p",
            "-r",
            str(fps),
            "-g",
            str(fps * 2),
            "-keyint_min",
            str(fps * 2),
            "-sc_threshold",
            "0",
            "-c:a",
            "aac",
            "-b:a",
            config.audio_bitrate,
            "-ar",
            "44100",
            "-ac",
            "2",
            "-max_muxing_queue_size",
            "4096",
            "-flvflags",
            "no_duration_filesize",
            "-f",
            "flv",
            output_url,
        ]
    )
    return command


def terminate_process(proc: subprocess.Popen[str]) -> None:
    if proc.poll() is not None:
        return
    try:
        if os.name == "nt":
            proc.terminate()
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        proc.wait(timeout=5)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


class RelayWorker(threading.Thread):
    def __init__(self, manager: "RelayManager", name: str, output_url: str, config: RelayConfig):
        super().__init__(name=name, daemon=True)
        self.manager = manager
        self.name_value = name
        self.output_url = output_url
        self.config = config
        self.stop_event = threading.Event()
        self.status = WorkerStatus(name=name, output_url=output_url)
        self._status_lock = threading.RLock()
        self._last_frame = -1
        self._last_out_us = -1
        self.break_proc: Optional[subprocess.Popen[str]] = None
        self._break_lock = threading.Lock()

    def update(self, **changes: Any) -> None:
        with self._status_lock:
            for key, value in changes.items():
                setattr(self.status, key, value)

    def snapshot(self) -> dict[str, Any]:
        with self._status_lock:
            data = asdict(self.status)
        if data["started_at"]:
            data["uptime_seconds"] = max(0, int(time.time() - data["started_at"]))
        else:
            data["uptime_seconds"] = 0
        data["output_url"] = mask_output_url(data["output_url"])
        return data

    def wait_before_retry(self, delay: int) -> None:
        end = time.monotonic() + delay
        while not self.stop_event.is_set() and time.monotonic() < end:
            self.stop_event.wait(timeout=0.5)

    # -------------------- البث البديل (شاشة «سنعود قريباً») --------------------
    def _break_alive(self) -> bool:
        return self.break_proc is not None and self.break_proc.poll() is None

    def _ensure_break(self) -> None:
        """يضمن أن شاشة الاستراحة تبث للوجهة عندما لا يوجد بث حي (انقطاع/انتظار/حظر)."""
        if not self.config.break_enabled:
            return
        if self.stop_event.is_set() or self._break_alive():
            return
        try:
            command = build_break_command(self.config, self.output_url)
        except Exception as exc:
            LOGGER.warning("[%s] تعذر بناء شاشة الاستراحة: %s", self.name_value, exc)
            return
        try:
            creation_kwargs: dict[str, Any] = {
                "stdin": subprocess.DEVNULL,
                "stdout": subprocess.PIPE,
                "stderr": subprocess.STDOUT,
                "text": True,
                "bufsize": 1,
            }
            if os.name != "nt":
                creation_kwargs["start_new_session"] = True
            env = proxy_env(self.config)
            if env is not None:
                creation_kwargs["env"] = env
            proc = subprocess.Popen(command, **creation_kwargs)
        except OSError as exc:
            LOGGER.warning("[%s] تعذر تشغيل شاشة الاستراحة: %s", self.name_value, exc)
            return
        with self._break_lock:
            self.break_proc = proc
        self.update(fallback_running=True)
        LOGGER.warning("[%s] ▶ البث البديل يعمل الآن (شاشة الاستراحة) حتى يعود المصدر الحي.", self.name_value)
        threading.Thread(target=self._drain_break_output, args=(proc,), daemon=True).start()

    def _stop_break(self) -> None:
        with self._break_lock:
            proc = self.break_proc
            self.break_proc = None
        if proc is not None:
            terminate_process(proc)
        self.update(fallback_running=False)
        LOGGER.info("[%s] توقف البث البديل.", self.name_value)

    def _drain_break_output(self, proc: subprocess.Popen[str]) -> None:
        if proc.stdout is None:
            return
        try:
            for line in proc.stdout:
                text = line.strip()
                if text:
                    LOGGER.info("[%s][بديل] %s", self.name_value, text)
        finally:
            try:
                proc.stdout.close()
            except Exception:
                pass
            # إن مات البديل وحده (مثلاً طرده يوتيوب عند وصول البث الحقيقي) ولم نتوقف عمداً
            with self._break_lock:
                if self.break_proc is proc:
                    self.break_proc = None
            self.update(fallback_running=False)

    def run(self) -> None:
        retry_delay = self.config.reconnect_delay
        while not self.stop_event.is_set():
            self.update(state="resolving", message="البحث عن البث المباشر", attempts=self.snapshot()["attempts"] + 1, pid=None, first_frame_at=None)
            try:
                source = self.manager.resolver.resolve(self.config, allow_muxed=self.manager.allow_muxed)
                if not source:
                    self.update(state="waiting", message=f"لا يوجد بث مباشر. المحاولة التالية خلال {retry_delay} ثانية")
                    LOGGER.warning("[%s] لم أجد بثاً مباشراً صالحاً. إعادة المحاولة بعد %d ثوانٍ.", self.name_value, retry_delay)
                    self._ensure_break()
                    self.wait_before_retry(retry_delay)
                    retry_delay = min(self.config.max_reconnect_delay, max(self.config.reconnect_delay, retry_delay * 2))
                    continue
                if source.source_url != self.config.source_url:
                    LOGGER.warning("[%s] تم التحويل إلى المصدر الاحتياطي: %s", self.name_value, source.source_url)
                    self.manager.notify("fallback", f"🔁 تحويل {self.name_value} إلى المصدر الاحتياطي: {source.source_url}")
                command = build_ffmpeg_command(source, self.output_url, self.config)
                LOGGER.info("[%s] بدء إعادة الإرسال للمصدر: %s (الوضع: %s)", self.name_value, source.title or "بدون عنوان", source.mode)
                creation_kwargs: dict[str, Any] = {
                    "stdin": subprocess.DEVNULL,
                    "stdout": subprocess.PIPE,
                    "stderr": subprocess.STDOUT,
                    "text": True,
                    "bufsize": 1,
                }
                if os.name != "nt":
                    creation_kwargs["start_new_session"] = True
                env = proxy_env(self.config)
                if env is not None:
                    creation_kwargs["env"] = env
                proc = subprocess.Popen(command, **creation_kwargs)
                started = time.time()
                self.update(state="running", message="البث يعمل", pid=proc.pid, started_at=started, first_frame_at=None, last_source=source.title, source_url=source.source_url, last_output_at=started, last_advance_at=started, stats_fps="", stats_bitrate="", stats_speed="", stats_frame=0, stats_out_time="", http_403_count=0)
                self.manager.notify("started", f"▶️ بدأ البث ({self.name_value}): {source.title or 'بدون عنوان'}")
                reader = threading.Thread(target=self.read_output, args=(proc,), daemon=True)
                reader.start()
                code = None
                dead_boot = False
                stalled = False
                starved = False
                refreshing = False
                # مهلة ظهور أول إطار: إذا لم يخرج ffmpeg أي شيء خلال هذه المدة
                # (روابط ميتة/مقاطع 403/انتظار DVR) نعيد التشغيل برابط طازج فوراً
                # بدل ترك يوتيوب بلا بيانات لعشرات الثواني.
                boot_grace = max(15, min(self.config.stall_timeout, 25))
                while not self.stop_event.is_set():
                    try:
                        code = proc.wait(timeout=1)
                        break
                    except subprocess.TimeoutExpired:
                        now = time.time()
                        snap = self.snapshot()
                        if snap["first_frame_at"] and now - started > 8 and self._break_alive():
                            # البث الحي مستقر — يوتيوب انتقل إليه تلقائياً؛ نوقف شاشة الاستراحة
                            self._stop_break()
                        last_output = snap["last_output_at"]
                        last_advance = snap["last_advance_at"]
                        first_frame_at = snap["first_frame_at"]
                        if first_frame_at is None:
                            # لا أول إطار بعد — انتظر حتى انتهاء مهلة الإقلاع ثم أعد التشغيل
                            if now - started <= boot_grace:
                                continue
                            dead_boot = True
                        elif last_advance and now - last_advance > self.config.stall_timeout:
                            starved = True
                        elif last_output and now - last_output > self.config.health_timeout:
                            stalled = True
                        elif self.config.max_session_minutes and now - started > self.config.max_session_minutes * 60:
                            refreshing = True
                        else:
                            continue
                        if dead_boot:
                            reason = "تعثر الإقلاع (لا أول إطار)"
                        elif stalled:
                            reason = "صمت ffmpeg"
                        elif starved:
                            reason = "توقف تدفق البيانات من المصدر"
                        else:
                            reason = "تجديد رابط المصدر الدوري"
                        LOGGER.warning("[%s] %s — إعادة تشغيل البث برابط جديد.", self.name_value, reason)
                        terminate_process(proc)
                        try:
                            code = proc.wait(timeout=6)
                        except subprocess.TimeoutExpired:
                            code = None
                        break
                if self.stop_event.is_set():
                    self._stop_break()
                    terminate_process(proc)
                    self.update(state="stopped", message="تم الإيقاف", pid=None, last_exit_code=code)
                    break
                reader.join(timeout=2)
                ran_for = time.time() - started
                failures = self.snapshot()["consecutive_failures"]
                blocked_hint = self.snapshot()["http_403_count"] >= 3
                fast_recovery = refreshing or stalled or starved or dead_boot or ran_for >= 120
                if fast_recovery:
                    if refreshing or ran_for >= 120:
                        # جلسة صحية أو تجديد دوري: نعيد الثقة بوضع المدمج
                        self.manager.allow_muxed = True
                        failures = 0
                    else:
                        # انقطاع/تعثر: عدّها فشلاً للإشعارات لكن أعد الاتصال بسرعة
                        failures += 1
                    retry_delay = max(3, min(5, self.config.reconnect_delay))
                else:
                    failures += 1
                    retry_delay = min(self.config.max_reconnect_delay, max(self.config.reconnect_delay, retry_delay * 2))
                # الرابط الحالي انتهى أو تعطل — أي إعادة تشغيل يجب أن تحل المصدر من جديد
                self.manager.resolver.invalidate()
                # إن كان وضع المدمج (فيديو+صوت في ملف واحد) يفشل بسرعة مرتين،
                # نتحول تلقائياً لمسارَي الفيديو والصوت المنفصلين بدل بث بلا صوت
                if source.mode == "muxed" and ran_for < 60:
                    self.manager.allow_muxed = False
                    LOGGER.warning("[%s] فشل التنسيق المدمج — سيُستخدم مسار الصوت المنفصل في المحاولة التالية.", self.name_value)
                if refreshing:
                    self.update(state="reconnecting", message=f"تجديد رابط المصدر الدوري — عودة خلال {retry_delay} ثوانٍ", pid=None, last_exit_code=code, consecutive_failures=failures)
                elif dead_boot:
                    if blocked_hint and failures >= 2:
                        # مقاطع يوتيوب نفسها ترفض الطلبات — غالباً IP الخادم محجوب
                        message = f"يوتيوب تحجب مقاطع الفيديو عن خادمك (403 متكررة) — جرّب بروكسي من الإعدادات أو غيّر الاستضافة. المحاولة التالية خلال {retry_delay} ثوانٍ"
                        LOGGER.warning("[%s] مقاطع يوتيوب مرفوضة بـ 403 (%d مرات) — يبدو أن IP الخادم محجوب لدى CDN يوتيوب؛ فعّل بروكسي أو غيّر الاستضافة.", self.name_value, self.snapshot()["http_403_count"])
                        self.manager.notify("blocked", f"🚫 {self.name_value}: يوتيوب تحجب مقاطع الفيديو عن عنوان IP الخادم (403) — فعّل بروكسي من الإعدادات أو غيّر الاستضافة")
                    else:
                        message = f"تعذر بدء التدفق من المصدر؛ إعادة الاتصال برابط جديد خلال {retry_delay} ثوانٍ"
                    self.update(state="reconnecting", message=message, pid=None, last_exit_code=code, consecutive_failures=failures)
                elif starved:
                    self.update(state="reconnecting", message=f"توقف تدفق البيانات من المصدر؛ إعادة الاتصال برابط جديد خلال {retry_delay} ثوانٍ", pid=None, last_exit_code=code, consecutive_failures=failures)
                    self.manager.notify("starved", f"⚠️ توقف تدفق البيانات في {self.name_value} رغم استمرار الاتصال — إعادة الاتصال برابط جديد تلقائياً")
                elif stalled:
                    self.update(state="reconnecting", message=f"صمت ffmpeg؛ تحديث المصدر بعد {retry_delay} ثانية", pid=None, last_exit_code=code, consecutive_failures=failures)
                    self.manager.notify("stalled", f"⚠️ توقف استجابة ffmpeg في {self.name_value} — إعادة تشغيل البث تلقائياً")
                else:
                    self.update(state="reconnecting", message=f"ffmpeg توقف برمز {code}; تحديث المصدر بعد {retry_delay} ثانية", pid=None, last_exit_code=code, consecutive_failures=failures)
                if failures == 3:
                    self.manager.notify("failing", f"🔴 {self.name_value} فشل 3 مرات متتالية — آخر رسالة: {self.snapshot()['message']}")
                LOGGER.warning("[%s] ffmpeg توقف برمز %s بعد %.0f ثانية. سيتم التحديث.", self.name_value, code, ran_for)
                if not refreshing:
                    # أثناء انتظار إعادة الاتصال: شاشة الاستراحة تحفظ البث من القطع
                    self._ensure_break()
                self.wait_before_retry(retry_delay)
            except StopRequested:
                break
            except Exception as exc:
                failures = self.snapshot()["consecutive_failures"] + 1
                retry_delay = min(self.config.max_reconnect_delay, max(self.config.reconnect_delay, retry_delay * 2))
                self.update(state="error", message=f"{exc}; إعادة المحاولة بعد {retry_delay} ثانية", pid=None, consecutive_failures=failures)
                LOGGER.exception("[%s] خطأ أثناء إعادة الإرسال", self.name_value)
                self.wait_before_retry(retry_delay)
        self._stop_break()
        self.update(state="stopped", message="متوقف", pid=None)

    def read_output(self, proc: subprocess.Popen[str]) -> None:
        if proc.stdout is None:
            return
        progress: dict[str, str] = {}
        first_seen = False
        http_403_seen = 0
        try:
            for line in proc.stdout:
                text = line.strip()
                if not text:
                    continue
                self.update(last_output_at=time.time())
                if "=" in text and not text.startswith(" "):
                    key, _, value = text.partition("=")
                    if re.fullmatch(r"[a-z0-9_]+", key):
                        progress[key] = value
                        if key == "progress":
                            frame = int(number(progress.get("frame"), 0))
                            out_us = int(number(progress.get("out_time_us") or progress.get("out_time_ms"), 0))
                            if frame != self._last_frame or out_us != self._last_out_us:
                                self._last_frame, self._last_out_us = frame, out_us
                                self.update(last_advance_at=time.time())
                            if frame > 0 and not first_seen:
                                first_seen = True
                                self.update(first_frame_at=time.time())
                            self.update(
                                stats_fps=progress.get("fps", ""),
                                stats_bitrate=progress.get("bitrate", ""),
                                stats_speed=progress.get("speed", ""),
                                stats_frame=frame,
                                stats_out_time=progress.get("out_time", "")[:11],
                            )
                            progress = {}
                        continue
                # مقاطع يوتيوب ترفضنا بـ 403 = غالباً IP الخادم محجوب لدى CDN
                if "403" in text and ("Forbidden" in text or "forbidden" in text):
                    http_403_seen += 1
                    if http_403_seen in {1, 3, 10}:
                        self.update(http_403_count=http_403_seen)
                LOGGER.info("[%s] %s", self.name_value, text)
        finally:
            try:
                proc.stdout.close()
            except Exception:
                pass


class RelayManager:
    def __init__(self, config: RelayConfig):
        self.config = config
        self.lock = threading.RLock()
        self.workers: list[RelayWorker] = []
        self.started_at: Optional[float] = None
        self.resolver = SourceResolver()
        # يسمح بالتنسيق المدمج (فيديو+صوت بملف واحد). يُعطَّل مؤقتاً عندما يفشل
        # المدمج مرتين متتاليتين فيُستخدم مسار الصوت المنفصل — يضمن عدم بث صامت.
        self.allow_muxed = True

    def output_urls(self) -> list[str]:
        return [f"{self.config.rtmp_base.rstrip('/')}/{key.lstrip('/')}" for key in self.config.stream_keys]

    def notify(self, event: str, message: str) -> None:
        NOTIFIER.notify(event, message, self.config.notify_webhook)

    def start(self) -> tuple[bool, str]:
        with self.lock:
            if any(worker.is_alive() for worker in self.workers):
                return False, "البث يعمل بالفعل"
            if not self.config.stream_keys:
                return False, "أدخل مفتاح بث واحداً على الأقل"
            errors = self.config.validate()
            if errors:
                return False, errors[0]
            self.workers = []
            self.started_at = time.time()
            self.allow_muxed = True
            for index, output_url in enumerate(self.output_urls(), start=1):
                worker = RelayWorker(self, f"stream-{index}", output_url, self.config)
                self.workers.append(worker)
                worker.start()
            LOGGER.info("بدأت المراقبة. عدد الوجهات: %d", len(self.workers))
            return True, "تم تشغيل المراقبة وإعادة الإرسال"

    def stop(self) -> tuple[bool, str]:
        with self.lock:
            workers = [worker for worker in self.workers if worker.is_alive()]
            if not workers:
                return False, "البث متوقف بالفعل"
            for worker in workers:
                worker.stop_event.set()
        for worker in workers:
            worker.join(timeout=8)
        LOGGER.info("تم إيقاف إعادة الإرسال")
        return True, "تم الإيقاف"

    def start_one(self, name: str) -> tuple[bool, str]:
        """تشغيل وجهة واحدة بالاسم (stream-N) دون باقي الوجهات."""
        with self.lock:
            urls = self.output_urls()
            if not urls:
                return False, "أدخل مفتاح بث واحداً على الأقل"
            errors = self.config.validate()
            if errors:
                return False, errors[0]
            match = re.fullmatch(r"stream-(\d+)", (name or "").strip())
            index = int(match.group(1)) - 1 if match else -1
            if index < 0 or index >= len(urls):
                return False, "وجهة غير معروفة"
            while len(self.workers) <= index:
                slot = len(self.workers)
                self.workers.append(RelayWorker(self, f"stream-{slot + 1}", urls[slot], self.config))
            if self.workers[index].is_alive():
                return False, "الوجهة تعمل بالفعل"
            worker = RelayWorker(self, f"stream-{index + 1}", urls[index], self.config)
            self.workers[index] = worker
            if not self.started_at:
                self.started_at = time.time()
            self.allow_muxed = True
            worker.start()
            LOGGER.info("تشغيل الوجهة %s", worker.name_value)
            return True, f"تم تشغيل {worker.name_value}"

    def stop_one(self, name: str) -> tuple[bool, str]:
        """إيقاف وجهة واحدة دون باقي الوجهات."""
        with self.lock:
            worker = next((item for item in self.workers if item.name_value == name), None)
            if not worker or not worker.is_alive():
                return False, "الوجهة متوقفة بالفعل"
            worker.stop_event.set()
        worker.join(timeout=8)
        LOGGER.info("إيقاف الوجهة %s", name)
        return True, f"تم إيقاف {name}"

    def restart(self) -> tuple[bool, str]:
        self.stop()
        time.sleep(0.3)
        return self.start()

    def break_all(self, action: str) -> tuple[bool, str]:
        """تشغيل/إيقاف شاشة الاستراحة يدوياً لكل الوجهات العاملة."""
        with self.lock:
            workers = [w for w in self.workers if w.is_alive()]
        if action == "start":
            if not workers:
                return False, "لا توجد وجهات عاملة"
            for worker in workers:
                worker._ensure_break()
            return True, "تم تشغيل شاشة الاستراحة على الوجهات العاملة"
        if action == "stop":
            for worker in workers:
                worker._stop_break()
            return True, "تم إيقاف شاشة الاستراحة"
        return False, "إجراء غير معروف" 

    def is_running(self) -> bool:
        with self.lock:
            return any(worker.is_alive() for worker in self.workers)

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            workers = list(self.workers)
            started_at = self.started_at
        worker_data = [worker.snapshot() for worker in workers]
        running = any(worker["state"] in {"running", "resolving", "reconnecting", "waiting", "error"} for worker in worker_data)
        return {
            "running": running,
            "started_at": started_at,
            "uptime_seconds": max(0, int(time.time() - started_at)) if started_at and running else 0,
            "worker_count": len(worker_data),
            "workers": worker_data,
            "ffmpeg": FFMPEG or "",
            "port": SERVER_PORT,
            "auth_required": bool(PANEL_TOKEN),
            "config": public_config(self.config),
        }


def mask_output_url(url: str) -> str:
    parsed = urlparse(url)
    if not parsed.scheme:
        return url
    path = parsed.path
    if path:
        parts = path.rsplit("/", 1)
        if len(parts) == 2:
            path = parts[0] + "/••••••••"
    return f"{parsed.scheme}://{parsed.netloc}{path}"


def public_config(config: RelayConfig) -> dict[str, Any]:
    data = asdict(config)
    data["stream_keys"] = []
    data["stream_key_count"] = len(config.stream_keys)
    data["has_stream_keys"] = bool(config.stream_keys)
    data.pop("panel_token", None)
    data["has_panel_token"] = bool(PANEL_TOKEN or config.panel_token)
    # إخفاء بيانات اعتماد البروكسي (user:pass) — الواجهة تعرض نسخة مقنّعة
    data["proxy"] = mask_proxy(config.proxy)
    data["has_proxy"] = bool(config.proxy)
    return data


HTML = r'''<!doctype html>
<html lang="ar" dir="rtl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'%3E%3Ccircle cx='50' cy='50' r='42' fill='%230da9d2'/%3E%3Cpolygon points='40,32 70,50 40,68' fill='white'/%3E%3C/svg%3E">
<title>لوحة تحكم البث المباشر</title>
<style>
:root{color-scheme:dark;--bg:#0a0d14;--panel:#111725;--panel2:#151d2e;--line:#26334b;--text:#edf3ff;--muted:#95a3bb;--cyan:#42d9ff;--green:#34d399;--red:#fb7185;--yellow:#fbbf24;--shadow:0 18px 60px #0005}*{box-sizing:border-box}body{margin:0;background:radial-gradient(circle at 85% -10%,#19365d 0,#0a0d14 42%);color:var(--text);font-family:Inter,ui-sans-serif,system-ui,-apple-system,"Segoe UI",Tahoma,Arial,sans-serif;min-height:100vh}main{max-width:1180px;margin:auto;padding:32px 20px 42px}.top{display:flex;justify-content:space-between;gap:20px;align-items:flex-start;margin-bottom:28px}.eyebrow{color:var(--cyan);font-size:12px;font-weight:800;letter-spacing:.14em;text-transform:uppercase}.title{font-size:clamp(28px,5vw,46px);line-height:1.05;margin:8px 0 10px;letter-spacing:-.04em}.subtitle{color:var(--muted);max-width:690px;margin:0;line-height:1.8}.status-pill{display:flex;align-items:center;gap:9px;border:1px solid var(--line);background:#121a2a;padding:10px 14px;border-radius:999px;white-space:nowrap;color:var(--muted);box-shadow:var(--shadow)}.dot{width:9px;height:9px;border-radius:50%;background:#64748b}.dot.live{background:var(--green);box-shadow:0 0 18px var(--green)}.dot.warn{background:var(--yellow);box-shadow:0 0 18px var(--yellow)}.dot.bad{background:var(--red);box-shadow:0 0 18px var(--red)}.grid{display:grid;grid-template-columns:1.1fr .9fr;gap:18px}.card{background:linear-gradient(145deg,#131b2bdf,#0f1521e8);border:1px solid var(--line);border-radius:20px;padding:22px;box-shadow:var(--shadow);backdrop-filter:blur(14px)}.card h2{font-size:17px;margin:0 0 16px}.card-head{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:16px}.stats{display:grid;grid-template-columns:repeat(3,1fr);gap:10px}.stat{padding:14px;border:1px solid var(--line);border-radius:14px;background:#0d1421}.stat b{display:block;font-size:22px;margin-top:7px}.label{font-size:12px;color:var(--muted)}form{display:grid;gap:14px}.row{display:grid;grid-template-columns:1fr 1fr;gap:12px}.field{display:grid;gap:7px}.field label{font-size:13px;color:#c2cee1}.field small{color:var(--muted);font-size:11px;line-height:1.5}.field input,.field textarea,.field select{width:100%;border:1px solid var(--line);border-radius:11px;background:#0b111d;color:var(--text);padding:11px 12px;font:inherit;outline:none;transition:.18s}.field textarea{min-height:92px;resize:vertical}.field input:focus,.field textarea:focus,.field select:focus{border-color:var(--cyan);box-shadow:0 0 0 3px #42d9ff18}.check{display:flex;align-items:center;gap:9px;color:#d6e1f3;font-size:13px}.check input{accent-color:var(--cyan);width:16px;height:16px}.actions{display:flex;flex-wrap:wrap;gap:10px;margin-top:4px}.btn{border:1px solid var(--line);background:#172237;color:var(--text);border-radius:11px;padding:11px 16px;font:inherit;font-weight:750;cursor:pointer;transition:.18s}.btn:hover{transform:translateY(-1px);border-color:#4e6b96}.btn.primary{background:linear-gradient(135deg,#0da9d2,#2873e3);border-color:#3dd5ff;color:#fff}.btn.danger{background:#3a1725;border-color:#743149;color:#fecdd3}.btn.ghost{background:transparent}.btn:disabled{opacity:.45;cursor:not-allowed;transform:none}.logbox{background:#080c13;border:1px solid #1f2a3f;border-radius:13px;min-height:310px;max-height:420px;overflow:auto;padding:14px;direction:ltr;text-align:left;font:12px/1.7 ui-monospace,SFMono-Regular,Menlo,monospace;color:#b8c7df;white-space:pre-wrap}.worker-list{display:grid;gap:10px}.worker{border:1px solid var(--line);background:#0c1320;border-radius:13px;padding:13px}.worker-top{display:flex;justify-content:space-between;gap:10px;align-items:center}.worker-name{font-weight:800}.badge{font-size:11px;padding:4px 8px;border-radius:999px;background:#1d2a40;color:#bac9df}.badge.running{background:#0e3b35;color:#6ee7b7}.badge.waiting,.badge.resolving,.badge.reconnecting{background:#3b3010;color:#fcd34d}.badge.error{background:#3d1724;color:#fda4af}.worker-msg{color:var(--muted);font-size:12px;margin-top:8px;line-height:1.5}.chart{width:100%;max-width:280px;height:44px;margin-top:10px;background:#080d16;border:1px solid #1c2740;border-radius:8px}.wbtn{padding:5px 11px;font-size:12px;border-radius:8px}.notice{display:none;margin-bottom:16px;padding:12px 14px;border-radius:11px;border:1px solid var(--line);background:#122036}.notice.show{display:block}.notice.error{border-color:#743149;background:#3a1725;color:#fecdd3}.notice.ok{border-color:#1d6658;background:#0e332e;color:#a7f3d0}.footer{color:#71829c;font-size:12px;margin-top:18px;text-align:center}.full{grid-column:1/-1}@media(max-width:850px){.grid{grid-template-columns:1fr}.top{flex-direction:column}.status-pill{align-self:flex-start}.row{grid-template-columns:1fr}.stats{grid-template-columns:1fr 1fr}.full{grid-column:auto}}@media(max-width:440px){main{padding:22px 12px}.card{padding:17px}.stats{grid-template-columns:1fr}}
</style>
</head>
<body>
<main>
<header class="top"><div><div class="eyebrow">AUTOMATED LIVE RELAY</div><h1 class="title">لوحة تحكم البث المباشر</h1><p class="subtitle">إدارة احترافية لإعادة إرسال البث المباشر مع تحديث تلقائي لرابط المصدر، إعادة اتصال بعد سقوط ffmpeg، ومراقبة واضحة لكل وجهة.</p></div><div class="status-pill"><span id="statusDot" class="dot"></span><span id="statusText">متوقف</span></div></header>
<div id="notice" class="notice"></div>
<section class="grid">
<div class="card"><div class="card-head"><h2>الحالة الآن</h2><span id="refreshLabel" class="label">تحديث تلقائي</span></div><div class="stats"><div class="stat"><span class="label">الحالة</span><b id="mainState">متوقف</b></div><div class="stat"><span class="label">الوجهات</span><b id="workerCount">0</b></div><div class="stat"><span class="label">مدة التشغيل</span><b id="uptime">00:00:00</b></div></div><div class="actions"><button id="startBtn" class="btn primary">تشغيل البث</button><button id="restartBtn" class="btn">إعادة تشغيل</button><button id="stopBtn" class="btn danger">إيقاف</button></div></div>
<div class="card"><div class="card-head"><h2>الوجهات</h2><span class="label">تحكم فردي بكل وجهة — المفاتيح مخفية</span></div><div id="workers" class="worker-list"><div class="worker-msg">لا توجد وجهات قيد التشغيل.</div></div></div>
<div class="card"><div class="card-head"><h2>أدوات التحكم</h2><span class="label">كل شيء من هنا</span></div><div class="actions"><button id="refreshSourceBtn" class="btn">تحديث المصدر الآن</button><button id="breakStartBtn" class="btn">تشغيل شاشة الاستراحة</button><button id="breakStopBtn" class="btn">إيقاف شاشة الاستراحة</button><button id="testWebhookBtn" class="btn">اختبار Webhook</button><button id="downloadLogBtn" class="btn">تنزيل السجل الكامل</button><button id="restartAppBtn" class="btn danger">إعادة تشغيل التطبيق</button></div><div class="row" style="margin-top:16px"><div class="field"><label for="log_level">مستوى السجل</label><select id="log_level"><option>DEBUG</option><option>INFO</option><option>WARNING</option><option>ERROR</option></select><small>يُطبق فوراً ويُحفظ مع الإعدادات.</small></div><div class="field"><label for="new_token">تغيير رمز اللوحة</label><input id="current_token" type="password" placeholder="الرمز الحالي (إن وُجد)" autocomplete="off"><input id="new_token" type="password" placeholder="رمز جديد — 6 أحرف فأكثر" autocomplete="new-password"><small>يُحفظ في config.json ويُطلب عند الدخول التالي.</small></div></div><div class="actions"><button id="applyLogLevelBtn" class="btn">تطبيق مستوى السجل</button><button id="changeTokenBtn" class="btn primary">تغيير الرمز</button></div></div>
<div class="card full"><div class="card-head"><h2>الإعدادات</h2><span class="label">يمكن الحفظ أثناء التشغيل — يُعاد تشغيل البث تلقائياً لتطبيق التغييرات</span></div><form id="configForm"><div class="row"><div class="field"><label for="source_url">رابط المصدر الرئيسي</label><input id="source_url" name="source_url" type="url" required><small>رابط بث YouTube أو صفحة /live للقناة.</small></div><div class="field"><label for="rtmp_base">عنوان RTMP الأساسي</label><input id="rtmp_base" name="rtmp_base" type="url" required><small>مثال: rtmp://a.rtmp.youtube.com/live2</small></div></div><div class="field"><label for="backup_sources">مصادر احتياطية اختيارية</label><textarea id="backup_sources" name="backup_sources" placeholder="ضع رابطاً في كل سطر"></textarea><small>إذا تعذر المصدر الرئيسي، يجرب التطبيق هذه الروابط بالترتيب تلقائياً.</small></div><div class="field"><label for="stream_keys">مفاتيح البث</label><textarea id="stream_keys" name="stream_keys" placeholder="ضع مفتاحاً في كل سطر أو افصل بينها بفواصل"></textarea><small id="keysHint">إذا كان هناك مفتاح محفوظ، اترك الحقل فارغاً للإبقاء عليه. لا يظهر المفتاح بعد الحفظ.</small></div><div class="row"><div class="field"><label for="resolution">الدقة</label><select id="resolution" name="resolution"><option>1920x1080</option><option>1280x720</option><option>854x480</option><option>640x360</option></select></div><div class="field"><label for="fps">الإطارات في الثانية</label><input id="fps" name="fps" type="number" min="1" max="120"></div></div><div class="row"><div class="field"><label for="video_bitrate">معدل الفيديو</label><input id="video_bitrate" name="video_bitrate" placeholder="3000k"></div><div class="field"><label for="audio_bitrate">معدل الصوت</label><input id="audio_bitrate" name="audio_bitrate" placeholder="160k"></div></div><div class="row"><div class="field"><label for="reconnect_delay">أول تأخير بين المحاولات (ثانية)</label><input id="reconnect_delay" name="reconnect_delay" type="number" min="3" max="300"></div><div class="field"><label for="max_reconnect_delay">أقصى تأخير تلقائي (ثانية)</label><input id="max_reconnect_delay" name="max_reconnect_delay" type="number" min="3" max="900"></div></div><div class="row"><div class="field"><label for="health_timeout">مهلة صحة البث (ثانية)</label><input id="health_timeout" name="health_timeout" type="number" min="15" max="600"><small>إذا صمت ffmpeg تماماً أكثر من هذه المدة يُعاد تشغيل البث.</small></div><div class="field"><label for="stall_timeout">مهلة انقطاع البيانات (ثانية)</label><input id="stall_timeout" name="stall_timeout" type="number" min="10" max="300"><small>إذا توقف تقدّم البيانات — أو لم يظهر أول إطار عند الإقلاع — أكثر من هذه المهلة يُعاد الاتصال برابط جديد فوراً قبل أن تظهر رسالة «لا تتوفّر بيانات» في يوتيوب. الموصى: 15.</small></div></div><div class="row"><div class="field"><label for="preset">سرعة ترميز x264</label><select id="preset" name="preset"><option>ultrafast</option><option>superfast</option><option>veryfast</option><option>faster</option><option>fast</option><option>medium</option><option>slow</option></select><small>على خادم ضعيف اختر faster أو fast حتى لا يتأخر الترميز عن الزمن الحقيقي (سبب رسالة «البيانات غير كافية»).</small></div><div class="field"><label for="max_session_minutes">تجديد رابط المصدر كل (دقيقة)</label><input id="max_session_minutes" name="max_session_minutes" type="number" min="0" max="720"><small>روابط بث يوتيوب تنتهي صلاحيتها أثناء العمل؛ يُجدد الرابط تلقائياً قبل موته. 0 = تعطيل. الموصى: 120.</small></div></div><div class="row"><div class="field"><label for="cookies_from_browser">مصدر Cookies اختياري</label><input id="cookies_from_browser" name="cookies_from_browser" placeholder="chrome أو chrome:Default"><small>اتركه فارغاً إلا إذا كان المصدر يحتاج تسجيل دخول.</small></div><div class="field"><label for="cookiefile">ملف Cookies اختياري</label><input id="cookiefile" name="cookiefile" placeholder="/path/to/cookies.txt"></div></div><div class="row"><div class="field"><label for="proxy">بروكسي اختياري (http/https)</label><input id="proxy" name="proxy" placeholder="http://user:pass@host:3128"><small>استخدمه إذا كانت يوتيوب تحجب مقاطع الفيديو عن IP الخادم (أخطاء 403 في السجل). اتركه فارغاً للإبقاء على القيمة الحالية.</small></div><div class="field"><label class="check" style="margin-top:26px"><input id="clear_proxy" type="checkbox"> حذف البروكسي الحالي عند الحفظ</label><small>فعّله فقط إذا أردت إزالة بروكسي محفوظ نهائياً.</small></div></div><div class="row"><div class="field"><label for="logo_path">شعار البث (صورة PNG بخلفية شفافة)</label><input id="logo_path" name="logo_path" placeholder="/opt/yoteblive/media/logo.png"><small>مسار صورة على الخادم. تُعرض فوق البث في زاوية الشاشة. اتركها فارغة لإيقاف الشعار.</small></div><div class="field"><label for="logo_mode">وضع الشعار</label><select id="logo_mode" name="logo_mode"><option value="off">متوقف</option><option value="always">دائم</option><option value="periodic">دوري (يظهر ثم يختفي)</option></select><small>الدوري يظهر الشعار logo_show ثانية كل (logo_show + logo_hide) ثانية.</small></div></div><div class="row"><div class="field"><label for="logo_position">موضع الشعار</label><select id="logo_position" name="logo_position"><option value="tl">أعلى يسار</option><option value="tr">أعلى يمين</option><option value="bl">أسفل يسار</option><option value="br">أسفل يمين</option><option value="center">الوسط</option></select></div><div class="field"><label for="logo_width">عرض الشعار بالبكسل (0 = تلقائي)</label><input id="logo_width" name="logo_width" type="number" min="0" max="2000"><small>يُصغَّر تلقائياً إن كانت الصورة أكبر.</small></div></div><div class="row"><div class="field"><label for="logo_show">مدة الظهور (ثانية)</label><input id="logo_show" name="logo_show" type="number" min="1" max="3600"></div><div class="field"><label for="logo_hide">مدة الاختفاء (ثانية)</label><input id="logo_hide" name="logo_hide" type="number" min="1" max="3600"></div></div><div class="field"><label for="pip_slots">منافذ العرض (PiP) — صورة/فيديو تُعرض فوق البث</label><textarea id="pip_slots" name="pip_slots" placeholder='{"name":"صورة الموضوع","path":"/opt/yoteblive/media/subject.png","position":"br","width":480,"mode":"periodic","show":20,"hide":30}'></textarea><small>سطر JSON واحد لكل منفذ (حتى 3). الوضع: off/always/periodic. مثال: صورة تعرضها عند حديث المتحدث عنها. الفيديو بصيغ mp4/mkv/webm. تُطبق التغييرات عند الحفظ (إعادة تشغيل البث).</small></div><div class="field"><label for="break_text">شاشة الاستراحة — النص (يظهر عند انقطاع المصدر)</label><input id="break_text" name="break_text" placeholder="سنعود قريباً"></div><div class="row"><div class="field"><label for="break_image">صورة خلفية الاستراحة (اختياري)</label><input id="break_image" name="break_image" placeholder="/opt/yoteblive/media/break.png"><small>فارغة = خلفية داكنة بنص عربي.</small></div><div class="field"><label for="break_audio">ملف صوت خلفية (اختياري)</label><input id="break_audio" name="break_audio" placeholder="/opt/yoteblive/media/break.mp3"><small>فارغ = صمت. mp3/m4a/aac.</small></div></div><label class="check"><input id="break_enabled" name="break_enabled" type="checkbox"> تفعيل البث البديل تلقائياً عند انقطاع المصدر (يمنع رسالة «سينتهي البث» في يوتيوب)</label><div class="field"><label for="notify_webhook">رابط إشعارات Webhook اختياري</label><input id="notify_webhook" name="notify_webhook" type="url" placeholder="https://discord.com/api/webhooks/..."><small>يصلك إشعار عند بدء البث، تحويل المصدر الاحتياطي، توقف التدفق، أو تكرار الفشل. يدعم Discord وSlack وntfy وأي نقطة تقبل JSON.</small></div><label class="check"><input id="auto_start" name="auto_start" type="checkbox"> تشغيل تلقائي عند تشغيل التطبيق</label><div class="actions"><button class="btn primary" type="submit">حفظ الإعدادات</button><button class="btn" type="button" id="testSourceBtn">اختبار المصدر</button><button class="btn ghost" type="button" id="clearKeysBtn">حذف المفاتيح المحفوظة</button></div></form></div>
<div class="card full"><div class="card-head"><h2>سجل التشغيل</h2><button class="btn ghost" id="clearLogBtn">مسح العرض</button></div><div id="logs" class="logbox">جاري تحميل السجل…</div></div>
</section><div class="footer">الواجهة تعمل على المنفذ __PORT__ — تحديث الحالة كل 3 ثوانٍ</div>
</main>
<script>
const $=id=>document.getElementById(id);let lastConfig=null;let panelToken=localStorage.getItem('panelToken')||'';let chartData={};const CHART_MAX=60;
function parseKbps(s){let m=String(s||'').match(/([\d.]+)\s*kbits/);return m?parseFloat(m[1]):null}
function drawChart(name){let c=document.getElementById('chart-'+name);if(!c)return;let data=chartData[name]||[];if(data.length<2)return;let ctx=c.getContext('2d'),w=c.width,h=c.height;ctx.clearRect(0,0,w,h);let max=Math.max(...data)*1.15||1,min=0;ctx.beginPath();data.forEach((v,i)=>{let x=i/(CHART_MAX-1)*w,y=h-4-((v-min)/(max-min))*(h-8);i?ctx.lineTo(x,y):ctx.moveTo(x,y)});ctx.strokeStyle='#42d9ff';ctx.lineWidth=1.6;ctx.stroke();ctx.lineTo((data.length-1)/(CHART_MAX-1)*w,h);ctx.lineTo(0,h);ctx.closePath();ctx.fillStyle='#42d9ff22';ctx.fill();let last=data[data.length-1];ctx.fillStyle='#95a3bb';ctx.font='10px ui-monospace,monospace';ctx.fillText(`${Math.round(last)} kb/s`,6,12)}
function esc(s){return String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]))}
function duration(sec){sec=Math.max(0,Number(sec||0));let h=String(Math.floor(sec/3600)).padStart(2,'0'),m=String(Math.floor(sec%3600/60)).padStart(2,'0'),s=String(Math.floor(sec%60)).padStart(2,'0');return `${h}:${m}:${s}`}
function notice(msg,type='ok'){let n=$('notice');n.textContent=msg;n.className=`notice show ${type}`;clearTimeout(window.noticeTimer);window.noticeTimer=setTimeout(()=>n.className='notice',5500)}
async function api(url,options={},retry=true){let headers={'Content-Type':'application/json',...(options.headers||{})};if(panelToken)headers['X-Panel-Token']=panelToken;let r=await fetch(url,{...options,headers});let d=await r.json().catch(()=>({message:'رد غير صالح'}));if(r.status===401&&retry){let t=prompt('أدخل رمز الدخول للوحة التحكم:');if(t!==null){panelToken=t.trim();localStorage.setItem('panelToken',panelToken);return api(url,options,false)}}if(!r.ok)throw new Error(d.message||d.error||'حدث خطأ');return d}
function stateLabel(s){return ({running:'يعمل',resolving:'يبحث عن المصدر',waiting:'ينتظر البث',reconnecting:'يعيد الاتصال',error:'خطأ',stopped:'متوقف'})[s]||s||'متوقف'}
function renderStatus(d){let active=d.running,states=(d.workers||[]).map(w=>w.state),hasError=states.includes('error'),waiting=states.some(s=>['waiting','resolving','reconnecting'].includes(s));$('statusText').textContent=active?(hasError?'يعمل مع خطأ مؤقت':waiting?'ينتظر المصدر':'البث يعمل'):'متوقف';$('statusDot').className=`dot ${active?(hasError?'bad':waiting?'warn':'live'):''}`;$('mainState').textContent=active?(hasError?'خطأ مؤقت':waiting?'إعادة اتصال':'يعمل'):'متوقف';$('workerCount').textContent=d.worker_count||0;$('uptime').textContent=duration(d.uptime_seconds);$('startBtn').disabled=active;$('stopBtn').disabled=!active;$('restartBtn').disabled=!active&&!(lastConfig&&lastConfig.has_stream_keys);let box=$('workers');if(!d.workers?.length){box.innerHTML='<div class="worker-msg">لا توجد وجهات قيد التشغيل.</div>';return}box.innerHTML=d.workers.map(w=>{let stats='',chart='',alive=w.state!=='stopped';if(w.state==='running'&&(w.stats_fps||w.stats_bitrate||w.stats_speed)){stats=`<br>⚡ ${esc(w.stats_fps||'—')} fps — ${esc(w.stats_bitrate||'—')} — سرعة ${esc(w.stats_speed||'—')}${w.stats_frame?` — ${w.stats_frame} إطار`:''}${w.stats_out_time?` — ${esc(w.stats_out_time)}`:''}`;let kb=parseKbps(w.stats_bitrate);if(kb!==null){(chartData[w.name]=chartData[w.name]||[]).push(kb);if(chartData[w.name].length>CHART_MAX)chartData[w.name].shift();chart=`<canvas class="chart" id="chart-${esc(w.name)}" width="280" height="44"></canvas>`}}let btn=alive?`<button class="btn danger wbtn" data-act="stop" data-name="${esc(w.name)}">إيقاف</button>`:`<button class="btn primary wbtn" data-act="start" data-name="${esc(w.name)}">تشغيل</button>`;let fb=w.fallback_running?`<span class="badge waiting">▶ استراحة تعمل</span>`:'';return `<div class="worker"><div class="worker-top"><span class="worker-name">${esc(w.name)}</span><span style="display:flex;gap:8px;align-items:center">${fb}<span class="badge ${esc(w.state)}">${esc(stateLabel(w.state))}</span>${btn}</span></div><div class="worker-msg">${esc(w.message)}${w.last_source?`<br>المصدر: ${esc(w.last_source)}`:''}${stats}${w.attempts?`<br>المحاولات: ${w.attempts} — مدة التشغيل: ${duration(w.uptime_seconds)}`:''}</div>${chart}</div>`}).join('');d.workers.forEach(w=>drawChart(w.name));box.querySelectorAll('.wbtn').forEach(b=>b.onclick=async()=>{b.disabled=true;try{let r=await api('/api/worker/'+b.dataset.act,{method:'POST',body:JSON.stringify({name:b.dataset.name})});notice(r.message)}catch(e){notice(e.message,'error')}finally{refresh()}})}
function renderConfig(c){lastConfig=c;$('source_url').value=c.source_url||'';$('backup_sources').value=(c.backup_sources||[]).join('\n');$('rtmp_base').value=c.rtmp_base||'';$('resolution').value=c.resolution||'1280x720';$('fps').value=c.fps||30;$('video_bitrate').value=c.video_bitrate||'3000k';$('audio_bitrate').value=c.audio_bitrate||'160k';$('preset').value=c.preset||'veryfast';$('reconnect_delay').value=c.reconnect_delay||10;$('max_reconnect_delay').value=c.max_reconnect_delay||60;$('health_timeout').value=c.health_timeout||45;$('cookies_from_browser').value=c.cookies_from_browser||'';$('cookiefile').value=c.cookiefile||'';$('proxy').value=c.proxy||'';$('proxy').placeholder=c.has_proxy?'بروكسي محفوظ — اتركه فارغاً للإبقاء عليه':'http://user:pass@host:3128';$('clear_proxy').checked=false;$('logo_path').value=c.logo_path||'';$('logo_mode').value=c.logo_mode||'off';$('logo_position').value=c.logo_position||'br';$('logo_width').value=c.logo_width||0;$('logo_show').value=c.logo_show||12;$('logo_hide').value=c.logo_hide||40;$('pip_slots').value=(c.pip_slots||[]).map(x=>JSON.stringify(x)).join('\n');$('break_text').value=c.break_text||'سنعود قريباً';$('break_image').value=c.break_image||'';$('break_audio').value=c.break_audio||'';$('break_enabled').checked=!!c.break_enabled;$('notify_webhook').value=c.notify_webhook||'';$('log_level').value=c.log_level||'INFO';$('auto_start').checked=!!c.auto_start;$('keysHint').textContent=c.has_stream_keys?`يوجد ${c.stream_key_count} مفتاح محفوظ. اترك الحقل فارغاً للإبقاء عليه.`:'لا يوجد مفتاح محفوظ حالياً.'}
async function refresh(){try{let [s,l]=await Promise.all([api('/api/status'),api('/api/logs')]);renderStatus(s);let box=$('logs'),nearBottom=box.scrollHeight-box.scrollTop-box.clientHeight<40;box.textContent=(l.lines||[]).join('\n')||'لا يوجد سجل بعد.';if(nearBottom)box.scrollTop=box.scrollHeight}catch(e){notice(e.message,'error')}}
$('configForm').addEventListener('submit',async e=>{e.preventDefault();let f=new FormData(e.target),keys=String(f.get('stream_keys')||'').trim();try{let d=await api('/api/config',{method:'POST',body:JSON.stringify({source_url:f.get('source_url'),backup_sources:String(f.get('backup_sources')||'').split(/[\n,]+/).map(x=>x.trim()).filter(Boolean),rtmp_base:f.get('rtmp_base'),stream_keys:keys?keys.split(/[\n,]+/).map(x=>x.trim()).filter(Boolean):[],resolution:f.get('resolution'),fps:Number(f.get('fps')),video_bitrate:f.get('video_bitrate'),audio_bitrate:f.get('audio_bitrate'),preset:f.get('preset')||'veryfast',reconnect_delay:Number(f.get('reconnect_delay')),max_reconnect_delay:Number(f.get('max_reconnect_delay')),health_timeout:Number(f.get('health_timeout')),stall_timeout:Number(f.get('stall_timeout')),max_session_minutes:Number(f.get('max_session_minutes')),cookies_from_browser:f.get('cookies_from_browser'),cookiefile:f.get('cookiefile'),proxy:$('proxy').value.trim(),clear_proxy:$('clear_proxy').checked,logo_path:$('logo_path').value.trim(),logo_position:$('logo_position').value,logo_width:Number($('logo_width').value),logo_mode:$('logo_mode').value,logo_show:Number($('logo_show').value),logo_hide:Number($('logo_hide').value),pip_slots:$('pip_slots').value.split(/\n+/).map(x=>x.trim()).filter(Boolean).map(x=>JSON.parse(x)),break_enabled:$('break_enabled').checked,break_image:$('break_image').value.trim(),break_text:$('break_text').value.trim(),break_audio:$('break_audio').value.trim(),notify_webhook:f.get('notify_webhook'),auto_start:$('auto_start').checked,keep_keys:!keys})});renderConfig(d.config);$('stream_keys').value='';notice(d.message||'تم حفظ الإعدادات')}catch(e){notice(e.message,'error')}});
$('testSourceBtn').onclick=async()=>{let b=$('testSourceBtn');b.disabled=true;b.textContent='جارٍ الاختبار…';try{let d=await api('/api/test-source',{method:'POST',body:'{}'});notice(`المصدر صالح: ${d.title||'بدون عنوان'} — ${d.mode}`)}catch(e){notice(e.message,'error')}finally{b.disabled=false;b.textContent='اختبار المصدر'}};
$('startBtn').onclick=async()=>{try{let d=await api('/api/start',{method:'POST',body:'{}'});notice(d.message);refresh()}catch(e){notice(e.message,'error')}};$('stopBtn').onclick=async()=>{try{let d=await api('/api/stop',{method:'POST',body:'{}'});notice(d.message);refresh()}catch(e){notice(e.message,'error')}};$('restartBtn').onclick=async()=>{try{let d=await api('/api/restart',{method:'POST',body:'{}'});notice(d.message);refresh()}catch(e){notice(e.message,'error')}};$('clearKeysBtn').onclick=async()=>{if(!confirm('حذف جميع مفاتيح البث المحفوظة؟'))return;try{let d=await api('/api/config',{method:'POST',body:JSON.stringify({clear_keys:true})});renderConfig(d.config);notice('تم حذف المفاتيح')}catch(e){notice(e.message,'error')}};$('clearLogBtn').onclick=()=>{$('logs').textContent=''};
$('refreshSourceBtn').onclick=async()=>{try{let d=await api('/api/source/refresh',{method:'POST',body:'{}'});notice(d.message);refresh()}catch(e){notice(e.message,'error')}};$('breakStartBtn').onclick=async()=>{try{let d=await api('/api/break',{method:'POST',body:JSON.stringify({action:'start'})});notice(d.message);refresh()}catch(e){notice(e.message,'error')}};$('breakStopBtn').onclick=async()=>{try{let d=await api('/api/break',{method:'POST',body:JSON.stringify({action:'stop'})});notice(d.message);refresh()}catch(e){notice(e.message,'error')}};
$('testWebhookBtn').onclick=async()=>{try{let d=await api('/api/webhook/test',{method:'POST',body:'{}'});notice(d.message)}catch(e){notice(e.message,'error')}};
$('applyLogLevelBtn').onclick=async()=>{try{let d=await api('/api/log-level',{method:'POST',body:JSON.stringify({level:$('log_level').value})});notice(d.message)}catch(e){notice(e.message,'error')}};
$('changeTokenBtn').onclick=async()=>{let nt=$('new_token').value.trim();if(nt.length<6){notice('الرمز الجديد يجب أن يكون 6 أحرف على الأقل','error');return}try{let d=await api('/api/token',{method:'POST',body:JSON.stringify({current:$('current_token').value,new:nt})});panelToken=nt;localStorage.setItem('panelToken',nt);$('current_token').value='';$('new_token').value='';notice(d.message)}catch(e){notice(e.message,'error')}};
$('downloadLogBtn').onclick=async()=>{try{let headers={};if(panelToken)headers['X-Panel-Token']=panelToken;let r=await fetch('/api/logs/download',{headers});if(!r.ok)throw new Error('تعذر تنزيل السجل');let blob=await r.blob(),a=document.createElement('a');a.href=URL.createObjectURL(blob);a.download='relay.log';a.click();URL.revokeObjectURL(a.href)}catch(e){notice(e.message,'error')}};
$('restartAppBtn').onclick=async()=>{if(!confirm('إعادة تشغيل التطبيق بالكامل؟ سيتوقف البث ويعود حسب إعداد التشغيل التلقائي.'))return;try{await api('/api/app/restart',{method:'POST',body:'{}'});notice('جارٍ إعادة التشغيل… سيتم تحديث الصفحة تلقائياً');setTimeout(()=>location.reload(),6000)}catch(e){notice(e.message,'error')}};
(async()=>{try{let c=await api('/api/config');renderConfig(c.config);await refresh();setInterval(refresh,3000)}catch(e){notice(e.message,'error')}})();
</script>
</body></html>'''


class AppHandler(BaseHTTPRequestHandler):
    server_version = "LiveRelay/6.0"
    _test_source_last: dict[str, float] = {}
    _test_source_lock = threading.Lock()

    def log_message(self, format: str, *args: Any) -> None:
        LOGGER.info("web %s", format % args)

    def authorized(self) -> bool:
        if not PANEL_TOKEN:
            return True
        header_token = self.headers.get("X-Panel-Token", "")
        auth_header = self.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            header_token = auth_header[7:]
        return hmac.compare_digest(header_token, PANEL_TOKEN)

    def require_auth(self) -> bool:
        if self.authorized():
            return True
        self.send_json({"message": "رمز الدخول مطلوب أو غير صحيح", "auth_required": True}, 401)
        return False

    def same_origin_ok(self) -> bool:
        """حماية CSRF عند غياب رمز الدخول: رفض الطلبات القادمة من مواقع أخرى."""
        fetch_site = (self.headers.get("Sec-Fetch-Site") or "").lower()
        if fetch_site and fetch_site not in {"same-origin", "same-site", "none"}:
            return False
        origin = self.headers.get("Origin") or ""
        if origin:
            origin_host = urlparse(origin).netloc.lower()
            host = (self.headers.get("Host") or "").lower()
            if origin_host and host and origin_host != host:
                return False
        return True

    def send_security_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")

    def send_json(self, data: dict[str, Any], status: int = 200) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_security_headers()
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length > 1_000_000:
            raise ValueError("الطلب كبير جداً")
        raw = self.rfile.read(length) if length else b"{}"
        data = json.loads(raw.decode("utf-8"))
        if not isinstance(data, dict):
            raise ValueError("صيغة الطلب غير صحيحة")
        return data

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        if path in {"/", "/index.html"}:
            body = HTML.replace("__PORT__", str(SERVER_PORT)).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src 'self' data:")
            self.send_security_headers()
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/healthz":
            self.send_json({"ok": True, "running": MANAGER.is_running()})
            return
        if path.startswith("/api/") and not self.require_auth():
            return
        if path == "/api/status":
            self.send_json(MANAGER.snapshot())
            return
        if path == "/api/config":
            self.send_json({"config": public_config(MANAGER.config)})
            return
        if path == "/api/logs":
            try:
                limit = max(10, min(600, int(parse_qs(parsed.query).get("limit", ["150"])[0])))
            except (TypeError, ValueError):
                limit = 150
            self.send_json({"lines": LOG_BUFFER.snapshot(limit)})
            return
        if path == "/api/logs/download":
            log_file = DATA_DIR / "relay.log"
            body = log_file.read_bytes() if log_file.exists() else "لا يوجد سجل محفوظ بعد.".encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Disposition", "attachment; filename=relay.log")
            self.send_security_headers()
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_json({"message": "غير موجود"}, 404)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if not self.same_origin_ok():
            self.send_json({"message": "طلب مرفوض من مصدر خارجي"}, 403)
            return
        if path.startswith("/api/") and not self.require_auth():
            return
        try:
            data = self.read_json()
            if path == "/api/config":
                if data.get("clear_keys"):
                    MANAGER.config.stream_keys = []
                else:
                    keep_keys = bool(data.get("keep_keys"))
                    incoming = data.get("stream_keys", [])
                    if isinstance(incoming, str):
                        incoming = re.split(r"[\n,]+", incoming)
                    incoming = [str(key).strip() for key in incoming if str(key).strip()]
                    if incoming or not keep_keys:
                        data["stream_keys"] = incoming
                    else:
                        data["stream_keys"] = MANAGER.config.stream_keys
                    # البروكسي: فارغ أو مقنّع (•••) = إبقاء الحالي؛ clear_proxy = حذفه
                    if data.get("clear_proxy"):
                        data["proxy"] = ""
                    else:
                        incoming_proxy = str(data.get("proxy", "")).strip()
                        if not incoming_proxy or "•••" in incoming_proxy:
                            data["proxy"] = MANAGER.config.proxy
                    candidate = RelayConfig.from_dict({**asdict(MANAGER.config), **data})
                    errors = candidate.validate()
                    if errors:
                        self.send_json({"message": errors[0], "errors": errors}, 400)
                        return
                    MANAGER.config = candidate
                MANAGER.config.save()
                MANAGER.resolver.invalidate()
                message = "تم حفظ الإعدادات بأمان"
                if MANAGER.is_running():
                    MANAGER.restart()
                    message = "تم حفظ الإعدادات وإعادة تشغيل البث لتطبيقها فوراً"
                self.send_json({"message": message, "config": public_config(MANAGER.config)})
                return
            if path == "/api/start":
                ok, message = MANAGER.start()
                self.send_json({"ok": ok, "message": message}, 200 if ok else 400)
                return
            if path == "/api/stop":
                ok, message = MANAGER.stop()
                self.send_json({"ok": ok, "message": message}, 200 if ok else 400)
                return
            if path == "/api/restart":
                ok, message = MANAGER.restart()
                self.send_json({"ok": ok, "message": message}, 200 if ok else 400)
                return
            if path == "/api/test-source":
                client_ip = self.client_address[0] if self.client_address else "?"
                with AppHandler._test_source_lock:
                    last_test = AppHandler._test_source_last.get(client_ip, 0.0)
                    if time.time() - last_test < 10:
                        self.send_json({"ok": False, "message": "اختبار المصدر متاح مرة كل 10 ثوانٍ"}, 429)
                        return
                    AppHandler._test_source_last[client_ip] = time.time()
                source = select_source(MANAGER.config)
                if not source:
                    self.send_json({"ok": False, "message": "لم أجد بثاً مباشراً صالحاً في المصدر الرئيسي أو الاحتياطي"}, 400)
                    return
                self.send_json({"ok": True, "title": source.title, "mode": source.mode, "source_url": source.source_url, "video_codec": source.video_codec, "audio_codec": source.audio_codec})
                return
            if path == "/api/worker/start":
                ok, message = MANAGER.start_one(str(data.get("name", "")))
                self.send_json({"ok": ok, "message": message}, 200 if ok else 400)
                return
            if path == "/api/worker/stop":
                ok, message = MANAGER.stop_one(str(data.get("name", "")))
                self.send_json({"ok": ok, "message": message}, 200 if ok else 400)
                return
            if path == "/api/break":
                ok, message = MANAGER.break_all(str(data.get("action", "")))
                self.send_json({"ok": ok, "message": message}, 200 if ok else 400)
                return
            if path == "/api/source/refresh":
                MANAGER.resolver.invalidate()
                if MANAGER.is_running():
                    MANAGER.restart()
                    message = "تم تحديث المصدر وإعادة الاتصال بكل الوجهات العاملة"
                else:
                    message = "تم مسح كاش المصدر — سيُحل من جديد عند التشغيل"
                self.send_json({"ok": True, "message": message})
                return
            if path == "/api/webhook/test":
                if not MANAGER.config.notify_webhook:
                    self.send_json({"ok": False, "message": "أدخل رابط إشعارات Webhook واحفظ الإعدادات أولاً"}, 400)
                    return
                MANAGER.notify("test", "🔔 إشعار تجريبي من لوحة تحكم البث المباشر")
                self.send_json({"ok": True, "message": "تم إرسال إشعار تجريبي — تحقق من وجهة Webhook"})
                return
            if path == "/api/token":
                global PANEL_TOKEN
                current = str(data.get("current", ""))
                new_token = str(data.get("new", "")).strip()
                if len(new_token) < 6:
                    self.send_json({"ok": False, "message": "الرمز الجديد يجب أن يكون 6 أحرف على الأقل"}, 400)
                    return
                if PANEL_TOKEN and not hmac.compare_digest(current, PANEL_TOKEN):
                    self.send_json({"ok": False, "message": "الرمز الحالي غير صحيح"}, 403)
                    return
                PANEL_TOKEN = new_token
                MANAGER.config.panel_token = new_token
                MANAGER.config.save()
                LOGGER.info("تم تغيير رمز لوحة التحكم من الواجهة")
                self.send_json({"ok": True, "message": "تم تغيير رمز اللوحة وحفظه — استخدمه في تسجيل الدخول القادم"})
                return
            if path == "/api/log-level":
                level = str(data.get("level", "")).strip().upper()
                if level not in {"DEBUG", "INFO", "WARNING", "ERROR"}:
                    self.send_json({"ok": False, "message": "مستوى سجل غير صالح"}, 400)
                    return
                LOGGER.setLevel(level)
                MANAGER.config.log_level = level
                MANAGER.config.save()
                self.send_json({"ok": True, "message": f"تم ضبط مستوى السجل على {level}"})
                return
            if path == "/api/app/restart":
                self.send_json({"ok": True, "message": "جارٍ إعادة تشغيل التطبيق — أعد فتح الصفحة بعد ثوانٍ"})
                def restart_app() -> None:
                    time.sleep(1)
                    LOGGER.info("إعادة تشغيل التطبيق بطلب من الواجهة")
                    MANAGER.stop()
                    os.execv(sys.executable, [sys.executable, os.path.abspath(__file__), *sys.argv[1:]])
                threading.Thread(target=restart_app, daemon=True).start()
                return
            self.send_json({"message": "غير موجود"}, 404)
        except Exception as exc:
            LOGGER.exception("خطأ في طلب الويب")
            self.send_json({"message": str(exc)}, 400)


def is_port_available(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, port))
            return True
        except OSError:
            return False


MANAGER: RelayManager
SERVER_PORT = 7861


def check_rtmp_reachable(rtmp_base: str, timeout: float = 6.0) -> tuple[bool, str]:
    """فحص اتصال TCP خام بخادم الوجهة — يكشف حظر جدار النار للمنفذ 1935 دون إرسال بيانات."""
    parsed = urlparse(rtmp_base)
    host = parsed.hostname or ""
    port = parsed.port or 1935
    if not host:
        return False, "عنوان RTMP بلا مضيف"
    try:
        with socket.create_connection((host, port), timeout=timeout):
            pass
        return True, f"الاتصال بـ {host}:{port} يعمل"
    except OSError as exc:
        return False, f"تعذر الاتصال بـ {host}:{port} ({exc}) — افحص جدار النار/المنفذ 1935"


def probe_media_flow(source: SourceSelection, config: RelayConfig, seconds: float = 10.0) -> dict[str, Any]:
    """تشغيل ffmpeg فعلياً على مداخل المصدر لثوانٍ وقياس الإطارات/البيانات الواصلة.

    هذا هو الفحص الحاسم لرسالة «لا تتوفّر أي بيانات» في يوتيوب:
    يثبت هل يستطيع الخادم فعلياً جلب مقاطع الفيديو/الصوت من CDN أم أنها محجوبة.
    """
    result: dict[str, Any] = {"frames": 0, "out_time_us": 0, "http_403": 0, "errors": [], "ran": False}
    if not FFMPEG:
        result["errors"].append("ffmpeg غير جاهز")
        return result
    command = [FFMPEG, "-hide_banner", "-loglevel", "warning", "-nostdin", "-nostats", "-progress", "pipe:1", "-stats_period", "1"]
    if source.mode == "split":
        command.extend(input_options(source.video_url, source.video_headers))
        command.extend(input_options(source.audio_url or "", source.audio_headers))
        command.extend(["-map", "0:v:0", "-map", "1:a:0"])
    else:
        command.extend(input_options(source.video_url, source.video_headers))
        if source.mode == "video-only":
            command.extend(["-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100"])
            command.extend(["-map", "0:v:0", "-map", "1:a:0"])
        else:
            command.extend(["-map", "0:v:0", "-map", "0:a:0?"])
    command.extend(["-c", "copy", "-t", str(int(seconds)), "-f", "null", "-"])
    creation_kwargs: dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.STDOUT,
        "text": True,
        "bufsize": 1,
    }
    if os.name != "nt":
        creation_kwargs["start_new_session"] = True
    env = proxy_env(config)
    if env is not None:
        creation_kwargs["env"] = env
    try:
        proc = subprocess.Popen(command, **creation_kwargs)
    except OSError as exc:
        result["errors"].append(str(exc))
        return result
    # حارس زمني: إن صمت ffmpeg تماماً (مقاطع معلّقة) ننهيه ونُرجع صفر إطار
    watchdog = threading.Timer(seconds + 20, terminate_process, args=(proc,))
    watchdog.daemon = True
    watchdog.start()
    started = time.time()
    frames = 0
    out_us = 0
    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            text = line.strip()
            if not text:
                continue
            if "=" in text and not text.startswith(" "):
                key, _, value = text.partition("=")
                if key == "frame":
                    frames = int(number(value, 0))
                elif key in {"out_time_us", "out_time_ms"}:
                    out_us = int(number(value, 0))
                continue
            if "403" in text and "orbidden" in text:
                result["http_403"] += 1
            elif any(token in text for token in ("error", "Error", "Invalid", "refused", "timed out")):
                if len(result["errors"]) < 4:
                    result["errors"].append(text[:160])
            if time.time() - started > seconds + 12:
                break
    finally:
        watchdog.cancel()
        terminate_process(proc)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
    result["frames"] = frames
    result["out_time_us"] = out_us
    result["ran"] = True
    return result


def run_check(config: RelayConfig) -> int:
    """فحص سريع بدون تشغيل الخادم: الإعدادات، ffmpeg، الوصول إلى RTMP، المصدر، وتدفق الوسائط فعلياً."""
    ok = True
    print("— فحص الإعدادات —")
    errors = config.validate()
    if errors:
        ok = False
        for error in errors:
            print(f"  ✗ {error}")
    else:
        print("  ✓ الإعدادات صالحة")
    print(f"  • المصدر: {config.source_url}")
    print(f"  • الوجهة: {config.rtmp_base} — عدد المفاتيح: {len(config.stream_keys)}")
    if not config.stream_keys:
        print("  ⚠ لا توجد مفاتيح بث محفوظة")
    if config.proxy:
        print(f"  • البروكسي: {mask_proxy(config.proxy)}")
    print("— فحص ffmpeg —")
    print(f"  ✓ {FFMPEG}")
    print("— فحص الوصول إلى خادم الوجهة (TCP) —")
    reachable, message = check_rtmp_reachable(config.rtmp_base)
    print(f"  {'✓' if reachable else '✗'} {message}")
    if not reachable:
        ok = False
    print("— فحص المصدر (قد يستغرق ثواني) —")
    source = select_source(config)
    if not source:
        ok = False
        print("  ✗ لم يتم العثور على بث مباشر صالح في المصدر الرئيسي أو الاحتياطي")
        print("  → إن كان البث شغالاً في المتصفح فقد تحتاج Cookies أو بروكسي")
        return 0 if ok else 1
    print(f"  ✓ بث صالح: {source.title or 'بدون عنوان'} — الوضع: {source.mode}")
    if source.mode == "split":
        print("  ✓ مسار الصوت موجود (split)")
    elif source.mode == "muxed":
        print("  ✓ تنسيق مدمج يحمل صوتاً (muxed)")
    else:
        print("  ⚠ المصدر بلا مسار صوت — سيُضاف صوت صامت")
    print("— فحص تدفق الوسائط فعلياً (≈10 ثوانٍ) —")
    probe = probe_media_flow(source, config)
    if probe["frames"] > 0:
        print(f"  ✓ وصلت {probe['frames']} إطاراً خلال الفحص — الخادم يجلب مقاطع الفيديو فعلياً")
    else:
        ok = False
        if probe["http_403"] >= 2:
            print("  ✗ مقاطع يوتيوب ترفض الخادم بـ 403 — يوتيوب تحجب عنوان IP لهذا الخادم")
            print("  → الحلول: فعّل بروكسي (حقل «البروكسي» في اللوحة)، أو بدّل IP/الاستضافة، أو جرّب ملف Cookies")
        else:
            print("  ✗ لم يصل أي إطار خلال الفحص رغم صلاحية المصدر")
            print("  → افحص رسائل الخطأ أعلاه، أو جرّب بروكسي، أو تحقق من سرعة الشبكة")
        for err in probe["errors"]:
            print(f"    • {err}")
    return 0 if ok else 1


def main() -> int:
    global FFMPEG, MANAGER, SERVER_PORT, PANEL_TOKEN
    parser = argparse.ArgumentParser(description="لوحة وإعادة إرسال بث مباشر تلقائية")
    parser.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "7861")))
    parser.add_argument("--token", default=os.environ.get("PANEL_TOKEN", ""), help="رمز حماية لوحة التحكم (أو المتغير PANEL_TOKEN)")
    parser.add_argument("--auto-start", action="store_true")
    parser.add_argument("--check", action="store_true", help="فحص الإعدادات والمصدر ثم الخروج بدون تشغيل الخادم")
    args = parser.parse_args()
    PANEL_TOKEN = args.token.strip()
    logging.basicConfig(level=logging.INFO)
    LOGGER.setLevel(os.environ.get("LOG_LEVEL", "INFO").upper())
    try:
        FFMPEG = resolve_ffmpeg()
    except Exception as exc:
        LOGGER.error(str(exc))
        return 1
    config = RelayConfig.load()
    if args.check:
        return run_check(config)
    if not args.token.strip() and config.panel_token:
        PANEL_TOKEN = config.panel_token
    if "LOG_LEVEL" not in os.environ and config.log_level:
        LOGGER.setLevel(config.log_level.upper())
    if not is_port_available(args.host, args.port):
        LOGGER.error("المنفذ %d مشغول على %s. أوقف العملية الأخرى أو اختر منفذاً آخر عبر --port", args.port, args.host)
        return 1
    if args.auto_start:
        config.auto_start = True
    MANAGER = RelayManager(config)
    SERVER_PORT = args.port
    server = ThreadingHTTPServer((args.host, args.port), AppHandler)
    server.daemon_threads = True
    LOGGER.info("لوحة التحكم: http://%s:%d", args.host, args.port)
    LOGGER.info("ffmpeg: %s", FFMPEG)
    if PANEL_TOKEN:
        LOGGER.info("حماية اللوحة مفعّلة برمز الدخول")
    elif args.host not in {"127.0.0.1", "localhost", "::1"}:
        LOGGER.warning("اللوحة مفتوحة على %s بدون رمز حماية — فعّل --token أو المتغير PANEL_TOKEN", args.host)
    if config.auto_start and config.stream_keys:
        MANAGER.start()
    def stop_server(*_args: Any) -> None:
        LOGGER.info("إغلاق التطبيق")
        MANAGER.stop()
        threading.Thread(target=server.shutdown, daemon=True).start()
    signal.signal(signal.SIGINT, stop_server)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, stop_server)
    try:
        server.serve_forever()
    finally:
        server.server_close()
        MANAGER.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
