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

document.getElementById('btn-min').onclick = () => window.jarvisDesktop.minimize();
document.getElementById('btn-max').onclick = () => window.jarvisDesktop.maximize();
document.getElementById('btn-close').onclick = () => window.jarvisDesktop.close();
document.getElementById('btn-update').onclick = () => window.jarvisDesktop.checkUpdates();
document.getElementById('btn-key').onclick = () => keyModal.classList.remove('hidden');
document.getElementById('btn-key-cancel').onclick = () => keyModal.classList.add('hidden');

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
  hudLayout?.classList.add('viewer-active');
  viewerPane.classList.remove('hidden');
  viewerResizer.classList.remove('hidden');
  viewerOpenState = true;
  if (url) viewerUrlInput.value = url;
  window.jarvisDesktop.viewerOpen(url || 'https://duckduckgo.com');
  requestAnimationFrame(() => syncViewerBounds());
}

function closeViewer() {
  viewerOpenState = false;
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
  sendCmd({ action: 'add_reminder', title });
  reminderInput.value = '';
});

function setState(state) {
  orb.className = `orb-core ${state}`;
  stateLabel.textContent = state.replace('_', ' ').toUpperCase();
}

function appendMessage(role, text, isDraft = false) {
  if (isDraft && liveDraftEl && liveDraftRole === role) {
    liveDraftEl.querySelector('.msg-body').textContent = text;
    chatFeed.scrollTop = chatFeed.scrollHeight;
    return;
  }

  if (!isDraft && liveDraftEl && liveDraftRole === role) {
    liveDraftEl.querySelector('.msg-body').textContent = text;
    liveDraftEl = null;
    liveDraftRole = null;
    chatFeed.scrollTop = chatFeed.scrollHeight;
    return;
  }

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
      card.className = 'card-item';
      card.innerHTML = `<strong>${escapeHtml(f.key)}</strong> <span style="opacity:.6">(${escapeHtml(f.category)})</span><br>${escapeHtml(f.value)}`;
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
    } else if (data.type === 'telemetry') {
      document.getElementById('cpu-val').textContent = `${data.cpu_percent}%`;
      document.getElementById('cpu-bar').style.width = `${Math.min(100, data.cpu_percent)}%`;
      document.getElementById('ram-val').textContent = `${data.memory_percent}%`;
      document.getElementById('ram-bar').style.width = `${Math.min(100, data.memory_percent)}%`;
      document.getElementById('disk-val').textContent = `${data.disk_free_gb} GB`;
      document.getElementById('bat-val').textContent = data.battery
        ? `${data.battery.percent}%${data.battery.plugged_in ? ' ⚡' : ''}`
        : 'AC';
      if (data.model) modelBadge.textContent = data.model.toUpperCase();
    } else if (data.type === 'memory_snapshot') {
      renderMemory(data);
    } else if (data.type === 'transcript') {
      appendMessage(data.role === 'user' ? 'user' : 'jarvis', data.text, !data.final);
    } else if (data.type === 'tool') {
      addToolLog(data);
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
      appendMessage('jarvis', `[Error] ${data.message}`, false);
    }
  };
}

connectBridge();
