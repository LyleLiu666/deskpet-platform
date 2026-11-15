# 局域网权限实验

此目录是 macOS 后台设备连接的权限实验，尚未作为正式安装程序。macOS 会分别管理登录后台进程的局域网权限；从终端成功连接不代表登录后台也能连接。

运行 `python3 tools/macos/build-network-access.py "$HOME/Applications/DeskPet Network Access.app"` 可构建一个新的授权应用；已有目标不会被覆盖。打开应用并点击「请求局域网权限」，在 macOS 提示时自行选择。窗口中的请求结果只表示一次网络发送结果，不能证明后台服务可用。

关联后台服务时，需使用 `local.deskpet.network-access` 作为 launchd 的 `AssociatedBundleIdentifiers`，并在授权后实测后台连接、前台接管和接管结束后的恢复。构建脚本使用临时签名；Apple 指出稳定的身份识别需要 Apple 签发的签名，因此正式分发前需检查签名与系统授权行为。未验证前不要自动安装或启动后台服务。
