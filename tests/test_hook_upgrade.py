import json
import os
from pathlib import Path
import shlex
import tempfile
import unittest
from unittest.mock import patch

from deskpet_platform import antigravity_feedback, opencode_feedback
from deskpet_platform.codex_hook import configure_codex_hooks
from deskpet_platform.windows_hook_command import build


class HookUpgradeTests(unittest.TestCase):
    def test_reinstall_replaces_old_python_for_only_the_selected_config(self):
        for windows in (False, True):
            with self.subTest(windows=windows), tempfile.TemporaryDirectory() as temporary:
                path = Path(temporary) / "hooks.json"
                config = r"C:\DeskPet State\device.json" if windows else "/state with spaces/device.json"
                other = r"C:\other\device.json" if windows else "/other/device.json"
                old_python = r"C:\Old DeskPet\Scripts\python.exe" if windows else "/old install/.venv/bin/python3.12"
                current_python = r"C:\New DeskPet\Scripts\python.exe" if windows else "/new install/.venv/bin/python"

                def command(python, target):
                    args = ["-m", "deskpet_platform", "--config", target, "codex-hook"]
                    return build(python, args) if windows else shlex.join([python, *args])

                old = command(old_python, config)
                new = command(current_python, config)
                unrelated = command(old_python, other)
                altered = old + " && echo custom"
                for existing in (old, unrelated, altered):
                    configure_codex_hooks(path, existing, [], enabled=True)

                before = json.loads(path.read_text(encoding="utf-8"))
                self.assertTrue(configure_codex_hooks(path, new, [], enabled=True))
                self.assertFalse(configure_codex_hooks(path, new, [], enabled=True))
                after = json.loads(path.read_text(encoding="utf-8"))
                for event, groups in after["hooks"].items():
                    commands = [hook["command"] for group in groups for hook in group["hooks"]]
                    self.assertEqual(commands.count(new), 2 if event in ("PreToolUse", "PostToolUse") else 1)
                    self.assertNotIn(old, commands)
                    # Existing trust slots must not move when the old hook is removed.
                    for index, group in enumerate(before["hooks"][event]):
                        if any(hook["command"] in (unrelated, altered) for hook in group["hooks"]):
                            self.assertEqual(groups[index], group)

                self.assertTrue(configure_codex_hooks(path, new, [], enabled=False))
                stopped = json.loads(path.read_text(encoding="utf-8"))["hooks"]["Stop"]
                self.assertEqual([hook["command"] for group in stopped for hook in group["hooks"]],
                                 [unrelated, altered])


    def test_antigravity_removes_the_old_generated_entry_only_for_this_pairing(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "hooks.json"
            config = str(Path(temporary) / "device.json")
            old = shlex.join(["/old install/python3", "-m", "deskpet_platform", "--config", config, "antigravity-hook"])
            new = shlex.join(["/new install/python3.13", "-m", "deskpet_platform", "--config", config, "antigravity-hook"])
            other = shlex.join(["/old install/python3", "-m", "deskpet_platform", "--config", "/other/device.json",
                                "antigravity-hook"])
            for command in (old, other):
                self.assertTrue(antigravity_feedback.configure(path, command, enabled=True))
            before = json.loads(path.read_text(encoding="utf-8"))
            shared = next(entry for entry in before.values() if entry.get("Stop", [{}])[0].get("command") == other)
            self.assertTrue(antigravity_feedback.configure(path, new, enabled=True))
            after = json.loads(path.read_text(encoding="utf-8"))
            self.assertCountEqual(list(after.values()), [shared, antigravity_feedback.hook_entry(new)])
            self.assertFalse(antigravity_feedback.configure(path, new, enabled=True))

    @unittest.skipIf(os.name == "nt", "OpenCode hooks are currently supported on POSIX")
    def test_opencode_replaces_the_existing_generated_plugin_and_preserves_custom_files(self):
        with tempfile.TemporaryDirectory() as temporary, patch.dict(os.environ, XDG_CONFIG_HOME=temporary):
            config = str(Path(temporary) / "device.json")
            old = shlex.join(["/old install/python3", "-m", "deskpet_platform", "--config", config, "opencode-hook"])
            new = shlex.join(["/new install/python3.13", "-m", "deskpet_platform", "--config", config, "opencode-hook"])
            old_path = opencode_feedback.plugin_path(old)
            new_path = opencode_feedback.plugin_path(new)
            self.assertTrue(opencode_feedback.configure(old_path, old, enabled=True))
            custom = old_path.with_name("deskpet-custom.js")
            custom.write_bytes(old_path.read_bytes() + b"// custom\n")

            self.assertTrue(opencode_feedback.configure_registry(new, [], enabled=True))
            self.assertFalse(old_path.exists())
            self.assertTrue(new_path.is_file())
            self.assertTrue(custom.read_bytes().endswith(b"// custom\n"))
            self.assertFalse(opencode_feedback.configure_registry(new, [], enabled=True))
