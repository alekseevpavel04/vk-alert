"""Wait for new outgoing messages of a stub-mode hub:  python tests/stub_wait.py since_id [count] [timeout_s]"""
import json, sys, time, urllib.request
since, count = int(sys.argv[1]), int(sys.argv[2]) if len(sys.argv) > 2 else 1
deadline = time.time() + (float(sys.argv[3]) if len(sys.argv) > 3 else 120)
while True:
    req = urllib.request.Request(f"http://127.0.0.1:8765/stub/out?since={since}", headers={"X-VK-Hub": "1"})
    msgs = json.loads(urllib.request.urlopen(req, timeout=10).read().decode())["messages"]
    if len(msgs) >= count or time.time() > deadline:
        for m in msgs:
            print(f"#{m['id']}: {m['text']}")
        break
    time.sleep(1)
