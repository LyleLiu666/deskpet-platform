#!/usr/bin/env python3
"""Prepare a reviewable launchd plist; never install or start it."""

import argparse
import hashlib
import os
from pathlib import Path
import plistlib

BUNDLE_ID='local.deskpet.network-access'


def _absolute_file(value, *, executable=False):
    path=Path(value)
    if not path.is_absolute() or not path.is_file() or (executable and not os.access(path,os.X_OK)):
        raise ValueError('Expected an existing absolute file path' + (' to an executable' if executable else ''))
    return path


def make_agent(app, executable, receiver_config, shared_config, token_probe=None):
    app=Path(app)
    if not app.is_absolute() or app.suffix!='.app' or not app.is_dir():raise ValueError('Expected an existing absolute app bundle')
    try:
        with (app/'Contents/Info.plist').open('rb') as stream:info=plistlib.load(stream)
    except (OSError,ValueError,plistlib.InvalidFileException) as error:
        raise ValueError('Cannot read application identity') from error
    if not isinstance(info,dict) or info.get('CFBundleIdentifier')!=BUNDLE_ID:raise ValueError('Unexpected application identity')
    launcher=_absolute_file(app/'Contents/MacOS/DeskPetReceiverLauncher',executable=True)
    executable=_absolute_file(executable,executable=True)
    receiver_config=_absolute_file(receiver_config)
    shared_config=_absolute_file(shared_config)
    arguments=[str(launcher),str(executable),'--config',str(receiver_config),'receiver-service','--shared-config',str(shared_config)]
    if token_probe is not None:arguments.extend(('--token-probe',str(_absolute_file(token_probe,executable=True))))
    identity=hashlib.sha256(os.fsencode(receiver_config.resolve())).hexdigest()[:16]
    stdout=str(receiver_config.parent/'launchd-receiver.out.log')
    stderr=str(receiver_config.parent/'launchd-receiver.err.log')
    return {'Label':'local.deskpet.receiver.'+identity,'ProgramArguments':arguments,
            'AssociatedBundleIdentifiers':[BUNDLE_ID],'RunAtLoad':True,'KeepAlive':True,
            'ThrottleInterval':30,'Umask':0o077,'StandardOutPath':stdout,'StandardErrorPath':stderr}


def write_agent(path, config):
    path=Path(path)
    if not path.is_absolute():raise ValueError('Output path must be absolute')
    descriptor=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(descriptor,'wb') as stream:plistlib.dump(config,stream,sort_keys=True)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--app',type=Path,required=True)
    parser.add_argument('--executable',type=Path,required=True)
    parser.add_argument('--receiver-config',type=Path,required=True)
    parser.add_argument('--shared-config',type=Path,required=True)
    parser.add_argument('--token-probe',type=Path)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    config=make_agent(args.app,args.executable,args.receiver_config,args.shared_config,args.token_probe)
    write_agent(args.output,config)
    print(f'Prepared {args.output}; it was not installed or started.')


if __name__=='__main__':main()
