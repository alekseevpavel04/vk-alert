"""vk-hub: local relay between a VK community dialog and Claude Code sessions.

One process per machine. Holds the VK Bots Long Poll connection, routes the
owner's messages to bridged sessions, and sends the sessions' messages back
to the owner's dialog with the community. Stdlib only.

Run:   python hub/vk_hub.py            (serve)
       python hub/vk_hub.py notify "text" [--label name] [--alert]

Config comes from <project>/.env (see .env.example). Stub mode, which talks to
no VK at all, is switched on only by the process environment (VK_STUB=1), never
by .env: /stub/in then feeds owner messages and /stub/out shows sent ones.

Every HTTP request carries X-VK-Hub-Auth, an HMAC over the request made with the
key in data/hub.key, and every response carries X-VK-Hub-Sig, an HMAC over the
response. A process that cannot read data/hub.key can neither talk to the hub
nor pretend to be it.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import random
import re
import secrets
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from logging.handlers import RotatingFileHandler
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = Path(os.environ.get("VK_HUB_DATA") or ROOT / "data")
API_V = "5.199"
MAX_UNITS = 3900         # UTF-16 code units per VK message, under the 4096 limit
DEAD_AFTER = 90          # seconds without an inbox poll -> bridge considered gone
IDLE_EXIT = 300          # seconds with no bridges -> hub exits
LABEL_MAX = 16


# ---------------------------------------------------------------- config

def load_env() -> dict:
    env = {}
    f = ROOT / ".env"
    if f.exists():
        for line in f.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    for k in ("VK_TOKEN", "VK_GROUP_ID", "VK_OWNER_ID", "HUB_PORT", "VK_API"):
        if k in os.environ:
            env[k] = os.environ[k]
    return env


def to_int(value, default: int = 0) -> int:
    try:
        return int(str(value).strip().lstrip("-") or default)
    except ValueError:
        return default


CFG = load_env()
STUB = os.environ.get("VK_STUB") == "1"
PORT = to_int(CFG.get("HUB_PORT"), 8765)
TOKEN = CFG.get("VK_TOKEN", "")
GROUP_ID = to_int(CFG.get("VK_GROUP_ID"))
OWNER_ID = to_int(CFG.get("VK_OWNER_ID"))
API_BASE = CFG.get("VK_API", "https://api.vk.ru/method/")


def private_dir(path: Path):
    """data/ holds the key, the log and the state: only this user may read it."""
    path.mkdir(parents=True, exist_ok=True)
    try:
        if os.name == "nt":
            user = os.environ.get("USERNAME", "")
            if user:
                subprocess.run(["icacls", str(path), "/inheritance:r", "/grant:r",
                                f"{user}:(OI)(CI)F", "*S-1-5-18:(OI)(CI)F"],
                               capture_output=True, timeout=15)
        else:
            os.chmod(path, 0o700)
    except Exception:
        pass


private_dir(DATA)
KEY_FILE = DATA / "hub.key"
if not KEY_FILE.exists():
    KEY_FILE.write_text(secrets.token_hex(32), encoding="ascii")
KEY = bytes.fromhex(KEY_FILE.read_text(encoding="ascii").strip())
STATE_FILE = DATA / "state.json"

logging.basicConfig(
    handlers=[RotatingFileHandler(DATA / "hub.log", maxBytes=1_000_000, backupCount=2, encoding="utf-8")],
    level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("hub")


def sign(*parts: str) -> str:
    return hmac.new(KEY, "\n".join(parts).encode(), hashlib.sha256).hexdigest()


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------- text

SECRET_PATTERNS = [
    re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY[A-Z ]*-----.*?(?:-----END [A-Z0-9 ]*PRIVATE KEY[A-Z ]*-----|\Z)", re.S),
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{10,}"),
    re.compile(r"\bsk-(?:proj-|svcacct-|admin-)?[A-Za-z0-9_\-]{20,}"),
    re.compile(r"\b[rs]k_(?:live|test)_[A-Za-z0-9]{16,}"),
    re.compile(r"\bAIza[0-9A-Za-z_\-]{35}"),
    re.compile(r"\bya29\.[0-9A-Za-z_\-]{20,}"),
    re.compile(r"\bhf_[A-Za-z0-9]{30,}"),
    re.compile(r"\bglpat-[A-Za-z0-9_\-]{20,}"),
    re.compile(r"\bnpm_[A-Za-z0-9]{36}"),
    re.compile(r"\b\d{6,12}:[A-Za-z0-9_\-]{35}\b"),
    re.compile(r"vk1\.a\.[A-Za-z0-9_\-]{20,}"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{10,}"),
    re.compile(r"hooks\.slack\.com/services/[A-Za-z0-9/]+"),
    re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}"),
    re.compile(r"(?<![:\w])[0-9a-fA-F]{64,}\b"),
]
URL_CREDS = re.compile(r"(?i)(\b[a-z][a-z0-9+.\-]*://[^\s:/@]*:)([^\s@/]+)(@)")
KV_SECRET = re.compile(
    r"(?i)(\b[A-Z0-9_\-]*(?:token|secret|passw(?:or)?d|passphrase|_pass|api[_-]?key|private[_-]?key|access[_-]?key"
    r"|authorization|credentials?|пароль)(?:_?key)?[\"']?)(\s*[=:]\s*)(['\"]?)"
    r"(?!(?:\d+|true|false|null|none)\b)((?:Bearer|Basic|token)\s+)?([^\s'\",}]{8,})")
BLOB = re.compile(r"(?<![\w/+=\-])[A-Za-z0-9+/_\-]{32,}={0,2}(?![\w/+=\-])")


def _blob(m):
    s = m.group(0)
    if re.fullmatch(r"[0-9a-fA-F]+", s) or not (re.search(r"[A-Z]", s) and re.search(r"[a-z]", s) and re.search(r"\d", s)):
        return s
    return s if s.count("_") + s.count("-") > 3 or s.count("/") > 3 else "[REDACTED]"


def redact(text: str) -> str:
    if TOKEN:
        text = text.replace(TOKEN, "[REDACTED]")
    for p in SECRET_PATTERNS:
        text = p.sub("[REDACTED]", text)
    text = URL_CREDS.sub(r"\1[REDACTED]\3", text)
    text = KV_SECRET.sub(lambda m: f"{m.group(1)}{m.group(2)}{m.group(3)}{m.group(4) or ''}[REDACTED]", text)
    return BLOB.sub(_blob, text)


def plain(text: str) -> str:
    """Markdown from the agent -> text that reads well in VK, which shows markup as is."""
    out = []
    for line in text.replace("\r\n", "\n").split("\n"):
        s = line.rstrip()
        if re.match(r"^\s*```", s):
            continue
        if re.match(r"^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?\s*$", s):
            continue                                            # table separator
        if re.match(r"^\s*([-*_])\s*(\1\s*){2,}$", s):
            continue                                            # horizontal rule
        if s.strip().startswith("|") and s.strip().endswith("|") and s.count("|") >= 2:
            cells = [c.strip() for c in s.strip().strip("|").split("|")]
            s = " · ".join(c for c in cells if c)
        s = re.sub(r"^(\s*)#{1,6}\s+", r"\1", s)                 # headings
        s = re.sub(r"^(\s*)[-*+]\s+(?=\S)", r"\1• ", s)           # bullets
        s = re.sub(r"^(\s*)>\s?", r"\1", s)                       # quotes
        s = re.sub(r"!?\[([^\]]+)\]\((\S+?)\)", r"\1 (\2)", s)    # links
        s = re.sub(r"(\*\*|__)(.+?)\1", r"\2", s)                 # bold
        s = re.sub(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])", r"\1", s)  # italic
        s = re.sub(r"~~(.+?)~~", r"\1", s)
        s = s.replace("`", "")
        out.append(s)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()


def units(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def chunks(text: str, limit: int = MAX_UNITS) -> list[str]:
    """Split on paragraph, line or word boundaries, counting UTF-16 units as VK does."""
    out = []
    while units(text) > limit:
        lo, hi = 0, len(text)
        while lo < hi:                                          # longest prefix that fits
            mid = (lo + hi + 1) // 2
            if units(text[:mid]) <= limit:
                lo = mid
            else:
                hi = mid - 1
        cut = lo
        for sep in ("\n\n", "\n", " "):
            pos = text.rfind(sep, 0, lo)
            if pos > lo // 2:
                cut = pos
                break
        if 0 < cut < len(text) and "\udc00" <= text[cut] <= "\udfff":
            cut -= 1                                            # never split a surrogate pair
        out.append(text[:cut].rstrip())
        text = text[cut:].lstrip()
    if text:
        out.append(text)
    return out or [""]


def norm_label(label: str) -> str:
    label = re.sub(r"\s+", "-", (label or "").strip())
    label = re.sub(r"-{2,}", "-", re.sub(r"[^\w\-.]", "", label))[:LABEL_MAX].strip("-.")
    return label or "сессия"


def norm_cmd(text: str) -> str:
    return re.sub(r"[\s.!?,…;:)]+$", "", text.strip().lower())


def is_stop(text: str) -> bool:
    return norm_cmd(text) in ("стоп", "stop", "/stop", "/стоп")


# ---------------------------------------------------------------- state

_seq_lock = threading.Lock()
_seq = int(time.time() * 1000)   # grows across hub restarts, so old acks never hide new items


def next_seq() -> int:
    global _seq
    with _seq_lock:
        _seq += 1
        return _seq


class Bridge:
    def __init__(self, sid: str, label: str, cwd: str, rules: str):
        self.sid, self.label, self.cwd, self.rules = sid, label, cwd, rules
        self.queue: list[dict] = []
        self.cond = threading.Condition()
        self.last_seen = time.time()
        self.busy = False
        self.waiting = 0

    def push(self, item: dict):
        with self.cond:
            item["id"] = next_seq()
            self.queue.append(item)
            self.cond.notify_all()

    def take(self, wait: float, ack: int) -> list[dict]:
        """Unacknowledged items with id > ack. An item stays queued until a later
        poll acknowledges it, so a mod that dies mid-poll loses nothing."""
        with self.cond:
            self.waiting += 1
            try:
                self.queue = [i for i in self.queue if i["id"] > ack]
                if not self.queue and wait > 0:
                    self.cond.wait(timeout=wait)
                return list(self.queue)
            finally:
                self.waiting -= 1
                self.last_seen = time.time()


class Hub:
    def __init__(self):
        self.lock = threading.RLock()
        self.send_lock = threading.Lock()
        self.bridges: dict[str, Bridge] = {}
        self.ghosts: dict[str, Bridge] = {}        # reaped bridges, kept to restore their queue
        self.cmid_to_sid: dict[int, str] = {}      # conversation_message_id of our message -> sid
        self.pending: dict[str, str] = {}          # route key -> text awaiting "кому?"
        self.stub_out: list[dict] = []
        self.no_bridges_since = time.time()
        self.started = time.time()

    # ---- bridges
    def unique_label(self, label: str, sid: str) -> str:
        taken = {b.label.lower() for b in self.bridges.values() if b.sid != sid}
        base, n = norm_label(label), 2
        label = base
        while label.lower() in taken:
            suffix = f"-{n}"
            label, n = base[:LABEL_MAX - len(suffix)] + suffix, n + 1
        return label

    def bridge_on(self, sid: str, label: str, cwd: str, rules: str, quiet: bool) -> Bridge:
        back = False
        with self.lock:
            b = self.bridges.get(sid)
            label = self.unique_label(label, sid)
            if b:
                b.label, b.cwd, b.rules = label, cwd or b.cwd, rules
            else:
                b = Bridge(sid, label, cwd, rules)
                ghost = self.ghosts.pop(sid, None)
                if ghost:
                    b.queue, back = ghost.queue, True
                self.bridges[sid] = b
            b.last_seen = time.time()
        if back:
            self.send(sid, "снова на связи", kind="service")
        elif not quiet:
            self.send(sid, "мост включён", kind="service")
        log.info("bridge on %s %s", sid, label)
        return b

    def bridge_off(self, sid: str, note: str | None):
        with self.lock:
            b = self.bridges.pop(sid, None)
            if not self.bridges:
                self.no_bridges_since = time.time()
        if b and note:
            self.send(None, note, kind="service", label=b.label)
        log.info("bridge off %s (%s)", sid, note)
        return b

    def reap(self):
        now = time.time()
        with self.lock:
            dead = [b for b in self.bridges.values() if not b.waiting and now - b.last_seen > DEAD_AFTER]
        for b in dead:
            self.ghosts[b.sid] = b
            self.bridge_off(b.sid, "сессия не отвечает, мост выключен")
        with self.lock:
            for sid in [s for s, g in self.ghosts.items() if now - g.last_seen > 3600]:
                del self.ghosts[sid]

    def find_label(self, name: str) -> Bridge | None:
        name = norm_label(name).lower()
        with self.lock:
            bs = list(self.bridges.values())
        exact = [b for b in bs if b.label.lower() == name]
        if exact:
            return exact[0]
        pref = [b for b in bs if b.label.lower().startswith(name)]
        return pref[0] if len(pref) == 1 else None

    # ---- outgoing
    def send(self, sid: str | None, text: str, kind: str = "reply", label: str | None = None,
             keyboard: dict | None = None) -> bool:
        if sid and not label:
            with self.lock:
                b = self.bridges.get(sid)
            label = b.label if b else None
        text = text.encode("utf-8", "replace").decode("utf-8")
        if kind == "service":
            body, head = (f"{label} · {text}" if label else text), None
        else:
            body = plain(text)
            head = (f"‼️ ПРОБЛЕМА · {label}" if kind == "alert" else label) if label else \
                   ("‼️ ПРОБЛЕМА" if kind == "alert" else None)
        body = redact(body)
        parts = chunks(body, MAX_UNITS - 60)
        n = len(parts)
        ok = True
        with self.send_lock:
            for i, part in enumerate(parts):
                h = head + (f" ({i + 1}/{n})" if n > 1 else "") if head else (f"({i + 1}/{n})" if n > 1 else None)
                msg = f"{h}\n{part}" if h else part
                cmids = vk_send(msg, keyboard if i == n - 1 else None)
                if cmids is None:
                    ok = False
                    continue
                if sid:
                    with self.lock:
                        for c in cmids:
                            self.cmid_to_sid[c] = sid
                        if len(self.cmid_to_sid) > 5000:
                            for k in list(self.cmid_to_sid)[:1000]:
                                del self.cmid_to_sid[k]
        return ok

    def say(self, text: str, keyboard: dict | None = None) -> bool:
        return self.send(None, text, kind="service", keyboard=keyboard)

    # ---- incoming from the owner
    def on_owner_message(self, msg: dict):
        text = (msg.get("text") or "").strip()
        payload = parse_payload(msg.get("payload"))

        if payload.get("route"):                    # a "кому?" button press
            return self.on_route_button(payload)

        extras = [a.get("type", "?") for a in msg.get("attachments") or []]
        if msg.get("fwd_messages"):
            extras.append("пересланные сообщения")
        if msg.get("geo"):
            extras.append("геометка")
        if not text:
            if extras:
                self.say("Агентам доходит только текст. Стикеры, голосовые, файлы и пересланные сообщения не передаются.")
            return

        low = norm_cmd(text)
        if low in ("/list", "/список", "список"):
            return self.cmd_list()
        if low in ("/help", "/помощь", "помощь"):
            return self.cmd_help()
        m = re.match(r"^/off(?:\s+(.*))?$", text.strip(), re.I | re.S)
        if m:
            return self.cmd_off((m.group(1) or "").strip())

        target = self.route(msg, text)
        if isinstance(target, tuple) and isinstance(target[0], Bridge):
            self.deliver(*target)
            if extras:
                self.say("Текст передан, вложения и пересланные сообщения агенту не уходят.")
            return
        if target == "gone":
            return self.say("Сессия, которой ты отвечаешь, уже отключилась. Не доставлено.")
        if isinstance(target, tuple) and target[0] == "nolabel":
            return self.say(f"Нет моста «{target[1]}». /list покажет метки.")
        if isinstance(target, tuple) and target[0] == "empty":
            return self.say(f"После #{target[1]} нужен текст.")
        if target == "none":
            return self.say("Нет включённых мостов. Включи мост в нужной сессии Claude командой /vk on.")
        if is_stop(text):                           # "стоп" without an address: stop every busy agent
            with self.lock:
                busy = [b for b in self.bridges.values() if b.busy]
            if not busy:
                return self.say("Сейчас никто не занят.")
            for b in busy:
                self.deliver(b, text)
            return
        self.ask(text)

    def on_route_button(self, payload: dict):
        key, target = str(payload.get("route")), payload.get("sid")
        with self.lock:
            pending = self.pending.get(key)
            b = self.bridges.get(target)
        if pending is None:
            return self.say("Это сообщение уже доставлено или устарело.")
        del self.pending[key]
        if not b:
            return self.ask(pending, note="Эта сессия уже отключилась.")
        self.deliver(b, pending)

    def ask(self, text: str, note: str | None = None):
        with self.lock:
            bs = list(self.bridges.values())
        if not bs:
            return self.say("Нет включённых мостов, сообщение не доставлено.")
        key = secrets.token_hex(4)
        with self.lock:
            self.pending[key] = text
            while len(self.pending) > 50:
                self.pending.pop(next(iter(self.pending)))
        rows = [[{"action": {"type": "text", "label": b.label[:40],
                             "payload": json.dumps({"route": key, "sid": b.sid})},
                  "color": "secondary"}] for b in bs[:10]]
        lead = (note + " ") if note else ""
        self.say(lead + "Кому отправить? Нажми кнопку. Можно ответить на сообщение агента или начать с #метка.",
                 keyboard={"one_time": True, "inline": False, "buttons": rows})

    def route(self, msg: dict, text: str):
        # 1) a reply to an agent's message
        ref = msg.get("reply_message") or {}
        if ref and ref.get("from_id") == -GROUP_ID:
            sid = self.cmid_to_sid.get(ref.get("conversation_message_id") or -1)
            if sid:
                with self.lock:
                    b = self.bridges.get(sid)
                return (b, text) if b else "gone"
        # 2) #label prefix
        m = re.match(r"^#(\S+)\s*(.*)$", text, re.S)
        if m:
            b = self.find_label(m.group(1))
            if not b:
                return ("nolabel", m.group(1))
            if not m.group(2).strip():
                return ("empty", m.group(1))
            return (b, m.group(2).strip())
        # 3) only one bridge
        with self.lock:
            bs = list(self.bridges.values())
        if len(bs) == 1:
            return (bs[0], text)
        return "none" if not bs else "ask"

    def deliver(self, b: Bridge, text: str):
        if not text:
            return
        kind = "stop" if is_stop(text) else "msg"
        b.push({"kind": kind, "text": text, "at": time.time()})
        log.info("deliver %s -> %s (%d симв.)", kind, b.label, len(text))

    def cmd_list(self):
        with self.lock:
            bs = list(self.bridges.values())
        if not bs:
            return self.say("Включённых мостов нет.")
        lines = [f"{b.label} · {'занят' if b.busy else 'свободен'}" + (f" · {Path(b.cwd).name}" if b.cwd else "")
                 for b in bs]
        self.say("Мосты\n" + "\n".join(lines))

    def cmd_off(self, arg: str):
        with self.lock:
            bs = list(self.bridges.values())
        if arg.lower() in ("all", "все", "всё"):
            targets = bs
        elif arg:
            targets = [self.find_label(arg)]
        else:
            targets = bs if len(bs) == 1 else []
        targets = [t for t in targets if t]
        if not targets:
            return self.say("Кого выключить? /off метка или /off all")
        for b in targets:
            b.push({"kind": "off", "text": "", "at": time.time()})

    def cmd_help(self):
        self.say(
            "Как писать агентам\n"
            "• включён один мост, просто пиши\n"
            "• несколько, отвечай на сообщение агента или начни с #метка\n"
            "• стоп, прервать работу агента\n"
            "• /list, какие мосты включены\n"
            "• /off метка или /off all, выключить мост")


HUB = Hub()


def parse_payload(p) -> dict:
    if not p:
        return {}
    if isinstance(p, dict):
        return p
    try:
        v = json.loads(p)
        return v if isinstance(v, dict) else {}
    except Exception:
        return {}


# ---------------------------------------------------------------- VK

def vk_call(method: str, params: dict, timeout: float = 30):
    params = dict(params, v=API_V)
    data = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request(API_BASE + method, data=data,
                                 headers={"Authorization": f"Bearer {TOKEN}"})
    last = None
    for attempt in range(5):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                res = json.loads(r.read().decode())
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            last = e                       # same random_id on retry, so no duplicates
            time.sleep(1 + attempt)
            continue
        err = res.get("error")
        if not err:
            return res.get("response")
        if err.get("error_code") in (6, 9, 10) and attempt < 4:
            time.sleep(1 + attempt)
            continue
        raise RuntimeError(f"VK {method} error {err.get('error_code')}: {err.get('error_msg')}")
    raise RuntimeError(f"VK {method}: {last or 'retries exhausted'}")


def vk_send(text: str, keyboard: dict | None = None) -> list[int] | None:
    """Sends one message to the owner. Returns its conversation_message_ids, None on failure."""
    if STUB:
        mid = 10_000 + len(HUB.stub_out)
        HUB.stub_out.append({"id": mid, "text": text, "keyboard": keyboard, "at": time.time()})
        log.info("stub send #%s (%d симв.)", mid, len(text))
        return [mid]
    params = {"peer_ids": OWNER_ID, "random_id": random.randint(1, 2**31 - 1),
              "message": text, "dont_parse_links": 1}
    if keyboard:
        params["keyboard"] = json.dumps(keyboard, ensure_ascii=False)
    try:
        res = vk_call("messages.send", params)
    except Exception as e:
        log.error("send failed: %s", e)
        return None
    ids = []
    for item in res if isinstance(res, list) else []:
        if not isinstance(item, dict):
            continue
        if item.get("error"):
            log.error("send to %s failed: %s", item.get("peer_id"), item["error"])
            return None
        if item.get("conversation_message_id"):
            ids.append(item["conversation_message_id"])
    return ids


def vk_mark_read(peer_id: int):
    if STUB:
        return
    try:
        vk_call("messages.markAsRead", {"peer_id": peer_id, "group_id": GROUP_ID})
    except Exception as e:
        log.warning("markAsRead: %s", e)


def load_ts():
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8")).get("ts")
    except Exception:
        return None


def save_ts(ts):
    try:
        STATE_FILE.write_text(json.dumps({"ts": ts}), encoding="utf-8")
    except Exception:
        pass


def longpoll_loop():
    server = key = None
    ts = load_ts()          # resume where the last hub stopped, so nothing sent meanwhile is lost
    fails = 0
    while True:
        try:
            if not server:
                lp = vk_call("groups.getLongPollServer", {"group_id": GROUP_ID})
                server, key = lp["server"], lp["key"]
                if ts is None:
                    ts = lp["ts"]
                    save_ts(ts)
                log.info("long poll connected")
            url = f"{server}?act=a_check&key={key}&ts={ts}&wait=25"
            with urllib.request.urlopen(url, timeout=40) as r:
                res = json.loads(r.read().decode())
            if "failed" in res:
                if res["failed"] == 1:
                    ts = res.get("ts", ts)
                    save_ts(ts)
                else:
                    server = None
                    if res["failed"] == 3:
                        ts = None
                    fails += 1
                    time.sleep(min(30, fails))
                continue
            fails = 0
            for upd in res.get("updates", []):
                try:
                    handle_update(upd)
                except Exception:
                    log.exception("bad update")
            ts = res.get("ts", ts)
            save_ts(ts)
        except Exception as e:
            log.error("long poll: %s", e)
            server = None                    # keep ts: events of the outage come next time
            fails += 1
            time.sleep(min(30, 5 * fails))


def handle_update(upd: dict):
    if upd.get("type") != "message_new":
        return
    msg = (upd.get("object") or {}).get("message")
    if not isinstance(msg, dict):
        log.warning("message_new without object.message: check the Long Poll API version (5.199)")
        return
    if msg.get("from_id") != OWNER_ID or msg.get("peer_id") != OWNER_ID:
        log.warning("dropped message from %s in %s", msg.get("from_id"), msg.get("peer_id"))
        return
    vk_mark_read(OWNER_ID)
    try:
        HUB.on_owner_message(msg)
    except Exception:
        log.exception("handling message")


# ---------------------------------------------------------------- HTTP API

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    auth = ""

    def log_message(self, fmt, *args):
        pass

    def _json(self, code: int, obj):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-VK-Hub-Sig", sign("resp", self.auth, sha(body)))
        self.end_headers()
        self.wfile.write(body)

    def _raw(self) -> bytes:
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        return self.rfile.read(n) if 0 < n <= 1_000_000 else b""

    def _guard(self, body: bytes = b"") -> bool:
        host = (self.headers.get("Host") or "").lower()
        self.auth = self.headers.get("X-VK-Hub-Auth") or ""
        ts, _, mac = self.auth.partition(".")
        ok = (host in (f"127.0.0.1:{PORT}", f"localhost:{PORT}")
              and not self.headers.get("Origin")
              and ts.isdigit() and abs(time.time() - int(ts)) < 60
              and hmac.compare_digest(mac, sign("req", self.command, self.path, ts, sha(body))))
        if not ok:
            self.auth = ""
            self._json(403, {"error": "forbidden"})
        return ok

    def do_GET(self):
        if not self._guard():
            return
        u = urllib.parse.urlparse(self.path)
        q = dict(urllib.parse.parse_qsl(u.query))
        if u.path == "/health":
            with HUB.lock:
                bs = [{"sid": b.sid, "label": b.label, "busy": b.busy} for b in HUB.bridges.values()]
            return self._json(200, {"ok": True, "stub": STUB, "bridges": bs, "pid": os.getpid()})
        if u.path == "/inbox":
            try:
                wait = max(0.0, min(float(q.get("wait", "20")), 25.0))
                ack = int(q.get("ack", "0") or 0)
            except ValueError:
                return self._json(400, {"error": "bad wait/ack"})
            with HUB.lock:
                b = HUB.bridges.get(q.get("sid", ""))
            if not b:
                return self._json(200, {"known": False, "messages": []})
            b.busy = q.get("busy") == "1"
            return self._json(200, {"known": True, "messages": b.take(wait, ack)})
        if u.path == "/stub/out" and STUB:
            since = int(q.get("since", "0") or 0)
            return self._json(200, {"messages": [m for m in HUB.stub_out if m["id"] >= since]})
        self._json(404, {"error": "not found"})

    def do_POST(self):
        raw = self._raw()
        if not self._guard(raw):
            return
        p = urllib.parse.urlparse(self.path).path
        try:
            d = json.loads(raw or b"{}")
        except ValueError:
            d = None
        if not isinstance(d, dict):
            return self._json(400, {"error": "json object expected"})
        sid = str(d.get("sid") or "")
        if p == "/bridge/on":
            if not sid:
                return self._json(400, {"error": "sid required"})
            b = HUB.bridge_on(sid, str(d.get("label") or ""), str(d.get("cwd") or ""),
                              str(d.get("rules") or ""), bool(d.get("quiet")))
            return self._json(200, {"ok": True, "label": b.label})
        if p == "/bridge/off":
            HUB.bridge_off(sid, d.get("note") and str(d["note"]))
            return self._json(200, {"ok": True})
        if p == "/send":
            text = str(d.get("text") or "").strip()
            kind = str(d.get("kind") or "reply")
            if not text or kind not in ("reply", "service", "alert"):
                return self._json(400, {"error": "text and kind reply|service|alert required"})
            with HUB.lock:
                known = sid in HUB.bridges
            if sid and not known:
                return self._json(200, {"ok": False, "known": False})
            label = norm_label(str(d["label"])) if d.get("label") and not sid else None
            ok = HUB.send(sid or None, text, kind=kind, label=label)
            return self._json(200 if ok else 502, {"ok": ok})
        if p == "/stub/in" and STUB:
            msg = {"from_id": OWNER_ID, "peer_id": OWNER_ID, "text": str(d.get("text") or ""),
                   "payload": d.get("payload"), "attachments": d.get("attachments") or []}
            if d.get("reply_to"):
                msg["reply_message"] = {"conversation_message_id": int(d["reply_to"]), "from_id": -GROUP_ID}
            HUB.on_owner_message(msg)
            return self._json(200, {"ok": True})
        self._json(404, {"error": "not found"})


class Server(ThreadingHTTPServer):
    """Exclusive bind: on Windows SO_REUSEADDR would let a second hub share the port."""
    daemon_threads = True
    allow_reuse_address = os.name != "nt"

    def server_bind(self):
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def janitor(server: Server):
    while True:
        time.sleep(10)
        HUB.reap()
        with HUB.lock:
            empty = not HUB.bridges
        if empty and time.time() - HUB.no_bridges_since > IDLE_EXIT \
                and time.time() - HUB.started > IDLE_EXIT:
            log.info("no bridges for %ss, exiting", IDLE_EXIT)
            server.shutdown()
            return


def serve():
    if not STUB and (not TOKEN or not GROUP_ID or not OWNER_ID):
        log.error("VK_TOKEN, VK_GROUP_ID and VK_OWNER_ID must be set in .env")
        print("vk-hub: .env is incomplete, see .env.example", file=sys.stderr)
        sys.exit(2)
    try:
        server = Server(("127.0.0.1", PORT), Handler)
    except OSError:
        log.info("port %s busy, another hub is running", PORT)
        sys.exit(0)
    (DATA / "hub.port").write_text(str(PORT), encoding="ascii")
    log.info("hub started pid=%s port=%s stub=%s", os.getpid(), PORT, STUB)
    if not STUB:
        threading.Thread(target=longpoll_loop, daemon=True).start()
    threading.Thread(target=janitor, args=(server,), daemon=True).start()
    server.serve_forever()
    log.info("hub stopped")


def signed_request(path: str, body: dict | None = None, timeout: float = 30):
    """Client side of the protocol, for notify and the tests: returns (status, json)."""
    raw = json.dumps(body).encode() if body is not None else b""
    method = "POST" if body is not None else "GET"
    ts = str(int(time.time()))
    auth = ts + "." + sign("req", method, path, ts, sha(raw))
    req = urllib.request.Request(f"http://127.0.0.1:{PORT}{path}", data=raw if body is not None else None,
                                 method=method, headers={"X-VK-Hub-Auth": auth, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            status, data, sig = r.status, r.read(), r.headers.get("X-VK-Hub-Sig", "")
    except urllib.error.HTTPError as e:
        status, data, sig = e.code, e.read(), e.headers.get("X-VK-Hub-Sig", "")
    if not hmac.compare_digest(sig, sign("resp", auth, sha(data))) and status != 403:
        raise RuntimeError("hub signature mismatch")
    return status, json.loads(data.decode() or "{}")


def notify_cli(argv: list[str]):
    """python hub/vk_hub.py notify "text" [--label name] [--alert]: one message to the owner."""
    usage = 'usage: vk_hub.py notify "text" [--label name] [--alert]'
    label, kind = None, "reply"
    if "--alert" in argv:
        argv = [a for a in argv if a != "--alert"]
        kind = "alert"
    if "--label" in argv:
        i = argv.index("--label")
        if i + 1 >= len(argv):
            print(usage, file=sys.stderr)
            sys.exit(2)
        label = argv[i + 1]
        argv = argv[:i] + argv[i + 2:]
    text = " ".join(argv).strip()
    if not text:
        print(usage, file=sys.stderr)
        sys.exit(2)
    try:                                      # through a running hub if there is one
        status, res = signed_request("/send", {"text": text, "label": label, "kind": kind}, timeout=180)
        sys.exit(0 if res.get("ok") else 1)
    except urllib.error.URLError as e:
        if not isinstance(e.reason, ConnectionRefusedError):
            print(f"vk-hub: {e.reason}", file=sys.stderr)
            sys.exit(1)
    except (TimeoutError, ConnectionError, RuntimeError) as e:
        print(f"vk-hub: {e}, сообщение могло уже уйти", file=sys.stderr)
        sys.exit(1)
    if STUB:
        print("hub is not running (stub mode)", file=sys.stderr)
        sys.exit(1)
    if not TOKEN or not OWNER_ID:
        print("vk-hub: .env is incomplete", file=sys.stderr)
        sys.exit(2)
    ok = HUB.send(None, text, kind=kind, label=norm_label(label) if label else None)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "notify":
        notify_cli(sys.argv[2:])
    else:
        serve()
