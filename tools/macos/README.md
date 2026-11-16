# 局域网权限实验

此目录是 macOS 后台设备连接的权限实验，尚未作为正式安装程序。macOS 会分别管理登录后台进程的局域网权限；从终端成功连接不代表登录后台也能连接。

运行 `python3 tools/macos/build-network-access.py "$HOME/Applications/DeskPet Network Access.app"` 可构建一个新的授权应用；已有目标不会被覆盖。打开应用并点击「请求局域网权限」，在 macOS 提示时自行选择。窗口中的请求结果只表示一次网络发送结果，不能证明后台服务可用。

构建结果包含原生 `DeskPetReceiverLauncher`。它作为应用包内的父进程启动电脑端服务，不通过 shell 拼接命令，并转发停止信号；这是为了让后台服务有明确的应用归属。可以先用 `receiver_agent.py --help` 查看参数，再用该脚本生成一个新路径的候选 plist。脚本只创建候选文件，不安装或启动登录项，也不会覆盖已有文件；输出 plist 使用 `local.deskpet.network-access` 作为 `AssociatedBundleIdentifiers`。不要直接安装引用临时目录中应用的候选 plist：应用位置确定后需重新生成并检查。

Apple 说明后台进程需要正确的[应用归属](https://developer.apple.com/documentation/technotes/tn3179-understanding-local-network-privacy)，并要求关联的可执行文件与应用的 [Team Identifier 匹配](https://developer.apple.com/documentation/servicemanagement/updating-helper-executables-from-earlier-versions-of-macos)。本机目前没有可用的 Apple 签名身份，构建结果只使用临时签名，不能据此保证权限身份稳定。需要用户在系统界面决定是否授权，并在授权后实测后台连接、前台接管和接管结束后的恢复；未完成验证前不要自动安装或启动后台服务。
