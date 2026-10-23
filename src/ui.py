"""本机配置面板：编辑模型入口和槽位，保存后重载代理。"""
from __future__ import annotations

import configparser
from contextlib import contextmanager
from .platform_support import fcntl
import hashlib
import http.server
import json
import math
import os
from pathlib import Path
import re
import secrets
import sqlite3
import stat
import tempfile
import time
import urllib.parse
import webbrowser

import requests

from . import cc as CC
from . import proxy as P
from . import settings as S
from .config import load as load_config
from .token_usage import UsageStore


_HEADER = re.compile(r"^\s*\[([^\]\r\n]+)\]\s*(?:[#;].*)?$")
_OPTION = re.compile(r"^(\s*[^\s#;:=][^:=]*?\s*[=:]\s*)(.*?)(\r?\n|$)")
_NAME = re.compile(r"^[^\[\]\r\n#;=:]+$")
_KEY = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]*$")
_PROTOCOLS = {"openai", "responses", "anthropic"}
_MAX_BODY = 1024 * 1024


class SaveError(RuntimeError):
    """对用户可展示的保存与恢复结果，不包含密钥或上游响应。"""


def _read(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


def _digest(raw: bytes | None) -> str:
    return hashlib.sha256(raw if raw is not None else b"\0missing").hexdigest()


def _parser(raw: bytes | None) -> configparser.ConfigParser:
    p = configparser.ConfigParser(interpolation=None)
    if raw is not None:
        p.read_string(raw.decode("utf-8"))
    return p


def _items(p: configparser.ConfigParser, section: str) -> list[tuple[str, str]]:
    if not p.has_section(section):
        return []
    defaults = set(p.defaults())
    return [(k, v) for k, v in p.items(section) if k not in defaults]


def _sections(text: str) -> tuple[str, list[tuple[str, str, str]]]:
    lines = text.splitlines(keepends=True)
    starts = [(i, m.group(1)) for i, line in enumerate(lines)
              if (m := _HEADER.match(line.rstrip("\r\n")))]
    if not starts:
        return text, []
    prefix = "".join(lines[:starts[0][0]])
    sections = []
    for index, (start, name) in enumerate(starts):
        end = starts[index + 1][0] if index + 1 < len(starts) else len(lines)
        sections.append((name, lines[start], "".join(lines[start + 1:end])))
    return prefix, sections


def _line_key(line: str) -> str | None:
    match = _OPTION.match(line)
    if not match:
        return None
    return match.group(1).rsplit("=", 1)[0].rsplit(":", 1)[0].strip().lower()


def _edit_body(body: str, options: list[tuple[str, str]], *, ordered: bool = False) -> str:
    wanted = {k.lower(): (k, str(v)) for k, v in options}
    lines = body.splitlines(keepends=True)
    existing = {_line_key(line): line for line in lines if _line_key(line)}

    def line_for(key: str, value: str) -> str:
        old = existing.get(key.lower())
        if old is None:
            return f"{key} = {value}\n"
        match = _OPTION.match(old)
        if match and match.group(2).strip() == value:
            return old
        return f"{match.group(1)}{value}{match.group(3) or chr(10)}"

    if ordered and [key for line in lines if (key := _line_key(line))] == [k.lower() for k, _ in options]:
        ordered = False
    if ordered:
        # 保留原注释和空行，只重新排布键值行。
        other = [line for line in lines if _line_key(line) is None]
        if other and other[-1].strip():
            other.append("\n")
        return "".join(other + [line_for(k, v) for k, v in options])

    seen = set()
    result = []
    for line in lines:
        key = _line_key(line)
        if key is None:
            result.append(line)
        elif key in wanted:
            result.append(line_for(*wanted[key]))
            seen.add(key)
    for key, value in options:
        if key.lower() not in seen:
            if result and not result[-1].endswith("\n"):
                result.append("\n")
            result.append(line_for(key, value))
    return "".join(result)


def _patch_sections(text: str, edits: dict[str, list[tuple[str, str]]],
                    *, renames: dict[str, str] | None = None,
                    deletes: set[str] | None = None,
                    ordered: set[str] | None = None) -> str:
    prefix, sections = _sections(text)
    renames, deletes, ordered = renames or {}, deletes or set(), ordered or set()
    out, seen = [prefix], set()
    for name, header, body in sections:
        if name in deletes:
            continue
        target = renames.get(name, name)
        if target in edits:
            seen.add(target)
            if target != name:
                header = f"[{target}]" + ("\n" if header.endswith("\n") else "")
            body = _edit_body(body, edits[target], ordered=target in ordered)
        out.extend((header, body))
    for name, options in edits.items():
        if name not in seen:
            if out and out[-1] and not out[-1].endswith("\n"):
                out.append("\n")
            if out and out[-1] and not out[-1].endswith("\n\n"):
                out.append("\n")
            out.extend((f"[{name}]\n", _edit_body("", options, ordered=name in ordered)))
    return "".join(out)


def _replace_atomic(path: Path, raw: bytes, mode: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = None
    try:
        with tempfile.NamedTemporaryFile(mode="wb", dir=path.parent,
                                         prefix=path.name + ".", delete=False) as f:
            tmp = Path(f.name)
            f.write(raw)
            if mode is not None and hasattr(os, "fchmod"):
                os.fchmod(f.fileno(), mode)
        os.replace(tmp, path)
    finally:
        if tmp is not None:
            tmp.unlink(missing_ok=True)


@contextmanager
def _lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


class ConfigEditor:
    def __init__(self, dcc_path: Path, settings_path: Path):
        self.dcc_path = dcc_path.resolve()
        self.settings_path = settings_path.expanduser().resolve()
        dcc = _parser(_read(self.dcc_path))
        self.llm_path = Path(dcc.get("proxy", "llm_ini_path", fallback="~/.dm/llm.ini")).expanduser().resolve()
        self.lock_path = self.llm_path.parent / ".dcc-ui.lock"

    def state(self) -> dict:
        dcc_raw, llm_raw = _read(self.dcc_path), _read(self.llm_path)
        dcc, llm = _parser(dcc_raw), _parser(llm_raw)
        models = []
        for section in llm.sections():
            fields = {k: v for k, v in llm.items(section) if k not in ("api_key", "section")}
            models.append({"section": section, "original_section": section,
                           "fields": fields, "has_key": bool(llm.get(section, "api_key", fallback=""))})
        slots = []
        for slot, section in _items(dcc, "models"):
            slots.append({"id": slot, "section": section,
                          "name": dcc.get("model_names", slot, fallback=""),
                          "context": dcc.get("context", slot, fallback="")})
        groups = [{"name": name, "members": [s.strip() for s in value.split(",") if s.strip()]}
                  for name, value in _items(dcc, "groups")]
        failover = dict(_items(dcc, "failover"))
        instances = 0
        if llm_raw is not None:
            try:
                cfg = load_config(self.dcc_path)
                instances = len(CC.list_instances(cfg))
            except (SystemExit, OSError, ValueError):
                pass
        return {"models": models, "slots": slots, "groups": groups, "failover": failover,
                "instances": instances,
                "revisions": {"dcc": _digest(dcc_raw), "llm": _digest(llm_raw),
                              "settings": _digest(_read(self.settings_path))}}

    def reveal_key(self, section: str) -> str:
        p = _parser(_read(self.llm_path))
        if not p.has_section(section):
            raise ValueError("模型入口不存在")
        return p.get(section, "api_key", fallback="")

    def runtime_status(self) -> dict:
        try:
            with _lock(self.lock_path):
                cfg = load_config(self.dcc_path)
                return {"proxy": P.status(cfg), "instances": len(CC.list_instances(cfg))}
        except (OSError, ValueError, configparser.Error, RuntimeError, SystemExit) as error:
            raise RuntimeError("运行状态读取失败，请检查磁盘配置和终端日志") from error

    def runtime_action(self, data: dict) -> dict:
        if not isinstance(data, dict) or data.get("action") not in ("start", "stop", "reset"):
            raise ValueError("运行操作无效")
        action = data["action"]
        try:
            with _lock(self.lock_path):
                cfg = load_config(self.dcc_path)
                stopped, proxy_stopped = 0, False
                if action == "start":
                    P.start(cfg, fixed_port=True)
                else:
                    stopped = CC.stop_all(cfg)
                    proxy_stopped = P.stop_proxy(cfg) if action == "stop" else P.restart(cfg)
                return {"stopped": stopped, "proxy_stopped": proxy_stopped}
        except (OSError, ValueError, configparser.Error, RuntimeError, SystemExit) as error:
            label = {"start": "启动代理", "stop": "停止全部", "reset": "重启代理"}[action]
            raise RuntimeError(label + "失败，请检查磁盘配置、配置端口和终端日志") from error

    def sync_settings(self) -> dict:
        try:
            with _lock(self.lock_path):
                cfg = load_config(self.dcc_path)
                changed = S.write(cfg, self.settings_path)
                return {"changed": changed,
                        "settings_revision": _digest(_read(self.settings_path))}
        except (OSError, ValueError, configparser.Error, RuntimeError, SystemExit) as error:
            raise RuntimeError("同步 Claude 配置失败，请检查磁盘配置、Claude 设置格式和文件权限") from error

    def usage_summary(self, days: int) -> dict:
        dcc = configparser.ConfigParser()
        dcc.read(self.dcc_path, encoding="utf-8")
        output_dir = Path(dcc.get("output", "dir", fallback=str(self.dcc_path.parent / "output/dcc"))).expanduser()
        store = UsageStore(output_dir / "token_usage.sqlite3")
        store.import_logs(output_dir / "log")
        return store.summary(days)

    def _assert_current(self, draft: dict) -> None:
        rev = draft.get("revisions") or {}
        if any(rev.get(name) != _digest(_read(path)) for name, path in (
            ("dcc", self.dcc_path), ("llm", self.llm_path),
            ("settings", self.settings_path))):
            raise ValueError("配置文件已被外部修改，请重新加载面板")

    def prepare(self, draft: dict) -> dict:
        self._assert_current(draft)
        if not isinstance(draft.get("models"), list) or not isinstance(draft.get("slots"), list):
            raise ValueError("模型和槽位必须是列表")
        llm_now = _parser(_read(self.llm_path))
        models, renames, seen_models, seen_sources = {}, {}, set(), set()
        original_sections = set(llm_now.sections())
        for item in draft["models"]:
            name = item.get("section", "").strip()
            source = item.get("original_section") or ""
            if not _NAME.fullmatch(name) or name.upper() == "DEFAULT" or name in seen_models:
                raise ValueError("模型入口名称无效或重复")
            if source and (source not in original_sections or source in seen_sources):
                raise ValueError("模型入口原名称已失效，请重新加载")
            if source:
                seen_sources.add(source)
            fields = item.get("fields")
            if not isinstance(fields, dict):
                raise ValueError("模型字段格式错误")
            values = {}
            for key, value in fields.items():
                if not _KEY.fullmatch(key) or key.lower() in ("api_key", "section") or not isinstance(value, str) or "\n" in value or "\r" in value:
                    raise ValueError("模型扩展字段无效")
                values[key.lower()] = value.strip()
            values.setdefault("supports_images", "false")
            if "api_key" in item:
                secret = item["api_key"]
            else:
                secret = llm_now.get(source, "api_key", fallback="") if source else ""
            if not isinstance(secret, str) or "\n" in secret or "\r" in secret:
                raise ValueError("密钥格式错误")
            values["api_key"] = secret
            if any(not values.get(k) for k in ("base_url", "api_key", "model")):
                raise ValueError(f"模型入口 {name} 缺少必填字段")
            if values.get("protocol") not in _PROTOCOLS:
                raise ValueError(f"模型入口 {name} 协议无效")
            url = urllib.parse.urlsplit(values["base_url"])
            if url.scheme not in ("http", "https") or not url.hostname or url.username or url.password:
                raise ValueError(f"模型入口 {name} 上游地址无效")
            if source and source != name:
                renames[source] = name
            seen_models.add(name)
            models[name] = list(values.items())

        slots, seen_slots = [], set()
        for item in draft["slots"]:
            slot = str(item.get("id", "")).strip()
            section = item.get("section", "")
            if not slot.isdecimal() or int(slot) < 1 or slot in seen_slots:
                raise ValueError("槽位号必须是唯一的正整数")
            if section not in models:
                raise ValueError(f"槽位 {slot} 引用了不存在的模型入口；请先调整引用")
            for key in ("name", "context"):
                value = item.get(key, "")
                if not isinstance(value, str) or "\n" in value or "\r" in value:
                    raise ValueError("槽位字段无效")
            slots.append(item)
            seen_slots.add(slot)
        if not slots:
            raise ValueError("至少保留一个有效槽位")
        deleted = original_sections - seen_sources
        for slot, section in _items(_parser(_read(self.dcc_path)), "models"):
            if section in deleted and any(str(item["id"]) == slot for item in slots):
                raise ValueError(f"模型入口 {section} 仍被槽位 {slot} 引用")

        groups, group_names, grouped_slots = [], set(), set()
        for group in draft.get("groups", []):
            name = str(group.get("name", "")).strip()
            members = group.get("members", [])
            if not _NAME.fullmatch(name) or name in group_names or not isinstance(members, list):
                raise ValueError("分组名称或成员无效")
            if len(set(members)) != len(members) or any(m not in seen_slots for m in members):
                raise ValueError("分组成员必须是已配置的唯一槽位")
            if grouped_slots.intersection(members):
                raise ValueError("同一槽位不能属于多个分组")
            grouped_slots.update(members)
            groups.append((name, ",".join(members)))
            group_names.add(name)
        fo = draft.get("failover") or {}
        try:
            timeout = float(fo.get("soft_timeout", 12))
            if not math.isfinite(timeout) or timeout <= 0 or int(fo.get("max_candidates", 3)) < 1:
                raise ValueError
            if str(fo.get("switch_on_429", "true")).lower() not in ("true", "false"):
                raise ValueError
        except (TypeError, ValueError):
            raise ValueError("切换参数无效") from None

        llm_text = (_read(self.llm_path) or b"").decode("utf-8")
        dcc_text = (_read(self.dcc_path) or b"").decode("utf-8")
        llm_text = _patch_sections(llm_text, models, renames=renames, deletes=deleted)
        edits = {
            "models": [(str(s["id"]), s["section"]) for s in slots],
            "groups": groups,
            "failover": [("soft_timeout", str(fo.get("soft_timeout", 12))),
                         ("switch_on_429", str(fo.get("switch_on_429", "true")).lower()),
                         ("max_candidates", str(fo.get("max_candidates", 3)))],
        }
        for section, field in (("model_names", "name"), ("context", "context")):
            edits[section] = [(str(s["id"]), s[field]) for s in slots
                              if s[field].strip()]
        dcc_text = _patch_sections(dcc_text, edits, deletes={"model_suffix"},
                                   ordered={"models", "groups"})
        _parser(llm_text.encode())
        _parser(dcc_text.encode())
        return {"llm": llm_text, "dcc": dcc_text}

    def save(self, draft: dict) -> dict:
        with _lock(self.lock_path):
            prepared = self.prepare(draft)
            before = {self.llm_path: _read(self.llm_path),
                      self.dcc_path: _read(self.dcc_path),
                      self.settings_path: _read(self.settings_path)}
            old_modes = {path: stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o600
                         for path in before}
            changed = {self.llm_path: prepared["llm"].encode(),
                       self.dcc_path: prepared["dcc"].encode()}
            changed = {p: raw for p, raw in changed.items() if raw != before[p]}
            if not changed:
                return {"changed": False, "stopped": 0}
            previous_cfg = load_config(self.dcc_path) if before[self.llm_path] is not None else None
            was_running = bool(previous_cfg and P.read_pid(previous_cfg.proxy_pidfile)
                               and P.status(previous_cfg)["healthy"])
            restart_attempted = False
            try:
                for path, raw in changed.items():
                    _replace_atomic(path, raw, 0o600 if path == self.llm_path else old_modes[path])
                cfg = load_config(self.dcc_path)
                old_slots = [(s.slot, s.local_name)
                             for s in previous_cfg.slots.values()] if previous_cfg else []
                new_slots = [(s.slot, s.local_name) for s in cfg.slots.values()]
                if old_slots != new_slots or _parser(before[self.dcc_path]).has_section("model_suffix"):
                    S.write(cfg, self.settings_path)
                stopped = CC.stop_all(cfg)
                restart_attempted = True
                P.restart(cfg)
                return {"changed": True, "stopped": stopped}
            except BaseException as error:
                restore_failed = False
                for path in (*changed, self.settings_path):
                    try:
                        original = before[path]
                        if original is None:
                            path.unlink(missing_ok=True)
                        elif _read(path) != original:
                            _replace_atomic(path, original, old_modes[path])
                    except OSError:
                        restore_failed = True
                proxy_failed = False
                if restart_attempted and was_running and previous_cfg:
                    try:
                        P.restart(previous_cfg)
                    except BaseException:
                        proxy_failed = True
                elif restart_attempted:
                    try:
                        P.stop_proxy(cfg)
                    except BaseException:
                        proxy_failed = True
                result = "保存失败"
                if restore_failed:
                    result += "；部分文件未恢复，请检查配置文件和文件权限"
                else:
                    result += "；文件已恢复"
                if proxy_failed:
                    result += "；旧代理恢复失败，请检查代理日志"
                elif restart_attempted:
                    result += "；代理已恢复到保存前状态，Claude CLI 会话需手工恢复"
                else:
                    result += "；代理未重载"
                raise SaveError(result + f"（{type(error).__name__}）") from error

    def test_connection(self, item: dict) -> dict:
        source = item.get("original_section") or ""
        fields = item.get("fields") or {}
        key = item.get("api_key", self.reveal_key(source) if source else "")
        if not isinstance(fields, dict) or not all(fields.get(k) for k in ("base_url", "model", "protocol")) or not key:
            raise ValueError("请先填好地址、密钥、模型 ID 和协议")
        protocol = fields["protocol"]
        if protocol not in _PROTOCOLS:
            raise ValueError("协议无效")
        base = fields["base_url"].rstrip("/")
        if not base.startswith(("http://", "https://")):
            raise ValueError("上游地址必须是 HTTP 或 HTTPS")
        headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
        prompt = "Reply OK"
        if protocol == "openai":
            url = base + "/chat/completions"
            payload = {"model": fields["model"], "messages": [{"role": "user", "content": prompt}],
                       "max_tokens": 16, "stream": True}
        elif protocol == "responses":
            url = base + "/responses"
            payload = {"model": fields["model"],
                       "input": [{"role": "user", "content": [{"type": "input_text", "text": prompt}]}],
                       "max_output_tokens": 16, "stream": True}
        else:
            url = base + "/v1/messages"
            payload = {"model": fields["model"], "messages": [{"role": "user", "content": prompt}],
                       "max_tokens": 16, "stream": True}
        started = time.monotonic()
        try:
            with requests.post(url, headers=headers, json=payload, timeout=(5, 30), stream=True) as response:
                if response.status_code >= 400:
                    return {"ok": False, "status": response.status_code,
                            "duration_ms": round((time.monotonic() - started) * 1000),
                            "message": f"上游返回 HTTP {response.status_code}"}
                response.encoding = "utf-8"
                chunks, completed, reasoning_seen = [], False, False
                for raw in response.iter_lines(decode_unicode=True):
                    if not raw or not raw.startswith("data:"):
                        continue
                    value = raw[5:].strip()
                    if value == "[DONE]":
                        completed = True
                        break
                    event = json.loads(value)
                    if protocol == "openai":
                        for choice in event.get("choices", []):
                            delta = choice.get("delta", {})
                            chunks.append(delta.get("content") or "")
                            reasoning_seen |= bool(delta.get("reasoning_content"))
                            completed |= choice.get("finish_reason") is not None
                    elif protocol == "responses":
                        if event.get("type") == "response.output_text.delta":
                            chunks.append(event.get("delta") or "")
                        reasoning_seen |= (event.get("type") == "response.reasoning_summary_text.delta"
                                           and bool(event.get("delta")))
                        completed |= event.get("type") == "response.completed"
                    else:
                        if event.get("type") == "content_block_delta":
                            delta = event.get("delta", {})
                            chunks.append(delta.get("text") or "")
                            reasoning_seen |= bool(delta.get("thinking"))
                        completed |= event.get("type") == "message_stop"
                    if completed:
                        break
                answer = "".join(chunks)
                elapsed = round((time.monotonic() - started) * 1000)
                ok = completed and (bool(answer.strip()) or reasoning_seen)
                return {"ok": ok, "status": response.status_code,
                        "duration_ms": elapsed,
                        "message": (answer[:200] if completed and answer.strip() else
                                    "上游已响应并结束，但测试额度内没有可见文本（仅有推理内容）" if ok else
                                    "上游未正常完成文本输出")}
        except (requests.RequestException, ValueError, KeyError, TypeError) as error:
            return {"ok": False, "duration_ms": round((time.monotonic() - started) * 1000),
                    "message": f"测试失败：{type(error).__name__}"}


def create_server(editor: ConfigEditor, token: str | None = None) -> tuple[http.server.ThreadingHTTPServer, str]:
    token = token or secrets.token_urlsafe(32)
    page = (Path(__file__).parent / "ui.html").read_bytes()
    icon = (Path(__file__).parent.parent / "docs/assets/dcc-icon-pink.png").read_bytes()
    guide = (Path(__file__).parent / "guide_intro.html").read_bytes()
    advanced_guide = (Path(__file__).parent / "guide.html").read_bytes()
    docs = {path: (Path(__file__).parent.parent / path.lstrip("/")).read_bytes() for path in (
        "/getting-started.md", "/dcc.md", "/docs/design.md", "/docs/known-issues.md")}

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args) -> None:
            pass

        def _send(self, status: int, body: bytes, content_type: str = "application/json") -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; connect-src 'self'")
            self.end_headers()
            self.wfile.write(body)

        def _authorized(self) -> bool:
            host = f"127.0.0.1:{self.server.server_port}"
            origin = self.headers.get("Origin")
            return (self.headers.get("Host") == host
                    and secrets.compare_digest(self.headers.get("X-DCC-Token", ""), token)
                    and (not origin or origin == f"http://{host}"))

        def _json(self, status: int, data: dict) -> None:
            self._send(status, json.dumps(data, ensure_ascii=False).encode())

        def do_GET(self) -> None:
            path = urllib.parse.urlsplit(self.path).path
            if path in ("/", "/icon.png", "/guide", *docs) and self.headers.get("Host") == f"127.0.0.1:{self.server.server_port}":
                if path == "/icon.png":
                    self._send(200, icon, "image/png")
                elif path == "/guide":
                    self._send(200, advanced_guide if urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).get("doc") else guide,
                               "text/html; charset=utf-8")
                elif path in docs:
                    self._send(200, docs[path], "text/plain; charset=utf-8")
                else:
                    self._send(200, page, "text/html; charset=utf-8")
                return
            if not self._authorized():
                self._json(403, {"error": "访问被拒绝"})
                return
            try:
                if self.path == "/api/state":
                    self._json(200, editor.state())
                elif self.path == "/api/runtime":
                    try:
                        self._json(200, editor.runtime_status())
                    except RuntimeError as error:
                        self._json(400, {"error": str(error)})
                elif urllib.parse.urlsplit(self.path).path == "/api/usage":
                    params = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
                    try:
                        self._json(200, editor.usage_summary(int(params.get("days", ["7"])[0])))
                    except (OSError, sqlite3.Error):
                        self._json(503, {"error": "本地用量统计暂不可用"})
                elif self.path.startswith("/api/key?"):
                    name = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).get("section", [""])[0]
                    self._json(200, {"api_key": editor.reveal_key(name)})
                else:
                    self._json(404, {"error": "页面不存在"})
            except (OSError, ValueError, configparser.Error) as error:
                self._json(400, {"error": str(error)})

        def do_POST(self) -> None:
            if not self._authorized():
                self._json(403, {"error": "访问被拒绝"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length < 1 or length > _MAX_BODY:
                    raise ValueError("请求内容大小无效")
                data = json.loads(self.rfile.read(length))
                if self.path == "/api/save":
                    self._json(200, editor.save(data))
                elif self.path == "/api/runtime":
                    try:
                        self._json(200, editor.runtime_action(data))
                    except RuntimeError as error:
                        self._json(400, {"error": str(error)})
                elif self.path == "/api/settings":
                    if not isinstance(data, dict) or data:
                        raise ValueError("同步配置不接受额外参数")
                    try:
                        self._json(200, editor.sync_settings())
                    except RuntimeError as error:
                        self._json(400, {"error": str(error)})
                elif self.path == "/api/test":
                    self._json(200, editor.test_connection(data))
                else:
                    self._json(404, {"error": "接口不存在"})
            except (OSError, ValueError, configparser.Error, RuntimeError, SystemExit) as error:
                # 上游错误和密钥不经 HTTP 接口回传。
                message = str(error) if isinstance(error, (ValueError, SaveError)) else "保存或测试失败，请查看终端日志"
                self._json(400, {"error": message})

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    return server, token


def run(dcc_path: Path, settings_path: Path) -> None:
    editor = ConfigEditor(dcc_path, settings_path)
    server, token = create_server(editor)
    url = f"http://127.0.0.1:{server.server_port}/#token={token}"
    print(f"dcc 配置面板已启动：{url}\n按 Ctrl-C 关闭", flush=True)
    webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
