# ollama_service.py
import shutil
import platform
import os
import subprocess
import requests
import psutil
import time
import logging
import json
from typing import Tuple, Optional, List, Callable, Any, Generator, Dict
import threading
import queue

import config
from interfaces import AbsOllamaService

logger = logging.getLogger(__name__)

class OllamaService(AbsOllamaService):
    def __init__(self, url_param: str = None):
        self._url = url_param if url_param is not None else config.DEFAULT_OLLAMA_URL
        self.connect_timeout = config.OLLAMA_CONNECT_TIMEOUT
        self.read_timeout = config.OLLAMA_READ_TIMEOUT
        self.pull_read_timeout = config.OLLAMA_PULL_READ_TIMEOUT
        logger.debug(f"OllamaService initialized with URL: {self._url}")

        self._models_cache: Optional[List[str]] = None
        self._models_cache_time: float = 0.0
        self._models_cache_ttl: int = getattr(config, 'MODELS_CACHE_TTL_SECONDS', 300)

        self._model_pull_states: Dict[str, Dict[str, Any]] = {}
        self._model_pull_state_lock = threading.Lock()

    @property
    def url(self) -> str:
        return self._url

    def is_installed(self) -> bool: 
        try:
            if shutil.which('ollama'):
                logger.debug("Ollama found in PATH via shutil.which")
                return True
            system = platform.system()
     
            exe_path = self._get_ollama_executable_path() # 경로 탐색 로직 재활용
            if exe_path:
                logger.debug(f"is_installed: Ollama 확인됨 (경로: {exe_path})")
                return True
            
            logger.debug("is_installed: Ollama 실행 파일 확인 불가.")
            return False

        except Exception as e:
            logger.error(f"Ollama 설치 확인 오류: {e}", exc_info=True)
            return False
        
    def _get_ollama_executable_path(self) -> Optional[str]:

        # 1. shutil.which 사용 (PATH 환경 변수 의존)
        ollama_exe = shutil.which('ollama')
        if ollama_exe:
            logger.debug(f"Ollama 실행 파일을 PATH에서 찾음: {ollama_exe}")
            return ollama_exe

        # 2. 일반적인 설치 위치 확인 (PATH에 없는 경우 대비)
        system = platform.system()
        paths_to_check = []
        if system == "Darwin":  # macOS
            paths_to_check = [
                "/usr/local/bin/ollama",
                "/opt/homebrew/bin/ollama",  # Apple Silicon Homebrew
                # Ollama.app 번들 내의 CLI 도구 경로 (존재한다면)
                # 일반적으로 앱 번들의 주 실행 파일은 Contents/MacOS/ 내에 위치합니다.
                "/Applications/Ollama.app/Contents/MacOS/ollama"
                # 다른 잠재적 경로들 (예: Ollama 설치 스크립트가 생성하는 경로)
            ]
        elif system == "Windows":
            # Windows용 일반 경로 (is_installed 메서드 참고)
            paths_to_check = [
                "C:\\Program Files\\Ollama\\ollama.exe",
                os.path.expanduser("~\\AppData\\Local\\Ollama\\ollama.exe"),
                os.path.expanduser("~\\AppData\\Local\\Programs\\Ollama\\ollama.exe")
            ]
        elif system == "Linux":
            # Linux용 일반 경로 (is_installed 메서드 참고)
            paths_to_check = [
                "/usr/local/bin/ollama",
                "/usr/bin/ollama",
                "/bin/ollama",
                os.path.expanduser("~/.local/bin/ollama")
            ]
        # 기타 OS에 대한 경로 추가 가능

        for path in paths_to_check:
            if os.path.exists(path) and os.access(path, os.X_OK): # 파일 존재 및 실행 권한 확인
                logger.debug(f"Ollama 실행 파일을 다음 위치에서 찾음: {path}")
                return path
        
        logger.warning("Ollama 실행 파일을 PATH 또는 일반적인 설치 위치에서 찾을 수 없습니다.")
        return None

    def is_running(self) -> Tuple[bool, Optional[str]]:
        # 기존 코드 유지
        try:
            response = requests.get(f"{self.url}/", timeout=self.connect_timeout)
            if response.status_code == 200:
                port = self.url.split(':')[-1].split('/')[0]
                logger.debug(f"Ollama running, confirmed via API on port {port}")
                return True, port
        except requests.exceptions.RequestException as e:
            logger.debug(f"Ollama API check failed (this is okay, will try process check): {e}")

        try:
            for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
                proc_info = proc.info
                if proc_info:
                    proc_name = proc_info.get('name', '').lower()
                    cmdline = proc_info.get('cmdline')
                    is_ollama_in_cmd = False
                    if cmdline and isinstance(cmdline, list):
                        is_ollama_in_cmd = any('ollama' in c.lower() for c in cmdline if isinstance(c, str))

                    if 'ollama' in proc_name or is_ollama_in_cmd:
                        logger.debug(f"Ollama process found: {proc_name} (PID: {proc_info.get('pid')}). Assuming default port if API failed.")
                        try:
                            port_from_url = self.url.split(':')[-1].split('/')[0]
                            if port_from_url.isdigit():
                                return True, port_from_url
                        except Exception:
                            pass
                        return True, "11434"
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            pass
        except Exception as e:
            logger.error(f"Ollama 상태 확인 중 psutil 오류: {e}", exc_info=True)
        logger.debug("Ollama not detected as running by API or process check.")
        return False, None

    def start_ollama(self) -> bool:
        is_running_now, _ = self.is_running()
        if is_running_now:
            logger.info("Ollama가 이미 실행 중입니다.")
            return True

        ollama_executable = self._get_ollama_executable_path()

        if not ollama_executable:
            logger.error("Ollama 실행 파일을 찾을 수 없어 시작할 수 없습니다. Ollama 설치 상태 및 경로를 확인해주세요.")
            # is_installed()를 호출하여 사용자에게 추가적인 힌트를 줄 수 있습니다.
            if not self.is_installed(): # is_installed()가 다른 방식으로 확인하는 경우
                 logger.warning("참고: is_installed() 확인 결과로도 Ollama가 설치되지 않았거나 찾을 수 없는 것으로 나타납니다.")
            return False

        try:
            # 실행 시점의 PATH 환경 변수 로깅 (디버깅 목적)
            current_path_env = os.environ.get('PATH', 'PATH 환경 변수 설정 안됨')
            logger.debug(f"Ollama 시작 시점의 PATH 환경 변수: {current_path_env}")
            logger.info(f"Ollama 시작 시도: '{ollama_executable} serve'")
            
            cmd = [ollama_executable, "serve"]
            
            process_options = {'stdout': subprocess.DEVNULL, 'stderr': subprocess.DEVNULL}
            if platform.system() == "Windows":
                process_options['creationflags'] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
            else:
                # macOS/Linux에서 프로세스를 완전히 분리하려면 start_new_session=True 사용.
                # 'ollama serve'는 백그라운드에서 계속 실행되어야 하는 서비스입니다.
                process_options['start_new_session'] = True

            subprocess.Popen(cmd, **process_options)

            # Ollama가 시작될 시간을 잠시 줍니다.
            for attempt in range(10):  # 최대 10초 대기
                time.sleep(1)
                running_after_start, port = self.is_running()
                if running_after_start:
                    logger.info(f"Ollama 시작 성공 (포트: {port}, 시도: {attempt + 1}).")
                    return True
            
            logger.warning("Ollama 시작 후 10초 동안 응답을 감지하지 못했습니다. 상태를 다시 확인해주세요.")
            # 마지막으로 한 번 더 확인
            final_check_running, final_port = self.is_running()
            if final_check_running:
                logger.info(f"Ollama가 늦게 시작되었지만, 최종 확인 결과 실행 중입니다 (포트: {final_port}).")
                return True
            else:
                logger.error("Ollama 시작에 실패했거나 응답이 없습니다.")
                return False

        except FileNotFoundError: # _get_ollama_executable_path에서 경로를 찾았으므로, 여기서 발생할 가능성은 낮음
            logger.error(f"Ollama 실행 파일 '{ollama_executable}'을(를) 찾을 수 없습니다 (예상치 못한 오류).")
            return False
        except PermissionError:
            logger.error(f"Ollama 실행 파일 '{ollama_executable}'을(를) 실행할 권한이 없습니다.")
            return False
        except Exception as e:
            logger.error(f"Ollama 시작 중 예기치 않은 오류 발생: {e}", exc_info=True)
            return False

    def get_text_models(self) -> List[str]:
        # 기존 코드 유지 (캐싱 로직 포함)
        current_time = time.time()
        if self._models_cache is not None and (current_time - self._models_cache_time < self._models_cache_ttl):
            logger.debug(f"Ollama 모델 목록 (캐시 사용, TTL: {self._models_cache_ttl}s): {self._models_cache}")
            return self._models_cache
        logger.debug("Ollama 모델 목록 (캐시 만료 또는 없음, 새로고침 시도)")
        running, _ = self.is_running()
        if not running:
            logger.warning("Ollama가 실행 중이지 않아 모델 목록을 가져올 수 없습니다.")
            self._models_cache = []
            self._models_cache_time = current_time
            return []
        try:
            response = requests.get(f"{self.url}/api/tags", timeout=(self.connect_timeout, self.read_timeout))
            if response.status_code == 200:
                try:
                    models_data = response.json()
                    if isinstance(models_data, dict) and 'models' in models_data:
                        models_list = models_data.get('models', [])
                        model_names = [model['name'] for model in models_list if isinstance(model, dict) and 'name' in model]
                        if model_names:
                            logger.debug(f"Ollama 모델 목록 (API 성공): {model_names}")
                            self._models_cache = model_names
                            self._models_cache_time = current_time
                            return model_names
                except json.JSONDecodeError as e: logger.warning(f"Ollama API 응답 JSON 파싱 오류: {e}")
            else: logger.warning(f"Ollama API 응답 상태 코드: {response.status_code}")
        except requests.exceptions.RequestException as e: logger.warning(f"Ollama API 요청 실패: {e}")
        except Exception as e: logger.error(f"Ollama API 모델 목록 가져오기 중 오류: {e}", exc_info=True)

        # CLI 폴백 (기존 코드 유지)
        if self.is_installed():
            try:
                logger.info("API 실패, CLI로 모델 목록 가져오기 시도...")
                ollama_path = shutil.which('ollama')
                if not ollama_path:
                    system = platform.system()
                    if system == "Windows": possible_paths = ["C:\\Program Files\\Ollama\\ollama.exe", os.path.expanduser("~\\AppData\\Local\\Ollama\\ollama.exe"), os.path.expanduser("~\\AppData\\Local\\Programs\\Ollama\\ollama.exe")]
                    elif system == "Darwin": possible_paths = ["/usr/local/bin/ollama", "/opt/homebrew/bin/ollama", "/Applications/Ollama.app/Contents/MacOS/ollama", "/Applications/Ollama.app/Contents/Resources/ollama"]
                    else: possible_paths = ["/usr/local/bin/ollama", "/usr/bin/ollama", "/bin/ollama", os.path.expanduser("~/.local/bin/ollama")]
                    for path in possible_paths:
                        if os.path.exists(path): ollama_path = path; break

                if ollama_path:
                    result = subprocess.run([ollama_path, "list"], capture_output=True, text=True, timeout=15)
                    if result.returncode == 0 and result.stdout:
                        lines = result.stdout.strip().split('\n')
                        if len(lines) > 1:
                            cli_models = [line.split()[0] for line in lines[1:] if line.strip() and line.split()]
                            if cli_models:
                                logger.info(f"Ollama 모델 목록 (CLI 성공): {cli_models}")
                                self._models_cache = cli_models; self._models_cache_time = current_time
                                return cli_models
                    else: logger.warning(f"ollama list 명령 실패: {result.stderr}")
                else: logger.warning("ollama 실행 파일을 찾을 수 없습니다.")
            except subprocess.TimeoutExpired: logger.warning("ollama list 명령어 실행 시간 초과.")
            except Exception as e: logger.error(f"ollama list 실행 중 오류: {e}", exc_info=True)

        logger.warning("모델 목록을 가져올 수 없습니다. 빈 목록 반환.")
        self._models_cache = []; self._models_cache_time = current_time
        return []

    def invalidate_models_cache(self):
        logger.info("Ollama 모델 목록 캐시가 수동으로 무효화되었습니다.")
        self._models_cache = None
        self._models_cache_time = 0.0

    def _cleanup_model_state(self, model_name: str, reason: str = "cleanup"):
        """모델 상태 정보를 안전하게 정리합니다."""
        with self._model_pull_state_lock:
            if model_name in self._model_pull_states:
                state = self._model_pull_states[model_name]
                logger.info(f"Cleaning up state for model '{model_name}' due to: {reason}. Current status: {state.get('status')}")
                
                # 진행 중인 스레드 중지 시도
                if state.get("thread") and state["thread"].is_alive():
                    if state.get("stop_event") and not state["stop_event"].is_set():
                        logger.debug(f"Setting stop_event for model '{model_name}' during cleanup.")
                        state["stop_event"].set()
                    # 스레드 join은 여기서 하지 않음 (데드락 방지). 워커 스레드가 stop_event를 확인하고 스스로 종료해야 함.

                # 구독자 큐 비우기 및 알림 (선택적)
                if "subscriber_queues" in state:
                    final_message = {
                        "status": state.get("status_text", "Cleanup initiated"),
                        "completed": state.get("completed_bytes", 0),
                        "total": state.get("total_bytes", 0),
                        "done": True,
                        "error": state.get("error_detail") or reason
                    }
                    for q_subscriber in state["subscriber_queues"]:
                        try:
                            # 큐가 가득 찼을 수 있으므로 기존 것을 비우고 넣거나, try-except로 처리
                            while not q_subscriber.empty():
                                try: q_subscriber.get_nowait()
                                except queue.Empty: break
                            q_subscriber.put_nowait(final_message)
                        except queue.Full:
                            logger.warning(f"Subscriber queue for {model_name} full during cleanup notification.")
                        except Exception as e_q_clean:
                            logger.error(f"Error notifying subscriber queue for {model_name} during cleanup: {e_q_clean}")
                    state["subscriber_queues"].clear() # 명시적 클리어

                del self._model_pull_states[model_name]
                logger.info(f"State for model '{model_name}' removed from _model_pull_states.")
            else:
                logger.debug(f"Cleanup requested for model '{model_name}', but no active state found.")

    def _update_pull_progress(self, model_name: str, status_str: str, completed_val: int, total_val: int, done_bool: bool, error_str: Optional[str] = None):
        with self._model_pull_state_lock:
            if model_name not in self._model_pull_states:
                logger.warning(f"_update_pull_progress: {model_name} 상태 정보 없음. 업데이트 무시.")
                return

            state = self._model_pull_states[model_name]
            progress_data = {
                "status": status_str, "completed": completed_val,
                "total": total_val, "done": done_bool, "error": error_str
            }

            state["status_text"] = status_str
            state["completed_bytes"] = completed_val
            state["total_bytes"] = total_val

            if done_bool:
                state["status"] = "completed" if not error_str else "error"
                if error_str: state["error_detail"] = error_str
                logger.info(f"Pull for {model_name} marked done. Status: {state['status']}. Error: {error_str}")
                # 여기서 _cleanup_model_state를 바로 호출하면 데드락 위험 (이미 락 보유 중)
                # 대신, get_model_pull_progress_stream에서 done 처리 후 정리하도록 유도하거나,
                # _pull_model_worker 완료 시 정리 로직 호출

            for q_subscriber in state.get("subscriber_queues", []):
                try:
                    q_subscriber.put_nowait(progress_data)
                except queue.Full:
                    try:
                        q_subscriber.get_nowait()
                        q_subscriber.put_nowait(progress_data)
                    except queue.Empty: pass

    def _pull_model_worker(self, model_name: str):
        stop_event = None
        initial_state_exists = False
        with self._model_pull_state_lock:
            if model_name in self._model_pull_states:
                stop_event = self._model_pull_states[model_name]["stop_event"]
                initial_state_exists = True
            
        if not initial_state_exists:
            logger.error(f"_pull_model_worker: No state found for {model_name} upon worker start. Aborting pull.")
            return False

        running, _ = self.is_running()
        if not running:
            logger.warning(f"Ollama 미실행. {model_name} 모델 다운로드 불가.")
            self._update_pull_progress(model_name, "Ollama server not running", 0, 0, True, "Ollama not running")
            self._cleanup_model_state(model_name, "Ollama not running at worker start")
            return False

        response = None
        success_status_received = False # 이 변수는 루프 후 최종 확인용으로 유지
        final_status_text_for_cleanup = "Worker finished unexpectedly" # 기본값

        try:
            logger.info(f"{model_name} 모델 실제 다운로드 시작 (Ollama API 호출)...")
            self._update_pull_progress(model_name, f"Starting download: {model_name}", 0, 0, False, None) # 시작 시 error는 None
            
            current_pull_timeout = self.pull_read_timeout
            
            response = requests.post(
                f"{self.url}/api/pull",
                json={"name": model_name, "stream": True}, stream=True,
                timeout=(self.connect_timeout, current_pull_timeout)
            )
            response.raise_for_status()

            for line in response.iter_lines():
                if stop_event and stop_event.is_set():
                    logger.info(f"{model_name} 모델 다운로드 중지됨 (사용자 요청).")
                    self._update_pull_progress(model_name, "Download stopped by user", 0, 0, True, "Stopped by user")
                    final_status_text_for_cleanup = "Stopped by user"
                    return False 

                if line:
                    try:
                        data = json.loads(line.decode('utf-8'))
                        status_from_api = data.get("status", "") # API에서 받은 원본 status
                        completed = data.get("completed", 0)
                        total = data.get("total", 0)
                        error_detail_from_api = data.get("error") # API에서 받은 error

                        if error_detail_from_api: # API가 명시적 에러를 보내면, 이것이 우선
                            error_msg = f"Error pulling model {model_name} from API: {error_detail_from_api}"
                            logger.error(error_msg)
                            # API 에러 시, status는 API에서 온 것을 사용하고, error_detail 전달
                            self._update_pull_progress(model_name, status_from_api, completed, total, True, error_detail_from_api)
                            final_status_text_for_cleanup = f"Error from API: {error_detail_from_api}"
                            return False # 오류로 간주하고 종료

                        is_done_from_ollama = status_from_api.lower() == "success"
                        
                        current_status_text_for_update = status_from_api # 기본적으로 API status 사용
                        
                        if is_done_from_ollama:
                            success_status_received = True # 성공 플래그 설정
                            current_status_text_for_update = "Completed successfully" # 명확한 성공 메시지 설정
                            logger.info(f"모델 {model_name} 다운로드 성공 (Ollama API 'success' 수신).")
                            self.invalidate_models_cache() # 성공 시 캐시 무효화
                            final_status_text_for_cleanup = "Completed successfully"
                            # 성공 상태 업데이트 (error는 None으로 명시)
                            self._update_pull_progress(model_name, current_status_text_for_update, completed, total, True, None)
                            return True # 성공적으로 작업 완료, 여기서 종료
                        else:
                            # 진행 중인 상태에 대한 텍스트 가공 (선택적)
                            if "downloading" in status_from_api.lower(): current_status_text_for_update = "downloading"
                            elif "verifying" in status_from_api.lower(): current_status_text_for_update = "verifying"
                            elif "extracting" in status_from_api.lower(): current_status_text_for_update = "extracting"
                            # 진행 중 상태 업데이트 (error는 None으로 명시)
                            self._update_pull_progress(model_name, current_status_text_for_update, completed, total, False, None)

                    except json.JSONDecodeError:
                        logger.debug(f"JSON 디코딩 오류 (무시 가능, 스트림 라인): {line.decode('utf-8', errors='ignore')}")
                    except Exception as e_stream_proc:
                        error_msg = f"모델 다운로드 스트림 처리 중 예외 ({model_name}): {e_stream_proc}"
                        logger.error(error_msg, exc_info=True)
                        self._update_pull_progress(model_name, "Stream processing error", 0, 0, True, str(e_stream_proc))
                        final_status_text_for_cleanup = f"Stream error: {str(e_stream_proc)}"
                        return False
            
            # 루프가 정상적으로 (break 없이) 끝났는데 success_status_received가 False인 경우 (이론상 발생하기 어려움, API가 success 없이 스트림을 닫는 경우)
            if not success_status_received:
                logger.warning(f"{model_name} 모델 다운로드 스트림이 종료되었으나 'success' 메시지를 받지 못했습니다.")
                self._update_pull_progress(model_name, "Stream ended without explicit success", 0, 0, True, "Incomplete stream or API behavior change")
                final_status_text_for_cleanup = "Incomplete stream"
            return False # success를 명시적으로 받지 못하면 실패로 간주 (또는 다른 정책 적용)

        except requests.exceptions.RequestException as e_req:
            error_msg = f"Ollama 모델 다운로드 요청 오류 ({model_name}): {e_req}"
            logger.error(error_msg, exc_info=True)
            self._update_pull_progress(model_name, "API request error", 0, 0, True, str(e_req))
            final_status_text_for_cleanup = f"API request error: {str(e_req)}"
            return False
        except Exception as e_pull_worker:
            error_msg = f"Ollama 모델 다운로드 중 예측하지 못한 오류 ({model_name}): {e_pull_worker}"
            logger.error(error_msg, exc_info=True)
            self._update_pull_progress(model_name, "Unexpected worker error", 0, 0, True, str(e_pull_worker))
            final_status_text_for_cleanup = f"Unexpected worker error: {str(e_pull_worker)}"
            return False
        finally:
            if response:
                try: response.close()
                except Exception: pass
            
            self._cleanup_model_state(model_name, final_status_text_for_cleanup)

    def start_model_pull(self, model_name: str) -> tuple[bool, str]:
        with self._model_pull_state_lock:

            if model_name in self._model_pull_states:
                state = self._model_pull_states[model_name]
                if state.get("thread") and state["thread"].is_alive():
                    logger.info(f"Model pull for {model_name} is already in progress.")
                    return True, f"Model pull for {model_name} is already in progress."
                else: # 스레드가 없거나 죽었지만 상태가 남아있는 경우 (예: 이전 오류)
                    logger.info(f"Previous pull state for {model_name} found (status: {state.get('status')}). Cleaning up before new pull.")
 
                    del self._model_pull_states[model_name]


            # 새 다운로드 상태 생성
            stop_event = threading.Event()
            new_state: Dict[str, Any] = {
                "status": "starting",
                "stop_event": stop_event,
                "thread": None,
                "subscriber_queues": [],
                "status_text": "Initializing...",
                "completed_bytes": 0,
                "total_bytes": 0,
                "error_detail": None
            }
            self._model_pull_states[model_name] = new_state
            
            thread = threading.Thread(target=self._pull_model_worker, args=(model_name,))
            thread.daemon = True
            new_state["thread"] = thread
            thread.start()
            logger.info(f"Model pull for {model_name} initiated in a background thread.")
            return True, f"Model pull for {model_name} initiated."

    def get_model_pull_progress_stream(self, model_name: str) -> Generator[dict, None, None]:
        subscriber_q: queue.Queue[Dict[str, Any]] = queue.Queue(maxsize=200)
        initial_status_sent = False

        with self._model_pull_state_lock:
            if model_name not in self._model_pull_states:
                logger.warning(f"Progress stream requested for unknown or not-yet-started pull: {model_name}")
                yield {"status": "Pull not initiated or model name not found", "completed":0, "total":0, "done": True, "error": "Not found"}
                return
            
            state = self._model_pull_states[model_name]
            # subscriber_queues가 없으면 초기화
            if "subscriber_queues" not in state or state["subscriber_queues"] is None:
                state["subscriber_queues"] = []
            state["subscriber_queues"].append(subscriber_q)
            logger.info(f"New SSE subscriber for model {model_name}. Total subscribers: {len(state['subscriber_queues'])}")
            
            current_progress_for_new_subscriber = {
                "status": state.get("status_text", "Initializing..."),
                "completed": state.get("completed_bytes", 0),
                "total": state.get("total_bytes", 0),
                "done": state.get("status") in ["completed", "error", "stopped", "finished_worker_exit"], # 좀 더 명확한 완료 조건
                "error": state.get("error_detail")
            }
            try:
                subscriber_q.put_nowait(current_progress_for_new_subscriber)
                initial_status_sent = True
            except queue.Full:
                logger.warning(f"Initial status send failed for {model_name} as subscriber queue was full.")

        try:
            while True:
                try:
                    progress_data = subscriber_q.get(timeout=30) # 하트비트 겸 타임아웃
                    yield progress_data
                    if progress_data.get("done"):
                        logger.info(f"SSE stream for {model_name} received 'done' message. Closing stream for this subscriber.")
                        break
                except queue.Empty:
                    # 타임아웃 시, 모델 상태를 다시 확인하여, pull 작업이 정말 끝났는지, 아니면 단순히 메시지가 없는지 판단
                    with self._model_pull_state_lock:
                        current_state = self._model_pull_states.get(model_name)
                        if not current_state or current_state.get("status") in ["completed", "error", "stopped", "finished_worker_exit"]:
                            logger.info(f"Pull for {model_name} is confirmed done or state removed. Closing SSE stream after timeout.")
                            final_state_on_timeout = {
                                "status": current_state.get("status_text", "Finished") if current_state else "Finished",
                                "completed": current_state.get("completed_bytes",0) if current_state else 0,
                                "total": current_state.get("total_bytes",0) if current_state else 0,
                                "done": True,
                                "error": (current_state.get("error_detail") if current_state else "Stream timeout, pull assumed finished/failed")
                            }
                            yield final_state_on_timeout
                            break
                        else: # 아직 진행 중이거나 상태가 남아있음 (예: 스레드는 종료됐으나 cleanup 전)
                            yield {"status": "heartbeat", "done": False, "completed":current_state.get("completed_bytes",0), "total":current_state.get("total_bytes",0), "error":current_state.get("error_detail")}
        except GeneratorExit:
            logger.info(f"SSE client for {model_name} disconnected (GeneratorExit).")
        except Exception as e_sse_gen:
            logger.error(f"Exception in SSE generator for {model_name}: {e_sse_gen}", exc_info=True)
        finally:
            logger.debug(f"Cleaning up subscriber queue for {model_name} in get_model_pull_progress_stream.")
            with self._model_pull_state_lock:
                if model_name in self._model_pull_states:
                    state = self._model_pull_states[model_name]
                    if "subscriber_queues" in state and subscriber_q in state["subscriber_queues"]:
                        state["subscriber_queues"].remove(subscriber_q)
                        logger.info(f"Subscriber queue removed for {model_name}. Remaining: {len(state['subscriber_queues'])}")
                    # 더 이상 구독자가 없고, 작업이 완료/오류/중지된 상태이면 전체 상태 정보 정리
                    if "subscriber_queues" in state and not state["subscriber_queues"] and \
                       state.get("status") in ["completed", "error", "stopped", "finished_worker_exit"]:
                        logger.info(f"Last subscriber for {model_name} disconnected and pull is finished. Triggering final cleanup.")
                        self._cleanup_model_state(model_name, "Last subscriber disconnected and task finished")


    def stop_model_pull(self, model_name: str) -> bool:
        logger.info(f"Attempting to stop model pull for: {model_name}")
        with self._model_pull_state_lock:
            if model_name in self._model_pull_states:
                state = self._model_pull_states[model_name]
                if state.get("stop_event") and not state["stop_event"].is_set():
                    state["stop_event"].set()
                    logger.info(f"Stop signal sent for model pull: {model_name}. Worker thread will handle actual stop and cleanup.")
                    # 상태를 'stopping'으로 변경하여 UI에 즉시 반영 (선택적)
                    state["status"] = "stopping"
                    state["status_text"] = "Stopping download..."
                    # 구독자에게 즉시 알림
                    self._update_pull_progress(model_name, "Stopping download...", state["completed_bytes"], state["total_bytes"], False, "User requested stop") # done은 False로 보내고, worker가 실제 중지 후 True로 보냄
                    return True
                elif state.get("stop_event") and state["stop_event"].is_set():
                    logger.info(f"Stop signal already sent for model pull: {model_name}")
                    return True
                elif state.get("status") in ["completed", "error", "stopped", "finished_worker_exit"]:
                     logger.info(f"Model pull for {model_name} already finished/stopped (status: {state.get('status')}). No action taken.")
                     return True # 이미 완료/중지됨
                else:
                    logger.warning(f"Cannot stop pull for {model_name}: stop_event not found or invalid state.")
                    return False
            else:
                logger.warning(f"Cannot stop pull for {model_name}: no active pull state found.")
                return False

    def pull_model_with_progress(self, model_name: str,
                                 progress_callback: Optional[Callable[[str, int, int, bool], None]] = None,
                                 stop_event: Optional[threading.Event] = None) -> bool:
        # 이 메서드는 Flask SSE와 직접 연동되지 않으므로, 기존 콜백 인터페이스 유지를 위해 사용될 수 있음.
        # start_model_pull과 get_model_pull_progress_stream을 사용하여 구현.
        logger.info(f"Direct call to pull_model_with_progress for {model_name} (non-SSE usage).")

        # 외부 stop_event가 주입되면, 내부 stop_model_pull 호출과 연동 필요.
        # 여기서는 외부 stop_event를 주기적으로 체크하고, 감지 시 self.stop_model_pull 호출.
        # 또는, start_model_pull이 외부 stop_event를 받을 수 있도록 인터페이스 변경.
        
        # 먼저 해당 모델에 대한 기존 pull 작업이 있다면 중지 시도
        # (만약 이 메서드가 호출되기 전에 UI에서 명시적으로 중지했다면 이 부분은 생략될 수 있음)
        # self.stop_model_pull(model_name) # 필요시 이전 작업 중지

        success_init, msg_init = self.start_model_pull(model_name)
        if not success_init and "already in progress" not in msg_init.lower(): # "already in progress"는 OK
            logger.error(f"Failed to initiate direct pull for {model_name}: {msg_init}")
            if progress_callback: progress_callback(f"Failed to start pull: {msg_init}", 0,0,True) # 에러로 간주하고 done=True
            return False
        
        final_pull_result = False
        try:
            for progress_data in self.get_model_pull_progress_stream(model_name):
                if stop_event and stop_event.is_set(): # 외부에서 주어진 stop_event 체크
                    logger.info(f"External stop_event triggered during pull_model_with_progress for {model_name}.")
                    self.stop_model_pull(model_name) # 내부 중지 요청
                    # 스트림은 알아서 done 메시지를 보내고 종료될 것임. 여기서 바로 break 하지 않아도 됨.
                
                if progress_callback:
                    status_for_cb = progress_data.get("status", "")
                    completed_for_cb = progress_data.get("completed",0)
                    total_for_cb = progress_data.get("total",0)
                    done_for_cb = progress_data.get("done", False)
                    error_for_cb = progress_data.get("error")

                    current_cb_message = status_for_cb
                    is_final_cb_call_error = False

                    if error_for_cb and done_for_cb:
                        current_cb_message = str(error_for_cb)
                        is_final_cb_call_error = True # 최종적으로 오류로 콜백
                        final_pull_result = False
                    elif status_for_cb.lower() == "completed successfully" and done_for_cb: # _pull_model_worker에서 성공시 설정한 값
                        current_cb_message = "다운로드 완료" # 또는 i18n 키
                        final_pull_result = True
                    elif status_for_cb.lower() == "stopped by user" and done_for_cb:
                        current_cb_message = "다운로드 중지됨" # 또는 i18n 키
                        final_pull_result = False # 중지는 성공이 아님
                    elif done_for_cb: # 기타 사유로 완료 (오류 없이)
                        # 일반적으로는 success 또는 error로 명확히 구분되어야 함.
                        # 여기서는 done이지만 error가 아니면 성공으로 간주 (기존 콜백 형태 맞추기 위함)
                        # 또는 status_for_cb를 그대로 전달
                        final_pull_result = not bool(error_for_cb)


                    # 콜백 호출 (done 플래그는 실제 done_for_cb 값 사용)
                    # 마지막 호출이 아니라면 is_error_or_done은 False, 마지막이면 True
                    is_error_or_done_for_cb = done_for_cb
                    progress_callback(current_cb_message, completed_for_cb, total_for_cb, is_error_or_done_for_cb)

                if progress_data.get("done"):
                    break # 스트림에서 done 메시지 오면 종료

        except Exception as e:
            logger.error(f"Error in compatibility pull_model_with_progress for {model_name}: {e}", exc_info=True)
            if progress_callback: progress_callback(f"Streaming error: {e}",0,0,True)
            final_pull_result = False
            
        # 작업 완료 후, 최종 상태가 불명확하면 한번 더 상태 확인하여 결과 결정
        if not progress_data.get("done"): # 루프가 예외 등으로 중간에 끝난 경우
             with self._model_pull_state_lock:
                final_state_check = self._model_pull_states.get(model_name)
                if final_state_check and final_state_check.get("status") == "completed":
                    final_pull_result = True
                else:
                    final_pull_result = False

        logger.info(f"pull_model_with_progress for {model_name} finished with result: {final_pull_result}")
        return final_pull_result