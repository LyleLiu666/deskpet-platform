import tempfile
from pathlib import Path
import shutil
import struct
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
import zlib
from zipfile import ZipFile

from deskpet_platform.channel import AckTimeout, CommandRejected
from deskpet_platform.mcp_server import BoardSession
from deskpet_platform.pairing import DeviceConfig
from deskpet_platform.role_install import check_unique, prepare_role
from deskpet_platform.role_sources import list_role_sources
from deskpet_platform.role_staging import prepare_role_source, stage_role
from test_role_archive import make_archive
from test_role_pack import make_pack, write_role_actions


class RoleInstallTests(unittest.TestCase):
    def test_legacy_backup_recovers_identity_from_card_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "sample-role"
            pack = source / "pet-v2"
            pack.mkdir(parents=True)
            make_pack(pack)
            write_role_actions(source)
            (source / "events.json").write_text('{"turn_finished":"wake"}')
            original = prepare_role(source)
            backup = root / "roles" / original.identity / original.version
            shutil.copytree(source, backup)
            (backup / "events.json").unlink()
            (backup / "event.txt").write_bytes(b"wake\n")
            listed = list_role_sources(backup.parent)["roles"]
            self.assertEqual(listed, [{"id": original.identity, "path": str(backup), "format": "PET1"}])
            restored = prepare_role_source(backup, root / "staged")
            self.assertEqual((restored.identity, restored.version, restored.finish_action),
                             (original.identity, original.version, "wake"))

            detached = root / original.version
            shutil.copytree(backup, detached)
            with self.assertRaisesRegex(ValueError, "角色 ID"):
                prepare_role_source(detached, root / "detached")

    def test_plain_root_manifest_does_not_change_legacy_role_format(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "legacy"
            pack = source / "pet-v2"
            pack.mkdir(parents=True)
            make_pack(pack)
            write_role_actions(source)
            (source / "manifest.json").write_text('{"name":"Legacy pet"}')
            listed = list_role_sources(root)["roles"]
            self.assertEqual(listed, [{"id": "legacy", "path": str(source), "format": "PET1"}])
            role = prepare_role_source(source, root / "staged")
            self.assertFalse(role.portable)
            self.assertIn("manifest.json", [item.relative for item in role.files])

            (source / "manifest.json").write_text('{"version":1,"id":"user-legacy","name":"Legacy pet"}')
            with self.assertRaisesRegex(ValueError, "缺少必需资源.*kind"):
                prepare_role_source(source, root / "broken-role1")

    def test_v2_timeline_rejects_invalid_action_records_with_valid_checksums(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "sample-role"
            pack = root / "pet-v2"
            pack.mkdir(parents=True)
            make_pack(pack)
            write_role_actions(root)
            timeline = pack / "timeline-v2.bin"
            original = timeline.read_bytes()
            for name, offset, value in (
                ("action index", 0, 1), ("first frame", 1, 12), ("frame count", 3, 0),
                ("duration", 5, 255), ("loop", 6, 2), ("fallback", 7, 12),
                ("safe frame", 8, 2), ("anchor x", 12, 176), ("anchor y", 14, 160),
                ("ordered frame", 16, 12),
            ):
                with self.subTest(field=name):
                    data = bytearray(original)
                    data[20 + offset] = value
                    struct.pack_into("<I", data, 12, zlib.crc32(data[20:]))
                    timeline.write_bytes(data)
                    with self.assertRaises(ValueError):
                        prepare_role(root)

    def test_role_is_versioned_and_incomplete_or_corrupt_assets_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "sample-role"
            pack = root / "pet-v2"
            pack.mkdir(parents=True)
            make_pack(pack)
            write_role_actions(root)
            role = prepare_role(root)
            self.assertEqual(role.identity, "sample-role")
            self.assertEqual({item.relative for item in role.files},
                             {"pet-v2/frames.bin", "pet-v2/timeline-v2.bin"})
            self.assertEqual(prepare_role(root).version, role.version)
            self.assertEqual(len(role.actions), 12)
            (root / "events.json").write_text('{"turn_finished":"not_available"}')
            with self.assertRaises(ValueError):
                prepare_role(root)
            (root / "events.json").unlink()

            timeline = pack / "timeline-v2.bin"
            original = timeline.read_bytes()
            timeline.write_bytes(original[:-1] + bytes([original[-1] ^ 1]))
            with self.assertRaises(ValueError):
                prepare_role(root)
            timeline.write_bytes(original)
            (pack / "frames.bin").unlink()
            with self.assertRaises(ValueError):
                prepare_role(root)

    def test_role_rejects_symlinks_and_case_collisions(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "sample-role"
            pack = root / "pet-v2"
            pack.mkdir(parents=True)
            make_pack(pack)
            write_role_actions(root)
            (root / "shortcut").symlink_to(pack / "frames.bin")
            with self.assertRaises(ValueError):
                prepare_role(root)
            (root / "shortcut").unlink()
            seen = set()
            check_unique("pet-v2/Name", seen)
            with self.assertRaises(ValueError):
                check_unique("pet-v2/name", seen)

    def test_names_and_completion_action_come_from_the_role(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "custom-role"
            pack = root / "pet-v2"
            pack.mkdir(parents=True)
            make_pack(pack)
            with self.assertRaises(ValueError):
                prepare_role(root)
            names = ["idle", "deliver_result"] + [f"action_{index}" for index in range(10)]
            (root / "actions.txt").write_text("\n".join(names) + "\n")
            self.assertIsNone(prepare_role(root).finish_action)
            (root / "events.json").write_text('{"turn_finished":"deliver_result"}')
            self.assertEqual(prepare_role(root).finish_action, "deliver_result")

    def test_role_can_declare_more_than_thirty_actions(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "role"
            pack = root / "pet-v2"
            pack.mkdir(parents=True)
            make_pack(pack, width=4, height=3, actions=48, frame_count=630)
            names = [f"action_{index}" for index in range(48)]
            (root / "actions.txt").write_text("\n".join(names) + "\n")
            self.assertEqual(prepare_role(root).actions, tuple(names))


class RoleInstallFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_larger_motion_requires_device_capability_before_upload(self):
        from test_role_archive import make_archive, make_motion
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = make_archive(root, motion=make_motion(width=245, height=256))
            session = BoardSession(root / "device.json")
            board = SimpleNamespace(channel=SimpleNamespace(capabilities={
                "card_transfer": 1, "role_actions": 1, "role_zip_motion": 1}),
                upload_file=AsyncMock(), prepare_role=AsyncMock())
            session.client = board
            self.assertEqual(await session.install_role(archive),
                             {"status": "unsupported", "reason": "role_large_motion_not_supported"})
            board.upload_file.assert_not_awaited()

    async def test_custom_anchor_requires_device_capability_before_upload(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = make_archive(root)
            with ZipFile(archive) as package:
                contents = {name: package.read(name) for name in package.namelist()}
            timeline = bytearray(contents["pet-v2/timeline-v2.bin"])
            struct.pack_into("<hh", timeline, 20 + 12, 87, 159)
            struct.pack_into("<I", timeline, 12, zlib.crc32(timeline[20:]))
            contents["pet-v2/timeline-v2.bin"] = timeline
            with ZipFile(archive, "w") as package:
                for name, data in contents.items():
                    package.writestr(name, data)
            session = BoardSession(root / "device.json")
            board = SimpleNamespace(channel=SimpleNamespace(capabilities={
                "card_transfer": 1, "role_actions": 1, "role_zip_motion": 1}),
                upload_file=AsyncMock(), prepare_role=AsyncMock())
            session.client = board
            self.assertEqual(await session.install_role(archive),
                             {"status": "unsupported", "reason": "role_large_frames_not_supported"})
            board.upload_file.assert_not_awaited()

    async def test_larger_portable_frames_require_device_capability_before_upload(self):
        from test_role_archive import make_archive
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = make_archive(root, width=240, height=218)
            session = BoardSession(root / "device.json")
            board = SimpleNamespace(channel=SimpleNamespace(capabilities={
                "card_transfer": 1, "role_actions": 1, "role_zip_motion": 1}),
                upload_file=AsyncMock(), prepare_role=AsyncMock())
            session.client = board
            self.assertEqual(await session.install_role(archive),
                             {"status": "unsupported", "reason": "role_large_frames_not_supported"})
            board.upload_file.assert_not_awaited()

    async def test_install_rejects_pet1_before_upload_on_full_display_firmware(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "sample-role"
            pack = source / "pet-v2"
            pack.mkdir(parents=True)
            make_pack(pack)
            write_role_actions(source)
            session = BoardSession(root / "device.json")
            for extra in ({"role1_required": 1}, {"pet_view": True}):
                with self.subTest(capabilities=extra):
                    capabilities = {"card_transfer": 1, "role_actions": 1, "role_zip_motion": 1, **extra}
                    board = SimpleNamespace(channel=SimpleNamespace(capabilities=capabilities),
                                            upload_file=AsyncMock(), prepare_role=AsyncMock())
                    session.client = board
                    self.assertEqual(await session.install_role(source),
                                     {"status": "unsupported", "reason": "role1_required"})
                    board.upload_file.assert_not_awaited()
                    board.prepare_role.assert_not_awaited()

    async def test_role1_backup_restores_completion_action_and_reuses_version(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = make_archive(root)
            extracted = root / "extracted" / "user-archive"
            with ZipFile(archive) as package:
                package.extractall(extracted)
            staged = stage_role(archive, root / "staged")
            (staged / "events.json").write_text('{"turn_finished":"wake"}')
            original = prepare_role(staged)
            backup = root / "backups" / original.version
            backup.mkdir(parents=True)
            for item in original.files:
                destination = backup / item.relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(item.source, destination)
            (backup / "actions.txt").write_text("\n".join(original.actions) + "\n")
            (backup / "event.txt").write_bytes(b"wake\n")
            for source in (extracted, backup):
                with self.subTest(source=source):
                    discovered = list_role_sources(source.parent)["roles"]
                    self.assertEqual([(item["id"], item["format"]) for item in discovered],
                                     [(original.identity, "role1")])
            self.assertTrue(prepare_role_source(extracted, root / "from-zip-directory").portable)
            restored = prepare_role_source(backup, root / "restored")
            self.assertEqual((restored.identity, restored.version, restored.finish_action),
                             (original.identity, original.version, "wake"))

            session = BoardSession(root / "device.json")
            capabilities = {"card_transfer": 1, "role_actions": 1, "role_zip_motion": 1, "role1_required": 1}
            board = SimpleNamespace(channel=SimpleNamespace(capabilities=capabilities),
                                    upload_file=AsyncMock(return_value={}), prepare_role=AsyncMock(),
                                    status=AsyncMock(return_value={"role_state": "ready", "role_id": original.identity,
                                                                   "role_persisted": True}))
            session.client = board
            result = await session.install_role(backup)
            self.assertEqual((result["status"], result["version"], result["files"]),
                             ("ready", original.version, len(original.files)))
            board.prepare_role.assert_awaited_once_with(original.identity, original.version, original.actions, "wake")
            self.assertFalse(any(call.args[0].endswith(("/actions.txt", "/event.txt"))
                                 for call in board.upload_file.await_args_list))

            (backup / "event.txt").write_bytes(b"-\n")
            self.assertIsNone(prepare_role_source(backup, root / "silent").finish_action)
            (backup / "event.txt").write_bytes(b"no_such_action\n")
            with self.assertRaisesRegex(ValueError, "event\\.txt"):
                prepare_role_source(backup, root / "invalid-event")
            (backup / "event.txt").unlink()
            self.assertIsNone(prepare_role_source(backup, root / "missing-event").finish_action)

    async def test_install_rechecks_role_and_sends_no_files_when_resources_or_action_names_are_wrong(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = make_archive(root)
            staged = stage_role(archive, root / "staged")
            bundle = prepare_role(staged)
            session = BoardSession(root / "device.json")
            channel = SimpleNamespace(capabilities={"card_transfer": 1, "role_actions": 1, "role_zip_motion": 1})
            session.client = board = SimpleNamespace(channel=channel, upload_file=AsyncMock(), prepare_role=AsyncMock())
            background = staged / "land.bin"
            wrong_case = staged / "Land.bin"
            background.rename(wrong_case)
            with self.assertRaisesRegex(ValueError, "缺少.*land\\.bin.*命名.*Land\\.bin"):
                await session.install_role(str(staged))
            wrong_case.rename(background)
            (staged / "actions.txt").write_text("\n".join(("idle_other", *bundle.actions[1:])) + "\n",
                                                encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "idle_breathe.*idle_other"):
                await session.install_role(str(staged))
            board.upload_file.assert_not_awaited()
            board.prepare_role.assert_not_awaited()

    async def test_wifi_role_upload_resumes_only_the_unconfirmed_file(self):
        class Channel:
            capabilities = {"card_transfer": 1, "card_fetch_async": 1, "role_actions": 1, "compact_status": 1}

            def __init__(self):
                self.online = True

        class Link:
            wireless = True

        class Board:
            link = Link()
            device_id = "12345678-1234-4abc-8123-123456789abc"

            def __init__(self, config, replies):
                self.config = config
                self.replies = iter(replies)
                self.channel = Channel()
                self.uploaded = []
                self.activations = []

            async def __aenter__(self):
                return self

            async def close(self):
                self.channel.online = False

            async def upload_file(self, path, source, *, expected_sha256):
                self.uploaded.append((path, source, expected_sha256))
                outcome = next(self.replies)
                if isinstance(outcome, AckTimeout):
                    self.channel.online = False
                if isinstance(outcome, Exception):
                    raise outcome
                return {"path": path, "unchanged": outcome}

            async def prepare_role(self, identity, *_args):
                self.activations.append(identity)

            async def status(self, *, compact=False):
                return {"role_state": "ready", "role_id": "sample-role", "role_persisted": True}

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "sample-role"
            pack = root / "pet-v2"
            pack.mkdir(parents=True)
            make_pack(pack)
            write_role_actions(root)
            config = DeviceConfig("02:00:00:00:00:01", bytes(32))
            cases = ((True, False), (True, True), (CommandRejected("role_version_immutable"), False))
            for outcome, poll_reconnected in cases:
                with (self.subTest(outcome=outcome, poll_reconnected=poll_reconnected),
                      patch("deskpet_platform.mcp_server.asyncio.sleep")):
                    first = Board(config, [False, AckTimeout("card fetch status lost")])
                    replacement = Board(config, [CommandRejected("card_busy"), outcome])
                    configs = []

                    def reconnect(requested):
                        configs.append(requested)
                        return replacement

                    session = BoardSession(Path(directory) / "device.json", client_factory=reconnect)
                    session.client = first
                    if poll_reconnected:
                        upload = first.upload_file

                        async def upload_with_background_reconnect(*args, **kwargs):
                            try:
                                return await upload(*args, **kwargs)
                            except AckTimeout:
                                session.client = replacement
                                raise

                        first.upload_file = upload_with_background_reconnect
                    result = await session.install_role(str(root))
                    self.assertEqual(configs, [] if poll_reconnected else [config])
                    self.assertEqual(replacement.uploaded, [first.uploaded[-1], first.uploaded[-1]])
                    self.assertEqual(first.activations, [])
                    if outcome is True:
                        self.assertEqual(result["status"], "ready")
                        self.assertEqual(result["files"], 2)
                        self.assertEqual(replacement.activations, ["sample-role"])
                    else:
                        self.assertEqual(result["status"], "transfer_failed")
                        self.assertEqual(result["completed"], [{"path": first.uploaded[0][0], "unchanged": False}])
                        self.assertEqual(replacement.activations, [])

    async def test_role_install_confirms_the_accepted_activation_after_reconnecting(self):
        class Channel:
            capabilities = {"card_transfer": 1, "role_actions": 1, "compact_status": 1}

            def __init__(self, boot):
                self.boot = boot
                self.online = True

        class Link:
            wireless = True

        class Board:
            link = Link()
            device_id = "12345678-1234-4abc-8123-123456789abc"

            def __init__(self, boot, *, status_lost=False, role_state="ready", rollback_failed=False):
                self.channel = Channel(boot)
                self.status_lost = status_lost
                self.role_state = role_state
                self.rollback_failed = rollback_failed
                self.config = object()
                self.activated = []
                self.status_reads = 0

            async def __aenter__(self):
                return self

            async def close(self):
                self.channel.online = False

            async def upload_file(self, path, _source, *, expected_sha256):
                return {"path": path}

            async def prepare_role(self, identity, *_args):
                self.activated.append(identity)
                return {"role_state": "loading"}

            async def status(self, *, compact=False):
                self.status_reads += 1
                assert compact
                if self.status_lost:
                    self.channel.online = False
                    raise AckTimeout("activation heartbeat timed out")
                status = {"role_state": self.role_state, "role_id": "sample-role", "role_persisted": True}
                if self.rollback_failed:
                    status["role_rollback_ok"] = False
                return status

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "sample-role"
            pack = root / "pet-v2"
            pack.mkdir(parents=True)
            make_pack(pack)
            write_role_actions(root)
            cases = ((77, "ready", True, False, "ready"),
                     (77, "failed", True, False, "activation_failed"),
                     (77, "failed", True, True, "activation_failed"),
                     (78, "ready", True, False, "activation_unconfirmed"),
                     (77, "ready", False, False, "activation_unconfirmed"))
            for boot, role_state, same_device, rollback_failed, expected in cases:
                with self.subTest(boot=boot, role_state=role_state, same_device=same_device,
                                  rollback_failed=rollback_failed):
                    first = Board(77, status_lost=True)
                    replacement = Board(boot, role_state=role_state, rollback_failed=rollback_failed)
                    if not same_device:
                        replacement.device_id = "4a7a34c0-186d-4f84-a183-ef05537c7a8b"
                    configs = []

                    def reconnect(config):
                        configs.append(config)
                        return replacement

                    session = BoardSession(Path(directory) / "device.json", client_factory=reconnect)
                    session.client = first
                    result = await session.install_role(str(root))
                    self.assertEqual(result["status"], expected)
                    if rollback_failed:
                        self.assertIs(result["role_rollback_ok"], False)
                    else:
                        self.assertNotIn("role_rollback_ok", result)
                    self.assertEqual(configs, [first.config])
                    self.assertEqual(first.activated, ["sample-role"])
                    self.assertEqual(replacement.activated, [])
                    self.assertEqual(replacement.status_reads, int(same_device and boot == 77))
                    self.assertFalse(first.channel.online)
                    self.assertIs(session.client, replacement if same_device else None)

    async def test_wait_for_role_uses_compact_wifi_status(self):
        class Channel:
            capabilities = {"compact_status": 1}
        class Link:
            wireless = True
        class Board:
            channel, link = Channel(), Link()
            requests = []
            async def status(self, *, compact=False):
                self.requests.append(compact)
                return ({"role_state": "loading"} if len(self.requests) == 1 else
                        {"role_state": "ready", "role_id": "fish", "role_persisted": True})
        with tempfile.TemporaryDirectory() as directory:
            session = BoardSession(Path(directory) / "device.json")
            session.client = board = Board()
            result = await session.wait_role("fish")
        self.assertEqual(result["status"], "ready")
        self.assertEqual(board.requests, [True, True])

    async def test_activates_only_after_all_role_files_arrive(self):
        class Channel:
            online = True
            capabilities = {"card_transfer": 1, "role_actions": 1}

        class Board:
            channel = Channel()
            def __init__(self):
                self.uploaded = []
                self.activated = False
                self.reject = False
            async def upload_file(self, path, source, *, expected_sha256):
                self.uploaded.append(path)
                return {"path": path, "bytes": source.stat().st_size}
            async def prepare_role(self, identity, version, actions, finish_action):
                if self.reject:
                    raise CommandRejected("role_prepare_failed")
                self.activated = True
                self.identity = identity
                self.actions = actions
                self.finish_action = finish_action
            async def status(self):
                assert not self.reject
                return {"role_state": "ready", "role_id": self.identity, "role_persisted": False}

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "sample-role"
            pack = root / "pet-v2"
            pack.mkdir(parents=True)
            make_pack(pack)
            write_role_actions(root)
            (root / "events.json").write_text('{"turn_finished":"wake"}')
            session = BoardSession(Path(directory) / "config.json")
            session.client = board = Board()
            result = await session.install_role(str(root))
            self.assertEqual(result["status"], "ready")
            self.assertFalse(result["remembered_after_restart"])
            self.assertTrue(board.activated)
            self.assertEqual(len(board.actions), 12)
            self.assertEqual(board.finish_action, "wake")
            self.assertEqual(len(board.uploaded), 2)
            self.assertTrue(all(path.startswith("roles/sample-role/r-") for path in board.uploaded))
            session.client = board = Board()
            board.reject = True
            result = await session.install_role(str(root))
            self.assertEqual(result["status"], "activation_failed")
            self.assertEqual(result["reason"], "device_error: role_prepare_failed")
            self.assertEqual(result["files"], 2)
            self.assertFalse(board.activated)

    async def test_does_not_activate_if_a_file_transfer_fails(self):
        class Channel:
            online = True
            capabilities = {"card_transfer": 1, "role_actions": 1}
        class Board:
            channel = Channel()
            activated = False
            async def upload_file(self, *_args, **_kwargs):
                raise RuntimeError("synthetic failure")
            async def prepare_role(self, *_args):
                self.activated = True
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "sample-role"
            pack = root / "pet-v2"
            pack.mkdir(parents=True)
            make_pack(pack)
            write_role_actions(root)
            session = BoardSession(Path(directory) / "config.json")
            session.client = board = Board()
            result = await session.install_role(str(root))
            self.assertEqual(result["status"], "transfer_failed")
            self.assertFalse(board.activated)
