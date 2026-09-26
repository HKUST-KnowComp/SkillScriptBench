#!/usr/bin/env node
// Generic Daytona Linux remote-desktop testing helper.
// Only hard dependency is @daytona/sdk (auto-installed under ~/.letta/skill-state/).
import { execFile } from 'node:child_process';
import { createRequire } from 'node:module';
import { existsSync } from 'node:fs';
import { copyFile, mkdir, mkdtemp, readFile, readdir, rm, writeFile } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { parseArgs } from 'node:util';
import { promisify } from 'node:util';

const execFileAsync = promisify(execFile);

const SKILL_ID = 'remote-desktop-testing-linux';
const DATA_DIR = path.join(os.homedir(), '.letta', 'skill-state', SKILL_ID);
const STATE_PATH = path.join(DATA_DIR, 'state.json');
const DEFAULT_WORK_ROOT = '/home/daytona/remote-desktop';
const DEFAULT_WORKSPACE = `${DEFAULT_WORK_ROOT}/workspace`;
const DEFAULT_IMAGE = 'daytonaio/sandbox:0.8.0'; // desktop-capable base image (xfce4 + noVNC)
const SDK_VERSION = '0.167.0';

const { values, positionals } = parseArgs({
  allowPositionals: true,
  options: {
    sandbox: { type: 'string' },
    snapshot: { type: 'string' },
    image: { type: 'string' },
    cpu: { type: 'string', default: '2' },
    memory: { type: 'string', default: '4' },
    disk: { type: 'string', default: '5' },
    name: { type: 'string' },
    fresh: { type: 'boolean', default: false },
    'env-path': { type: 'string' },
    'project-path': { type: 'string', default: process.cwd() },
    'sync-mode': { type: 'string', default: 'git_archive' },
    'auto-stop-minutes': { type: 'string', default: '60' },
    command: { type: 'string' },
    timeout: { type: 'string', default: '120' },
    port: { type: 'string' },
    'desktop-port': { type: 'string', default: '6080' },
    output: { type: 'string' },
    label: { type: 'string', default: 'demo' },
    'recording-id': { type: 'string' },
    'chrome-path': { type: 'string', default: '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome' },
    mp4: { type: 'boolean', default: false },
    'restart-after': { type: 'boolean', default: true },
    duration: { type: 'string', default: '10' },
    'wait-window': { type: 'string' },
  },
});

const command = positionals[0];

function usage(message) {
  if (message) console.error(message);
  console.error(`Usage: remote-desktop.mjs <command> [options]

Commands:
  start                 Create/reuse a Linux desktop sandbox and start the desktop (noVNC)
  sync                  Sync a local directory (--project-path) to ${DEFAULT_WORKSPACE}
  shell --command SH    Run a bash command in the active sandbox
  launch --command SH   Launch a GUI command on the interactive xfce4 desktop (backgrounded, logged)
  windows               Print open desktop windows from Daytona computerUse
  screenshot            Save a desktop screenshot locally
  preview               Print signed desktop noVNC URL (plus --port <n> app URL if given)
  record                Record a video of the live desktop via Playwright + local Chrome
  recording-start       Start native Daytona desktop recording
  recording-stop        Stop active/selected recording
  recording-download    Download selected/last recording
  snapshot --snapshot N Create a filesystem snapshot (stops sandbox first, restarts by default)
  cleanup               Stop the active sandbox

Common options:
  --sandbox <id>              Defaults to saved active sandbox
  --image <ref>               Create from image (default: ${DEFAULT_IMAGE}); allows --cpu/--memory/--disk
  --snapshot <name>           Create from a prepared snapshot instead (resources are baked in)
  --cpu/--memory/--disk <n>   Sandbox size when creating from an image (default 2/4/5; disk max 10)
  --env-path <path>           Dotenv with DAYTONA_API_KEY (default: env vars, then ./.env)
  --project-path <path>       Local directory for sync (default: cwd)
  --sync-mode <mode>          git_archive (committed HEAD) | working_tree | none
  --wait-window <regex>       With launch: wait until a window title matches before returning
`);
  process.exit(message ? 1 : 0);
}
if (!command || command === 'help' || command === '--help') usage();

function safeString(value) {
  return typeof value === 'string' ? value.trim() : '';
}

function parseDotenv(text) {
  const env = {};
  for (const rawLine of text.split(/\r?\n/)) {
    const line = rawLine.trim();
    if (!line || line.startsWith('#') || !line.includes('=')) continue;
    const index = line.indexOf('=');
    const key = line.slice(0, index).trim();
    let value = line.slice(index + 1).trim();
    if ((value.startsWith('"') && value.endsWith('"')) || (value.startsWith("'") && value.endsWith("'"))) value = value.slice(1, -1);
    env[key] = value;
  }
  return env;
}

async function readState() {
  try { return JSON.parse(await readFile(STATE_PATH, 'utf8')); } catch { return {}; }
}
async function writeState(next) {
  await mkdir(DATA_DIR, { recursive: true });
  await writeFile(STATE_PATH, `${JSON.stringify(next, null, 2)}\n`);
}

async function loadEnv() {
  const env = { ...process.env };
  const envPath = safeString(values['env-path']) || path.join(process.cwd(), '.env');
  try { Object.assign(env, parseDotenv(await readFile(envPath, 'utf8'))); } catch {}
  if (!env.DAYTONA_API_KEY) throw new Error(`DAYTONA_API_KEY is required (set it in the environment or in ${envPath}, or pass --env-path)`);
  return env;
}

async function installIntoSkillState(spec) {
  await mkdir(DATA_DIR, { recursive: true });
  const pkgPath = path.join(DATA_DIR, 'package.json');
  if (!existsSync(pkgPath)) await writeFile(pkgPath, JSON.stringify({ private: true, type: 'module' }, null, 2));
  await execFileAsync('npm', ['install', '--silent', '--prefix', DATA_DIR, spec], { timeout: 180_000, maxBuffer: 2 * 1024 * 1024 });
  return createRequire(pkgPath);
}

async function ensureDaytonaSdk() {
  try {
    const req = createRequire(path.join(DATA_DIR, 'package.json'));
    return await import(req.resolve('@daytona/sdk'));
  } catch {}
  const req = await installIntoSkillState(`@daytona/sdk@${SDK_VERSION}`);
  return await import(req.resolve('@daytona/sdk'));
}

// Resolve a Playwright chromium implementation for `record`: project first, then
// skill state (auto-installs playwright-core, which drives the local Chrome binary).
async function ensureChromium() {
  const candidates = [path.join(values['project-path'], 'package.json'), path.join(DATA_DIR, 'package.json')];
  for (const pkg of candidates) {
    for (const name of ['playwright', 'playwright-core']) {
      try { return createRequire(pkg)(name).chromium; } catch {}
    }
  }
  const req = await installIntoSkillState('playwright-core@1');
  return req('playwright-core').chromium;
}

async function getClient() {
  const env = await loadEnv();
  const { Daytona } = await ensureDaytonaSdk();
  return new Daytona({ apiKey: env.DAYTONA_API_KEY, apiUrl: env.DAYTONA_API_URL, target: env.DAYTONA_TARGET || 'us' });
}

async function getSandbox() {
  const state = await readState();
  const sandboxId = safeString(values.sandbox) || state.sandboxId;
  if (!sandboxId) throw new Error('No sandbox id; pass --sandbox or run start first');
  return { sandbox: await (await getClient()).get(sandboxId), sandboxId, state };
}

function resultText(result) {
  return String(result?.result ?? result?.output ?? result?.stdout ?? result?.artifacts?.stdout ?? '');
}

function shSingle(value) {
  return `'${String(value).replace(/'/g, `'\\''`)}'`;
}

// Run a bash command/script string in the sandbox.
async function runSh(sandbox, script, timeout = Number(values.timeout || 120), env) {
  const result = await sandbox.process.executeCommand(`bash -lc ${shSingle(script)}`, undefined, env, timeout);
  const text = resultText(result);
  if (typeof result?.exitCode === 'number' && result.exitCode !== 0) throw new Error(`bash failed (${result.exitCode}): ${text}`);
  return text;
}

// Upload a script file and run it (for larger scripts). Avoids any shell quoting limits.
async function runShFile(sandbox, script, timeout = Number(values.timeout || 120), env) {
  const remotePath = `/tmp/skill-${Date.now()}-${Math.random().toString(16).slice(2)}.sh`;
  await sandbox.fs.uploadFile(Buffer.from(script, 'utf8'), remotePath, 60);
  try {
    return await runSh(sandbox, `bash ${remotePath}`, timeout, env);
  } finally {
    await runSh(sandbox, `rm -f ${remotePath}`, 30).catch(() => {});
  }
}

async function makeWorkspaceArchive(sourcePath, mode) {
  const temp = await mkdtemp(path.join(os.tmpdir(), `${SKILL_ID}-`));
  const tarPath = path.join(temp, 'workspace.tar.gz');
  if (mode === 'git_archive') {
    await execFileAsync('git', ['archive', '--format=tar.gz', '-o', tarPath, 'HEAD'], { cwd: sourcePath, timeout: 180_000, maxBuffer: 8 * 1024 * 1024 });
    return { tarPath, cleanup: () => rm(temp, { recursive: true, force: true }) };
  }
  if (mode !== 'working_tree') throw new Error(`Unknown sync mode: ${mode}`);
  const excludes = ['.git', 'node_modules', '.next', 'coverage', '.turbo', '.nx', '.letta', '.DS_Store'].flatMap((name) => ['--exclude', name]);
  await execFileAsync('tar', ['czf', tarPath, ...excludes, '-C', sourcePath, '.'], { timeout: 300_000, maxBuffer: 8 * 1024 * 1024 });
  return { tarPath, cleanup: () => rm(temp, { recursive: true, force: true }) };
}

async function syncWorkspace(sandbox) {
  const mode = values['sync-mode'];
  const { tarPath, cleanup } = await makeWorkspaceArchive(values['project-path'], mode);
  try {
    await runSh(sandbox, `mkdir -p ${DEFAULT_WORK_ROOT} && rm -rf ${DEFAULT_WORK_ROOT}/incoming && mkdir -p ${DEFAULT_WORK_ROOT}/incoming && rm -f ${DEFAULT_WORK_ROOT}/workspace.tar.gz`, 120);
    await sandbox.fs.uploadFile(tarPath, `${DEFAULT_WORK_ROOT}/workspace.tar.gz`, 0);
    await runShFile(sandbox, `
set -e
ROOT=${DEFAULT_WORK_ROOT}
WS=${DEFAULT_WORKSPACE}
tar -xzf "$ROOT/workspace.tar.gz" -C "$ROOT/incoming"
if [ -d "$WS" ]; then
  rsync -a --delete --exclude node_modules --exclude .git "$ROOT/incoming/" "$WS/"
else
  mv "$ROOT/incoming" "$WS"
fi
rm -f "$ROOT/workspace.tar.gz"
rm -rf "$ROOT/incoming"
echo "Workspace synced to $WS"
`, 600).then((text) => console.log(text.trim()));
  } finally {
    await cleanup().catch(() => {});
  }
}

async function createOrReuseSandbox() {
  const state = await readState();
  const client = await getClient();
  if (!values.fresh && (values.sandbox || state.sandboxId)) {
    const sandboxId = safeString(values.sandbox) || state.sandboxId;
    try {
      const sandbox = await client.get(sandboxId);
      if (['stopped', 'archived'].includes(String(sandbox.state || '').toLowerCase())) await sandbox.start();
      return { sandbox, sandboxId, reused: true };
    } catch {}
  }
  const snapshot = safeString(values.snapshot);
  const common = {
    name: values.name || `remote-desktop-linux-${Date.now()}`,
    labels: { app: SKILL_ID },
    autoStopInterval: Number(values['auto-stop-minutes'] || 60),
    autoArchiveInterval: 1440,
    autoDeleteInterval: -1,
    ephemeral: false,
  };
  // Two creation paths:
  //  - snapshot: resources are baked into the snapshot (Daytona rejects `resources` with it).
  //  - image (default): explicit resources; the stock default sandbox is cpu1/mem1/disk3,
  //    which is too small for most real projects. Daytona caps disk at 10GB/sandbox.
  const params = snapshot
    ? { snapshot, ...common }
    : { image: safeString(values.image) || DEFAULT_IMAGE, resources: { cpu: Number(values.cpu || 2), memory: Number(values.memory || 4), disk: Number(values.disk || 5) }, ...common };
  const sandbox = await client.create(params, { timeout: 600 });
  return { sandbox, sandboxId: sandbox.id, reused: false };
}

async function ensureComputerUse(sandbox) {
  const status = await sandbox.computerUse?.getStatus?.().catch(() => null);
  if (!JSON.stringify(status || {}).toLowerCase().includes('active')) await sandbox.computerUse.start();
}

async function waitForWindow(sandbox, pattern, seconds = 30) {
  const regex = new RegExp(pattern, 'i');
  for (let i = 0; i < seconds; i++) {
    const wins = await sandbox.computerUse.display.getWindows().catch(() => null);
    const list = wins?.windows || wins?.data?.windows || (Array.isArray(wins) ? wins : []);
    if (list.some((w) => regex.test(String(w?.title ?? '')))) return true;
    await new Promise((r) => setTimeout(r, 1000));
  }
  return false;
}

// Launch a GUI command on the interactive xfce4 desktop. The process is detached
// (setsid) with DISPLAY set to the live desktop, so it shows up in noVNC and survives
// the shell session. Output goes to a log under DEFAULT_WORK_ROOT.
async function launchGui(sandbox) {
  const guiCommand = safeString(values.command);
  if (!guiCommand) usage('Missing --command');
  await ensureComputerUse(sandbox);
  const label = (values.label || 'app').replace(/[^a-zA-Z0-9_-]/g, '-');
  const output = await runShFile(sandbox, `
set -e
ROOT=${DEFAULT_WORK_ROOT}
mkdir -p "$ROOT"
LOG="$ROOT/launch-${label}.log"
DISP=$(ls /tmp/.X11-unix 2>/dev/null | head -1 | sed 's/^X/:/')
[ -z "$DISP" ] && DISP=:0
rm -f "$LOG"
launcher="$ROOT/launch-${label}.sh"
cat > "$launcher" <<EOS
#!/bin/bash
export DISPLAY="$DISP"
cd "${DEFAULT_WORKSPACE}" 2>/dev/null || cd "\\$HOME"
exec ${guiCommand} > "$LOG" 2>&1
EOS
chmod +x "$launcher"
setsid bash "$launcher" >/dev/null 2>&1 &
sleep 2
echo "launched on display $DISP; log: $LOG"
tail -5 "$LOG" 2>/dev/null || true
`, Number(values.timeout || 120));
  let summary = output.trim();
  const pattern = safeString(values['wait-window']);
  if (pattern) {
    const visible = await waitForWindow(sandbox, pattern, 45);
    summary += `\nwindow matching /${pattern}/i visible: ${visible}${visible ? '' : ' (WARNING: process launched but window not detected)'}`;
  }
  return summary;
}

async function ensureLinuxFfmpeg(sandbox) {
  return await runShFile(sandbox, `
set -e
if command -v ffmpeg >/dev/null 2>&1; then ffmpeg -version | head -1; exit 0; fi
(sudo apt-get update -y && sudo apt-get install -y ffmpeg) || (apt-get update -y && apt-get install -y ffmpeg)
ffmpeg -version | head -1
`, 600);
}

async function getPreviewLinks({ sandbox, sandboxId }) {
  await ensureComputerUse(sandbox);
  const desktopPort = Number(values['desktop-port'] || 6080);
  const desktopBase = String((await sandbox.getSignedPreviewUrl(desktopPort, 21600)).url).replace(/\/+$/, '');
  const desktopUrl = `${desktopBase}/vnc.html?autoconnect=true&resize=remote&reconnect=true&reconnect_delay=2000`;
  const links = { sandboxId, desktopUrl };
  const appPort = Number(safeString(values.port) || 0);
  if (appPort) links.appUrl = String((await sandbox.getSignedPreviewUrl(appPort, 21600)).url);
  return links;
}

// Generic video recorder. Records the live noVNC desktop stream via Playwright and the
// local Chrome binary, i.e. exactly what a teammate sees through the desktop link.
async function recordPreview(sandbox) {
  await ensureComputerUse(sandbox);
  const desktopPort = Number(values['desktop-port'] || 6080);
  const desktopBase = String((await sandbox.getSignedPreviewUrl(desktopPort, 21600)).url).replace(/\/+$/, '');
  const desktopUrl = `${desktopBase}/vnc.html?autoconnect=true&resize=remote&reconnect=true&reconnect_delay=2000`;
  const durationMs = Math.max(2, Number(values.duration || 10)) * 1000;
  const chromium = await ensureChromium();
  const videoDir = await mkdtemp(path.join(os.tmpdir(), 'remote-desktop-video-'));
  const browser = await chromium.launch({ headless: true, executablePath: values['chrome-path'] });
  const context = await browser.newContext({ viewport: { width: 1280, height: 800 }, deviceScaleFactor: 1, recordVideo: { dir: videoDir, size: { width: 1280, height: 800 } } });
  const page = await context.newPage();
  await page.goto(desktopUrl, { waitUntil: 'domcontentloaded', timeout: 60_000 });
  await page.waitForSelector('canvas', { timeout: 60_000 }).catch(() => {});
  await page.waitForTimeout(durationMs);
  await context.close();
  await browser.close();
  const files = await readdir(videoDir);
  const webm = files.find((file) => file.endsWith('.webm'));
  if (!webm) throw new Error(`No video produced in ${videoDir}`);
  const wantMp4 = values.mp4 || String(values.output || '').endsWith('.mp4');
  const output = values.output || (wantMp4 ? '/tmp/remote-desktop-demo.mp4' : '/tmp/remote-desktop-demo.webm');
  const webmPath = String(output).endsWith('.webm') ? output : String(output).replace(/\.mp4$/i, '.webm');
  await copyFile(path.join(videoDir, webm), webmPath);
  await rm(videoDir, { recursive: true, force: true }).catch(() => {});
  if (wantMp4) {
    const mp4 = String(output).endsWith('.mp4') ? output : `${output}.mp4`;
    await execFileAsync('ffmpeg', ['-y', '-i', webmPath, '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-movflags', '+faststart', mp4], { timeout: 120_000, maxBuffer: 4 * 1024 * 1024 });
    console.log(`VIDEO ${mp4}`);
  } else {
    console.log(`VIDEO ${webmPath}`);
  }
}

async function main() {
  if (command === 'start') {
    const { sandbox, sandboxId, reused } = await createOrReuseSandbox();
    await ensureComputerUse(sandbox);
    await writeState({ ...(await readState()), sandboxId, workspace: DEFAULT_WORKSPACE, updatedAt: new Date().toISOString() });
    console.log(JSON.stringify({ sandboxId, reused, desktop: 'started' }, null, 2));
    return;
  }

  const { sandbox, sandboxId, state } = await getSandbox();
  if (command === 'sync') await syncWorkspace(sandbox);
  else if (command === 'shell') console.log(await runSh(sandbox, values.command || usage('Missing --command'), Number(values.timeout || 120)));
  else if (command === 'launch') console.log(await launchGui(sandbox));
  else if (command === 'windows') { await ensureComputerUse(sandbox); console.log(JSON.stringify(await sandbox.computerUse.display.getWindows(), null, 2)); }
  else if (command === 'screenshot') {
    await ensureComputerUse(sandbox);
    const shot = await sandbox.computerUse.screenshot.takeFullScreen(true);
    const output = values.output || `/tmp/daytona-${sandboxId}-screenshot.png`;
    const b64 = shot.screenshot || shot.image || shot.data;
    await writeFile(output, Buffer.from(b64, 'base64'));
    console.log(output);
  } else if (command === 'preview') {
    console.log(JSON.stringify(await getPreviewLinks({ sandbox, sandboxId }), null, 2));
  } else if (command === 'record') {
    await recordPreview(sandbox);
  } else if (command === 'recording-start') {
    await ensureComputerUse(sandbox);
    let rec;
    try { rec = await sandbox.computerUse.recording.start(values.label || 'demo'); }
    catch (error) {
      if (String(error?.errorCode || error?.message || '').includes('ffmpeg')) { await ensureLinuxFfmpeg(sandbox); rec = await sandbox.computerUse.recording.start(values.label || 'demo'); }
      else throw error;
    }
    await writeState({ ...state, sandboxId, activeRecordingId: rec.id, updatedAt: new Date().toISOString() });
    console.log(JSON.stringify(rec, null, 2));
  } else if (command === 'recording-stop') {
    const id = values['recording-id'] || state.activeRecordingId;
    if (!id) throw new Error('Missing --recording-id and no active recording in state');
    const rec = await sandbox.computerUse.recording.stop(id);
    await writeState({ ...state, sandboxId, activeRecordingId: null, lastRecordingId: rec.id || id, updatedAt: new Date().toISOString() });
    console.log(JSON.stringify(rec, null, 2));
  } else if (command === 'recording-download') {
    const id = values['recording-id'] || state.lastRecordingId || state.activeRecordingId;
    if (!id) throw new Error('Missing --recording-id and no last recording in state');
    const output = values.output || `/tmp/${id}.mp4`;
    await sandbox.computerUse.recording.download(id, output);
    console.log(output);
  } else if (command === 'snapshot') {
    const name = safeString(values.snapshot);
    if (!name) usage('Missing --snapshot <name>');
    await sandbox.stop(600);
    try { await sandbox._experimental_createSnapshot(name, 0); }
    finally { if (values['restart-after']) await sandbox.start(600).catch(() => {}); }
    await writeState({ ...state, sandboxId, preparedSnapshot: name, snapshotCreatedAt: new Date().toISOString(), updatedAt: new Date().toISOString() });
    console.log(JSON.stringify({ sandboxId, snapshot: name }, null, 2));
  } else if (command === 'cleanup') {
    await sandbox.stop();
    console.log(`Stopped ${sandboxId}`);
  } else {
    usage(`Unknown command: ${command}`);
  }
}

await main();
