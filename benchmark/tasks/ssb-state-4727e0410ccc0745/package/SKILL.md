---
name: remote-desktop-testing-windows
description: Test any GUI app or change on a Daytona Windows remote desktop sandbox. Use to launch a GUI program, sync a local project, take a screenshot, record a video, or share a clickable live-desktop link with a teammate. Generic — the only dependency is Daytona. For Linux, use remote-desktop-testing-linux.
---

# Remote Desktop Testing on Windows

## Overview

Generic Daytona Windows sandbox driver. It creates (or reuses) a Windows sandbox whose desktop
is streamed over noVNC, and provides the primitives for visual testing of any GUI app: sync
code, run PowerShell, launch GUI programs in the interactive desktop session, screenshot,
record video, and mint a clickable desktop link a teammate can open to drive the app.

## When to Use

- Demo or visually verify a GUI app (Electron, browser, native Win32) on Windows without a local Windows machine
- Produce a screenshot + video + clickable live-desktop link for a teammate
- Debug Windows-specific desktop behavior of a cross-platform app
- Run throwaway desktop sessions that are fully disposable (stop with `cleanup`)

## Usage

Run the bundled script with Node from the skill directory:

```bash
node scripts/remote-desktop.mjs <command> [options]
```

Credentials: `DAYTONA_API_KEY` (and optionally `DAYTONA_API_URL`/`DAYTONA_TARGET`) from the
environment or a dotenv file — `./.env` by default, override with `--env-path <file>`.
The script auto-installs `@daytona/sdk` under `~/.letta/skill-state/remote-desktop-testing-windows/`
on first use and remembers the active sandbox id there, so later commands don't need `--sandbox`.

## Typical flow

```bash
# 1. Start (or reuse) a Windows sandbox (default snapshot: "windows", the stock Daytona one)
node scripts/remote-desktop.mjs start

# 2. (Optional) push a local project to C:\Users\Public\remote-desktop\workspace
node scripts/remote-desktop.mjs sync --project-path ~/repos/my-app --sync-mode working_tree

# 3. Set up / build with PowerShell commands
node scripts/remote-desktop.mjs shell \
  --command "Set-Location C:\Users\Public\remote-desktop\workspace; npm install"

# 4. Launch the GUI app in the interactive desktop session and verify its window appeared
node scripts/remote-desktop.mjs launch \
  --command "my-app.exe --some-flag" --label my-app --wait-window "My App"

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
| `start` | Create/reuse a sandbox and start the desktop stream. `--fresh` forces a new one. |
| `sync` | Sync `--project-path` to `C:\Users\Public\remote-desktop\workspace` (`git_archive` = committed HEAD, `working_tree` = uncommitted too; `robocopy /MIR` preserving remote `node_modules`). |
| `shell --command PS` | Run PowerShell. `--timeout <s>` for long ones. |
| `launch --command CMD` | Launch a GUI command in the interactive desktop session via `schtasks /IT` (logged to `C:\Users\Public\remote-desktop\launch-<label>.log`). `--wait-window <regex>` verifies a matching window title appears. |
| `windows` | List visible desktop windows. |
| `screenshot` | Save a PNG of the full desktop locally. |
| `preview` | Print signed `desktopUrl` (noVNC), plus `appUrl` if `--port` given. |
| `record` | Record the live desktop via Playwright + local Chrome (`--duration <s>`, `--mp4`). |
| `recording-start/stop/download` | Native Daytona recording — **unreliable on Windows** (recordings can silently vanish); prefer `record`. |
| `snapshot --snapshot <name>` | Bake the sandbox into a snapshot (Daytona snapshots Windows from STOPPED; stops, snapshots, restarts). |
| `cleanup` | Stop the active sandbox. |

## Snapshots

Cold Windows boxes are slow to set up. After installing your toolchain/deps once, bake a
prepared snapshot so future `start --snapshot <name>` runs skip setup:

```bash
node scripts/remote-desktop.mjs snapshot --snapshot my-app-windows-dev-v1
```

Revert throwaway edits before baking. Keep install-skip markers or caches **outside** the
workspace directory (e.g. directly under `C:\Users\Public\remote-desktop\`) — `sync` mirrors
with `robocopy /MIR`, which deletes anything in the workspace not present locally.

## Gotchas

- **Daytona shell/process commands run as SYSTEM.** GUI apps launched from `shell` can run
  invisibly. `launch` exists precisely for this: it wraps the command in a `.cmd` and runs it
  via `schtasks /IT /RU Administrator`, which puts the window in the interactive session that
  the noVNC link shows. Always use `launch` (not `shell`) to start GUI apps.
- Verify with `--wait-window` or `windows` + `screenshot` — a running process is not a visible
  window.
- For the shareable link, send `desktopUrl` (the interactive desktop), not a raw app-port URL.
- `record` runs Playwright locally: it resolves `playwright`/`playwright-core` from
  `--project-path` first, else auto-installs `playwright-core` into skill state, and drives the
  local Chrome binary (`--chrome-path`, default macOS Chrome). `--mp4` needs local ffmpeg.
- Maintainer note: the launch `.cmd` generation uses `String.raw` — keep it that way. A plain
  template literal turns Windows path backslashes (`\node_modules`, `\bin`) into JS escapes
  (`\n`, `\b`) and silently corrupts the launcher.
- Native `schtasks /Delete` on a missing task writes to stderr, which PowerShell's
  `$ErrorActionPreference='Stop'` escalates — the script routes it through `cmd.exe /c` to
  swallow that; do the same for any other best-effort native command you add.
