# macOS 后台局域网权限实验工具

本目录用于实验 macOS 登录后台服务（`launchd` LaunchAgent）的局域网设备访问权限，**尚未作为正式安装程序发布**。

## 背景说明

macOS 对交互式终端与登录后台进程的「本地网络（Local Network）」权限分别独立管理：即使你在终端或 IDE 中已能正常连接托肯，也不代表脱离终端的 `launchd` 后台服务拥有局域网访问权限。

Apple 官方文档说明，后台守护进程需要具备明确的[应用归属（App Attribution）](https://developer.apple.com/documentation/technotes/tn3179-understanding-local-network-privacy)，且关联可执行文件需与宿主应用的 [Team Identifier 匹配](https://developer.apple.com/documentation/servicemanagement/updating-helper-executables-from-earlier-versions-of-macos)。

## 构建与实验步骤

1. **构建授权宿主应用**：
   在仓库根目录运行以下命令，在固定目录生成一个新的 `.app` 包（目标路径若已存在则不会覆盖）：
   ```sh
   python3 tools/macos/build-network-access.py "$HOME/Applications/DeskPet Network Access.app"
   ```
   打开生成的 `DeskPet Network Access.app`，点击窗口中的 **「请求局域网权限」**，并在 macOS 弹出系统授权提示时手动确认（注：窗口显示的探测结果仅代表该次交互式 UDP 发送结果，不能直接证明 `launchd` 后台服务已获权）。

2. **生成候选 `launchd` 配置文件（`plist`）**：
   构建产物的应用包内包含原生启动器 `DeskPetReceiverLauncher`——它作为 `.app` 包内的父进程直接启动电脑端服务（不经 shell 拼接命令，并负责转发停止信号），从而为后台服务提供应用归属。
   - 先运行 `python3 tools/macos/receiver_agent.py --help` 查看所需参数；
   - 再运行 `receiver_agent.py` 生成新路径的候选 `.plist` 文件（使用 `local.deskpet.network-access` 作为 `AssociatedBundleIdentifiers`）。
   - 该脚本仅生成候选 `.plist` 文件，**不会**自动注册或启动登录项，也不会覆盖已有文件。请勿将引用临时目录 `.app` 的 `.plist` 直接装入 `~/Library/LaunchAgents`；调整 `.app` 存放位置后需重新生成并检查 `.plist`。

## 限制与注意事项

由于本地构建默认仅使用临时签名（Ad-hoc Signature），无法保证跨次重新编译后的 macOS 权限身份（Code Requirement）保持稳定。请仅在用户亲自于系统界面完成授权，且实测验证过「后台独立连接」「前台 Agent 会话接管」与「前台退出后后台自动恢复」完整闭环后，再手动加载该 LaunchAgent。
