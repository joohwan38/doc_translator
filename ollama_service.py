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
import queue # 추가

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

        # --- 모델 다운로드 상태 관리를 위한 속성 추가 ---
        self._model_pull_states = {}  # {"model_name": {"status": "...", "stop_event": threading.Event(), "thread": threading.Thread, "subscriber_queues": [queue.Queue, ...]}}
        self._model_pull_state_lock = threading.Lock() # _model_pull_states 접근을 위한 락

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
                ollama_path = shutil.which('ollama') # ... (기존 ollama_path 찾는 로직) ...
                if not ollama_path:
                    system = platform.system()
                    # ... (기존 시스템별 ollama_path 찾는 로직) ...
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

    def _update_pull_progress(self, model_name: str, status_str: str, completed_val: int, total_val: int, done_bool: bool, error_str: Optional[str] = None):
        """내부 헬퍼: 모델의 모든 구독자 큐에 진행 상황 업데이트 전송"""
        with self._model_pull_state_lock:
            if model_name not in self._model_pull_states:
                logger.warning(f"_update_pull_progress: {model_name} 상태 정보 없음. 업데이트 무시.")
                return

            state = self._model_pull_states[model_name]
            progress_data = {
                "status": status_str, "completed": completed_val,
                "total": total_val, "done": done_bool, "error": error_str
            }
            
            # 상태 업데이트
            state["status_text"] = status_str
            state["completed_bytes"] = completed_val
            state["total_bytes"] = total_val
            
            if done_bool:
                state["status"] = "completed" if not error_str else "error"
                if error_str: state["error_detail"] = error_str
                logger.info(f"Pull for {model_name} marked done. Status: {state['status']}. Error: {error_str}")

            for q_subscriber in state.get("subscriber_queues", []):
                try:
                    q_subscriber.put_nowait(progress_data)
                except queue.Full:
                    try: 
                        q_subscriber.get_nowait() # 오래된 것 제거
                        q_subscriber.put_nowait(progress_data)
                    except queue.Empty: pass # 거의 발생 안 함
    
    def _pull_model_worker(self, model_name: str):
        """실제 모델 다운로드 워커 함수 (백그라운드 스레드에서 실행)"""
        stop_event = None
        with self._model_pull_state_lock:
            if model_name in self._model_pull_states:
                stop_event = self._model_pull_states[model_name]["stop_event"]
            else: # 상태가 없다면 시작할 수 없음 (이론적으로는 start_model_pull에서 생성)
                logger.error(f"_pull_model_worker: No state found for {model_name}. Aborting pull.")
                return False # 혹은 예외 발생

        running, _ = self.is_running()
        if not running:
            logger.warning(f"Ollama 미실행. {model_name} 모델 다운로드 불가.")
            self._update_pull_progress(model_name, "Ollama server not running", 0, 0, True, "Ollama not running")
            return False

        response = None
        success_status_received = False
        try:
            logger.info(f"{model_name} 모델 실제 다운로드 시작 (Ollama API 호출)...")
            self._update_pull_progress(model_name, f"Starting download: {model_name}", 0, 0, False)
            
            current_pull_timeout = self.pull_read_timeout # config에서 가져온 타임아웃 사용
            
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
                    return False

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
                            return False

                        is_done_from_ollama = status.lower() == "success"
                        if is_done_from_ollama:
                            success_status_received = True
                        
                        progress_text = status
                        # 상태 메시지 개선 (UI에서 i18n 처리하도록 일반적인 상태값 전달)
                        if "downloading" in status.lower(): progress_text = "downloading"
                        elif "verifying" in status.lower(): progress_text = "verifying"
                        elif "extracting" in status.lower(): progress_text = "extracting"
                        # ... 기타 상태들 ...
                        
                        self._update_pull_progress(model_name, progress_text, completed, total, is_done_from_ollama)

                        if is_done_from_ollama:
                            logger.info(f"모델 {model_name} 다운로드 성공 (Ollama API 'success' 수신).")
                            self.invalidate_models_cache() # 모델 목록 캐시 갱신
                            return True
                    except json.JSONDecodeError:
                        logger.debug(f"JSON 디코딩 오류 (무시 가능, 스트림 라인): {line.decode('utf-8', errors='ignore')}")
                    except Exception as e_stream_proc:
                        error_msg = f"모델 다운로드 스트림 처리 중 예외 ({model_name}): {e_stream_proc}"
                        logger.error(error_msg, exc_info=True)
                        self._update_pull_progress(model_name, "Stream processing error", 0, 0, True, str(e_stream_proc))
                        return False
            
            # 루프 종료 후 'success' 상태가 오지 않았다면
            if not success_status_received:
                logger.warning(f"{model_name} 모델 다운로드 확인 실패 (스트림 종료, 'success' 메시지 없음).")
                self._update_pull_progress(model_name, "Stream ended without success", 0, 0, True, "Incomplete stream")
            return False # success 메시지 없이 종료 시 실패로 간주

        except requests.exceptions.RequestException as e_req:
            error_msg = f"Ollama 모델 다운로드 요청 오류 ({model_name}): {e_req}"
            logger.error(error_msg, exc_info=True)
            self._update_pull_progress(model_name, "API request error", 0, 0, True, str(e_req))
            return False
        except Exception as e_pull_worker:
            error_msg = f"Ollama 모델 다운로드 중 예측하지 못한 오류 ({model_name}): {e_pull_worker}"
            logger.error(error_msg, exc_info=True)
            self._update_pull_progress(model_name, "Unexpected worker error", 0, 0, True, str(e_pull_worker))
            return False
        finally:
            if response:
                try: response.close()
                except Exception: pass
            # 워커 스레드 종료 시 상태 정리 (중요)
            with self._model_pull_state_lock:
                if model_name in self._model_pull_states:
                    self._model_pull_states[model_name]["thread"] = None # 스레드 참조 제거
                    if self._model_pull_states[model_name].get("status") not in ["completed", "error", "stopped"]:
                        # 명시적인 완료/오류/중지 상태가 아니면 일반 종료로 처리
                         self._model_pull_states[model_name]["status"] = "finished_worker_exit"
                         # 구독자들에게 마지막 알림 (선택적)
                         self._update_pull_progress(model_name, "Worker thread finished", 
                                                   self._model_pull_states[model_name].get("completed_bytes",0),
                                                   self._model_pull_states[model_name].get("total_bytes",0), 
                                                   True, "Worker finished unexpectedly")


    def start_model_pull(self, model_name: str) -> tuple[bool, str]:
        """모델 다운로드를 시작하거나, 이미 진행 중인 경우 상태를 알립니다."""
        with self._model_pull_state_lock:
            if model_name in self._model_pull_states:
                state = self._model_pull_states[model_name]
                if state.get("thread") and state["thread"].is_alive():
                    logger.info(f"Model pull for {model_name} is already in progress.")
                    return True, f"Model pull for {model_name} is already in progress."
                elif state.get("status") == "completed": # 이미 성공적으로 완료된 경우
                    # 이 경우, 사용자가 다시 pull을 원하면, 이전 상태를 지우고 새로 시작할 수 있음
                    # 또는 "이미 다운로드됨" 메시지를 반환할 수 있음. 여기서는 새로 시작.
                    logger.info(f"Model {model_name} was previously downloaded. Re-initiating pull state.")
                    # del self._model_pull_states[model_name] # 이전 상태 삭제 후 새로 시작
                elif state.get("status") in ["error", "stopped", "finished_worker_exit"]:
                    logger.info(f"Previous pull for {model_name} ended with status '{state.get('status')}'. Re-initiating.")
                    # del self._model_pull_states[model_name] # 이전 상태 삭제 후 새로 시작

            # 새 다운로드 상태 생성
            stop_event = threading.Event()
            new_state = {
                "status": "starting", # 초기 상태 (워커 시작 전)
                "stop_event": stop_event,
                "thread": None, # 워커 스레드 (곧 할당됨)
                "subscriber_queues": [], # 이 모델의 진행 상황을 구독하는 큐 목록
                "status_text": "Initializing...",
                "completed_bytes": 0,
                "total_bytes": 0,
                "error_detail": None
            }
            self._model_pull_states[model_name] = new_state
            
            thread = threading.Thread(target=self._pull_model_worker, args=(model_name,))
            thread.daemon = True # 메인 스레드 종료 시 함께 종료
            new_state["thread"] = thread # 스레드 참조 저장
            thread.start()
            logger.info(f"Model pull for {model_name} initiated in a background thread.")
            return True, f"Model pull for {model_name} initiated."

    def get_model_pull_progress_stream(self, model_name: str) -> Generator[dict, None, None]:
        """특정 모델의 다운로드 진행 상황을 스트리밍하는 제너레이터 반환 (SSE용)"""
        subscriber_q = queue.Queue(maxsize=200) # 각 구독자별 큐 (버퍼 크기)
        initial_status_sent = False

        with self._model_pull_state_lock:
            if model_name not in self._model_pull_states:
                # 아직 start_model_pull이 호출되지 않았거나, 알 수 없는 모델인 경우
                logger.warning(f"Progress stream requested for unknown or not-yet-started pull: {model_name}")
                yield {"status": "Pull not initiated or model name not found", "completed":0, "total":0, "done": True, "error": "Not found"}
                return
            
            state = self._model_pull_states[model_name]
            state["subscriber_queues"].append(subscriber_q)
            logger.info(f"New SSE subscriber for model {model_name}. Total subscribers: {len(state['subscriber_queues'])}")
            
            # 구독자에게 현재까지의 상태를 즉시 전송 (선택적)
            # 이렇게 하면 클라이언트가 연결 즉시 마지막 상태를 받을 수 있음
            current_progress_for_new_subscriber = {
                "status": state.get("status_text", "Initializing..."),
                "completed": state.get("completed_bytes", 0),
                "total": state.get("total_bytes", 0),
                "done": state.get("status") in ["completed", "error", "stopped", "finished_worker_exit"],
                "error": state.get("error_detail")
            }
            try:
                subscriber_q.put_nowait(current_progress_for_new_subscriber)
                initial_status_sent = True
            except queue.Full: # 거의 발생하지 않음 (새 큐이므로)
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
                    # 타임아웃 시, 실제 pull thread가 아직 살아있는지 확인
                    with self._model_pull_state_lock:
                        state = self._model_pull_states.get(model_name)
                        is_done = state and state.get("status") in ["completed", "error", "stopped", "finished_worker_exit"]
                        is_thread_alive = state and state.get("thread") and state.get("thread").is_alive()

                    if is_done or not is_thread_alive:
                        logger.info(f"Pull for {model_name} is done or thread is not alive. Closing SSE stream.")
                        final_state_on_timeout = {
                            "status": state.get("status_text", "Finished") if state else "Finished",
                            "completed": state.get("completed_bytes",0) if state else 0,
                            "total": state.get("total_bytes",0) if state else 0,
                            "done": True,
                            "error": state.get("error_detail") if state else "Stream timeout, pull assumed finished/failed"
                        }
                        yield final_state_on_timeout
                        break
                    else:
                        # 스레드가 살아있고 아직 완료되지 않았으면 하트비트
                        yield {"status": "heartbeat", "done": False, "completed":0, "total":0, "error":None} # SSE 표준은 아니지만 클라이언트에서 처리 가능
        except GeneratorExit: # 클라이언트가 연결을 끊은 경우
            logger.info(f"SSE client for {model_name} disconnected.")
        finally:
            # 구독자 큐 정리
            with self._model_pull_state_lock:
                if model_name in self._model_pull_states:
                    state = self._model_pull_states[model_name]
                    if subscriber_q in state.get("subscriber_queues", []):
                        state["subscriber_queues"].remove(subscriber_q)
                    # 마지막 구독자이고, 풀 작업이 완료/오류/중지된 상태이면 전체 상태 정보 정리 (선택적)
                    if not state.get("subscriber_queues") and state.get("status") in ["completed", "error", "stopped", "finished_worker_exit"]:
                        logger.info(f"Last subscriber for {model_name} disconnected and pull is finished. Cleaning up model pull state.")
                        del self._model_pull_states[model_name]

    def stop_model_pull(self, model_name: str) -> bool:
        """특정 모델의 다운로드를 중지하도록 요청합니다."""
        with self._model_pull_state_lock:
            if model_name in self._model_pull_states:
                state = self._model_pull_states[model_name]
                if state.get("stop_event") and not state["stop_event"].is_set():
                    state["stop_event"].set()
                    logger.info(f"Stop signal sent for model pull: {model_name}")
                    # 실제 중지 및 상태 업데이트는 _pull_model_worker 및 _update_pull_progress에서 처리
                    return True
                elif state.get("stop_event") and state["stop_event"].is_set():
                    logger.info(f"Stop signal already sent for model pull: {model_name}")
                    return True # 이미 중지 요청됨
                else: # stop_event가 없는 비정상 상태
                    logger.warning(f"Cannot stop pull for {model_name}: no stop_event found in state.")
                    return False
            else:
                logger.warning(f"Cannot stop pull for {model_name}: no active pull state found.")
                return False


    def pull_model_with_progress(self, model_name: str,
                                 progress_callback: Optional[Callable[[str, int, int, bool], None]] = None,
                                 stop_event: Optional[threading.Event] = None) -> bool:
        """
        [호환성 유지 또는 내부용] 모델을 다운로드하고 진행 상황을 콜백으로 알립니다.
        Flask SSE와 직접 연동되지 않고, 기존 방식의 콜백을 사용하려는 경우에 사용될 수 있습니다.
        """
        logger.info(f"Direct call to pull_model_with_progress for {model_name} (may be used for non-SSE scenarios).")

        # 이 메서드는 start_model_pull과 get_model_pull_progress_stream을 내부적으로 사용하여
        # 기존 콜백 인터페이스를 만족시키도록 구현하거나,
        # 혹은 _pull_model_worker를 직접 호출하는 방식으로 구현할 수 있습니다.
        # 여기서는 start_model_pull을 호출하고, 진행 상황 스트림을 구독하여 콜백을 호출하는 방식으로 시뮬레이션합니다.

        success_init, msg_init = self.start_model_pull(model_name)
        if not success_init and "already in progress" not in msg_init.lower():
            logger.error(f"Failed to initiate direct pull for {model_name}: {msg_init}")
            if progress_callback: progress_callback(f"Failed to start pull: {msg_init}", 0,0,True)
            return False
        
        # 만약 stop_event가 외부에서 주입되었다면, OllamaService 내부의 stop_event와 동기화 필요
        # 또는, start_model_pull이 외부 stop_event를 받을 수 있도록 수정
        # 여기서는 stop_event를 무시하고, 필요시 self.stop_model_pull(model_name)을 호출하는 것으로 가정

        final_pull_result = False
        try:
            for progress_data in self.get_model_pull_progress_stream(model_name):
                if progress_callback:
                    # 기존 콜백의 is_error_or_done 플래그 재구성
                    is_done_for_cb = progress_data.get("done", False)
                    error_for_cb = progress_data.get("error")
                    status_for_cb = progress_data.get("status", "")
                    
                    # 기존 콜백은 마지막 'done'이 False일 때도 성공으로 간주하는 경우가 있었으므로,
                    # 'success' 상태를 명확히 확인
                    if status_for_cb.lower() == "success" and is_done_for_cb:
                        final_pull_result = True
                        # 기존 콜백은 "다운로드 완료" 메시지를 기대할 수 있음
                        progress_callback("다운로드 완료", progress_data.get("completed",0), progress_data.get("total",0), False) # is_done=False지만 성공
                        progress_callback("다운로드 완료", progress_data.get("completed",0), progress_data.get("total",0), True)  # 그 후 done=True로 완료 알림
                    elif error_for_cb and is_done_for_cb:
                        final_pull_result = False
                        progress_callback(str(error_for_cb), progress_data.get("completed",0), progress_data.get("total",0), True)
                    elif is_done_for_cb: # error 없이 done (예: 중지)
                        final_pull_result = False # 중지된 경우 성공은 아님
                        progress_callback(status_for_cb, progress_data.get("completed",0), progress_data.get("total",0), True)
                    else: # 진행 중
                        progress_callback(status_for_cb, progress_data.get("completed",0), progress_data.get("total",0), False)

                if progress_data.get("done"):
                    break
                
                if stop_event and stop_event.is_set(): # 외부 stop_event 체크
                    logger.info(f"External stop_event triggered for direct pull of {model_name}.")
                    self.stop_model_pull(model_name) # 서비스 내부 중지 요청
                    # 여기서 break하면 마지막 상태를 못 받을 수 있으므로, 스트림이 알아서 done 메시지 줄 때까지 대기
        except Exception as e:
            logger.error(f"Error in compatibility pull_model_with_progress for {model_name}: {e}", exc_info=True)
            if progress_callback: progress_callback(f"Streaming error: {e}",0,0,True)
            final_pull_result = False
            
        return final_pull_result