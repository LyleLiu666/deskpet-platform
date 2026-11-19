"""Device tools a user may authorize for a voice conversation.

Nothing is enabled by default. The caller must present the available tools and
confirmation policy, persist the user's choice, and pass that explicit set here.
"""
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

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


_TOOLS = {
    'device_status': ('检查该配对设备当前能否与电脑通信，并查看页面和角色状态。', _NoArgs),
    'list_role_actions': ('列出当前角色支持的动作。', _NoArgs),
    'play_role_action': ('让角色做指定动作。started 只表示动作已开始。', _ActionArgs),
    'open_page': ('临时切换到指定页面，不修改默认首页。', _PageArgs),
}


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

    handlers = {'device_status': device_status, 'list_role_actions': list_role_actions,
                'play_role_action': play_role_action, 'open_page': open_page}
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
        return VoiceTool(tool.name,tool.description,tool.parameters,invoke,tool.requires_confirmation)
    return [guarded(VoiceTool(name, *_TOOLS[name], handlers[name])) for name in sorted(enabled)]
