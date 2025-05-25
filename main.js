// main.js
const { performance } = require('node:perf_hooks');
const { app, BrowserWindow, ipcMain, shell, dialog } = require('electron');
const path = require('node:path');
const { spawn } = require('node:child_process');
const fs = require('node:fs');
const findFreePort = require('find-free-port');

let mainWindow;
let pythonProcess = null;
let flaskPort = 5001; // 기본 포트, findFreePort로 실제 사용 포트 결정
let isQuitting = false; // 앱 종료 플래그

const APP_NAME_FOR_PATHS = "PowerpointDocumentTranslator";
let APP_DATA_DIR_CONFIG;
let LOGS_DIR;
let HISTORY_DIR;

function getPlatformSpecificAppDataPath(appName) {
    // console.log(`[PathUtils] Getting app data path for: "${appName}" on ${process.platform}`);
    let basePath;
    switch (process.platform) {
        case 'darwin': // macOS
            basePath = app.getPath('appData'); // '~/Library/Application Support'
            if (!basePath) throw new Error("Failed to get 'appData' path on macOS.");
            return path.join(basePath, appName);
        case 'win32': // Windows
            basePath = app.getPath('appData'); // '%APPDATA%'
            if (!basePath) throw new Error("Failed to get 'appData' path on Windows.");
            return path.join(basePath, appName);
        default: // Linux and other OS
            basePath = app.getPath('home');
            if (!basePath) throw new Error("Failed to get 'home' path on Linux/other OS.");
            return path.join(basePath, '.config', appName);
    }
}

async function initializeAppPaths() {
    console.log(`[TIME] initializeAppPaths - Start: ${performance.now().toFixed(2)}`);
    try {
        APP_DATA_DIR_CONFIG = getPlatformSpecificAppDataPath(APP_NAME_FOR_PATHS);
        LOGS_DIR = path.join(APP_DATA_DIR_CONFIG, process.platform === 'darwin' ? 'Logs' : 'logs');
        HISTORY_DIR = path.join(APP_DATA_DIR_CONFIG, 'hist');
        const pythonUploadFolder = path.join(APP_DATA_DIR_CONFIG, 'uploads');

        console.log("[PathInit] APP_DATA_DIR_CONFIG:", APP_DATA_DIR_CONFIG);
        console.log("[PathInit] LOGS_DIR:", LOGS_DIR);
        console.log("[PathInit] HISTORY_DIR:", HISTORY_DIR);
        console.log("[PathInit] Python UPLOAD_FOLDER (Electron pre-created):", pythonUploadFolder);
        if (fs.existsSync(pythonUploadFolder)) {
            console.log(`[PathInit] Cleaning up contents of existing uploads folder: ${pythonUploadFolder}`);
            try {
                const files = fs.readdirSync(pythonUploadFolder);
                for (const file of files) {
                    const filePath = path.join(pythonUploadFolder, file);
                    fs.unlinkSync(filePath); // 동기 방식으로 파일 삭제
                    console.log(`[PathInit] Deleted old file: ${filePath}`);
                }
            } catch (err) {
                console.error(`[PathInit] Error cleaning up uploads folder ${pythonUploadFolder}:`, err);
                // 여기서 앱을 중단할 필요는 없을 수 있지만, 로깅은 중요합니다.
            }
        }
        const pathsToEnsure = [APP_DATA_DIR_CONFIG, LOGS_DIR, HISTORY_DIR, pythonUploadFolder];
        
        console.log(`[TIME] initializeAppPaths - Before directory creation: ${performance.now().toFixed(2)}`);
        for (const dirPath of pathsToEnsure) {
            if (!fs.existsSync(dirPath)) {
                fs.mkdirSync(dirPath, { recursive: true });
                console.log(`[PathInit] Created directory: ${dirPath}`);
            }
        }
        console.log(`[TIME] initializeAppPaths - After directory creation: ${performance.now().toFixed(2)}`);
    } catch (error) {
        console.error("[PathInit] Critical error during path initialization:", error);
        dialog.showErrorBox("Initialization Error", `Failed to initialize application directories: ${error.message}.`);
        app.quit();
        return false;
    }
    console.log(`[TIME] initializeAppPaths - End: ${performance.now().toFixed(2)}`);
    return true;
}

function createWindow() {
    console.log(`[TIME] createWindow - Start: ${performance.now().toFixed(2)}`);
    mainWindow = new BrowserWindow({
        width: 1280,
        height: 800,
        webPreferences: {
            preload: path.join(__dirname, 'preload.js'), //
            contextIsolation: true,
            nodeIntegration: false,
        },
        icon: path.join(__dirname, 'static', 'LINEstudio2.png') // 아이콘 경로 확인
    });
    console.log(`[TIME] createWindow - BrowserWindow object created: ${performance.now().toFixed(2)}`);

    console.log(`[TIME] createWindow - Before loadURL (http://127.0.0.1:${flaskPort}): ${performance.now().toFixed(2)}`);
    mainWindow.loadURL(`http://127.0.0.1:${flaskPort}`)
        .then(() => {
            console.log(`[TIME] createWindow - loadURL succeeded: ${performance.now().toFixed(2)}`);
        })
        .catch(err => {
            console.error(`[ElectronUI] Failed to load URL http://127.0.0.1:${flaskPort}`, err);
            console.log(`[TIME] createWindow - loadURL failed: ${performance.now().toFixed(2)}`);
            dialog.showErrorBox("Load Error", `Failed to load application UI from http://127.0.0.1:${flaskPort}. Ensure the backend server started correctly and is accessible.`);
        });

    mainWindow.on('ready-to-show', () => {
        console.log(`[TIME] createWindow - Event 'ready-to-show': ${performance.now().toFixed(2)}`);
        // mainWindow.show(); // If BrowserWindow was created with show: false
    });

    mainWindow.on('closed', () => {
        mainWindow = null;
    });
    console.log(`[TIME] createWindow - End of function: ${performance.now().toFixed(2)}`);
}

function startPythonServer() {
    console.log(`[TIME] startPythonServer - Start: ${performance.now().toFixed(2)}`);
    return new Promise((resolve, reject) => {
        let resolveInvoked = false;
        let startupTimeout;

        findFreePort(5001, '127.0.0.1')
            .then(async ([freePort]) => {
                flaskPort = freePort;
                console.log(`[TIME] startPythonServer - Port ${flaskPort} found: ${performance.now().toFixed(2)}`);
                console.log(`[PythonServer] Flask server will run on port: ${flaskPort}`);

                // ... (pythonExecutable, scriptPath, pythonEnv 설정은 이전과 동일) ...
                let pythonExecutable = process.platform === 'win32' ? 'python' : 'python3';
                const bundledPythonPath = process.platform === 'win32'
                    ? path.join(process.resourcesPath, 'python_runtime', 'python.exe')
                    : path.join(process.resourcesPath, 'python_runtime', 'bin', 'python3');

                if (app.isPackaged && fs.existsSync(bundledPythonPath)) {
                    pythonExecutable = bundledPythonPath;
                    console.log(`[PythonServer] Using bundled Python: ${pythonExecutable}`);
                } else if (app.isPackaged) {
                    console.warn(`[PythonServer] Bundled Python not found at ${bundledPythonPath}. Falling back to system Python.`);
                }
                const scriptPath = path.join(__dirname, 'web_app.py');
                const pythonEnv = {
                    ...process.env,
                    FLASK_PORT: flaskPort.toString(),
                    PYTHONUNBUFFERED: "1",
                    POWERPOINT_TRANSLATOR_APP_DATA_DIR: APP_DATA_DIR_CONFIG,
                    POWERPOINT_TRANSLATOR_LOGS_DIR: LOGS_DIR,
                    POWERPOINT_TRANSLATOR_HISTORY_DIR: HISTORY_DIR
                };
                // --- 설정 부분 끝 ---

                console.log(`[TIME] startPythonServer - Before spawn: ${performance.now().toFixed(2)}`);
                pythonProcess = spawn(pythonExecutable, [scriptPath], { stdio: 'pipe', env: pythonEnv, cwd: __dirname });
                console.log(`[TIME] startPythonServer - After spawn: ${performance.now().toFixed(2)}`);

                let outputBuffer = ''; // stdout과 stderr 데이터를 공통으로 누적하거나, 별도 버퍼 사용 가능
                                      // 여기서는 공통 버퍼 사용 예시
                const readyPattern = new RegExp(`Running on http://(127\\.0\\.0\\.1|0\\.0\\.0\\.0):${flaskPort}`);
                console.log(`[DEBUG] Using regex pattern: ${readyPattern}`);

                const commonDataHandler = (dataSourceName, chunkString) => {
                    outputBuffer += chunkString; // 데이터를 공통 버퍼에 누적

                    const trimmedChunk = chunkString.trim();
                    if (trimmedChunk) {
                        console.log(`[Python ${dataSourceName} (Trimmed Chunk)]: ${trimmedChunk}`);
                    }
                    
                    // --- 디버깅 로그 (필요시 활성화) ---
                    // console.log(`[DEBUG ${dataSourceName}] --- Checking condition with regex ---`);
                    // const bufferLogSnippet = outputBuffer.length > 300 ? `...${outputBuffer.slice(-300)}` : outputBuffer;
                    // console.log(`[DEBUG ${dataSourceName}] outputBuffer (last 300 chars or full): "${bufferLogSnippet.replace(/\n/g, "\\n")}"`);
                    // const patternTestResult = readyPattern.test(outputBuffer);
                    // console.log(`[DEBUG ${dataSourceName}] readyPattern.test(outputBuffer)?: ${patternTestResult}`);
                    // console.log(`[DEBUG ${dataSourceName}] --- End of condition check ---`);
                    // --- 디버깅 로그 끝 ---

                    if (!resolveInvoked && readyPattern.test(outputBuffer)) {
                        resolveInvoked = true;
                        clearTimeout(startupTimeout);
                        const detectionSource = dataSourceName === "STDOUT" ? "STDOUT" : "STDERR";
                        console.log(`[TIME] startPythonServer - Python server ready message DETECTED via ${detectionSource} (Regex): ${performance.now().toFixed(2)}`);
                        console.log(`[PythonServer] Flask server confirmed running via ${detectionSource}.`);
                        console.log(`[PythonServer] Matched buffer content (last 300 chars from combined buffer): ...${outputBuffer.slice(-300).replace(/\n/g, "\\n")}`);
                        
                        // 리스너 정리
                        pythonProcess.stdout.removeListener('data', onStdoutData);
                        pythonProcess.stderr.removeListener('data', onStderrData);
                        resolve(flaskPort);
                    }
                };

                const onStdoutData = (data) => {
                    commonDataHandler("STDOUT", data.toString());
                };
                const onStderrData = (data) => {
                    commonDataHandler("STDERR", data.toString());
                };

                pythonProcess.stdout.on('data', onStdoutData);
                pythonProcess.stderr.on('data', onStderrData); // stderr 감시 추가

                pythonProcess.on('error', (err) => {
                    // ... (기존 에러 처리 로직과 동일하게 resolveInvoked 확인 및 리스너 정리) ...
                    if (!resolveInvoked) {
                        resolveInvoked = true;
                        clearTimeout(startupTimeout);
                        console.error('[PythonServer] Failed to start Python process:', err);
                        dialog.showErrorBox("Backend Error", `Failed to start the Python backend: ${err.message}.`);
                        pythonProcess.stdout.removeListener('data', onStdoutData);
                        pythonProcess.stderr.removeListener('data', onStderrData);
                        reject(err);
                    }
                });

                pythonProcess.on('close', (code) => {
                    // ... (기존 close 처리 로직과 동일) ...
                    console.log(`[PythonServer] Python process exited with code ${code}.`);
                    if (code !== 0 && !isQuitting && !resolveInvoked && mainWindow) {
                         dialog.showErrorBox("Backend Error", `Python backend stopped unexpectedly (code: ${code}).`);
                    }
                    pythonProcess = null;
                });

                startupTimeout = setTimeout(() => {
                    if (!resolveInvoked) {
                        resolveInvoked = true;
                        console.warn(`[PythonServer] Flask server startup timed out after 20s (Port: ${flaskPort}). Proceeding.`);
                        console.log(`[TIME] startPythonServer - Timeout occurred: ${performance.now().toFixed(2)}`);
                        pythonProcess.stdout.removeListener('data', onStdoutData);
                        pythonProcess.stderr.removeListener('data', onStderrData);
                        resolve(flaskPort);
                    }
                }, 20000);
            })
            .catch((err) => {
                 // ... (기존 catch 로직과 동일하게 resolveInvoked 확인) ...
                if (!resolveInvoked) {
                    resolveInvoked = true;
                    console.error('[PythonServer] Error before spawn (e.g., findFreePort):', err);
                    dialog.showErrorBox("Backend Setup Error", `Failed to prepare backend server: ${err.message}`);
                    reject(err);
                }
            });
    });
}

app.whenReady().then(async () => {
    console.log(`[TIME] app.whenReady - Start: ${performance.now().toFixed(2)}`);
    if (!await initializeAppPaths()) {
        console.log(`[TIME] app.whenReady - initializeAppPaths failed: ${performance.now().toFixed(2)}`);
        return; // initializeAppPaths 내부에서 app.quit() 호출됨
    }
    console.log(`[TIME] app.whenReady - After initializeAppPaths: ${performance.now().toFixed(2)}`);

    try {
        await startPythonServer();
        console.log(`[TIME] app.whenReady - After startPythonServer resolved: ${performance.now().toFixed(2)}`);
        console.log("[AppLifecycle] Python server started successfully (or timed out).");
        
        createWindow();
        console.log(`[TIME] app.whenReady - After createWindow function call: ${performance.now().toFixed(2)}`);
        console.log("[AppLifecycle] Main window creation process initiated.");
    } catch (error) {
        console.error("[AppLifecycle] Fatal error during app startup:", error);
        dialog.showErrorBox("Application Startup Error", `Could not start: ${error.message || error}`);
        app.quit();
    }
});

app.on('window-all-closed', () => {
    if (process.platform !== 'darwin') {
        app.quit();
    }
});

app.on('before-quit', () => {
    isQuitting = true;
    console.log("[AppLifecycle] Before quit triggered.");
});

app.on('quit', () => {
    console.log('[AppLifecycle] Application is quitting...');
    if (pythonProcess) {
        console.log('[AppLifecycle] Terminating Python process...');
        pythonProcess.kill();
        pythonProcess = null;
    }
});

// IPC Handlers (기존과 동일하게 유지 또는 필요시 간소화)
ipcMain.handle('get-flask-port', async () => flaskPort);

ipcMain.handle('open-log-folder', async () => {
    if (LOGS_DIR && fs.existsSync(LOGS_DIR)) {
        try {
            await shell.openPath(LOGS_DIR);
            return { success: true, path: LOGS_DIR };
        } catch (error) {
            return { success: false, message: `Failed to open log folder: ${error.message}` };
        }
    }
    return { success: false, message: `Log directory not found: ${LOGS_DIR}` };
});

ipcMain.handle('delete-translation-history', async () => {
    const historyFilePath = path.join(HISTORY_DIR, 'translation_history.json');
    if (fs.existsSync(historyFilePath)) {
        try {
            fs.unlinkSync(historyFilePath);
            return { success: true, message: 'Translation history deleted.' };
        } catch (error) {
            return { success: false, message: `Error deleting history: ${error.message}` };
        }
    }
    return { success: true, message: 'Translation history file not found.' };
});