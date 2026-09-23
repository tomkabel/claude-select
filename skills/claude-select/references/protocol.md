# claude-select protocol reference

Agent ⇄ `picker.py` (CLI, JSON on stdout) ⇄ local HTTP server ⇄ browser page.
The agent never talks HTTP directly; the browser never talks to the agent directly.

```
agent ── start session.json ─▶ server (127.0.0.1:8765…)  ◀── GET /api/session every 2 s ── page
agent ◀─ poll (GET /api/next every 2 s) ─ event queue  ◀── POST /api/event ─────────────── page
agent ── update items.json ─▶ POST /api/items ─▶ version++ ─▶ page re-renders
```

## Session (agent → page) — `start SESSION.json`

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "title": "claude-select session",
  "type": "object",
  "required": ["items", "tasks"],
  "properties": {
    "title": {"type": "string", "default": "Choose an option"},
    "description": {"type": "string"},
    "mode": {"enum": ["single", "multi"], "default": "single"},
    "timeout_s": {"type": "number", "exclusiveMinimum": 0, "default": 1800},
    "tasks": {
      "type": "object",
      "description": "generation_task_id → whatever is needed to re-run that generation",
      "additionalProperties": {
        "type": "object",
        "properties": {"prompt": {"type": "string"}, "params": {"type": "object"}}
      }
    },
    "items": {"type": "array", "minItems": 1, "items": {"$ref": "#/$defs/item"}}
  },
  "$defs": {
    "item": {
      "type": "object",
      "required": ["id", "type", "content", "generation_task_id"],
      "properties": {
        "id": {"type": "string", "minLength": 1, "description": "unique within the session; reuse it to replace the item"},
        "type": {"enum": ["image", "short_text", "long_text"]},
        "content": {"type": "string", "minLength": 1,
                    "description": "image: file path (relative to the JSON file) or http(s)/data:image URL; short_text: ≤ 500 chars; long_text: unlimited"},
        "metadata": {"type": "object", "default": {},
                     "description": "label → card title, alt → image alt text, anything else → shown as key: value"},
        "generation_task_id": {"type": "string", "description": "must be a key of tasks"}
      }
    }
  }
}
```

## Update (agent → page) — `update ITEMS.json`

`{"items": [item, …], "tasks": {…}?, "title"?: …, "description"?: …}` or a bare
item array. Existing id → replaced (revision + 1, pending flag cleared); new id →
appended. Resets the timeout and reopens a submitted/expired session.

## Event (page → agent) — output of `poll`

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "title": "claude-select event",
  "type": "object",
  "required": ["type", "selected_ids", "selection_mode", "timestamp"],
  "properties": {
    "type": {"enum": ["submit", "regenerate", "timeout"]},
    "selected_ids": {"type": "array", "items": {"type": "string"}},
    "selection_mode": {"enum": ["single", "multi"]},
    "timestamp": {"type": "string", "format": "date-time"},
    "feedback": {"type": "string", "description": "free-text note typed by the user; may be empty"},
    "selected": {"type": "array", "description": "submit only: the full chosen items"},
    "regenerate": {"const": true, "description": "regenerate only"},
    "tasks": {
      "type": "array",
      "description": "regenerate only: selected ids grouped by their original task",
      "items": {
        "type": "object",
        "properties": {
          "generation_task_id": {"type": "string"},
          "task": {"type": "object"},
          "item_ids": {"type": "array", "items": {"type": "string"}}
        }
      }
    },
    "_instructions": {"type": "string"}
  }
}
```

`poll` may also print `{"type": "waiting", …}` (only with `--timeout`) or
`{"type": "error", "error": "no_server" | "server_unreachable", …}`.

## Rules the server enforces

| Situation | Result |
|---|---|
| single mode, submit ≠ 1 id | 409, page keeps Submit disabled anyway |
| multi mode, submit 0 ids | allowed after a confirm; agent gets `selected_ids: []` |
| regenerate 0 ids | 409 |
| any action while a regeneration is pending | 409 "regeneration in progress" |
| any action after submit/timeout | 409 until the agent runs `update` |
| same `client_event_id` twice (browser retry) | accepted once, second returns `duplicate: true` |
| request without the session token | 401 |
| `Host` other than `127.0.0.1:PORT` / `localhost:PORT` | 403 (DNS-rebinding guard) |

## HTTP endpoints (internal)

All but `/` need the token as `X-Select-Token` header or `?t=` query.

| Method | Path | Purpose |
|---|---|---|
| GET | `/` | the page (`ui.html`) |
| GET | `/api/session` | items, mode, status, pending ids, version, remaining seconds; also the page heartbeat |
| GET | `/api/next` | pops the next undelivered event (`?peek=1` doesn't pop) |
| GET | `/file/<id>` | serves a local image item |
| POST | `/api/event` | `{client_event_id, action: submit\|regenerate, selected_ids, feedback}` |
| POST | `/api/items` | the `update` body |
| POST | `/api/shutdown` | stop |

## Transport

Both sides poll every 2 s over plain HTTP. For a single local user this beats a
WebSocket on simplicity and survives sleep/wake and flaky tabs for free. To
upgrade later: swap `/api/next` for a Server-Sent Events stream (`text/event-stream`,
still stdlib) and the page's `setTimeout(refresh)` for an `EventSource`. Neither
the event schema nor the CLI would change.

## State and files

`$CLAUDE_SELECT_HOME` (default `~/.cache/claude-select/`):
`server.json` (port, pid, token; mode 600), `session.json` (latest session incl.
regenerated items; `start` on it recovers a crashed server), `server.log`.
One server/session at a time; `start` replaces a running one.
