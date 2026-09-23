# Security policy

## Reporting a vulnerability

Email **tom at proksiabel dot ee**, or open a private report via GitHub's
**Report a vulnerability** flow (Security → Advisories). Please do not open a
public issue for security-relevant bugs. We aim to acknowledge reports within
72 hours.

## What the server exposes

`picker.py` starts an HTTP server for the selection page. By design:

- It binds **127.0.0.1 only**, never `0.0.0.0`. Other machines cannot reach it
  unless you forward the port yourself (e.g. `ssh -L`).
- Every API call needs a **random per-session token** (in the URL the agent
  gives you, sent back as the `X-Select-Token` header). Because the header is
  custom, cross-origin requests from other local pages need a CORS preflight
  that the server never approves.
- Requests whose `Host` header is not `127.0.0.1:<port>` or `localhost:<port>`
  are refused (**DNS-rebinding guard**).
- It only serves image files that the agent listed in the session. There is no
  path parameter and no directory listing.
- The token and port are stored in `~/.cache/claude-select/server.json` with
  mode `600`.

## Trust boundary

Item content comes from the agent and is rendered with `textContent` and `<img src>`
only, never as HTML, so generated text cannot inject script into the page.
User feedback typed on the page goes back to the agent as **data**. The agent
should treat it like any other user message, not as a tool command.

Anyone who can read the URL (shell history, screen share) can make a selection
for as long as the session is open. Run `picker.py stop` when you are done, or
rely on the session timeout (default 30 minutes).
