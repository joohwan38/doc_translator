const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('electronAPI', {
    getFlaskPort: () => ipcRenderer.invoke('get-flask-port'),
    openLogFolder: () => ipcRenderer.invoke('open-log-folder'),
    deleteTranslationHistory: () => ipcRenderer.invoke('delete-translation-history'),
    openOutputFolder: () => ipcRenderer.invoke('open-output-folder'),
    saveTranslatedFile: (options) => ipcRenderer.invoke('save-translated-file', options)
});