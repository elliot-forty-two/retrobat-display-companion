import ctypes
import math
import shutil
import subprocess
import threading
import time

from ctypes import wintypes
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageStat


# ----------------------------------------------------------------------
# Win32
# ----------------------------------------------------------------------

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
CREATE_NO_WINDOW = 0x08000000


class RECT(ctypes.Structure):
    _fields_ = [
        ("left", ctypes.c_long),
        ("top", ctypes.c_long),
        ("right", ctypes.c_long),
        ("bottom", ctypes.c_long),
    ]


EnumWindowsProc = ctypes.WINFUNCTYPE(
    wintypes.BOOL,
    wintypes.HWND,
    wintypes.LPARAM,
)


user32.EnumWindows.argtypes = [
    EnumWindowsProc,
    wintypes.LPARAM,
]
user32.EnumWindows.restype = wintypes.BOOL

user32.IsWindowVisible.argtypes = [
    wintypes.HWND,
]
user32.IsWindowVisible.restype = wintypes.BOOL

user32.GetWindowRect.argtypes = [
    wintypes.HWND,
    ctypes.POINTER(RECT),
]
user32.GetWindowRect.restype = wintypes.BOOL

user32.GetWindowThreadProcessId.argtypes = [
    wintypes.HWND,
    ctypes.POINTER(wintypes.DWORD),
]

kernel32.OpenProcess.argtypes = [
    wintypes.DWORD,
    wintypes.BOOL,
    wintypes.DWORD,
]
kernel32.OpenProcess.restype = wintypes.HANDLE

kernel32.CloseHandle.argtypes = [
    wintypes.HANDLE,
]

kernel32.QueryFullProcessImageNameW.argtypes = [
    wintypes.HANDLE,
    wintypes.DWORD,
    wintypes.LPWSTR,
    ctypes.POINTER(wintypes.DWORD),
]


# ----------------------------------------------------------------------
# Models
# ----------------------------------------------------------------------

@dataclass(frozen=True)
class CaptureDisplay:
    name: str
    x: int
    y: int
    width: int
    height: int

    # Video capture options.
    video_enabled: bool = False
    video_framerate: int = 15
    video_quality: int = 25


@dataclass(frozen=True)
class WindowInfo:
    hwnd: int
    pid: int
    process_name: str
    x: int
    y: int
    width: int
    height: int


@dataclass
class StillCandidate:
    score: float
    path: Path
    captured_at: float


@dataclass
class VideoCapture:
    display: CaptureDisplay
    process: subprocess.Popen
    log_file: object
    output_path: Path


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------

def _process_name(pid: int) -> str:
    handle = kernel32.OpenProcess(
        PROCESS_QUERY_LIMITED_INFORMATION,
        False,
        pid,
    )

    if not handle:
        return ""

    try:
        size = wintypes.DWORD(32768)
        buf = ctypes.create_unicode_buffer(size.value)

        if not kernel32.QueryFullProcessImageNameW(
            handle,
            0,
            buf,
            ctypes.byref(size),
        ):
            return ""

        return Path(buf.value).name

    finally:
        kernel32.CloseHandle(handle)


def _windows() -> list[WindowInfo]:
    result = []

    @EnumWindowsProc
    def callback(hwnd, _):
        if not user32.IsWindowVisible(hwnd):
            return True

        rect = RECT()

        if not user32.GetWindowRect(
            hwnd,
            ctypes.byref(rect),
        ):
            return True

        width = rect.right - rect.left
        height = rect.bottom - rect.top

        if width <= 0 or height <= 0:
            return True

        pid = wintypes.DWORD()

        user32.GetWindowThreadProcessId(
            hwnd,
            ctypes.byref(pid),
        )

        if not pid.value:
            return True

        result.append(
            WindowInfo(
                hwnd=int(hwnd),
                pid=int(pid.value),
                process_name=_process_name(pid.value),
                x=rect.left,
                y=rect.top,
                width=width,
                height=height,
            )
        )

        return True

    user32.EnumWindows(callback, 0)

    return result


def _safe_filename(name: str) -> str:
    invalid = '<>:"/\\|?*'

    for char in invalid:
        name = name.replace(char, "_")

    name = " ".join(name.split())

    return name.strip(" .") or "UnknownTable"


# ----------------------------------------------------------------------
# Capture manager
# ----------------------------------------------------------------------

class CaptureManager:
    def __init__(
        self,
        *,
        enabled: bool,
        systems: set[str],
        output_dir: Path,
        ffmpeg: str,
        duration_seconds: int,
        displays: list[CaptureDisplay],

        window_timeout_seconds: float = 60.0,
        window_stable_seconds: float = 2.0,
        settle_seconds: float = 3.0,
        window_poll_seconds: float = 0.25,

        screenshot_seconds: float = 1.0,

        still_start_delay_seconds: float = 3.0,
        still_sample_seconds: float = 2.0,
        still_sample_duration_seconds: float = 30.0,

        qsv_preset: str = "veryfast",
        qsv_async_depth: int = 2,

        log=None,
    ):
        self.enabled = enabled

        self.systems = {
            system.strip().lower()
            for system in systems
            if system.strip()
        }

        self.output_dir = Path(output_dir)
        self.ffmpeg = self._resolve_ffmpeg(ffmpeg) if enabled else ""

        self.duration_seconds = duration_seconds
        self.displays = displays

        self.window_timeout_seconds = window_timeout_seconds
        self.window_stable_seconds = window_stable_seconds
        self.settle_seconds = settle_seconds
        self.window_poll_seconds = window_poll_seconds

        self.screenshot_seconds = screenshot_seconds

        self.still_start_delay_seconds = (
            still_start_delay_seconds
        )

        self.still_sample_seconds = (
            still_sample_seconds
        )

        self.still_sample_duration_seconds = (
            still_sample_duration_seconds
        )

        self.qsv_preset = qsv_preset
        self.qsv_async_depth = qsv_async_depth

        self.log = log or (lambda message: None)

        self._lock = threading.Lock()
        self._stop_event = None
        self._capture_processes = []

        self._playfield = next(
            (
                display
                for display in displays
                if display.name.lower() == "playfield"
            ),
            None,
        )

        if self.enabled and not self._playfield:
            raise ValueError(
                "Capture enabled but no playfield display configured"
            )

        if self.enabled:
            self.output_dir.mkdir(
                parents=True,
                exist_ok=True,
            )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def game_started(
        self,
        system: str,
        path: str,
    ):
        if not self.enabled:
            return

        system = (
            system or ""
        ).strip().lower()

        if system not in self.systems:
            return

        path = (
            path or ""
        ).strip().strip('"')

        if not path:
            self.log(
                "[capture] game-start ignored: no game path"
            )
            return

        table_path = Path(path)
        table_name = _safe_filename(
            table_path.stem
        )

        with self._lock:
            if self._stop_event:
                self._stop_event.set()

            stop_event = threading.Event()
            self._stop_event = stop_event

        self.log(
            f"[capture] armed "
            f"system={system!r} "
            f"table={table_name!r}"
        )

        thread = threading.Thread(
            target=self._capture_worker,
            args=(
                table_name,
                stop_event,
            ),
            name="vpin_capture",
            daemon=True,
        )

        thread.start()

    def game_ended(self):
        if not self.enabled:
            return

        with self._lock:
            stop_event = self._stop_event

        if stop_event:
            self.log(
                "[capture] game-end received"
            )

            stop_event.set()

    # ------------------------------------------------------------------
    # FFmpeg
    # ------------------------------------------------------------------

    @staticmethod
    def _resolve_ffmpeg(
        configured: str,
    ) -> str:
        configured = (
            configured or ""
        ).strip().strip('"')

        if configured:
            if not Path(configured).is_file():
                raise RuntimeError(
                    f"Configured FFmpeg does not exist: "
                    f"{configured}"
                )

            return configured

        found = (
            shutil.which("ffmpeg.exe")
            or shutil.which("ffmpeg")
        )

        if not found:
            raise RuntimeError(
                "FFmpeg not found in PATH and no "
                "CaptureFfmpeg value configured"
            )

        return found

    # ------------------------------------------------------------------
    # VPX player detection
    # ------------------------------------------------------------------

    @staticmethod
    def _is_vpx_process(
        name: str,
    ) -> bool:
        name = (
            name or ""
        ).lower()

        return (
            name.startswith("vpinballx")
            and name.endswith(".exe")
        )

    def _find_player_window(
        self,
    ) -> WindowInfo | None:
        playfield = self._playfield

        min_width = int(
            playfield.width * 0.80
        )

        min_height = int(
            playfield.height * 0.80
        )

        position_tolerance = 100

        candidates = []

        for window in _windows():
            if not self._is_vpx_process(
                window.process_name
            ):
                continue

            if window.width < min_width:
                continue

            if window.height < min_height:
                continue

            if (
                abs(window.x - playfield.x)
                > position_tolerance
            ):
                continue

            if (
                abs(window.y - playfield.y)
                > position_tolerance
            ):
                continue

            candidates.append(window)

        if not candidates:
            return None

        return max(
            candidates,
            key=lambda window:
                window.width * window.height,
        )

    def _wait_for_player(
        self,
        stop_event: threading.Event,
    ) -> WindowInfo | None:
        deadline = (
            time.monotonic()
            + self.window_timeout_seconds
        )

        stable_hwnd = None
        stable_since = None

        self.log(
            "[capture] waiting for VPX player window"
        )

        while time.monotonic() < deadline:
            if stop_event.is_set():
                return None

            window = self._find_player_window()

            if not window:
                stable_hwnd = None
                stable_since = None

                stop_event.wait(
                    self.window_poll_seconds
                )

                continue

            if window.hwnd != stable_hwnd:
                stable_hwnd = window.hwnd
                stable_since = time.monotonic()

                self.log(
                    f"[capture] VPX player detected "
                    f"pid={window.pid} "
                    f"hwnd=0x{window.hwnd:X} "
                    f"rect={window.x},{window.y} "
                    f"{window.width}x{window.height}"
                )

            elif (
                stable_since is not None
                and (
                    time.monotonic()
                    - stable_since
                )
                >= self.window_stable_seconds
            ):
                self.log(
                    f"[capture] VPX player stable "
                    f"for "
                    f"{self.window_stable_seconds:.1f}s"
                )

                return window

            stop_event.wait(
                self.window_poll_seconds
            )

        self.log(
            "[capture] timed out waiting "
            "for VPX player window"
        )

        return None

    # ------------------------------------------------------------------
    # Worker
    # ------------------------------------------------------------------

    def _capture_worker(
        self,
        table_name: str,
        stop_event: threading.Event,
    ):
        try:
            window = self._wait_for_player(
                stop_event
            )

            if not window:
                return

            self.log(
                f"[capture] waiting "
                f"{self.settle_seconds:.1f}s "
                f"for table displays to settle"
            )

            if stop_event.wait(
                self.settle_seconds
            ):
                return

            self._capture(
                table_name,
                stop_event,
            )

        except Exception as e:
            self.log(
                f"[capture] ERROR: "
                f"{type(e).__name__}: {e}"
            )

    # ------------------------------------------------------------------
    # Capture input construction
    # ------------------------------------------------------------------

    def _capture_input_args(
        self,
        display: CaptureDisplay,
        framerate: int,
    ) -> list[str]:
        return [
            "-f",
            "gdigrab",

            "-framerate",
            str(framerate),

            "-offset_x",
            str(display.x),

            "-offset_y",
            str(display.y),

            "-video_size",
            f"{display.width}x{display.height}",

            "-draw_mouse",
            "0",

            "-i",
            "desktop",
        ]


    # ------------------------------------------------------------------
    # Main capture
    # ------------------------------------------------------------------

    def _capture(
        self,
        table_name: str,
        stop_event: threading.Event,
    ):
        stamp = datetime.now().strftime(
            "%Y%m%d-%H%M%S-%f"
        )

        run_dir = (
            self.output_dir
            / f"{stamp}_{table_name}"
        )

        run_dir.mkdir(
            parents=True,
            exist_ok=True,
        )

        enabled_video_displays = [
            display
            for display in self.displays
            if display.video_enabled
        ]

        self.log(
            f"[capture] starting "
            f"table={table_name!r} "
            f"duration={self.duration_seconds}s "
            f"videos="
            f"{','.join(d.name for d in enabled_video_displays) or 'none'} "
            f"output={run_dir}"
        )

        #
        # Backglass/DMD best-frame selection remains independent of
        # whether their videos are also enabled.
        #
        still_threads = []

        for display in self.displays:
            if display.name.lower() == "playfield":
                continue

            thread = threading.Thread(
                target=self._capture_best_still,
                args=(
                    display,
                    run_dir,
                    table_name,
                    stop_event,
                ),
                name=f"capture_still_{display.name}",
                daemon=True,
            )

            thread.start()
            still_threads.append(thread)

        #
        # Start all enabled video captures.
        #
        video_captures = []

        try:
            for display in enabled_video_displays:
                capture = self._start_video_capture(
                    display,
                    run_dir,
                    table_name,
                )

                video_captures.append(
                    capture
                )

            with self._lock:
                self._capture_processes = [
                    capture.process
                    for capture in video_captures
                ]

            #
            # Wait for all videos to finish, or stop all of them on
            # game-end.
            #
            while True:
                running = [
                    capture
                    for capture in video_captures
                    if capture.process.poll() is None
                ]

                if not running:
                    break

                if stop_event.wait(0.25):
                    self.log(
                        "[capture] stopping active "
                        "video captures"
                    )

                    for capture in running:
                        self._stop_ffmpeg(
                            capture.process
                        )

                    break

            #
            # Ensure all encoders have stopped cleanly.
            #
            for capture in video_captures:
                process = capture.process

                try:
                    process.wait(
                        timeout=5
                    )

                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()

        except Exception:
            for capture in video_captures:
                self._stop_ffmpeg(capture.process)
            raise

        finally:
            with self._lock:
                self._capture_processes = []

            for capture in video_captures:
                try:
                    capture.log_file.close()
                except Exception:
                    pass

        #
        # Allow PNG selector threads to finish.
        #
        for thread in still_threads:
            thread.join(
                timeout=5
            )

        #
        # Playfield image.
        #
        # Prefer extracting it from the video if the playfield video
        # exists. Otherwise take a direct still.
        #
        playfield = self._playfield

        playfield_video = (
            run_dir
            / f"{table_name}-video.mp4"
        )

        playfield_png = (
            run_dir
            / f"{table_name}-image.png"
        )

        if (
            playfield_video.is_file()
            and playfield_video.stat().st_size > 0
        ):
            self._create_video_screenshot(
                playfield_video,
                playfield_png,
            )

        elif not stop_event.is_set():
            self._grab_still(
                playfield,
                playfield_png,
            )

        self.log(
            f"[capture] complete "
            f"table={table_name!r}"
        )

    # ------------------------------------------------------------------
    # Video
    # ------------------------------------------------------------------

    def _video_output_path(
        self,
        run_dir: Path,
        table_name: str,
        display: CaptureDisplay,
    ) -> Path:
        if display.name.lower() == "playfield":
            return (
                run_dir
                / f"{table_name}-video.mp4"
            )

        return (
            run_dir
            / f"{table_name}-{display.name}.mp4"
        )

    def _start_video_capture(
        self,
        display: CaptureDisplay,
        run_dir: Path,
        table_name: str,
    ) -> VideoCapture:
        output_path = self._video_output_path(
            run_dir,
            table_name,
            display,
        )

        log_path = (
            run_dir
            / f"{table_name}-{display.name}.log"
        )

        args = [
            self.ffmpeg,
            "-y",
        ]

        args.extend(
            self._capture_input_args(
                display,
                display.video_framerate,
            )
        )

        args.extend([
            #
            # Intel Quick Sync H.264
            #
            "-c:v",
            "h264_qsv",

            #
            # Fastest QSV preset.
            #
            "-preset",
            self.qsv_preset,

            #
            # ICQ-style quality control.
            #
            "-global_quality",
            str(display.video_quality),

            #
            # Look-ahead costs additional resources and isn't useful
            # for this transient frontend-media capture.
            #
            "-look_ahead",
            "0",

            #
            # Keep only a small number of QSV jobs in flight.
            #
            "-async_depth",
            str(self.qsv_async_depth),

            "-pix_fmt",
            "nv12",

            "-t",
            str(self.duration_seconds),

            output_path,
        ])

        self.log(
            f"[capture] starting video "
            f"display={display.name!r} "
            f"fps={display.video_framerate} "
            f"quality={display.video_quality} "
            f"size={display.width}x{display.height}"
        )

        log_file = open(
            log_path,
            "wb",
        )

        try:
            process = subprocess.Popen(
                args,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=log_file,
                creationflags=CREATE_NO_WINDOW,
            )

        except Exception:
            log_file.close()
            raise

        return VideoCapture(
            display=display,
            process=process,
            log_file=log_file,
            output_path=output_path,
        )

    # ------------------------------------------------------------------
    # Best PNG selection
    # ------------------------------------------------------------------

    def _capture_best_still(
        self,
        display: CaptureDisplay,
        run_dir: Path,
        table_name: str,
        stop_event: threading.Event,
    ):
        if stop_event.wait(
            self.still_start_delay_seconds
        ):
            return

        samples_dir = (
            run_dir
            / f".{display.name}-samples"
        )

        samples_dir.mkdir(
            parents=True,
            exist_ok=True,
        )

        best = None
        start = time.monotonic()
        index = 0

        try:
            while (
                time.monotonic() - start
                < self.still_sample_duration_seconds
            ):
                if stop_event.is_set():
                    break

                sample_path = (
                    samples_dir
                    / f"sample-{index:03d}.png"
                )

                if self._grab_still(
                    display,
                    sample_path,
                ):
                    try:
                        score = self._score_still(
                            sample_path
                        )

                        elapsed = (
                            time.monotonic()
                            - start
                        )

                        self.log(
                            f"[capture] "
                            f"{display.name} "
                            f"sample={index} "
                            f"time={elapsed:.1f}s "
                            f"score={score:.1f}"
                        )

                        if (
                            best is None
                            or score > best.score
                        ):
                            best = StillCandidate(
                                score=score,
                                path=sample_path,
                                captured_at=elapsed,
                            )

                    except Exception as e:
                        self.log(
                            f"[capture] unable to score "
                            f"{display.name} sample: {e}"
                        )

                index += 1

                if stop_event.wait(
                    self.still_sample_seconds
                ):
                    break

            if best is None:
                self.log(
                    f"[capture] no usable "
                    f"{display.name} still"
                )

                return

            output = (
                run_dir
                / f"{table_name}-{display.name}.png"
            )

            shutil.copy2(
                best.path,
                output,
            )

            self.log(
                f"[capture] selected "
                f"{display.name} still "
                f"score={best.score:.1f} "
                f"at {best.captured_at:.1f}s"
            )

        finally:
            shutil.rmtree(
                samples_dir,
                ignore_errors=True,
            )

    # ------------------------------------------------------------------
    # Single-frame capture
    # ------------------------------------------------------------------

    def _grab_still(
        self,
        display: CaptureDisplay,
        output_path: Path,
    ) -> bool:
        args = [
            self.ffmpeg,
            "-y",

            "-loglevel",
            "error",
        ]

        args.extend(
            self._capture_input_args(
                display,
                1,
            )
        )

        args.extend([
            "-frames:v",
            "1",

            output_path,
        ])

        result = subprocess.run(
            args,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            creationflags=CREATE_NO_WINDOW,
        )

        if result.returncode != 0:
            error = result.stderr.decode(
                "utf-8",
                errors="replace",
            ).strip()

            self.log(
                f"[capture] still capture failed "
                f"display={display.name!r}: "
                f"{error}"
            )

            return False

        return (
            output_path.is_file()
            and output_path.stat().st_size > 0
        )

    # ------------------------------------------------------------------
    # Still scoring
    # ------------------------------------------------------------------

    def _score_still(
        self,
        path: Path,
    ) -> float:
        with Image.open(path) as image:
            image = image.convert("L")

            #
            # Full resolution isn't needed for overall brightness /
            # contrast analysis.
            #
            image.thumbnail(
                (320, 240)
            )

            stat = ImageStat.Stat(
                image
            )

            mean = stat.mean[0]
            stddev = stat.stddev[0]

            histogram = image.histogram()
            total = sum(histogram)

            if total <= 0:
                return -math.inf

            dark_fraction = (
                sum(histogram[0:20])
                / total
            )

            white_fraction = (
                sum(histogram[245:256])
                / total
            )

            #
            # Prefer a bright, contrasty image while avoiding either
            # an almost-black startup image or a mostly-white flash.
            #
            return (
                mean
                + (stddev * 1.5)
                - (dark_fraction * 80.0)
                - (white_fraction * 40.0)
            )

    # ------------------------------------------------------------------
    # Playfield PNG from video
    # ------------------------------------------------------------------

    def _create_video_screenshot(
        self,
        mp4: Path,
        png: Path,
    ):
        if (
            not mp4.is_file()
            or mp4.stat().st_size == 0
        ):
            return

        args = [
            self.ffmpeg,
            "-y",

            "-loglevel",
            "error",

            "-ss",
            str(self.screenshot_seconds),

            "-i",
            str(mp4),

            "-frames:v",
            "1",

            str(png),
        ]

        result = subprocess.run(
            args,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            creationflags=CREATE_NO_WINDOW,
        )

        if result.returncode != 0:
            error = result.stderr.decode(
                "utf-8",
                errors="replace",
            ).strip()

            self.log(
                f"[capture] playfield screenshot "
                f"failed: {error}"
            )

    # ------------------------------------------------------------------
    # FFmpeg shutdown
    # ------------------------------------------------------------------

    @staticmethod
    def _stop_ffmpeg(
        process: subprocess.Popen,
    ):
        if process.poll() is not None:
            return

        #
        # q asks FFmpeg to stop normally, allowing it to finish the
        # MP4 moov atom rather than leaving a broken file.
        #
        try:
            if process.stdin:
                process.stdin.write(
                    b"q\n"
                )

                process.stdin.flush()

            process.wait(
                timeout=5
            )

            return

        except Exception:
            pass

        try:
            process.terminate()

            process.wait(
                timeout=2
            )

            return

        except Exception:
            pass

        try:
            process.kill()
        except Exception:
            pass
