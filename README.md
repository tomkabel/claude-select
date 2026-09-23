# claude-select

A skill for Claude Code and Hermes Agent. When the agent has generated
alternatives (images, taglines, drafts, anything), it shows them on a
localhost page. You pick one winner or several, leave a note, or send specific
ones back for regeneration. Your choice goes back to the agent as JSON.

- Stdlib Python ≥ 3.10 and one HTML file. No dependencies, no build step.
- Single-winner (radio) and multi-winner (checkbox, 0..N) modes, with Select all and Clear.
- Images (png/jpg/svg/webp/gif, local path or URL) with click-to-zoom, short
  text (≤ 500 chars), and long text (scrollable, expandable) can be mixed in one session.
- Regenerate selected: the agent gets the original task back, re-runs it, and
  the cards are replaced in place (`v2`, `v3`…).
- Handles port fallback, reconnect/retry, empty submissions, timeouts and crash recovery.

```
claude-select/
├── .claude-plugin/
│   ├── plugin.json           Claude Code plugin manifest
│   └── marketplace.json      lets this repo be added as a marketplace
├── skills/claude-select/     ← the skill (same folder for both platforms)
│   ├── SKILL.md              agent instructions + frontmatter for both harnesses
│   ├── references/protocol.md  JSON schemas, endpoints, enforcement rules
│   └── scripts/
│       ├── picker.py         CLI + HTTP server
│       ├── ui.html           the page
│       └── test_picker.py    end-to-end self-check
└── examples/                 session files for the scenarios below
```

## Install

### Claude Code

As a plugin (recommended):

```bash
claude plugin marketplace add /path/to/claude-select
claude plugin install claude-select@claude-select
```

Or as a bare skill: `ln -s /path/to/claude-select/skills/claude-select ~/.claude/skills/claude-select`
(or into `<project>/.claude/skills/` for a single project).

Restart Claude Code. The skill loads automatically when you ask for options to
choose from, or you can invoke it with `/claude-select`.

### Hermes Agent

Global install:

```bash
cp -r skills/claude-select "${HERMES_HOME:-$HOME/.hermes}/skills/"
```

Or point Hermes at this checkout without copying (`~/.hermes/config.yaml`):

```yaml
skills:
  external_dirs:
    - /path/to/claude-select/skills
```

Project-local installs (`<project>/.hermes/skills/claude-select`) need a one-time
`hermes skills trust` from the project root. Hermes has no plugin manifest, so the
SKILL.md frontmatter is the registration. It uses `name`, `description`,
`version`, `platforms` and `metadata.hermes.tags`.

### Permissions

| What | Why | Where it is declared |
|---|---|---|
| Run `python3 …/picker.py` | the only command the skill runs | `allowed-tools: Bash(python3 *picker.py*)` in SKILL.md (Claude Code pre-approves it) |
| Bind a port on `127.0.0.1` | the page | 8765 by default (`CLAUDE_SELECT_PORT`), then 8766–8774, then any free port. Never binds to `0.0.0.0` |
| Write `~/.cache/claude-select/` | server state, crash-recovery copy, log | override with `CLAUDE_SELECT_HOME` |
| Open a browser tab | convenience | `start --no-open` skips it |

Only your browser can reach the server. Every API call needs a random per-session
token (it is in the URL the agent gives you), and requests with a foreign `Host`
header are rejected to block DNS rebinding.

### Verify

```bash
python3 skills/claude-select/scripts/test_picker.py   # prints "ok"
```

The test starts isolated servers and covers validation, port fallback, auth,
both modes, regenerate and update, retry de-duplication, empty submit and timeout.

## How it works

```
agent ── picker.py start session.json ─▶ server ◀── page polls /api/session every 2 s
agent ◀─ picker.py poll (every 2 s) ──── events ◀── Submit / Regenerate clicks
agent ── picker.py update items.json ──▶ server ──▶ page re-renders changed cards
```

| Command | Output (one JSON object, always with `_instructions`) |
|---|---|
| `start SESSION.json [--port N] [--timeout S] [--no-open]` | `url`, `port`, `port_fallback`, `session_id`, `browser_opened` |
| `poll [--timeout S]` | the next event: `submit` / `regenerate` / `timeout`, or `waiting` / `error` |
| `update ITEMS.json` | `version`, ids still `pending` |
| `status` | running?, url, status, `browser_connected`, `remaining_s` |
| `stop` | `stopped` |

The agent runs `poll` as a background task: `run_in_background` on Claude Code,
`background` with `notify_on_complete` on Hermes. It is woken when you click. See
[references/protocol.md](skills/claude-select/references/protocol.md) for the full
session, item and event JSON schemas and the HTTP endpoints.

### Session file (agent → page)

```json
{
  "title": "Pick a tagline", "mode": "single", "timeout_s": 900,
  "tasks": {"tagline": {"prompt": "Hero tagline, max 8 words", "params": {"tone": "warm"}}},
  "items": [
    {"id": "t1", "type": "short_text", "content": "Money that moves at your pace.",
     "metadata": {"label": "Option A"}, "generation_task_id": "tagline"}
  ]
}
```

### Event (page → agent)

```json
{
  "type": "regenerate",
  "selected_ids": ["logo-b"],
  "selection_mode": "multi",
  "timestamp": "2026-09-23T04:55:52Z",
  "feedback": "darker, bolder",
  "regenerate": true,
  "tasks": [{"generation_task_id": "logo", "item_ids": ["logo-b"],
             "task": {"prompt": "Minimal geometric logo mark…", "params": {"size": "240x180"}}}]
}
```

A `submit` event carries `selected` (the full chosen items) in place of `tasks`.

## Example scenarios

The files are in [`examples/`](examples/). Run the commands yourself to see the
flow the agent drives. `P` is `python3 skills/claude-select/scripts/picker.py`.

### 1. Single winner: taglines

```bash
P start examples/single-taglines.json     # tab opens with 3 radio cards
P poll                                    # blocks until you click Submit
```

Pick "Option B" and submit. `poll` prints `"type": "submit"`,
`"selected_ids": ["t2"]`, and `selected[0].content` is
`"Irregular income. Regular calm."`. Submit stays disabled until exactly one card is picked.

### 2. Multiple winners with regeneration: mixed launch assets

```bash
P start examples/multi-mixed.json         # 3 SVG logos, a subtitle, a 141-word post
P poll
```

1. Tick *Mark · square*, type "darker, bolder", then click **Regenerate selected**.
   The card shows "Regenerating…" and all actions lock.
2. `poll` returns a `regenerate` event with the `logo` task and `item_ids: ["logo-b"]`.
   The agent re-runs the prompt with the feedback, writes the new image, and runs:
   ```bash
   P update examples/regenerated-logo-b.json   # same id "logo-b", new image
   P poll
   ```
3. Within 2 seconds the card swaps to the new image with a `v2` badge. Your selection is kept.
4. Tick the circle, the new square and the post, then click **Submit selection**. `poll`
   returns `selected_ids: ["logo-a", "logo-b", "post-1"]`. Then run `P stop`.

In multi mode, submitting with nothing ticked asks for confirmation. The agent then
gets `selected_ids: []`, meaning "none of these". Its instructions say to read the
feedback and generate fresh options.

### 3. What a user says to the agent

> Give me four hero image options for the pricing page and let me pick.

The agent generates the images, writes a session file with one `hero` task, runs
`start`, and sends you the URL in one line. It then polls in the background and
continues once you submit or regenerate.

## Error handling

| Situation | Behaviour |
|---|---|
| Port in use | tries the next 9 ports, then an OS-assigned one; `port_fallback: true` |
| Server fails to start | `server_start_failed` with the log tail; the agent retries on another port, then falls back to chat |
| Invalid session (bad type, text over 500 chars, missing image, unknown task id, duplicate id) | `invalid_session` listing every problem; nothing starts |
| Network drop while submitting | the page shows "Connection lost, retrying…" and resends with the same `client_event_id` until the server answers. The server drops duplicates. |
| Page reload or tab closed | selection is kept in `localStorage`; `poll --timeout` reports `browser_connected: false` so the agent can resend the URL |
| Server crash | `poll` returns `server_unreachable`; `start ~/.cache/claude-select/session.json` restores the latest items, including regenerated ones |
| No decision in time | a `timeout` event after `timeout_s` (default 30 min, and each `update` resets it). The page says so, and `update` reopens the session. |
| Session left open | the server exits on its own 10 minutes after the session closes or expires |
| Clicks during regeneration, after submit, or a wrong count in single mode | the buttons are disabled and the server also rejects them with 409 |

## Design notes: what was taken from impeccable

This is modelled on impeccable 4.3.1's `live` mode (`/impeccable live`), which
also shows AI-generated variants in a browser and relays the pick back:

| impeccable live | claude-select |
|---|---|
| One plugin root with `.claude-plugin/plugin.json` → `"skills": "./skills/"`. The same `skills/impeccable/` folder is shipped to `~/.hermes/skills/` for Hermes. | Same layout: one skill folder serves both harnesses. |
| Launcher at `${CLAUDE_SKILL_DIR}/scripts/impeccable` with JSON output. | `scripts/picker.py` with JSON output. |
| Every tool output carries `_instructions`, the authoritative next step. | Same. The agent never has to reason about protocol state. |
| `live-poll` long poll, run as a Claude Code background task; `--reply` acknowledges. | `poll` run as a background task. No reply is needed because `update` doubles as the acknowledgement for regenerations. |
| Separate small helper server with a token; the page URL comes from your dev server. | The helper server also serves the page, so there is no dev server to depend on. |
| Journal under `.impeccable/live/` replays unacknowledged work. | `session.json` holds the latest items for a restart. |
| Accept / discard / variant params / HMR splicing into source files. | Not needed: options are standalone artifacts, not DOM elements in your app. |

Skipped for now: WebSocket or SSE push. Polling every 2 s is enough for one local
user, and the upgrade path is in protocol.md. Also skipped: several concurrent sessions
(one server, and `start` replaces the old one) and ranking or scoring of options.
