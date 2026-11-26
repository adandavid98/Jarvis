/**
 * Shared Mark VIII "Obsidian Bioluminescent Core" Canvas Engine
 * Renders multi-layer organic plasma halo + 3D Fibonacci volumetric particle sphere
 * reactive to live microphone & TTS output audio energy.
 */
(function attachOrbEngine() {
  function buildFibonacciParticles(count) {
    const pts = [];
    const goldenAngle = Math.PI * (3 - Math.sqrt(5));
    for (let i = 0; i < count; i++) {
      const y = 1 - (i / Math.max(1, count - 1)) * 2;
      const r = Math.sqrt(Math.max(0, 1 - y * y));
      const theta = goldenAngle * i;
      pts.push({
        x: Math.cos(theta) * r,
        y,
        z: Math.sin(theta) * r,
        phase: (i * 1.6180339) % (Math.PI * 2),
        sizeBias: 0.65 + ((i * 37) % 100) / 110,
      });
    }
    return pts;
  }

  function paletteForState(state) {
    if (state === 'tool_running' || state === 'thinking') {
      return { primary: '255, 184, 38', secondary: '255, 128, 16', glow: '#ffb826' };
    }
    if (state === 'stopped' || state === 'offline' || state === 'needs_key') {
      return { primary: '255, 68, 114', secondary: '190, 28, 72', glow: '#ff4472' };
    }
    return { primary: '0, 238, 255', secondary: '0, 168, 255', glow: '#00eeff' };
  }

  function createJarvisOrbEngine(canvas, options = {}) {
    if (!canvas) return null;
    const ctx = canvas.getContext('2d');
    if (!ctx) return null;

    const particleCount = options.particleCount || 260;
    const particles = buildFibonacciParticles(particleCount);
    const haloLayers = options.haloLayers || 3;
    const haloPoints = options.haloPoints || 64;

    let state = 'connecting';
    let micLevel = 0;
    let outputLevel = 0;
    let smoothEnergy = 0.08;
    let rotY = 0;
    let rotX = 0.22;
    let phaseT = 0;
    let lastTs = 0;
    let lastRenderTs = 0;
    let rafId = null;
    let running = false;
    let viewerActive = false;

    function isPassiveState(s) {
      return s !== 'speaking' && s !== 'listening' && s !== 'tool_running' && s !== 'thinking';
    }

    function shouldFreezeIdle() {
      return viewerActive && isPassiveState(state);
    }

    function scheduleNext() {
      if (!running || rafId !== null) return;
      rafId = requestAnimationFrame(drawFrame);
    }

    function syncBackingSize() {
      const dpr = Math.min(1.25, window.devicePixelRatio || 1);
      const rect = canvas.getBoundingClientRect();
      const targetW = Math.max(40, Math.round((rect.width || canvas.width) * dpr));
      const targetH = Math.max(40, Math.round((rect.height || canvas.height) * dpr));
      if (canvas.width !== targetW || canvas.height !== targetH) {
        canvas.width = targetW;
        canvas.height = targetH;
      }
      return dpr;
    }

    function drawFrame(ts) {
      rafId = null;
      if (!running) {
        return;
      }
      if (typeof document !== 'undefined' && document.hidden) {
        return;
      }
      const isSpeaking = state === 'speaking';
      const isListening = state === 'listening';
      const isBusy = state === 'tool_running' || state === 'thinking';
      const isStopped = state === 'stopped' || state === 'offline' || state === 'needs_key';
      const isStandby = state === 'idle_wake';
      const minFrameMs = (isSpeaking || isListening || isBusy)
        ? 32
        : isStopped
          ? 180
          : isStandby
            ? 160
            : 64;
      if (lastRenderTs && (ts - lastRenderTs) < minFrameMs) {
        scheduleNext();
        return;
      }
      lastRenderTs = ts;
      if (!lastTs) lastTs = ts;
      const dt = Math.min(0.08, Math.max(0.001, (ts - lastTs) / 1000));
      lastTs = ts;

      syncBackingSize();
      const w = canvas.width;
      const h = canvas.height;
      const cx = w / 2;
      const cy = h / 2;
      const minDim = Math.min(w, h);

      const liveOut = outputLevel / 100;
      const liveMic = micLevel / 100;
      let targetEnergy = 0.08;
      if (isSpeaking) {
        targetEnergy = Math.max(0.32, liveOut > 0.02 ? liveOut * 1.15 : 0.42 + 0.25 * Math.sin(ts * 0.01));
      } else if (isListening) {
        targetEnergy = Math.max(0.16, liveMic * 1.1);
      } else if (isBusy) {
        targetEnergy = 0.36 + 0.14 * Math.sin(ts * 0.009);
      } else if (isStopped) {
        targetEnergy = 0.03;
      }
      smoothEnergy += (targetEnergy - smoothEnergy) * Math.min(1, dt * 11);

      const rotSpeed = isSpeaking ? 1.15 : isBusy ? 0.95 : isListening ? 0.55 : 0.22;
      rotY += dt * rotSpeed;
      rotX = 0.25 + Math.sin(ts * 0.0007) * 0.18;
      phaseT += dt * (1.4 + smoothEnergy * 3.8);

      const pal = paletteForState(state);
      ctx.clearRect(0, 0, w, h);

      const coreRadius = minDim * (0.29 + smoothEnergy * 0.025);
      const haloBaseRadius = minDim * (0.34 + smoothEnergy * 0.045);

      // Ambient outer radial bloom
      const ambient = ctx.createRadialGradient(cx, cy, coreRadius * 0.65, cx, cy, minDim * 0.49);
      ambient.addColorStop(0, `rgba(${pal.primary}, ${0.14 + smoothEnergy * 0.16})`);
      ambient.addColorStop(0.55, `rgba(${pal.secondary}, ${0.07 + smoothEnergy * 0.09})`);
      ambient.addColorStop(1, 'rgba(0, 0, 0, 0)');
      ctx.fillStyle = ambient;
      ctx.fillRect(0, 0, w, h);

      // Layer 1: Fluid Organic Plasma Halo Rings (screen/lighter composite)
      ctx.save();
      ctx.globalCompositeOperation = 'lighter';
      for (let layer = 0; layer < haloLayers; layer++) {
        const layerRatio = layer / Math.max(1, haloLayers - 1);
        const radius = haloBaseRadius + layer * (minDim * 0.024) * (0.7 + smoothEnergy * 0.7);
        const wobbleAmp = minDim * (0.012 + smoothEnergy * 0.042) * (0.65 + layerRatio * 0.65);
        const phaseOffset = layer * 1.45;
        const layerAlpha = isStopped ? 0.12 : Math.max(0.1, (0.38 - layerRatio * 0.18) + smoothEnergy * 0.26);

        ctx.beginPath();
        for (let i = 0; i <= haloPoints; i++) {
          const theta = (i / haloPoints) * Math.PI * 2;
          const wave =
            Math.sin(theta * 2 + phaseT * 1.1 + phaseOffset) * 0.55 +
            Math.sin(theta * 3 - phaseT * 1.6 + phaseOffset * 1.3) * 0.32 +
            Math.cos(theta * 5 + phaseT * 2.1 - phaseOffset) * 0.18;
          const r = radius + wave * wobbleAmp;
          const px = cx + Math.cos(theta) * r;
          const py = cy + Math.sin(theta) * r;
          if (i === 0) ctx.moveTo(px, py);
          else ctx.lineTo(px, py);
        }
        ctx.closePath();
        const baseLw = minDim * (0.030 - layerRatio * 0.010);
        const rgb = layer % 2 === 0 ? pal.primary : pal.secondary;
        // Soft wide glow pass + crisp inner core pass (zero shadowBlur CPU cost)
        ctx.lineWidth = baseLw * 2.2;
        ctx.strokeStyle = `rgba(${rgb}, ${(layerAlpha * 0.28).toFixed(3)})`;
        ctx.stroke();
        ctx.lineWidth = baseLw * 0.9;
        ctx.strokeStyle = `rgba(${rgb}, ${Math.min(0.85, layerAlpha * 0.75).toFixed(3)})`;
        ctx.stroke();
      }
      ctx.restore();

      // Layer 2: Obsidian Core Cavity (masks halo inside center so particles stay crisp)
      ctx.save();
      const cavityGrad = ctx.createRadialGradient(cx, cy, coreRadius * 0.15, cx, cy, coreRadius * 1.05);
      cavityGrad.addColorStop(0, '#04080c');
      cavityGrad.addColorStop(0.84, '#05090e');
      cavityGrad.addColorStop(1, 'rgba(5, 10, 15, 0.92)');
      ctx.fillStyle = cavityGrad;
      ctx.beginPath();
      ctx.arc(cx, cy, coreRadius * 1.02, 0, Math.PI * 2);
      ctx.fill();
      ctx.restore();

      // Subtle inner rim highlight
      ctx.save();
      ctx.strokeStyle = `rgba(${pal.primary}, ${0.22 + smoothEnergy * 0.35})`;
      ctx.lineWidth = Math.max(1, minDim * 0.004);
      ctx.beginPath();
      ctx.arc(cx, cy, coreRadius * 0.98, 0, Math.PI * 2);
      ctx.stroke();
      ctx.restore();

      // Layer 3: Batched 3D Fibonacci Particle Sphere (3 depth buckets = 3 fill() calls/frame)
      const cosY = Math.cos(rotY);
      const sinY = Math.sin(rotY);
      const cosX = Math.cos(rotX);
      const sinX = Math.sin(rotX);
      const sphereR = coreRadius * (0.74 + smoothEnergy * 0.12);
      const buckets = [[], [], []];

      for (let i = 0; i < particles.length; i++) {
        const p = particles[i];
        const pulse = 1 + Math.sin(phaseT * 2.1 + p.phase) * (0.04 + smoothEnergy * 0.16);
        const rx = p.x * pulse;
        const ry = p.y * pulse;
        const rz = p.z * pulse;

        // Rotate Y then X
        const x1 = rx * cosY - rz * sinY;
        const z1 = rx * sinY + rz * cosY;
        const y2 = ry * cosX - z1 * sinX;
        const z2 = ry * sinX + z1 * cosX;

        const perspective = 2.4 / (2.4 - z2 * 0.65);
        const sx = cx + x1 * sphereR * perspective;
        const sy = cy + y2 * sphereR * perspective;
        const depthNorm = (z2 + 1) * 0.5; // 0..1

        const dotRadius = Math.max(0.7, (minDim * 0.0038) * p.sizeBias * (0.55 + depthNorm * 0.85));
        const bucketIdx = depthNorm < 0.34 ? 0 : depthNorm < 0.68 ? 1 : 2;
        buckets[bucketIdx].push(sx, sy, dotRadius);
      }

      const bucketAlphas = isStopped
        ? [0.12, 0.20, 0.30]
        : [
            0.22,
            Math.min(0.62, 0.40 + smoothEnergy * 0.24),
            Math.min(0.95, 0.62 + smoothEnergy * 0.32),
          ];

      for (let b = 0; b < 3; b++) {
        const coords = buckets[b];
        if (!coords.length) continue;
        ctx.fillStyle = `rgba(${pal.primary}, ${bucketAlphas[b].toFixed(3)})`;
        ctx.beginPath();
        for (let k = 0; k < coords.length; k += 3) {
          const sx = coords[k];
          const sy = coords[k + 1];
          const r = coords[k + 2];
          ctx.moveTo(sx + r, sy);
          ctx.arc(sx, sy, r, 0, Math.PI * 2);
        }
        ctx.fill();
      }

      // Draw once in freeze-idle mode (e.g. standby while watching video in split viewer), then pause RAF
      if (!shouldFreezeIdle()) {
        scheduleNext();
      }
    }

    return {
      setState(nextState) {
        state = String(nextState || 'connecting');
        if (running && !shouldFreezeIdle()) {
          scheduleNext();
        }
      },
      setViewerActive(active) {
        viewerActive = Boolean(active);
        if (running) {
          scheduleNext();
        }
      },
      setAudio({ mic, output } = {}) {
        if (mic !== undefined) micLevel = Math.max(0, Math.min(100, Number(mic) || 0));
        if (output !== undefined) outputLevel = Math.max(0, Math.min(100, Number(output) || 0));
      },
      start() {
        if (running) return;
        running = true;
        lastTs = 0;
        scheduleNext();
      },
      stop() {
        running = false;
        if (rafId) {
          cancelAnimationFrame(rafId);
          rafId = null;
        }
      },
    };
  }

  window.createJarvisOrbEngine = createJarvisOrbEngine;
})();
