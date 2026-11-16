"""A short-lived localhost page for entering board credentials outside the chat."""
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import secrets
import threading
import time
from urllib.parse import parse_qs

from .wifi_link import NO_DEVICE_REPLY_HELP

PAGE = """<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>连接搭搭</title>
<style>body{font:16px system-ui;color:#203632;background:#eef4f2;margin:0}
main{max-width:560px;margin:8vh auto;padding:24px}form{background:white;padding:24px;border-radius:12px}
p{line-height:1.6}input,button{box-sizing:border-box;width:100%;font:inherit;padding:12px;margin:8px 0;
border-radius:6px;border:1px solid #759088}button{background:#206b58;color:white;border:0}</style>
<main><h1>连接搭搭</h1>{notice}<p>让搭搭连上 Wi-Fi，在桌面设置里打开「电脑配对」。用手机扫码复制，
再通过可信方式粘贴到这台电脑。</p>
<form method="post"><label>配对码<input name="code" type="password" autocomplete="off" required></label>
<label>搭搭的局域网地址（找不到设备时填写）<input name="address" maxlength="15" inputmode="decimal"></label>
<button>连接搭搭</button><p>配对码只提交到这台电脑，不需要发到聊天里。</p></form></main></html>"""
PAIRED = "已验证并保存配对信息。可以关闭此页面，在聊天中查看连接状态。"
NOTICE = "{notice}"


class PairPortal:
    def __init__(self, pair, *, lifetime=300):
        self.pair = pair
        self.lifetime = lifetime
        self.created = time.monotonic()
        self.state = "waiting"
        self.device = None
        self.lock = threading.Lock()
        self.server = self.thread = self.timer = None

    def start(self):
        portal = self
        path = "/" + secrets.token_urlsafe(24) + "/"

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def allowed(self):
                expected = f"127.0.0.1:{self.server.server_port}"
                return (self.client_address[0] == "127.0.0.1" and
                        self.headers.get("Host") == expected and self.path == path)

            def page(self, status, body):
                data = body.encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                # Chromium sends Origin: null for a POST form under no-referrer.
                self.send_header("Referrer-Policy", "same-origin")
                self.send_header("Content-Security-Policy",
                                 "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; base-uri 'none'")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def form(self, status=200, message=""):
                notice = f'<p role="alert">{escape(message)}</p>' if message else ""
                self.page(status, PAGE.replace(NOTICE, notice))

            def do_GET(self):
                if not self.allowed():
                    return self.page(404, "Not found")
                if time.monotonic() - portal.created >= portal.lifetime:
                    return self.page(410, "配对页面已过期，请重新打开。")
                with portal.lock:
                    state = portal.state
                if state == "paired":
                    return self.page(200, PAIRED)
                if state == "checking":
                    return self.page(200, "正在验证搭搭，请等待提交结果。")
                self.form()

            def do_POST(self):
                if not self.allowed():
                    return self.page(404, "Not found")
                if self.headers.get("Origin") != f"http://127.0.0.1:{self.server.server_port}":
                    return self.page(403, "请求来源无效。")
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 0 < length <= 512:
                        raise ValueError()
                    values = parse_qs(self.rfile.read(length).decode("utf-8"),
                                      strict_parsing=True, keep_blank_values=True)
                    if set(values) - {"code", "address"} or "code" not in values or any(len(v) != 1 for v in values.values()):
                        raise ValueError()
                    address = values.get("address", [""])[0].strip() or None
                except (ValueError, UnicodeError):
                    return self.form(400, "配对信息无效，请重新输入。")
                with portal.lock:
                    if portal.state == "checking" and time.monotonic() - portal.created < portal.lifetime:
                        return self.page(409, "正在验证搭搭，请等待上一次提交的结果。")
                    if portal.state != "waiting" or time.monotonic() - portal.created >= portal.lifetime:
                        return self.page(410, "配对页面已使用或过期。")
                    portal.state = "checking"
                try:
                    result = portal.pair(values["code"][0], address)
                except (ValueError, RuntimeError, OSError) as error:
                    with portal.lock:
                        portal.state = "waiting"
                    if isinstance(error, OSError) and str(error) in ("wifi_device_absent", "wifi_discovery_unreachable"):
                        return self.form(400, NO_DEVICE_REPLY_HELP)
                    return self.form(400, "配对未成功。请确认配对码、Wi-Fi 和设备占用情况后重试。")
                with portal.lock:
                    portal.state, portal.device = "paired", result["device"]
                self.page(200, PAIRED)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.server.server_port}{path}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.timer = threading.Timer(self.lifetime, self.close)
        self.timer.daemon = True
        self.timer.start()
        return self

    def status(self):
        with self.lock:
            return {"state": self.state, "device": self.device}

    def close(self):
        with self.lock:
            server, self.server = self.server, None
            if self.state == "waiting":
                self.state = "expired"
        if self.timer:
            self.timer.cancel()
        if server:
            server.shutdown()
            server.server_close()
        if self.thread and self.thread is not threading.current_thread():
            self.thread.join(timeout=2)
