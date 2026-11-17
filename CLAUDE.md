# CLAUDE.md

Guidance for AI assistants working in this repository.

## Language

- **All source code, code comments, commit messages and Markdown documentation
  (README, docs/, etc.) are written in English.** This applies to every change
  in this project, now and in the future.
- The web UI uses English as the source language with a German translation
  catalog (same approach as `pat95st/auto-event-play`).
- Conversation with the project owner may happen in German.

## Project

`event-stage-screen` turns a Raspberry Pi (4 or 5) into a dual-output stage
display for eventrentmanager. The full specification lives in
[`docs/SPEC.md`](docs/SPEC.md); read it before making architectural changes.

## Conventions

- Stack and structure mirror `pat95st/auto-event-play`: Python 3.11+,
  Starlette/uvicorn, Jinja2, Bootstrap 5.3.3 (dark theme), SQLite, CQRS
  (CommandBus/QueryBus), systemd, `deploy/install.sh`.
- Tests: pytest, with a fake eventrentmanager server for API tests.
- License: GPLv3. Never commit proprietary NDI SDK files; the installer
  downloads the NDI runtime only after the user accepts its license.
- Work milestone by milestone (see `docs/SPEC.md`, "Milestones").
