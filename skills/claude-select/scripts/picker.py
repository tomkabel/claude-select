#!/usr/bin/env python3
"""claude-select: show generated options on a localhost page, relay the user's pick back.

Every subcommand prints exactly one JSON object on stdout. Objects carry an
`_instructions` field: the authoritative next step for the agent.

  start SESSION.json [--port N] [--no-open] [--timeout S]
  poll [--timeout S]        wait (checking every 2 s) for the next user event
  update ITEMS.json         replace (same id) or append items, reopen the session
  status                    server + session state
  stop                      shut the server down
  serve                     internal: the server process itself

Stdlib only; Python >= 3.10.
"""
import argparse
import json
import mimetypes
import os
import secrets
import signal
import subprocess
import sys
import threading
import time
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import NoReturn
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen

HERE = Path(__file__).resolve().parent
STATE_DIR = Path(os.environ.get("CLAUDE_SELECT_HOME") or Path.home() / ".cache" / "claude-select")
STATE = STATE_DIR / "server.json"      # port, pid, token of the running server
SAVED = STATE_DIR / "session.json"     # latest session incl. regenerated items (crash recovery)
LOG = STATE_DIR / "server.log"
DEFAULT_PORT = int(os.environ.get("CLAUDE_SELECT_PORT", "8765"))
PORT_TRIES = 10                        # DEFAULT_PORT .. +9, then an OS-assigned port
POLL_INTERVAL = 2
DEFAULT_TIMEOUT = 1800                 # seconds the user has to decide, reset by each update
BROWSER_STALE = 10                     # no page heartbeat for this long = tab closed / offline
SHORT_LIMIT = 500
TYPES = ("image", "short_text", "long_text")
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".svg", ".webp", ".gif"}
SELF = f'python3 "{Path(__file__).resolve()}"'
URL_PREFIXES = ("http://", "https://", "data:image/")


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def out(obj, code=0) -> NoReturn:
    print(json.dumps(obj, indent=2, ensure_ascii=False))
    sys.exit(code)


# ---------------------------------------------------------------- validation

def check_items(items, base, tasks):
    """Validate items in place (resolves local image paths). Returns a list of errors."""
    errs, seen = [], set()
    if not isinstance(items, list) or not items:
        return ["items must be a non-empty list"]
    for i, it in enumerate(items):
        at = f"items[{i}]"
        if not isinstance(it, dict):
            errs.append(f"{at} must be an object")
            continue
        iid, kind, content = it.get("id"), it.get("type"), it.get("content")
        if not isinstance(iid, str) or not iid:
            errs.append(f"{at}.id must be a non-empty string")
        elif iid in seen:
            errs.append(f"{at}.id '{iid}' is duplicated")
        seen.add(iid)
        if kind not in TYPES:
            errs.append(f"{at}.type must be one of {list(TYPES)}")
        if not isinstance(it.setdefault("metadata", {}), dict):
            errs.append(f"{at}.metadata must be an object")
        tid = it.get("generation_task_id")
        if not isinstance(tid, str) or tid not in tasks:
            errs.append(f"{at}.generation_task_id must name an entry in tasks")
        if not isinstance(content, str) or not content:
            errs.append(f"{at}.content must be a non-empty string")
            continue
        if kind == "short_text" and len(content) > SHORT_LIMIT:
            errs.append(f"{at}: short_text is {len(content)} chars (max {SHORT_LIMIT}); use long_text")
        if kind == "image" and not content.startswith(URL_PREFIXES):
            p = Path(content).expanduser()
            p = (p if p.is_absolute() else base / p).resolve()
            if p.suffix.lower() not in IMAGE_EXT:
                errs.append(f"{at}: unsupported image type '{p.suffix}' (use {sorted(IMAGE_EXT)})")
            elif not p.is_file():
                errs.append(f"{at}: image file not found: {p}")
            else:
                it["content"] = str(p)
    return errs


def check_tasks(tasks):
    if not isinstance(tasks, dict) or not all(isinstance(v, dict) for v in tasks.values()):
        return ["tasks must be an object: {generation_task_id: {prompt, params?}}"]
    return []


def check_session(s, base):
    if not isinstance(s, dict):
        return ["session must be a JSON object"]
    errs = []
    if s.setdefault("mode", "single") not in ("single", "multi"):
        errs.append("mode must be 'single' or 'multi'")
    t = s.get("timeout_s", DEFAULT_TIMEOUT)
    if not isinstance(t, (int, float)) or t <= 0:
        errs.append("timeout_s must be a positive number")
    errs += check_tasks(s.setdefault("tasks", {}))
    return errs or check_items(s.get("items"), base, s["tasks"])


def load_json(path):
    p = Path(path).expanduser().resolve()
    try:
        return json.loads(p.read_text()), p.parent
    except (OSError, ValueError) as e:
        out({"ok": False, "error": "unreadable_json", "details": [f"{p}: {e}"],
             "_instructions": "Fix the file path or JSON syntax and rerun."}, 1)


# ---------------------------------------------------------------- server

def serve(args):
    token = os.environ["CLAUDE_SELECT_TOKEN"]
    s = json.loads(SAVED.read_text())
    lock = threading.Lock()
    st = {
        "version": 1, "status": "open", "events": [], "delivered": 0, "client_ids": set(),
        "pending": set(), "last_browser": 0.0,
        "deadline": time.time() + s.get("timeout_s", DEFAULT_TIMEOUT),
    }
    for it in s["items"]:
        it["revision"] = 1

    def by_id():
        return {it["id"]: it for it in s["items"]}

    def tick():  # caller holds lock
        if st["status"] == "open" and time.time() > st["deadline"]:
            st["status"] = "expired"
            st["pending"].clear()
            st["events"].append({"type": "timeout", "selected_ids": [], "selection_mode": s["mode"],
                                 "timestamp": now_iso()})

    def public_item(it):
        pub = {k: v for k, v in it.items() if k != "content"}
        if it["type"] == "image" and not it["content"].startswith(URL_PREFIXES):
            pub["src"] = f"/file/{it['id']}?r={it['revision']}&t={token}"
        elif it["type"] == "image":
            pub["src"] = it["content"]
        else:
            pub["content"] = it["content"]
        return pub

    def record_event(body):  # caller holds lock; returns error string or None
        action, ids = body.get("action"), body.get("selected_ids")
        items = by_id()
        if action not in ("submit", "regenerate"):
            return "action must be submit or regenerate"
        if not isinstance(ids, list) or not all(i in items for i in ids) or len(set(ids)) != len(ids):
            return "selected_ids must be unique ids of shown items"
        if st["status"] != "open":
            return f"session is {st['status']}"
        if st["pending"]:
            return "regeneration in progress"
        if action == "submit" and s["mode"] == "single" and len(ids) != 1:
            return "single mode needs exactly one selection"
        if action == "regenerate" and not ids:
            return "select at least one item to regenerate"
        ev = {"type": action, "selected_ids": ids, "selection_mode": s["mode"], "timestamp": now_iso(),
              "feedback": str(body.get("feedback") or "").strip()}
        if action == "submit":
            ev["selected"] = [items[i] for i in ids]
            st["status"] = "submitted"
            st["deadline"] = time.time()  # starts the reaper's 10-minute idle clock
        else:
            ev["regenerate"] = True
            groups = {}
            for i in ids:
                groups.setdefault(items[i]["generation_task_id"], []).append(i)
            ev["tasks"] = [{"generation_task_id": t, "task": s["tasks"][t], "item_ids": v} for t, v in groups.items()]
            st["pending"] = set(ids)
        st["events"].append(ev)
        return None

    class H(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            pass

        def send(self, code, body, ctype="application/json"):
            data = body if isinstance(body, bytes) else json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(data)

        def guard(self, url):
            # Host check blocks DNS rebinding; the token (custom header forces a CORS
            # preflight we never answer) blocks other local pages from driving the API.
            host = self.headers.get("Host", "")
            if host not in (f"127.0.0.1:{port}", f"localhost:{port}"):
                self.send(403, {"error": "bad host"})
                return False
            if url.path == "/":
                return True
            got = self.headers.get("X-Select-Token") or parse_qs(url.query).get("t", [""])[0]
            if not secrets.compare_digest(got, token):
                self.send(401, {"error": "bad token"})
                return False
            return True

        def body(self):
            n = int(self.headers.get("Content-Length") or 0)
            try:
                return json.loads(self.rfile.read(n) or b"{}")
            except ValueError:
                return None

        def do_GET(self):
            url = urlparse(self.path)
            if not self.guard(url):
                return
            if url.path == "/":
                return self.send(200, (HERE / "ui.html").read_bytes(), "text/html; charset=utf-8")
            path = None
            with lock:
                tick()
                if url.path == "/api/health":
                    return self.send(200, {"ok": True, "session_id": s["session_id"]})
                if url.path == "/api/session":
                    st["last_browser"] = time.time()
                    return self.send(200, {
                        "session_id": s["session_id"], "title": s.get("title", "Choose an option"),
                        "description": s.get("description", ""), "mode": s["mode"],
                        "items": [public_item(it) for it in s["items"]], "version": st["version"],
                        "status": st["status"], "pending": sorted(st["pending"]),
                        "remaining_s": max(0, int(st["deadline"] - time.time())),
                    })
                if url.path == "/api/next":
                    ev = None
                    if st["delivered"] < len(st["events"]) and "peek" not in url.query:
                        ev = st["events"][st["delivered"]]
                        st["delivered"] += 1
                    return self.send(200, {
                        "event": ev, "status": st["status"],
                        "browser_connected": time.time() - st["last_browser"] < BROWSER_STALE,
                        "remaining_s": max(0, int(st["deadline"] - time.time())),
                    })
                if url.path.startswith("/file/"):
                    it = by_id().get(url.path[6:])
                    if not it or it["type"] != "image" or it["content"].startswith(URL_PREFIXES):
                        return self.send(404, {"error": "no such image"})
                    path = Path(it["content"])
            if path:
                try:
                    data = path.read_bytes()
                except OSError:
                    return self.send(404, {"error": "image file vanished"})
                return self.send(200, data, mimetypes.guess_type(path.name)[0] or "application/octet-stream")
            self.send(404, {"error": "not found"})

        def do_POST(self):
            url = urlparse(self.path)
            if not self.guard(url):
                return
            body = self.body()
            if not isinstance(body, dict):
                return self.send(400, {"error": "body must be a JSON object"})
            if url.path == "/api/shutdown":
                self.send(200, {"ok": True})
                return threading.Thread(target=server.shutdown).start()
            with lock:
                tick()
                if url.path == "/api/event":
                    cid = body.get("client_event_id")
                    if cid and cid in st["client_ids"]:  # browser retry after a dropped response
                        return self.send(200, {"ok": True, "duplicate": True, "status": st["status"]})
                    err = record_event(body)
                    if err:
                        return self.send(409, {"error": err, "status": st["status"]})
                    if cid:
                        st["client_ids"].add(cid)
                    return self.send(200, {"ok": True, "status": st["status"]})
                if url.path == "/api/items":
                    tasks = {**s["tasks"], **(body.get("tasks") or {})}
                    new = body.get("items") or []
                    errs = check_tasks(tasks) or check_items(new, Path(body.get("base", ".")), tasks)
                    if errs:
                        return self.send(400, {"error": "invalid_items", "details": errs})
                    current = by_id()
                    for it in new:
                        old = current.get(it["id"])
                        it["revision"] = old["revision"] + 1 if old else 1
                        if old:
                            s["items"][s["items"].index(old)] = it
                        else:
                            s["items"].append(it)
                    s["tasks"] = tasks
                    for k in ("title", "description"):
                        if k in body:
                            s[k] = body[k]
                    st["pending"] -= {it["id"] for it in new}
                    st["status"] = "open"
                    st["deadline"] = time.time() + s.get("timeout_s", DEFAULT_TIMEOUT)
                    st["version"] += 1
                    SAVED.write_text(json.dumps(s, indent=2))
                    return self.send(200, {"ok": True, "version": st["version"], "pending": sorted(st["pending"])})
            self.send(404, {"error": "not found"})

    httpd, port = None, args.port
    for p in [*range(args.port, args.port + PORT_TRIES), 0]:
        try:
            httpd = ThreadingHTTPServer(("127.0.0.1", p), H)
            break
        except OSError:
            continue
    if httpd is None:
        sys.exit("could not bind any port")
    port = httpd.server_address[1]
    server = httpd

    def reaper():  # don't outlive an abandoned session forever
        while True:
            time.sleep(30)
            with lock:
                tick()
                idle = st["status"] != "open" and time.time() > st["deadline"] + 600
            if idle:
                return server.shutdown()

    threading.Thread(target=reaper, daemon=True).start()
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps({"port": port, "pid": os.getpid(), "token": token, "session_id": s["session_id"],
                               "url": f"http://127.0.0.1:{port}/?t={token}", "requested_port": args.port}))
    tmp.chmod(0o600)
    tmp.replace(STATE)
    server.serve_forever()
    if json.loads(STATE.read_text()).get("pid") == os.getpid():
        STATE.unlink(missing_ok=True)


# ---------------------------------------------------------------- client

def state():
    try:
        return json.loads(STATE.read_text())
    except (OSError, ValueError):
        return None


def call(st, path, body=None, timeout=5):
    req = Request(f"http://127.0.0.1:{st['port']}{path}", method="POST" if body is not None else "GET",
                  data=json.dumps(body).encode() if body is not None else None,
                  headers={"X-Select-Token": st["token"], "Content-Type": "application/json"})
    try:
        with urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except HTTPError as e:
        return json.loads(e.read() or b"{}") | {"http_status": e.code}


def alive(st):
    try:
        return bool(st and call(st, "/api/health", timeout=2).get("ok"))
    except (URLError, OSError, ValueError):
        return False


def stop_server(st):
    try:
        call(st, "/api/shutdown", {}, timeout=2)
    except (URLError, OSError, ValueError):
        try:
            os.kill(st["pid"], signal.SIGTERM)
        except (OSError, KeyError):
            pass
    STATE.unlink(missing_ok=True)


def cmd_start(a):
    s, base = load_json(a.session)
    if a.timeout:
        s["timeout_s"] = a.timeout
    errs = check_session(s, base)
    if errs:
        out({"ok": False, "error": "invalid_session", "details": errs,
             "_instructions": "Fix every listed problem in the session file and rerun start."}, 1)
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    old = state()
    if old and alive(old):
        stop_server(old)
    s["session_id"] = f"s{int(time.time())}-{secrets.token_hex(2)}"
    SAVED.write_text(json.dumps(s, indent=2))
    STATE.unlink(missing_ok=True)
    env = {**os.environ, "CLAUDE_SELECT_TOKEN": secrets.token_urlsafe(18)}
    with open(LOG, "w") as log:
        proc = subprocess.Popen([sys.executable, __file__, "serve", "--port", str(a.port)], env=env,
                                stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
    for _ in range(50):
        time.sleep(0.1)
        st = state()
        if st and st.get("session_id") == s["session_id"] and alive(st):
            break
        if proc.poll() is not None:
            st = None
            break
    else:
        st = None
    if not st:
        proc.kill()
        out({"ok": False, "error": "server_start_failed", "log": LOG.read_text()[-2000:],
             "_instructions": "Server did not come up. Read the log; retry with --port <free port>. "
                              "If it keeps failing, fall back to presenting the options in chat."}, 1)
    opened = False
    if not a.no_open:
        try:
            opened = webbrowser.open(st["url"])
        except webbrowser.Error:
            pass
    out({"ok": True, "url": st["url"], "port": st["port"], "port_fallback": st["port"] != a.port,
         "session_id": s["session_id"], "mode": s["mode"], "items": len(s["items"]),
         "browser_opened": opened, "timeout_s": s.get("timeout_s", DEFAULT_TIMEOUT),
         "_instructions": ("Give the user the url in one line" + ("" if opened else " (browser did not open)") +
                           f", then run `{SELF} poll` in the background and wait for its result.")})


INSTRUCTIONS = {
    "submit": "User chose selected_ids; their full items are in `selected`. Continue the task with them "
              "(apply `feedback` if any), then run stop — or update to start another round and poll again.",
    "submit_empty": "User submitted with nothing selected: none of the options work. Read `feedback`, ask in chat "
                    "or generate fresh options, then run update FILE and poll again (or stop).",
    "regenerate": "Re-run each entry in `tasks` once per item in its item_ids (apply `feedback`). Write "
                  "{\"items\": [...]} reusing the SAME ids to replace the cards in place, run update FILE, "
                  "then poll again. The cards show 'Regenerating' until update lands.",
    "timeout": "User did not decide before the session timed out. Ask in chat whether they still want to "
               "choose: update FILE reopens the page, stop closes it.",
}


def cmd_poll(a):
    st = state()
    if not st:
        out({"type": "error", "error": "no_server", "_instructions": "No server running. Run start SESSION.json."}, 1)
    deadline = time.time() + a.timeout if a.timeout else float("inf")
    failures, r = 0, {}
    while True:
        try:
            r = call(st, "/api/next")
            failures = 0
        except (URLError, OSError, ValueError):
            failures += 1
            if failures >= 3:
                out({"type": "error", "error": "server_unreachable", "_instructions":
                     f"The server died. Restart it with `{SELF} start {SAVED}` (it holds the latest items), "
                     "give the user the new url, and poll again."}, 1)
            r = {}
        ev = r.get("event")
        if ev:
            key = "submit_empty" if ev["type"] == "submit" and not ev["selected_ids"] else ev["type"]
            out({**ev, "_instructions": INSTRUCTIONS[key]})
        if r.get("status") == "expired" or time.time() >= deadline:
            break
        time.sleep(POLL_INTERVAL)
    tab = "" if r.get("browser_connected") else f" The page is not open; give the user the url again: {st['url']}"
    if r.get("status") == "expired":
        out({"type": "timeout", "selected_ids": [], "timestamp": now_iso(), "_instructions": INSTRUCTIONS["timeout"]})
    out({"type": "waiting", "status": r.get("status"), "browser_connected": r.get("browser_connected", False),
         "remaining_s": r.get("remaining_s"), "_instructions": "No decision yet. Run poll again." + tab})


def cmd_update(a):
    st = state()
    if not st or not alive(st):
        out({"ok": False, "error": "no_server", "_instructions": f"Server is down. Merge your new items into {SAVED} "
             "and run start on it."}, 1)
    body, base = load_json(a.items)
    if isinstance(body, list):
        body = {"items": body}
    r = call(st, "/api/items", {**body, "base": str(base)})
    if not r.get("ok"):
        out({"ok": False, **r, "_instructions": "Fix the listed problems and rerun update."}, 1)
    out({**r, "_instructions": f"Page refreshes within 2 s. Run `{SELF} poll` in the background again."})


def cmd_status(_):
    st = state()
    if not st or not alive(st):
        out({"running": False, "_instructions": "No server. Run start SESSION.json to open one."})
    out({"running": True, "url": st["url"], "port": st["port"], "session_id": st["session_id"],
         **{k: v for k, v in call(st, "/api/next?peek=1").items() if k != "event"},
         "_instructions": "Do not call status in a loop; use poll."})


def cmd_stop(_):
    st = state()
    if st:
        stop_server(st)
    out({"ok": True, "stopped": bool(st), "_instructions": "Done. Tell the user they can close the tab."})


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("start")
    p.add_argument("session")
    p.add_argument("--port", type=int, default=DEFAULT_PORT)
    p.add_argument("--timeout", type=int, help="seconds before the session expires (overrides timeout_s)")
    p.add_argument("--no-open", action="store_true", help="don't open a browser tab")
    p = sub.add_parser("poll")
    p.add_argument("--timeout", type=int, default=0, help="return 'waiting' after S seconds (0 = until an event)")
    p = sub.add_parser("update")
    p.add_argument("items")
    sub.add_parser("status")
    sub.add_parser("stop")
    p = sub.add_parser("serve")
    p.add_argument("--port", type=int, default=DEFAULT_PORT)
    a = ap.parse_args()
    {"start": cmd_start, "poll": cmd_poll, "update": cmd_update, "status": cmd_status,
     "stop": cmd_stop, "serve": serve}[a.cmd](a)


if __name__ == "__main__":
    main()
