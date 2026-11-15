import AppKit
import Darwin

final class AppDelegate: NSObject, NSApplicationDelegate {
    private var window: NSWindow!
    private let message = NSTextField(wrappingLabelWithString: "桌面屏需要访问局域网，才能持续显示电脑数据和新消息。请点击下方按钮，并在 macOS 提示时选择允许。")

    func applicationDidFinishLaunching(_ notification: Notification) {
        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 440, height: 170),
                          styleMask: [.titled, .closable], backing: .buffered, defer: false)
        window.title = "桌面屏局域网连接"
        window.center()
        message.frame = NSRect(x: 24, y: 88, width: 392, height: 55)
        let button = NSButton(title: "请求局域网权限", target: self, action: #selector(requestAccess))
        button.frame = NSRect(x: 24, y: 30, width: 160, height: 32)
        window.contentView?.addSubview(message)
        window.contentView?.addSubview(button)
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    @objc private func requestAccess() {
        let fd = socket(AF_INET, SOCK_DGRAM, 0)
        guard fd >= 0 else { message.stringValue = "无法请求权限，请检查网络设置。"; return }
        defer { close(fd) }
        var enabled: Int32 = 1
        _ = setsockopt(fd, SOL_SOCKET, SO_BROADCAST, &enabled, socklen_t(MemoryLayout<Int32>.size))
        var address = sockaddr_in()
        address.sin_len = UInt8(MemoryLayout<sockaddr_in>.size)
        address.sin_family = sa_family_t(AF_INET)
        address.sin_port = in_port_t(34915).bigEndian
        address.sin_addr.s_addr = INADDR_BROADCAST.bigEndian
        let bytes = Array("DESKPET-ACCESS-CHECK".utf8)
        let sent = bytes.withUnsafeBytes { buffer in
            withUnsafePointer(to: &address) { pointer in
                pointer.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                    sendto(fd, buffer.baseAddress, buffer.count, 0, $0, socklen_t(MemoryLayout<sockaddr_in>.size))
                }
            }
        }
        message.stringValue = sent >= 0 ? "局域网请求已发出。后台连接仍需要完成设备验证。" : "请在系统弹窗中允许局域网访问；如果没有弹窗，请检查系统设置中的局域网权限。"
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }
}

let application = NSApplication.shared
let delegate = AppDelegate()
application.delegate = delegate
application.setActivationPolicy(.regular)
application.run()
