"""Static layout, body-stability checks, and 320x240 home-screen previews for role1 packages."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import struct
from typing import Any

from PIL import Image, ImageDraw, ImageFont

from .role_archive import INTERACTIONS, ROLE1_MOTION
from .role_staging import _decode_frame


_MOTION_HEADER = struct.Struct("<4s6HI")
_CLIP = struct.Struct("<HH")
_MOTION_ENTRY = struct.Struct("<IIIHH")
_PET_HEADER = struct.Struct("<4sHHHHII")
_TIMELINE_V2_RECORD = struct.Struct("<BHBHBBIhh")


@dataclass(frozen=True)
class ScreenGeometry:
    screen_width: int
    screen_height: int
    motion_width: int
    motion_height: int
    right_data_x: int
    recommended_right_x: int
    header_safe_y: int
    menu_hit_right: int
    menu_icon_bottom: int
    footer_top_y: int
    anchor_screen_x: int
    anchor_screen_y: int
    max_align_delta: float
    max_idle_drift_x: int
    max_step_x: int
    max_step_y: int


@dataclass(frozen=True)
class FrameMetrics:
    left: int
    top: int
    right: int
    bottom: int
    median_x: int
    median_y: int


def _geometry_for_motion(width: int, height: int) -> ScreenGeometry:
    scale = width / 184.0
    return ScreenGeometry(
        screen_width=round(320 * scale),
        screen_height=round(240 * scale),
        motion_width=width,
        motion_height=height,
        right_data_x=round(176 * scale),
        recommended_right_x=round(168 * scale),
        header_safe_y=round(20 * scale),
        menu_hit_right=round(34 * scale),
        menu_icon_bottom=round(24 * scale),
        footer_top_y=height,
        anchor_screen_x=round(92 * scale),
        anchor_screen_y=round(184 * scale),
        max_align_delta=round(12.0 * scale, 1),
        max_idle_drift_x=round(16 * scale),
        max_step_x=round(18 * scale),
        max_step_y=round(22 * scale),
    )


def _rgba565(packed: int) -> tuple[int, int, int]:
    r, g, b = (packed >> 11) & 31, (packed >> 5) & 63, packed & 31
    return ((r << 3) | (r >> 2), (g << 2) | (g >> 4), (b << 3) | (b >> 2))


def _decode_pet_frame(raw: bytes, width: int, height: int) -> Image.Image:
    palette = bytearray()
    for rgb, alpha in struct.iter_unpack("<HB", raw[:768]):
        palette.extend((*_rgba565(rgb), alpha))
    indexed = Image.frombytes("P", (width, height), raw[768:])
    indexed.putpalette(palette, rawmode="RGBA")
    return indexed.convert("RGBA")


def _decode_land_rgb(path: Path, screen_width: int = 320, screen_height: int = 240) -> Image.Image:
    if not path.is_file():
        return Image.new("RGB", (screen_width, screen_height), "#243b44")
    data = path.read_bytes()
    if len(data) != 512 + 336 * 240:
        return Image.new("RGB", (screen_width, screen_height), "#243b44")
    palette = [_rgba565(color) for (color,) in struct.iter_unpack("<H", data[:512])]
    rgb = bytearray(320 * 240 * 3)
    out = 0
    for y in range(240):
        row = 512 + y * 336 + 8
        for index in data[row:row + 320]:
            r, g, b = palette[index]
            rgb[out] = r
            rgb[out + 1] = g
            rgb[out + 2] = b
            out += 3
    image = Image.frombytes("RGB", (320, 240), bytes(rgb))
    if (screen_width, screen_height) != (320, 240):
        return image.resize((screen_width, screen_height), Image.Resampling.BILINEAR)
    return image


def _weighted_quantile(pairs: list[tuple[int, int]], ratio: float) -> int:
    threshold = sum(weight for _, weight in pairs) * ratio
    accumulated = 0
    for position, weight in pairs:
        accumulated += weight
        if accumulated >= threshold:
            return position
    return pairs[-1][0]


def _measure_frame(image: Image.Image, origin_x: int, origin_y: int) -> FrameMetrics | None:
    width, height = image.size
    alpha = image.getchannel("A").tobytes()
    col_weights = [0] * width
    row_weights = [0] * height
    for y in range(height):
        row_offset = y * width
        row_sum = 0
        for x in range(width):
            value = alpha[row_offset + x]
            if value >= 16:
                col_weights[x] += value
                row_sum += value
        row_weights[y] = row_sum
    if not sum(row_weights):
        return None
    xs = [(x + origin_x, weight) for x, weight in enumerate(col_weights) if weight]
    ys = [(y + origin_y, weight) for y, weight in enumerate(row_weights) if weight]
    return FrameMetrics(
        left=xs[0][0],
        top=ys[0][0],
        right=xs[-1][0] + 1,
        bottom=ys[-1][0] + 1,
        median_x=_weighted_quantile(xs, 0.5),
        median_y=_weighted_quantile(ys, 0.5),
    )


def _read_motion(path: Path) -> dict[str, Any]:
    data = path.read_bytes()
    _, version, width, height, clip_count, frame_count, burst_count, _ = _MOTION_HEADER.unpack_from(data)
    segments = list(_CLIP.iter_unpack(data[20:20 + clip_count * 4]))
    table = 20 + clip_count * 4 + (44 if version == 3 else 0)
    frames: list[tuple[Image.Image, int]] = []
    for index in range(frame_count):
        offset, length, _, duration_ms, _ = _MOTION_ENTRY.unpack_from(data, table + index * _MOTION_ENTRY.size)
        frames.append((_decode_frame(data[offset:offset + length], width, height), duration_ms))
    moving_count = sum(count for _, count in segments)
    return {
        "version": version,
        "width": width,
        "height": height,
        "segments": segments,
        "moving_count": moving_count,
        "burst_count": burst_count,
        "frames": frames,
    }


def _read_interactions(pet_dir: Path) -> dict[str, Any]:
    frames_bytes = (pet_dir / "frames.bin").read_bytes()
    _, _, width, height, action_count, frame_count, _ = _PET_HEADER.unpack_from(frames_bytes)
    stride = 768 + width * height
    frames = [
        _decode_pet_frame(frames_bytes[20 + idx * stride:20 + (idx + 1) * stride], width, height)
        for idx in range(frame_count)
    ]
    timeline_bytes = (pet_dir / "timeline-v2.bin").read_bytes()
    actions = []
    for index in range(action_count):
        rec_offset = 20 + index * 80
        _, _, count, duration_ms, loop, _, _, anchor_x, anchor_y = _TIMELINE_V2_RECORD.unpack_from(
            timeline_bytes, rec_offset
        )
        order = struct.unpack_from("<32H", timeline_bytes, rec_offset + 16)[:count]
        actions.append({
            "name": INTERACTIONS[index],
            "order": order,
            "duration_ms": duration_ms,
            "loop": bool(loop),
            "anchor": (anchor_x, anchor_y),
        })
    return {"width": width, "height": height, "frames": frames, "actions": actions}


def inspect_role_layout(staged_root: Path) -> dict[str, Any]:
    """Inspect home-screen bounds and multi-frame core stability of a validated role directory."""
    staged_root = Path(staged_root)
    motion_path = staged_root / ROLE1_MOTION
    if not motion_path.is_file():
        return {"status": "skipped", "reason": "not_role1"}

    motion = _read_motion(motion_path)
    geom = _geometry_for_motion(motion["width"], motion["height"])
    interactions = _read_interactions(staged_root / "pet-v2")

    non_burst_count = len(motion["frames"]) - motion["burst_count"]
    motion_metrics = [_measure_frame(img, 0, 0) for img, _ in motion["frames"][:non_burst_count]]

    action_sequences: list[tuple[str, list[FrameMetrics], bool]] = []
    all_metrics: list[FrameMetrics] = [m for m in motion_metrics if m is not None]

    for gear_idx, (first, count) in enumerate(motion["segments"]):
        name = f"dance_{gear_idx + 1}" if gear_idx < 5 else f"glide_{gear_idx - 4}"
        seq = [m for m in motion_metrics[first:first + count] if m is not None]
        if seq:
            action_sequences.append((name, seq, True))

    idle_seq = [m for m in motion_metrics[motion["moving_count"]:] if m is not None]

    interaction_metrics_by_name: dict[str, list[FrameMetrics]] = {}
    for action in interactions["actions"]:
        origin_x = geom.anchor_screen_x - action["anchor"][0]
        origin_y = geom.anchor_screen_y - action["anchor"][1]
        seq = []
        for frame_idx in action["order"]:
            metric = _measure_frame(interactions["frames"][frame_idx], origin_x, origin_y)
            if metric is not None:
                seq.append(metric)
                all_metrics.append(metric)
        if seq:
            interaction_metrics_by_name[action["name"]] = seq
            action_sequences.append((action["name"], seq, action["loop"]))

    min_left = min(m.left for m in all_metrics)
    min_top = min(m.top for m in all_metrics)
    max_right = max(m.right for m in all_metrics)
    max_bottom = max(m.bottom for m in all_metrics)
    right_clearance = geom.right_data_x - max_right

    warnings: list[dict[str, Any]] = []
    if max_right > geom.right_data_x:
        warnings.append({
            "code": "right_data_overlap",
            "message": f"角色可见像素延伸至 x={max_right}，进入右侧数据栏（x>={geom.right_data_x}），会被数据或提醒遮挡；建议控制在 x<={geom.recommended_right_x}。",
        })
    if min_top < geom.header_safe_y or any(m.left < geom.menu_hit_right and m.top < geom.menu_icon_bottom for m in all_metrics):
        warnings.append({
            "code": "top_header_overlap",
            "message": f"角色顶部延伸至 y={min_top}，靠近或遮挡左上菜单/顶栏时钟区域；建议保持 y>={geom.menu_icon_bottom}。",
        })
    if max_bottom > geom.footer_top_y:
        warnings.append({
            "code": "bottom_footer_overlap",
            "message": f"角色底部延伸至 y={max_bottom}，进入底部趋势折线区（y>={geom.footer_top_y}）。",
        })

    idle_drift_x = (max(m.median_x for m in idle_seq) - min(m.median_x for m in idle_seq)) if idle_seq else 0
    if idle_drift_x > geom.max_idle_drift_x:
        warnings.append({
            "code": "idle_body_drift",
            "message": f"待机多帧主体横向中心漂移 {idle_drift_x}px（上限 {geom.max_idle_drift_x}px），请固定角色脚底/身体锚点，不要按单帧透明边界重新居中。",
        })

    align_dx = 0.0
    align_dy = 0.0
    rest_seq = interaction_metrics_by_name.get("idle_blink") or interaction_metrics_by_name.get("idle_breathe")
    if idle_seq and rest_seq:
        idle_cx = sum(m.median_x for m in idle_seq) / len(idle_seq)
        idle_cy = sum(m.median_y for m in idle_seq) / len(idle_seq)
        rest_cx = sum(m.median_x for m in rest_seq) / len(rest_seq)
        rest_cy = sum(m.median_y for m in rest_seq) / len(rest_seq)
        align_dx = round(abs(idle_cx - rest_cx), 1)
        align_dy = round(abs(idle_cy - rest_cy), 1)
        if align_dx > geom.max_align_delta or align_dy > geom.max_align_delta:
            warnings.append({
                "code": "interaction_layer_misaligned",
                "message": (
                    f"运动层与互动层主体位置相差 ({align_dx}, {align_dy})px；"
                    "请确保 pet-v2 互动帧与运动画布使用同一屏幕角色坐标（标准 320x240 下从 (4, 25, 180, 185) 裁切）。"
                ),
            })

    max_step_x = 0
    max_step_y = 0
    worst_step_action = ""
    for action_name, seq, looped in action_sequences:
        if len(seq) < 2:
            continue
        pairs = list(zip(seq, seq[1:] + (seq[:1] if looped else [])))
        for prev_m, next_m in pairs:
            dx = abs(prev_m.median_x - next_m.median_x)
            dy = abs(prev_m.median_y - next_m.median_y)
            if dx > max_step_x or (dx == max_step_x and dy > max_step_y):
                max_step_x = dx
                worst_step_action = action_name
            if dy > max_step_y:
                max_step_y = dy
                if not worst_step_action:
                    worst_step_action = action_name

    if max_step_x > geom.max_step_x or max_step_y > geom.max_step_y:
        warnings.append({
            "code": "frame_body_jump",
            "action": worst_step_action,
            "message": (
                f"动作 {worst_step_action!r} 相邻帧主体中心跳变 ({max_step_x}, {max_step_y})px；"
                "手脚伸展可自然变化，但主体躯干与支点应保持连续，避免逐帧自动裁切或不等比缩放。"
            ),
        })

    return {
        "status": "ok" if not warnings else "warning",
        "screen_size": [geom.screen_width, geom.screen_height],
        "safe_zone": {
            "recommended_x": [8, geom.recommended_right_x],
            "right_data_x": geom.right_data_x,
            "recommended_y": [geom.menu_icon_bottom, geom.footer_top_y - 2],
        },
        "bounds": [min_left, min_top, max_right, max_bottom],
        "right_clearance_px": right_clearance,
        "stability": {
            "idle_drift_x": idle_drift_x,
            "motion_interaction_offset": [align_dx, align_dy],
            "max_adjacent_step": [max_step_x, max_step_y],
        },
        "warnings": warnings,
    }


def _load_preview_font(size: int) -> ImageFont.ImageFont | ImageFont.FreeTypeFont:
    bundled = Path(__file__).resolve().parent / "assets/fonts/NotoSansCJKsc-Regular.otf"
    if bundled.is_file():
        return ImageFont.truetype(str(bundled), size=size)
    return ImageFont.load_default(size=size)


def _draw_dashboard_overlay(canvas: Image.Image) -> None:
    """Draw the 320x240 DeskPet home header, right-column data, reminders, and trend ABOVE role artwork."""
    width, height = canvas.size
    scale = width / 320.0
    s = lambda v: round(v * scale)

    shade = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    shade_draw = ImageDraw.Draw(shade)
    shade_draw.rectangle((s(172), 0, width - 1, height - 1), fill=(8, 16, 22, 84))
    shade_draw.rectangle((0, s(192), width - 1, height - 1), fill=(8, 16, 22, 108))
    canvas.alpha_composite(shade)

    draw = ImageDraw.Draw(canvas)
    font_sm = _load_preview_font(max(9, s(11)))
    font_md = _load_preview_font(max(11, s(13)))
    font_lg = _load_preview_font(max(14, s(19)))

    # Top-left 2x2 menu mark, clock, inbox, and top-right weather.
    for idx in range(4):
        mx = s(12 + (idx % 2) * 11)
        my = s(8 + (idx // 2) * 11)
        draw.rounded_rectangle((mx, my, mx + s(7), my + s(7)), radius=max(1, s(1)), fill="#ffffff")
    draw.rounded_rectangle((s(78), s(9), s(102), s(24)), radius=max(1, s(2)), outline="#ffffff", width=1)
    draw.text((s(114), s(8)), "10:42", fill="#ffffff", font=font_md)
    draw.ellipse((s(243), s(6), s(253), s(16)), fill="#ffd257")
    draw.text((s(258), s(4)), "22°C", fill="#ffffff", font=font_md)
    draw.text((s(252), s(18)), "Suzhou", fill="#d8e6ef", font=font_sm)

    # Right-column notification cards (x=176..314, y=36..99).
    draw.rounded_rectangle((s(176), s(36), s(314), s(65)), radius=max(2, s(6)), fill="#ffe5c2")
    draw.text((s(186), s(43)), "2 个未读", fill="#142b3b", font=font_md)
    draw.rounded_rectangle((s(176), s(69), s(314), s(98)), radius=max(2, s(6)), fill="#ffdad2")
    draw.text((s(186), s(76)), "1 条待应答", fill="#142b3b", font=font_md)

    # Right-column CPU/MEM, speed, quota, credits, and reset countdown (x=176..316, y=101..206).
    draw.text((s(246), s(102)), "CPU 24%  MEM 61%", fill="#8ce0ff", font=font_sm, anchor="mt")
    draw.text((s(246), s(116)), "48.6 t/s", fill="#8ce0ff", font=font_lg, anchor="mt")
    draw.text((s(210), s(150)), "5H 82%", fill="#ffffff", font=font_sm, anchor="mt")
    draw.text((s(281), s(150)), "7D 64%", fill="#ffffff", font=font_sm, anchor="mt")
    draw.rectangle((s(182), s(167), s(238), s(170)), fill="#2a4c5e")
    draw.rectangle((s(182), s(167), s(228), s(170)), fill="#4998ff")
    draw.rectangle((s(253), s(167), s(309), s(170)), fill="#2a4c5e")
    draw.rectangle((s(253), s(167), s(289), s(170)), fill="#4998ff")
    draw.text((s(246), s(173)), "积分 128 | 重置 2", fill="#d8e6ef", font=font_sm, anchor="mt")
    draw.text((s(246), s(190)), "还剩 4 天 12 小时", fill="#8ce0ff", font=font_sm, anchor="mt")

    # Bottom 60s speed trend (y=207..222) and POWER footer (y=229..240).
    points = [
        (s(8 + i * 5), s(220 - ((i * 7) % 11 + 2)))
        for i in range(60)
    ]
    draw.line(points, fill="#6ee7ff", width=max(1, s(1)))
    draw.text((s(8), s(228)), "POWER  G5", fill="#d8e6ef", font=font_sm)
    draw.text((s(312), s(228)), "3 SESS", fill="#8ce0ff", font=font_sm, anchor="rt")


def _compose_home_frame(
    backdrop: Image.Image,
    sprite: Image.Image,
    origin_x: int,
    origin_y: int,
    *,
    effect: Image.Image | None = None,
) -> Image.Image:
    canvas = backdrop.convert("RGBA")
    if effect is not None:
        canvas.alpha_composite(effect, (0, 0))
    layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    layer.paste(sprite, (origin_x, origin_y))
    canvas.alpha_composite(layer)
    # Dashboard data always renders ABOVE the character image.
    _draw_dashboard_overlay(canvas)
    return canvas.convert("RGB")


def render_role_home_previews(staged_root: Path, output_dir: Path) -> dict[str, str]:
    """Render 320x240 home-screen composite PNGs/GIFs and interaction sheet for visual acceptance."""
    staged_root = Path(staged_root)
    output_dir = Path(output_dir).expanduser().resolve()
    if output_dir.exists() and not output_dir.is_dir():
        raise ValueError("preview_dir 必须是目录路径。")
    output_dir.mkdir(parents=True, exist_ok=True)

    motion = _read_motion(staged_root / ROLE1_MOTION)
    geom = _geometry_for_motion(motion["width"], motion["height"])
    interactions = _read_interactions(staged_root / "pet-v2")
    backdrop = _decode_land_rgb(staged_root / "land.bin", geom.screen_width, geom.screen_height)

    frames = motion["frames"]
    idle_start = motion["moving_count"]
    idle_end = len(frames) - motion["burst_count"]
    idle_frames = [_compose_home_frame(backdrop, img, 0, 0) for img, _ in frames[idle_start:idle_end]]
    idle_durations = [max(40, ms) for _, ms in frames[idle_start:idle_end]]

    gear5_first, gear5_count = motion["segments"][4]
    gear5_frames = [_compose_home_frame(backdrop, img, 0, 0) for img, _ in frames[gear5_first:gear5_first + gear5_count]]
    gear5_durations = [max(40, ms) for _, ms in frames[gear5_first:gear5_first + gear5_count]]

    burst_effect_img = frames[-motion["burst_count"]][0]
    burst_frame = _compose_home_frame(backdrop, frames[gear5_first][0], 0, 0, effect=burst_effect_img)

    by_name = {act["name"]: act for act in interactions["actions"]}
    touched_act = by_name["touched"]
    tx = geom.anchor_screen_x - touched_act["anchor"][0]
    ty = geom.anchor_screen_y - touched_act["anchor"][1]
    touched_frames = [_compose_home_frame(backdrop, interactions["frames"][idx], tx, ty) for idx in touched_act["order"]]
    step_ms = max(40, touched_act["duration_ms"] // max(1, len(touched_frames)))

    idle_png = output_dir / "home-idle.png"
    idle_gif = output_dir / "home-idle.gif"
    gear5_png = output_dir / "home-dance-5.png"
    gear5_gif = output_dir / "home-dance-5.gif"
    burst_png = output_dir / "home-burst.png"
    touched_gif = output_dir / "home-touched.gif"
    sheet_png = output_dir / "interactions-sheet.png"

    idle_frames[0].save(idle_png)
    idle_frames[0].save(idle_gif, save_all=True, append_images=idle_frames[1:], duration=idle_durations, loop=0)
    gear5_frames[0].save(gear5_png)
    gear5_frames[0].save(gear5_gif, save_all=True, append_images=gear5_frames[1:], duration=gear5_durations, loop=0)
    burst_frame.save(burst_png)
    touched_frames[0].save(
        touched_gif,
        save_all=True,
        append_images=touched_frames[1:],
        duration=[step_ms] * len(touched_frames),
        loop=0,
    )

    # 3x4 contact sheet for the 12 core interactions at representative mid-action pose.
    cols, rows = 3, 4
    pad, label_h = 8, 20
    cell_w, cell_h = geom.screen_width, geom.screen_height + label_h
    sheet = Image.new("RGB", (cols * cell_w + (cols + 1) * pad, rows * cell_h + (rows + 1) * pad), "#f4f1ea")
    sheet_draw = ImageDraw.Draw(sheet)
    label_font = _load_preview_font(12)
    for idx, name in enumerate(INTERACTIONS[:12]):
        act = by_name[name]
        ox = geom.anchor_screen_x - act["anchor"][0]
        oy = geom.anchor_screen_y - act["anchor"][1]
        mid_frame_idx = act["order"][len(act["order"]) // 2]
        composed = _compose_home_frame(backdrop, interactions["frames"][mid_frame_idx], ox, oy)
        col, row = idx % cols, idx // cols
        px = pad + col * (cell_w + pad)
        py = pad + row * (cell_h + pad)
        sheet_draw.text((px + 4, py + 2), name, fill="#1c3038", font=label_font)
        sheet.paste(composed, (px, py + label_h))
    sheet.save(sheet_png)

    return {
        "home_idle_png": str(idle_png),
        "home_idle_gif": str(idle_gif),
        "home_dance_5_png": str(gear5_png),
        "home_dance_5_gif": str(gear5_gif),
        "home_burst_png": str(burst_png),
        "home_touched_gif": str(touched_gif),
        "interactions_sheet_png": str(sheet_png),
    }
