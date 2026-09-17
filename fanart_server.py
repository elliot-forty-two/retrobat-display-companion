import json
import os
import time
import hashlib
import threading
import configparser
from dataclasses import dataclass
from pathlib import Path
from datetime import datetime
from PIL import Image

from image_compositor import cached_composite_path, make_composite_and_show

# -------------------- CONFIG --------------------
ES_EVENT_PIPE_NAME = r"\\.\pipe\EmulationStation.Events"

BACKGLASS_PIPE_NAME = r"\\.\pipe\retrobat_backglass"
DMD_PIPE_NAME = r"\\.\pipe\retrobat_dmd"

RETROBAT_ROOT = Path(r"C:\RetroBat")
IMAGE_EXTS = [".png", ".jpg", ".jpeg", ".webp"]
VIDEO_EXTS = [".mp4", ".mkv", ".avi", ".mov", ".webm", ".m4v"]

BASE_DIR = Path(__file__).resolve().parent
LOG_FILE = BASE_DIR / "fanart_debug.log"
FALLBACK_IMAGE = BASE_DIR / "fallback.png"
VIEWER_CONFIG_FILE = BASE_DIR / "fanart_server.ini"
SYSTEM_MEDIA_DIR = BASE_DIR / "systems"

DMD_CACHE_VERSION = "dmd_center_v2"

class ViewerShutdown(Exception):
    pass

def _load_viewer_setting(name: str, default: str, allowed: tuple[str, ...]) -> str:
    value = default
    config = configparser.ConfigParser()
    if VIEWER_CONFIG_FILE.exists():
        try:
            config.read(VIEWER_CONFIG_FILE, encoding="utf-8")
            section = config["BackglassViewer"] if "BackglassViewer" in config else {}
            value = str(section.get(name, default)).strip().lower()
        except Exception:
            value = default

    if value not in allowed:
        return default
    return value


def _load_int_setting(name: str, default: int, min_value: int, max_value: int) -> int:
    value = default
    config = configparser.ConfigParser()
    if VIEWER_CONFIG_FILE.exists():
        try:
            config.read(VIEWER_CONFIG_FILE, encoding="utf-8")
            section = config["BackglassViewer"] if "BackglassViewer" in config else {}
            raw = section.get(name, default)
            value = int(str(raw).strip())
        except Exception:
            value = default
    return max(min_value, min(max_value, value))


def _load_float_setting(name: str, default: float, min_value: float, max_value: float) -> float:
    value = default
    config = configparser.ConfigParser()
    if VIEWER_CONFIG_FILE.exists():
        try:
            config.read(VIEWER_CONFIG_FILE, encoding="utf-8")
            section = config["BackglassViewer"] if "BackglassViewer" in config else {}
            raw = section.get(name, default)
            value = float(str(raw).strip())
        except Exception:
            value = default
    return max(min_value, min(max_value, value))

def _load_csv_setting(name: str, default: str) -> set[str]:
    value = default
    config = configparser.ConfigParser()
    if VIEWER_CONFIG_FILE.exists():
        try:
            config.read(VIEWER_CONFIG_FILE, encoding="utf-8")
            section = config["BackglassViewer"] if "BackglassViewer" in config else {}
            value = str(section.get(name, default))
        except Exception:
            value = default
    return {
        item.strip().lower()
        for item in value.split(",")
        if item.strip()
    }


BACKGLASS_SCREEN_MODE = _load_viewer_setting(
    "BackglassScreenMode",
    "auto",
    ("auto", "fanart", "backglass"),
)

DMD_SCREEN_MODE = _load_viewer_setting(
    "DmdScreenMode",
    "logo",
    ("logo", "marquee"),
)

DMD_TARGET_W = _load_int_setting("DmdWidth", 1920, 320, 8192)
DMD_TARGET_H = _load_int_setting("DmdHeight", 480, 120, 4096)
DMD_LOGO_SCALE = _load_float_setting("DmdLogoScale", 1.0, 0.1, 1.0)
BLANK_ON_GAME_START_SYSTEMS = _load_csv_setting(
    "BlankOnGameStartSystems",
    "vpinball,futurepinball",
)

# Performance/debug knobs
DEBUG_TIMINGS = True          # write detailed event/timing logs to fanart_debug.log
SLOW_MS = 75.0                # flag individual operations slower than this
# ------------------------------------------------


def _now_ms() -> float:
    return time.perf_counter() * 1000.0


def _elapsed_ms(start_ms: float) -> float:
    return _now_ms() - start_ms


def _fmt_ms(value) -> str:
    return "n/a" if value is None else f"{value:.1f}ms"


def _short_path(p) -> str:
    if not p:
        return ""
    try:
        return str(p)
    except Exception:
        return repr(p)


def log(msg: str):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"[{ts}] {msg}\n")
    except Exception:
        pass


def debug(msg: str):
    if DEBUG_TIMINGS:
        log(msg)


def send_mpv(cmd_obj: dict, pipe_name: str = BACKGLASS_PIPE_NAME, timeout_s: float = 1.5, event_id: str = "") -> bool:
    start_ms = _now_ms()
    deadline = time.time() + timeout_s
    last_err = None
    attempts = 0
    payload = json.dumps(cmd_obj)
    cmd = cmd_obj.get("command", ["?"])[0] if isinstance(cmd_obj, dict) else "?"

    while time.time() < deadline:
        attempts += 1
        try:
            with open(pipe_name, "r+", encoding="utf-8") as pipe:
                pipe.write(payload + "\n")
                pipe.flush()
                elapsed = _elapsed_ms(start_ms)
                if DEBUG_TIMINGS or elapsed >= SLOW_MS or attempts > 1:
                    debug(f"{event_id} mpv cmd={cmd!r} pipe={pipe_name} ok elapsed={elapsed:.1f}ms attempts={attempts}")
                return True
        except OSError as e:
            last_err = e
            time.sleep(0.02)

    elapsed = _elapsed_ms(start_ms)
    log(f"{event_id} ERROR: could not connect to mpv IPC pipe {pipe_name}: {last_err}; elapsed={elapsed:.1f}ms attempts={attempts} cmd={cmd!r}")
    return False


def show_in_mpv(path: str, pipe_name: str = BACKGLASS_PIPE_NAME, event_id: str = ""):
    global _last_loaded_by_pipe
    start_ms = _now_ms()
    path = (path or "").strip().strip('"')
    if not path or not os.path.isfile(path):
        debug(f"{event_id} show_in_mpv missing path={path!r}; sending stop to {pipe_name}")
        blank_mpv(pipe_name, event_id=event_id)
        return

    is_video = Path(path).suffix.lower() in VIDEO_EXTS
    last_path = _last_loaded_by_pipe.get(pipe_name)
    if last_path == path:
        debug(f"{event_id} show_in_mpv skip unchanged pipe={pipe_name} path={path}")
        return

    debug(f"{event_id} show_in_mpv start pipe={pipe_name} video={is_video} path={path}")
    keepaspect = not is_video
    if _last_keepaspect_by_pipe.get(pipe_name) != keepaspect:
        if send_mpv({"command": ["set_property", "keepaspect", keepaspect]}, pipe_name=pipe_name, event_id=event_id):
            _last_keepaspect_by_pipe[pipe_name] = keepaspect
    else:
        debug(f"{event_id} mpv keepaspect skip unchanged pipe={pipe_name} value={keepaspect}")

    ok = send_mpv({"command": ["loadfile", path, "replace"]}, pipe_name=pipe_name, event_id=event_id)
    if ok:
        _last_loaded_by_pipe[pipe_name] = path
        if not _fullscreen_set_by_pipe.get(pipe_name):
            try:
                if send_mpv({"command": ["set_property", "fullscreen", True]}, pipe_name=pipe_name, event_id=event_id):
                    _fullscreen_set_by_pipe[pipe_name] = True
            except Exception as e:
                log(f"{event_id} mpv fullscreen set failed: {type(e).__name__}: {e}")
        else:
            debug(f"{event_id} mpv fullscreen skip already set pipe={pipe_name}")
    debug(f"{event_id} show_in_mpv done elapsed={_elapsed_ms(start_ms):.1f}ms pipe={pipe_name}")


def blank_mpv(pipe_name: str, event_id: str = ""):
    global _last_loaded_by_pipe
    if _last_loaded_by_pipe.get(pipe_name) == "":
        debug(f"{event_id} blank_mpv skip already blank pipe={pipe_name}")
        return
    debug(f"{event_id} blank_mpv pipe={pipe_name}")
    if send_mpv({"command": ["stop"]}, pipe_name=pipe_name, event_id=event_id):
        _last_loaded_by_pipe[pipe_name] = ""


def show_media_or_blank(path: Path | None, pipe_name: str, event_id: str = "", blank_missing: bool = True) -> bool:
    if path and os.path.isfile(path):
        show_in_mpv(str(path), pipe_name=pipe_name, event_id=event_id)
        return True
    if blank_missing:
        blank_mpv(pipe_name, event_id=event_id)
    else:
        debug(f"{event_id} no media for pipe={pipe_name}; keeping current display")
    return False


def is_video_path(path: Path | None) -> bool:
    return bool(path and path.suffix.lower() in VIDEO_EXTS)


def file_sig(path: str) -> str:
    try:
        st = os.stat(path)
        m = getattr(st, "st_mtime_ns", int(st.st_mtime * 1e9))
        return f"{st.st_size}:{m}"
    except Exception:
        return "missing"


def centered_dmd_asset_path(src_path: Path, event_id: str = "") -> Path | None:
    if not src_path or not src_path.exists():
        return None

    cache_dir = BASE_DIR / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)

    key_src = f"{src_path}|{file_sig(str(src_path))}|{DMD_TARGET_W}x{DMD_TARGET_H}|scale={DMD_LOGO_SCALE:.3f}|{DMD_CACHE_VERSION}"
    key = hashlib.sha1(key_src.encode("utf-8", errors="ignore")).hexdigest()
    out_path = cache_dir / f"dmd_center_{key}.png"
    if out_path.exists() and out_path.stat().st_size > 0:
        debug(f"{event_id} dmd center cache_hit path={out_path}")
        return out_path

    try:
        with Image.open(src_path) as src:
            src_rgba = src.convert("RGBA")
            iw, ih = src_rgba.size
            if iw <= 0 or ih <= 0:
                return None

            fit_w = max(1, int(DMD_TARGET_W * DMD_LOGO_SCALE))
            fit_h = max(1, int(DMD_TARGET_H * DMD_LOGO_SCALE))
            scale = min(fit_w / float(iw), fit_h / float(ih))
            nw = max(1, int(iw * scale))
            nh = max(1, int(ih * scale))
            logo = src_rgba.resize((nw, nh), Image.LANCZOS)

            canvas = Image.new("RGB", (DMD_TARGET_W, DMD_TARGET_H), (0, 0, 0))
            x = (DMD_TARGET_W - nw) // 2
            y = (DMD_TARGET_H - nh) // 2
            canvas.paste(logo, (x, y), logo)
            canvas.save(out_path, format="PNG")
            debug(f"{event_id} dmd center rendered path={out_path}")
            return out_path
    except Exception as e:
        log(f"DMD centering failed: {type(e).__name__}: {e}")
        return None


def show_dmd_media_or_blank(path: Path | None, event_id: str = "", blank_missing: bool = True) -> bool:
    if DMD_SCREEN_MODE == "logo" and path and os.path.isfile(path) and not is_video_path(path):
        centered = centered_dmd_asset_path(path, event_id=event_id)
        if centered and centered.exists() and centered.stat().st_size > 0:
            show_in_mpv(str(centered), pipe_name=DMD_PIPE_NAME, event_id=event_id)
            return True
    return show_media_or_blank(path, DMD_PIPE_NAME, event_id=event_id, blank_missing=blank_missing)


def normalize_rom_path(p: str) -> Path:
    p = (p or "").strip().strip('"').replace("/", "\\")
    return Path(p)


def find_media_file(media_dir: Path, rom_stem: str, suffix: str, exts: list[str] | tuple[str, ...] = IMAGE_EXTS) -> Path | None:
    for ext in exts:
        p = media_dir / f"{rom_stem}-{suffix}{ext}"
        if p.exists():
            return p
    return None


def find_media_file_stem(media_dir: Path, rom_stem: str, exts: list[str] | tuple[str, ...]) -> Path | None:
    for ext in exts:
        p = media_dir / f"{rom_stem}{ext}"
        if p.exists():
            return p
    return None


def find_media_file_any(media_dir: Path, rom_stem: str, suffixes: tuple[str, ...], exts: list[str] | tuple[str, ...] = IMAGE_EXTS) -> Path | None:
    for suffix in suffixes:
        media_file = find_media_file(media_dir, rom_stem, suffix, exts=exts)
        if media_file:
            return media_file
    return None


def find_media_across_dirs(media_dirs: list[Path], rom_stem: str, suffix: str, exts: list[str] | tuple[str, ...]) -> Path | None:
    for media_dir in media_dirs:
        media_file = find_media_file(media_dir, rom_stem, suffix, exts=exts)
        if media_file:
            return media_file
    return None


def find_media_any_across_dirs(media_dirs: list[Path], rom_stem: str, suffixes: tuple[str, ...], exts: list[str] | tuple[str, ...]) -> Path | None:
    for media_dir in media_dirs:
        media_file = find_media_file_any(media_dir, rom_stem, suffixes, exts=exts)
        if media_file:
            return media_file
    return None


def _find_system_asset(system: str, names: tuple[str, ...]) -> Path | None:
    """Find BackglassViewer-owned artwork for a system.

    Preferred layout:
        systems/<system>/backglass.png
        systems/<system>/fanart.png
        systems/<system>/marquee.png
        systems/<system>/logo.png

    A flat layout such as systems/<system>-marquee.png is also accepted.
    """
    system = (system or "").strip().strip('"')
    if not system:
        return None

    system_dir = SYSTEM_MEDIA_DIR / system

    for name in names:
        for ext in IMAGE_EXTS:
            nested = system_dir / f"{name}{ext}"
            if nested.exists():
                return nested

            flat = SYSTEM_MEDIA_DIR / f"{system}-{name}{ext}"
            if flat.exists():
                return flat

    return None

def find_system_media(system: str, event_id: str = ""):
    start_ms = _now_ms()

    fanart = _find_system_asset(system, ("fanart", "background"))
    marquee = _find_system_asset(system, ("marquee", "logo"))
    backglass = _find_system_asset(system, ("backglass",))
    logo = _find_system_asset(system, ("logo", "marquee"))

    debug(
        f"{event_id} system_media_lookup elapsed={_elapsed_ms(start_ms):.1f}ms "
        f"system={system!r} fanart={_short_path(fanart)} "
        f"marquee={_short_path(marquee)} backglass={_short_path(backglass)} "
        f"logo={_short_path(logo)}"
    )

    return fanart, marquee, backglass, logo, None


def find_media_files(system: str, rom_path: Path, event_id: str = ""):
    start_ms = _now_ms()
    rom_name = rom_path.stem
    system = (system or "").strip().strip('"')
    images_dir = RETROBAT_ROOT / "roms" / system / "images"
    backglass_video_dirs = [
        RETROBAT_ROOT / "roms" / system / "video" / "Backglass",
        RETROBAT_ROOT / "roms" / system / "videos" / "Backglass",
    ]
    dmd_video_dirs = [
        RETROBAT_ROOT / "roms" / system / "video" / "DMD",
        RETROBAT_ROOT / "roms" / system / "videos" / "DMD",
    ]

    fanart = find_media_file(images_dir, rom_name, "fanart")
    marquee = find_media_file(images_dir, rom_name, "marquee", exts=IMAGE_EXTS)
    backglass = find_media_file(images_dir, rom_name, "backglass", exts=IMAGE_EXTS)
    logo = find_media_file_any(images_dir, rom_name, ("logo", "marquee-topper", "topper"), exts=IMAGE_EXTS)

    backglass_video = None
    for media_dir in backglass_video_dirs:
        backglass_video = find_media_file_stem(media_dir, rom_name, VIDEO_EXTS)
        if backglass_video:
            break

    dmd_video = None
    for media_dir in dmd_video_dirs:
        dmd_video = find_media_file_stem(media_dir, rom_name, VIDEO_EXTS)
        if dmd_video:
            break

    if backglass_video:
        backglass = backglass_video

    elapsed = _elapsed_ms(start_ms)
    debug(f"{event_id} media_lookup elapsed={elapsed:.1f}ms system={system!r} rom={rom_name!r} fanart={_short_path(fanart)} marquee={_short_path(marquee)} backglass={_short_path(backglass)} logo={_short_path(logo)} dmd_video={_short_path(dmd_video)}")
    return fanart, marquee, backglass, logo, dmd_video


def show_selected_media(fanart: Path | None, marquee: Path | None, backglass: Path | None, logo: Path | None, dmd_video: Path | None, event_id: str = "", should_continue=None) -> bool:
    """Pick media and display it quickly.

    Backglass handling is intentionally simple:
    - real backglass media wins in auto/backglass mode
    - otherwise fanart is shown through the compositor so it is smart-fit
    - if a composite is cached, it is loaded immediately
    - if not cached, it is rendered synchronously once, but the final mpv load is
      guarded so an older job cannot overwrite a newer selection
    """
    start_ms = _now_ms()

    def alive() -> bool:
        return True if should_continue is None else bool(should_continue())

    def show_smart_fanart(use_marquee: bool) -> bool:
        if not fanart or not os.path.isfile(fanart):
            return False
        overlay = str(marquee) if use_marquee and marquee and os.path.isfile(marquee) else None
        try:
            cached = cached_composite_path(str(fanart), overlay)
            if cached.exists() and cached.stat().st_size > 0:
                if alive():
                    debug(f"{event_id} fanart composite cache_hit path={cached}")
                    show_in_mpv(str(cached), pipe_name=BACKGLASS_PIPE_NAME, event_id=event_id)
                    return True
                debug(f"{event_id} fanart composite cache_hit ignored; superseded")
                return False

            if not alive():
                debug(f"{event_id} fanart composite render skipped; superseded")
                return False

            render_start_ms = _now_ms()
            debug(f"{event_id} fanart composite render start fanart={fanart} marquee={overlay}")

            shown = {"value": False}

            def guarded_show(out_path: str):
                if not alive():
                    debug(f"{event_id} fanart composite render produced stale output; not displaying path={out_path}")
                    return
                show_in_mpv(out_path, pipe_name=BACKGLASS_PIPE_NAME, event_id=event_id)
                shown["value"] = True

            make_composite_and_show(str(fanart), overlay, guarded_show)
            debug(f"{event_id} fanart composite render done displayed={shown['value']} elapsed={_elapsed_ms(render_start_ms):.1f}ms")
            return shown["value"]
        except Exception as e:
            log(f"{event_id} fanart composite failed: {type(e).__name__}: {e}")
            if alive():
                return show_media_or_blank(fanart, BACKGLASS_PIPE_NAME, event_id=event_id, blank_missing=True)
            return False

    dmd_media = dmd_video or (logo if DMD_SCREEN_MODE == "logo" else marquee) or (marquee if DMD_SCREEN_MODE == "logo" else logo)
    dmd_handled = False
    if alive():
        dmd_handled = show_dmd_media_or_blank(dmd_media, event_id=event_id, blank_missing=True)
    else:
        debug(f"{event_id} display abort before DMD; superseded")
        return False

    if not alive():
        debug(f"{event_id} display abort after DMD; superseded")
        return dmd_handled

    bg_handled = False

    if BACKGLASS_SCREEN_MODE == "fanart":
        bg_handled = show_smart_fanart(use_marquee=False)
    elif BACKGLASS_SCREEN_MODE == "backglass":
        if backglass and os.path.isfile(backglass):
            bg_handled = show_media_or_blank(backglass, BACKGLASS_PIPE_NAME, event_id=event_id, blank_missing=True)
        else:
            bg_handled = show_smart_fanart(use_marquee=False)
    else:
        if backglass and os.path.isfile(backglass):
            bg_handled = show_media_or_blank(backglass, BACKGLASS_PIPE_NAME, event_id=event_id, blank_missing=True)
        else:
            bg_handled = show_smart_fanart(use_marquee=True)

    if not bg_handled and alive():
        blank_mpv(BACKGLASS_PIPE_NAME, event_id=event_id)

    handled = dmd_handled or bg_handled
    debug(f"{event_id} show_selected_media done handled={handled} dmd={dmd_handled} backglass={bg_handled} elapsed={_elapsed_ms(start_ms):.1f}ms")
    return handled


@dataclass
class ViewerState:
    mode: str = "browsing"
    system: str = ""
    browse_event: dict | None = None
    suspended: bool = False

_viewer_state = ViewerState()

# -------------------- Simple latest-only display handling --------------------
_lock = threading.Lock()
_job_id = 0
_last_loaded_by_pipe: dict[str, str] = {}
_last_keepaspect_by_pipe: dict[str, bool] = {}
_fullscreen_set_by_pipe: dict[str, bool] = {}

_pending_cond = threading.Condition()
_pending_msg = None
_pending_job_id = 0
_worker_started = False

def _cancel_pending_display(reason: str = ""):
    """Invalidate any in-progress/pending render before an immediate state change."""
    global _job_id, _pending_msg, _pending_job_id

    with _lock:
        _job_id += 1
        cancel_job_id = _job_id

    with _pending_cond:
        _pending_msg = None
        _pending_job_id = 0
        _pending_cond.notify_all()

    debug(f"[state] cancelled pending display job={cancel_job_id} reason={reason!r}")

def _blank_displays(reason: str):
    _cancel_pending_display(reason)
    debug(f"[state] blank displays reason={reason!r}")
    blank_mpv(BACKGLASS_PIPE_NAME, event_id="[state]")
    blank_mpv(DMD_PIPE_NAME, event_id="[state]")

def _restore_browse_state(reason: str):
    event = _viewer_state.browse_event
    if not event:
        debug(f"[state] no browse state to restore reason={reason!r}")
        return

    debug(
        f"[state] restore browse state reason={reason!r} "
        f"event={event.get('event')!r} system={event.get('system')!r}"
    )
    handle_message(dict(event))

def is_job_stale(job_id: int) -> bool:
    with _lock:
        return job_id != _job_id


def _ensure_worker_started():
    global _worker_started
    with _pending_cond:
        if _worker_started:
            return
        t = threading.Thread(target=_latest_only_worker, name="backglass_fast_latest_worker", daemon=True)
        t.start()
        _worker_started = True
        debug("[worker] fast latest-only worker started")


def handle_message(msg: dict):
    global _job_id, _pending_msg, _pending_job_id

    handle_start_ms = _now_ms()

    with _lock:
        _job_id += 1
        job_id = _job_id
        event_id = f"[job {job_id}]"
    
    debug(f"{event_id} received msg={msg}")
    _ensure_worker_started()

    with _pending_cond:
        replaced_job = _pending_job_id if _pending_msg is not None else 0
        _pending_msg = msg
        _pending_job_id = job_id
        _pending_cond.notify()

    if replaced_job:
        debug(f"{event_id} queued latest; replaced pending job {replaced_job}; handle_elapsed={_elapsed_ms(handle_start_ms):.1f}ms")
    else:
        debug(f"{event_id} queued latest; handle_elapsed={_elapsed_ms(handle_start_ms):.1f}ms")


def _latest_only_worker():
    global _pending_msg, _pending_job_id

    while True:
        with _pending_cond:
            while _pending_msg is None:
                _pending_cond.wait()
            msg = _pending_msg
            job_id = _pending_job_id
            _pending_msg = None
            _pending_job_id = 0

        event_id = f"[job {job_id}]"
        if is_job_stale(job_id):
            debug(f"{event_id} worker drop before display; newer job already queued")
            continue

        _process_message_for_display(msg, job_id)


def _process_message_for_display(msg: dict, job_id: int):
    event_id = f"[job {job_id}]"
    start_ms = _now_ms()

    try:
        event_name = (msg.get("event") or "").strip()
        system = (msg.get("system") or "").strip().strip('"')
        path = (msg.get("path") or "").strip().strip('"')
        debug(
            f"{event_id} normalized event={event_name!r} "
            f"system={system!r} path={path!r}"
        )

        if event_name == "system-selected":
            if not system:
                debug(f"{event_id} worker skipped system-selected without system")
                return

            fanart, marquee, backglass, logo, dmd_video = find_system_media(
                system,
                event_id=event_id,
            )

            if is_job_stale(job_id):
                debug(f"{event_id} worker abort after system media lookup; superseded")
                return

            handled = show_selected_media(
                fanart, marquee, backglass, logo, dmd_video,
                event_id=event_id,
                should_continue=lambda: not is_job_stale(job_id),
            )

            if not handled and not is_job_stale(job_id):
                show_in_mpv(str(FALLBACK_IMAGE), pipe_name=BACKGLASS_PIPE_NAME, event_id=event_id)
                show_in_mpv(str(FALLBACK_IMAGE), pipe_name=DMD_PIPE_NAME, event_id=event_id)

            debug(f"{event_id} worker display done system handled={handled} elapsed={_elapsed_ms(start_ms):.1f}ms")

            return

        if not system or not path:
            debug(f"{event_id} worker skipped missing system/path system={system!r} path={path!r}")
            return

        rom_path = normalize_rom_path(path)
        fanart, marquee, backglass, logo, dmd_video = find_media_files(system, rom_path, event_id=event_id)

        if is_job_stale(job_id):
            debug(f"{event_id} worker abort after media_lookup; superseded")
            return

        handled = show_selected_media(
            fanart, marquee, backglass, logo, dmd_video,
            event_id=event_id,
            should_continue=lambda: not is_job_stale(job_id),
        )
        debug(f"{event_id} worker display done handled={handled} elapsed={_elapsed_ms(start_ms):.1f}ms")
    except Exception as e:
        log(f"{event_id} worker display error: {type(e).__name__}: {e}; elapsed={_elapsed_ms(start_ms):.1f}ms")


def _handle_es_event(event: dict):
    """Apply EmulationStation frontend events to BackglassViewer state."""
    event_name = (event.get("event") or "").strip()

    if event_name == "quit":
        _shutdown_viewer("EmulationStation quit event")
        raise ViewerShutdown()

    if event_name == "system-selected":
        if _viewer_state.suspended:
            debug(f"[pipe] ignored system-selected while suspended event={event!r}")
            return

        _viewer_state.mode = "browsing"
        _viewer_state.system = (event.get("system") or "").strip().strip('"')
        _viewer_state.browse_event = dict(event)
        handle_message(event)
        return

    if event_name == "game-selected":
        if _viewer_state.suspended:
            debug(f"[pipe] ignored game-selected while suspended event={event!r}")
            return

        _viewer_state.mode = "browsing"
        _viewer_state.system = (
            (event.get("system") or "").strip().strip('"')
            or _viewer_state.system
        )
        _viewer_state.browse_event = dict(event)
        handle_message(event)
        return

    if event_name == "game-start":
        _viewer_state.mode = "running"

        # game-start's legacy ES arguments don't necessarily include the
        # selected game's system, so the last browse selection is authoritative
        # when the translated event doesn't provide one.
        system = (
            (event.get("system") or "").strip().strip('"')
            or _viewer_state.system
        )

        debug(f"[state] game-start system={system!r} event={event!r}")

        if system.lower() in BLANK_ON_GAME_START_SYSTEMS:
            _blank_displays(f"game-start system={system}")
        return

    if event_name == "game-end":
        _viewer_state.mode = "browsing"
        if not _viewer_state.suspended:
            _restore_browse_state("game-end")
        return

    if event_name in ("screensaver-start", "sleep"):
        _viewer_state.suspended = True
        _blank_displays(event_name)
        return

    if event_name in ("screensaver-stop", "wake"):
        _viewer_state.suspended = False
        _viewer_state.mode = "browsing"
        _restore_browse_state(event_name)
        return
    
    debug(f"[pipe] ignored event={event!r}")


def _event_pipe_listener():
    """Continuously connect to ES and consume newline-delimited JSON events."""
    while True:
        try:
            debug(f"[pipe] connecting to {ES_EVENT_PIPE_NAME}")
            with open(ES_EVENT_PIPE_NAME, "rb", buffering=0) as pipe:
                log(f"[pipe] connected to {ES_EVENT_PIPE_NAME}")
                while True:
                    raw = pipe.readline()
                    if not raw:
                        raise BrokenPipeError("EmulationStation event pipe closed")
                    try:
                        event = json.loads(raw.decode("utf-8-sig"))
                    except (UnicodeDecodeError, json.JSONDecodeError) as e:
                        log(f"[pipe] ignored malformed event: {type(e).__name__}: {e}")
                        continue
                    if not isinstance(event, dict):
                        debug(f"[pipe] ignored non-object event type={type(event).__name__}")
                        continue
                    _handle_es_event(event)
        except ViewerShutdown:
            log("[pipe] shutdown requested")
            return
        except FileNotFoundError:
            debug(f"[pipe] waiting for EmulationStation pipe {ES_EVENT_PIPE_NAME}")
        except OSError as e:
            debug(f"[pipe] disconnected: {type(e).__name__}: {e}")
        except Exception as e:
            log(f"[pipe] listener error: {type(e).__name__}: {e}")
        time.sleep(0.25)


def _shutdown_viewer(reason: str):
    log(f"[quit] {reason}; stopping server")
    try:
        send_mpv({"command": ["quit"]}, pipe_name=BACKGLASS_PIPE_NAME, event_id="[quit]")
        send_mpv({"command": ["quit"]}, pipe_name=DMD_PIPE_NAME, event_id="[quit]")
    except Exception as e:
        log(f"[quit] mpv quit failed: {type(e).__name__}: {e}")


def serve():
    log("fanart_server starting (EmulationStation named-pipe listener)...")
    log(
        f"config BackglassScreenMode={BACKGLASS_SCREEN_MODE} "
        f"DmdScreenMode={DMD_SCREEN_MODE} SimpleFastMode=True Compositing=True "
        f"InputMode=named-pipe EventPipe={ES_EVENT_PIPE_NAME} "
        f"SystemMediaDir={SYSTEM_MEDIA_DIR} "
        f"BlankOnGameStartSystems={sorted(BLANK_ON_GAME_START_SYSTEMS)} "
        f"DebugTimings={DEBUG_TIMINGS} SlowMs={SLOW_MS}"
    )
    log(f"Listening for EmulationStation events: {ES_EVENT_PIPE_NAME}")
    _event_pipe_listener()


if __name__ == "__main__":
    try:
        serve()
    except Exception as e:
        log(f"FATAL ERROR: {type(e).__name__}: {e}")
