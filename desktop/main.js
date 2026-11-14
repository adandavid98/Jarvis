const { app, BrowserWindow, WebContentsView, Tray, Menu, globalShortcut, ipcMain, screen, nativeImage, dialog, shell } = require('electron');
const path = require('path');
const { spawn } = require('child_process');
const WebSocket = require('ws');
const { autoUpdater } = require('electron-updater');

const BRIDGE_PORT = 8765;
let mainWindow = null;
let overlayWindow = null;
let viewerView = null;
let viewerAttached = false;
let tray = null;
let bridgeProc = null;
let isQuitting = false;
let overlayHideTimer = null;

function startPythonBridge() {
  const args = ['--port', String(BRIDGE_PORT), '--auto-voice'];

  if (app.isPackaged) {
    const bridgeExe = path.join(process.resourcesPath, 'jarvis-bridge', 'jarvis-bridge.exe');
    const userDataDir = app.getPath('userData');
    const docsDir = app.getPath('documents');
    bridgeProc = spawn(bridgeExe, args, {
      cwd: userDataDir,
      env: {
        ...process.env,
        JARVIS_DATA_DIR: userDataDir,
        JARVIS_WORKSPACE_DIR: docsDir,
        JARVIS_AUTO_APPROVE_DANGEROUS: process.env.JARVIS_AUTO_APPROVE_DANGEROUS ?? 'true',
      },
      stdio: ['ignore', 'pipe', 'pipe'],
      windowsHide: true,
    });
  } else {
    const rootDir = path.resolve(__dirname, '..');
    const pythonExe = path.join(rootDir, '.venv', 'Scripts', 'python.exe');
    bridgeProc = spawn(pythonExe, ['-m', 'jarvis.bridge', ...args], {
      cwd: rootDir,
      env: {
        ...process.env,
        JARVIS_AUTO_APPROVE_DANGEROUS: process.env.JARVIS_AUTO_APPROVE_DANGEROUS ?? 'true',
      },
      stdio: ['ignore', 'pipe', 'pipe'],
      windowsHide: true,
    });
  }

  bridgeProc.stdout.on('data', (data) => {
    process.stdout.write(`[bridge] ${data}`);
  });
  bridgeProc.stderr.on('data', (data) => {
    process.stderr.write(`[bridge:err] ${data}`);
  });
  bridgeProc.on('exit', (code) => {
    console.log(`[bridge] exited with code ${code}`);
    bridgeProc = null;
  });
}

function stopPythonBridge() {
  if (!bridgeProc) return;
  try {
    bridgeProc.kill();
  } catch (err) {
    console.error('Failed to stop Python bridge:', err);
  }
  bridgeProc = null;
}

function createMainWindow() {
  mainWindow = new BrowserWindow({
    width: 1240,
    height: 800,
    minWidth: 960,
    minHeight: 640,
    frame: false,
    backgroundColor: '#04090b',
    icon: path.join(__dirname, 'assets', 'jarvis-icon.png'),
    title: 'J.A.R.V.I.S. — Windows Command HUD',
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
      backgroundThrottling: true,
    },
  });

  mainWindow.loadFile(path.join(__dirname, 'renderer', 'index.html'));

  mainWindow.on('hide', () => setTelemetryPaused(true));
  mainWindow.on('minimize', () => setTelemetryPaused(true));
  mainWindow.on('show', () => setTelemetryPaused(false));
  mainWindow.on('restore', () => setTelemetryPaused(false));

  // Hide to System Tray on close so wake word ("Hey Jarvis") stays active
  mainWindow.on('close', (event) => {
    if (!isQuitting) {
      event.preventDefault();
      mainWindow.hide();
    }
  });
}

function createOverlayWindow() {
  const { workArea } = screen.getPrimaryDisplay();
  const width = 480;
  const height = 136;
  const x = Math.round(workArea.x + (workArea.width - width) / 2);
  const y = Math.round(workArea.y + workArea.height - height - 20);

  overlayWindow = new BrowserWindow({
    width,
    height,
    x,
    y,
    show: false,
    frame: false,
    transparent: true,
    alwaysOnTop: true,
    skipTaskbar: true,
    resizable: false,
    focusable: false,
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
    },
  });

  overlayWindow.setAlwaysOnTop(true, 'floating');
  overlayWindow.loadFile(path.join(__dirname, 'renderer', 'overlay.html'));
}

function showOverlay() {
  if (!overlayWindow) return;
  if (overlayHideTimer) {
    clearTimeout(overlayHideTimer);
    overlayHideTimer = null;
  }
  if (!mainWindow || !mainWindow.isVisible() || mainWindow.isMinimized()) {
    overlayWindow.showInactive();
  }
}

function scheduleHideOverlay(delayMs = 2200) {
  if (!overlayWindow || !overlayWindow.isVisible()) return;
  if (overlayHideTimer) clearTimeout(overlayHideTimer);
  overlayHideTimer = setTimeout(() => {
    overlayWindow?.hide();
    overlayHideTimer = null;
  }, delayMs);
}

function sendBridgeCommand(payload) {
  try {
    const ws = new WebSocket(`ws://127.0.0.1:${BRIDGE_PORT}`);
    ws.on('open', () => {
      ws.send(JSON.stringify(payload));
      ws.close();
    });
    ws.on('error', () => {});
  } catch (_) {}
}

function setTelemetryPaused(paused) {
  sendBridgeCommand({ action: 'set_telemetry_paused', paused: Boolean(paused) });
}

function triggerManualWake() {
  showOverlay();
  sendBridgeCommand({ action: 'wake_now' });
}

const WAKE_SHORTCUT_CANDIDATES = [
  'CommandOrControl+Alt+J',
  'Alt+Shift+J',
  'CommandOrControl+Shift+Space',
];
let activeWakeShortcut = 'Ctrl+Alt+J';

function formatShortcutLabel(accel) {
  return accel.replace('CommandOrControl', 'Ctrl');
}

function registerWakeShortcut() {
  for (const accel of WAKE_SHORTCUT_CANDIDATES) {
    if (globalShortcut.register(accel, triggerManualWake)) {
      activeWakeShortcut = formatShortcutLabel(accel);
      return activeWakeShortcut;
    }
  }
  activeWakeShortcut = 'Unavailable';
  return null;
}

function createTray() {
  const icon = nativeImage.createFromPath(path.join(__dirname, 'assets', 'tray-icon.png'));
  tray = new Tray(icon);
  tray.setToolTip('J.A.R.V.I.S. — Listening for "Hey Jarvis"');

  const buildMenu = () => {
    const login = app.getLoginItemSettings();
    return Menu.buildFromTemplate([
      {
        label: 'Open Full HUD',
        click: () => {
          overlayWindow?.hide();
          mainWindow?.show();
          mainWindow?.focus();
        },
      },
      {
        label: `Wake Jarvis Now (${activeWakeShortcut})`,
        click: triggerManualWake,
      },
      { type: 'separator' },
      {
        label: 'Start with Windows',
        type: 'checkbox',
        checked: login.openAtLogin,
        click: (item) => {
          app.setLoginItemSettings({ openAtLogin: item.checked });
          tray.setContextMenu(buildMenu());
        },
      },
      { type: 'separator' },
      {
        label: 'Check for Updates...',
        click: () => checkForUpdates(true),
      },
      { type: 'separator' },
      {
        label: 'Quit Jarvis',
        click: () => {
          isQuitting = true;
          app.quit();
        },
      },
    ]);
  };

  tray.setContextMenu(buildMenu());
  tray.on('double-click', () => {
    overlayWindow?.hide();
    mainWindow?.show();
    mainWindow?.focus();
  });
}

ipcMain.on('window:minimize', () => mainWindow?.minimize());
ipcMain.on('window:maximize', () => {
  if (!mainWindow) return;
  if (mainWindow.isMaximized()) mainWindow.unmaximize();
  else mainWindow.maximize();
});
ipcMain.on('window:close', () => mainWindow?.close());
ipcMain.on('app:check-updates', () => checkForUpdates(true));

ipcMain.on('overlay:expand', () => {
  overlayWindow?.hide();
  mainWindow?.show();
  mainWindow?.focus();
});
ipcMain.on('overlay:hide', () => overlayWindow?.hide());
ipcMain.on('jarvis:wake', () => showOverlay());
ipcMain.on('jarvis:state', (_ev, state) => {
  if (state === 'listening' || state === 'speaking' || state === 'tool_running' || state === 'thinking') {
    showOverlay();
  } else if (state === 'idle_wake' || state === 'stopped') {
    scheduleHideOverlay(2200);
  }
});

function normalizeViewerUrl(raw) {
  const trimmed = String(raw || '').trim();
  if (!trimmed) return 'https://duckduckgo.com';
  if (/^https?:\/\//i.test(trimmed)) return trimmed;
  if (/^[a-z0-9.-]+\.[a-z]{2,}(\/.*)?$/i.test(trimmed)) return `https://${trimmed}`;
  return `https://duckduckgo.com/?q=${encodeURIComponent(trimmed)}`;
}

function emitViewerNavState() {
  if (!viewerView || !mainWindow || mainWindow.isDestroyed()) return;
  const wc = viewerView.webContents;
  mainWindow.webContents.send('viewer:navigated', {
    url: wc.getURL(),
    title: wc.getTitle(),
    canGoBack: wc.navigationHistory.canGoBack(),
    canGoForward: wc.navigationHistory.canGoForward(),
  });
}

function ensureViewerView() {
  if (viewerView) return viewerView;
  viewerView = new WebContentsView({
    webPreferences: {
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
    },
  });
  viewerView.setBackgroundColor('#071216');
  const wc = viewerView.webContents;
  wc.on('did-navigate', emitViewerNavState);
  wc.on('did-navigate-in-page', emitViewerNavState);
  wc.on('page-title-updated', emitViewerNavState);
  wc.setWindowOpenHandler(({ url }) => {
    if (url && /^https?:\/\//i.test(url)) {
      wc.loadURL(url);
    }
    return { action: 'deny' };
  });
  return viewerView;
}

ipcMain.on('viewer:open', (_ev, rawUrl) => {
  if (!mainWindow) return;
  const view = ensureViewerView();
  if (!viewerAttached) {
    mainWindow.contentView.addChildView(view);
    viewerAttached = true;
  }
  view.webContents.loadURL(normalizeViewerUrl(rawUrl));
});

ipcMain.on('viewer:set-bounds', (_ev, bounds) => {
  if (!viewerView || !viewerAttached || !bounds) return;
  viewerView.setBounds({
    x: Math.max(0, Math.round(bounds.x || 0)),
    y: Math.max(0, Math.round(bounds.y || 0)),
    width: Math.max(100, Math.round(bounds.width || 300)),
    height: Math.max(100, Math.round(bounds.height || 300)),
  });
});

ipcMain.on('viewer:close', () => {
  if (viewerView && viewerAttached && mainWindow && !mainWindow.isDestroyed()) {
    mainWindow.contentView.removeChildView(viewerView);
    viewerAttached = false;
  }
});

ipcMain.on('viewer:navigate', (_ev, action) => {
  if (!viewerView) return;
  const wc = viewerView.webContents;
  if (action === 'back' && wc.navigationHistory.canGoBack()) wc.navigationHistory.goBack();
  else if (action === 'forward' && wc.navigationHistory.canGoForward()) wc.navigationHistory.goForward();
  else if (action === 'reload') wc.reload();
});

ipcMain.on('viewer:open-external', (_ev, url) => {
  const target = url || viewerView?.webContents?.getURL();
  if (target && /^https?:\/\//i.test(target)) {
    shell.openExternal(target);
  }
});

let manualUpdateCheck = false;
let updateCheckTimer = null;

function setupAutoUpdater() {
  if (!app.isPackaged) return;

  autoUpdater.autoDownload = true;
  autoUpdater.autoInstallOnAppQuit = true;

  autoUpdater.on('update-not-available', () => {
    if (!manualUpdateCheck) return;
    manualUpdateCheck = false;
    dialog.showMessageBox({
      type: 'info',
      title: 'Jarvis Updates',
      message: `Jarvis v${app.getVersion()} is up to date.`,
    });
  });

  autoUpdater.on('update-downloaded', (info) => {
    manualUpdateCheck = false;
    dialog.showMessageBox({
      type: 'info',
      title: 'Jarvis Update Ready',
      message: `Jarvis v${info.version} has been downloaded.`,
      detail: 'Restart Jarvis now to apply the update, or it will install automatically when you quit.',
      buttons: ['Restart & Update Now', 'Later'],
      defaultId: 0,
      cancelId: 1,
    }).then(({ response }) => {
      if (response === 0) {
        isQuitting = true;
        autoUpdater.quitAndInstall();
      }
    });
  });

  autoUpdater.on('error', (err) => {
    if (!manualUpdateCheck) return;
    manualUpdateCheck = false;
    dialog.showMessageBox({
      type: 'warning',
      title: 'Jarvis Update Check Failed',
      message: 'Could not check for updates right now.',
      detail: err?.message || String(err),
    });
  });

  setTimeout(() => checkForUpdates(false), 10_000);
  updateCheckTimer = setInterval(() => checkForUpdates(false), 4 * 60 * 60 * 1000);
}

function checkForUpdates(isManual = false) {
  if (!app.isPackaged) {
    if (isManual) {
      dialog.showMessageBox({
        type: 'info',
        title: 'Jarvis Updates',
        message: 'Auto-updates only run in the packaged Windows app.',
        detail: 'In development mode (npm.cmd start), pull from GitHub or run locally.',
      });
    }
    return;
  }
  manualUpdateCheck = isManual;
  autoUpdater.checkForUpdates().catch(() => {});
}

const gotLock = app.requestSingleInstanceLock();
if (!gotLock) {
  app.quit();
} else {
  app.on('second-instance', () => {
    if (mainWindow) {
      mainWindow.show();
      mainWindow.focus();
    }
  });

  app.whenReady().then(() => {
    startPythonBridge();
    createMainWindow();
    createOverlayWindow();
    registerWakeShortcut();
    createTray();
    setupAutoUpdater();
  });
}

app.on('before-quit', () => {
  isQuitting = true;
  if (updateCheckTimer) clearInterval(updateCheckTimer);
  globalShortcut.unregisterAll();
  stopPythonBridge();
});

app.on('window-all-closed', () => {
  if (isQuitting) {
    stopPythonBridge();
    if (process.platform !== 'darwin') app.quit();
  }
});
