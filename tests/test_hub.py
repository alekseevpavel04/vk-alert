"""Stub-mode tests for vk-hub: run with  python tests/test_hub.py"""
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PORT = 8799
BASE = f"http://127.0.0.1:{PORT}"


def call(method, path, body=None, headers=None):
    h = {"X-VK-Hub": "1", "Content-Type": "application/json"}
    h.update(headers or {})
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


def out(since=0):
    return call("GET", f"/stub/out?since={since}")[1]["messages"]


ACKS = {}


def inbox(sid, wait=0):
    """Poll like the mod does: take new items, acknowledge them on the next poll."""
    r = call("GET", f"/inbox?sid={sid}&wait={wait}&ack={ACKS.get(sid, 0)}")[1]
    for m in r.get("messages", []):
        ACKS[sid] = max(ACKS.get(sid, 0), m["id"])
    return r


def main():
    env = dict(os.environ, VK_STUB="1", HUB_PORT=str(PORT), VK_OWNER_ID="1", VK_TOKEN="")
    proc = subprocess.Popen([sys.executable, str(ROOT / "hub" / "vk_hub.py")], env=env)
    ok = 0
    try:
        for _ in range(50):
            try:
                call("GET", "/health")
                break
            except Exception:
                time.sleep(0.1)

        # guard: no header, or a browser Origin -> 403
        req = urllib.request.Request(BASE + "/health")
        try:
            urllib.request.urlopen(req)
            raise AssertionError("no header must be refused")
        except urllib.error.HTTPError as e:
            assert e.code == 403
        assert call("GET", "/health", headers={"Origin": "http://evil"})[0] == 403
        ok += 1

        # no bridges
        call("POST", "/stub/in", {"text": "привет"})
        assert "Нет включённых мостов" in out()[-1]["text"]
        ok += 1

        # one bridge: plain text goes to it
        assert call("POST", "/bridge/on", {"sid": "s1", "label": "A · Задание 2", "cwd": "D:/A"})[1]["label"] == "A · Задание 2"
        assert out()[-1]["text"] == "[A · Задание 2] 🔌 мост включён"
        call("POST", "/stub/in", {"text": "сделай X"})
        assert inbox("s1")["messages"][0]["text"] == "сделай X"
        ok += 1

        # agent message, then a second bridge with a duplicate label
        call("POST", "/send", {"sid": "s1", "text": "готово, token=abcdef123456"})
        last = out()[-1]
        assert last["text"] == "[A · Задание 2] готово, token=[REDACTED]", last
        a_msg = last["id"]
        assert call("POST", "/bridge/on", {"sid": "s2", "label": "A · Задание 2"})[1]["label"] == "A · Задание 2 2"
        call("POST", "/bridge/on", {"sid": "s2", "label": "Проект 2", "quiet": True})
        ok += 1

        # two bridges: reply routes, #label routes, plain text asks
        call("POST", "/stub/in", {"text": "ещё", "reply_to": a_msg})
        assert inbox("s1")["messages"][0]["text"] == "ещё"
        call("POST", "/stub/in", {"text": "#проект привет"})
        assert inbox("s2")["messages"][0]["text"] == "привет"
        call("POST", "/stub/in", {"text": "кому это?"})
        ask = out()[-1]
        assert ask["text"].startswith("Кому отправить") and len(ask["keyboard"]["buttons"]) == 2
        payload = ask["keyboard"]["buttons"][1][0]["action"]["payload"]
        call("POST", "/stub/in", {"text": "Проект 2", "payload": payload})
        assert inbox("s2")["messages"][0]["text"] == "кому это?"
        ok += 1

        # stop and off
        call("POST", "/stub/in", {"text": "стоп", "reply_to": a_msg})
        assert inbox("s1")["messages"][0]["kind"] == "stop"
        call("POST", "/stub/in", {"text": "/off all"})
        assert inbox("s1")["messages"][0]["kind"] == "off" and inbox("s2")["messages"][0]["kind"] == "off"
        ok += 1

        # long poll wakes on delivery
        t0 = time.time()
        import threading
        threading.Timer(0.5, lambda: call("POST", "/stub/in", {"text": "#проект пинг"})).start()
        got = inbox("s2", wait=5)
        assert got["messages"][0]["text"] == "пинг" and time.time() - t0 < 2
        ok += 1

        # chunking and list
        call("POST", "/send", {"sid": "s2", "text": "x" * 9000})
        assert len([m for m in out() if m["text"].startswith("[Проект 2] xxx") or set(m["text"]) == {"x"}]) == 3
        call("POST", "/stub/in", {"text": "/list"})
        assert "Проект 2" in out()[-1]["text"]
        ok += 1

        # off, unknown sid after off
        call("POST", "/bridge/off", {"sid": "s1", "note": "⏹ мост выключен"})
        assert out()[-1]["text"] == "[A · Задание 2] ⏹ мост выключен"
        assert inbox("s1")["known"] is False
        ok += 1
        # unacknowledged items are delivered again (mod died mid-poll)
        call("POST", "/bridge/on", {"sid": "s3", "label": "R"})
        call("POST", "/stub/in", {"text": "#R раз"})
        first = call("GET", "/inbox?sid=s3&wait=0&ack=0")[1]["messages"]
        again = call("GET", "/inbox?sid=s3&wait=0&ack=0")[1]["messages"]
        assert [m["text"] for m in again] == ["раз"] and again[0]["id"] == first[0]["id"]
        gone = call("GET", f"/inbox?sid=s3&wait=0&ack={first[0]['id']}")[1]["messages"]
        assert gone == []
        ok += 1
        print(f"OK {ok}/10")
    finally:
        proc.terminate()


if __name__ == "__main__":
    main()
