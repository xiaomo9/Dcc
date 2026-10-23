"""Installed copies keep mutable state outside the application bundle."""
import configparser
import os
from pathlib import Path
import sys

from .platform_support import fcntl


def data_dir() -> Path:
    override = os.environ.get("DCC_DATA_DIR")
    if override:
        return Path(override).expanduser().resolve()
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData/Local"))) / "DCC"
    return Path.home() / "Library/Application Support/DCC"


def config_path() -> Path:
    if not getattr(sys, "frozen", False) and not os.environ.get("DCC_DATA_DIR"):
        return Path(__file__).resolve().parent.parent / "dcc.ini"
    root = data_dir()
    root.mkdir(parents=True, exist_ok=True)
    path = root / "dcc.ini"
    with (root / "init.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            if not path.exists():
                llm = root / "llm.ini"
                if not llm.exists():
                    llm.write_text("", encoding="utf-8")
                    llm.chmod(0o600)
                parser = configparser.ConfigParser()
                parser["proxy"] = {"port": "4000", "llm_ini_path": str(llm).replace("%", "%%")}
                parser["output"] = {"dir": str(root / "output").replace("%", "%%")}
                parser["models"] = {}
                with path.open("w", encoding="utf-8") as out:
                    parser.write(out)
                path.chmod(0o600)
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)
    return path
