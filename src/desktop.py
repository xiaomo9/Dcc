"""Desktop launcher; closing the window closes the panel, not the proxy."""
import json
import os
from pathlib import Path
import threading
import urllib.request
import webbrowser

from .app_paths import config_path, data_dir
from .platform_support import fcntl, pid_alive
from .ui import ConfigEditor, create_server


def run():
    import tkinter as tk
    from tkinter import messagebox

    window = tk.Tk()
    window.withdraw()
    server = None
    server_started = False
    state = data_dir() / "panel.json"
    try:
        path = config_path()
        with (data_dir() / "panel.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                if state.exists():
                    try:
                        previous = json.loads(state.read_text(encoding="utf-8"))
                        port = int(previous["port"])
                        token = previous["token"]
                        if pid_alive(int(previous["pid"])):
                            request = urllib.request.Request(
                                f"http://127.0.0.1:{port}/api/state",
                                headers={"X-DCC-Token": token})
                            with urllib.request.urlopen(request, timeout=2) as response:
                                if response.status == 200:
                                    webbrowser.open(f"http://127.0.0.1:{port}/#token={token}")
                                    return 0
                    except (OSError, ValueError, KeyError):
                        pass
                server, token = create_server(ConfigEditor(path, Path.home() / ".claude/settings.json"))
                state.write_text(json.dumps({"pid": os.getpid(), "port": server.server_port,
                                             "token": token}), encoding="utf-8")
                state.chmod(0o600)
                threading.Thread(target=server.serve_forever, daemon=True).start()
                server_started = True
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)
        url = f"http://127.0.0.1:{server.server_port}/#token={token}"
        window.title("DCC")
        window.geometry("380x180")
        window.resizable(False, False)
        tk.Label(window, text="DCC 配置面板已启动", font=("", 17)).pack(pady=(22, 12))
        tk.Button(window, text="打开配置面板", command=lambda: webbrowser.open(url)).pack()
        tk.Label(window, text="关闭此窗口会关闭面板，代理继续运行。\n停止代理请使用面板中的「停止全部」。").pack(pady=12)
        window.deiconify()
        webbrowser.open(url)
        window.mainloop()
        return 0
    except Exception:
        messagebox.showerror("DCC 启动失败", f"无法打开配置面板。请检查配置目录权限及配置格式：\n{data_dir()}")
        return 1
    finally:
        if server is not None:
            if server_started:
                server.shutdown()
            server.server_close()
            try:
                with (data_dir() / "panel.lock").open("a") as lock:
                    fcntl.flock(lock, fcntl.LOCK_EX)
                    try:
                        if state.exists() and json.loads(state.read_text(encoding="utf-8")).get("pid") == os.getpid():
                            state.unlink()
                    finally:
                        fcntl.flock(lock, fcntl.LOCK_UN)
            except (OSError, ValueError, AttributeError):
                pass  # 状态写入失败时仍须完成退出，下次启动会忽略损坏状态。
        try:
            window.destroy()
        except tk.TclError:
            pass
