"""Shared frozen entry for the GUI, CLI and background proxy."""
import multiprocessing
import os
import sys


def main():
    multiprocessing.freeze_support()
    for name in ("stdout", "stderr"):
        if getattr(sys, name) is None:
            setattr(sys, name, open(os.devnull, "w"))
    if sys.argv[1:2] == ["--serve-proxy"]:
        sys.argv.pop(1)
        from src.proxy_server import main as serve
        serve()
        return 0
    if len(sys.argv) > 1:
        from src.cli import main as cli
        return cli(sys.argv[1:])
    from src.desktop import run
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
