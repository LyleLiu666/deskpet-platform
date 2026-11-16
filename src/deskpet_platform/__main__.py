import argparse
import asyncio
from getpass import getpass
from importlib.metadata import version
import json
import os
from pathlib import Path
import sys
from dataclasses import replace

from .pairing import DEFAULT_CONFIG, DeviceConfig
from .device_registry import DeviceRegistry


async def interact(args):
    from .client import Client
    from .mcp_server import status_view
    registry = DeviceRegistry(args.config)
    existing = None
    if args.command == "pair":
        config = DeviceConfig.from_code(getpass("小板配对码（输入不回显）："), args.address)
        if not args.add_device:
            try:
                existing = DeviceConfig.load(args.config)
            except FileNotFoundError:
                pass
            if existing is not None and existing.identity != config.identity:
                raise ValueError("这台电脑已配对另一台设备，请使用 --add-device。")
    else:
        devices = registry.list()
        if args.device_id is None and len(devices) > 1:
            raise ValueError("请使用 --device-id 选择要查询的设备。")
        identity = args.device_id or (devices[0]["id"] if devices else None)
        config = registry.get(identity) if identity is not None else DeviceConfig.load(args.config)
    async with Client(config) as client:
        status = status_view(await client.status(), client.channel.capabilities)
        if args.command == "pair":
            if existing is not None and existing.device_id is not None and existing.device_id != client.device_id:
                raise ValueError("设备标识与已保存的配对不符。")
            if client.device_id is not None:
                config = replace(config, device_id=client.device_id)
    if args.command == "pair":
        if args.add_device:
            registry.add(config)
        else:
            registry.save_default(config, existing)
    return status


def main():
    # Windows pipes may default to a legacy code page, but both our Chinese
    # messages and MCP's JSON transport need UTF-8.
    if os.name == "nt":
        for stream in (sys.stdout, sys.stderr):
            if hasattr(stream, "reconfigure"):
                stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(prog="deskpet-platform")
    parser.add_argument("--version", action="version", version=f"%(prog)s {version('deskpet-platform')}")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    commands = parser.add_subparsers(dest="command", required=True)
    pair = commands.add_parser("pair", help="Authenticate and save a board on this computer")
    pair.add_argument("--address")
    pair.add_argument("--add-device", action="store_true", help="Add another board without replacing an existing one")
    status = commands.add_parser("status", help="Check a paired board; run without an MCP owner")
    status.add_argument("--device-id", help="Device ID from desk_list_devices")
    from .doctor import CLIENTS
    doctor = commands.add_parser("doctor", help="Check local pairing, reminder receipts, and probe setup")
    doctor.add_argument("--client", required=True, choices=CLIENTS)
    doctor.add_argument("--device-id", help="Device ID from desk_list_devices")
    doctor.add_argument("--shared-config", type=Path, help="Configuration that owns notification hooks")
    probe = doctor.add_mutually_exclusive_group()
    probe.add_argument("--token-probe", type=Path)
    probe.add_argument("--no-token-probe", action="store_true")
    find_usb = commands.add_parser("find-usb", help="Read UUID and MAC from explicitly selected USB ports")
    find_usb.add_argument("ports", nargs="+", metavar="PORT")
    find_usb.add_argument("--device-id", help="Only report a board with this persistent UUID")
    receiver = commands.add_parser("receiver-add-usb", help="Pair an independent notification receiver without changing the default board")
    receiver.add_argument("--port", required=True, help="Confirmed USB serial port")
    receiver.add_argument("--expected-identity", required=True, help="Expected board MAC address")
    receiver.add_argument("--credentials-file", type=Path, help="Optional private Wi-Fi credentials file")
    mcp = commands.add_parser("mcp", help="Start the local stdio MCP")
    mcp.add_argument(
        "--role-dir", type=Path, help="Optional absolute path to local role resources")
    token = mcp.add_mutually_exclusive_group()
    mcp.add_argument("--token-sessions", type=Path, help="Optional Codex session directory for quota history")
    token.add_argument("--token-collector", type=Path, help="Original desk display checkout for its multi-source collector")
    token.add_argument("--token-probe", type=Path, help="AgentSpeedProbe executable used by the original display")
    token.add_argument("--no-token-probe", action="store_true", help="Disable live Token collection, including an installed AgentSpeedProbe")
    mcp.add_argument("--usb-port", help="Confirmed serial port for USB fallback")
    mcp.add_argument("--shared-config", type=Path, help="Existing DeskPet configuration to share notification and quota sources")
    commands.add_parser("codex-hook", help="接收 Codex 完成、提问和回答事件（由 Codex 调用）")
    commands.add_parser("claude-hook", help="接收 Claude Code 完成和提问事件（由 Claude Code 调用）")
    antigravity_hook = commands.add_parser("antigravity-hook", help="接收 Antigravity 的提醒事件（由 Antigravity 调用）")
    antigravity_hook.add_argument("--phase", choices=("stop", "before", "after"), default="stop")
    commands.add_parser("opencode-hook", help="接收 OpenCode 的完成和待应答事件（由插件调用）")
    from .workbuddy_feedback import EDITIONS
    workbuddy = commands.add_parser("workbuddy-hook", help="接收 WorkBuddy 的完成和提问事件（由 WorkBuddy 调用）")
    workbuddy.add_argument("--edition", choices=EDITIONS, default="workbuddy")
    commands.add_parser("notification-worker", help="在后台发送待处理的提醒")
    commands.add_parser("receiver-worker", help="在后台发送独立设备的提醒")
    service = commands.add_parser("receiver-service", help="持续更新独立设备的数据和提醒")
    service.add_argument("--shared-config", type=Path, required=True, help="原设备配置，用于共享提醒和额度")
    service.add_argument("--token-probe", type=Path, help="AgentSpeedProbe 程序路径")
    args = parser.parse_args()
    args.config = args.config.expanduser().resolve()
    if args.command == "codex-hook":
        from .codex_hook import receive
        receive(args.config)
        return
    if args.command == "claude-hook":
        from .claude_feedback import deliver
        from .codex_hook import receive
        receive(args.config, handle=deliver)
        return
    if args.command == "antigravity-hook":
        from .antigravity_feedback import deliver, HOOK_RESPONSES
        from .codex_hook import receive
        receive(args.config, handle=deliver, response=HOOK_RESPONSES[args.phase])
        return
    if args.command == "opencode-hook":
        from .opencode_feedback import deliver
        from .codex_hook import receive
        receive(args.config, handle=deliver)
        return
    if args.command == "workbuddy-hook":
        from .workbuddy_feedback import deliver
        from .codex_hook import receive
        receive(args.config, handle=lambda store, event: deliver(store, event, edition=args.edition))
        return
    if args.command == "notification-worker":
        from .notifications import run_worker
        asyncio.run(run_worker(args.config))
        return
    if args.command == "receiver-worker":
        from .notifications import run_receiver_worker
        asyncio.run(run_receiver_worker(args.config))
        return
    if args.command == "receiver-service":
        from .notifications import run_live_receiver
        asyncio.run(run_live_receiver(args.config, args.shared_config.expanduser().resolve(), token_probe=args.token_probe))
        return
    if args.command == "mcp":
        from .mcp_server import create_server
        create_server(args.config, role_dir=args.role_dir, token_root=args.token_sessions,
                      usb_port=args.usb_port, token_collector=args.token_collector,
                      token_probe=args.token_probe, no_token_probe=args.no_token_probe,
                      shared_config_path=args.shared_config).run(transport="stdio")
        return
    try:
        if args.command == "doctor":
            from .doctor import diagnose
            shared = args.shared_config.expanduser().resolve() if args.shared_config else None
            probe_path = args.token_probe.expanduser().resolve() if args.token_probe else None
            result = diagnose(args.config, client=args.client, device_id=args.device_id, shared_config=shared,
                              token_probe=probe_path, no_token_probe=args.no_token_probe)
        elif args.command == "find-usb":
            from .usb_link import find_usb_devices
            result = find_usb_devices(args.ports, args.device_id)
        elif args.command == "receiver-add-usb":
            from .receiver_setup import add_usb_receiver
            result = asyncio.run(add_usb_receiver(args.config,args.port,args.expected_identity,args.credentials_file))
        else:
            if args.command == "pair" and not sys.stdin.isatty():
                parser.exit(2, "请在本机交互式终端输入配对码。\n")
            result = asyncio.run(interact(args))
        # Redirected output can use a Windows code page that cannot represent
        # paths to the installed app or to the client's hook configuration.
        encoding = sys.stdout.encoding or "utf-8"
        print(json.dumps(result, ensure_ascii=encoding.lower() not in ("utf-8", "utf8")))
    except KeyError:
        parser.exit(2, "找不到所选搭搭，请用 doctor 查看已配对设备。\n")
    except FileNotFoundError:
        parser.exit(2, "尚未配对，请运行 deskpet-platform pair。\n")
    except (OSError, RuntimeError, ValueError, TimeoutError) as error:
        if isinstance(error, OSError) and str(error) == "wifi_device_absent":
            from .wifi_link import NO_DEVICE_REPLY_HELP
            parser.exit(2, NO_DEVICE_REPLY_HELP + "\n")
        parser.exit(2, f"设备操作失败：{error}\n")


if __name__ == "__main__":
    main()
