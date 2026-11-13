"""Resolve each operation to a device-specific session without a shared selection."""


class DeviceSessions:
    def __init__(self, registry, default_session, session_factory, on_create=None):
        self.registry = registry
        self.session_factory = session_factory
        self.on_create = on_create
        self.sessions = {registry.default_path: default_session}

    def resolve(self, device_id):
        if device_id is None:
            devices = self.registry.list()
            if not devices:
                return self.sessions[self.registry.default_path]
            if len(devices) != 1:
                raise ValueError('请选择要控制的设备。')
            device_id = devices[0]['id']
        path = self.registry.path_for(device_id)
        if path not in self.sessions:
            session = self.session_factory(path)
            self.sessions[path] = session
            if self.on_create is not None:
                self.on_create(session)
        return self.sessions[path]
