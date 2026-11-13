const connDot = document.getElementById('conn-dot');
const modelBadge = document.getElementById('model-badge');
const orb = document.getElementById('jarvis-orb');
const stateLabel = document.getElementById('state-label');
const chatFeed = document.getElementById('chat-feed');
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
document.getElementById('btn-key').onclick = () => keyModal.classList.remove('hidden');
document.getElementById('btn-key-cancel').onclick = () => keyModal.classList.add('hidden');

keyForm.addEventListener('submit', (e) => {
  e.preventDefault();
  const key = apiKeyInput.value.trim();
  if (!key) return;
  sendCmd({ action: 'save_api_key', api_key: key, start_voice: true });
  apiKeyInput.value = '';
  keyModal.classList.add('hidden');
});

let ws = null;
let liveDraftEl = null;
let liveDraftRole = null;

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

  // Initial chat hydration on first connect
  if (chatFeed.children.length === 0 && snapshot.recent_messages) {
    for (const m of snapshot.recent_messages) {
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
      }
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
    } else if (data.type === 'error') {
      appendMessage('jarvis', `[Error] ${data.message}`, false);
    }
  };
}

connectBridge();
