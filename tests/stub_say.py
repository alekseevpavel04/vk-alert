"""Fake owner message for a stub-mode hub:  python tests/stub_say.py "text" [reply_to_id]

Start the hub in stub mode first:  VK_STUB=1 python hub/vk_hub.py
"""
import importlib.util
import sys
from pathlib import Path

spec = importlib.util.spec_from_file_location("vk_hub", Path(__file__).resolve().parent.parent / "hub" / "vk_hub.py")
hub = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hub)

body = {"text": sys.argv[1]}
if len(sys.argv) > 2:
    body["reply_to"] = int(sys.argv[2])
print(hub.signed_request("/stub/in", body))
