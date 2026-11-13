const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('jarvisDesktop', {
  bridgeUrl: 'ws://127.0.0.1:8765',
  minimize: () => ipcRenderer.send('window:minimize'),
  maximize: () => ipcRenderer.send('window:maximize'),
  close: () => ipcRenderer.send('window:close'),
  openFullHud: () => ipcRenderer.send('overlay:expand'),
  hideOverlay: () => ipcRenderer.send('overlay:hide'),
  notifyWake: () => ipcRenderer.send('jarvis:wake'),
  notifyState: (state) => ipcRenderer.send('jarvis:state', state),
  checkUpdates: () => ipcRenderer.send('app:check-updates'),
  viewerOpen: (url) => ipcRenderer.send('viewer:open', url),
  viewerClose: () => ipcRenderer.send('viewer:close'),
  viewerNavigate: (action) => ipcRenderer.send('viewer:navigate', action),
  viewerSetBounds: (bounds) => ipcRenderer.send('viewer:set-bounds', bounds),
  viewerOpenExternal: (url) => ipcRenderer.send('viewer:open-external', url),
  onViewerNavigated: (cb) => {
    const handler = (_ev, data) => cb(data);
    ipcRenderer.on('viewer:navigated', handler);
    return () => ipcRenderer.removeListener('viewer:navigated', handler);
  },
});
