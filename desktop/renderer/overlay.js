const ovCanvas = document.getElementById('ov-canvas');
const ovOrbEngine = window.createJarvisOrbEngine
  ? window.createJarvisOrbEngine(ovCanvas, { particleCount: 140, haloLayers: 3, haloPoints: 64 })
  : null;
const stateEl = document.getElementById('ov-state');
const toolEl = document.getElementById('ov-tool');
const textEl = document.getElementById('ov-text');
const barsWrap = document.getElementById('ov-bars');
const bars = barsWrap ? Array.from(barsWrap.querySelectorAll('span')) : [];

let overlayVisible = false;
let cachedState = 'idle_wake';
let cachedText = 'Ready.';

if (window.jarvisDesktop.onWindowVisibility) {
  window.jarvisDesktop.onWindowVisibility((visible) => {
    overlayVisible = Boolean(visible);
    if (overlayVisible) {
      stateEl.textContent = cachedState.replace('_', ' ').toUpperCase();
      textEl.textContent = cachedText;
      ovOrbEngine?.setState(cachedState);
      ovOrbEngine?.start();
    } else {
      ovOrbEngine?.stop();
    }
  });
}

document.getElementById('ov-expand').onclick = () => window.jarvisDesktop.openFullHud();
document.getElementById('ov-close').onclick = () => window.jarvisDesktop.hideOverlay();

function updateBars(levelPercent) {
  if (!bars.length) return;
  const lvl = Math.max(0, Math.min(100, Number(levelPercent || 0)));
  bars.forEach((bar, idx) => {
    const factor = 1 - Math.abs(idx - 2) * 0.22;
    const h = Math.max(2, Math.round(2 + (lvl / 100) * 9 * factor));
    bar.style.height = `${h}px`;
  });
}

function connectOverlay() {
  const ws = new WebSocket(window.jarvisDesktop.bridgeUrl);

  ws.onclose = () => {
    setTimeout(connectOverlay, 1500);
  };

  ws.onmessage = (event) => {
    const data = JSON.parse(event.data);

    if (data.type === 'wake') {
      cachedState = 'listening';
      cachedText = 'Listening...';
      if (overlayVisible) {
        textEl.textContent = cachedText;
        toolEl.classList.add('hidden');
        ovOrbEngine?.setState('listening');
      }
      window.jarvisDesktop.notifyWake();
    } else if (data.type === 'state') {
      cachedState = String(data.state || 'listening');
      if (overlayVisible) {
        ovOrbEngine?.setState(cachedState);
        stateEl.textContent = cachedState.replace('_', ' ').toUpperCase();
      }
      window.jarvisDesktop.notifyState(cachedState);
    } else if (data.type === 'mic_level') {
      if (!overlayVisible) return;
      const outLvl = Number(data.output_level || 0);
      const micLvl = Number(data.level || 0);
      updateBars(Math.max(micLvl, outLvl));
      ovOrbEngine?.setAudio({ mic: micLvl, output: outLvl });
    } else if (data.type === 'transcript') {
      const prefix = data.role === 'user' ? 'You: ' : 'Jarvis: ';
      cachedText = `${prefix}${data.text}`;
      if (overlayVisible) textEl.textContent = cachedText;
    } else if (data.type === 'tool') {
      if (!overlayVisible) return;
      if (data.phase === 'start') {
        toolEl.textContent = `▶ ${data.name}`;
        toolEl.classList.remove('hidden');
      } else {
        toolEl.textContent = `✓ ${data.name}`;
        setTimeout(() => toolEl.classList.add('hidden'), 2000);
      }
    } else if (data.type === 'error') {
      cachedText = `Error: ${data.message}`;
      if (overlayVisible) textEl.textContent = cachedText;
    }
  };
}

connectOverlay();
