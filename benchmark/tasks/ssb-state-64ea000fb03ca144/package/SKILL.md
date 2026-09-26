---
name: remote-desktop-testing-linux
description: Test any GUI app or change on a Daytona Linux (Ubuntu xfce4 + noVNC) remote desktop sandbox. Use to launch a GUI program, sync a local project, take a screenshot, record a video, or share a clickable live-desktop link with a teammate. Generic — the only dependency is Daytona. For Windows, use remote-desktop-testing-windows.
---

# Remote Desktop Testing on Linux

## Overview

Generic Daytona Linux desktop sandbox driver. It creates (or reuses) a sandbox running an
xfce4 desktop streamed over noVNC, and provides the primitives for visual testing of any
GUI app: sync code, run shell commands, launch GUI programs on the live desktop, screenshot,
record video, and mint a clickable desktop link a teammate can open to drive the app.

## When to Use

- Demo or visually verify a GUI app (Electron, browser, native X11) on Linux without a local Linux desktop
- Produce a screenshot + video + clickable live-desktop link for a teammate
- Debug Linux-specific desktop behavior of a cross-platform app
- Run throwaway desktop sessions that are fully disposable (stop with `cleanup`)

## Usage

Run the bundled script with Node from the skill directory:

```bash
node scripts/remote-desktop.mjs <command> [options]
```

Credentials: `DAYTONA_API_KEY` (and optionally `DAYTONA_API_URL`/`DAYTONA_TARGET`) from the
environment or a dotenv file — `./.env` by default, override with `--env-path <file>`.
The script auto-installs `@daytona/sdk` under `~/.letta/skill-state/remote-desktop-testing-linux/`
on first use and remembers the active sandbox id there, so later commands don't need `--sandbox`.

## Typical flow

```bash
# 1. Start (or reuse) a desktop sandbox. Default: image daytonaio/sandbox:0.8.0, cpu 2 / mem 4 / disk 5.
node scripts/remote-desktop.mjs start

# 2. (Optional) push a local project to /home/daytona/remote-desktop/workspace
node scripts/remote-desktop.mjs sync --project-path ~/repos/my-app --sync-mode working_tree

# 3. Set up / build with shell commands (runs bash in the sandbox)
node scripts/remote-desktop.mjs shell \
  --command "cd /home/daytona/remote-desktop/workspace && npm install"

# 4. Launch the GUI app on the live desktop and verify its window appeared
node scripts/remote-desktop.mjs launch \
  --command "./my-app --some-flag" --label my-app --wait-window "My App"

# 5. Deliverables: screenshot, video, and a clickable live-desktop link
node scripts/remote-desktop.mjs screenshot --output /tmp/demo.png
node scripts/remote-desktop.mjs record --output /tmp/demo.mp4 --mp4 --duration 12
node scripts/remote-desktop.mjs preview
```

Always confirm the result visually (open the screenshot, or check `windows` / `--wait-window`)
before reporting success — never report success from process existence alone. When sharing with
a teammate, send the `desktopUrl` from `preview` (signed noVNC link, expires ~6h; re-run
`preview` to refresh). Pass `--port <n>` to `preview` to also mint a signed URL for an app's
HTTP port.

## Commands

| Command | What it does |
| --- | --- |
| `start` | Create/reuse a sandbox and start the desktop. `--fresh` forces a new one. |
| `sync` | Sync `--project-path` to `/home/daytona/remote-desktop/workspace` (`git_archive` = committed HEAD, `working_tree` = uncommitted too; preserves remote `node_modules`). |
| `shell --command SH` | Run a bash command. `--timeout <s>` for long ones. |
| `launch --command CMD` | Launch a GUI command on the xfce4 desktop (detached, `DISPLAY` set, logged to `/home/daytona/remote-desktop/launch-<label>.log`). `--wait-window <regex>` verifies a matching window title appears. |
| `windows` | List visible desktop windows. |
| `screenshot` | Save a PNG of the full desktop locally. |
| `preview` | Print signed `desktopUrl` (noVNC), plus `appUrl` if `--port` given. |
| `record` | Record the live desktop via Playwright + local Chrome (`--duration <s>`, `--mp4`). |
| `recording-start/stop/download` | Native Daytona recording (installs ffmpeg in-sandbox on first use). |
| `snapshot --snapshot <name>` | Bake the sandbox into a snapshot (stops, snapshots, restarts). |
| `cleanup` | Stop the active sandbox. |

## Sizing and snapshots

- Daytona's **default Linux sandbox is tiny (cpu 1 / mem 1 / disk 3GB)**. This skill creates
  from the desktop-capable image `daytonaio/sandbox:0.8.0` with `--cpu 2 --memory 4 --disk 5`
  by default; raise them for big projects (`--cpu 4 --memory 8 --disk 10`). Disk is capped at
  **10GB per sandbox**.
- `resources` cannot be combined with a snapshot — `--snapshot <name>` creates with whatever
  size was baked in. Use `snapshot` to bake a prepared box (deps installed, caches warm) so
  future `start --snapshot <name>` runs skip setup. Revert throwaway edits before baking.

## Gotchas

- Electron/Chromium apps need `--no-sandbox` inside the container, and often
  `--disable-gpu --disable-dev-shm-usage` too; put those flags in your `launch --command`.
- `launch` runs the command detached with `DISPLAY` pointed at the live desktop (detected from
  `/tmp/.X11-unix`, falls back to `:0`). Check the launch log via `shell` if nothing appears.
- For the shareable link, send `desktopUrl` (the interactive desktop), not a raw app-port URL —
  the app URL only serves HTTP and shows nothing of the desktop.
- `record` runs Playwright locally: it resolves `playwright`/`playwright-core` from
  `--project-path` first, else auto-installs `playwright-core` into skill state, and drives the
  local Chrome binary (`--chrome-path`, default macOS Chrome). `--mp4` needs local ffmpeg.
- The base image has `xfce4-terminal`, `chromium`, and `xeyes` available for quick desktop
  sanity checks (e.g. `launch --command "xfce4-terminal" --wait-window Terminal`).
