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
});
