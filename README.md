# Jarvis — Windows Voice Automation Assistant

A modular, low-latency **"Jarvis"** voice automation prototype for **Windows**, powered by:

1. **Local Wake Word Detection (`openWakeWord`)** — listens locally on your microphone (`"Hey Jarvis"`) using minimal CPU so audio isn't streamed to the cloud while idle.
2. **Gemini Live API (`google-genai`)** — real-time bidirectional 16 kHz / 24 kHz PCM voice conversation over WebSockets with native barge-in interruption, live transcriptions, context window compression, session resumption, and `gemini-3.8-live-extended-thinking` async reasoning.
3. **Async Automation Tools (`NON_BLOCKING`)** —
   - **Desktop / OS Control:** system status (CPU, RAM, disk, battery, time), open apps/URLs, run PowerShell commands (with safety confirmation guardrails), list/read/write files.
   - **Productivity & Memory:** SQLite-backed long-term memory (`remember_fact`, `recall_facts`), persistent reminders (`manage_reminders`), and JSON webhooks (`trigger_webhook` for n8n or custom automations).
   - **Developer Assistant:** fast workspace code search (`search_code`), git inspection (`git_inspect`), and test execution (`run_tests`).

---

## Setup

### 1. Configure Environment

Copy `.env.example` to `.env` and add your Gemini API key:

```powershell
Copy-Item .env.example .env
```

Edit `.env` and set `GEMINI_API_KEY=...` (get a key from [Google AI Studio](https://aistudio.google.com/apikey)).

### 2. Run Self-Check

Verify audio devices, wake-word dependencies, and registered tools without making network calls:

```powershell
.venv\Scripts\python.exe -m jarvis.main --check
```

### 3. Start Jarvis

- **Windows Desktop HUD App (Electron + Voice + Text + Synced Memory):**
  ```powershell
  cd C:\Users\SMS-ADAN\Documents\Jarvis\desktop
  npm.cmd start
  ```

- **Wake Word + Voice Mode (CLI):**
  Listens locally for `"Hey Jarvis"`, opens a Gemini Live session when triggered, and returns to idle listening after `JARVIS_IDLE_TIMEOUT_SEC` seconds of inactivity:
  ```powershell
  .venv\Scripts\python.exe -m jarvis.main
  ```

- **Direct Voice Mode (skip wake word):**
  ```powershell
  .venv\Scripts\python.exe -m jarvis.main --no-wakeword
  ```

- **Text Mode (test tools & Live API without a mic):**
  ```powershell
  .venv\Scripts\python.exe -m jarvis.main --text
  ```

---

## Tips for Windows

- **Echo protection & headphones:** By default (`JARVIS_MUTE_WHILE_SPEAKING=true`), Jarvis mutes mic streaming while speaking so speaker audio doesn't echo back and loop. If you wear headphones and want mid-sentence barge-in interruption, run with `--barge-in` or set `JARVIS_MUTE_WHILE_SPEAKING=false` in `.env`.
- **English & Spanish:** Say *"Hey Jarvis"* to wake it, then speak naturally in English or Spanish (or switch languages anytime mid-conversation).
- **First-run wake word download:** On first launch in wake-word mode, `openWakeWord` downloads its lightweight ONNX model (`hey_jarvis`) automatically.
- **Safety guardrails:** Destructive PowerShell commands and overwriting existing files require verbal confirmation (`confirmed=true` / `overwrite=true`), and file writes are scoped to `JARVIS_WORKSPACE_DIR` by default.

## Install Flavors (Same App, Different Deployment Methods)

1. **Personal / Dev PC Replicator (`install.bat`):**
   Copy or `git clone` this folder to another Windows PC and double-click `install.bat` (or run `scripts/setup.ps1`). It checks/installs Python 3.12 and Node.js LTS via `winget`, creates `.venv`, installs dependencies, caches the `hey_jarvis` wake-word model, sets up `.env`, and creates a `Jarvis` Desktop shortcut.
2. **Standalone `.exe` Installer (future distribution):**
   Bundle `jarvis/bridge.py` with PyInstaller and package `desktop/` with `electron-builder` (NSIS / portable) so non-technical PCs need zero prerequisites.

## Tests

```powershell
.venv\Scripts\python.exe -m pytest -q
```
