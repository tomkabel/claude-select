#!/usr/bin/env python3
"""End-to-end self-check: python3 test_picker.py (runs isolated servers, touches nothing global)."""
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

HERE = Path(__file__).resolve().parent
EX = HERE.parents[2] / "examples"
HOME = tempfile.mkdtemp(prefix="claude-select-test-")
ENV = {**os.environ, "CLAUDE_SELECT_HOME": HOME}


def cli(*args):
    r = subprocess.run([sys.executable, str(HERE / "picker.py"), *args], env=ENV, capture_output=True, text=True, timeout=30, check=False)
    return r.returncode, json.loads(r.stdout)


def http(url, body=None, token=None, host=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-Select-Token"] = token
    if host:
        headers["Host"] = host
    req = Request(url, data=json.dumps(body).encode() if body is not None else None, headers=headers)
    try:
        with urlopen(req, timeout=5) as r:
            return r.status, r.read()
    except HTTPError as e:
        return e.code, e.read()


def main():
    # validation rejects an over-long short_text before any server starts
    bad = Path(HOME) / "bad.json"
    bad.write_text(json.dumps({"tasks": {"t": {}}, "items": [
        {"id": "x", "type": "short_text", "content": "x" * 501, "generation_task_id": "t"}]}))
    code, r = cli("start", str(bad), "--no-open")
    assert code == 1 and r["error"] == "invalid_session" and "501 chars" in r["details"][0], r

    # port conflict: occupy a port, start there, expect a fallback port
    blocker = socket.socket()
    blocker.bind(("127.0.0.1", 0))
    blocker.listen()
    busy = blocker.getsockname()[1]
    code, r = cli("start", str(EX / "multi-mixed.json"), "--no-open", "--port", str(busy))
    assert code == 0 and r["ok"] and r["port"] != busy and r["port_fallback"], r
    st = json.loads((Path(HOME) / "server.json").read_text())
    base, tok = f"http://127.0.0.1:{st['port']}", st["token"]

    assert http(base + "/api/session")[0] == 401                           # no token
    assert http(base + "/api/session", token=tok, host="evil.test")[0] == 403  # DNS rebinding
    code, raw = http(base + "/api/session", token=tok)
    s = json.loads(raw)
    assert code == 200 and s["mode"] == "multi" and len(s["items"]) == 5
    img = s["items"][0]["src"]
    code, raw = http(base + img)
    assert code == 200 and raw.startswith(b"<svg")

    # regenerate one item -> agent gets the task back
    ev = {"client_event_id": "r1", "action": "regenerate", "selected_ids": ["logo-b"], "feedback": "bolder"}
    assert http(base + "/api/event", ev, tok)[0] == 200
    code, r = cli("poll", "--timeout", "10")
    assert r["type"] == "regenerate" and r["regenerate"] and r["feedback"] == "bolder", r
    assert r["tasks"] == [{"generation_task_id": "logo", "item_ids": ["logo-b"],
                           "task": {"prompt": "Minimal geometric logo mark for Aurora, flat, two colours.",
                                    "params": {"size": "240x180", "format": "svg"}}}], r
    code, raw = http(base + "/api/event", {"client_event_id": "x", "action": "submit", "selected_ids": []}, tok)
    assert code == 409 and b"regeneration in progress" in raw

    code, r = cli("update", str(EX / "regenerated-logo-b.json"))
    assert code == 0 and r["pending"] == [], r
    s = json.loads(http(base + "/api/session", token=tok)[1])
    b = next(i for i in s["items"] if i["id"] == "logo-b")
    assert b["revision"] == 2 and "bolder" in b["metadata"]["label"] and s["pending"] == []

    # empty multi submit, retried with the same client id -> one event only
    ev = {"client_event_id": "s1", "action": "submit", "selected_ids": [], "feedback": "none fit"}
    assert http(base + "/api/event", ev, tok)[0] == 200
    assert json.loads(http(base + "/api/event", ev, tok)[1]).get("duplicate")
    code, r = cli("poll", "--timeout", "10")
    assert r["type"] == "submit" and r["selected_ids"] == [] and "nothing selected" in r["_instructions"], r
    code, r = cli("poll", "--timeout", "3")
    assert r["type"] == "waiting", r
    blocker.close()

    # single mode: exactly one winner, selected item content comes back
    code, r = cli("start", str(EX / "single-taglines.json"), "--no-open")
    assert code == 0, r
    st = json.loads((Path(HOME) / "server.json").read_text())
    base, tok = f"http://127.0.0.1:{st['port']}", st["token"]
    two = {"client_event_id": "a", "action": "submit", "selected_ids": ["t1", "t2"]}
    assert http(base + "/api/event", two, tok)[0] == 409
    one = {"client_event_id": "b", "action": "submit", "selected_ids": ["t2"]}
    assert http(base + "/api/event", one, tok)[0] == 200
    code, r = cli("poll")
    assert r["selection_mode"] == "single" and r["selected"][0]["content"] == "Irregular income. Regular calm.", r

    # timeout
    code, r = cli("start", str(EX / "single-taglines.json"), "--no-open", "--timeout", "1")
    time.sleep(1.5)
    code, r = cli("poll")
    assert r["type"] == "timeout", r
    code, r = cli("stop")
    assert r["stopped"] and not (Path(HOME) / "server.json").exists()
    code, r = cli("poll")
    assert code == 1 and r["error"] == "no_server"
    print("ok")


if __name__ == "__main__":
    main()
