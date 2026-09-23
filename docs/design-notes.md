# Design notes

## Prior art: impeccable `live` mode

claude-select is modelled on [impeccable](https://github.com/pbakaus/impeccable)
4.3.1's `/impeccable live`, which also shows AI-generated variants in a browser
and relays the pick back to the agent. What carried over, and what didn't:

| impeccable live | claude-select |
|---|---|
| One plugin root: `.claude-plugin/plugin.json` → `"skills": "./skills/"`; the same `skills/impeccable/` folder is copied to `~/.hermes/skills/` for Hermes | Same layout: one skill folder serves both harnesses |
| Launcher at `${CLAUDE_SKILL_DIR}/scripts/impeccable`, JSON output | `scripts/picker.py`, JSON output |
| Every tool output carries `_instructions`, the authoritative next step | Same: the agent never has to reason about protocol state |
| `live-poll` long poll, run as a Claude Code background task; `--reply` acknowledges | `poll` as a background task; `update` doubles as the acknowledgement for regenerations |
| Separate helper server with a token; the page comes from your dev server | The helper also serves the page, so there is no dev server to depend on |
| Append-only journal under `.impeccable/live/` replays unacknowledged work | `session.json` holds the latest items; `start` on it recovers a crash |
| Accept / discard / variant params / HMR splicing into source files | Not needed: options are standalone artifacts, not DOM elements in your app |

## Decisions

**HTTP polling every 2 s, not WebSockets.** One local user, one tab. Polling
survives laptop sleep, flaky tabs and server restarts with no reconnect logic,
and it needs nothing outside the Python standard library. The upgrade path is
Server-Sent Events on `/api/next` plus an `EventSource` in the page, with no
change to the event schema or the CLI. See [protocol.md](../skills/claude-select/references/protocol.md#transport).

**The agent never speaks HTTP.** Harness shells are the one tool every agent
has. A CLI that prints one JSON object per call works the same in Claude Code,
Hermes, Codex or a plain terminal. The HTTP API is an internal detail between
`picker.py` and the page.

**`_instructions` in every response.** Borrowed from impeccable. Protocol state
(pending regenerations, timeouts, dead servers) is resolved by the tool, not
re-derived by the model from a long SKILL.md.

**Regeneration replaces in place.** The agent re-emits items with the *same*
id. The card keeps its position and the user's selection, and gains a revision
badge. The alternative (new ids) loses the user's place and makes "which one
did I send back?" ambiguous.

**Tasks are stored by the caller, verbatim.** The server does not interpret
`tasks`; it hands the entry back on regenerate. Any generator (image model,
LLM prompt, script) fits without schema changes.

**One session at a time.** `start` replaces the running server. Concurrent
sessions would need session routing in every command for a case nobody has
asked for yet.

**Security posture.** Binds 127.0.0.1 only; a random per-session token is
required on every API call (a custom header forces a CORS preflight the server
never answers, so other local pages can't drive it); requests with a foreign
`Host` header are refused to block DNS rebinding.
