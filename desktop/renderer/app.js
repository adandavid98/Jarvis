const connDot = document.getElementById('conn-dot');
const modelBadge = document.getElementById('model-badge');
const orb = document.getElementById('jarvis-orb');
const stateLabel = document.getElementById('state-label');
const taskFeed = document.getElementById('task-feed');
const chatFeed = document.getElementById('chat-feed');
const tabTasks = document.getElementById('tab-tasks');
const tabRaw = document.getElementById('tab-raw');
const btnNewChat = document.getElementById('btn-new-chat');
const btnOpenBrowser = document.getElementById('btn-open-browser');
const hudLayout = document.querySelector('.hud-layout');
const centerWorkspace = document.getElementById('center-workspace');
const viewerPane = document.getElementById('viewer-pane');
const viewerResizer = document.getElementById('viewer-resizer');
const viewerExpandBtn = document.getElementById('viewer-expand');
const viewerHost = document.getElementById('viewer-host');
const viewerUrlForm = document.getElementById('viewer-url-form');
const viewerUrlInput = document.getElementById('viewer-url-input');
const chatForm = document.getElementById('chat-form');
const chatInput = document.getElementById('chat-input');
const factsList = document.getElementById('facts-list');
const remindersList = document.getElementById('reminders-list');
const toolsList = document.getElementById('tools-list');
const reminderForm = document.getElementById('reminder-form');
const reminderInput = document.getElementById('reminder-input');
const keyModal = document.getElementById('key-modal');
const keyForm = document.getElementById('key-form');
const apiKeyInput = document.getElementById('api-key-input');
const micBar = document.getElementById('mic-bar');
const hudClock = document.getElementById('hud-clock');
const stateSub = document.getElementById('state-sub');
const reactorCanvas = document.getElementById('reactor-canvas');
const btnSfx = document.getElementById('btn-sfx');
const btnLogs = document.getElementById('btn-logs');
const btnWorkspace = document.getElementById('btn-workspace');
const appShell = document.getElementById('app-shell');
const btnViewMode = document.getElementById('btn-view-mode');
const liveSubtitle = document.getElementById('live-subtitle');
const liveSubtitleText = document.getElementById('live-subtitle-text');
const cpuArc = document.getElementById('cpu-arc');
const ramArc = document.getElementById('ram-arc');
const RADIAL_CIRC = 175.93;

let sfxEnabled = localStorage.getItem('jarvis_sfx') !== 'off';
let audioCtx = null;
let currentHudState = 'connecting';
let targetMicLevel = 0;
let smoothMicLevel = 0;
let windowVisible = !document.hidden;
let reactorRaf = null;
let lastFrameTs = 0;
let reactorAngle = 0;

function updateSfxButton() {
  if (!btnSfx) return;
  btnSfx.textContent = sfxEnabled ? 'SFX: ON' : 'SFX: OFF';
  btnSfx.classList.toggle('muted', !sfxEnabled);
}
updateSfxButton();

function playHudSfx(kind) {
  if (!sfxEnabled) return;
  try {
    const Ctx = window.AudioContext || window.webkitAudioContext;
    if (!Ctx) return;
    if (!audioCtx) audioCtx = new Ctx();
    if (audioCtx.state === 'suspended') audioCtx.resume();
    const now = audioCtx.currentTime;
    const osc = audioCtx.createOscillator();
    const gain = audioCtx.createGain();
    osc.connect(gain);
    gain.connect(audioCtx.destination);
    if (kind === 'wake') {
      osc.type = 'sine';
      osc.frequency.setValueAtTime(520, now);
      osc.frequency.exponentialRampToValueAtTime(980, now + 0.14);
      gain.gain.setValueAtTime(0.05, now);
      gain.gain.exponentialRampToValueAtTime(0.001, now + 0.18);
      osc.start(now);
      osc.stop(now + 0.19);
    } else if (kind === 'done') {
      osc.type = 'triangle';
      osc.frequency.setValueAtTime(740, now);
      osc.frequency.exponentialRampToValueAtTime(1120, now + 0.11);
      gain.gain.setValueAtTime(0.035, now);
      gain.gain.exponentialRampToValueAtTime(0.001, now + 0.14);
      osc.start(now);
      osc.stop(now + 0.15);
    } else if (kind === 'error') {
      osc.type = 'sawtooth';
      osc.frequency.setValueAtTime(240, now);
      osc.frequency.exponentialRampToValueAtTime(140, now + 0.22);
      gain.gain.setValueAtTime(0.04, now);
      gain.gain.exponentialRampToValueAtTime(0.001, now + 0.24);
      osc.start(now);
      osc.stop(now + 0.25);
    }
  } catch (_) {}
}

function updateHudClock() {
  if (!hudClock || !windowVisible) return;
  const d = new Date();
  const hh = String(d.getHours()).padStart(2, '0');
  const mm = String(d.getMinutes()).padStart(2, '0');
  const ss = String(d.getSeconds()).padStart(2, '0');
  hudClock.textContent = `${hh}:${mm}:${ss}`;
}
updateHudClock();
setInterval(updateHudClock, 1000);

function setRadialGauge(arcEl, pct) {
  if (!arcEl) return;
  const clamped = Math.max(0, Math.min(100, Number(pct || 0)));
  arcEl.style.strokeDashoffset = (RADIAL_CIRC * (1 - clamped / 100)).toFixed(2);
  arcEl.classList.toggle('warn', clamped >= 85);
}

const orbEngine = window.createJarvisOrbEngine
  ? window.createJarvisOrbEngine(reactorCanvas, { particleCount: 280, haloLayers: 3 })
  : null;
let coreMode = false;
let subtitleHideTimer = null;

function setCoreMode(enabled) {
  coreMode = Boolean(enabled);
  if (appShell) appShell.classList.toggle('core-mode', coreMode);
  if (btnViewMode) {
    btnViewMode.textContent = coreMode ? 'HUD VIEW' : 'CORE VIEW';
    btnViewMode.classList.toggle('active', coreMode);
  }
  if (!coreMode && viewerOpenState) {
    requestAnimationFrame(() => syncViewerBounds());
  }
}

if (btnViewMode) {
  btnViewMode.onclick = () => setCoreMode(!coreMode);
}

function showLiveSubtitle(role, text, isFinal = false) {
  if (!liveSubtitle || !liveSubtitleText) return;
  const clean = String(text || '').trim();
  if (!clean) return;
  liveSubtitleText.textContent = role === 'user' ? `You: ${clean}` : clean;
  liveSubtitle.classList.toggle('user-voice', role === 'user');
  liveSubtitle.classList.remove('hidden');
  if (subtitleHideTimer) clearTimeout(subtitleHideTimer);
  if (isFinal) {
    subtitleHideTimer = setTimeout(() => {
      if (currentHudState === 'idle_wake' || currentHudState === 'stopped') {
        liveSubtitle.classList.add('hidden');
      }
    }, 6500);
  }
}

function setReactorVisibility(visible) {
  windowVisible = Boolean(visible) && !document.hidden;
  if (windowVisible) {
    updateHudClock();
    orbEngine?.start();
  } else {
    orbEngine?.stop();
  }
}

document.addEventListener('visibilitychange', () => setReactorVisibility(!document.hidden));
if (window.jarvisDesktop.onWindowVisibility) {
  window.jarvisDesktop.onWindowVisibility((visible) => setReactorVisibility(visible));
}
setReactorVisibility(!document.hidden);

document.getElementById('btn-min').onclick = () => window.jarvisDesktop.minimize();
document.getElementById('btn-max').onclick = () => window.jarvisDesktop.maximize();
document.getElementById('btn-close').onclick = () => window.jarvisDesktop.close();
document.getElementById('btn-update').onclick = () => window.jarvisDesktop.checkUpdates();
if (btnLogs && window.jarvisDesktop.openLogsFolder) {
  btnLogs.onclick = () => window.jarvisDesktop.openLogsFolder();
}
if (btnSfx) {
  btnSfx.onclick = () => {
    sfxEnabled = !sfxEnabled;
    localStorage.setItem('jarvis_sfx', sfxEnabled ? 'on' : 'off');
    updateSfxButton();
    if (sfxEnabled) playHudSfx('done');
  };
}
document.getElementById('btn-key').onclick = () => keyModal.classList.remove('hidden');
document.getElementById('btn-key-cancel').onclick = () => keyModal.classList.add('hidden');

const settingsModal = document.getElementById('settings-modal');
const settingsForm = document.getElementById('settings-form');
const setVoice = document.getElementById('set-voice');
const setWakeThreshold = document.getElementById('set-wake-threshold');
const setWakeVal = document.getElementById('set-wake-val');
const setSilenceMs = document.getElementById('set-silence-ms');
const setSilenceVal = document.getElementById('set-silence-val');
const setIdleSec = document.getElementById('set-idle-sec');
const setIdleVal = document.getElementById('set-idle-val');
const setBargeIn = document.getElementById('set-barge-in');
const setAutoApprove = document.getElementById('set-auto-approve');
const setWorkspace = document.getElementById('set-workspace');
const btnBrowseWs = document.getElementById('btn-browse-ws');
const reminderDue = document.getElementById('reminder-due');

function updateWorkspaceBadge(wsPath) {
  if (!wsPath) return;
  const parts = String(wsPath).replaceAll('/', '\\').split('\\').filter(Boolean);
  const folder = parts[parts.length - 1] || wsPath;
  if (btnWorkspace) {
    btnWorkspace.textContent = `WS: ${folder.slice(0, 16)}`;
    btnWorkspace.title = `Active Workspace: ${wsPath} (click to switch)`;
  }
  if (setWorkspace && document.activeElement !== setWorkspace) {
    setWorkspace.value = wsPath;
  }
}

if (btnWorkspace && window.jarvisDesktop.pickWorkspaceFolder) {
  btnWorkspace.onclick = async () => {
    const picked = await window.jarvisDesktop.pickWorkspaceFolder();
    if (picked) {
      updateWorkspaceBadge(picked);
      sendCmd({ action: 'set_workspace', path: picked });
      playHudSfx('done');
    }
  };
}

if (btnBrowseWs && window.jarvisDesktop.pickWorkspaceFolder) {
  btnBrowseWs.onclick = async () => {
    const picked = await window.jarvisDesktop.pickWorkspaceFolder();
    if (picked && setWorkspace) {
      setWorkspace.value = picked;
    }
  };
}

function applySettingsToForm(s) {
  if (!s) return;
  if (setVoice && s.voice_name) setVoice.value = s.voice_name;
  if (setWakeThreshold && s.wake_threshold !== undefined) {
    setWakeThreshold.value = String(s.wake_threshold);
    if (setWakeVal) setWakeVal.textContent = Number(s.wake_threshold).toFixed(2);
  }
  if (setSilenceMs && s.silence_duration_ms !== undefined) {
    setSilenceMs.value = String(s.silence_duration_ms);
    if (setSilenceVal) setSilenceVal.textContent = `${s.silence_duration_ms} ms`;
  }
  if (setIdleSec && s.idle_timeout_sec !== undefined) {
    setIdleSec.value = String(s.idle_timeout_sec);
    if (setIdleVal) setIdleVal.textContent = `${s.idle_timeout_sec} s`;
  }
  if (setBargeIn && s.barge_in_enabled !== undefined) {
    setBargeIn.checked = Boolean(s.barge_in_enabled);
  }
  if (setAutoApprove && s.auto_approve_dangerous !== undefined) {
    setAutoApprove.checked = Boolean(s.auto_approve_dangerous);
  }
  if (s.workspace_dir) {
    updateWorkspaceBadge(s.workspace_dir);
  }
}

if (setWakeThreshold) {
  setWakeThreshold.oninput = () => {
    if (setWakeVal) setWakeVal.textContent = Number(setWakeThreshold.value).toFixed(2);
  };
}
if (setSilenceMs) {
  setSilenceMs.oninput = () => {
    if (setSilenceVal) setSilenceVal.textContent = `${setSilenceMs.value} ms`;
  };
}
if (setIdleSec) {
  setIdleSec.oninput = () => {
    if (setIdleVal) setIdleVal.textContent = `${setIdleSec.value} s`;
  };
}

const btnSettings = document.getElementById('btn-settings');
if (btnSettings && settingsModal) {
  btnSettings.onclick = () => {
    sendCmd({ action: 'get_settings' });
    settingsModal.classList.remove('hidden');
  };
}
const btnSettingsCancel = document.getElementById('btn-settings-cancel');
if (btnSettingsCancel && settingsModal) {
  btnSettingsCancel.onclick = () => settingsModal.classList.add('hidden');
}
if (settingsForm) {
  settingsForm.addEventListener('submit', (e) => {
    e.preventDefault();
    sendCmd({
      action: 'save_settings',
      settings: {
        voice_name: setVoice?.value || 'Fenrir',
        wake_threshold: Number(setWakeThreshold?.value || 0.5),
        silence_duration_ms: Number(setSilenceMs?.value || 1100),
        idle_timeout_sec: Number(setIdleSec?.value || 15),
        barge_in_enabled: Boolean(setBargeIn?.checked),
        auto_approve_dangerous: Boolean(setAutoApprove?.checked),
        workspace_dir: setWorkspace?.value?.trim() || undefined,
      },
    });
    settingsModal?.classList.add('hidden');
  });
}

keyForm.addEventListener('submit', (e) => {
  e.preventDefault();
  const key = apiKeyInput.value.trim();
  if (!key) return;
  if (!ws || ws.readyState !== WebSocket.OPEN) {
    appendMessage('jarvis', '[Error] Bridge is not connected yet. Wait a moment and try again.', false);
    return;
  }
  sendCmd({ action: 'save_api_key', api_key: key, start_voice: true });
  apiKeyInput.value = '';
  keyModal.classList.add('hidden');
});

const MAX_RECENT_ITEMS = 10;
let ws = null;
let liveDraftEl = null;
let liveDraftRole = null;
let taskRuns = [];
let viewerOpenState = false;
let viewerExpanded = false;

function setFeedMode(mode) {
  const showTasks = mode === 'tasks';
  tabTasks.classList.toggle('active', showTasks);
  tabRaw.classList.toggle('active', !showTasks);
  taskFeed.classList.toggle('hidden', !showTasks);
  chatFeed.classList.toggle('hidden', showTasks);
}

tabTasks.onclick = () => setFeedMode('tasks');
tabRaw.onclick = () => setFeedMode('raw');

btnNewChat.onclick = () => {
  sendCmd({ action: 'new_chat' });
  taskRuns = [];
  renderTaskFeed();
  chatFeed.innerHTML = '';
  liveDraftEl = null;
  liveDraftRole = null;
};

function syncViewerBounds() {
  if (!viewerOpenState || viewerPane.classList.contains('hidden')) return;
  const rect = viewerHost.getBoundingClientRect();
  if (rect.width > 10 && rect.height > 10) {
    window.jarvisDesktop.viewerSetBounds({
      x: rect.left,
      y: rect.top,
      width: rect.width,
      height: rect.height,
    });
  }
}

function openViewer(url) {
  if (coreMode) setCoreMode(false);
  hudLayout?.classList.add('viewer-active');
  viewerPane.classList.remove('hidden');
  viewerResizer.classList.remove('hidden');
  viewerOpenState = true;
  orbEngine?.setViewerActive?.(true);
  if (url) viewerUrlInput.value = url;
  window.jarvisDesktop.viewerOpen(url || 'https://duckduckgo.com');
  requestAnimationFrame(() => syncViewerBounds());
}

function closeViewer() {
  viewerOpenState = false;
  orbEngine?.setViewerActive?.(false);
  viewerExpanded = false;
  hudLayout?.classList.remove('viewer-active');
  centerWorkspace.classList.remove('viewer-expanded');
  viewerExpandBtn.textContent = '⤢';
  viewerPane.classList.add('hidden');
  viewerResizer.classList.add('hidden');
  viewerPane.style.flex = '';
  viewerPane.style.width = '';
  window.jarvisDesktop.viewerClose();
}

viewerExpandBtn.onclick = () => {
  viewerExpanded = !viewerExpanded;
  centerWorkspace.classList.toggle('viewer-expanded', viewerExpanded);
  viewerExpandBtn.textContent = viewerExpanded ? '⤡' : '⤢';
  requestAnimationFrame(() => syncViewerBounds());
};

let isResizingViewer = false;
viewerResizer.addEventListener('mousedown', (e) => {
  e.preventDefault();
  isResizingViewer = true;
  viewerResizer.classList.add('dragging');
});

window.addEventListener('mousemove', (e) => {
  if (!isResizingViewer || viewerExpanded) return;
  const wsRect = centerWorkspace.getBoundingClientRect();
  const newWidth = Math.max(260, Math.min(wsRect.width - 220, wsRect.right - e.clientX));
  viewerPane.style.flex = 'none';
  viewerPane.style.width = `${newWidth}px`;
  syncViewerBounds();
});

window.addEventListener('mouseup', () => {
  if (!isResizingViewer) return;
  isResizingViewer = false;
  viewerResizer.classList.remove('dragging');
  syncViewerBounds();
});

btnOpenBrowser.onclick = () => {
  if (viewerOpenState) closeViewer();
  else openViewer(viewerUrlInput.value.trim() || 'https://duckduckgo.com');
};

document.getElementById('viewer-back').onclick = () => window.jarvisDesktop.viewerNavigate('back');
document.getElementById('viewer-fwd').onclick = () => window.jarvisDesktop.viewerNavigate('forward');
document.getElementById('viewer-reload').onclick = () => window.jarvisDesktop.viewerNavigate('reload');
document.getElementById('viewer-ext').onclick = () => window.jarvisDesktop.viewerOpenExternal(viewerUrlInput.value.trim());
document.getElementById('viewer-close').onclick = closeViewer;

viewerUrlForm.addEventListener('submit', (e) => {
  e.preventDefault();
  const target = viewerUrlInput.value.trim();
  if (!target) return;
  openViewer(target);
});

if (typeof ResizeObserver !== 'undefined') {
  new ResizeObserver(() => syncViewerBounds()).observe(viewerHost);
}
window.addEventListener('resize', syncViewerBounds);

window.jarvisDesktop.onViewerNavigated((nav) => {
  if (nav?.url) viewerUrlInput.value = nav.url;
});

function actionIcon(kind) {
  switch (kind) {
    case 'web_search': return '🔍';
    case 'open_url': return '🌐';
    case 'file_edit': return '📝';
    case 'command': return '💻';
    case 'memory': return '🧠';
    case 'system': return '🎛️';
    default: return '⚡';
  }
}

function renderTaskFeed() {
  taskFeed.innerHTML = '';
  const visible = taskRuns
    .filter((t) => t.actions && t.actions.length > 0)
    .slice(-MAX_RECENT_ITEMS);

  if (visible.length === 0) {
    taskFeed.innerHTML = '<div class="empty-hint">No tasks yet. Give Jarvis a command by voice or text.</div>';
    return;
  }

  for (const task of visible) {
    const card = document.createElement('div');
    card.className = 'task-card';

    const header = document.createElement('div');
    header.className = 'task-card-header';
    const promptEl = document.createElement('div');
    promptEl.className = 'task-prompt';
    promptEl.textContent = task.prompt;
    const statusEl = document.createElement('span');
    statusEl.className = `task-status ${task.status || 'running'}`;
    statusEl.textContent = (task.status || 'running').toUpperCase();
    header.append(promptEl, statusEl);
    card.appendChild(header);

    if (task.actions && task.actions.length > 0) {
      const actionsEl = document.createElement('div');
      actionsEl.className = 'task-actions-list';
      for (const act of task.actions) {
        const row = document.createElement('div');
        row.className = 'task-action-row';
        const label = document.createElement('span');
        const stateMark = act.status === 'running' ? '⏳' : act.status === 'error' ? '✕' : '✓';
        label.textContent = `${actionIcon(act.kind)} ${act.title} (${stateMark})`;
        row.appendChild(label);

        if (act.target && /^https?:\/\//i.test(act.target)) {
          const openBtn = document.createElement('button');
          openBtn.type = 'button';
          openBtn.className = 'task-action-link';
          openBtn.textContent = 'View';
          openBtn.onclick = () => openViewer(act.target);
          row.appendChild(openBtn);
        }
        actionsEl.appendChild(row);
      }
      card.appendChild(actionsEl);
    }

    if (task.summary) {
      const summaryEl = document.createElement('div');
      summaryEl.className = 'task-summary';
      summaryEl.textContent = task.summary;
      card.appendChild(summaryEl);
    }

    taskFeed.appendChild(card);
  }
  taskFeed.scrollTop = taskFeed.scrollHeight;
}

function upsertTask(task) {
  if (!task || !task.task_id) return;
  const idx = taskRuns.findIndex((t) => t.task_id === task.task_id);
  if (idx >= 0) taskRuns[idx] = task;
  else taskRuns.push(task);
  if (taskRuns.length > MAX_RECENT_ITEMS) {
    taskRuns = taskRuns.slice(-MAX_RECENT_ITEMS);
  }
  renderTaskFeed();
}

function sendCmd(payload) {
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify(payload));
  }
}

document.getElementById('btn-wakeword').onclick = () =>
  sendCmd({ action: 'start_voice', mode: 'wakeword' });
document.getElementById('btn-direct').onclick = () =>
  sendCmd({ action: 'start_voice', mode: 'direct' });
document.getElementById('btn-stop').onclick = () =>
  sendCmd({ action: 'stop_voice' });

orb.onclick = () => {
  if (currentHudState === 'listening' || currentHudState === 'speaking') {
    sendCmd({ action: 'standby' });
  } else {
    sendCmd({ action: 'wake_now' });
  }
};

chatForm.addEventListener('submit', (e) => {
  e.preventDefault();
  const text = chatInput.value.trim();
  if (!text) return;
  sendCmd({ action: 'send_text', text });
  chatInput.value = '';
});

reminderForm.addEventListener('submit', (e) => {
  e.preventDefault();
  const title = reminderInput.value.trim();
  if (!title) {
    reminderInput.focus();
    return;
  }
  const due_at = reminderDue ? reminderDue.value.trim() : '';
  sendCmd({ action: 'add_reminder', title, due_at });
  reminderInput.value = '';
  if (reminderDue) reminderDue.value = '';
});

const STATE_META = {
  idle_wake: { label: 'STANDBY', sub: 'WAKE WORD // SAY "HEY JARVIS"', amber: false },
  loading_wakeword: { label: 'INIT CORE', sub: 'LOADING WAKE WORD MODEL', amber: true },
  listening: { label: 'LISTENING', sub: 'VOICE LINK ACTIVE', amber: false },
  speaking: { label: 'SPEAKING', sub: 'AUDIO SYNTHESIS ACTIVE', amber: false },
  tool_running: { label: 'EXECUTING', sub: 'SUBSYSTEM TOOL ACTIVE', amber: true },
  thinking: { label: 'ANALYZING', sub: 'NEURAL CORE REASONING', amber: true },
  stopped: { label: 'PAUSED', sub: 'VOICE SUBSYSTEM STOPPED', amber: false },
  offline: { label: 'OFFLINE', sub: 'RECONNECTING TO BRIDGE...', amber: false },
  needs_key: { label: 'NEEDS KEY', sub: 'API KEY REQUIRED', amber: true },
};

function setState(state) {
  const prev = currentHudState;
  currentHudState = String(state || 'stopped');
  orb.className = `orb-core ${currentHudState}`;
  orbEngine?.setState(currentHudState);
  const meta = STATE_META[currentHudState] || {
    label: currentHudState.replace('_', ' ').toUpperCase(),
    sub: 'TACTICAL LINK ACTIVE',
    amber: false,
  };
  stateLabel.textContent = meta.label;
  if (stateSub) {
    stateSub.textContent = meta.sub;
    stateSub.classList.toggle('amber', Boolean(meta.amber));
  }
  if (prev === 'idle_wake' && currentHudState === 'listening') {
    playHudSfx('wake');
  }
}

let pendingDraftUpdate = null;
let draftFlushScheduled = false;

function scheduleDraftFlush() {
  if (draftFlushScheduled) return;
  draftFlushScheduled = true;
  requestAnimationFrame(() => {
    draftFlushScheduled = false;
    if (!pendingDraftUpdate) return;
    const { el } = pendingDraftUpdate;
    const text = pendingDraftUpdate.text;
    pendingDraftUpdate = null;
    const body = el.querySelector('.msg-body');
    if (body) body.textContent = text;
    chatFeed.scrollTop = chatFeed.scrollHeight;
  });
}

function appendMessage(role, text, isDraft = false) {
  if (isDraft && liveDraftEl && liveDraftRole === role) {
    pendingDraftUpdate = { el: liveDraftEl, text };
    scheduleDraftFlush();
    return;
  }

  if (!isDraft && liveDraftEl && liveDraftRole === role) {
    pendingDraftUpdate = null;
    liveDraftEl.querySelector('.msg-body').textContent = text;
    liveDraftEl = null;
    liveDraftRole = null;
    chatFeed.scrollTop = chatFeed.scrollHeight;
    return;
  }

  if (!isDraft) pendingDraftUpdate = null;

  const div = document.createElement('div');
  div.className = `msg ${role}`;
  const roleEl = document.createElement('div');
  roleEl.className = 'msg-role';
  roleEl.textContent = role === 'user' ? 'YOU' : 'JARVIS';
  const bodyEl = document.createElement('div');
  bodyEl.className = 'msg-body';
  bodyEl.textContent = text;
  div.append(roleEl, bodyEl);
  chatFeed.appendChild(div);
  while (chatFeed.children.length > MAX_RECENT_ITEMS) {
    chatFeed.firstChild.remove();
  }
  chatFeed.scrollTop = chatFeed.scrollHeight;

  if (isDraft) {
    liveDraftEl = div;
    liveDraftRole = role;
  }
}

function renderMemory(snapshot) {
  // Facts
  factsList.innerHTML = '';
  if (!snapshot.facts || snapshot.facts.length === 0) {
    factsList.innerHTML = '<div class="empty-hint">No facts saved yet.</div>';
  } else {
    for (const f of snapshot.facts) {
      const card = document.createElement('div');
      card.className = 'card-item fact-item';

      const top = document.createElement('div');
      top.className = 'fact-top';
      const titleSpan = document.createElement('span');
      titleSpan.innerHTML = `<strong>${escapeHtml(f.key)}</strong> <span style="opacity:.6">(${escapeHtml(f.category)})</span>`;

      const actions = document.createElement('div');
      actions.className = 'fact-actions';
      const editBtn = document.createElement('button');
      editBtn.type = 'button';
      editBtn.textContent = '✎';
      editBtn.title = 'Edit fact';
      const delBtn = document.createElement('button');
      delBtn.type = 'button';
      delBtn.className = 'del';
      delBtn.textContent = '✕';
      delBtn.title = 'Delete fact';
      actions.append(editBtn, delBtn);
      top.append(titleSpan, actions);

      const valDiv = document.createElement('div');
      valDiv.textContent = f.value;

      delBtn.onclick = () => sendCmd({ action: 'delete_fact', key: f.key });
      editBtn.onclick = () => {
        if (card.querySelector('.fact-edit-row')) return;
        const row = document.createElement('form');
        row.className = 'fact-edit-row';
        const inp = document.createElement('input');
        inp.type = 'text';
        inp.value = f.value;
        const saveBtn = document.createElement('button');
        saveBtn.type = 'submit';
        saveBtn.className = 'ctrl-btn primary';
        saveBtn.textContent = '✓';
        row.append(inp, saveBtn);
        row.onsubmit = (ev) => {
          ev.preventDefault();
          const newVal = inp.value.trim();
          if (newVal) {
            sendCmd({ action: 'remember_fact', key: f.key, value: newVal, category: f.category });
          }
        };
        card.appendChild(row);
        inp.focus();
      };

      card.append(top, valDiv);
      factsList.appendChild(card);
    }
  }

  // Reminders
  remindersList.innerHTML = '';
  if (!snapshot.reminders || snapshot.reminders.length === 0) {
    remindersList.innerHTML = '<div class="empty-hint">No outstanding reminders.</div>';
  } else {
    for (const r of snapshot.reminders) {
      const card = document.createElement('div');
      card.className = 'card-item reminder-item';
      const txt = document.createElement('span');
      txt.textContent = r.due_at ? `${r.title} (${r.due_at})` : r.title;
      const doneBtn = document.createElement('button');
      doneBtn.textContent = 'DONE';
      doneBtn.onclick = () => sendCmd({ action: 'complete_reminder', id: r.id });
      card.append(txt, doneBtn);
      remindersList.appendChild(card);
    }
  }

  // Initial chat & task hydration on first connect
  if (snapshot.recent_tasks) {
    taskRuns = snapshot.recent_tasks.slice(-MAX_RECENT_ITEMS);
    renderTaskFeed();
  }
  if (chatFeed.children.length === 0 && snapshot.recent_messages) {
    for (const m of snapshot.recent_messages.slice(-MAX_RECENT_ITEMS)) {
      appendMessage(m.role === 'user' ? 'user' : 'jarvis', m.content, false);
    }
  }
}

function addToolLog(ev) {
  if (toolsList.querySelector('.empty-hint')) {
    toolsList.innerHTML = '';
  }
  const card = document.createElement('div');
  card.className = 'card-item';
  const label = ev.phase === 'start'
    ? `▶ ${ev.name}`
    : `✓ ${ev.name} (${ev.status})`;
  card.textContent = label;
  toolsList.prepend(card);
  while (toolsList.children.length > 25) {
    toolsList.lastChild.remove();
  }
}

function escapeHtml(str) {
  return String(str)
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;');
}

function connectBridge() {
  ws = new WebSocket(window.jarvisDesktop.bridgeUrl);

  ws.onopen = () => {
    connDot.classList.add('online');
    sendCmd({ action: 'refresh_memory' });
  };

  ws.onclose = () => {
    connDot.classList.remove('online');
    setState('offline');
    setTimeout(connectBridge, 1500);
  };

  ws.onmessage = (event) => {
    const data = JSON.parse(event.data);
    if (data.type === 'state') {
      setState(data.state);
    } else if (data.type === 'needs_api_key') {
      if (data.missing) {
        keyModal.classList.remove('hidden');
        setState('needs_key');
      } else {
        keyModal.classList.add('hidden');
        if (stateLabel.textContent === 'NEEDS KEY') setState('stopped');
      }
    } else if (data.type === 'api_key_saved') {
      keyModal.classList.add('hidden');
      if (stateLabel.textContent === 'NEEDS KEY') setState('stopped');
    } else if (data.type === 'mic_level') {
      targetMicLevel = Math.max(0, Math.min(100, Number(data.level || 0)));
      if (micBar) micBar.style.width = `${targetMicLevel}%`;
      orbEngine?.setAudio({ mic: targetMicLevel, output: Number(data.output_level || 0) });
    } else if (data.type === 'telemetry') {
      document.getElementById('cpu-val').textContent = `${data.cpu_percent}%`;
      document.getElementById('cpu-bar').style.width = `${Math.min(100, data.cpu_percent)}%`;
      setRadialGauge(cpuArc, data.cpu_percent);
      document.getElementById('ram-val').textContent = `${data.memory_percent}%`;
      document.getElementById('ram-bar').style.width = `${Math.min(100, data.memory_percent)}%`;
      setRadialGauge(ramArc, data.memory_percent);
      if (data.jarvis_cpu_percent !== undefined) {
        const jCpu = Number(data.jarvis_cpu_percent || 0);
        const jValEl = document.getElementById('jarvis-cpu-val');
        const jBarEl = document.getElementById('jarvis-cpu-bar');
        const jBridgeEl = document.getElementById('jarvis-bridge-cpu');
        const jUiEl = document.getElementById('jarvis-ui-cpu');
        const jProcEl = document.getElementById('jarvis-proc-cnt');
        if (jValEl) jValEl.textContent = `${jCpu.toFixed(1)}%`;
        if (jBarEl) {
          jBarEl.style.width = `${Math.min(100, jCpu * 2.5)}%`;
          jBarEl.classList.toggle('warn', jCpu >= 15);
        }
        if (jBridgeEl) jBridgeEl.textContent = `${Number(data.jarvis_bridge_cpu || 0).toFixed(1)}%`;
        if (jUiEl) jUiEl.textContent = `${Number(data.jarvis_ui_cpu || 0).toFixed(1)}%`;
        if (jProcEl) jProcEl.textContent = `${Number(data.jarvis_process_count || 1)} procs`;
      }
      document.getElementById('disk-val').textContent = `${data.disk_free_gb} GB`;
      document.getElementById('bat-val').textContent = data.battery
        ? `${data.battery.percent}%${data.battery.plugged_in ? ' ⚡' : ''}`
        : 'AC';
      if (data.model) modelBadge.textContent = data.model.toUpperCase();
      if (data.workspace_dir) updateWorkspaceBadge(data.workspace_dir);
    } else if (data.type === 'settings_snapshot') {
      applySettingsToForm(data.settings);
    } else if (data.type === 'settings_saved') {
      applySettingsToForm(data.settings);
      playHudSfx('done');
    } else if (data.type === 'reminder_due') {
      playHudSfx('wake');
      const rem = data.reminder || {};
      appendMessage('jarvis', `⏰ Reminder: ${rem.title || 'Scheduled item'}${rem.due_at ? ` (${rem.due_at})` : ''}`, false);
      if (window.jarvisDesktop.notifyReminder) {
        window.jarvisDesktop.notifyReminder(rem);
      }
    } else if (data.type === 'memory_snapshot') {
      renderMemory(data);
    } else if (data.type === 'transcript') {
      const role = data.role === 'user' ? 'user' : 'jarvis';
      appendMessage(role, data.text, !data.final);
      showLiveSubtitle(role, data.text, Boolean(data.final));
    } else if (data.type === 'tool') {
      addToolLog(data);
      if (data.phase === 'end' && data.status === 'ok') playHudSfx('done');
    } else if (data.type === 'task_event') {
      upsertTask(data.task);
    } else if (data.type === 'open_viewer') {
      if (data.url) openViewer(data.url);
    } else if (data.type === 'chat_cleared') {
      taskRuns = [];
      renderTaskFeed();
      chatFeed.innerHTML = '';
      liveDraftEl = null;
      liveDraftRole = null;
    } else if (data.type === 'error') {
      playHudSfx('error');
      appendMessage('jarvis', `[Error] ${data.message}`, false);
    }
  };
}

connectBridge();
