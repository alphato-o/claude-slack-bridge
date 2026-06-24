# Unified memory (terminal ↔ Slack bridge)

By default the **terminal Claude** (CLI, run from the host project dir) and the
**Slack-bridge Claude** (`claude -p` inside the container) keep *separate*
file-based memory, because Claude keys memory by working directory and the two
runtimes see the project at different paths:

| Runtime | cwd | memory dir |
|---|---|---|
| Terminal (host) | `~/Dev/ccplayground/<P>` | `~/.claude/projects/-Users-fydeos-Dev-ccplayground-<P>/memory/` |
| Bridge (container) | `/projects/<P>` | `~/.claude/projects/-projects-<P>/memory/` |

We **unify** them with a surgical bind-mount of *only the memory subdir*, so a
memory written on either side is instantly visible to the other. (Session
transcripts stay per-runtime in the `claude-home` named volume — we share the
curated `MEMORY.md` + memory files, not raw chat logs.)

## Add it for a new bridge project `P`

1. Make sure the host memory dir exists (the bind source must exist, or Docker
   creates it root-owned):

   ```sh
   mkdir -p ~/.claude/projects/-Users-fydeos-Dev-ccplayground-<P>/memory
   ```

2. Add one line to `docker-compose.yml` under `volumes:` (alongside the others):

   ```yaml
   - ${HOME}/.claude/projects/-Users-fydeos-Dev-ccplayground-<P>/memory:/home/appuser/.claude/projects/-projects-<P>/memory
   ```

3. Recreate the container and verify:

   ```sh
   docker compose up -d
   docker inspect claude-slack-bridge --format '{{range .Mounts}}{{.Destination}} <= {{.Source}}{{"\n"}}{{end}}' | grep memory
   # quick bidirectional check:
   echo probe > ~/.claude/projects/-Users-fydeos-Dev-ccplayground-<P>/memory/.probe
   docker exec claude-slack-bridge cat /home/appuser/.claude/projects/-projects-<P>/memory/.probe
   ```

## The slug rule

The path component is the absolute cwd with every `/` replaced by `-`:

- host `~/Dev/ccplayground/<P>` → `-Users-fydeos-Dev-ccplayground-<P>`
- container `/projects/<P>` → `-projects-<P>` (always, since the bridge runs in `/projects/<P>`)

If a project lives outside `~/Dev/ccplayground`, recompute the **host** slug from
its real path; the container slug is always `-projects-<P>`.

## Currently unified

`emmshopify`, `RoxImproved`, `phicampaign`, `claude-slack-bridge` (the dogfood
channel) — all in `docker-compose.yml`. Every configured project is unified;
keep it that way.

> **When wiring any new project into the bridge, do this step too** — it's part
> of onboarding a project, not optional. (Recorded in memory `bridge-memory-unification`.)
