# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/).

## [0.1.1] - 2026-09-23

### Fixed

- Server startup no longer stalls on macOS: skip `http.server`'s reverse-DNS
  `getfqdn()` lookup on bind, and allow up to 15 s for a cold start.

## [0.1.0] - 2026-09-23

### Added

- `picker.py` CLI (`start`, `poll`, `update`, `status`, `stop`) and a localhost
  server, standard library only, Python ≥ 3.10.
- Selection page: single-winner (radio) and multi-winner (checkbox, 0..N)
  modes, Select all / Clear, optional feedback note, light and dark themes.
- Content types: images (png/jpg/svg/webp/gif, path or URL, click to zoom),
  short text (≤ 500 chars), long text (scrollable and expandable), mixed freely.
- Regenerate selected: returns the original generation tasks to the agent;
  `update` replaces cards in place with a revision badge.
- Port fallback, retries de-duplicated by client event id, empty-submit
  confirmation, session timeout, crash recovery from `session.json`, and
  an idle shutdown for the server.
- Claude Code plugin and marketplace manifests. The Hermes Agent skill uses the
  same folder.
- End-to-end self-check and CI on Linux, macOS and Windows.

[0.1.1]: https://github.com/tomkabel/claude-select/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/tomkabel/claude-select/releases/tag/v0.1.0
