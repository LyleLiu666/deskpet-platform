"""Local paired devices; identity remains independent of their current transport."""
from contextlib import contextmanager
from dataclasses import replace
import fcntl
import os
from pathlib import Path
import re

from .pairing import DEVICE_ID, DeviceConfig, valid_device_uuid


def selection_id(config):
    return config.device_id or 'mac:' + config.identity


def _directory_name(identity):
    if valid_device_uuid(identity):
        return identity
    if isinstance(identity, str) and identity.startswith('mac:') and DEVICE_ID.fullmatch(identity[4:]):
        return 'mac-' + identity[4:].replace(':', '')
    raise ValueError('invalid_device_selection')


class DeviceRegistry:
    """Keep the existing default config intact and store additional pairings separately."""

    def __init__(self, default_path):
        self.default_path = Path(default_path)
        self.root = self.default_path.parent / 'devices'

    @contextmanager
    def _locked(self):
        self.root.mkdir(parents=True, mode=0o700, exist_ok=True)
        if self.root.is_symlink() or self.root.stat().st_mode & 0o077:
            raise ValueError('device_registry_requires_private_directory')
        flags = os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0)
        fd = os.open(self.root / '.lock', flags, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def _entries(self):
        if self.default_path.is_symlink():
            raise ValueError('device_configuration_is_symlink')
        if self.default_path.exists():
            yield self.default_path, DeviceConfig.load(self.default_path)
        for directory in sorted(self.root.iterdir()):
            if directory.name == '.lock':
                continue
            if directory.is_symlink() or not directory.is_dir():
                raise ValueError('invalid_device_registry_entry')
            if directory.stat().st_mode & 0o077:
                raise ValueError('device_registry_requires_private_directory')
            path = directory / 'device.json'
            if path.is_symlink():
                raise ValueError('device_configuration_is_symlink')
            config = DeviceConfig.load(path)
            valid_names = {_directory_name(selection_id(config)), _directory_name('mac:' + config.identity)}
            if directory.name not in valid_names:
                raise ValueError('device_registry_identity_mismatch')
            yield path, config

    def _validated_entries(self):
        entries = list(self._entries())
        macs, uuids = set(), set()
        for _, config in entries:
            if config.identity in macs or (config.device_id is not None and config.device_id in uuids):
                raise ValueError('device_registry_identity_conflict')
            macs.add(config.identity)
            if config.device_id is not None:
                uuids.add(config.device_id)
        return entries

    def list(self):
        with self._locked():
            return [{'id': selection_id(config), 'device_id': config.device_id,
                     'identity': config.identity} for _, config in self._validated_entries()]

    def paths(self):
        with self._locked():
            return [path for path, _ in self._validated_entries()]


    def path_for(self, identity):
        _directory_name(identity)
        with self._locked():
            matches = [path for path, config in self._validated_entries() if selection_id(config) == identity]
            if len(matches) != 1:
                raise KeyError('unknown_or_ambiguous_device')
            return matches[0]

    def get(self, identity):
        # Resolve and read under one lock so an entry cannot be replaced between checks.
        _directory_name(identity)
        with self._locked():
            matches = [config for _, config in self._validated_entries() if selection_id(config) == identity]
            if len(matches) != 1:
                raise KeyError('unknown_or_ambiguous_device')
            return matches[0]

    def add(self, config):
        if not isinstance(config, DeviceConfig):
            raise TypeError('expected_device_configuration')
        identity = selection_id(config)
        with self._locked():
            for _, existing in self._validated_entries():
                if existing.identity == config.identity or (config.device_id and existing.device_id == config.device_id):
                    raise ValueError('device_already_paired_or_identity_conflict')
            directory = self.root / _directory_name(identity)
            directory.mkdir(mode=0o700)
            try:
                config.save(directory / 'device.json')
            except BaseException:
                directory.rmdir()
                raise
        return identity

    def save_default(self, config, expected):
        """Replace only the default pairing that was checked before authentication."""
        if not isinstance(config, DeviceConfig):
            raise TypeError('expected_device_configuration')
        with self._locked():
            entries = self._validated_entries()
            existing = next((item for path, item in entries if path == self.default_path), None)
            if existing != expected:
                raise ValueError('device_configuration_changed')
            if existing is not None:
                if existing.identity != config.identity or (existing.device_id is not None and existing.device_id != config.device_id):
                    raise ValueError('device_identity_mismatch')
            for path, item in entries:
                if path != self.default_path and (item.identity == config.identity or
                                                  (config.device_id is not None and item.device_id == config.device_id)):
                    raise ValueError('device_registry_identity_conflict')
            config.save(self.default_path)

    def upgrade(self, path, expected, device_id):
        """Bind a legacy pairing to the UUID observed on its authenticated connection."""
        if not valid_device_uuid(device_id):
            raise ValueError('invalid_device_uuid')
        path = Path(path)
        with self._locked():
            entries = self._validated_entries()
            matching = [config for item_path, config in entries if item_path == path]
            if len(matching) != 1 or matching[0] != expected:
                raise ValueError('device_configuration_changed')
            if expected.device_id is not None and expected.device_id != device_id:
                raise ValueError('device_identity_mismatch')
            if any(item_path != path and config.device_id == device_id for item_path, config in entries):
                raise ValueError('device_registry_identity_conflict')
            if expected.device_id is None:
                expected = replace(expected, device_id=device_id)
                expected.save(path)
            return expected
