"""Read Codex allowance using the desktop display controller’s account protocol."""
import asyncio
from decimal import Decimal, InvalidOperation, ROUND_DOWN
import json
import os
from pathlib import Path
import shutil
import time


def quota_reading(result, now):
    buckets = result.get('rateLimitsByLimitId') or {}
    bucket = buckets.get('codex')
    if not bucket:
        fallback = result.get('rateLimits') or {}
        if fallback.get('limitId') == 'codex':
            bucket = fallback
    extras = {}
    credits = (bucket or {}).get('credits') or {}
    balance = credits.get('balance')
    if isinstance(balance, str) and len(balance) <= 32:
        try:
            number = Decimal(balance)
            if number.is_finite() and 0 <= number < 10_000_000:
                rounded = number.quantize(Decimal('0.01'), rounding=ROUND_DOWN).normalize()
                extras['credits'] = format(rounded if rounded else Decimal(0), 'f')
        except InvalidOperation:
            pass
    reset_cards = (result.get('rateLimitResetCredits') or {}).get('availableCount')
    if type(reset_cards) is int and 0 <= reset_cards <= 65535:
        extras['reset_cards'] = reset_cards
    windows = []
    for name in ('primary', 'secondary'):
        window = (bucket or {}).get(name) or {}
        minutes, used, reset = (window.get(key) for key in ('windowDurationMins', 'usedPercent', 'resetsAt'))
        if type(minutes) is int and 0 < minutes <= 525600 and type(used) is int and 0 <= used <= 100 and type(reset) is int and reset > now:
            windows.append([100-used, minutes, reset])
    return dict(quotas=windows, **extras)


async def fetch_quota():
    # A background launch may lack the interactive shell's package-manager
    # paths. Extend only this child environment, including Node for npm Codex.
    path = os.pathsep.join((
        os.environ.get('PATH') or os.defpath,
        '/opt/homebrew/bin',
        '/usr/local/bin',
        str(Path.home()/'.local/bin'),
        '/Applications/ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS',
    ))
    command = ['codex', 'app-server', '--listen', 'stdio://']
    if os.name == 'nt':
        executable = shutil.which('codex', path=path)
        if executable is None:
            raise FileNotFoundError('codex executable not found')
        command[0] = executable
        if executable.lower().endswith(('.cmd', '.bat')):
            if any(character in executable for character in ('%', '"', '\r', '\n')):
                raise ValueError('codex command path cannot be safely invoked')
            command = [os.environ.get('COMSPEC', 'cmd.exe'), '/d', '/v:off', '/c',
                       executable, 'app-server', '--listen', 'stdio://']
    process = await asyncio.create_subprocess_exec(
        *command,
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=None,
        limit=262144, env={**os.environ, 'PATH': path},
    )

    async def send(method, **fields):
        process.stdin.write((json.dumps(dict(method=method, **fields))+'\n').encode())
        await process.stdin.drain()

    async def receive(request_id):
        while line := await process.stdout.readline():
            message = json.loads(line)
            if message.get('id') == request_id:
                return message.get('result')
        return None

    try:
        async with asyncio.timeout(10):
            await send('initialize', id=1, params={'clientInfo': {'name': 'deskpet_platform', 'version': '0.1.0'}})
            if not await receive(1):
                return None
            await send('initialized')
            await send('account/rateLimits/read', id=2, params={'excludeResetCreditDetails': True})
            result = await receive(2)
            return quota_reading(result, time.time()) if result else None
    finally:
        if os.name == 'nt' and process.stdin is not None and process.returncode is None:
            process.stdin.close()
            try:
                await asyncio.wait_for(process.wait(), 1)
            except asyncio.TimeoutError:
                pass
        if process.returncode is None:
            try:
                process.terminate()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(process.wait(), 2)
            except asyncio.TimeoutError:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
                await process.wait()
