"""Small OS boundaries shared by the CLI, proxy and editor."""
import os
import time

if os.name == "nt":
    import msvcrt
    import psutil

    class FileLocks:
        LOCK_EX = 1
        LOCK_UN = 2

        @staticmethod
        def flock(file, operation):
            file.seek(0)
            if operation == FileLocks.LOCK_UN:
                msvcrt.locking(file.fileno(), msvcrt.LK_UNLCK, 1)
                return
            while True:
                try:
                    msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
                    return
                except OSError as error:
                    if error.errno not in (13, 36):
                        raise
                    time.sleep(0.05)

    fcntl = FileLocks
else:
    import fcntl


def pid_alive(pid):
    if pid <= 0:
        return False
    if os.name == "nt":
        return psutil.pid_exists(pid)
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False
