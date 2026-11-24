"""Coordinate one authorized conversation and its interruptible spoken replies.

The caller owns recording, user interface, authorization and confirmation. This
object does not start listening or approve a tool call on the user's behalf.
"""
import asyncio
from dataclasses import dataclass

from .voice_agent import AgentBusy, AgentError


async def create_device_conversation(session, backend, *, enabled_tools, authorize=None, permission_lock=None, voice_role_action=None):
    """Start with this device's saved names and the caller's explicit tool permissions."""
    from .voice_agent import VoiceAgent
    from .voice_tools import build_voice_tools
    tools=build_voice_tools(session,enabled_tools,authorize=authorize,permission_lock=permission_lock,voice_role_action=voice_role_action)
    names=await session.voice_names()
    if not isinstance(names,dict):raise AgentError('voice_names_invalid_response')
    if names.get('status')!='ok':
        status=names.get('status')
        if not isinstance(status,str):raise AgentError('voice_names_invalid_response')
        raise AgentError({'not_connected':'voice_names_not_connected',
                          'unsupported':'voice_names_unsupported'}.get(status,'voice_names_unavailable'))
    try:
        agent=VoiceAgent(backend,tools,voice_names={'user_name':names.get('user_name'),
                                                   'device_name':names.get('device_name')})
    except ValueError:
        raise AgentError('voice_names_invalid_response') from None
    return VoiceConversation(agent,session.speak_voice_reply)


class ConversationBusy(RuntimeError):
    pass


@dataclass(frozen=True)
class ConversationResult:
    agent: object
    playback: dict | None


class VoiceConversation:
    def __init__(self, agent, speak_reply):
        self.agent = agent
        self.speak_reply = speak_reply
        self._active = None
        self._closing = False
        self._needs_reset = False
        self._end_lock = asyncio.Lock()

    async def turn(self, text, *, speak_reply=None, speak_stream=None, show_streamed_reply=None):
        return await self._run(self.agent.turn, text, speak_reply=speak_reply, speak_stream=speak_stream,
                               show_streamed_reply=show_streamed_reply)

    async def confirm(self, identity, approved):
        return await self._run(self.agent.confirm, identity, approved)

    async def _run(self, operation, *args, speak_reply=None, speak_stream=None, show_streamed_reply=None):
        if self._active is not None or self._closing or self._needs_reset:
            raise ConversationBusy('conversation_busy_or_requires_end')
        self._active = asyncio.current_task()
        first_phrase = None
        continuation = None
        streamed_playback = None

        async def start_phrase(phrase):
            nonlocal first_phrase, continuation, streamed_playback
            first_phrase = phrase
            continuation = asyncio.get_running_loop().create_future()
            streamed_playback = asyncio.create_task(speak_stream(phrase, continuation=continuation))

        try:
            try:
                options = {'on_first_phrase': start_phrase} if speak_stream is not None else {}
                result = await operation(*args, **options)
                if first_phrase is not None and (not result.text or not result.text.startswith(first_phrase)):
                    raise AgentError('model_invalid_response')
            except AgentError as error:
                if not isinstance(error, AgentBusy) and error.code != 'unknown_confirmation':
                    self._needs_reset = True
                raise
            except Exception:
                # A failed model/tool round may leave a partial protocol history.
                self._needs_reset = True
                raise
            if streamed_playback is None:
                playback = await (speak_reply or self.speak_reply)(result.text) if result.text else None
            else:
                if show_streamed_reply is not None:
                    await show_streamed_reply(result.text)
                continuation.set_result(result.text[len(first_phrase):].strip())
                playback = await streamed_playback
            return ConversationResult(result, playback)
        except asyncio.CancelledError:
            # The model or tool may have been interrupted midway. Do not reuse
            # its history until end() has cleared it.
            self._needs_reset = True
            raise
        finally:
            try:
                if streamed_playback is not None:
                    streamed_playback.cancel()
                    continuation.cancel()
                    await asyncio.gather(streamed_playback, return_exceptions=True)
            finally:
                self._active = None

    async def end(self):
        """Stop active work, then discard conversation history and confirmations."""
        async with self._end_lock:
            self._closing = True
            try:
                task = self._active
                if task is asyncio.current_task():
                    raise RuntimeError('cannot_end_from_active_conversation')
                if task is not None:
                    self._needs_reset = True
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        if asyncio.current_task().cancelling():
                            raise
                    except Exception:
                        pass  # The caller of that turn observes its own error.
                await self.agent.end()
                self._needs_reset = False
            finally:
                self._closing = False
