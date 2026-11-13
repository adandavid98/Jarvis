const orb = document.getElementById('ov-orb');
const stateEl = document.getElementById('ov-state');
const toolEl = document.getElementById('ov-tool');
const textEl = document.getElementById('ov-text');

document.getElementById('ov-expand').onclick = () => window.jarvisDesktop.openFullHud();
document.getElementById('ov-close').onclick = () => window.jarvisDesktop.hideOverlay();

function connectOverlay() {
  const ws = new WebSocket(window.jarvisDesktop.bridgeUrl);

  ws.onclose = () => {
    setTimeout(connectOverlay, 1500);
  };

  ws.onmessage = (event) => {
    const data = JSON.parse(event.data);

    if (data.type === 'wake') {
      textEl.textContent = 'Listening...';
      toolEl.classList.add('hidden');
      window.jarvisDesktop.notifyWake();
    } else if (data.type === 'state') {
      const st = String(data.state || 'listening');
      orb.className = `orb ${st}`;
      stateEl.textContent = st.replace('_', ' ').toUpperCase();
      window.jarvisDesktop.notifyState(st);
    } else if (data.type === 'transcript') {
      const prefix = data.role === 'user' ? 'You: ' : 'Jarvis: ';
      textEl.textContent = `${prefix}${data.text}`;
    } else if (data.type === 'tool') {
      if (data.phase === 'start') {
        toolEl.textContent = `▶ ${data.name}`;
        toolEl.classList.remove('hidden');
      } else {
        toolEl.textContent = `✓ ${data.name}`;
        setTimeout(() => toolEl.classList.add('hidden'), 2000);
      }
    } else if (data.type === 'error') {
      textEl.textContent = `Error: ${data.message}`;
    }
  };
}

connectOverlay();
