"""vk-hub: local relay between a VK community dialog and Claude Code sessions.

One process per machine. Holds the VK Bots Long Poll connection, routes the
owner's messages to bridged sessions, and sends the sessions' messages back
to the owner's dialog with the community. Stdlib only.

Run:   python hub/vk_hub.py            (serve)
       python hub/vk_hub.py notify "text" [--label name]   (one-off message)

Config comes from <project>/.env (see .env.example). With VK_STUB=1 the hub
talks to no VK at all: /stub/in feeds owner messages, /stub/out shows sent ones.
"""
from __future__ import annotations

import json
import logging
import os
import random
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
API_V = "5.199"
MAX_CHUNK = 4000
DEAD_AFTER = 90          # seconds without an inbox poll -> bridge considered gone
IDLE_EXIT = 300          # seconds with no bridges -> hub exits
HEADER = "X-VK-Hub"      # required on every request: blocks browser CSRF to localhost


# ---------------------------------------------------------------- config

def load_env() -> dict:
    env = {}
    f = ROOT / ".env"
    if f.exists():
        for line in f.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    for k in ("VK_TOKEN", "VK_GROUP_ID", "VK_OWNER_ID", "VK_STUB", "HUB_PORT", "VK_API"):
        if os.environ.get(k):
            env[k] = os.environ[k]
    return env


CFG = load_env()
PORT = int(CFG.get("HUB_PORT", "8765"))
STUB = CFG.get("VK_STUB", "") == "1"
TOKEN = CFG.get("VK_TOKEN", "")
GROUP_ID = CFG.get("VK_GROUP_ID", "").lstrip("-")
OWNER_ID = int(CFG.get("VK_OWNER_ID", "0") or 0)
API_BASE = CFG.get("VK_API", "https://api.vk.ru/method/")

DATA.mkdir(exist_ok=True)
logging.basicConfig(
    filename=DATA / "hub.log", level=logging.INFO, encoding="utf-8",
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("hub")


# ---------------------------------------------------------------- secrets filter

SECRET_PATTERNS = [
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{10,}"),
    re.compile(r"vk1\.a\.[A-Za-z0-9_\-]{20,}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"xox[abpr]-[A-Za-z0-9\-]{10,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S),
    re.compile(r"eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}"),
]
KV_SECRET = re.compile(
    r"(?i)\b([A-Z0-9_]*(?:token|secret|password|passwd|api[_-]?key)[A-Z0-9_]*)(\s*[=:]\s*)(['\"]?)([^\s'\"]{6,})"
)


def redact(text: str) -> str:
    if TOKEN:
        text = text.replace(TOKEN, "[REDACTED]")
    for p in SECRET_PATTERNS:
        text = p.sub("[REDACTED]", text)
    return KV_SECRET.sub(lambda m: f"{m.group(1)}{m.group(2)}{m.group(3)}[REDACTED]", text)


def chunks(text: str, size: int = MAX_CHUNK) -> list[str]:
    out = []
    while len(text) > size:
        cut = text.rfind("\n", 0, size)
        if cut < size // 2:
            cut = size
        out.append(text[:cut])
        text = text[cut:].lstrip("\n")
    if text:
        out.append(text)
    return out or [""]


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

    def push(self, item: dict):
        with self.cond:
            item["id"] = next_seq()
            self.queue.append(item)
            self.cond.notify_all()

    def take(self, wait: float, ack: int) -> list[dict]:
        """Unacknowledged items with id > ack. An item stays queued until a later
        poll acknowledges it, so a mod that dies mid-poll loses nothing."""
        with self.cond:
            self.queue = [i for i in self.queue if i["id"] > ack]
            if not self.queue and wait > 0:
                self.cond.wait(timeout=wait)
            self.last_seen = time.time()
            return list(self.queue)


class Hub:
    def __init__(self):
        self.lock = threading.RLock()
        self.bridges: dict[str, Bridge] = {}
        self.msg_to_sid: dict[int, str] = {}      # VK message id / cmid -> sid
        self.pending: dict[str, str] = {}          # route key -> text awaiting "кому?"
        self.stub_out: list[dict] = []
        self.no_bridges_since = time.time()
        self.started = time.time()

    # ---- bridges
    def unique_label(self, label: str, sid: str) -> str:
        taken = {b.label.lower() for b in self.bridges.values() if b.sid != sid}
        base, n = label, 2
        while label.lower() in taken:
            label, n = f"{base} {n}", n + 1
        return label

    def bridge_on(self, sid: str, label: str, cwd: str, rules: str, quiet: bool) -> Bridge:
        with self.lock:
            b = self.bridges.get(sid)
            label = self.unique_label(label or "сессия", sid)
            if b:
                b.label, b.cwd, b.rules = label, cwd or b.cwd, rules
            else:
                b = Bridge(sid, label, cwd, rules)
                self.bridges[sid] = b
            b.last_seen = time.time()
        if not quiet:
            self.send(sid, "🔌 мост включён", prefix=True)
        log.info("bridge on %s %s", sid, label)
        return b

    def bridge_off(self, sid: str, note: str | None):
        with self.lock:
            b = self.bridges.pop(sid, None)
            if not self.bridges:
                self.no_bridges_since = time.time()
        if b and note:
            self.send_raw(f"[{b.label}] {note}", sid=None)
        log.info("bridge off %s (%s)", sid, note)

    def reap(self):
        now = time.time()
        with self.lock:
            dead = [b for b in self.bridges.values() if now - b.last_seen > DEAD_AFTER]
        for b in dead:
            self.bridge_off(b.sid, "⚠️ сессия пропала, мост выключен")

    def find_label(self, name: str) -> Bridge | None:
        name = name.strip().lower()
        with self.lock:
            exact = [b for b in self.bridges.values() if b.label.lower() == name]
            if exact:
                return exact[0]
            pref = [b for b in self.bridges.values() if b.label.lower().startswith(name)]
            return pref[0] if len(pref) == 1 else None

    # ---- outgoing
    def send(self, sid: str | None, text: str, prefix: bool = True, keyboard: dict | None = None):
        label = None
        if sid:
            with self.lock:
                b = self.bridges.get(sid)
            label = b.label if b else None
        body = f"[{label}] {text}" if (prefix and label) else text
        self.send_raw(body, sid=sid, keyboard=keyboard)

    def send_raw(self, text: str, sid: str | None, keyboard: dict | None = None):
        text = redact(text)
        parts = chunks(text)
        for i, part in enumerate(parts):
            kb = keyboard if i == len(parts) - 1 else None
            ids = vk_send(part, kb)
            if sid:
                with self.lock:
                    for mid in ids:
                        self.msg_to_sid[mid] = sid
                    if len(self.msg_to_sid) > 5000:
                        for k in list(self.msg_to_sid)[:1000]:
                            del self.msg_to_sid[k]

    # ---- incoming from the owner
    def on_owner_message(self, msg: dict):
        text = (msg.get("text") or "").strip()
        payload = parse_payload(msg.get("payload"))

        # a "кому?" button press
        if payload.get("route"):
            key, target = payload["route"], payload.get("sid")
            pending = self.pending.pop(key, None)
            with self.lock:
                b = self.bridges.get(target)
            if pending is None:
                return self.send_raw("Это сообщение уже доставлено или устарело.", sid=None)
            if not b:
                return self.send_raw("Эта сессия уже отключилась.", sid=None)
            return self.deliver(b, pending)

        low = text.lower()
        if low in ("/list", "/список", "список"):
            return self.cmd_list()
        if low in ("/help", "/помощь", "помощь"):
            return self.cmd_help()
        if low.startswith("/off"):
            arg = text[4:].strip()
            return self.cmd_off(arg)

        target = self.route(msg, text)
        if isinstance(target, tuple):  # (bridge, text without #label)
            return self.deliver(*target)
        if target == "none":
            return self.send_raw("Нет включённых мостов. Включи мост в нужной сессии Claude.", sid=None)
        # ambiguous: ask with buttons
        key = f"{time.time():.6f}"
        self.pending[key] = text
        if len(self.pending) > 50:
            self.pending.pop(next(iter(self.pending)))
        with self.lock:
            bs = list(self.bridges.values())
        rows = [[{
            "action": {"type": "text", "label": b.label[:40],
                       "payload": json.dumps({"route": key, "sid": b.sid})},
            "color": "primary"}] for b in bs[:10]]
        self.send_raw("Кому отправить? Нажми кнопку или в следующий раз ответь на сообщение агента.",
                      sid=None, keyboard={"one_time": True, "inline": False, "buttons": rows})

    def route(self, msg: dict, text: str):
        # 1) reply / forward of an agent's message
        ref = msg.get("reply_message") or (msg.get("fwd_messages") or [None])[0]
        if ref:
            for k in ("id", "conversation_message_id"):
                sid = self.msg_to_sid.get(ref.get(k) or -1)
                if sid:
                    with self.lock:
                        b = self.bridges.get(sid)
                    if b:
                        return (b, text)
        # 2) #label prefix
        m = re.match(r"^#(\S+)\s*(.*)$", text, re.S)
        if m:
            b = self.find_label(m.group(1).replace("_", " "))
            if b:
                return (b, m.group(2))
        # 3) only one bridge
        with self.lock:
            bs = list(self.bridges.values())
        if len(bs) == 1:
            return (bs[0], text)
        return "none" if not bs else "ask"

    def deliver(self, b: Bridge, text: str):
        if not text:
            return
        kind = "stop" if text.strip().lower() in ("стоп", "stop", "/stop", "/стоп") else "msg"
        b.push({"kind": kind, "text": text, "at": time.time()})
        log.info("deliver %s -> %s: %s", kind, b.label, text[:80])

    def cmd_list(self):
        with self.lock:
            bs = list(self.bridges.values())
        if not bs:
            return self.send_raw("Включённых мостов нет.", sid=None)
        lines = [f"• {b.label} ({'занят' if b.busy else 'свободен'}) · {Path(b.cwd).name if b.cwd else ''}"
                 for b in bs]
        self.send_raw("Мосты\n" + "\n".join(lines), sid=None)

    def cmd_off(self, arg: str):
        with self.lock:
            bs = list(self.bridges.values())
        targets = bs if arg.lower() in ("all", "все", "всё") else ([self.find_label(arg)] if arg else
                                                                  (bs if len(bs) == 1 else []))
        targets = [t for t in targets if t]
        if not targets:
            return self.send_raw("Кого выключить? /off метка или /off all", sid=None)
        for b in targets:
            b.push({"kind": "off", "text": "", "at": time.time()})

    def cmd_help(self):
        self.send_raw(
            "Как писать агентам\n"
            "• один мост включён, просто пиши\n"
            "• несколько, отвечай на сообщение нужного агента или начни с #метка\n"
            "• стоп, прервать текущую работу агента\n"
            "• /list, какие мосты включены\n"
            "• /off метка или /off all, выключить мост", sid=None)


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

def vk_call(method: str, params: dict, timeout: float = 30) -> dict:
    params = dict(params, v=API_V)
    data = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request(API_BASE + method, data=data,
                                 headers={"Authorization": f"Bearer {TOKEN}"})
    for attempt in range(5):
        with urllib.request.urlopen(req, timeout=timeout) as r:
            res = json.loads(r.read().decode())
        err = res.get("error")
        if not err:
            return res.get("response")
        if err.get("error_code") in (6, 9, 10) and attempt < 4:
            time.sleep(1 + attempt)
            continue
        raise RuntimeError(f"VK {method} error {err.get('error_code')}: {err.get('error_msg')}")
    raise RuntimeError(f"VK {method}: retries exhausted")


def vk_send(text: str, keyboard: dict | None = None) -> list[int]:
    if STUB:
        mid = 10_000 + len(HUB.stub_out)
        HUB.stub_out.append({"id": mid, "text": text, "keyboard": keyboard, "at": time.time()})
        log.info("stub send #%s: %s", mid, text[:120])
        return [mid]
    params = {"peer_ids": OWNER_ID, "random_id": random.randint(1, 2**31 - 1),
              "message": text, "dont_parse_links": 1}
    if keyboard:
        params["keyboard"] = json.dumps(keyboard, ensure_ascii=False)
    try:
        res = vk_call("messages.send", params)
    except Exception as e:
        log.error("send failed: %s", e)
        return []
    ids = []
    for item in res if isinstance(res, list) else [res]:
        if isinstance(item, int):
            ids.append(item)
        elif isinstance(item, dict):
            for k in ("message_id", "conversation_message_id"):
                if item.get(k):
                    ids.append(item[k])
    return ids


def vk_mark_read(peer_id: int):
    if STUB:
        return
    try:
        vk_call("messages.markAsRead", {"peer_id": peer_id, "group_id": GROUP_ID})
    except Exception as e:
        log.warning("markAsRead: %s", e)


def longpoll_loop():
    server = key = ts = None
    while True:
        try:
            if not server:
                lp = vk_call("groups.getLongPollServer", {"group_id": GROUP_ID})
                server, key, ts = lp["server"], lp["key"], lp["ts"]
                log.info("long poll connected")
            url = f"{server}?act=a_check&key={key}&ts={ts}&wait=25"
            with urllib.request.urlopen(url, timeout=40) as r:
                res = json.loads(r.read().decode())
            if "failed" in res:
                if res["failed"] == 1:
                    ts = res.get("ts", ts)
                else:
                    server = None
                continue
            ts = res.get("ts", ts)
            for upd in res.get("updates", []):
                handle_update(upd)
        except Exception as e:
            log.error("long poll: %s", e)
            server = None
            time.sleep(5)


def handle_update(upd: dict):
    if upd.get("type") != "message_new":
        return
    msg = (upd.get("object") or {}).get("message") or {}
    if msg.get("from_id") != OWNER_ID or msg.get("peer_id") != OWNER_ID:
        log.warning("dropped message from %s in %s", msg.get("from_id"), msg.get("peer_id"))
        return
    vk_mark_read(OWNER_ID)
    try:
        HUB.on_owner_message(msg)
    except Exception as e:
        log.exception("handling message: %s", e)


# ---------------------------------------------------------------- HTTP API

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def _json(self, code: int, obj):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _guard(self) -> bool:
        if self.headers.get(HEADER) != "1" or self.headers.get("Origin"):
            self._json(403, {"error": "forbidden"})
            return False
        return True

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            return {}

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
            sid = q.get("sid", "")
            with HUB.lock:
                b = HUB.bridges.get(sid)
            if not b:
                return self._json(200, {"known": False, "messages": []})
            b.busy = q.get("busy") == "1"
            wait = max(0.0, min(float(q.get("wait", "20")), 25.0))
            ack = int(q.get("ack", "0") or 0)
            return self._json(200, {"known": True, "messages": b.take(wait, ack)})
        if u.path == "/stub/out" and STUB:
            since = int(q.get("since", "0"))
            return self._json(200, {"messages": [m for m in HUB.stub_out if m["id"] >= since]})
        self._json(404, {"error": "not found"})

    def do_POST(self):
        if not self._guard():
            return
        p = urllib.parse.urlparse(self.path).path
        d = self._body()
        if p == "/bridge/on":
            if not d.get("sid"):
                return self._json(400, {"error": "sid required"})
            b = HUB.bridge_on(d["sid"], d.get("label", ""), d.get("cwd", ""), d.get("rules", ""),
                              bool(d.get("quiet")))
            return self._json(200, {"ok": True, "label": b.label})
        if p == "/bridge/off":
            HUB.bridge_off(d.get("sid", ""), d.get("note"))
            return self._json(200, {"ok": True})
        if p == "/send":
            text = (d.get("text") or "").strip()
            if not text:
                return self._json(400, {"error": "text required"})
            sid = d.get("sid")
            with HUB.lock:
                known = sid in HUB.bridges
            if sid and not known:
                return self._json(200, {"ok": False, "known": False})
            label = d.get("label")
            if not sid and label:
                text = f"[{label}] {text}"
            HUB.send(sid, text, prefix=True)
            return self._json(200, {"ok": True})
        if p == "/stub/in" and STUB:
            msg = {"from_id": OWNER_ID, "peer_id": OWNER_ID, "text": d.get("text", ""),
                   "payload": d.get("payload")}
            if d.get("reply_to"):
                msg["reply_message"] = {"id": int(d["reply_to"])}
            HUB.on_owner_message(msg)
            return self._json(200, {"ok": True})
        self._json(404, {"error": "not found"})


def janitor(server: ThreadingHTTPServer):
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
        log.error("VK_TOKEN, VK_GROUP_ID and VK_OWNER_ID must be set in .env (or VK_STUB=1)")
        print("vk-hub: .env is incomplete, see .env.example", file=sys.stderr)
        sys.exit(2)
    try:
        server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    except OSError:
        log.info("port %s busy, another hub is running", PORT)
        sys.exit(0)
    server.daemon_threads = True
    log.info("hub started pid=%s port=%s stub=%s", os.getpid(), PORT, STUB)
    if not STUB:
        threading.Thread(target=longpoll_loop, daemon=True).start()
    threading.Thread(target=janitor, args=(server,), daemon=True).start()
    server.serve_forever()
    log.info("hub stopped")


def notify_cli(argv: list[str]):
    """python hub/vk_hub.py notify "text" [--label name]: one message to the owner."""
    label = None
    if "--label" in argv:
        i = argv.index("--label")
        label = argv[i + 1]
        argv = argv[:i] + argv[i + 2:]
    text = " ".join(argv).strip()
    if not text:
        print("usage: vk_hub.py notify \"text\" [--label name]", file=sys.stderr)
        sys.exit(2)
    # through a running hub if there is one, else straight to VK
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{PORT}/send",
            data=json.dumps({"text": text, "label": label}).encode(),
            headers={HEADER: "1", "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as r:
            r.read()
        return
    except Exception:
        pass
    if STUB:
        print("hub is not running (stub mode)", file=sys.stderr)
        sys.exit(1)
    vk_send(redact(f"[{label}] {text}" if label else text))


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "notify":
        notify_cli(sys.argv[2:])
    else:
        serve()
