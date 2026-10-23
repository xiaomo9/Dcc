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

macOS 输出 `dist/DCC.app`；Windows 输出 `dist/DCC/DCC.exe`。双击打开启动窗口和浏览器面板，再次打开会复用面板。关闭启动窗口会关闭面板；代理继续运行，关闭前可在面板点击“停止全部”。

命令行入口是同目录中的 `dcc-cli`（Windows 为 `dcc-cli.exe`），支持原有命令。安装程序不修改 shell 配置或 PATH。macOS App 内的入口位于 `DCC.app/Contents/MacOS/dcc-cli`。

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

Windows CI 执行新增配置测试和打包后代理冒烟测试；现有完整测试集含 POSIX 专用测试，仍在 macOS 执行。Windows 实机交互与真实模型服务验证不能以这些测试代替。
