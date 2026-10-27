# 构建安装版

安装版包含 Python 和依赖。用户仍需填写模型服务地址、密钥和模型 ID；使用 Claude Code 会话还需要自行安装 Claude Code。未安装 Claude Code 不影响打开面板。

## 本地构建

在目标系统安装 Python 3.11（含 tkinter），创建虚拟环境，然后在仓库根目录执行：

```sh
python -m pip install -r packaging/requirements.txt
python -m unittest discover -s tests -p test_release.py
python packaging/build.py
python packaging/smoke.py
```

macOS 输出 `dist/DCC.app`；Windows 输出 `dist/DCC/DCC-panel.exe`。双击打开启动窗口和浏览器面板，再次打开会复用面板。关闭启动窗口会关闭面板；代理继续运行，关闭前可在面板点击“停止全部”。

Windows 命令行入口为安装目录中的 `dcc.exe`。安装或升级时可勾选「将 dcc 添加到当前用户 Path」，每次默认不勾选；选中后才添加目录，重新打开终端即可使用 `dcc 2`、`dcc status` 等原有命令。未勾选或添加失败时，完成页会提示用户手动加入 Path，并显示实际安装目录；图形面板仍可正常使用。取消勾选不会移除已存在的 Path 项。桌面和开始菜单快捷方式仍名为 DCC，打开图形面板。升级会移除旧的 `dcc-cli.exe`，卸载仅移除由安装程序添加的 Path 项，保留其他路径及用户自行添加的项。

macOS 命令行入口统一为 `DCC.app/Contents/MacOS/dcc`，图形入口为 `DCC-panel`，应用名称仍是 DCC。先将 App 放到固定安装位置，然后点击启动窗口的“配置 dcc 终端命令…”。确认后才在当前用户的 shell 启动文件中添加有标记的 Path 配置，支持 zsh（`.zshrc`，遵循 `ZDOTDIR`）和 bash（已有的 `.bash_profile`、`.bash_login` 或 `.profile`，没有则创建 `.bash_profile`）。保留已有内容和文件权限，重复配置不重复写入；取消或启动 App 本身不会修改 shell 配置。重新打开终端后可直接运行 `dcc 2`；不配置时可使用完整路径 `/Applications/DCC.app/Contents/MacOS/dcc 2`。若移动 App，需从新位置重新配置；删除 App 后可手动移除 shell 文件中 `DCC PATH` 标记之间的配置块。

Windows 需另外安装 Inno Setup 6，再运行 `ISCC /DAppVersion=<版本号> packaging/windows.iss` 生成安装程序。macOS DMG 生成步骤见 `.github/workflows/release.yml`。

## 数据与升级

- macOS：`~/Library/Application Support/DCC/`
- Windows：`%LOCALAPPDATA%\DCC\`
- 测试可用 `DCC_DATA_DIR` 指向独立临时目录。

首次启动创建空的 `dcc.ini` 和 `llm.ini`。不会导入或覆盖源码配置、`~/.dm/llm.ini` 或 Claude 设置；需要同步 Claude 设置时在面板操作。升级不会重写用户配置，卸载保留数据目录。打包仅包含显式列出的静态资源，不包含仓库里的 dcc.ini、日志、密钥和使用记录。

## 自动构建与验收

手动触发 GitHub Actions，或推送 `v` 开头的版本标签，分别构建 macOS ARM64、macOS Intel 和 Windows x64 的候选安装包。工作流仅上传构建产物，不自动公开发布。

当前构建为未签名候选版本。正式发布前应添加 macOS Developer ID 签名和公证、Windows 代码签名，并在干净电脑验收：

1. 没有 Python 时安装、打开面板、重复启动及关闭。
2. 空配置录入、保存、模型连接测试和真实对话。
3. 代理启动、重启、停止；端口冲突时明确失败。
4. 多个真实 Claude Code 会话登记和停止，不能停止其他工作会话。
5. 升级保留配置，中文和带空格的用户目录可用。

Windows CI 执行新增配置测试、打包后代理冒烟测试，以及安装程序的命令发现、重复升级、Path 保留与卸载清理验证；现有完整测试集含 POSIX 专用测试，仍在 macOS 执行。Windows 实机交互与真实模型服务验证不能以这些测试代替。

## 用户检查更新

面板打开时自动检查一次 GitHub Releases，右上角也可手动“检查更新”；成功结果缓存 5 分钟，不定时轮询。只有正式 Release 的数字版本高于当前 `src/VERSION` 才提示更新，草稿、预发布不推送。网络失败不会阻塞配置编辑和代理操作。

发布时，标签采用 `v主版本.次版本.修订号`（例如 `v2.1.14`），并与源码 VERSION 一致。将 Actions 下载产物解压，把三个安装包分别上传到同一个正式 Release，保持文件名：`DCC-macOS-arm64.dmg`、`DCC-macOS-x64.dmg`、`DCC-Windows-x64-Setup.exe`。仅上传 Actions 压缩包不会产生安装包下载按钮。

用户可查看说明、稍后处理或由浏览器下载安装包。下载本身不修改配置、不停止会话；安装前需保存修改、停止代理和会话、关闭 DCC，再运行 Windows 安装程序或替换 macOS Applications 中的 App。当前不包含静默安装、自动替换或自动重启。新版读取同一个用户数据目录。
