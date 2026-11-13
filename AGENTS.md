# Jarvis Project Rules

1. **Keep All Installers & Local Setup in Sync:** Whenever any code, dependency, or integration is added or modified:
   - Update `requirements.txt` (`.venv\Scripts\pip.exe freeze > requirements.txt`) and `desktop/package.json` if dependencies change.
   - Ensure the local replicator ([install.bat](install.bat) / [scripts/setup.ps1](scripts/setup.ps1)) stays compatible.
   - Rebuild the standalone Windows installer ([scripts/build-installer.ps1](scripts/build-installer.ps1) → `desktop/dist/Jarvis-Setup-*.exe`) after tests pass so the `.exe` installer always reflects the latest changes.
2. **Synced Memory:** Keep `data/jarvis_memory.db` and `data/SYNC.md` up to date for cross-session continuity.
