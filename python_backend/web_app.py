# web_app.py
from flask import Flask, render_template, request, jsonify, send_file, Response
from flask_cors import CORS
import os
import json
import threading
import queue # SSE 진행률 관리에 사용될 수 있음 (OllamaService 내부에서 관리)
import uuid
from datetime import datetime, timedelta
import tempfile # UPLOAD_FOLDER 기본 경로 설정에 사용되었으나 config.py로 이전
from werkzeug.utils import secure_filename
import time
import traceback
from functools import wraps
import logging # 로깅 모듈 추가
import logging.handlers # Add this import
import atexit
from typing import Optional, Dict, Any, List, Tuple, Callable # Optional 및 다른 필요한 타입 힌트 추가
import sys
from interfaces import AbsOcrHandler

from pptx import Presentation

from apscheduler.schedulers.background import BackgroundScheduler

# 모듈 import
import config # 설정 파일 import
from ollama_service import OllamaService
from translator import OllamaTranslator
from pptx_handler import PptxHandler
from chart_xml_handler import ChartXmlHandler
from excel_handler import ExcelHandler # 새로 추가
from ocr_handler import OcrHandlerFactory
import utils # 유틸리티 모듈 import (open_folder 등)

# --- 로거 설정 ---
# logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__) # Flask 기본 로거 사용 또는 위 주석 해제 후 커스텀
app = Flask(__name__, template_folder='templates', static_folder='static')

CORS(app)

# --- 로깅 설정 ---
def setup_logging(app_instance):
    log_dir = config.LOGS_DIR
    os.makedirs(log_dir, exist_ok=True)

    log_file_path = os.path.join(log_dir, 'app.log')
    
    # 기본 로거 가져오기
    root_logger = logging.getLogger()
    root_logger.setLevel(config.DEBUG_LOG_LEVEL if app_instance.debug else config.DEFAULT_LOG_LEVEL)

    # 기존 핸들러 제거 (중복 로깅 방지)
    for handler in root_logger.handlers[:]:
        root_logger.removeHandler(handler)

    # 파일 핸들러 추가 (매일 자정에 롤오버)
    file_handler = logging.handlers.TimedRotatingFileHandler(
        log_file_path, when='midnight', interval=1, backupCount=7, encoding='utf-8'
    )
    file_handler.setFormatter(logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s'))
    root_logger.addHandler(file_handler)

    # 콘솔 핸들러 추가 (개발 중에는 콘솔에도 출력)
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
    root_logger.addHandler(console_handler)

    logger.info(f"Logging initialized. Logs will be saved to {log_file_path}")

# --- 설정 로드 및 UPLOAD_FOLDER 설정 ---
app.config.from_object(config) # config.py의 설정들을 Flask app config로 로드
setup_logging(app) # 로깅 설정 함수 호출
# UPLOAD_FOLDER가 config.py에 정의되어 있으므로 app.config['UPLOAD_FOLDER']로 사용
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True) # 업로드 폴더 생성
app.config['MAX_CONTENT_LENGTH'] = 100 * 1024 * 1024  # 100MB (config.py로 이동 가능)

# 작업 관리를 위한 스레드 안전 딕셔너리 (기존 코드 유지)
tasks_lock = threading.Lock()
tasks = {} # 번역 작업 저장

# --- 다국어 리소스 로드 ---
language_resources = {}
def load_language_resources():
    global language_resources
    for lang_code in config.UI_SUPPORTED_LANGUAGES.keys():
        lang_file = os.path.join(config.LANGUAGES_DIR, f"{lang_code}.json")
        if os.path.exists(lang_file):
            try:
                with open(lang_file, 'r', encoding='utf-8') as f:
                    language_resources[lang_code] = json.load(f)
            except Exception as e:
                logger.error(f"Error loading language file {lang_file}: {e}")
                language_resources[lang_code] = {}
load_language_resources()


# --- 전역 서비스 인스턴스 ---
# OllamaService는 모델 다운로드 상태를 내부적으로 관리하도록 수정되었다고 가정
ollama_service = OllamaService()
translator = OllamaTranslator()
pptx_handler = PptxHandler()
excel_handler = ExcelHandler() # 새로 추가
# ChartXmlHandler 초기화 시 ollama_service 인스턴스 전달
chart_processor = ChartXmlHandler(translator, ollama_service)
ocr_handler_factory = OcrHandlerFactory()

def cleanup_on_exit():
    logger.info("애플리케이션 종료 시작... OCR 핸들러 정리 중.")
    if ocr_handler_factory and hasattr(ocr_handler_factory, 'cleanup_handlers'):
        ocr_handler_factory.cleanup_handlers()
    logger.info("OCR 핸들러 정리 완료.")
    # 다른 정리 작업이 있다면 여기에 추가

atexit.register(cleanup_on_exit) # 종료 시 cleanup_on_exit 함수 실행 등록


def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in config.ALLOWED_EXTENSIONS # config에서 ALLOWED_EXTENSIONS 사용

def error_handler(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except Exception as e:
            logger.error(f"Error in {func.__name__}: {str(e)}", exc_info=True)
            # 클라이언트 UI 언어에 맞춰 에러 메시지 반환 시도
            ui_lang_code = request.args.get('lang', config.DEFAULT_UI_LANGUAGE) # 또는 request.headers.get('Accept-Language') 파싱
            error_message_key = "error_generic_server_error" # 일반적인 서버 오류 키
            default_error_message = "An unexpected server error occurred."
            
            # 특정 예외 유형에 따라 다른 메시지 키 사용 가능
            # if isinstance(e, FileNotFoundError): error_message_key = "error_file_not_found"

            localized_message = language_resources.get(ui_lang_code, {}).get(error_message_key, str(e) or default_error_message)
            return jsonify({'error': localized_message, 'details': str(e) if app.debug else None}), 500
    return wrapper

# web_app.py 에 포함된 TaskProgress 클래스
class TaskProgress:
    def __init__(self, task_id, ui_language='ko'):
        self.task_id = task_id
        self.progress = 0
        self.status = language_resources.get(ui_language, {}).get("status_preparing", "Preparing...")
        self.current_task = ""
        self.queue = queue.Queue(maxsize=200) # SSE 메시지 큐 (크기 조절 가능)
        self.completed = False
        self.output_file = None
        self.error = None
        self.total_estimated_work = 0
        self.current_completed_work = 0
        self.current_ui_language = ui_language
        self.creation_time = time.time() # float 타임스탬프
        self.completion_time: Optional[float] = None # float 타임스탬프 또는 None
        self.original_filepath = None
        self.log_filepath = None
        self.stop_event = threading.Event()
        self.lock = threading.Lock() # 진행률 업데이트 동기화용

    def update_progress(self, location_key: str, task_type_key: str, weighted_work: float, text_snippet: str):
        with self.lock: # 스레드 안전하게 상태 업데이트
            if self.completed: # 이미 완료된 작업은 업데이트하지 않음
                return

            # 중단 요청 시 상태 변경 (한 번만)
            current_status_key_stopping = "status_stopping"
            stopping_text = language_resources.get(self.current_ui_language, {}).get(current_status_key_stopping, "Stopping...")
            if self.stop_event.is_set() and self.status != stopping_text :
                self.status = stopping_text
                self.current_task = language_resources.get(self.current_ui_language, {}).get("status_stopping_detail", "Stopping process...")[:50]
            elif not self.stop_event.is_set(): # 중단 요청이 아닐 때만 일반 상태 업데이트
                loc_text = language_resources.get(self.current_ui_language, {}).get(location_key, location_key)
                task_text = language_resources.get(self.current_ui_language, {}).get(task_type_key, task_type_key)
                self.status = f"{loc_text} - {task_text}"
                self.current_task = text_snippet[:50] if text_snippet else ""

            self.current_completed_work += weighted_work
            
            if self.total_estimated_work > 0:
                # 진행률은 0과 99 사이로 유지 (100%는 작업 완료 시에만 설정)
                self.progress = min(max(0, int((self.current_completed_work / self.total_estimated_work) * 100)), 99)
            
            update_data = {
                'progress': self.progress, 'status': self.status, 'current_task': self.current_task,
                'completed': self.completed, 'error': self.error
                # download_url은 작업 완료 시점에 추가
            }
            self._put_to_queue(update_data)

    def mark_as_final_status(self, final_status_key: str, error_message: Optional[str] = None, progress_val: Optional[int] = None):
        """작업의 최종 상태(완료, 오류, 중지 등)를 설정하고 큐에 알립니다."""
        with self.lock:
            if self.completed and self.status == language_resources.get(self.current_ui_language, {}).get(final_status_key, final_status_key):
                # 이미 동일한 최종 상태로 마크되었다면 중복 호출 방지 (선택적)
                # logger.debug(f"Task {self.task_id} already marked as {final_status_key}.")
                # return # 필요 시 활성화
                pass


            self.completed = True
            self.completion_time = time.time()
            self.status = language_resources.get(self.current_ui_language, {}).get(final_status_key, final_status_key)
            if error_message:
                self.error = str(error_message)[:200] # 오류 메시지 길이 제한
            if progress_val is not None:
                self.progress = min(max(0, progress_val), 100)
            elif self.error: # 오류 시 진행률은 현재 값 유지 또는 0으로 설정
                self.progress = min(self.progress, 99) # 오류 시 100% 안되도록
            else: # 정상 완료
                self.progress = 100

            final_update_data = {
                'progress': self.progress, 'status': self.status,
                'completed': self.completed, 'error': self.error,
                'current_task': self.current_task # 마지막 작업 내용 유지
            }
            if self.output_file and os.path.exists(self.output_file) and (not self.error or self.stop_event.is_set()):
                final_update_data['download_url'] = f'/api/download/{self.task_id}'
                final_update_data['temp_output_path'] = self.output_file 

            
            self._put_to_queue(final_update_data)
            logger.info(f"Task {self.task_id} marked as final status: {self.status}, Progress: {self.progress}%, Error: {self.error}")

            # 큐에 더 이상 넣을 데이터가 없음을 알리는 특별한 메시지 (선택적, SSE 스트림 종료용)
            # self._put_to_queue({"_task_stream_end_": True})


    def _put_to_queue(self, data: dict):
        """SSE 큐에 데이터를 추가합니다. 큐가 가득 차면 가장 오래된 것을 제거합니다."""
        try:
            self.queue.put_nowait(data)
        except queue.Full:
            try:
                self.queue.get_nowait() # 가장 오래된 데이터 제거
                self.queue.put_nowait(data) # 새 데이터 추가
                logger.warning(f"Task {self.task_id} progress queue was full. Oldest item discarded.")
            except queue.Empty: # 거의 발생 안 함
                pass
            except Exception as e_put_retry:
                 logger.error(f"Task {self.task_id} progress queue retry put failed: {e_put_retry}")
        except Exception as e_put:
            logger.error(f"Task {self.task_id} progress queue put failed: {e_put}")

@app.route('/')
def index_route(): # 함수 이름 변경 (index는 파이썬 예약어와 혼동 가능성)
    return render_template('index.html')

@app.route('/api/check_ollama')
@error_handler
def check_ollama_route(): # 함수 이름 변경
    installed = ollama_service.is_installed()
    running, port = ollama_service.is_running()
    models = []
    if running:
        models = ollama_service.get_text_models() # 모델 목록 가져오기
    return jsonify({'installed': installed, 'running': running, 'port': port, 'models': models})

@app.route('/api/start_ollama', methods=['POST'])
@error_handler
def start_ollama_route(): # 함수 이름 변경
    success = ollama_service.start_ollama()
    return jsonify({'success': success})

# --- 신규 API: 모델 다운로드 시작 ---
@app.route('/api/pull_model', methods=['POST'])
@error_handler
def pull_model_route():
    data = request.json
    model_name = data.get('model_name')
    ui_lang_code = data.get('ui_language', config.DEFAULT_UI_LANGUAGE)

    if not model_name:
        error_msg = language_resources.get(ui_lang_code, {}).get("error_model_name_required", "Model name is required.")
        return jsonify({'success': False, 'error': error_msg}), 400

    # OllamaService를 통해 모델 다운로드 시작 (비동기)
    # start_model_pull은 (성공여부, 메시지) 튜플을 반환하도록 OllamaService에 구현 가정
    success, message = ollama_service.start_model_pull(model_name)
    
    if success:
        # 이미 진행 중이거나, 새로 시작했거나 모두 클라이언트에게는 '시작됨'으로 응답 가능
        return jsonify({'success': True, 'message': message}), 202 # 202 Accepted: 요청 수락, 처리 중
    else:
        # 시작 자체에 실패한 경우 (예: Ollama 서비스 연결 불가 등 OllamaService 내부 판단)
        return jsonify({'success': False, 'error': message}), 500


# --- 신규 API: 모델 다운로드 진행 상황 SSE ---
@app.route('/api/model_pull_progress/<model_name>')
def model_pull_progress_sse_route(model_name):
    # OllamaService로부터 해당 모델의 진행 상황 스트림을 받아 SSE로 전송
    # get_model_pull_progress_stream은 제너레이터를 반환하도록 OllamaService에 구현 가정
    # 제너레이터는 {"status": ..., "completed": ..., "total": ..., "done": ..., "error": ...} 형식의 dict를 yield
    
    # 클라이언트 UI 언어 가져오기 (진행률 메시지 다국어화 위함, 스트림 데이터 자체는 고정 포맷)
    # ui_lang_code = request.args.get('lang', config.DEFAULT_UI_LANGUAGE) # 필요시 사용

    def generate_sse_from_service(model_name_local):
        try:
            for progress_data in ollama_service.get_model_pull_progress_stream(model_name_local):
                yield f"data: {json.dumps(progress_data)}\n\n"
                if progress_data.get("done"):
                    break # 완료 또는 오류 시 스트림 종료
        except Exception as e:
            logger.error(f"Error in SSE stream for model {model_name_local}: {e}", exc_info=True)
            error_data = {"status": "SSE stream error", "done": True, "error": str(e)}
            yield f"data: {json.dumps(error_data)}\n\n"
        finally:
            logger.info(f"SSE stream for model pull {model_name_local} ended.")
            # OllamaService의 get_model_pull_progress_stream 내부에서 해당 subscriber 정리 필요

    return Response(generate_sse_from_service(model_name), mimetype='text/event-stream')


@app.route('/api/translate', methods=['POST'])
@error_handler
def start_translation_route(): # 함수 이름 변경
    data = request.json
    required_fields = ['filepath', 'src_lang', 'tgt_lang', 'model']
    ui_lang_code = data.get('ui_language', config.DEFAULT_UI_LANGUAGE)

    missing_fields = [field for field in required_fields if not data.get(field)]
    if missing_fields:
        error_msg_template = language_resources.get(ui_lang_code, {}).get("error_missing_params", "Missing required parameters: {fields}")
        error_msg = error_msg_template.replace("{fields}", ", ".join(missing_fields))
        return jsonify({'error': error_msg}), 400

    filepath = data['filepath']
    if not os.path.exists(filepath) or not os.path.isfile(filepath): # 파일 존재 및 파일인지 확인
        error_msg = language_resources.get(ui_lang_code, {}).get("error_file_not_found_on_server", "File not found on server.")
        return jsonify({'error': error_msg}), 404

    task_id = str(uuid.uuid4())
    # TaskProgress 생성 시 ui_language 전달
    task = TaskProgress(task_id, ui_language=ui_lang_code)
    task.original_filepath = filepath # 원본 파일 경로 저장
    task.original_folder_path = data.get('original_folder_path') # [!INFO] 이 줄을 추가합니다.


    with tasks_lock:
        tasks[task_id] = task

        file_extension = filepath.rsplit('.', 1)[1].lower()

        target_worker = None
        worker_args = ()

        if file_extension == 'pptx':
            target_worker = translate_worker
            worker_args = (
                task_id, filepath, data['src_lang'], data['tgt_lang'], data['model'],
                data.get('image_translation', True),
                data.get('ocr_temperature', config.DEFAULT_ADVANCED_SETTINGS['ocr_temperature']),
                data.get('ocr_use_gpu', config.DEFAULT_ADVANCED_SETTINGS['ocr_use_gpu'])
            )
        elif file_extension == 'xlsx':
            target_worker = translate_excel_worker
            worker_args = (
                task_id, filepath, data['src_lang'], data['tgt_lang'], data['model']
            )

        if target_worker:
            thread = threading.Thread(target=target_worker, args=worker_args)
            thread.daemon = True
            thread.start()
            return jsonify({'task_id': task_id})
        else:
            # 지원하지 않는 파일 형식 오류 반환
            return jsonify({'error': 'Unsupported file type for translation.'}), 400
        # --- 수정 끝 ---

def translate_worker(task_id, filepath, src_lang, tgt_lang, model,
                    image_translation, ocr_temperature, ocr_use_gpu):
    task = tasks.get(task_id)
    if not task:
        logger.error(f"Translate worker: Task {task_id} not found.")
        return

    task_log_filename = f"task_{task_id}_{datetime.now().strftime('%Y%m%d%H%M%S')}.log"
    log_dir_path = config.LOGS_DIR if hasattr(config, 'LOGS_DIR') and config.LOGS_DIR else os.path.join(app.config['UPLOAD_FOLDER'], 'task_logs')
    
    # 로그 디렉토리 생성 시도
    if not utils.ensure_directory_exists(log_dir_path):
        task.mark_as_final_status("status_error_log_setup", f"Failed to create log directory: {log_dir_path}")
        return # 로그 설정 실패 시 작업 중단

    task.log_filepath = os.path.join(log_dir_path, task_log_filename)

    original_filename_base = os.path.splitext(os.path.basename(filepath))[0]
    output_filename = f"{original_filename_base}_{tgt_lang}_translated_{task_id[:8]}.pptx"
    output_path = os.path.join(app.config['UPLOAD_FOLDER'], output_filename)
    task.output_file = output_path # 초기 output_file 경로 설정

    prs = None # Presentation 객체 참조

    try:
        task.update_progress("status_key_file_info", "status_task_analyzing", 0, os.path.basename(filepath))
        file_info = pptx_handler.get_file_info(filepath)
        if "error" in file_info or not isinstance(file_info, dict):
            err_msg = file_info.get("error", "Unknown error during file analysis") if isinstance(file_info, dict) else "Invalid file_info format"
            task.mark_as_final_status("status_error_file_analysis", err_msg)
            return

        task.total_estimated_work = (
            file_info.get("total_text_char_count", 0) * config.WEIGHT_TEXT_CHAR +
            file_info.get("image_elements_count", 0) * config.WEIGHT_IMAGE +
            file_info.get("chart_elements_count", 0) * config.WEIGHT_CHART
        )
        if task.total_estimated_work <= 0: task.total_estimated_work = 1 # 0으로 나누기 방지

        ocr_handler_instance: Optional[AbsOcrHandler] = None # 타입 명시
        if image_translation:
            src_lang_name_for_ocr = config.TRANSLATION_LANGUAGES_MAP.get(src_lang, src_lang)
            # get_ocr_handler 내부에서 캐싱/생성 처리
            ocr_handler_instance = ocr_handler_factory.get_ocr_handler(
                src_lang_name_for_ocr, ocr_use_gpu, debug_enabled=app.debug
            )
            if not ocr_handler_instance:
                logger.warning(f"OCR Handler for {src_lang_name_for_ocr} (GPU: {ocr_use_gpu}) could not be initialized. Image translation will be skipped.")
                # 사용자에게 알림 (예: task.status 업데이트 또는 로그)
                # task.update_progress("status_key_ocr_setup", "status_task_ocr_skip_no_handler", 0, "")


        prs = Presentation(filepath) # 여기서 Presentation 객체 로드
        src_lang_name = config.TRANSLATION_LANGUAGES_MAP.get(src_lang, src_lang)
        tgt_lang_name = config.TRANSLATION_LANGUAGES_MAP.get(tgt_lang, tgt_lang)
        
        ui_tgt_lang_display_name = src_lang # 기본값
        for code, name_map in config.TRANSLATION_LANGUAGES_MAP.items():
            if code == tgt_lang: # tgt_lang은 'ko', 'en' 같은 코드
                # 이 코드를 사용하는 UI 언어 이름을 찾아야 함 (예: 'ko' -> '한국어')
                ui_tgt_lang_display_name = next((ui_name for ui_code, ui_name in config.UI_SUPPORTED_LANGUAGES.items() if ui_code == tgt_lang), tgt_lang)
                break
        
        font_code_for_render = config.UI_LANG_TO_FONT_CODE_MAP.get(ui_tgt_lang_display_name, 'en')


        task.update_progress("status_key_stage1_prep", "status_task_translating_text_images", 0, "")
        success_stage1 = pptx_handler.translate_presentation_stage1(
            prs, src_lang_name, tgt_lang_name,
            translator, ocr_handler_instance, model, ollama_service, font_code_for_render, task.log_filepath,
            task.update_progress, task.stop_event, image_translation, ocr_temperature
        )

        if task.stop_event.is_set():
            task.mark_as_final_status("status_stopped_before_charts")
            # 중지 시에도 현재까지 작업된 파일 저장 시도
            try:
                if prs: prs.save(output_path)
                logger.info(f"Task {task_id} (stopped): Partially translated file saved to {output_path}")
            except Exception as e_save:
                logger.error(f"Task {task_id} (stopped): Error saving partially translated file: {e_save}")
            return # 작업자 스레드 종료

        if not success_stage1:
            task.mark_as_final_status("status_error_stage1_failed", language_resources.get(task.current_ui_language, {}).get("error_stage1_translation_failed_detail", "Text/Image translation stage failed internally."))
            return

        task.update_progress("status_key_stage2_prep", "status_task_translating_charts", 0, "")
        temp_chart_input_path = os.path.join(app.config['UPLOAD_FOLDER'], f"temp_chart_input_{task_id}.pptx")
        
        try:
            if prs: prs.save(temp_chart_input_path)
        except Exception as e_save_temp:
            task.mark_as_final_status("status_error_saving_temp_chart_file", f"Failed to save temp file for chart processing: {e_save_temp}")
            return

        final_path_from_chart_proc = chart_processor.translate_charts_in_pptx(
            temp_chart_input_path, src_lang_name, tgt_lang_name, model,
            output_path=output_path, # 최종 출력 경로 지정
            progress_callback_item_completed=task.update_progress,
            stop_event=task.stop_event,
            task_log_filepath=task.log_filepath
        )

        if os.path.exists(temp_chart_input_path):
            try: os.remove(temp_chart_input_path)
            except Exception as e_remove: logger.warning(f"Could not remove temporary chart input file {temp_chart_input_path}: {e_remove}")

        if task.stop_event.is_set():
            task.mark_as_final_status("status_stopped_during_charts")
            # output_path에 이미 일부 차트 번역된 파일이 저장되었을 수 있음
            logger.info(f"Task {task_id} (stopped during charts): Output (if any) at {output_path}")
            return

        if final_path_from_chart_proc and os.path.exists(final_path_from_chart_proc):
            task.output_file = final_path_from_chart_proc # 최종 파일 경로 업데이트 (실제 chart_processor가 반환한 경로)
            task.mark_as_final_status("status_completed", progress_val=100)
        else:
            task.mark_as_final_status("status_error_chart_failed", language_resources.get(task.current_ui_language, {}).get("error_chart_translation_failed_detail", "Chart translation failed or output file not created by chart processor."))

    except InterruptedError: # task.stop_event.is_set()으로 이미 처리되지만, 명시적 예외 처리
        logger.info(f"Task {task_id} was explicitly interrupted. Status already set by stop_event check.")
        # mark_as_final_status는 stop_event 체크 로직에서 호출되었을 것임
    except Exception as e:
        logger.error(f"Error in translate_worker (task {task_id}): {str(e)}", exc_info=True)
        # 오류 발생 시에도 task.mark_as_final_status 호출
        task.mark_as_final_status("status_error_worker_exception", f"Unhandled exception in worker: {str(e)}")
    finally:
        # prs 객체는 지역 변수이므로 스코프 벗어나면 자동으로 정리됨.
        # 명시적인 파일 닫기 등은 Presentation 라이브러리 내부에서 처리.
        
        # 작업 완료/오류/중지 시 최종 상태를 한 번 더 큐에 넣어 SSE 스트림이 확실히 종료되도록 함.
        # mark_as_final_status에서 이미 처리됨.
        
        # 번역 히스토리 저장 (task의 최종 status 사용)
        # output_file이 None이거나 존재하지 않을 수 있으므로 체크
        final_output_filename = os.path.basename(task.output_file) if task.output_file and os.path.exists(task.output_file) else ""
        save_translation_history(
            os.path.basename(filepath),
            final_output_filename,
            src_lang, tgt_lang, model, task.status, task_id # task.status는 이미 번역된 텍스트일 수 있음. 키로 저장하려면 변경 필요.
        )
        logger.info(f"Translate worker for task {task_id} finished. Final status: {task.status}")

def translate_excel_worker(task_id, filepath, src_lang, tgt_lang, model):
    task = tasks.get(task_id)
    if not task:
        logger.error(f"Excel Translate worker: Task {task_id} not found.")
        return

    original_filename_base = os.path.splitext(os.path.basename(filepath))[0]
    output_filename = f"{original_filename_base}_{tgt_lang}_translated_{task_id[:8]}.xlsx"
    output_path = os.path.join(app.config['UPLOAD_FOLDER'], output_filename)
    task.output_file = output_path

    try:
        task.update_progress("status_key_file_info", "status_task_analyzing", 0, os.path.basename(filepath))
        file_info = excel_handler.get_file_info(filepath)
        if file_info.get("error"):
            raise Exception(file_info["error"])

        task.total_estimated_work = (file_info.get("translatable_cell_count", 0) * config.WEIGHT_EXCEL_CELL) # config에 WEIGHT_EXCEL_CELL = 1 추가 권장
        if task.total_estimated_work <= 0: task.total_estimated_work = 1

        src_lang_name = config.TRANSLATION_LANGUAGES_MAP.get(src_lang, src_lang)
        tgt_lang_name = config.TRANSLATION_LANGUAGES_MAP.get(tgt_lang, tgt_lang)

        final_path = excel_handler.translate_workbook(
            filepath, output_path, translator, src_lang_name, tgt_lang_name,
            model, ollama_service, task.update_progress, task.stop_event
        )

        if task.stop_event.is_set():
            task.mark_as_final_status("status_stopped")
            return

        if final_path and os.path.exists(final_path):
            task.output_file = final_path
            task.mark_as_final_status("status_completed", progress_val=100)
        else:
            task.mark_as_final_status("status_error", "Excel translation failed or output file not created.")

    except Exception as e:
        logger.error(f"Error in translate_excel_worker (task {task_id}): {e}", exc_info=True)
        task.mark_as_final_status("status_error_worker_exception", f"Unhandled exception in worker: {str(e)}")
    finally:
        save_translation_history(
            os.path.basename(filepath),
            os.path.basename(task.output_file) if task.output_file else "",
            src_lang, tgt_lang, model, task.status, task_id
        )
        logger.info(f"Excel Translate worker for task {task_id} finished. Final status: {task.status}")

# --- 기존 APScheduler 및 나머지 라우트들 ---
scheduler = BackgroundScheduler(daemon=True)
def cleanup_old_files_and_tasks():
    with app.app_context(): # Flask 애플리케이션 컨텍스트 내에서 실행
        try:
            current_time = time.time()
            # 파일 정리
            retention_days = app.config.get('FILE_RETENTION_DAYS', 7) # app.config 사용
            cutoff_time_files = current_time - (retention_days * 24 * 60 * 60)
            upload_dir = app.config['UPLOAD_FOLDER']

            if os.path.exists(upload_dir):
                for filename in os.listdir(upload_dir):
                    filepath = os.path.join(upload_dir, filename)
                    try:
                        if os.path.isfile(filepath) and os.path.getmtime(filepath) < cutoff_time_files:
                            os.remove(filepath)
                            logger.info(f"Removed old uploaded file: {filepath}")
                    except Exception as e:
                        logger.error(f"Error removing old uploaded file {filepath}: {e}")
            
            # 로그 파일 정리 (config.LOGS_DIR 경로에 있는 task_{task_id}_YYYYMMDDHHMMSS.log 형식)
            log_dir_to_clean = config.LOGS_DIR if hasattr(config, 'LOGS_DIR') and config.LOGS_DIR else os.path.join(app.config['UPLOAD_FOLDER'], 'task_logs')
            if os.path.exists(log_dir_to_clean):
                 for filename in os.listdir(log_dir_to_clean):
                    if filename.startswith("task_") and filename.endswith(".log"):
                        filepath = os.path.join(log_dir_to_clean, filename)
                        try:
                            if os.path.isfile(filepath) and os.path.getmtime(filepath) < cutoff_time_files: # 동일한 보존 기간 적용
                                os.remove(filepath)
                                logger.info(f"Removed old task log file: {filepath}")
                        except Exception as e:
                            logger.error(f"Error removing old task log file {filepath}: {e}")


            # 작업 객체 정리
            retention_hours = app.config.get('TASK_RETENTION_HOURS', 24) # app.config 사용
            cutoff_time_tasks = current_time - (retention_hours * 60 * 60)
            with tasks_lock:
                tasks_to_delete = [
                    task_id for task_id, task_obj in tasks.items()
                    if task_obj.completed and task_obj.completion_time and task_obj.completion_time < cutoff_time_tasks
                ]
                for task_id in tasks_to_delete:
                    del tasks[task_id]
                    logger.info(f"Removed old task object from memory: {task_id}")
        except Exception as e:
            logger.error(f"Error during cleanup_old_files_and_tasks: {e}", exc_info=True)


@app.route('/api/progress/<task_id>')
def get_progress_route(task_id):
    task = tasks.get(task_id)
    if not task:
        ui_lang_code = request.args.get('lang', config.DEFAULT_UI_LANGUAGE)
        error_msg = language_resources.get(ui_lang_code, {}).get("error_task_not_found", "Task not found.")
        # 작업이 없을 경우, 클라이언트가 재시도하지 않도록 명확한 에러 또는 빈 스트림 후 종료.
        # 여기서는 404 에러를 JSON으로 반환하는 것이 더 적절할 수 있음.
        # return jsonify({'error': error_msg}), 404
        # 또는 SSE 스트림으로 에러를 보내고 바로 닫기:
        def empty_or_error_stream():
            error_data = {'progress': 0, 'status': error_msg, 'completed': True, 'error': error_msg, 'current_task': ""}
            yield f"data: {json.dumps(error_data)}\n\n"
            logger.warning(f"SSE stream requested for non-existent task {task_id}.")
        return Response(empty_or_error_stream(), mimetype='text/event-stream')


    def generate_sse_for_task():
        # 연결 시 현재까지의 상태 즉시 전송
        with task.lock: # task 객체 상태 접근 시 락 사용
            initial_data = {
                'progress': task.progress, 'status': task.status,
                'current_task': task.current_task, 'completed': task.completed,
                'error': task.error
            }
            if task.output_file and os.path.exists(task.output_file) and (not task.error or task.stop_event.is_set()):
                initial_data['download_url'] = f'/api/download/{task_id}'
        
        yield f"data: {json.dumps(initial_data)}\n\n"
        logger.debug(f"SSE stream for task {task_id}: Sent initial state.")

        # 작업이 이미 완료된 상태로 시작될 수 있으므로 체크
        if initial_data['completed']:
            logger.info(f"SSE stream for task {task_id}: Task already completed. Closing stream.")
            return # 스트림 바로 종료

        while True: # task.completed 될 때까지 또는 예외 발생 시까지 루프
            try:
                # 큐에서 새 진행률 데이터 가져오기 (타임아웃 설정)
                # 타임아웃을 짧게 (예: 1초) 하여 stop_event나 task.completed를 더 자주 체크
                data_from_queue = task.queue.get(timeout=1.0) # 1초 타임아웃

                # "_task_stream_end_" 같은 특별한 메시지로 스트림 종료 신호 처리 (선택적)
                # if isinstance(data_from_queue, dict) and data_from_queue.get("_task_stream_end_"):
                #    logger.info(f"SSE stream for task {task_id}: Received explicit end signal. Closing stream.")
                #    break
                
                # 클라이언트에 데이터 전송
                yield f"data: {json.dumps(data_from_queue)}\n\n"

                # 받은 데이터가 최종 완료 상태를 나타내면 루프 종료
                if data_from_queue.get('completed'):
                    logger.info(f"SSE stream for task {task_id}: Received 'completed' in data. Closing stream.")
                    break
            
            except queue.Empty: # 타임아웃 (큐에 새 데이터 없음)
                # 작업이 실제로 완료되었는지, 또는 중단되었는지 다시 확인
                with task.lock:
                    is_task_really_done = task.completed
                    current_progress_on_timeout = task.progress
                    current_status_on_timeout = task.status
                    current_task_on_timeout = task.current_task
                    current_error_on_timeout = task.error

                if is_task_really_done:
                    logger.info(f"SSE stream for task {task_id}: Task confirmed completed during queue timeout. Closing stream.")
                    # 마지막 상태 한 번 더 보내고 종료 (선택적, mark_as_final_status에서 이미 보냈을 수 있음)
                    final_heartbeat_data = {
                        'progress': current_progress_on_timeout, 'status': current_status_on_timeout,
                        'completed': True, 'error': current_error_on_timeout,
                        'current_task': current_task_on_timeout
                    }
                    if task.output_file and os.path.exists(task.output_file) and (not current_error_on_timeout or task.stop_event.is_set()):
                        final_heartbeat_data['download_url'] = f'/api/download/{task_id}'
                    yield f"data: {json.dumps(final_heartbeat_data)}\n\n"
                    break
                else:
                    # 아직 작업 진행 중, heartbeat 역할 (또는 현재 상태 다시 전송)
                    heartbeat_data = {
                        'progress': current_progress_on_timeout, 'status': current_status_on_timeout,
                        'completed': False, 'error': current_error_on_timeout,
                        'current_task': current_task_on_timeout
                    }
                    yield f"data: {json.dumps(heartbeat_data)}\n\n" # 현재 상태를 heartbeat처럼 사용
            
            except GeneratorExit: # 클라이언트가 연결을 끊은 경우
                logger.info(f"SSE client for task {task_id} disconnected (GeneratorExit). Stream closing.")
                break
            except Exception as e:
                logger.error(f"Error during SSE generation for task {task_id}: {e}", exc_info=True)
                error_stream_data = {'progress': task.progress, 'status': "SSE Stream Error", 'completed': True, 'error': str(e), 'current_task': task.current_task}
                yield f"data: {json.dumps(error_stream_data)}\n\n"
                break
        
        logger.info(f"SSE stream for task {task_id} officially ended.")

    return Response(generate_sse_for_task(), mimetype='text/event-stream')


@app.route('/api/download/<task_id>')
def download_file_route(task_id): # 함수 이름 변경
    task = tasks.get(task_id)
    ui_lang_code = request.args.get('lang', config.DEFAULT_UI_LANGUAGE) # 또는 task.current_ui_language 사용
    if task: ui_lang_code = task.current_ui_language # 작업 생성 시 언어 사용

    if not task or not task.output_file or not os.path.exists(task.output_file):
        error_msg = language_resources.get(ui_lang_code, {}).get("error_file_not_found_for_download", "File not found for download.")
        return jsonify({'error': error_msg}), 404
    
    # 사용자가 다운로드 시 파일명에 원본 파일명 유지하도록 개선
    download_name = os.path.basename(task.output_file) # 서버에 저장된 이름
    if task.original_filepath: # 원본 파일명이 있다면 그것을 기반으로 다운로드 파일명 생성
        base, ext = os.path.splitext(os.path.basename(task.original_filepath))
        # 대상 언어 코드를 가져오기 (task 객체에 저장되어 있다면 사용, 아니면 history에서 가져오거나 기본값)
        # 이 부분은 save_translation_history 와 TaskProgress 객체에 tgt_lang 정보가 저장되어야 함.
        # 여기서는 output_file 명에 tgt_lang 이 포함되어 있다고 가정하고 그대로 사용.
        # 또는 history에서 task_id로 찾아와서 사용
        history_file = os.path.join(config.HISTORY_DIR, "translation_history.json")
        target_lang_code_for_filename = "translated"
        if os.path.exists(history_file):
            try:
                with open(history_file, 'r', encoding='utf-8') as f:
                    history_list = json.load(f)
                    entry = next((item for item in history_list if item.get("id") == task_id), None)
                    if entry and entry.get("tgt"):
                        target_lang_code_for_filename = entry.get("tgt")
            except: pass # 오류 발생 시 기본값 사용
        
        download_name = f"{base}_{target_lang_code_for_filename}{ext}"


    return send_file(task.output_file, as_attachment=True, download_name=download_name)

@app.route('/api/stop_translation/<task_id>', methods=['POST'])
@error_handler
def stop_translation_task_route(task_id): # 함수 이름 변경
    task = tasks.get(task_id)
    ui_lang_code = request.args.get('lang', config.DEFAULT_UI_LANGUAGE)
    if task: ui_lang_code = task.current_ui_language

    if not task:
        error_msg = language_resources.get(ui_lang_code, {}).get("error_task_not_found_to_stop", "Task to stop not found.")
        return jsonify({'error': error_msg}), 404
    if task.completed:
        error_msg = language_resources.get(ui_lang_code, {}).get("error_task_already_completed", "Task already completed.")
        return jsonify({'error': error_msg, 'already_completed': True}), 400 # 이미 완료됨을 명시
    
    task.stop_event.set()
    # 추가: 모델 다운로드 중지 요청 (OllamaService에 해당 기능 구현 필요)
    # if task.current_task == "모델 다운로드 중" and hasattr(ollama_service, 'stop_model_pull'):
    #    model_being_pulled = ... # 알아낼 방법 필요
    #    ollama_service.stop_model_pull(model_being_pulled)

    success_msg = language_resources.get(ui_lang_code, {}).get("success_stop_signal_sent", "Stop signal sent to task {task_id}.")
    return jsonify({'message': success_msg.replace("{task_id}", task_id)}), 200

# --- 나머지 기존 라우트들 (history, open_log_folder, file_info, languages, ui_languages) ---
# 함수명 변경 및 app.config 사용, 다국어 메시지 적용 등 유사하게 수정 가능
history_lock = threading.Lock()
def save_translation_history(original_filename, translated_filename, src_lang, tgt_lang, model, status_key, task_id):
    # status_key는 "status_completed", "status_error" 등의 i18n 키로 전달받고,
    # 실제 저장 시에는 현재 UI 언어의 번역된 텍스트를 저장하거나, 혹은 영어 기준 고정 텍스트 저장
    # 여기서는 전달받은 status_key를 그대로 저장 (클라이언트가 필요시 번역하도록)
    history_file = os.path.join(config.HISTORY_DIR, "translation_history.json") # config 사용
    os.makedirs(config.HISTORY_DIR, exist_ok=True) # config 사용
    
    new_entry = {
        "id": task_id,
        "name": original_filename, # 원본 파일명
        "translated_name": translated_filename, # 번역된 파일명 (서버 저장 이름)
        "src": src_lang, # 소스 언어 코드
        "tgt": tgt_lang, # 타겟 언어 코드
        "model": model,  # 사용된 모델
        "status_key": status_key, # 상태 (i18n 키 또는 고정 영문)
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    with history_lock:
        history = []
        if os.path.exists(history_file):
            try:
                with open(history_file, 'r', encoding='utf-8') as f: history = json.load(f)
            except Exception as e: logger.error(f"Error reading history file: {e}"); history = []
        
        history.insert(0, new_entry)
        history = history[:app.config.get('MAX_HISTORY_ITEMS', 50)] # app.config 사용 및 기본값 설정

        try:
            with open(history_file, 'w', encoding='utf-8') as f: json.dump(history, f, ensure_ascii=False, indent=4)
        except Exception as e: logger.error(f"Error writing history file: {e}")


@app.route('/api/history')
@error_handler
def get_history_route():
    history_file = os.path.join(config.HISTORY_DIR, "translation_history.json")
    history_data = []
    if os.path.exists(history_file):
        try:
            with open(history_file, 'r', encoding='utf-8') as f:
                history_data_raw = json.load(f)
                # 클라이언트에 전달 시 status_key를 현재 요청 언어에 맞게 번역
                ui_lang_code = request.args.get('lang', config.DEFAULT_UI_LANGUAGE)
                for item in history_data_raw:
                    status_text = language_resources.get(ui_lang_code, {}).get(item.get("status_key"), item.get("status_key", "Unknown"))
                    item["status"] = status_text # 번역된 status 텍스트 추가
                history_data = history_data_raw
        except Exception as e:
            logger.error(f"Error loading or processing history: {e}")
            history_data = [] # 오류 시 빈 목록 반환
    return jsonify(history_data)


@app.route('/api/file_info', methods=['POST'])
@error_handler
def get_file_info_route():
    ui_lang_code = request.args.get('lang', config.DEFAULT_UI_LANGUAGE)
    if 'file' not in request.files:
        error_msg = language_resources.get(ui_lang_code, {}).get("error_no_file_provided", "No file provided.")
        return jsonify({'error': error_msg}), 400
    file = request.files['file']
    if file.filename == '':
        error_msg = language_resources.get(ui_lang_code, {}).get("error_no_file_selected", "No file selected.")
        return jsonify({'error': error_msg}), 400

    # [!INFO] --- 여기서부터 로직이 크게 개선됩니다 ---
    if file and allowed_file(file.filename):
        try:
            # 1. 원본 파일명에서 이름과 확장자를 먼저 분리합니다.
            original_basename, file_extension_with_dot = os.path.splitext(file.filename)
            file_extension = file_extension_with_dot[1:].lower() # '.' 제거

            # 2. 순수 파일 이름 부분만 secure_filename으로 처리합니다.
            safe_basename = secure_filename(original_basename)
            
            # 3. 안전한 파일명과 원본 확장자를 조합하여 서버에 저장될 파일명을 만듭니다.
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S_%f')
            # 저장될 최종 파일명: 타임스탬프 + 안전한 파일명 + 원본 확장자
            server_filename = f"{timestamp}_{safe_basename}{file_extension_with_dot}"
            filepath = os.path.join(app.config['UPLOAD_FOLDER'], server_filename)

            file.save(filepath)
            
            # 4. 파일 정보 분석
            info = {}
            if file_extension == 'pptx':
                info = pptx_handler.get_file_info(filepath)
            elif file_extension == 'xlsx':
                info = excel_handler.get_file_info(filepath)
            else:
                raise ValueError("지원하지 않는 파일 형식입니다.")

            if "error" in info and info["error"]:
                 raise Exception(info["error"])

            # 5. 프론트엔드에는 원본 파일명을 보여줍니다.
            return jsonify({'filename': file.filename, 'filepath': filepath, 'info': info })
        
        except Exception as e:
            # 오류 처리 로직은 기존과 동일
            if 'filepath' in locals() and os.path.exists(filepath):
                try: os.remove(filepath)
                except Exception as e_remove: logger.error(f"Error removing uploaded file on failure: {e_remove}")
            
            logger.error(f"Error processing file info for {file.filename}: {e}", exc_info=True)
            error_msg = language_resources.get(ui_lang_code, {}).get("error_file_processing_failed", "Failed to process file.")
            return jsonify({'error': error_msg, 'details': str(e)}), 500
    
    # allowed_file에서 False가 반환된 경우
    error_msg = language_resources.get(ui_lang_code, {}).get("error_invalid_file_type", "Invalid file type.")
    return jsonify({'error': error_msg}), 400

@app.route('/api/ui_languages')
@error_handler
def get_ui_languages_route():
    current_lang = request.args.get('lang', config.DEFAULT_UI_LANGUAGE)
    if current_lang not in language_resources:
        current_lang = config.DEFAULT_UI_LANGUAGE
    return jsonify({
        'supported_languages': config.UI_SUPPORTED_LANGUAGES,
        'current_language': current_lang,
        'resources': language_resources.get(current_lang, {})
    })


if __name__ == '__main__':
    default_flask_port = 5001  # UI의 일반적인 시작 포트 또는 findFreePort의 시작점
    flask_port_to_use = default_flask_port

    port_env_var = os.environ.get('FLASK_PORT')
    if port_env_var:
        try:
            flask_port_to_use = int(port_env_var)
            # main.js 로부터 FLASK_PORT를 성공적으로 받았음을 명시
            print(f"--- Flask app using FLASK_PORT (from Electron main.js): {flask_port_to_use} ---", flush=True)
        except ValueError:
            print(f"--- WARNING: Invalid FLASK_PORT value '{port_env_var}'. Using default port {default_flask_port}. ---", flush=True)
            flask_port_to_use = default_flask_port
    else:
        # 이 경우는 main.js에서 FLASK_PORT를 설정하지 못했거나, web_app.py를 직접 실행한 경우
        print(f"--- FLASK_PORT environment variable not set. Using default port: {default_flask_port} ---", flush=True)

    print(f"--- Flask app attempting to run on host 127.0.0.1, port {flask_port_to_use} ---", flush=True)
    sys.stdout.flush()

    # (스케줄러 설정 코드는 기존대로 유지)
    scheduler = BackgroundScheduler(daemon=True)
    cleanup_hour = app.config.get('CLEANUP_HOUR', 3)
    if not scheduler.running:
        scheduler.add_job(cleanup_old_files_and_tasks, 'cron', hour=cleanup_hour, minute=0, id="cleanup_job", replace_existing=True)
        try:
            scheduler.start()
            logger.info(f"File and task cleanup scheduler started (runs daily at {cleanup_hour:02}:00).")
        except Exception as e_scheduler:
            if "conflicting job" in str(e_scheduler).lower() or "already running" in str(e_scheduler).lower():
                 logger.warning(f"Scheduler job 'cleanup_job' already exists or scheduler already running. Details: {e_scheduler}")
            else:
                logger.error(f"Failed to start scheduler: {e_scheduler}")

    try:
        app.run(debug=app.config.get('DEBUG', False), host='127.0.0.1', port=flask_port_to_use)
        # app.run이 정상적으로 시작했다가 종료된 경우 (일반적으로 Ctrl+C 등으로 인한 종료)
        print(f"--- Flask app server on port {flask_port_to_use} has shut down. ---", flush=True)
    except OSError as e:
        # 포트 사용 중이거나 권한 문제 등으로 바인딩 실패 시 OSError 발생
        print(f"--- CRITICAL FLASK ERROR: Failed to bind to 127.0.0.1:{flask_port_to_use}. Error: {e} ---", flush=True)
        print(f"--- This might be due to the port already being in use or permissions issues. ---", flush=True)
        # 이 오류를 main.js가 감지할 수 있도록 stderr로 출력되는 것이 중요
    except Exception as e:
        print(f"--- CRITICAL ERROR in web_app.py __main__ trying to run Flask: {e} ---", flush=True)
        traceback.print_exc() # 전체 트레이스백 출력
    finally:
        sys.stdout.flush() # 프로그램 종료 전 모든 버퍼 플러시

