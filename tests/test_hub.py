"""vk-hub tests, stub mode, isolated data dir:  python tests/test_hub.py"""
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PORT = 8799
DATA = Path(tempfile.mkdtemp(prefix="vkhub-test-"))
ENV = dict(os.environ, VK_STUB="1", HUB_PORT=str(PORT), VK_OWNER_ID="1", VK_GROUP_ID="5",
           VK_TOKEN="", VK_HUB_DATA=str(DATA))
os.environ.update({k: ENV[k] for k in ("HUB_PORT", "VK_OWNER_ID", "VK_GROUP_ID", "VK_TOKEN", "VK_HUB_DATA")})

spec = importlib.util.spec_from_file_location("vk_hub", ROOT / "hub" / "vk_hub.py")
hub = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hub)

RESULTS = []


def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond)))
    print(("ok   " if cond else "FAIL ") + name + ("" if cond else f"  {detail}"))


def call(path, body=None):
    return hub.signed_request(path, body)


def out(since=0):
    return call(f"/stub/out?since={since}")[1]["messages"]


ACKS = {}


def inbox(sid, wait=0):
    r = call(f"/inbox?sid={sid}&wait={wait}&ack={ACKS.get(sid, 0)}")[1]
    for m in r.get("messages", []):
        ACKS[sid] = max(ACKS.get(sid, 0), m["id"])
    return r


def say(text, **kw):
    return call("/stub/in", dict(kw, text=text))


def last():
    return out()[-1]["text"]


def unit_tests():
    # markdown -> plain text
    md = "## Итог\n\n**Готово**, тесты `pytest` проходят.\n- пункт\n| a | b |\n|---|---|\n| 1 | 2 |\n[док](http://x.y)\n```py\nprint(1)\n```"
    p = hub.plain(md)
    check("plain: заголовок, жирный, код, список", p.startswith("Итог\n\nГотово, тесты pytest проходят.\n• пункт"), p)
    check("plain: таблица и ссылка", "a · b\n1 · 2\nдок (http://x.y)\nprint(1)" in p, p)
    check("plain: snake_case не трогает", hub.plain("файл my_var_name.py") == "файл my_var_name.py")
    # chunks in UTF-16 units, no split surrogates
    parts = hub.chunks("😀" * 5000, 3900)
    check("chunks: эмодзи по UTF-16", all(hub.units(x) <= 3900 for x in parts) and "".join(parts) == "😀" * 5000,
          [hub.units(x) for x in parts])
    words = hub.chunks(("слово " * 2000).strip(), 3900)
    check("chunks: режет по словам", all(not x.startswith("ово") for x in words))
    # labels
    check("label: пробелы и длина", hub.norm_label("Проект А · Задание номер 2") == "Проект-А-Задание")
    check("label: пустая", hub.norm_label("  ") == "сессия")
    # redact
    leaks = ["sk-ant-api03-abcdefghijklmnop", "sk-proj-abcdefghijklmnopqrstuvwx", "AIza" + "B" * 35,
             "postgres://user:hunter2pass@db:5432/x", '{"api_key": "abcd1234efgh"}',
             "Authorization: Bearer abcdefghijklmnop123456", "123456789:" + "A" * 35, "token=abcdef123456"]
    bad = [s for s in leaks if any(part in hub.redact(s) for part in ("abcdefghijklmnop", "hunter2pass", "abcd1234efgh", "B" * 35, "A" * 35, "abcdef123456"))]
    check("redact: ловит секреты", not bad, bad)
    safe = ["max_tokens=200000", "tokenizer: bert-base-uncased", "token count = 5", "tokens_used: 123456",
            "secret_santa", "файл my_var_name.py"]
    changed = [s for s in safe if hub.redact(s) != s]
    check("redact: не трогает обычный текст", not changed, changed)
    check("stop: пунктуация и регистр", all(hub.is_stop(s) for s in ("стоп", "Стоп!", "СТОП.", "stop ", "стоп…")))


def live_tests():
    proc = subprocess.Popen([sys.executable, str(ROOT / "hub" / "vk_hub.py")], env=ENV)
    try:
        for _ in range(50):
            try:
                call("/health")
                break
            except Exception:
                time.sleep(0.1)

        # exclusive bind: a second hub exits
        second = subprocess.Popen([sys.executable, str(ROOT / "hub" / "vk_hub.py")], env=ENV)
        check("второй хаб не занимает порт", second.wait(timeout=10) == 0)

        # auth
        def raw_get(headers):
            req = urllib.request.Request(f"http://127.0.0.1:{PORT}/health", headers=headers)
            try:
                return urllib.request.urlopen(req).status
            except urllib.error.HTTPError as e:
                return e.code
        check("без подписи 403", raw_get({}) == 403)
        ts = str(int(time.time()))
        good = ts + "." + hub.sign("req", "GET", "/health", ts, hub.sha(b""))
        check("подпись проходит", raw_get({"X-VK-Hub-Auth": good}) == 200)
        check("чужой Host 403", raw_get({"X-VK-Hub-Auth": good, "Host": "evil.example:8799"}) == 403)
        check("Origin 403", raw_get({"X-VK-Hub-Auth": good, "Origin": "http://evil"}) == 403)
        old = "100." + hub.sign("req", "GET", "/health", "100", hub.sha(b""))
        check("старая подпись 403", raw_get({"X-VK-Hub-Auth": old}) == 403)

        say("привет")
        check("нет мостов", "Нет включённых мостов" in last(), last())

        call("/bridge/on", {"sid": "s1", "label": "shopper", "cwd": "D:/shopper"})
        check("включение, служебная строка", last() == "shopper · мост включён", last())
        say("сделай X")
        check("один мост, без адреса", inbox("s1")["messages"][0]["text"] == "сделай X")

        call("/send", {"sid": "s1", "text": "**Готово**, token=abcdef123456"})
        m = out()[-1]
        check("ответ: шапка, без markdown, секрет скрыт", m["text"] == "shopper\nГотово, token=[REDACTED]", m["text"])
        a_msg = m["id"]
        call("/send", {"sid": "s1", "text": "Прод упал", "kind": "alert"})
        check("тревога заметная", last() == "‼️ ПРОБЛЕМА · shopper\nПрод упал", last())
        status, res = call("/send", {"sid": "s1", "text": "x", "kind": "loud"})
        check("неизвестный kind 400", status == 400)

        call("/bridge/on", {"sid": "s2", "label": "shopper", "quiet": True})
        check("дубль метки", call("/health")[1]["bridges"][1]["label"] == "shopper-2")
        call("/bridge/on", {"sid": "s2", "label": "ltt 2", "quiet": True})

        say("ещё", reply_to=a_msg)
        check("ответ на сообщение агента", inbox("s1")["messages"][0]["text"] == "ещё")
        say("#ltt привет")
        check("#метка по началу", inbox("s2")["messages"][0]["text"] == "привет")
        say("#ltt-2 и так")
        check("#метка целиком", inbox("s2")["messages"][0]["text"] == "и так")
        say("#нету привет")
        check("#неизвестная метка не уходит другим", "Нет моста «нету»" in last() and not inbox("s1")["messages"])
        say("#ltt")
        check("#метка без текста", "нужен текст" in last(), last())

        say("кому это?")
        ask = out()[-1]
        check("без адреса кнопки", ask["text"].startswith("Кому отправить") and len(ask["keyboard"]["buttons"]) == 2)
        payload = ask["keyboard"]["buttons"][1][0]["action"]["payload"]
        say("ltt-2", payload=payload)
        check("кнопка доставляет", inbox("s2")["messages"][0]["text"] == "кому это?")
        say("ltt-2", payload=payload)
        check("повторная кнопка", "устарело" in last())

        # reply to a session that is gone
        call("/send", {"sid": "s2", "text": "я ltt"})
        ltt_msg = out()[-1]["id"]
        call("/bridge/off", {"sid": "s2", "note": "сессия закрыта"})
        check("закрытие, служебная строка", last() == "ltt-2 · сессия закрыта", last())
        say("удали build", reply_to=ltt_msg)
        check("ответ отключённой сессии не уходит другой",
              "уже отключилась" in last() and not inbox("s1")["messages"], last())

        # attachments
        say("", attachments=[{"type": "sticker"}])
        check("стикер без текста", "только текст" in last(), last())
        say("смотри", attachments=[{"type": "photo"}])
        check("фото с подписью: текст дошёл", inbox("s1")["messages"][0]["text"] == "смотри" and "вложения" in last())

        # stop
        say("Стоп!")
        check("Стоп! это stop", inbox("s1")["messages"][0]["kind"] == "stop")
        call("/bridge/on", {"sid": "s3", "label": "aux", "quiet": True})
        call(f"/inbox?sid=s1&wait=0&busy=1&ack={ACKS.get('s1', 0)}")
        say("стоп")
        check("стоп без адреса бьёт занятых", inbox("s1")["messages"][0]["kind"] == "stop" and not inbox("s3")["messages"])

        # /off
        say("/offline")
        check("/offline не команда", "Кому отправить" in last(), last())
        say("/off all")
        check("/off all", inbox("s1")["messages"][-1]["kind"] == "off" and inbox("s3")["messages"][-1]["kind"] == "off")

        # long poll wakes on delivery
        call("/bridge/off", {"sid": "s3"})
        t0 = time.time()
        threading.Timer(0.5, lambda: say("пинг")).start()
        got = inbox("s1", wait=5)
        check("long poll будит", got["messages"][0]["text"] == "пинг" and time.time() - t0 < 2)

        # chunks with headers
        before = out()[-1]["id"]
        call("/send", {"sid": "s1", "text": "x" * 9000})
        parts = out(before + 1)
        check("длинный ответ частями с шапкой",
              len(parts) == 3 and all(p["text"].startswith(f"shopper ({i}/3)\n") for i, p in enumerate(parts, 1)),
              [p["text"][:20] for p in parts])

        # unacknowledged items are delivered again
        call("/bridge/on", {"sid": "s4", "label": "R", "quiet": True})
        say("#R раз")
        first = call("/inbox?sid=s4&wait=0&ack=0")[1]["messages"]
        again = call("/inbox?sid=s4&wait=0&ack=0")[1]["messages"]
        gone = call(f"/inbox?sid=s4&wait=0&ack={first[0]['id']}")[1]["messages"]
        check("без подтверждения приходит снова", again[0]["id"] == first[0]["id"] and gone == [])
        check("плохой ack 400", call("/inbox?sid=s4&ack=abc")[0] == 400)

        call("/bridge/off", {"sid": "s1"})
        check("после выключения sid неизвестен", inbox("s1")["known"] is False)
    finally:
        proc.terminate()


if __name__ == "__main__":
    unit_tests()
    live_tests()
    bad = [n for n, ok in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(bad)}/{len(RESULTS)} ok")
    sys.exit(1 if bad else 0)
