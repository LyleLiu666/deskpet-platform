"""Device tools a user may authorize for a voice conversation.

Nothing is enabled by default. The caller must present the available tools and
confirmation policy, persist the user's choice, and pass that explicit set here.
"""
from dataclasses import replace
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .card_upload import validate_music_path

from .voice_agent import ToolOutcome, VoiceTool


class _NoArgs(BaseModel):
    model_config = ConfigDict(extra='forbid')


class _PageArgs(BaseModel):
    model_config = ConfigDict(extra='forbid')
    page: Literal['role', 'home', 'clock', 'settings', 'roles', 'photos', 'weather', 'messages']


class _ActionArgs(BaseModel):
    model_config = ConfigDict(extra='forbid')
    action: str = Field(min_length=1, max_length=64)
    duration_ms: int | None = Field(default=None, ge=100, le=6000)


class _DeviceSettingsArgs(BaseModel):
    model_config = ConfigDict(extra='forbid')
    volume: int | None = Field(default=None, ge=0, le=100)
    brightness: int | None = Field(default=None, ge=10, le=100)


class _MusicArgs(BaseModel):
    model_config = ConfigDict(extra='forbid')
    action: Literal['status', 'play', 'pause', 'stop', 'prev', 'next', 'shuffle']
    path: str | None = Field(default=None, description='仅 play 可指定设备曲库已知路径。')

    @model_validator(mode='after')
    def validate_path(self):
        if self.path is not None:
            if self.action != 'play':
                raise ValueError('只有播放可以指定歌曲路径')
            validate_music_path(self.path)
        return self


class _PhotoSlideshowArgs(BaseModel):
    model_config = ConfigDict(extra='forbid')
    mode: Literal['play', 'pause', 'interval']
    interval_seconds: Literal[5, 10, 15] | None = Field(default=None, description='仅 interval 必填。')

    @model_validator(mode='after')
    def validate_interval(self):
        if (self.mode == 'interval') != (self.interval_seconds is not None):
            raise ValueError('仅设置轮播间隔时填写秒数')
        return self


_TOOLS = {
    'device_status': ('检查该配对设备当前能否与电脑通信，并查看页面和角色状态。', _NoArgs),
    'list_role_actions': ('列出当前角色支持的动作。', _NoArgs),
    'play_role_action': ('让角色做指定动作。started 只表示动作已开始。', _ActionArgs),
    'open_page': ('临时切换到指定页面，不修改默认首页。', _PageArgs),
    'battery_status': ('查看电压、估算电量与电源状态。没有数据时如实报告未知。', _NoArgs),
    'device_settings': ('查看或调整当前会话的音量和亮度。省略参数时只查询。', _DeviceSettingsArgs),
    'device_restart': ('请求在设备屏幕上确认软重启，用户实际点按后才会重启。', _NoArgs),
    'music_control': ('查询、播放、暂停、停止、切歌或打乱设备曲库顺序。播放和切歌排到语音回复结束后执行。', _MusicArgs),
    'photo_slideshow': ('开始、暂停或临时调整相册轮播，间隔可设 5、10 或 15 秒。', _PhotoSlideshowArgs),
}
_CAPABILITIES = {
    'device_status': (),
    'list_role_actions': ('role_actions',),
    'play_role_action': ('role_actions', 'capture_voice_role'),
    'open_page': ('screen_views',),
    'battery_status': ('battery_status',),
    'device_settings': ('device_settings',),
    'device_restart': ('system_restart',),
    'music_control': ('music_control', 'capture_media_tools'),
    'photo_slideshow': ('photo_album', 'capture_media_tools'),
}


def voice_tool_supported(name, capabilities):
    return name in _CAPABILITIES and all(capabilities.get(item) == 1 for item in _CAPABILITIES[name])


def voice_tool_requires_confirmation(name):
    return name == 'device_restart'


def voice_operation_schemas():
    """Describe possible intents independently of the device's tool permissions."""
    return {name: {'name': name, 'description': description, 'parameters': args.model_json_schema()}
            for name, (description, args) in _TOOLS.items()}


def build_voice_tools(session, enabled, *, authorize=None, permission_lock=None, voice_role_action=None):
    """Construct only the selected tools, bound to exactly one paired device."""
    if not isinstance(enabled,(set,frozenset)):
        raise ValueError('invalid_tool_permissions')
    if authorize is not None and not callable(authorize):raise ValueError('invalid_tool_permissions')
    if permission_lock is not None and not callable(permission_lock):raise ValueError('invalid_tool_permissions')
    if voice_role_action is not None and not callable(voice_role_action):raise ValueError('invalid_tool_permissions')
    if not enabled <= _TOOLS.keys():raise ValueError('invalid_tool_permissions')

    async def device_status():
        response=await session.get_status()
        if not isinstance(response,dict) or not isinstance(response.get('connection'),str):
            return ToolOutcome(False, {'error':'invalid_status'})
        visible={'connection':response['connection']}
        if isinstance(response.get('screen_page'),str):
            visible['screen_page']=response['screen_page']
        role=response.get('role')
        if isinstance(role,dict):visible['role']={key:role[key] for key in ('id','state') if key in role}
        return ToolOutcome(True,visible)

    async def list_role_actions():
        response=await session.list_actions()
        if not isinstance(response,dict):return ToolOutcome(False,{'status':'invalid_response'})
        status=response.get('status')
        actions=response.get('actions')
        if status=='ok' and isinstance(actions,list) and len(actions)<=64 and all(isinstance(a,str) and 0<len(a)<=64 for a in actions):
            return ToolOutcome(True,{'status':'ok','actions':actions})
        failure='设备未连接，无法查看动作。' if status=='not_connected' else '无法获取角色动作。'
        return ToolOutcome(False,{'status':status if isinstance(status,str) and status!='ok' else 'invalid_response'},failure_message=failure)

    async def play_role_action(action,duration_ms=None):
        response=await (voice_role_action or session.play_role)(action,duration_ms)
        if not isinstance(response,dict):return ToolOutcome(False,{'status':'invalid_response'})
        status=response.get('status')
        if not isinstance(status,str):return ToolOutcome(False,{'status':'invalid_response'})
        visible={key:response[key] for key in ('status','action','operation','boot','available_actions') if key in response}
        failure={'busy':'设备正忙，动作未开始。','unsupported':'当前角色不支持这个动作。',
                 'not_connected':'设备未连接，动作未开始。'}.get(status,'动作未能开始。')
        return ToolOutcome(status=='started',visible,completed=False,failure_message=failure if status!='started' else None)

    async def open_page(page):
        response=await session.set_view(page,False,None)
        if not isinstance(response,dict):return ToolOutcome(False,{'status':'invalid_response'})
        visible={key:response[key] for key in ('status','page') if key in response}
        status=response.get('status')
        if not isinstance(status,str):return ToolOutcome(False,{'status':'invalid_response'})
        failure={'not_connected':'设备未连接，页面未打开。','unsupported':'设备暂不支持打开这个页面。'}.get(status,'页面未能打开。')
        return ToolOutcome(status=='shown',visible,failure_message=failure if status!='shown' else None)

    async def battery_status():
        result = await session.voice_battery_status()
        if result.get('status') != 'ok':
            return ToolOutcome(False, {'status': result['status']}, failure_message='暂时无法读取电池状态。')
        return ToolOutcome(True, result['battery'])

    async def device_settings(volume=None, brightness=None):
        result = await session.voice_device_settings(volume=volume, brightness=brightness)
        status = result['status']
        if status == 'ok':
            return ToolOutcome(True, {name: result[name] for name in ('volume', 'brightness')})
        if status == 'unknown':
            message = ('设备可能已应用部分设置，请先核对音量和亮度。' if result.get('reason') == 'settings_partial'
                       else '调整结果未知，请先核对音量和亮度。')
            return ToolOutcome(False, {'status': status}, failure_message=message, result_unknown=True)
        return ToolOutcome(False, {'status': status}, failure_message='目前无法完成音量或亮度操作。')

    async def device_restart():
        result = await session.voice_restart()
        status = result['status']
        if status == 'confirm_pending':
            return ToolOutcome(True, {'status': status}, completed=False)
        if status == 'unknown':
            return ToolOutcome(False, {'status': status}, failure_message='重启请求结果未知，请先核对设备状态。',
                               result_unknown=True)
        return ToolOutcome(False, {'status': status}, failure_message='目前无法请求设备重启。')

    async def music_control(action, path=None):
        result = await session.voice_music_control(action, path)
        return _media_result(result, '音乐操作结果未知，请先核对播放器状态。', '目前无法完成音乐操作。')

    async def photo_slideshow(mode, interval_seconds=None):
        result = await session.voice_photo_slideshow(mode, interval_seconds)
        return _media_result(result, '轮播操作结果未知，请先核对相册状态。', '目前无法完成轮播操作。')

    handlers = {'device_status': device_status, 'list_role_actions': list_role_actions,
                'play_role_action': play_role_action, 'open_page': open_page, 'battery_status': battery_status,
                'device_settings': device_settings, 'device_restart': device_restart,
                'music_control': music_control, 'photo_slideshow': photo_slideshow}
    def guarded(tool):
        if authorize is None:return tool
        async def invoke(**arguments):
            async def checked():
                try:permitted=authorize(tool.name) is True
                except Exception:permitted=False
                if not permitted:return ToolOutcome(False,{'status':'not_authorized'},failure_message='这项语音操作未获授权。')
                return await tool.invoke(**arguments)
            if permission_lock is None:return await checked()
            try:
                async with permission_lock():return await checked()
            except (TimeoutError,OSError):
                return ToolOutcome(False,{'status':'unavailable'},failure_message='这项语音操作暂时不可用。')
        return replace(tool, invoke=invoke)
    return [guarded(VoiceTool(name, *_TOOLS[name], handlers[name],
                             requires_confirmation=voice_tool_requires_confirmation(name),
                             confirmation_on_device=voice_tool_requires_confirmation(name))) for name in sorted(enabled)]


def _media_result(result, unknown_message, failure_message):
    status = result['status']
    if status == 'unknown':
        return ToolOutcome(False, {'status': status}, failure_message=unknown_message, result_unknown=True)
    if status != 'ok':
        return ToolOutcome(False, result, failure_message=failure_message)
    if result.get('queued') is True:
        return ToolOutcome(True, result, completed=False, deferred=True)
    return ToolOutcome(True, result)
