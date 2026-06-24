# Claude ↔ Slack Bridge — a self-hosted Claude Tag

> On **June 23, 2026** Anthropic launched [**Claude Tag**](https://www.anthropic.com/news/introducing-claude-tag): Claude as a Slack teammate you `@mention` to delegate work — **one shared Claude per channel** that remembers context, picks up where anyone left off, and runs on Opus 4.8. It's a paid Team/Enterprise, Anthropic-hosted product.
>
> **This is the open-source, self-hosted equivalent.** `@mention` Claude in a Slack channel and it works in your *real* repo, **streams its work live** (tokens, tool calls, a live plan, a "thinking" status), keeps **one shared brain per channel**, **remembers across tasks** (and shares that memory with your *terminal* Claude Code), and routes long-running results back to the right thread. It runs on **your** box, on **your** Claude Code subscription, with **your** tools and full repo access — no Enterprise plan, no data leaving your infra.

![slack-claude-small](https://github.com/user-attachments/assets/d4460f40-5c68-48a0-8fc5-9b386881a765)

## Claude Tag vs this bridge

| | **Claude Tag** (Anthropic) | **This bridge** (self-hosted) |
|---|---|---|
| `@mention` Claude in Slack to delegate work | ✅ | ✅ |
| One shared Claude per channel; pick up where anyone left off | ✅ | ✅ — one continuous session per channel |
| Watch it work live (streaming, tool/plan widgets, "thinking" status) | ✅ | ✅ — native Slack token streaming |
| Remembers context from its channels | ✅ | ✅ — persistent per-project memory + journal |
| Shares memory with your local **Claude Code** terminal | — | ✅ — one unified memory store, both directions |
| Runs on **Opus 4.8** | ✅ | ✅ — your Claude Code CLI |
| Works in your real repo & runs your CLI tools | scoped | ✅ — full access to the mapped project dir |
| Interrupt / redirect mid-task | ✅ | ✅ — soft (queue) + hard (`!`/`停`) interrupts |
| Hosting | Anthropic cloud | **your infra** (Docker + Slack Socket Mode) |
| Plan required | Team / Enterprise (paid, per-seat) | **none** — your existing Claude subscription |
| Where your data lives | Anthropic-managed | **on your machine** |

> Same idea, shipped the same week — but open, free, and yours. ⭐ it if that's your kind of thing.

## What you get

- **Tag Claude in any channel** — `@claude-bot fix the login redirect bug`. It spawns `claude -p` in the matching project directory and works with full repo context. Route each channel to a project (or git worktree) via `projects.json`.
- **Live, streamed work** — native Slack streaming shows Claude's tokens, tool calls (`📖 Reading…`, `⚡ Running tests`), a live `Task`/`TodoWrite` plan, and an animated "thinking" status — not just a final answer.
- **One brain per channel, with memory** — every `@mention`/reply continues the same session, so a new task inherits the last one's context; a durable journal + file-based memory persist across restarts. That memory is **bind-mounted to share with your terminal Claude Code** — write a note either side, both see it.
- **Interrupt like a CLI** — type more mid-task to **queue** it for the next turn, or send `!` / `停` / `stop` to **hard-interrupt** and redirect. Long jobs that outlive a turn report their results **back to the originating thread**.
- **`ask_on_slack` (Claude → Slack)** — the reverse direction: your terminal Claude pauses mid-task, asks a question in Slack, blocks until you reply in the thread, then resumes. Concurrent sessions routed by `thread_ts`.
- **Full-process plugin** — a turnkey feature-dev workflow from Slack (`/process start` → pick a task → worktree → design → plan → run-plan → PR per step).
- **Scoped & private** — per-user / per-channel access control, Slack tokens stripped from the model's environment, Socket Mode (no public URL or inbound firewall rules).

```
Slack @bot   ──────────────────▶  claude -p (in project dir) ──stream──▶  live thread
Claude Code  ──ask_on_slack──▶  Slack channel  ──your reply──▶  Claude Code resumes
```

---

## Real-world use cases

Patterns already running in production (details neutralized):

> **🛒 E-commerce ops, away from the desk.** Someone drops `@claude assess the fraud risk on this order <link>` in the channel; it reads the order, the customer history, and the team's risk playbook, then gives a ship/hold call right in the thread — no laptop needed. Same channel handles "how did order volume this week compare to last?" by pulling live store data.

> **🤖 Closing the loop on a support bot.** A team maintains a customer-support assistant (RAG over their docs). When it answers something wrong, they just say `@claude the bot quoted the wrong price — learn the real numbers from our site, fix the prompt, run the regression battery, and ship it`. It edits the prompt, runs ~100 test cases, deploys to staging, and posts the pass/fail tally back to the thread — picking the task up across several messages without losing context.

> **📈 A 24/7 autopilot that reports to you.** An unattended campaign loop runs around the clock and mirrors each cycle's outcome to Slack — what it did, what landed — so nobody babysits a terminal. The team `@mention`s to retune the strategy and it folds the change into the next cycle.

> **🛠️ Software it builds itself.** Maintainers develop projects from the project's own Slack channel — tag it with a change, watch it stream the edits and run the test suite live, and it reports back when the long run finishes. (This very bridge is developed that way.)

What ties these together: **one shared Claude per channel** that anyone can hand off to, that **remembers** the project between tasks, **streams** what it's doing, and brings **long-running results home** to the right thread.

---

## Quickstart — Claude → Slack

### 1. Create a Slack app and get tokens

Follow [docs/slack-setup.md](docs/slack-setup.md) to create a Slack app, get your `xoxb-` and `xapp-` tokens, and invite the bot to a channel.

*(Optional)* If you plan to use the `/process` workflow — which opens GitHub PRs and reads review comments from inside the container — also follow [docs/github-setup.md](docs/github-setup.md) to create a fine-grained PAT and set `GITHUB_TOKEN`. Skip this if you don't need GitHub integration.

### 2. Clone, configure, and start the daemon

```bash
git clone https://github.com/alphato-o/claude-slack-bridge.git
cd claude-slack-bridge
cp .env.example .env   # fill in SLACK_BOT_TOKEN and SLACK_APP_TOKEN
docker compose up -d --build
```

The container starts automatically on system boot (`restart: unless-stopped`) and uses Socket Mode — no public URL or inbound firewall rules needed.

**You only do this once.** The daemon stays running in the background and serves all your Claude Code projects.

### 3. Add `.mcp.json` to your Claude Code project

Create `.mcp.json` in the root of any project where you want Claude to be able to ask you questions:

```json
{
  "mcpServers": {
    "claude-slack-bridge": {
      "command": "docker",
      "args": [
        "exec", "-i",
        "-e", "SLACK_CHANNEL",
        "claude-slack-bridge",
        "python", "session.py"
      ],
      "env": {
        "SLACK_CHANNEL": "#your-project-channel"
      }
    }
  }
}
```

> **Important:** Add `.mcp.json` to your `.gitignore` — it contains your channel name and is project-specific.

### 4. Add the Slack communication rule to your `CLAUDE.md`

To make Claude automatically use Slack for all communication once it sends its first message, add the following to your project's `CLAUDE.md`:

```markdown
Once you use `mcp__claude-slack-bridge__ask_on_slack` for the first time in a conversation, ALL further communication with the user must go through that tool. Do not use `AskUserQuestion`, and do not ask questions or request feedback as text in the terminal. Continue communicating exclusively via Slack until the user explicitly tells you to switch back to the terminal.
```

Open the project in Claude Code and Claude will have access to `ask_on_slack`. Reply **in the Slack thread** (not the channel directly) and Claude resumes from where it left off.

---

## Quickstart — Slack → Claude

Tag the bot in a Slack channel and Claude runs inside the matching project directory.

### 1. Set `PROJECTS_DIR` in `.env`

```
PROJECTS_DIR=C:\Users\you\projects
```

This is the parent directory that contains all your projects. It's mounted into the container at `/projects/`.

### 2. Create `projects.json`

Map each Slack channel to a project folder:

```json
{
  "#my-project-channel": "/projects/my-project"
}
```

### 3. Rebuild

```bash
docker compose up -d --build
```

Then in Slack:

```
@claude-bot fix the login redirect bug
```

The bot replies in a thread. Continue the conversation by replying in that thread.

→ Full reference (channel formats, `plugin_dir`, worktrees, routing rules): **[docs/slack-to-claude-projects.md](docs/slack-to-claude-projects.md)**

---

## The full-process plugin

A turnkey feature-development workflow driven entirely from Slack. After a one-time `/process-setup` in your repo, you can start a feature from Slack with:

```
@claude-bot /process start
```

The bot lists your open tasks (from Notion, Linear, Jira, …), creates a git worktree for the one you pick, walks the work through your configured steps (typically **design → plan → run-plan**), opens a GitHub PR after each step, and waits for your approval in Slack before moving on.

→ Full guide: **[docs/full-process-plugin.md](docs/full-process-plugin.md)**

---

## Next steps

| Want to... | See |
|---|---|
| Tag the bot from Slack | [docs/slack-to-claude-projects.md#how-it-works](docs/slack-to-claude-projects.md#how-it-works) |
| Route channels to projects | [docs/slack-to-claude-projects.md#projectsjson--channel--project-routing](docs/slack-to-claude-projects.md#projectsjson--channel--project-routing) |
| Use git worktrees from Slack | [docs/slack-to-claude-projects.md#worktrees](docs/slack-to-claude-projects.md#worktrees) |
| Run the turnkey feature-dev workflow from Slack | [docs/full-process-plugin.md](docs/full-process-plugin.md) |
| Share memory between Slack-Claude and terminal-Claude | [docs/memory-unification.md](docs/memory-unification.md) |
| Configure access control or see all env vars | [docs/security.md](docs/security.md) |
| Understand the daemon + session internals | [docs/architecture.md](docs/architecture.md) |
| Use the `/process` GitHub PR workflow | [docs/github-setup.md](docs/github-setup.md) |
| Wire `.mcp.json` in a Claude Code project | [docs/mcp-client-setup.md](docs/mcp-client-setup.md) |

---

## Requirements

- Docker (with Docker Compose)
- A Slack workspace where you can create apps
- Claude Code (or any MCP-compatible client)

---

## Credits

Built on the original **[claude-slack-bridge](https://github.com/tomeraitz/claude-slack-bridge)** by **[@tomeraitz](https://github.com/tomeraitz)** — thank you for the foundation (the daemon + session model, `ask_on_slack`, and the project bot). MIT-licensed, so this is a friendly fork: it adds the live native streaming, per-channel continuity, shared/unified memory, soft+hard interrupts, async-results-home, and the "self-hosted Claude Tag" framing on top.

## License

MIT — same as upstream. See [LICENSE](LICENSE).
