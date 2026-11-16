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
           VK_TOKEN="", VK_HUB_DATA=str(DATA), VK_SHELL="1")
os.environ.update({k: ENV[k] for k in ("HUB_PORT", "VK_OWNER_ID", "VK_GROUP_ID", "VK_TOKEN", "VK_HUB_DATA", "VK_SHELL")})

spec = importlib.util.spec_from_file_location("vk_hub", ROOT / "hub" / "vk_hub.py")
hub = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hub)

RESULTS = []


def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond)))
    print(("ok   " if cond else "FAIL ") + name + ("" if cond else f"  {detail}"))


def call(path, body=None):
    return hub.signed_request(path, body)


def no_circles(text):
    for c in hub.CIRCLES:
        text = text.replace(c + " ", "")
    return text


def raw_out(since=0):
    return call(f"/stub/out?since={since}")[1]["messages"]


def out(since=0):
    """Отправленное без цветных кружков, чтобы проверки текста не зависели от цвета."""
    msgs = raw_out(since)
    for m in msgs:
        m["text"] = no_circles(m["text"])
        for row in (m.get("keyboard") or {}).get("buttons", []):
            for btn in row:
                btn["action"]["label"] = no_circles(btn["action"]["label"])
    return msgs


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
    # shell commands
    check("! команда", hub.shell_command("! python x.py --a") == "python x.py --a" and hub.shell_command("!ls") == "ls")
    check("!! и ! без текста не команды", not any(hub.shell_command(s) for s in ("!", "!!", "!!! срочно", "ну ! ls")))
    # TLS for Long Poll
    check("long poll только https", hub.https_url("http://lp.vk.com/whp/5") == "https://lp.vk.com/whp/5"
          and hub.https_url("lp.vk.com/x") == "https://lp.vk.com/x"
          and hub.https_url("https://lp.vk.com/x") == "https://lp.vk.com/x")
    # shell switched off: the command is not delivered
    b = hub.Bridge("u1", "u", "", "")
    hub.SHELL, hub.STUB = False, True
    hub.HUB.deliver(b, "! rm -rf build")
    hub.SHELL, hub.STUB = True, False
    check("VK_SHELL выключен: команда не доставлена", not b.queue and "выключены" in hub.HUB.stub_out[-1]["text"])
    # VK refuses keyboards without the bot feature (error 912): the text still goes
    sent = []

    def fake_call(method, params, timeout=30):
        if "keyboard" in params:
            raise RuntimeError("VK messages.send error 912: This is a chat bot feature")
        sent.append(params["message"])
        return [{"peer_id": 1, "conversation_message_id": 7}]
    real_call, hub.vk_call = hub.vk_call, fake_call
    try:
        ids = hub.vk_send("Нажми кнопку", {"inline": True, "buttons": []})
    finally:
        hub.vk_call = real_call
    check("ошибка 912: сообщение уходит без кнопок с подсказкой",
          ids == [7] and sent and sent[0].startswith("Нажми кнопку") and "Возможностей ботов" in sent[0], sent)


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

        status, on = call("/bridge/on", {"sid": "s1", "label": "shopper", "cwd": "D:/shopper"})
        check("включение, служебная строка", last() == "shopper · мост включён", last())
        check("хаб сообщает моду, что команды разрешены", on.get("shell") is True, on)
        say("сделай X")
        check("один мост, без адреса", inbox("s1")["messages"][0]["text"] == "сделай X")

        # "! команда" goes as shell with the time it was sent
        say("! git status", date=1700000000)
        m = inbox("s1")["messages"][0]
        check("! команда уходит как shell со временем отправки",
              m["kind"] == "shell" and m["text"] == "! git status" and m["at"] == 1700000000 and "wake" not in m, m)
        say("!!! срочно глянь")
        check("!!! это обычное сообщение", inbox("s1")["messages"][0]["kind"] == "msg")
        say("#shop ! ls")
        check("#метка и команда", inbox("s1")["messages"][0]["kind"] == "shell")

        # permission question: buttons, a word answer, a closed question
        status, a = call("/ask", {"sid": "s1", "text": "Нужно твоё разрешение\nBash: modal run x.py"})
        q = out()[-1]
        check("вопрос с кнопками Да и Нет", status == 200 and q["keyboard"]["inline"]
              and [b["action"]["label"] for b in q["keyboard"]["buttons"][0]] == ["Да", "Нет"], q)
        check("пока ответа нет", call(f"/ask?id={a['id']}&wait=0")[1]["answer"] == "")
        yes = q["keyboard"]["buttons"][0][0]["action"]["payload"]
        threading.Timer(0.5, lambda: say("Да", payload=yes)).start()
        t0 = time.time()
        check("кнопка «Да» будит ожидание", call(f"/ask?id={a['id']}&wait=5")[1]["answer"] == "yes"
              and time.time() - t0 < 3 and last() == "shopper · разрешил", last())
        say("Да", payload=yes)
        check("повторное нажатие", "уже закрыт" in last(), last())
        _, a2 = call("/ask", {"sid": "s1", "text": "ещё вопрос"})
        say("нет!")
        check("ответ словом и не уходит агенту", call(f"/ask?id={a2['id']}&wait=0")[1]["answer"] == "no"
              and not inbox("s1")["messages"])
        _, a3 = call("/ask", {"sid": "s1", "text": "третий"})
        call("/ask/close", {"id": a3["id"], "note": "вопрос закрыт в терминале"})
        check("закрыт в терминале", call(f"/ask?id={a3['id']}&wait=0")[1]["answer"] == "gone"
              and last() == "shopper · вопрос закрыт в терминале", last())
        say("да")
        check("«да» без открытого вопроса идёт агенту", inbox("s1")["messages"][0]["text"] == "да")
        call("/bridge/on", {"sid": "s9", "label": "other", "quiet": True})
        _, a4 = call("/ask", {"sid": "s9", "text": "вопрос второй сессии"})
        say("да")
        check("два моста, один открытый вопрос: «да» без адреса уходит ему",
              call(f"/ask?id={a4['id']}&wait=0")[1]["answer"] == "yes" and last() == "other · разрешил", last())
        call("/bridge/off", {"sid": "s9"})

        # command buttons from the agent: shown as text, run once
        call("/send", {"sid": "s1", "text": "Запусти выгрузку", "buttons": [
            {"label": "Выгрузить", "command": "python up.py --all *.bin"}, {"command": "git status"}]})
        m = out()[-1]
        labels = [r[0]["action"]["label"] for r in m["keyboard"]["buttons"]]
        check("кнопки с командами и их текст", labels == ["shopper · Выгрузить", "shopper · git status"]
              and m["text"].endswith("Кнопки\n• Выгрузить → ! python up.py --all *.bin\n• git status → ! git status"), m)
        p = m["keyboard"]["buttons"][0][0]["action"]["payload"]
        say("Выгрузить", payload=p)
        it = inbox("s1")["messages"][0]
        check("нажатие запускает команду", it["kind"] == "shell" and it["text"] == "! python up.py --all *.bin" and it.get("wake") is True, it)
        say("Выгрузить", payload=p)
        check("второе нажатие не запускает", "уже нажата" in last() and not inbox("s1")["messages"], last())

        # command output is sent as is, without the markdown rewrite
        call("/send", {"sid": "s1", "text": "| a | b |\n**x** token=abcdef123456", "kind": "output"})
        check("вывод команды без переделки, секрет скрыт",
              last() == "shopper\n| a | b |\n**x** token=[REDACTED]", last())

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
        # a circle per agent: different for two live bridges, in headers and on buttons
        call("/send", {"sid": "s1", "text": "раз"})
        c1 = raw_out()[-1]["text"].split(" ", 1)[0]
        call("/send", {"sid": "s2", "text": "два"})
        c2 = raw_out()[-1]["text"].split(" ", 1)[0]
        check("у двух агентов разные кружки", c1 in hub.CIRCLES and c2 in hub.CIRCLES and c1 != c2, (c1, c2))
        call("/send", {"sid": "s1", "text": "кнопка", "buttons": [{"label": "Пуск", "command": "ls"}]})
        lab = raw_out()[-1]["keyboard"]["buttons"][0][0]["action"]["label"]
        check("кружок и метка на кнопке", lab == f"{c1} shopper · Пуск", lab)
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
