"""Send a fake owner message to a stub-mode hub:  python tests/stub_say.py "text" [reply_to_id]"""
import json, sys, urllib.request
body = {"text": sys.argv[1]}
if len(sys.argv) > 2:
    body["reply_to"] = int(sys.argv[2])
req = urllib.request.Request("http://127.0.0.1:8765/stub/in", data=json.dumps(body).encode(),
                             headers={"X-VK-Hub": "1", "Content-Type": "application/json"})
print(urllib.request.urlopen(req, timeout=10).read().decode())
