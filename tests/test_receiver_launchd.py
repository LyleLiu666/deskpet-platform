import importlib.util
import os
from pathlib import Path
import plistlib
import select
import signal
import subprocess
import sys
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('receiver_agent',ROOT/'tools/macos/receiver_agent.py')
agent=importlib.util.module_from_spec(spec)
spec.loader.exec_module(agent)


class ReceiverAgentTests(unittest.TestCase):
    def test_config_preserves_arguments_and_never_overwrites(self):
        with tempfile.TemporaryDirectory(prefix='deskpet agent ') as directory:
            root=Path(directory);app=root/'Desk Pet.app';binary=app/'Contents/MacOS';binary.mkdir(parents=True)
            (app/'Contents/Info.plist').write_bytes(plistlib.dumps({'CFBundleIdentifier':'local.deskpet.network-access'}))
            launcher=binary/'DeskPetReceiverLauncher';launcher.touch();launcher.chmod(0o700)
            executable=root/'deskpet-platform';executable.touch();executable.chmod(0o700)
            config=root/'device config.json';config.touch()
            shared=root/'shared config.json';shared.touch()
            probe=root/'probe executable';probe.touch();probe.chmod(0o700)
            result=agent.make_agent(app,executable,config,shared,probe)
            self.assertEqual(result['ProgramArguments'],[str(launcher),str(executable),'--config',str(config),
                'receiver-service','--shared-config',str(shared),'--token-probe',str(probe)])
            self.assertEqual(result['AssociatedBundleIdentifiers'],['local.deskpet.network-access'])
            self.assertTrue(result['RunAtLoad']);self.assertTrue(result['KeepAlive'])
            self.assertEqual(result['Umask'],0o077)
            self.assertNotEqual(result['StandardOutPath'],result['StandardErrorPath'])
            target=root/'candidate.plist';agent.write_agent(target,result)
            self.assertEqual(plistlib.loads(target.read_bytes()),result)
            self.assertEqual(target.stat().st_mode & 0o777,0o600)
            with self.assertRaises(FileExistsError):agent.write_agent(target,result)
            self.assertEqual(plistlib.loads(target.read_bytes()),result)
            (app/'Contents/Info.plist').write_bytes(plistlib.dumps({'CFBundleIdentifier':'different.app'}))
            with self.assertRaises(ValueError):agent.make_agent(app,executable,config,shared,probe)

    def test_refuses_missing_or_relative_inputs(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            with self.assertRaises(ValueError):agent.make_agent(Path('relative.app'),root/'bin',root/'a',root/'b')
            with self.assertRaises(ValueError):agent.make_agent(root/'missing.app',root/'bin',root/'a',root/'b')


@unittest.skipUnless(sys.platform=='darwin','macOS native launcher')
class ReceiverLauncherTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp=tempfile.TemporaryDirectory(prefix='deskpet launcher ')
        cls.launcher=Path(cls.tmp.name)/'launcher'
        subprocess.run(['xcrun','swiftc',str(ROOT/'tools/macos/DeskPetReceiverLauncher.swift'),'-o',str(cls.launcher)],check=True,capture_output=True)

    @classmethod
    def tearDownClass(cls):cls.tmp.cleanup()

    def test_forwards_arguments_and_child_exit(self):
        child='import sys; print(repr(sys.argv[1:])); sys.exit(17)'
        result=subprocess.run([str(self.launcher),sys.executable,'-c',child,'a b','$(not-a-shell)'],capture_output=True,text=True,timeout=10)
        self.assertEqual(result.returncode,17)
        self.assertIn("['a b', '$(not-a-shell)']",result.stdout)

    def test_executes_a_shebang_script_without_a_shell(self):
        script=Path(self.tmp.name)/'receiver script'
        script.write_text(f'#!{sys.executable}\nimport sys\nprint(repr(sys.argv[1:]))\n')
        script.chmod(0o700)
        result=subprocess.run([str(self.launcher),str(script),'arg with spaces'],capture_output=True,text=True,timeout=10)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertIn("['arg with spaces']",result.stdout)

    def test_forwards_termination_to_child(self):
        child='import signal,time,sys; signal.signal(signal.SIGTERM,lambda *_:sys.exit(23)); print("ready",flush=True); time.sleep(60)'
        process=subprocess.Popen([str(self.launcher),sys.executable,'-u','-c',child],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,start_new_session=True)
        try:
            self.assertTrue(select.select([process.stdout],[],[],5)[0],'child did not start')
            self.assertEqual(process.stdout.readline().strip(),'ready')
            process.send_signal(signal.SIGTERM)
            _,error=process.communicate(timeout=5)
            self.assertEqual(process.returncode,23,error)
        finally:
            if process.poll() is None:os.killpg(process.pid,signal.SIGKILL);process.communicate()

    def test_child_default_termination_is_not_ignored(self):
        child='import time; print("ready",flush=True); time.sleep(60)'
        process=subprocess.Popen([str(self.launcher),sys.executable,'-u','-c',child],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,start_new_session=True)
        try:
            self.assertTrue(select.select([process.stdout],[],[],5)[0],'child did not start')
            self.assertEqual(process.stdout.readline().strip(),'ready')
            process.send_signal(signal.SIGTERM)
            _,error=process.communicate(timeout=5)
            self.assertEqual(process.returncode,128+signal.SIGTERM,error)
        finally:
            if process.poll() is None:os.killpg(process.pid,signal.SIGKILL);process.communicate()


if __name__=='__main__':unittest.main()
