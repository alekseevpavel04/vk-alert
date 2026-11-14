"""Wait for messages a stub-mode hub sent:  python tests/stub_wait.py since_id [count] [timeout_s]"""
import importlib.util
import sys
import time
from pathlib import Path

spec = importlib.util.spec_from_file_location("vk_hub", Path(__file__).resolve().parent.parent / "hub" / "vk_hub.py")
hub = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hub)

since, count = int(sys.argv[1]), int(sys.argv[2]) if len(sys.argv) > 2 else 1
deadline = time.time() + (float(sys.argv[3]) if len(sys.argv) > 3 else 120)
while True:
    msgs = hub.signed_request(f"/stub/out?since={since}")[1]["messages"]
    if len(msgs) >= count or time.time() > deadline:
        for m in msgs:
            print(f"#{m['id']}: {m['text']}")
        break
    time.sleep(1)
