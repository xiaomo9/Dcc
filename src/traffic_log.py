"""dcc-proxy 日志基础设施 · 线程安全 · 按天 + 大小拆分 · 后台线程刷盘。

两种日志(共用 RollingWriter 基类):
    TrafficLog: 全量流量 JSONL(cc_in / gw_req / gw_chunk / gw_status / done)
                → output/dcc/log/YYYYMMDD[.N].jsonl
    ExecLog:    精简执行日志文本(启动/路由/错误/上游状态 · 不含会话上下文)
                → output/dcc/exec_log/YYYYMMDD[.N].log

用法(任意线程直接调用,非阻塞):
    TL = TrafficLog(base_dir="output/dcc/log")
    TL.log("cc_in", req_id="abc", body={...})

    EL = ExecLog(base_dir="output/dcc/exec_log")
    EL.log("[dcc-proxy] proxy 就绪 · port=4000")

文件命名规则:
    YYYYMMDD.jsonl / .log     当天首个日志
    YYYYMMDD.1.jsonl / .log   首个超 max_bytes 后拆的下一个
    YYYYMMDD.2.jsonl / .log   再拆

保留策略:
    实例化时可传 retention_days=3,启动时会清 base_dir 下老于 N 天的文件。
    不再进程内定期清 · 只在 boot 一次(避免死锁 · YAGNI)。

线程模型:
    - 调用方 queue.put_nowait() · 队满静默丢(不阻塞 SSE 转发)
    - 单一 daemon worker 顺序消费 → 单文件顺序追加,无跨线程锁
    - 主进程退出时 daemon 自动结束,可能丢 queue 末尾几条(可接受)
"""
from __future__ import annotations

import json
import queue
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path


DEFAULT_MAX_BYTES = 10 * 1024 * 1024
DEFAULT_QUEUE_MAX = 20000


class RollingWriter:
    """按天 + 按大小拆的滚动文件 · 后台线程刷盘 · 子类实现 _format(item)。"""

    ext = ""  # 子类覆盖(.jsonl / .log)
    worker_name = "dcc-rolling"

    def __init__(
        self,
        base_dir: Path | str,
        max_bytes: int = DEFAULT_MAX_BYTES,
        queue_max: int = DEFAULT_QUEUE_MAX,
        retention_days: int | None = None,
    ) -> None:
        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self.max_bytes = max_bytes
        self._q: queue.Queue = queue.Queue(maxsize=queue_max)
        self._fp = None
        self._cur_path: Path | None = None
        self._cur_size = 0
        self._cur_day = ""
        self._dropped = 0
        if retention_days is not None and retention_days > 0:
            self._cleanup_old(retention_days)
        self._worker = threading.Thread(
            target=self._run_worker, name=self.worker_name, daemon=True
        )
        self._worker.start()

    def _enqueue(self, item) -> None:
        try:
            self._q.put_nowait(item)
        except queue.Full:
            self._dropped += 1

    def stats(self) -> dict:
        return {
            "queue_size": self._q.qsize(),
            "dropped": self._dropped,
            "cur_path": str(self._cur_path) if self._cur_path else None,
            "cur_size": self._cur_size,
        }

    # ---- override ----

    def _format(self, item) -> bytes:
        """子类实现:把入队 item 转成待写入的 bytes(含换行)。"""
        raise NotImplementedError

    # ---- worker ----

    def _run_worker(self) -> None:
        while True:
            try:
                item = self._q.get(timeout=1.0)
            except queue.Empty:
                continue
            try:
                data = self._format(item)
                self._rotate_if_needed(len(data))
                assert self._fp is not None
                self._fp.write(data)
                self._fp.flush()
                self._cur_size += len(data)
            except (OSError, TypeError, ValueError):
                continue

    def _rotate_if_needed(self, adding: int) -> None:
        day = time.strftime("%Y%m%d")
        if self._fp is None or self._cur_day != day:
            self._open_current(day)
            return
        if self._cur_size + adding > self.max_bytes:
            seq = _seq_of(self._cur_path, day, self.ext) + 1
            self._open_seq(day, seq)

    def _open_current(self, day: str) -> None:
        if self._fp is not None:
            try:
                self._fp.close()
            except OSError:
                pass
            self._fp = None
        seq = _latest_seq(self.base_dir, day, self.ext)
        path = _path_of(self.base_dir, day, seq, self.ext)
        if path.exists() and path.stat().st_size >= self.max_bytes:
            seq += 1
            path = _path_of(self.base_dir, day, seq, self.ext)
        self._fp = path.open("ab")
        self._cur_path = path
        self._cur_size = path.stat().st_size if path.exists() else 0
        self._cur_day = day

    def _open_seq(self, day: str, seq: int) -> None:
        if self._fp is not None:
            try:
                self._fp.close()
            except OSError:
                pass
        path = _path_of(self.base_dir, day, seq, self.ext)
        self._fp = path.open("ab")
        self._cur_path = path
        self._cur_size = path.stat().st_size if path.exists() else 0
        self._cur_day = day

    # ---- retention ----

    def _cleanup_old(self, days: int) -> None:
        """清 base_dir 下超过 N 天前的文件(按文件名 YYYYMMDD 前缀)。

        语义:"保留 N 天" = 保留今天 + 前 N-1 天 · 共 N 天窗口。
        今天=D · retention=3 · 保留 [D-2, D-1, D] · 删 ≤ D-3(即 < D-N+1)。
        """
        try:
            cutoff = (datetime.now() - timedelta(days=days - 1)).strftime("%Y%m%d")
        except (ValueError, OverflowError):
            return
        for p in self.base_dir.iterdir():
            if not p.is_file():
                continue
            name = p.name
            if len(name) < 8 or not name[:8].isdigit():
                continue
            if name[:8] < cutoff:
                try:
                    p.unlink()
                except OSError:
                    pass


class TrafficLog(RollingWriter):
    """全量流量 JSONL · 每行一条事件 {ts, tid, kind, ...fields}。"""

    ext = ".jsonl"
    worker_name = "dcc-traffic-log"

    def log(self, kind: str, **fields) -> None:
        now = time.time()
        item = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(now))
            + f".{int((now % 1) * 1000):03d}",
            "tid": threading.get_ident(),
            "kind": kind,
            **fields,
        }
        self._enqueue(item)

    def _format(self, item) -> bytes:
        line = json.dumps(item, ensure_ascii=False, default=_json_fallback)
        return (line + "\n").encode("utf-8")


class ExecLog(RollingWriter):
    """精简执行日志文本 · 每行 `[HH:MM:SS] msg`。

    与 TrafficLog 的定位差异:
      TrafficLog 记录每个 chunk / cc_in / gw_req 等【会话级】数据(体积大)
      ExecLog   只记 【控制流】(启动/路由/错误/上游状态摘要 · 单条 <200 字)
    """

    ext = ".log"
    worker_name = "dcc-exec-log"

    def log(self, msg: str) -> None:
        now = time.time()
        ts = time.strftime("%H:%M:%S", time.localtime(now))
        self._enqueue(f"[{ts}] {msg}\n")

    def _format(self, item) -> bytes:
        return item.encode("utf-8")


# ---- helpers ----


def _path_of(base_dir: Path, day: str, seq: int, ext: str) -> Path:
    return base_dir / (f"{day}{ext}" if seq == 0 else f"{day}.{seq}{ext}")


def _seq_of(path: Path | None, day: str, ext: str) -> int:
    if path is None:
        return 0
    name = path.name
    if not name.endswith(ext):
        return 0
    stem = name[: -len(ext)]
    if stem == day:
        return 0
    if stem.startswith(day + "."):
        try:
            return int(stem[len(day) + 1 :])
        except ValueError:
            return 0
    return 0


def _latest_seq(base_dir: Path, day: str, ext: str) -> int:
    max_seq = 0
    found = False
    for p in base_dir.glob(f"{day}*{ext}"):
        found = True
        s = _seq_of(p, day, ext)
        if s > max_seq:
            max_seq = s
    return max_seq if found else 0


def _json_fallback(obj):
    if isinstance(obj, bytes):
        try:
            return obj.decode("utf-8", errors="replace")
        except (UnicodeDecodeError, AttributeError):
            return f"<bytes len={len(obj)}>"
    if isinstance(obj, (set, frozenset)):
        return list(obj)
    return repr(obj)
