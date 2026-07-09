# Full deployment runbook — Mattermost + agent-harness + mm-bridge

**Audience:** an AI agent (or engineer) standing up the complete stack from scratch on a
Linux host. Follow the steps in order. Each step ends with a **✅ Checkpoint** you must
verify before moving on. If a checkpoint fails, stop and fix it — do not proceed.

**What you are building:** a Mattermost chat server where each channel (or thread) is
bound to a coding-agent session. Humans talk to `@<bot>` in Mattermost; the bridge relays
turns to a local harness, which runs the real `claude` / `codex` / `pi` CLIs as
subprocesses on this host.

```
Mattermost (Docker)  ──WebSocket/REST──▶  mm-bridge  (host service)
   :8065, bot @<bot>                          │  HTTP + SSE
                                              ▼
                                       agent-harness  (host service, :8877)
                                              │  spawns as subprocesses
                                              ▼
                                   claude / codex / pi CLIs
                              (run as the host user, inside your repos)
```

## Topology — why this split

Install **Mattermost in a container** and the **two Python services on the host**. Do
**not** containerize agent-harness or mm-bridge:

- agent-harness spawns the coding-agent CLIs, which must run **as the host user** with
  access to that user's git identity, repositories on disk, CLI credentials
  (`~/.claude`, `~/.codex`), and PATH. Putting them in a container fights all of that.
- Mattermost is a self-contained stateful server — an ideal container.

| Component | Where | Runs as |
|---|---|---|
| Mattermost + Postgres | Docker (`mattermost/docker`) | container |
| agent-harness | Host, systemd **user** unit | the host user |
| mm-bridge | Host, systemd **user** unit | the host user |

---

## Step 0 — Ask the operator these questions FIRST

Do not run anything until you have answers. Record them in the table below; later steps
reference these names. If the operator says "use your judgement" / "go", pick the
**default** shown and tell them what you chose.

The two services are public repos — you will clone them in Steps 4 and 5:

- agent-harness → `https://github.com/tijszwinkels/agent-harness-echo`
- mm-bridge → `https://github.com/tijszwinkels/mm-bridge`

### 0a. Host & backends (blocking — you cannot proceed without these)

| # | Question | Notes |
|---|---|---|
| 1 | **Host & OS user** — which machine, and which non-root user owns the repos and runs the agents? | All host services run as this user. |
| 2 | **Install directory** — where under this user's home to clone the two repos. | Default `~/projects`. |
| 3 | **Backend credentials** — which coding agents should work: `claude`, `codex`, `pi`? For each, is the CLI installed and **logged in as this user**, or do I need to authenticate it? | At least one is required. Have API keys / logins ready. If a CLI login is interactive, stop and ask the operator to complete it. |

### 0b. Mattermost

| # | Question | Default |
|---|---|---|
| 4 | **New Mattermost, or reuse an existing one?** If existing, give me its URL + admin access and skip Step 1. | New container |
| 5 | **Where will Mattermost be reachable (`DOMAIN`)?** Any of these is fine — ask the operator, don't assume: `localhost` (single-host / eval), a Tailnet hostname (tailnet-only access), or a public domain. | `localhost` |
| 6 | **TLS?** nginx variant (HTTPS on :443) or plain `:8065` behind your own proxy / Tailscale. | Plain `:8065` |
| 7 | **`public_url`** — the URL humans actually click (may differ from where the daemon reaches MM, e.g. a Tailscale hostname). | `http://<DOMAIN>:8065` |
| 8 | **Team slug** (`MM_TEAM`) the bot operates in. | Ask — must be created |

### 0c. The bot & session behaviour

| # | Question | Default |
|---|---|---|
| 9  | **Bot username** — the `@name` the agent posts as. | `bmo` |
| 10 | **Auto-join public channels?** If on, the bot silently joins every public channel it can see (sessions still created only on first engagement). If off, someone must `/invite @<bot>` per channel. | **off** |
| 11 | **Autorespond default** — reply to every message, or only when `@mentioned`? | **mention-only** (off) |
| 12 | **Default backend** for new channels (`claude` / `codex`). | `claude` |
| 13 | **Default model** per backend. | `claude=opus, codex=gpt-5.5` |
| 14 | **`default_cwd`** — working directory new sessions start in (usually the repos root). | `~/projects` |
| 15 | **`allowed_attachment_roots`** — directories the bridge may upload files from via `<openFile>`. | `["~/projects"]` |
| 16 | **Show tool-use posts?** Coalesced per-turn tool-use placeholders, or hide them (only real replies + errors). | show |

> **Fill this in before continuing:**
> host=`____` user=`____` install_dir=`____` backends=`____`
> DOMAIN=`____` tls=`____` public_url=`____` MM_TEAM=`____` bot=`____`
> auto_join=`____` autorespond=`____` default_backend=`____` default_cwd=`____`

---

## Step 1 — Prerequisites

Install and verify the toolchain **as the host user**:

```bash
# Docker + compose plugin (for Mattermost)
docker --version && docker compose version

# uv (both Python services use it)
command -v uv || curl -LsSf https://astral.sh/uv/install.sh | sh

# Node (for the claude CLI), plus screen / jq / git
node --version; screen --version | head -1; jq --version; git --version
```

**✅ Checkpoint:** every command above prints a version. Docker can run containers as this
user (`docker run --rm hello-world`).

---

## Step 2 — Backend coding-agent CLIs

agent-harness runs these as subprocesses inheriting its PATH. Install **at least the
default backend** and authenticate **as the host user** (not root):

```bash
# Claude Code → installs into ~/.npm-global/bin (which the harness puts on PATH)
npm config set prefix ~/.npm-global
export PATH="$HOME/.npm-global/bin:$PATH"
npm install -g @anthropic-ai/claude-code
claude    # run once and complete login / API-key setup

# codex / pi: install their CLIs and authenticate the same way if requested in Q3
```

**✅ Checkpoint:** `claude --version` (and any other chosen backend) resolves, and a trivial
prompt works when run manually as this user. If a CLI lives outside `~/.npm-global/bin`,
note its directory — you'll adjust the harness `run.sh` PATH line in Step 4.

---

## Step 3 — Mattermost (container install)

Skip if reusing an existing instance (Q4) — go to Step 3b to create the bot.

Official quick-start (`github.com/mattermost/docker`):

```bash
git clone https://github.com/mattermost/docker
cd docker
cp env.example .env
# Edit .env: set DOMAIN=<DOMAIN>   (from Q5)

mkdir -p ./volumes/app/mattermost/{config,data,logs,plugins,client/plugins,bleve-indexes}
sudo chown -R 2000:2000 ./volumes/app/mattermost
```

Bring it up — pick the variant from Q6:

```bash
# Plain HTTP on :8065  (evaluation / behind your own proxy)
docker compose -f docker-compose.yml -f docker-compose.without-nginx.yml up -d

# OR HTTPS via bundled nginx
# docker compose -f docker-compose.yml -f docker-compose.nginx.yml up -d
```

> **Host already runs an `nginx-proxy` (auto-vhost + acme-companion)?** Don't use the
> bundled nginx — you'll get a cert/routing conflict. Bring Mattermost up with the
> **without-nginx** compose, then attach the `mattermost` service to the proxy's network and
> hand it the discovery/cert env vars via an override file:
>
> ```yaml
> # docker-compose.override.yml  (alongside the mattermost compose files)
> services:
>   mattermost:
>     networks: [proxy]
>     environment:
>       VIRTUAL_HOST: <DOMAIN>
>       VIRTUAL_PORT: "8065"
>       LETSENCRYPT_HOST: <DOMAIN>
>       LETSENCRYPT_EMAIL: <you@example.com>   # ← placeholder — set a real address
>       MM_SERVICESETTINGS_SITEURL: https://<DOMAIN>
> networks:
>   proxy:
>     external: true
>     name: <proxy-network-name>               # ← placeholder — your proxy's docker network
> ```
>
> Then bring it up including the override:
> `docker compose -f docker-compose.yml -f docker-compose.without-nginx.yml -f docker-compose.override.yml up -d`.
> (Env-var names follow the standard `nginx-proxy` / `acme-companion` convention; the
> network name and email are host-specific — confirm them with the operator.)

Then in the browser at `https://<DOMAIN>/` (or `http://<DOMAIN>:8065/` for the plain variant):

1. Create the **system-admin** user.
2. Create a **team**; note its slug → this is `MM_TEAM` (Q8).

**✅ Checkpoint:** `curl -sSf http://localhost:8065/api/v4/system/ping` returns
`{"status":"OK"}` and you can log in as the admin.

### Step 3b — Bot account + personal access token

In **System Console**:

1. **Integrations → Bot Accounts** → *Enable Bot Account Creation* = **true**.
2. **Integrations → Integration Management** → *Enable Personal Access Tokens* = **true**.
3. Product menu → **Integrations → Bot Accounts → Add Bot Account**. Username = `<bot>`
   (Q9). Create a token — **copy it immediately, it is shown once.**

That token is `MM_BOT_TOKEN`.

**✅ Checkpoint:** the token works:

```bash
curl -s -H "Authorization: Bearer <MM_BOT_TOKEN>" \
  http://localhost:8065/api/v4/users/me | jq .username    # → "<bot>"
```

---

## Step 4 — agent-harness

Clone into the install directory (Q2) and sync. Example uses `~/projects`:

```bash
cd ~/projects                    # <install_dir> from Q2
git clone https://github.com/tijszwinkels/agent-harness-echo
cd agent-harness-echo
uv sync
cp run.sh.example run.sh && chmod +x run.sh   # run.sh is gitignored (per-host) — create it from the template
```

`run.sh` is gitignored (per-host), so you create it from `run.sh.example`. It must
(a) put the backend CLIs **and** `uv` on PATH and (b) pass `--execute-runs`. The template:

```bash
#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
export PATH="$HOME/.npm-global/bin:$HOME/.local/bin:$PATH"   # ← adjust if backends live elsewhere (Step 2)
exec uv run agent-harness serve \
  --host 0.0.0.0 --port 8877 \
  --database .agent-harness.db.8877 \
  --execute-runs                              # ← REQUIRED: without it, runs are recorded but no CLI launches
  # --cors-origin https://your-dashboard      # only if a browser app calls the harness cross-origin
```

Start it once in the foreground to verify, then Ctrl-C:

```bash
./run.sh &
sleep 3
curl -s localhost:8877/v1/health    # → ok
kill %1
```

**✅ Checkpoint:** `/v1/health` responds. `--execute-runs` is present in `run.sh`.

> Notes: `--execute-runs` is what actually spawns `claude`/`codex`; omit it and you get a
> dry recorder. By default the harness also *observes* `~/.claude`, `~/.codex`, `~/.pi`
> transcript roots so terminal sessions appear too (`--no-observer` disables).
>
> PATH: the `export PATH=` line assumes `claude` came from `npm -g` (→ `~/.npm-global/bin`).
> If your backends are **system-installed** (e.g. `/usr/bin/claude`) they're already on PATH
> and you can drop that dir — but keep `~/.local/bin` on PATH so `uv run` resolves under
> systemd's non-interactive shell (e.g. `export PATH="$HOME/.local/bin:$PATH"`).

---

## Step 5 — mm-bridge

```bash
cd ~/projects                    # <install_dir> from Q2
git clone https://github.com/tijszwinkels/mm-bridge
cd mm-bridge
uv sync                          # creates .venv + installs deps
cp run.sh.example run.sh && chmod +x run.sh   # run.sh is gitignored (per-host) — create it from the template
```

> Use `uv sync`, **not** `uv pip install -e .` — the latter needs an already-active venv
> and fails here with *"No virtual environment found"*.

`run.sh` is gitignored (per-host), so you create it from `run.sh.example`. The template
launches via `uv run` — a bare `mm-bridge serve` only works if the console script is on
PATH, which it isn't after a plain `uv sync`:

```bash
#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
export PATH="$HOME/.local/bin:$PATH"   # so `uv` resolves under systemd's non-interactive shell
set -a; source .env; set +a
exec uv run mm-bridge serve
```

### 5a. Config file — `~/.config/mm-bridge/config.toml`

Fill from the Step 0 answers:

> **TOML gotcha — key order matters.** In TOML, every key after a `[section]` header
> belongs to that section. The bridge reads the session defaults below from the **top
> level** of the file, so they must appear **before** any `[mattermost]` / `[agent_harness]`
> header. Put them under a section and they get nested there, are silently ignored, and you
> fall back to built-in defaults (e.g. `auto_join`/`autorespond` stay off no matter what you
> wrote). For the on/off flags, the `.env` overrides in 5b are the most reliable route.

```toml
# ── Top-level keys — MUST come before the [sections] below ──
default_backend           = "<default_backend>"   # Q12
default_cwd               = "<default_cwd>"         # Q14
default_autorespond       = false                  # Q11 (true = reply to every message)
auto_join_public_channels = false                  # Q10 (true = bot joins all public channels)
show_tool_use             = true                   # Q16
allowed_attachment_roots  = ["~/projects"]          # Q15
default_models            = { claude = "opus", codex = "gpt-5.5" }   # Q13
state_file                = "~/.config/mm-bridge/state.json"
sidecar_dir               = "~/.mm-bridge/sessions"

[mattermost]
url        = "localhost"
port       = 8065
scheme     = "http"
team       = "<MM_TEAM>"                       # Q8
public_url = "<public_url>"                    # Q7 — the URL humans click

[agent_harness]
url = "http://localhost:8877"
```

### 5b. Secrets — `.env` next to `run.sh` (git-ignored)

`run.sh` does `set -a; source .env; set +a` before launching the daemon:

```bash
MM_BOT_TOKEN=<MM_BOT_TOKEN>          # from Step 3b — REQUIRED
MM_URL=http://localhost:8065
MM_TEAM=<MM_TEAM>
MM_PUBLIC_URL=<public_url>
AH_URL=http://localhost:8877

# Flag overrides — env wins over BOTH the TOML and the built-in defaults, so this
# is the most reliable way to turn a flag on. Uncomment only what you want ON.
# MM_AUTO_JOIN=true                    # Q10 — bot silently joins all public channels
# MM_BRIDGE_DEFAULT_AUTORESPOND=true   # Q11 — reply to every message, not just @mentions
```

Lock it down: `chmod 600 .env`.

**✅ Checkpoint:** run `./run.sh` in the foreground. It logs a successful Mattermost
WebSocket connection and an agent-harness SSE subscription, with no auth errors. Ctrl-C.

---

## Step 6 — Run both as ordered systemd **user** services

Create `~/.config/systemd/user/agent-harness.service`:

```ini
[Unit]
Description=agent-harness
After=network-online.target
Wants=network-online.target

[Service]
Type=forking
ExecStart=/usr/bin/screen -dmS harness bash -lc '%h/projects/agent-harness-echo/run.sh'
ExecStop=/usr/bin/screen -S harness -X quit
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
```

Create `~/.config/systemd/user/mm-bridge.service` (note the `After=` ordering on the
harness):

```ini
[Unit]
Description=mm-bridge
After=network-online.target agent-harness.service
Wants=network-online.target

[Service]
Type=forking
ExecStart=/usr/bin/screen -dmS mmbridge bash -lc '%h/projects/mm-bridge/run.sh'
ExecStop=/usr/bin/screen -S mmbridge -X quit
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
```

Enable, and make them start at boot **without an interactive login**:

```bash
loginctl enable-linger "$USER"
systemctl --user daemon-reload
systemctl --user enable --now agent-harness.service mm-bridge.service
systemctl --user status agent-harness.service mm-bridge.service
```

**✅ Checkpoint:** both units are `active`. `screen -ls` shows `harness` and `mmbridge`
sessions (attach with `screen -r <name>` to read logs; detach with `Ctrl-a d`).

> The `screen` wrapper is optional but matches production and makes logs easy to read. A
> plain `Type=simple` unit calling `run.sh` directly also works.

---

## Step 7 — Claude Code SessionStart hook (in-session helpers)

So a Claude Code session on this host can self-identify as "live in Mattermost" and use
`mm-bridge invite / spawn / channel`, install `~/.claude/hooks/export-session-id.sh`:

```bash
#!/usr/bin/env bash
set -euo pipefail
[ -n "${CLAUDE_ENV_FILE:-}" ] || exit 0
sid=$(jq -r '.session_id // empty')
[ -n "$sid" ] || exit 0
printf 'export CLAUDE_SESSION_ID=%q\n' "$sid" >> "$CLAUDE_ENV_FILE"
```

`chmod +x` it and register it as a `SessionStart` hook in `~/.claude/settings.json`.
(Codex needs no hook — the bridge discovers its session via `/proc` and the rollout file.)

**✅ Checkpoint:** in a Claude Code session bound to a channel, `mm-bridge channel` prints
a channel id instead of "not in MM channel".

---

## Step 8 — End-to-end smoke test

1. `curl -s localhost:8877/v1/health` → ok.
2. In Mattermost, create a channel and `/invite @<bot>` (skip the invite if auto-join is on).
3. Post `@<bot> hello`. Within a few seconds you get a reply.
4. Type `.status` → shows session id, backend, model, cwd, autorespond flag, harness status.
5. Type `.help` → lists dot-commands (`.stop`, `.model`, `.sessions`, `.autorespond`, …).

**✅ Done** when a message to the bot produces a model reply and `.status` reports the
harness as reachable.

---

## Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| Bot never replies | mm-bridge not connected — `screen -r mmbridge` for logs; check `MM_BOT_TOKEN`, `MM_URL`, team slug. |
| Reply says harness unreachable | agent-harness down or wrong `AH_URL`; `curl localhost:8877/v1/health`. |
| Turn recorded but no output | `--execute-runs` missing from harness `run.sh`. |
| `FileNotFoundError: claude/codex` | Backend CLI not on the harness PATH — fix the `export PATH=` line in `run.sh` (systemd's non-interactive shell skips `~/.bashrc`). |
| Services don't start after reboot | `loginctl enable-linger "$USER"` not set. |
| `<openFile>` uploads nothing | Path is outside `allowed_attachment_roots`. |
| `mm-bridge` says "not in MM channel" | SessionStart hook missing (claude) or invoked in the startup race before the sidecar exists. |

---

## Sources

- Mattermost container install — https://github.com/mattermost/docker ·
  https://docs.mattermost.com/deployment-guide/server/deploy-containers.html
- Bot accounts / personal access tokens —
  https://developers.mattermost.com/integrate/reference/bot-accounts/ ·
  https://developers.mattermost.com/integrate/reference/personal-access-token/
- Service details — this repo's `README.md`, `run.sh`, and the agent-harness `README.md` /
  `run.sh`. (Mattermost docs retrieved 2026-07-09.)
