import asyncio
from io import BytesIO
import json
from pathlib import Path
import tempfile
import unittest
from zipfile import ZIP_DEFLATED, ZipFile

from PIL import Image, ImageDraw

from deskpet_platform.mcp_server import create_server
from deskpet_platform.role_example import (
    IDENTITY,
    INTERACTION_BOX,
    MOTION_SIZE,
    NAME,
    burst_effect,
    catalog_row,
    create_role_example,
    indexed_palette,
    interaction_files,
    motion_cel,
    motion_file,
    pose,
    scenery,
)
from deskpet_platform.role_layout import inspect_role_layout, render_role_home_previews
from deskpet_platform.role_staging import stage_role


def _build_custom_role_zip(
    destination: Path,
    *,
    idle_poses: list[Image.Image],
    gear_poses: list[Image.Image] | None = None,
    interaction_poses: list[Image.Image] | None = None,
) -> Path:
    gears = [[(motion_cel(img), 150) for img in (gear_poses or idle_poses[:2])] for _ in range(5)]
    idle = [(motion_cel(img), 300) for img in idle_poses]
    motion = motion_file(gears, idle, [(motion_cel(burst_effect()), 400)])
    base_interactions = interaction_poses or [idle_poses[0], idle_poses[1], idle_poses[0], idle_poses[-1]]
    clips = {
        "idle_breathe": ((0, 1, 0), 1200, True),
        "idle_blink": ((0, 1, 0), 420, False),
        "wake": ((1, 2, 0), 720, False),
        "focus": ((0,), 900, True),
        "raise_hand": ((0, 3, 3, 0), 1200, False),
        "show_approval": ((0, 2, 2, 0), 1000, False),
        "point_list": ((0, 3, 0), 900, False),
        "wait_reply": ((0, 1, 0), 1800, True),
        "celebrate_small": ((0, 2, 3, 2, 0), 1300, False),
        "startled": ((2, 1, 0), 600, False),
        "touched": ((0, 3, 2, 0), 960, False),
        "disconnected": ((0, 1, 1), 1500, True),
    }
    frames, timeline = interaction_files(base_interactions, clips)
    bg_indexed, bg_palette = indexed_palette(scenery())
    payload = {
        "manifest.json": json.dumps({"version": 1, "id": IDENTITY, "name": NAME}, ensure_ascii=False),
        "kind": b"role1\n",
        "row.565": catalog_row(idle_poses[0]),
        "land.bin": bg_palette + bg_indexed.tobytes(),
        "motion.bin": motion,
        "pet-v2/frames.bin": frames,
        "pet-v2/timeline-v2.bin": timeline,
    }
    buf = BytesIO()
    with ZipFile(buf, "w", compression=ZIP_DEFLATED) as zf:
        for name, content in payload.items():
            zf.writestr(name, content)
    destination.write_bytes(buf.getvalue())
    return destination


class RoleLayoutTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.work = Path(directory.name)

    def test_pocket_bot_and_natural_hand_wave_pass_without_requiring_identical_bboxes(self):
        # Natural limb extension changes transparent bounding box by ~20px while keeping torso stable.
        base = pose()
        blink = pose(blink=True)
        big_wave = pose(wave=True)
        draw = ImageDraw.Draw(big_wave)
        draw.rounded_rectangle((146, 78, 166, 108), 5, fill="#42d4b3")  # Extended hand within x<=166.
        archive = _build_custom_role_zip(
            self.work / "natural-wave.zip",
            idle_poses=[base, blink, pose(smile=True)],
            gear_poses=[pose(lift=4, stride=-3), big_wave],
            interaction_poses=[base, blink, pose(smile=True), big_wave],
        )
        staged = stage_role(archive, self.work / "staged")
        report = inspect_role_layout(staged)
        self.assertEqual(report["status"], "ok", report)
        self.assertEqual(report["warnings"], [])
        self.assertGreaterEqual(report["right_clearance_px"], 9)

        previews = render_role_home_previews(staged, self.work / "previews")
        for path_str in previews.values():
            self.assertTrue(Path(path_str).is_file(), path_str)
        with Image.open(previews["home_idle_png"]) as home_img:
            self.assertEqual(home_img.size, (320, 240))
        with Image.open(previews["interactions_sheet_png"]) as sheet_img:
            self.assertGreater(sheet_img.width, 960)
            self.assertGreater(sheet_img.height, 960)

    def test_detects_right_overlap_header_overlap_body_jump_and_layer_misalignment(self):
        # 1. Right-column overlap (x reaches 182 >= 176) + top header overlap (y reaches 10 < 20).
        crowded = pose()
        draw = ImageDraw.Draw(crowded)
        draw.rectangle((160, 10, 182, 140), fill="#ff5577")
        # 2. Body jump: frame 2 shifts the whole body 34px left.
        jumped = Image.new("RGBA", MOTION_SIZE)
        jumped.paste(pose(), (-34, 0))
        # 3. Interaction misalignment: shift interaction pose down by 24px before cropping INTERACTION_BOX.
        shifted_interaction = Image.new("RGBA", MOTION_SIZE)
        shifted_interaction.paste(pose(), (0, 24))

        archive = _build_custom_role_zip(
            self.work / "flawed.zip",
            idle_poses=[crowded, jumped, pose(smile=True)],
            gear_poses=[crowded, jumped],
            interaction_poses=[shifted_interaction, shifted_interaction, shifted_interaction, shifted_interaction],
        )
        staged = stage_role(archive, self.work / "staged-flawed")
        report = inspect_role_layout(staged)
        self.assertEqual(report["status"], "warning")
        codes = {w["code"] for w in report["warnings"]}
        self.assertIn("right_data_overlap", codes)
        self.assertIn("top_header_overlap", codes)
        self.assertIn("idle_body_drift", codes)
        self.assertIn("frame_body_jump", codes)
        self.assertIn("interaction_layer_misaligned", codes)

        # Verify preview still renders right-column notifications ABOVE encroaching artwork.
        previews = render_role_home_previews(staged, self.work / "flawed-previews")
        with Image.open(previews["home_idle_png"]) as rendered:
            # (180, 50) lies inside both the encroaching red bar (x<=182) and notification pill #1 (x=176..314, y=36..65).
            r, g, b = rendered.getpixel((180, 50))
            self.assertGreater(g, 200)  # Warm cream pill (#ffe5c2) sits on top of red (#ff5577) sprite.

    def test_mcp_check_role_returns_layout_and_optional_previews(self):
        archive_info = create_role_example(self.work / "pocket-bot.zip")
        server = create_server(self.work / "device.json", no_token_probe=True)

        async def call():
            _, structured = await server.call_tool(
                "desk_check_role",
                {"source": archive_info["path"], "preview_dir": str(self.work / "mcp-previews")},
            )
            return structured

        result = asyncio.run(call())
        self.assertEqual(result["status"], "valid")
        self.assertEqual(result["layout"]["status"], "ok")
        self.assertTrue(Path(result["previews"]["home_dance_5_gif"]).is_file())


if __name__ == "__main__":
    unittest.main()
