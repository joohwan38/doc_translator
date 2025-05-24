// main.js

const { app, BrowserWindow, ipcMain, shell, dialog } = require('electron');
const path = require('node:path'); // Node.js path 모듈 사용
const { spawn } = require('node:child_process');
const fs = require('node:fs');
const findFreePort = require('find-free-port');

let mainWindow;
let pythonProcess = null;
let flaskPort = 5001;

// Python 설정 경로 (config.py 기반으로 추정 및 생성)
let APP_NAME_FOR_PATHS = "PowerpointDocumentTranslator"; // config.py의 값과 일치
let APP_DATA_DIR_CONFIG;
let LOGS_DIR;
let HISTORY_DIR;

// 수정된 부분: getPlatformSpecificAppDataPath 함수
// main.js의 getPlatformSpecificAppDataPath 함수만 수정

function getPlatformSpecificAppDataPath(appName) {
    console.log(`[PathUtils] Attempting to get platform-specific app data path for appName: "${appName}" on platform: ${process.platform}`);
    
    let basePath;
    switch (process.platform) {
        case 'darwin': // macOS
            console.log("[PathUtils][macOS] Trying to get standard paths:");
            try {
                console.log(`[PathUtils][macOS] app.getPath('home'): ${app.getPath('home')}`);
                console.log(`[PathUtils][macOS] app.getPath('appData'): ${app.getPath('appData')}`); // 보통 ~/Library/Application Support
                console.log(`[PathUtils][macOS] app.getPath('userData'): ${app.getPath('userData')}`); // 보통 ~/Library/Application Support/YourAppName
                console.log(`[PathUtils][macOS] app.getPath('logs'): ${app.getPath('logs')}`);
                console.log(`[PathUtils][macOS] app.getPath('library'): ${app.getPath('library')}`); // 원래 문제를 일으켰던 호출
            } catch (e) {
                console.error("[PathUtils][macOS] Error while trying to log app.getPath results:", e);
            }

            // appData 경로를 우선적으로 사용 시도
            basePath = app.getPath('appData'); // appData는 '~/Library/Application Support'를 반환
            if (!basePath) {
                console.error("[PathUtils][macOS] Failed to get 'appData' path. This is unexpected.");
                // appData도 실패하면, 원래 방식(library) 시도 또는 에러 처리
                // 여기서는 library도 실패할 가능성이 높으므로 에러를 던집니다.
                throw new Error("Failed to get 'appData' path on macOS via app.getPath('appData').");
            }
            // app.getPath('appData')는 이미 'Application Support' 경로이므로, appName만 추가합니다.
            return path.join(basePath, appName); 

        case 'win32': // Windows
            basePath = app.getPath('appData');
            if (!basePath) {
                console.error("[PathUtils][Windows] Failed to get 'appData' path on Windows.");
                throw new Error("Failed to get 'appData' path on Windows via app.getPath('appData').");
            }
            return path.join(basePath, appName);

        default: // Linux and other OS
            basePath = app.getPath('home');
            if (!basePath) {
                console.error("[PathUtils][Linux/Other] Failed to get 'home' path.");
                throw new Error("Failed to get 'home' path on Linux/other OS via app.getPath('home').");
            }
            return path.join(basePath, '.config', appName);
    }
}

async function initializeAppPaths() {
    try { // getPlatformSpecificAppDataPath가 에러를 던질 수 있으므로 try-catch 추가
        APP_DATA_DIR_CONFIG = getPlatformSpecificAppDataPath(APP_NAME_FOR_PATHS);
    } catch (error) {
        console.error("[PathInit] Critical error getting APP_DATA_DIR_CONFIG:", error);
        dialog.showErrorBox("Initialization Error", `Failed to determine application data directory: ${error.message}. The application cannot start.`);
        app.quit();
        return false; // 초기화 실패
    }

    LOGS_DIR = path.join(APP_DATA_DIR_CONFIG, process.platform === 'darwin' ? 'Logs' : 'logs');
    HISTORY_DIR = path.join(APP_DATA_DIR_CONFIG, 'hist');

    console.log("[PathInit] Electron derived paths:");
    console.log("[PathInit] APP_DATA_DIR_CONFIG:", APP_DATA_DIR_CONFIG);
    console.log("[PathInit] LOGS_DIR:", LOGS_DIR);
    console.log("[PathInit] HISTORY_DIR:", HISTORY_DIR);

    const pathsToEnsure = [APP_DATA_DIR_CONFIG, LOGS_DIR, HISTORY_DIR];
    
    // Python 스크립트 내의 UPLOAD_FOLDER는 Python 서버 시작 시 Python 내부에서 생성/관리하는 것이
    // 책임 분리 측면에서 더 나을 수 있습니다. Electron에서 생성해야 한다면 아래 로직 유지.
    // pythonProcess가 이 시점에는 아직 null이므로, UPLOAD_FOLDER 경로 생성 로직은
    // startPythonServer 함수 내부 또는 그 이후로 옮기는 것을 고려해볼 수 있습니다.
    // 여기서는 일단 pythonProcess null 체크를 제거하고 경로를 pathsToEnsure에 추가합니다.
    // (단, pythonProcess 관련 로직이 아니므로 이 부분은 원래 의도에 따라 재검토 필요)
    const pythonUploadFolder = path.join(APP_DATA_DIR_CONFIG, 'uploads'); // config.py의 UPLOAD_FOLDER 경로와 일치 가정
    pathsToEnsure.push(pythonUploadFolder);
    console.log("[PathInit] Python UPLOAD_FOLDER (Electron pre-created):", pythonUploadFolder);


    for (const dirPath of pathsToEnsure) {
        if (!fs.existsSync(dirPath)) {
            try {
                fs.mkdirSync(dirPath, { recursive: true });
                console.log(`[PathInit] Created directory: ${dirPath}`);
            } catch (error) {
                console.error(`[PathInit] Failed to create directory ${dirPath}:`, error);
                dialog.showErrorBox("Initialization Error", `Failed to create essential directory: ${dirPath}. Please check permissions.`);
                app.quit();
                return false; // 초기화 실패
            }
        }
    }
    return true; // 초기화 성공
}


function createWindow() {
    mainWindow = new BrowserWindow({
        width: 1280,
        height: 800,
        webPreferences: {
            preload: path.join(__dirname, 'preload.js'),
            contextIsolation: true,
            nodeIntegration: false,
        },
        icon: path.join(__dirname, 'static', 'LINEstudio2.png')
    });

    mainWindow.loadURL(`http://127.0.0.1:${flaskPort}`)
        .catch(err => {
            console.error(`[ElectronUI] Failed to load URL: http://127.0.0.1:${flaskPort}`, err);
            dialog.showErrorBox("Load Error", `Failed to load application UI from http://127.0.0.1:${flaskPort}. Ensure the backend server started correctly.`);
        });

    // mainWindow.webContents.openDevTools();

    mainWindow.on('closed', () => {
        mainWindow = null;
    });
}

function startPythonServer() {
    return new Promise((resolve, reject) => {
        findFreePort(5001, '127.0.0.1') // 기본 포트 5001부터 검색
            .then(async ([freePort]) => {
                flaskPort = freePort;
                console.log(`[PythonServer] Flask server will run on port: ${flaskPort}`);

                let pythonExecutable = process.platform === 'win32' ? 'python' : 'python3';
                
                // 패키징된 앱 내부 Python 사용 로직 (예시, 실제 경로 및 조건 확인 필요)
                const bundledPythonPath = process.platform === 'win32'
                    ? path.join(process.resourcesPath, 'python_runtime', 'python.exe')
                    : path.join(process.resourcesPath, 'python_runtime', 'bin', 'python3');

                if (app.isPackaged && fs.existsSync(bundledPythonPath)) {
                    pythonExecutable = bundledPythonPath;
                    console.log(`[PythonServer] Using bundled Python: ${pythonExecutable}`);
                } else if (app.isPackaged) {
                    console.warn(`[PythonServer] Bundled Python not found at ${bundledPythonPath}. Falling back to system Python. Ensure Python is packaged correctly.`);
                }


                const scriptPath = path.join(__dirname, 'web_app.py'); // web_app.py는 main.js와 같은 디렉토리에 있다고 가정
                console.log(`[PythonServer] Attempting to start Python server: ${pythonExecutable} ${scriptPath} on port ${flaskPort}`);

                // 환경 변수 설정
                const pythonEnv = {
                    ...process.env,
                    FLASK_PORT: flaskPort.toString(),
                    PYTHONUNBUFFERED: "1",
                    // APP_DATA_DIR_CONFIG가 initializeAppPaths에서 설정된 이후에 사용 가능
                    // Python 스크립트가 이 값을 필요로 한다면, startPythonServer 호출 전에 initializeAppPaths가 완료되어야 함.
                    // (현재 app.whenReady() 로직에서는 순서가 맞음)
                    POWERPOINT_TRANSLATOR_APP_DATA_DIR: APP_DATA_DIR_CONFIG,
                    // Python에서 로그 디렉토리 등을 생성/사용할 때 이 경로를 기준으로 할 수 있음
                    POWERPOINT_TRANSLATOR_LOGS_DIR: LOGS_DIR,
                    POWERPOINT_TRANSLATOR_HISTORY_DIR: HISTORY_DIR
                };
                console.log("[PythonServer] Environment variables for Python process:", {
                    FLASK_PORT: pythonEnv.FLASK_PORT,
                    POWERPOINT_TRANSLATOR_APP_DATA_DIR: pythonEnv.POWERPOINT_TRANSLATOR_APP_DATA_DIR,
                    POWERPOINT_TRANSLATOR_LOGS_DIR: pythonEnv.POWERPOINT_TRANSLATOR_LOGS_DIR,
                    POWERPOINT_TRANSLATOR_HISTORY_DIR: pythonEnv.POWERPOINT_TRANSLATOR_HISTORY_DIR,
                });


                pythonProcess = spawn(pythonExecutable, [scriptPath], {
                    stdio: ['pipe', 'pipe', 'pipe'], // stdin, stdout, stderr
                    env: pythonEnv,
                    cwd: __dirname // Python 스크립트의 작업 디렉토리 (web_app.py 기준)
                });

                pythonProcess.stdout.on('data', (data) => {
                    const output = data.toString().trim();
                    console.log(`[Python STDOUT] ${output}`);
                    if (output.includes(`Running on http://127.0.0.1:${flaskPort}`) || output.includes(`Running on http://0.0.0.0:${flaskPort}`)) {
                        console.log('[PythonServer] Flask server confirmed running.');
                        // startupTimeout 클리어 로직 추가 가능
                        clearTimeout(startupTimeout); // 서버 시작 확인 시 타임아웃 해제
                        resolve(flaskPort);
                    }
                });

                pythonProcess.stderr.on('data', (data) => {
                    const errorOutput = data.toString().trim();
                    console.error(`[Python STDERR] ${errorOutput}`);
                    // 필요시 특정 에러에 따라 reject(new Error(...)) 호출
                });

                pythonProcess.on('error', (err) => {
                    console.error('[PythonServer] Failed to start Python process:', err);
                    dialog.showErrorBox("Backend Error", `Failed to start the Python backend process: ${err.message}. Ensure Python is installed and accessible, or check bundled Python.`);
                    clearTimeout(startupTimeout);
                    reject(err);
                });

                pythonProcess.on('close', (code) => {
                    console.log(`[PythonServer] Python process exited with code ${code}.`);
                    // isQuitting 플래그는 app.on('before-quit', ...) 에서 설정
                    if (code !== 0 && !isQuitting) { // 앱 종료 중이 아닐 때 비정상 종료된 경우
                        if (mainWindow) { // mainWindow가 아직 살아있다면 (예: 서버만 죽은 경우)
                           dialog.showErrorBox("Backend Error", `The Python backend process stopped unexpectedly (code: ${code}). Some functionalities might be unavailable. Try restarting the application.`);
                        }
                    }
                    pythonProcess = null; // 프로세스 참조 정리
                });

                // Flask 서버 시작 타임아웃
                const startupTimeout = setTimeout(() => {
                     if (!mainWindow && pythonProcess && !pythonProcess.killed) { 
                        console.warn(`[PythonServer] Flask server startup timed out after 20s on port ${flaskPort}. Check Python logs. Proceeding to create window.`);
                        resolve(flaskPort); // UI라도 띄우기 위해 resolve
                     } else if (pythonProcess && !pythonProcess.killed) { // mainWindow는 생성되었으나, resolve가 안 된 경우
                        console.warn(`[PythonServer] Flask server startup timed out after 20s (window exists). Server might be slow or had an issue.`);
                        // resolve(flaskPort); // 이미 resolve 되었을 수도 있으므로 중복 호출 방지 필요
                     }
                }, 20000);

            })
            .catch((err) => {
                console.error('[PythonServer] Could not find a free port or other pre-Python error:', err);
                dialog.showErrorBox("Backend Setup Error", `Failed to secure a port for the backend server: ${err.message}`);
                reject(err); // startPythonServer Promise를 reject
            });
    });
}

app.whenReady().then(async () => {
    console.log("[AppLifecycle] App is ready.");
    // 1. 경로 초기화 (필수)
    if (!await initializeAppPaths()) {
        console.error("[AppLifecycle] Failed to initialize application paths. Quitting.");
        // initializeAppPaths 내부에서 이미 에러 박스 표시 및 app.quit() 호출됨
        return; 
    }

    // 2. Python 서버 시작
    try {
        await startPythonServer();
        console.log("[AppLifecycle] Python server started successfully.");
        // 3. 메인 윈도우 생성
        createWindow();
        console.log("[AppLifecycle] Main window created.");
    } catch (error) {
        console.error("[AppLifecycle] Fatal error during app startup:", error);
        dialog.showErrorBox("Application Startup Error", "Could not start the application due to a backend or configuration issue.\n\n" + (error.message || error));
        app.quit();
    }

    app.on('activate', () => {
        console.log("[AppLifecycle] App activated.");
        if (BrowserWindow.getAllWindows().length === 0) {
            console.log("[AppLifecycle] No windows open, attempting to recreate.");
            if (pythonProcess && !pythonProcess.killed) { // Python 프로세스가 살아있으면 창만 생성
                createWindow();
            } else { // Python 프로세스도 죽었으면 서버부터 재시작
                console.log("[AppLifecycle] Python process not running on activate, attempting to restart server and create window.");
                startPythonServer().then(() => {
                    createWindow();
                }).catch(err => {
                     console.error("[AppLifecycle] Could not restart backend services on activate:", err);
                     dialog.showErrorBox("Application Restart Error", "Could not restart backend services on activate: " + (err.message || err));
                     app.quit(); // 심각한 오류로 간주하고 종료
                });
            }
        }
    });
});

app.on('window-all-closed', () => {
    console.log("[AppLifecycle] All windows closed.");
    if (process.platform !== 'darwin') { // macOS 제외하고 모든 창 닫히면 앱 종료
        app.quit();
    }
});

let isQuitting = false; // 앱이 정상적으로 종료되는 중인지 확인하는 플래그
app.on('before-quit', () => {
    console.log("[AppLifecycle] Before quit event.");
    isQuitting = true;
});

app.on('quit', () => {
    console.log('[AppLifecycle] Application is quitting...');
    if (pythonProcess && !pythonProcess.killed) {
        console.log('[AppLifecycle] Terminating Python process...');
        pythonProcess.kill();
        pythonProcess = null;
    }
    console.log("[AppLifecycle] Quit sequence finished.");
});

// --- IPC 핸들러 ---
ipcMain.handle('get-flask-port', async () => {
    console.log("[IPC] Received 'get-flask-port' request.");
    return flaskPort;
});

ipcMain.handle('open-log-folder', async () => {
    console.log("[IPC] Received 'open-log-folder' request.");
    // LOGS_DIR은 initializeAppPaths에서 설정됨. 만약 해당 함수가 실패했으면 LOGS_DIR이 없을 수 있음.
    // 하지만 initializeAppPaths 실패 시 앱이 시작되지 않으므로, 여기까지 왔다면 LOGS_DIR은 설정되어 있어야 함.
    if (!LOGS_DIR) { // 방어 코드
        console.warn("[IPC] LOGS_DIR is not set. Attempting to re-initialize paths (should not happen).");
        if (!await initializeAppPaths()) {
            return { success: false, message: "Log directory path could not be determined."};
        }
    }

    if (LOGS_DIR && fs.existsSync(LOGS_DIR)) {
        try {
            await shell.openPath(LOGS_DIR);
            console.log(`[IPC] Log folder opened: ${LOGS_DIR}`);
            return { success: true, path: LOGS_DIR };
        } catch (error) {
            console.error(`[IPC] Error opening log folder ${LOGS_DIR}:`, error);
            return { success: false, message: `Failed to open log folder: ${error.message}` };
        }
    }
    const message = `[IPC] Log directory not found or not accessible: ${LOGS_DIR}`;
    console.warn(message);
    return { success: false, message };
});

ipcMain.handle('delete-translation-history', async () => {
    console.log("[IPC] Received 'delete-translation-history' request.");
    // HISTORY_DIR도 initializeAppPaths에서 설정됨.
    if (!HISTORY_DIR) { // 방어 코드
        console.warn("[IPC] HISTORY_DIR is not set. Attempting to re-initialize paths (should not happen).");
        if (!await initializeAppPaths()) {
            return { success: false, message: "History directory path could not be determined."};
        }
    }
    // config.py와 파일 이름이 다를 수 있으므로, Electron에서 관리하는 파일명으로 지정
    const historyFilePath = path.join(HISTORY_DIR, 'translation_history.json'); 
    console.log(`[IPC] Attempting to delete history file: ${historyFilePath}`);

    if (fs.existsSync(historyFilePath)) {
        try {
            fs.unlinkSync(historyFilePath); // 동기 방식. 비동기를 원하면 fs.promises.unlink 사용
            console.log(`[IPC] Translation history file deleted: ${historyFilePath}`);
            return { success: true, message: 'Translation history deleted.' };
        } catch (error) {
            console.error("[IPC] Error deleting history file:", error);
            return { success: false, message: `Error deleting history: ${error.message}` };
        }
    }
    console.log(`[IPC] Translation history file not found (already deleted or never created): ${historyFilePath}`);
    return { success: true, message: 'Translation history file not found.' };
});