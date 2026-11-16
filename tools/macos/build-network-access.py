#!/usr/bin/env python3
"""Build the local-network permission app without replacing an existing app."""
import argparse
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('destination', type=Path, help='New .app path')
args = parser.parse_args()
if sys.platform != 'darwin' or args.destination.suffix != '.app':
    parser.error('Build on macOS and provide a new .app path.')
app = args.destination.expanduser().absolute()
app.parent.mkdir(parents=True, exist_ok=True)
app.mkdir()  # Refuse to replace or modify an existing application.
try:
    contents = app / 'Contents'
    binary = contents / 'MacOS'
    binary.mkdir(parents=True)
    with (contents / 'Info.plist').open('wb') as stream:
        plistlib.dump({'CFBundleName': '桌面屏局域网连接', 'CFBundleDisplayName': '桌面屏局域网连接',
                       'CFBundleIdentifier': 'local.deskpet.network-access',
                       'CFBundleExecutable': 'DeskPetNetworkAccess', 'CFBundlePackageType': 'APPL',
                       'CFBundleVersion': '1', 'CFBundleShortVersionString': '1.0',
                       'NSLocalNetworkUsageDescription': '连接桌面屏，持续同步电脑数据和新消息。',
                       'NSHighResolutionCapable': True}, stream)
    source = Path(__file__).with_name('DeskPetNetworkAccess.swift')
    subprocess.run(['xcrun', 'swiftc', str(source), '-o', str(binary / 'DeskPetNetworkAccess')], check=True)
    launcher = Path(__file__).with_name('DeskPetReceiverLauncher.swift')
    subprocess.run(['xcrun', 'swiftc', str(launcher), '-o', str(binary / 'DeskPetReceiverLauncher')], check=True)
    subprocess.run(['codesign', '--force', '--sign', '-', str(binary / 'DeskPetReceiverLauncher')], check=True)
    subprocess.run(['codesign', '--force', '--sign', '-', str(app)], check=True)
except BaseException:
    shutil.rmtree(app)
    raise
print(app)
