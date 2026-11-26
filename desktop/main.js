const { app, BrowserWindow, WebContentsView, Tray, Menu, Notification, globalShortcut, ipcMain, screen, nativeImage, dialog, shell } = require('electron');
const fs = require('fs');
const path = require('path');
const { spawn } = require('child_process');
const WebSocket = require('ws');
const { autoUpdater } = require('electron-updater');

// Enable hardware-accelerated GPU rasterization & video decode for embedded WebContentsView
app.commandLine.appendSwitch('ignore-gpu-blocklist');
app.commandLine.appendSwitch('enable-gpu-rasterization');
app.commandLine.appendSwitch('enable-zero-copy');

const BRIDGE_PORT = 8765;
let mainWindow = null;
let overlayWindow = null;
let viewerView = null;
let viewerAttached = false;
let tray = null;
let bridgeProc = null;
let isQuitting = false;
let overlayHideTimer = null;
let bridgeRestartTimestamps = [];
let bridgeRestartTimer = null;

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
        PYTHONIOENCODING: 'utf-8',
        PYTHONUTF8: '1',
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
        PYTHONIOENCODING: 'utf-8',
        PYTHONUTF8: '1',
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
    if (isQuitting) return;
    const now = Date.now();
    bridgeRestartTimestamps = bridgeRestartTimestamps.filter((ts) => now - ts < 60000);
    if (bridgeRestartTimestamps.length >= 5) {
      console.error('[bridge] Restart rate limit exceeded (5 restarts/min). Automatic restart paused.');
      return;
    }
    bridgeRestartTimestamps.push(now);
    const delayMs = Math.min(8000, 1200 * bridgeRestartTimestamps.length);
    console.log(`[bridge] Scheduling auto-restart in ${delayMs}ms...`);
    if (bridgeRestartTimer) clearTimeout(bridgeRestartTimer);
    bridgeRestartTimer = setTimeout(() => {
      bridgeRestartTimer = null;
      if (!isQuitting && !bridgeProc) {
        startPythonBridge();
      }
    }, delayMs);
  });
}

function stopPythonBridge() {
  if (bridgeRestartTimer) {
    clearTimeout(bridgeRestartTimer);
    bridgeRestartTimer = null;
  }
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
    backgroundColor: '#02070d',
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
      backgroundThrottling: true,
    },
  });

  overlayWindow.setAlwaysOnTop(true, 'floating');
  overlayWindow.on('show', () => overlayWindow?.webContents.send('window:visibility', true));
  overlayWindow.on('hide', () => overlayWindow?.webContents.send('window:visibility', false));
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

let bridgeCtrlWs = null;
const bridgeCtrlQueue = [];

function sendBridgeCommand(payload) {
  try {
    const raw = JSON.stringify(payload);
    if (bridgeCtrlWs && bridgeCtrlWs.readyState === WebSocket.OPEN) {
      bridgeCtrlWs.send(raw);
      return;
    }
    bridgeCtrlQueue.push(raw);
    if (bridgeCtrlQueue.length > 16) bridgeCtrlQueue.shift();

    if (bridgeCtrlWs && bridgeCtrlWs.readyState === WebSocket.CONNECTING) {
      return;
    }
    const ws = new WebSocket(`ws://127.0.0.1:${BRIDGE_PORT}`);
    bridgeCtrlWs = ws;
    ws.on('open', () => {
      while (bridgeCtrlQueue.length > 0 && ws.readyState === WebSocket.OPEN) {
        ws.send(bridgeCtrlQueue.shift());
      }
    });
    ws.on('close', () => {
      if (bridgeCtrlWs === ws) bridgeCtrlWs = null;
    });
    ws.on('error', () => {
      if (bridgeCtrlWs === ws) bridgeCtrlWs = null;
    });
  } catch (_) {}
}

function setTelemetryPaused(paused) {
  sendBridgeCommand({ action: 'set_telemetry_paused', paused: Boolean(paused) });
  if (mainWindow && !mainWindow.isDestroyed()) {
    mainWindow.webContents.send('window:visibility', !paused);
  }
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
ipcMain.on('app:open-logs', () => {
  const logsDir = app.isPackaged
    ? path.join(app.getPath('userData'), 'logs')
    : path.resolve(__dirname, '..', 'data', 'logs');
  try {
    fs.mkdirSync(logsDir, { recursive: true });
    shell.openPath(logsDir);
  } catch (err) {
    console.error('Failed opening logs folder:', err);
  }
});
ipcMain.on('bridge:restart', () => {
  bridgeRestartTimestamps = [];
  stopPythonBridge();
  if (!isQuitting) startPythonBridge();
});
ipcMain.on('reminder:notify', (_ev, rem) => {
  if (!Notification.isSupported()) return;
  const title = rem?.title ? String(rem.title) : 'Recordatorio de Jarvis';
  const due = rem?.due_at ? ` (${rem.due_at})` : '';
  const n = new Notification({
    title: 'J.A.R.V.I.S. // Recordatorio',
    body: `${title}${due}`,
    icon: path.join(__dirname, 'assets', 'jarvis-icon.png'),
  });
  n.on('click', () => {
    overlayWindow?.hide();
    mainWindow?.show();
    mainWindow?.focus();
  });
  n.show();
});

ipcMain.on('overlay:expand', () => {
  overlayWindow?.hide();
  mainWindow?.show();
  mainWindow?.focus();
});
ipcMain.on('overlay:hide', () => overlayWindow?.hide());
ipcMain.handle('dialog:pick-folder', async () => {
  if (!mainWindow || mainWindow.isDestroyed()) return null;
  const res = await dialog.showOpenDialog(mainWindow, {
    title: 'Select Jarvis Workspace Folder',
    properties: ['openDirectory'],
  });
  if (res.canceled || !res.filePaths?.length) return null;
  return res.filePaths[0];
});
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

function pauseViewerMedia() {
  if (!viewerView || viewerView.webContents.isDestroyed()) return;
  viewerView.webContents.executeJavaScript(
    "document.querySelectorAll('video,audio').forEach(m => { try { m.pause(); } catch (_) {} });"
  ).catch(() => {});
}

ipcMain.on('viewer:close', () => {
  pauseViewerMedia();
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
    pauseViewerMedia();
    shell.openExternal(target);
  }
});

let manualUpdateCheck = false;
let updateCheckTimer = null;

function setupAutoUpdater() {
  if (!app.isPackaged) return;

  autoUpdater.autoDownload = true;
  autoUpdater.autoInstallOnAppQuit = true;

  autoUpdater.on('update-available', (info) => {
    if (!manualUpdateCheck) return;
    dialog.showMessageBox({
      type: 'info',
      title: 'Actualización disponible',
      message: `Descargando Jarvis v${info.version}...`,
      detail: 'La aplicación se instalará y reiniciará automáticamente al terminar la descarga.',
    });
  });

  autoUpdater.on('update-not-available', () => {
    if (!manualUpdateCheck) return;
    manualUpdateCheck = false;
    dialog.showMessageBox({
      type: 'info',
      title: 'Jarvis',
      message: 'No hay actualización disponible.',
      detail: `Jarvis v${app.getVersion()} ya está en la última versión.`,
    });
  });

  autoUpdater.on('update-downloaded', (info) => {
    const wasManual = manualUpdateCheck;
    manualUpdateCheck = false;
    if (wasManual) {
      dialog.showMessageBox({
        type: 'info',
        title: 'Actualización lista',
        message: `Jarvis v${info.version} descargada.`,
        detail: 'Se instalará y reiniciará ahora.',
        buttons: ['Reiniciar e instalar'],
        defaultId: 0,
      }).then(() => {
        isQuitting = true;
        autoUpdater.quitAndInstall(false, true);
      });
    } else {
      isQuitting = true;
      autoUpdater.quitAndInstall(false, true);
    }
  });

  autoUpdater.on('error', (err) => {
    if (!manualUpdateCheck) return;
    manualUpdateCheck = false;
    const rawMsg = err?.message || String(err);
    const isPrivateOrMissing = rawMsg.includes('404') || rawMsg.includes('releases.atom');
    dialog.showMessageBox({
      type: 'warning',
      title: 'No se pudo buscar actualizaciones',
      message: isPrivateOrMissing
        ? 'No hay acceso público a los releases de GitHub (404).'
        : 'No se pudo verificar actualizaciones en este momento.',
      detail: isPrivateOrMissing
        ? 'El repositorio adandavid98/Jarvis es privado o aún no tiene un release público con latest.yml. Haz el repositorio público en GitHub para activar el auto-updater.'
        : rawMsg.split('\n')[0].slice(0, 220),
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
      if (mainWindow.isMinimized()) mainWindow.restore();
      mainWindow.show();
      mainWindow.moveTop();
      mainWindow.focus();
    }
  });

  app.whenReady().then(() => {
    if (process.platform === 'win32') {
      app.setAppUserModelId('com.jarvis.desktop');
    }
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
