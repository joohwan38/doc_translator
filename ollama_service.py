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
from typing import Tuple, Optional, List, Callable, Any, Generator
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
        # 기존 코드 유지
        try:
            if shutil.which('ollama'):
                logger.debug("Ollama found in PATH via shutil.which")
                return True
            system = platform.system()
            if system == "Windows":
                paths_to_check = [
                    "C:\\Program Files\\Ollama\\ollama.exe",
                    os.path.expanduser("~\\AppData\\Local\\Ollama\\ollama.exe"),
                    os.path.expanduser("~\\AppData\\Local\\Programs\\Ollama\\ollama.exe")
                ]
                for path in paths_to_check:
                    if os.path.exists(path):
                        logger.debug(f"Ollama found at: {path}")
                        return True
            elif system == "Darwin": # macOS
                paths_to_check = [
                    "/usr/local/bin/ollama",
                    "/opt/homebrew/bin/ollama",
                    "/Applications/Ollama.app/Contents/MacOS/ollama",
                    "/Applications/Ollama.app/Contents/Resources/ollama"
                ]
                for path in paths_to_check:
                    if os.path.exists(path):
                        logger.debug(f"Ollama found at: {path}")
                        return True
            elif system == "Linux":
                paths_to_check = [
                    "/usr/local/bin/ollama",
                    "/usr/bin/ollama",
                    "/bin/ollama",
                    os.path.expanduser("~/.local/bin/ollama")
                ]
                for path in paths_to_check:
                    if os.path.exists(path):
                        logger.debug(f"Ollama found at {path}")
                        return True
            logger.debug("Ollama executable not found in common locations or PATH.")
            return False
        except Exception as e:
            logger.error(f"Ollama 설치 확인 오류: {e}", exc_info=True)
            return False

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
        # 기존 코드 유지
        if not self.is_installed():
            logger.warning("Ollama가 설치되어 있지 않아 시작할 수 없습니다.")
            return False
        is_already_running, _ = self.is_running()
        if is_already_running:
            logger.info("Ollama가 이미 실행 중입니다.")
            return True
        try:
            logger.info("Ollama 시작 시도 중 ('ollama serve')...")
            cmd = ["ollama", "serve"]
            process_options = {'stdout': subprocess.DEVNULL, 'stderr': subprocess.DEVNULL}
            if platform.system() == "Windows":
                process_options['creationflags'] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
            else:
                process_options['start_new_session'] = True
            subprocess.Popen(cmd, **process_options)
            for attempt in range(10):
                time.sleep(1)
                running, _ = self.is_running()
                if running:
                    logger.info(f"Ollama 시작 성공 (시도: {attempt + 1})")
                    return True
            logger.warning("Ollama 시작 시간 초과 (10초). 상태를 다시 확인해주세요.")
            return False
        except FileNotFoundError:
            logger.error("Ollama 실행 파일을 찾을 수 없습니다. PATH 설정을 확인하거나 Ollama를 올바르게 설치해주세요.")
            return False
        except Exception as e:
            logger.error(f"Ollama 시작 오류: {e}", exc_info=True)
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
            # 이 경우, _cleanup_model_state를 호출할 필요 없음 (상태 자체가 없으므로)
            return False

        running, _ = self.is_running()
        if not running:
            logger.warning(f"Ollama 미실행. {model_name} 모델 다운로드 불가.")
            self._update_pull_progress(model_name, "Ollama server not running", 0, 0, True, "Ollama not running")
            self._cleanup_model_state(model_name, "Ollama not running at worker start")
            return False

        response = None
        success_status_received = False
        final_status_text_for_cleanup = "Worker finished unexpectedly"

        try:
            logger.info(f"{model_name} 모델 실제 다운로드 시작 (Ollama API 호출)...")
            self._update_pull_progress(model_name, f"Starting download: {model_name}", 0, 0, False)
            
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
                    return False # 여기서 리턴하면 finally에서 정리

                if line:
                    try:
                        data = json.loads(line.decode('utf-8'))
                        status = data.get("status", "")
                        completed = data.get("completed", 0)
                        total = data.get("total", 0)
                        error_detail = data.get("error")

                        if error_detail:
                            error_msg = f"Error pulling model {model_name}: {error_detail}"
                            logger.error(error_msg)
                            self._update_pull_progress(model_name, status, completed, total, True, error_detail)
                            final_status_text_for_cleanup = f"Error: {error_detail}"
                            return False

                        is_done_from_ollama = status.lower() == "success"
                        if is_done_from_ollama:
                            success_status_received = True
                        
                        progress_text = status
                        if "downloading" in status.lower(): progress_text = "downloading"
                        elif "verifying" in status.lower(): progress_text = "verifying"
                        elif "extracting" in status.lower(): progress_text = "extracting"
                        
                        self._update_pull_progress(model_name, progress_text, completed, total, is_done_from_ollama)

                        if is_done_from_ollama:
                            logger.info(f"모델 {model_name} 다운로드 성공 (Ollama API 'success' 수신).")
                            self.invalidate_models_cache()
                            final_status_text_for_cleanup = "Completed successfully"
                            return True # 성공 시 여기서 리턴
                    except json.JSONDecodeError:
                        logger.debug(f"JSON 디코딩 오류 (무시 가능, 스트림 라인): {line.decode('utf-8', errors='ignore')}")
                    except Exception as e_stream_proc:
                        error_msg = f"모델 다운로드 스트림 처리 중 예외 ({model_name}): {e_stream_proc}"
                        logger.error(error_msg, exc_info=True)
                        self._update_pull_progress(model_name, "Stream processing error", 0, 0, True, str(e_stream_proc))
                        final_status_text_for_cleanup = f"Stream error: {str(e_stream_proc)}"
                        return False
            
            if not success_status_received: # 루프 정상 종료했으나 success 못 받은 경우
                logger.warning(f"{model_name} 모델 다운로드 확인 실패 (스트림 종료, 'success' 메시지 없음).")
                self._update_pull_progress(model_name, "Stream ended without success", 0, 0, True, "Incomplete stream")
                final_status_text_for_cleanup = "Incomplete stream"
            return False

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
            
            # 워커 스레드 종료 시 상태 정리 (중요)
            # final_status_text_for_cleanup는 try 블록에서 설정된 마지막 상태를 반영
            self._cleanup_model_state(model_name, final_status_text_for_cleanup)

    def start_model_pull(self, model_name: str) -> tuple[bool, str]:
        with self._model_pull_state_lock:
            # 다른 모델의 pull 작업이 있다면, 우선 중지 및 정리 (선택적 정책)
            # 예를 들어, 한 번에 하나의 모델만 pull 하도록 강제할 수 있음
            # 여기서는 기존 진행 중인 동일 모델 pull만 확인
            if model_name in self._model_pull_states:
                state = self._model_pull_states[model_name]
                if state.get("thread") and state["thread"].is_alive():
                    logger.info(f"Model pull for {model_name} is already in progress.")
                    return True, f"Model pull for {model_name} is already in progress."
                else: # 스레드가 없거나 죽었지만 상태가 남아있는 경우 (예: 이전 오류)
                    logger.info(f"Previous pull state for {model_name} found (status: {state.get('status')}). Cleaning up before new pull.")
                    # _cleanup_model_state 내부에서 락을 다시 잡으려 하지 않도록 주의
                    # 여기서는 해당 model_name의 상태만 del하고 새 상태로 덮어쓰는 방식으로 처리 가능
                    # 또는 _cleanup_model_state를 호출하지 않고, 새 상태로 바로 덮어쓰기.
                    # 더 안전하게는, _cleanup_model_state를 호출하기 전에 락을 풀고, 호출 후 다시 잡거나,
                    # _cleanup_model_state가 락을 내부에서 관리하도록 수정.
                    # 지금은 단순히 새 상태로 덮어쓰는 것으로 가정하고, _pull_model_worker의 finally에서 최종 정리.
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