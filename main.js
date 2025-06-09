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
let pythonUploadFolder; // 전역 변수로 선언
let lastDownloadedFilePath = null;

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
        pythonUploadFolder = path.join(APP_DATA_DIR_CONFIG, 'uploads'); // 전역 변수에 할당

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
        width: 1100,
        height:1100,
        webPreferences: {
            preload: path.join(__dirname, 'preload.js'), //
            contextIsolation: true,
            nodeIntegration: false,
        },
        icon: path.join(__dirname, 'assets', 'app_icon.icns')
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

                let pythonExecutable = process.platform === 'win32' ? 'python' : 'python3';
                
                // 패키징된 앱과 개발 환경에 따라 Python 실행 파일 및 스크립트 경로, 작업 디렉토리 설정
                const baseDir = app.isPackaged ? process.resourcesPath : __dirname;
                const scriptPath = path.join(baseDir, 'python_backend', 'web_app.py');
                const pythonCwd = path.join(baseDir, 'python_backend');

                if (app.isPackaged) {
                    const pythonVersionFolder = '3.12'; // 사용자가 명시한 버전 폴더명
                    let bundledPythonPath;
                    if (process.platform === 'win32') {
                        bundledPythonPath = path.join(baseDir, 'python_runtime', pythonVersionFolder, 'python.exe');
                    } else { // macOS, Linux
                        bundledPythonPath = path.join(baseDir, 'python_runtime', pythonVersionFolder, 'bin', 'python3.12');
                    }
                    
                    console.log(`[PythonServer] Packaged app: Attempting to use bundled Python at: ${bundledPythonPath}`);
                    if (fs.existsSync(bundledPythonPath)) {
                        pythonExecutable = bundledPythonPath;
                        console.log(`[PythonServer] Using bundled Python: ${pythonExecutable}`);
                    } else {
                        console.error(`[PythonServer] CRITICAL: Bundled Python not found at ${bundledPythonPath}. Falling back to system Python.`);
                        dialog.showErrorBox("Fatal Error", `Bundled Python not found. The application might not work correctly.\nExpected at: ${bundledPythonPath}`);
                    }
                } else {
                    console.log(`[PythonServer] Development mode: Using system Python: ${pythonExecutable}`);
                }

                const pythonEnv = {
                    ...process.env,
                    FLASK_PORT: flaskPort.toString(),
                    PYTHONUNBUFFERED: "1",
                    POWERPOINT_TRANSLATOR_APP_DATA_DIR: APP_DATA_DIR_CONFIG, // 이 값들은 외부 경로이므로 유지
                    POWERPOINT_TRANSLATOR_LOGS_DIR: LOGS_DIR,
                    POWERPOINT_TRANSLATOR_HISTORY_DIR: HISTORY_DIR
                };

                console.log(`[TIME] startPythonServer - Before spawn: ${performance.now().toFixed(2)}`);
                console.log(`[PythonServer] Executing: ${pythonExecutable}`);
                console.log(`[PythonServer] Script: ${scriptPath}`);
                console.log(`[PythonServer] CWD: ${pythonCwd}`); // 작업 디렉토리 명시

                // Python 프로세스 실행 시 CWD (Current Working Directory) 설정
                pythonProcess = spawn(pythonExecutable, [scriptPath], { stdio: 'pipe', env: pythonEnv, cwd: pythonCwd });
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
                        // 경고 대신 에러로 로깅하고, 사용자에게 알림
                        console.error(`[PythonServer] Flask server startup timed out after 20s (Port: ${flaskPort}). The backend failed to start.`);
                        console.log(`[TIME] startPythonServer - Timeout failure: ${performance.now().toFixed(2)}`);

                        if (pythonProcess) { // pythonProcess가 null이 아닐 때만 리스너 및 kill 처리
                            pythonProcess.stdout.removeListener('data', onStdoutData);
                            pythonProcess.stderr.removeListener('data', onStderrData);
                            console.log('[PythonServer] Attempting to kill Python process due to startup timeout.');
                            pythonProcess.kill(); // 응답 없는 프로세스 종료 시도
                        }
                        // UI 로드를 진행하는 대신, 시작 실패로 간주하고 에러 처리
                        // reject(new Error(`Python server startup timed out on port ${flaskPort}.`)); // app.whenReady().catch() 블록으로 에러 전달
                        // 또는 직접 다이얼로그 표시 후 앱 종료
                        dialog.showErrorBox("Backend Startup Timeout", 
                            `The Python backend server failed to start on port ${flaskPort} within the 20-second time limit. The application will now close.`);
                        app.quit();
                    }
                }, 20000); // 20초 타임아웃
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

// IPC Handlers
ipcMain.handle('get-flask-port', async () => flaskPort);

ipcMain.handle('open-log-folder', async () => {
    if (LOGS_DIR && fs.existsSync(LOGS_DIR)) {
        try {
            await shell.openPath(LOGS_DIR);
            return { success: true, path: LOGS_DIR };
        } catch (error) {
            return { success: false, message: `로그 폴더를 여는 데 실패했습니다: ${error.message}` };
        }
    }
    return { success: false, message: `로그 디렉토리를 찾을 수 없습니다: ${LOGS_DIR}` };
});

ipcMain.handle('save-translated-file', async (event, { tempFilePath, originalPath, targetLang }) => {
    if (!tempFilePath || !originalPath || !targetLang) {
        return { success: false, message: '파일 경로 또는 대상 언어 정보가 누락되었습니다.' };
    }

    const originalFolder = path.dirname(originalPath);
    const { name, ext } = path.parse(originalPath); // 예: name="원본프레젠테이션", ext=".pptx"
    const newFileName = `${name}_${targetLang}${ext}`; // 예: "원본프레젠테이션_ko.pptx"


    try {
        const { canceled, filePath } = await dialog.showSaveDialog(mainWindow, {
            title: '번역된 파일 저장',
            defaultPath: path.join(originalFolder, newFileName), // 새로 생성한 파일명을 기본값으로 사용
            filters: [
                { name: 'PowerPoint or Excel', extensions: ['pptx', 'xlsx'] },
                { name: 'All Files', extensions: ['*'] }
            ]
        });

        if (canceled || !filePath) {
            return { success: false, message: '파일 저장이 취소되었습니다.' };
        }

        // 임시 파일을 사용자가 선택한 경로로 복사합니다.
        fs.copyFileSync(tempFilePath, filePath);
        console.log(`[Save] File copied from ${tempFilePath} to ${filePath}`);

        lastDownloadedFilePath = filePath; // [!INFO] 최종 저장 경로를 기억합니다.

        return { success: true, path: filePath };

    } catch (error) {
        console.error('[Save] Failed to save file:', error);
        return { success: false, message: `파일 저장 중 오류 발생: ${error.message}` };
    }
});

ipcMain.handle('delete-translation-history', async () => {
    const historyFilePath = path.join(HISTORY_DIR, 'translation_history.json');
    if (fs.existsSync(historyFilePath)) {
        try {
            fs.unlinkSync(historyFilePath);
            return { success: true, message: '번역 기록이 삭제되었습니다.' };
        } catch (error) {
            return { success: false, message: `기록 삭제 중 오류 발생: ${error.message}` };
        }
    }
    return { success: true, message: '번역 기록 파일을 찾을 수 없습니다.' };
});

// 출력/업로드 폴더를 열기 위한 IPC 핸들러 추가
ipcMain.handle('open-output-folder', async () => {
    // [!INFO] --- 디버깅 코드 추가 ---
    console.log('[FolderOpen] "결과 폴더 열기" 버튼 클릭됨');
    console.log(`[FolderOpen] 현재 lastDownloadedFilePath 값: ${lastDownloadedFilePath}`);

    let folderToOpen = pythonUploadFolder;
    if (lastDownloadedFilePath && fs.existsSync(lastDownloadedFilePath)) {
        folderToOpen = path.dirname(lastDownloadedFilePath);
        console.log(`[FolderOpen] 열 폴더 경로를 실제 저장된 경로로 변경: ${folderToOpen}`);
    } else {
        console.log(`[FolderOpen] 실제 저장된 경로가 없어 임시 폴더를 엽니다: ${folderToOpen}`);
    }

    if (folderToOpen && fs.existsSync(folderToOpen)) {
        try {
            await shell.openPath(folderToOpen);
            return { success: true, path: folderToOpen };
        } catch (error) {
            console.error(`[FolderOpen] 폴더 열기 실패: ${error.message}`);
            return { success: false, message: `폴더를 여는 데 실패했습니다: ${error.message}` };
        }
    }
    console.error(`[FolderOpen] 폴더 경로를 찾을 수 없음: ${folderToOpen}`);
    return { success: false, message: `디렉토리를 찾을 수 없습니다: ${folderToOpen}` };
});