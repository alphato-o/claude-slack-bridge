# Claude ↔ Slack Bridge

A two-way bridge between Claude Code and Slack, run as a Dockerized daemon.

- **Claude → Slack:** the `ask_on_slack` MCP tool — Claude pauses mid-task, posts
  a question to a Slack channel, blocks until you reply in the thread, then resumes.
  Concurrent sessions are routed by `thread_ts`.
- **Slack → Claude:** `@claude-bot` in a channel spawns `claude -p` in the matching
  project directory inside the container. Channel→project routing via `projects.json`;
  git-worktree support via a `[label]` prefix.
- **Full-process plugin:** a turnkey feature-dev workflow driven from Slack
  (`/process start` → pick task → worktree → design → plan → run-plan → PR per step).

## Repo layout

- `src/` — the daemon. Key files:
  - `main.py` — entrypoint, wires up the daemon.
  - `slack_daemon.py` — Slack Socket Mode event loop (handles `@bot` mentions).
  - `mcp_server.py` / `session.py` — the `ask_on_slack` MCP server (run via
    `docker exec … python session.py` from a client project's `.mcp.json`).
  - `session_broker.py` — maps Slack threads ↔ in-flight Claude sessions.
  - `claude_handler.py` — spawns and streams `claude -p`.
  - `config.py` — env + `projects.json` loading. `security.py` — access control.
- `plugin/` — the `/process` full-process plugin (`plugin.json` + `skills/`).
- `docs/` — setup & reference (architecture, slack-setup, github-setup, security,
  slack-to-claude-projects, mcp-client-setup, full-process-plugin).
- `Dockerfile` / `docker-compose.yml` / `entrypoint.sh` — the container.
- `.env.example`, `projects.json.example` — templates; real copies are git-ignored.

## How it runs

- `docker compose up -d --build` starts the `claude-slack-bridge` container.
  Socket Mode → no public URL / inbound ports. `restart: unless-stopped`.
- Config the daemon reads:
  - `.env` — `SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN`, optional `GITHUB_TOKEN`,
    `PROJECTS_DIR`, `LOG_LEVEL`, `SECURITY_*`. **Git-ignored.**
  - `projects.json` — channel-ID → project-path map. **Git-ignored.** Bind-mounted
    live into the container, so edits apply on restart without a rebuild.
- `${PROJECTS_DIR}` is mounted to `/projects`; paths in `projects.json` are
  container paths under `/projects`.
- Tests: `pytest` (config in `pytest.ini`, tests in `tests/`).

## Secrets & local state

Never commit secrets. Git-ignored: `.env`, `projects.json`, `.mcp.json`, `.claude/`,
and the entire **`private/`** folder. Host-specific deployment notes (what's running
here, real channel IDs, token locations, fork/upstream layout) live in
**`private/deployment.md`** — read it for this machine's specifics.

## Fork & upstream

- `origin` → `github.com/alphato-o/claude-slack-bridge` (our fork — push here).
- `upstream` → `github.com/tomeraitz/claude-slack-bridge` (pull updates here).
- Our delta lives on `main`. Sync upstream with
  `git fetch upstream && git merge upstream/main`.

---

# Communication

Once you use `mcp__claude-slack-bridge__ask_on_slack` for the first time in a conversation, ALL further communication with the user must go through that tool. Do not use `AskUserQuestion`, and do not ask questions or request feedback as text in the terminal. Continue communicating exclusively via Slack until the user explicitly tells you to switch back to the terminal.

**Exception — setup/configuration skills:** The following skills run locally inside Claude Code as part of `/process-setup` and must use `AskUserQuestion` (not Slack), even if `ask_on_slack` was already used earlier in the session:

- `build-design-workflow`
- `build-plan-workflow`
- `build-run-plan-flow`
- `build-process-skill`

While executing any of these skills, follow the skill's own instructions for clarifications (local `AskUserQuestion`). Resume the Slack-only rule once the skill returns.
