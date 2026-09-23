---
name: claude-select
description: Show the user several generated options (images, short text, long text, or a mix) on a localhost web page so they can pick one or several winners, leave a note, or ask to regenerate specific ones. Use when you produced alternatives the user should choose between visually instead of in chat, e.g. "give me 4 logo options", "draft 3 intros and let me pick", "which headline", "shortlist these", "regenerate the ones I don't like".
version: 0.1.0
license: MIT
platforms: [linux, macos, windows]
allowed-tools: Bash(python3 *picker.py*)
compatibility: Python >= 3.10, stdlib only. Binds 127.0.0.1 (port 8765, falls back to the next 9, then any free port). Works in Claude Code and Hermes Agent.
metadata:
  hermes:
    tags: [Selection, UI, Images, Writing, Feedback]
    related_skills: [decision-picker]
---

# claude-select

Generate options, show them on a local page, wait for the user's choice, act on it.
The server and page are `scripts/picker.py` + `scripts/ui.html`. Every command
prints one JSON object whose `_instructions` field is the authoritative next step.

`PICKER` below means `python3 "<skill-dir>/scripts/picker.py"`, where `<skill-dir>`
is the directory holding this file: `${CLAUDE_SKILL_DIR}` on Claude Code,
`${HERMES_SKILL_DIR}` (or the `[Skill directory: …]` line) on Hermes.

## When not to use

- 2–4 short options with no visual component: ask in chat (`AskUserQuestion` /
  `clarify`) — a browser tab is overkill.
- Headless/remote sessions where the user cannot open `127.0.0.1` on this machine.
  Tell them to forward the port (`ssh -L 8765:127.0.0.1:8765 host`) or fall back to chat.

## 1. Write the session file

Put it in the project or a temp dir. Image paths resolve relative to the file.

```json
{
  "title": "Pick a hero image",
  "description": "Optional one-liner shown under the title.",
  "mode": "single",
  "timeout_s": 1800,
  "tasks": {
    "hero": {"prompt": "the exact prompt/instructions you used", "params": {"size": "1024x768"}}
  },
  "items": [
    {"id": "h1", "type": "image", "content": "out/hero-1.png",
     "metadata": {"label": "Warm", "alt": "Sunlit desk"}, "generation_task_id": "hero"}
  ]
}
```

- `mode`: `single` (exactly one winner, radio) or `multi` (0..N, checkboxes).
- `type`: `image` (local path, `http(s)://` or `data:image/` URL; png/jpg/svg/webp/gif),
  `short_text` (≤ 500 chars), `long_text` (any length, scrollable/expandable).
  Mix freely.
- `tasks`: one entry per distinct generation job. Record what you'd need to re-run
  it — the regenerate event hands this back verbatim.
- `metadata.label` is the card title; `metadata.alt` the image alt text; other keys
  are shown as small facts on the card.

Full schemas: [references/protocol.md](references/protocol.md).

## 2. Start, share the URL, poll

```bash
PICKER start session.json          # opens a browser tab; --no-open to skip
```

Send the returned `url` to the user in one line. Then run `PICKER poll` **as a
background task** and wait for it to finish:

- Claude Code: Bash with `run_in_background: true`; you are notified on exit.
- Hermes: terminal with `background: true, notify_on_complete: true`.
- Other harnesses: foreground `PICKER poll --timeout 540`, rerun on `waiting`.

`poll` checks every 2 s and exits with the first user event. Don't loop on `status`.

## 3. Handle the event

| `type` | Do this |
|---|---|
| `submit` with ids | Use `selected` (full items) and `feedback`. Continue the task. `PICKER stop` when done. |
| `submit` with `selected_ids: []` | None of the options worked. Read `feedback`; make new options (step 4) or ask in chat. |
| `regenerate` | For each `tasks[]` entry, re-run `task` once per id in `item_ids`, applying `feedback`. Then step 4. |
| `timeout` | The user did not decide in time. Ask in chat; `update` reopens, `stop` closes. |
| `waiting` | Only with `--timeout`. Poll again; if `browser_connected` is false, resend the url. |
| `error` | Follow `_instructions` (restart from the saved session, or fall back to chat). |

## 4. Push new or regenerated items

```bash
PICKER update items.json   # {"items": [...], "tasks": {...optional new tasks}}
```

Same `id` → the card is replaced in place (revision badge `v2`, `v3`…). New id →
appended. `update` also reopens a submitted or timed-out session. Then poll again.

## Rules

- Regenerate **only** the ids you were given, keep their ids, keep their
  `generation_task_id` unless the task itself changed.
- Don't narrate the protocol to the user. One line with the url; after the event,
  just continue the work.
- On `server_start_failed`, retry once with `--port <other>`, then present options in chat.
- Always `PICKER stop` at the end of the job (the server also exits on its own 10
  minutes after the session closes).
