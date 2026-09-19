import os
import hashlib
import time
from pathlib import Path
from collections import OrderedDict
from typing import Callable

import numpy as np
import cv2
from PIL import Image, ImageFilter, ImageEnhance

BASE_DIR = Path(__file__).resolve().parent
CACHE_DIR = BASE_DIR / "cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

TARGET_W = 1280
TARGET_H = 1024

MARQUEE_PADDING = 20
MARQUEE_MAX_W_FRAC = 0.50
MARQUEE_MAX_H_FRAC = 0.50
MARQUEE_SCALES = (1.00, 0.90, 0.82, 0.74, 0.66)

SMALL_W = 240
CACHE_HIT_FASTPATH = True

CROP_VERSION = "salcrop_v2"
PLACE_VERSION = "avoidplace_v2"
COMP_VERSION = "comp_v2"

SAL_CROP_CACHE = OrderedDict()
SAL_CROP_CACHE_MAX = 250
AVOID_CACHE = OrderedDict()
AVOID_CACHE_MAX = 250

CACHE_MAX_BYTES = 1024 * 1024 * 1024
CACHE_MAX_AGE_SECONDS = 30 * 24 * 60 * 60


def cleanup_disk_cache() -> None:
    """Remove expired cache files, then trim oldest files to the size limit."""
    now = time.time()
    entries = []
    total_size = 0

    for path in CACHE_DIR.glob("*.png"):
        try:
            stat = path.stat()
            if now - stat.st_mtime > CACHE_MAX_AGE_SECONDS:
                path.unlink()
                continue
            entries.append((stat.st_mtime, stat.st_size, path))
            total_size += stat.st_size
        except OSError:
            continue

    if total_size <= CACHE_MAX_BYTES:
        return

    for _, size, path in sorted(entries):
        try:
            path.unlink()
            total_size -= size
        except OSError:
            continue
        if total_size <= CACHE_MAX_BYTES:
            break


cleanup_disk_cache()


def lru_get(cache: OrderedDict, key: str):
    v = cache.get(key)
    if v is None:
        return None
    cache.move_to_end(key)
    return v


def lru_set(cache: OrderedDict, key: str, val, max_items: int):
    cache[key] = val
    cache.move_to_end(key)
    while len(cache) > max_items:
        cache.popitem(last=False)


def file_sig(path: str) -> str:
    try:
        st = os.stat(path)
        m = getattr(st, "st_mtime_ns", int(st.st_mtime * 1e9))
        return f"{st.st_size}:{m}"
    except Exception:
        return "missing"


def make_cache_key(*parts: str) -> str:
    h = hashlib.sha1()
    for p in parts:
        h.update(str(p).encode("utf-8", errors="ignore"))
        h.update(b"\0")
    return h.hexdigest()


def trim_transparent(im: Image.Image) -> Image.Image:
    if im.mode != "RGBA":
        im = im.convert("RGBA")
    bbox = im.getbbox()
    return im.crop(bbox) if bbox else im


def resize_cover(im: Image.Image, out_w: int, out_h: int) -> Image.Image:
    im = im.convert("RGBA")
    iw, ih = im.size
    if iw <= 0 or ih <= 0:
        return im
    scale = max(out_w / iw, out_h / ih)
    nw, nh = max(1, int(iw * scale)), max(1, int(ih * scale))
    im = im.resize((nw, nh), Image.LANCZOS)
    left = (nw - out_w) // 2
    top = (nh - out_h) // 2
    return im.crop((left, top, left + out_w, top + out_h))


def marquee_luminance(marquee_rgba: Image.Image) -> float:
    arr = np.asarray(marquee_rgba.convert("RGBA"), dtype=np.uint8)
    rgb = arr[..., :3].astype(np.float32)
    a = arr[..., 3].astype(np.float32) / 255.0
    if a.max() <= 0:
        return 128.0
    lum = 0.2126 * rgb[..., 0] + 0.7152 * rgb[..., 1] + 0.0722 * rgb[..., 2]
    return float((lum * a).sum() / (a.sum() + 1e-6))


def rect_sum_integral(ii: np.ndarray, x0: int, y0: int, x1: int, y1: int) -> float:
    h1, w1 = ii.shape
    x0 = max(0, min(w1 - 1, x0))
    x1 = max(0, min(w1 - 1, x1))
    y0 = max(0, min(h1 - 1, y0))
    y1 = max(0, min(h1 - 1, y1))
    if x1 <= x0 or y1 <= y0:
        return 0.0
    return float(ii[y1, x1] - ii[y0, x1] - ii[y1, x0] + ii[y0, x0])


def compute_saliency_integral_cv(pil_img: Image.Image, small_w: int = SMALL_W):
    W, H = pil_img.size
    scale = small_w / float(W)
    sh = max(1, int(H * scale))
    sim = pil_img.convert("RGB").resize((small_w, sh), Image.BILINEAR)
    bgr = cv2.cvtColor(np.array(sim), cv2.COLOR_RGB2BGR)

    sal = cv2.saliency.StaticSaliencySpectralResidual_create()
    ok, salmap = sal.computeSaliency(bgr)
    if not ok or salmap is None:
        sal_f = np.zeros((sh, small_w), dtype=np.float32)
    else:
        sal_f = salmap.astype(np.float32)
        sal_f = (sal_f - sal_f.min()) / (sal_f.max() - sal_f.min() + 1e-6)
    sal_f = cv2.GaussianBlur(sal_f, (0, 0), 1.0)
    ii = cv2.integral(sal_f, sdepth=cv2.CV_64F)
    return ii, scale, (small_w, sh)


def saliency_integral_cached(key: str, pil_img: Image.Image):
    cached = lru_get(SAL_CROP_CACHE, key)
    if cached is not None:
        return cached
    payload = compute_saliency_integral_cv(pil_img)
    lru_set(SAL_CROP_CACHE, key, payload, SAL_CROP_CACHE_MAX)
    return payload


def crop_to_aspect_by_saliency(im: Image.Image, target_aspect: float, sal_payload, bias_bottom: float = 0.12):
    ii, scale, (sw, sh) = sal_payload
    W, H = im.size

    cur_aspect = sw / float(sh)
    if cur_aspect > target_aspect:
        win_h = sh
        win_w = int(sh * target_aspect)
    else:
        win_w = sw
        win_h = int(sw / target_aspect)

    win_w = max(1, min(sw, win_w))
    win_h = max(1, min(sh, win_h))

    step_x = max(2, win_w // 16)
    step_y = max(2, win_h // 16)

    cx_target = (sw - win_w) / 2.0
    cy_target = (sh - win_h) * (0.5 + bias_bottom)

    best = None
    best_score = -1e30

    bias_center = 0.00007

    for y0 in range(0, sh - win_h + 1, step_y):
        y1 = y0 + win_h
        for x0 in range(0, sw - win_w + 1, step_x):
            x1 = x0 + win_w
            s = rect_sum_integral(ii, x0, y0, x1, y1)
            dx = x0 - cx_target
            dy = y0 - cy_target
            s -= bias_center * (dx * dx + dy * dy)
            if s > best_score:
                best_score = s
                best = (x0, y0, x1, y1)

    if not best:
        return im

    x0, y0, x1, y1 = best
    ox0 = int(x0 / scale)
    oy0 = int(y0 / scale)
    ox1 = int(x1 / scale)
    oy1 = int(y1 / scale)

    ox0 = max(0, min(W - 1, ox0))
    oy0 = max(0, min(H - 1, oy0))
    ox1 = max(1, min(W, ox1))
    oy1 = max(1, min(H, oy1))

    return im.crop((ox0, oy0, ox1, oy1))


def compute_avoid_integral_cv(bg_rgb: Image.Image, small_w: int = SMALL_W):
    W, H = bg_rgb.size
    scale = small_w / float(W)
    sh = max(1, int(H * scale))

    sim = bg_rgb.resize((small_w, sh), Image.BILINEAR)
    rgb = np.array(sim.convert("RGB"))
    bgr = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)

    edges = cv2.Canny(gray, 60, 140).astype(np.float32) / 255.0
    lap = cv2.Laplacian(gray, cv2.CV_32F)
    lap = np.abs(lap).astype(np.float32)
    lap = lap / (lap.max() + 1e-6)
    busy = 0.65 * edges + 0.35 * lap

    sal = cv2.saliency.StaticSaliencySpectralResidual_create()
    ok, salmap = sal.computeSaliency(bgr)
    if ok and salmap is not None:
        sal_f = salmap.astype(np.float32)
        sal_f = (sal_f - sal_f.min()) / (sal_f.max() - sal_f.min() + 1e-6)
    else:
        sal_f = np.zeros_like(busy, dtype=np.float32)

    avoid = 0.55 * busy + 0.45 * sal_f
    avoid = cv2.GaussianBlur(avoid, (0, 0), 1.0)

    rgb_f = rgb.astype(np.float32)
    lum = 0.2126 * rgb_f[..., 0] + 0.7152 * rgb_f[..., 1] + 0.0722 * rgb_f[..., 2]

    ii_avoid = cv2.integral(avoid, sdepth=cv2.CV_64F)
    ii_lum = cv2.integral(lum, sdepth=cv2.CV_64F)
    return ii_avoid, ii_lum, scale, (small_w, sh)


def avoid_integral_cached(key: str, bg_rgb: Image.Image):
    cached = lru_get(AVOID_CACHE, key)
    if cached is not None:
        return cached
    payload = compute_avoid_integral_cv(bg_rgb)
    lru_set(AVOID_CACHE, key, payload, AVOID_CACHE_MAX)
    return payload


def paste_marquee_with_glow(canvas_rgb: Image.Image, marquee_rgba: Image.Image, x: int, y: int):
    cw, ch = canvas_rgb.size
    mw, mh = marquee_rgba.size

    mar_lum = marquee_luminance(marquee_rgba)
    layer = Image.new("RGBA", (cw, ch), (0, 0, 0, 0))

    blur_radius = 26
    glow_pad = blur_radius * 3

    padded = Image.new("RGBA", (mw + 2 * glow_pad, mh + 2 * glow_pad), (0, 0, 0, 0))
    padded.paste(marquee_rgba, (glow_pad, glow_pad), marquee_rgba)

    glow = padded.filter(ImageFilter.GaussianBlur(radius=blur_radius))

    if mar_lum >= 140:
        glow = ImageEnhance.Brightness(glow).enhance(1.35)
    else:
        glow = ImageEnhance.Brightness(glow).enhance(0.55)

    r, g, b, a = glow.split()
    a = a.point(lambda p: int(p * 0.62))
    glow = Image.merge("RGBA", (r, g, b, a))

    layer.paste(glow, (x - glow_pad, y - glow_pad), glow)
    layer.paste(marquee_rgba, (x, y), marquee_rgba)

    out = Image.alpha_composite(canvas_rgb.convert("RGBA"), layer).convert("RGB")
    canvas_rgb.paste(out)


def place_marquee_best_fit(canvas_rgb: Image.Image, mar_rgba: Image.Image, avoid_key: str):
    ii_avoid, ii_lum, scale, (sw, sh) = avoid_integral_cached(avoid_key, canvas_rgb)
    W, H = canvas_rgb.size

    max_w = int(TARGET_W * MARQUEE_MAX_W_FRAC)
    max_h = int(TARGET_H * MARQUEE_MAX_H_FRAC)
    avail_w0 = max(1, max_w - 2 * MARQUEE_PADDING)
    avail_h0 = max(1, max_h - 2 * MARQUEE_PADDING)

    best = None
    best_score = -1e30

    for s in MARQUEE_SCALES:
        box_w = max(1, int(avail_w0 * s))
        box_h = max(1, int(avail_h0 * s))

        mw, mh = mar_rgba.size
        if mw <= 0 or mh <= 0:
            continue

        if mw >= mh:
            tw = box_w
            sc = tw / float(mw)
            th = max(1, int(mh * sc))
        else:
            th = box_h
            sc = th / float(mh)
            tw = max(1, int(mw * sc))

        if tw > box_w:
            sc2 = box_w / float(tw)
            tw = int(tw * sc2)
            th = max(1, int(th * sc2))
        if th > box_h:
            sc2 = box_h / float(th)
            tw = max(1, int(tw * sc2))
            th = int(th * sc2)

        if tw < 8 or th < 8:
            continue

        mar_resized = mar_rgba.resize((tw, th), Image.LANCZOS)
        mar_lum = marquee_luminance(mar_resized)

        x_min = MARQUEE_PADDING
        y_min = MARQUEE_PADDING
        x_max = max(MARQUEE_PADDING, W - tw - MARQUEE_PADDING)
        y_max = max(MARQUEE_PADDING, H - th - MARQUEE_PADDING)
        if x_max <= x_min or y_max <= y_min:
            continue

        smw = max(1, int(tw * scale))
        smh = max(1, int(th * scale))

        sx_min = int(x_min * scale)
        sy_min = int(y_min * scale)
        sx_max = int(x_max * scale)
        sy_max = int(y_max * scale)

        sx_max = max(sx_min, min(sw - smw, sx_max))
        sy_max = max(sy_min, min(sh - smh, sy_max))
        if sx_max <= sx_min or sy_max <= sy_min:
            continue

        step_x = max(1, (sx_max - sx_min) // 14)
        step_y = max(1, (sy_max - sy_min) // 10)

        px = sx_min
        py = sy_min

        for sy in range(sy_min, sy_max + 1, step_y):
            for sx in range(sx_min, sx_max + 1, step_x):
                area = float(smw * smh) + 1e-6
                avoid_mean = rect_sum_integral(ii_avoid, sx, sy, sx + smw, sy + smh) / area
                L_mean = rect_sum_integral(ii_lum, sx, sy, sx + smw, sy + smh) / area
                contrast = abs(L_mean - mar_lum)

                dx = sx - px
                dy = sy - py
                pos_pen = 0.0013 * (dx * dx + dy * dy)

                score = (contrast * 1.0) - (avoid_mean * 320.0) - pos_pen

                if score > best_score:
                    best_score = score
                    best = (mar_resized, int(sx / scale), int(sy / scale))

    return best


def cached_composite_path(fanart_path: str, marquee_path: str | None) -> Path:
    fan_sig = file_sig(fanart_path)
    mar_sig = file_sig(marquee_path) if marquee_path else "none"
    key = make_cache_key(
        "composite",
        COMP_VERSION,
        f"{TARGET_W}x{TARGET_H}",
        f"pad={MARQUEE_PADDING}",
        f"mwf={MARQUEE_MAX_W_FRAC}",
        f"mhf={MARQUEE_MAX_H_FRAC}",
        f"scales={','.join(map(str, MARQUEE_SCALES))}",
        CROP_VERSION,
        PLACE_VERSION,
        fanart_path,
        fan_sig,
        str(marquee_path or ""),
        mar_sig,
    )
    return CACHE_DIR / f"{key}.png"


def make_composite_and_show(
    fanart_path: str,
    marquee_path: str | None,
    show_func: Callable[[str], None],
):
    out_cache = cached_composite_path(fanart_path, marquee_path)

    if CACHE_HIT_FASTPATH and out_cache.exists() and out_cache.stat().st_size > 0:
        show_func(str(out_cache))
        return

    with Image.open(fanart_path) as fan_source:
        fan = fan_source.convert("RGBA")

    sal_key = make_cache_key("sal", CROP_VERSION, fanart_path, file_sig(fanart_path))
    sal_payload = saliency_integral_cached(sal_key, fan)

    fan_region_aspect = TARGET_W / float(TARGET_H)
    fan_cropped = crop_to_aspect_by_saliency(fan, fan_region_aspect, sal_payload, bias_bottom=0.12)
    fan_final = resize_cover(fan_cropped, TARGET_W, TARGET_H)

    canvas = Image.new("RGB", (TARGET_W, TARGET_H), (0, 0, 0))
    canvas.paste(fan_final.convert("RGB"), (0, 0))

    if marquee_path and os.path.isfile(marquee_path):
        with Image.open(marquee_path) as marquee_source:
            mar = trim_transparent(marquee_source.convert("RGBA"))
        avoid_key = make_cache_key(
            "avoid",
            PLACE_VERSION,
            fanart_path,
            file_sig(fanart_path),
            f"{TARGET_W}x{TARGET_H}",
        )
        placed = place_marquee_best_fit(canvas, mar, avoid_key)
        if placed:
            mar_resized, mx, my = placed
            paste_marquee_with_glow(canvas, mar_resized, mx, my)

    canvas.save(str(out_cache), format="PNG")
    show_func(str(out_cache))
