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

from apscheduler.schedulers.background import BackgroundScheduler

# 모듈 import
import config # 설정 파일 import
from ollama_service import OllamaService
from translator import OllamaTranslator
from pptx_handler import PptxHandler
from chart_xml_handler import ChartXmlHandler
from ocr_handler import OcrHandlerFactory
import utils # 유틸리티 모듈 import (open_folder 등)

# --- 로거 설정 ---
# logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__) # Flask 기본 로거 사용 또는 위 주석 해제 후 커스텀

app = Flask(__name__)
CORS(app)

# --- 설정 로드 및 UPLOAD_FOLDER 설정 ---
app.config.from_object(config) # config.py의 설정들을 Flask app config로 로드
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
# ChartXmlHandler 초기화 시 ollama_service 인스턴스 전달
chart_processor = ChartXmlHandler(translator, ollama_service)
ocr_handler_factory = OcrHandlerFactory()


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

class TaskProgress: # 기존 TaskProgress 클래스 유지 또는 개선
    def __init__(self, task_id, ui_language='ko'):
        self.task_id = task_id
        self.progress = 0
        # status 초기화 시 language_resources 사용
        self.status = language_resources.get(ui_language, {}).get("status_preparing", "Preparing...")
        self.current_task = ""
        self.queue = queue.Queue(maxsize=100) # SSE 메시지 큐
        self.completed = False
        self.output_file = None
        self.error = None
        self.total_estimated_work = 0
        self.current_completed_work = 0
        self.current_ui_language = ui_language # UI 언어 저장
        self.creation_time = time.time()
        self.completion_time = None
        self.original_filepath = None
        self.log_filepath = None # 작업별 로그 파일 경로
        self.stop_event = threading.Event()
        self.lock = threading.Lock()

    def update_progress(self, location_key, task_type_key, weighted_work, text_snippet):
        with self.lock:
            if self.stop_event.is_set() and "stopping" not in self.status.lower() and \
               language_resources.get(self.current_ui_language, {}).get("status_stopping", "Stopping...") not in self.status: # 다국어 비교
                self.status = language_resources.get(self.current_ui_language, {}).get("status_stopping", "Stopping...")
            else:
                loc_text = language_resources.get(self.current_ui_language, {}).get(location_key, location_key)
                task_text = language_resources.get(self.current_ui_language, {}).get(task_type_key, task_type_key)
                self.status = f"{loc_text} - {task_text}"
            
            self.current_task = text_snippet[:50] if text_snippet else "" # 작업 중인 텍스트 조각
            self.current_completed_work += weighted_work
            
            if self.total_estimated_work > 0:
                self.progress = min(int((self.current_completed_work / self.total_estimated_work) * 100), 99) # 최대 99%로 제한, 완료 시 100%
            
            update_data = {
                'progress': self.progress, 'status': self.status, 'current_task': self.current_task,
                'completed': self.completed, 'error': self.error
            }
            try:
                self.queue.put_nowait(update_data)
            except queue.Full:
                try:
                    self.queue.get_nowait() # 오래된 데이터 제거
                    self.queue.put_nowait(update_data)
                except queue.Empty: pass


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

    with tasks_lock:
        tasks[task_id] = task

    thread = threading.Thread(target=translate_worker, args=(
        task_id, filepath, data['src_lang'], data['tgt_lang'], data['model'],
        data.get('image_translation', True), 
        data.get('ocr_temperature', config.DEFAULT_ADVANCED_SETTINGS['ocr_temperature']), # config에서 기본값 사용
        data.get('ocr_use_gpu', config.DEFAULT_ADVANCED_SETTINGS['ocr_use_gpu'])
    ))
    thread.daemon = True
    thread.start()
    return jsonify({'task_id': task_id})

def translate_worker(task_id, filepath, src_lang, tgt_lang, model,
                    image_translation, ocr_temperature, ocr_use_gpu):
    task = tasks.get(task_id)
    if not task: 
        logger.error(f"Translate worker: Task {task_id} not found.")
        return

    # 작업별 로그 파일 경로 설정
    task_log_filename = f"task_{task_id}_{datetime.now().strftime('%Y%m%d%H%M%S')}.log"
    # config.LOGS_DIR 사용 (config.py에 정의되어 있어야 함)
    if not hasattr(config, 'LOGS_DIR') or not config.LOGS_DIR:
        # LOGS_DIR가 config에 없거나 비어있을 경우 임시 폴더 사용
        task_log_dir = os.path.join(app.config['UPLOAD_FOLDER'], 'task_logs')
        logger.warning(f"config.LOGS_DIR not defined, using temporary log directory: {task_log_dir}")
    else:
        task_log_dir = config.LOGS_DIR
    os.makedirs(task_log_dir, exist_ok=True)
    task.log_filepath = os.path.join(task_log_dir, task_log_filename)
    
    original_filename_base = os.path.splitext(os.path.basename(filepath))[0]
    # 출력 파일명에 task_id 대신 좀 더 의미있는 정보나 타임스탬프 사용 고려 가능
    output_filename = f"{original_filename_base}_{tgt_lang}_translated_{task_id[:8]}.pptx"
    output_path = os.path.join(app.config['UPLOAD_FOLDER'], output_filename) # app.config 사용
    task.output_file = output_path

    try:
        file_info = pptx_handler.get_file_info(filepath)
        if "error" not in file_info:
            task.total_estimated_work = (
                file_info.get("total_text_char_count", 0) * config.WEIGHT_TEXT_CHAR +
                file_info.get("image_elements_count", 0) * config.WEIGHT_IMAGE +
                file_info.get("chart_elements_count", 0) * config.WEIGHT_CHART)
            if task.total_estimated_work == 0: task.total_estimated_work = 1 # 0으로 나누기 방지
        else:
            task.total_estimated_work = 1 # 정보 분석 실패 시 기본값
            logger.warning(f"Could not get file_info for {filepath}, using default estimated work.")


        ocr_handler = None
        if image_translation:
            # UI 언어 코드(예: "ko")를 OCR 핸들러 팩토리가 이해하는 코드 (예: "Korean")로 변환
            src_lang_name_for_ocr = config.TRANSLATION_LANGUAGES_MAP.get(src_lang, src_lang)
            ocr_handler = ocr_handler_factory.get_ocr_handler(src_lang_name_for_ocr, ocr_use_gpu, debug_enabled=app.debug)
            if not ocr_handler:
                logger.warning(f"OCR Handler for {src_lang_name_for_ocr} (GPU: {ocr_use_gpu}) could not be initialized. Image translation will be skipped.")
                # 사용자에게 알릴 방법 고려 (예: task.status 업데이트)

        prs = Presentation(filepath)
        # UI 언어 코드(예: "ko")를 번역기가 이해하는 언어 이름 (예: "Korean")으로 변환
        src_lang_name = config.TRANSLATION_LANGUAGES_MAP.get(src_lang, src_lang)
        tgt_lang_name = config.TRANSLATION_LANGUAGES_MAP.get(tgt_lang, tgt_lang)
        # 폰트 코드 결정 시 대상 언어의 UI 표시 이름 기준 (예: "한국어" -> "korean")
        font_code_for_render = config.UI_LANG_TO_FONT_CODE_MAP.get(
            # TRANSLATION_LANGUAGES_MAP의 value (영어 이름)를 UI_SUPPORTED_LANGUAGES의 value (UI 표시 이름)로 매칭
            next((ui_name for code, ui_name in config.UI_SUPPORTED_LANGUAGES.items() if code == tgt_lang), tgt_lang),
            'en' # 기본값 영어
        )


        success_stage1 = pptx_handler.translate_presentation_stage1(
            prs, src_lang_name, tgt_lang_name,
            translator, ocr_handler, model, ollama_service, font_code_for_render, task.log_filepath,
            task.update_progress, task.stop_event, image_translation, ocr_temperature
        )

        if task.stop_event.is_set():
            task.status = language_resources.get(task.current_ui_language, {}).get("status_stopped_before_charts", "Stopped (before chart processing)")
            # 중지 시에도 현재까지 작업된 파일 저장 시도
            try: prs.save(output_path)
            except Exception as e_save: logger.error(f"Error saving partially translated file on stop: {e_save}")
            raise InterruptedError("Translation stopped by user during stage 1.")

        if not success_stage1:
            # pptx_handler.translate_presentation_stage1 내부에서 오류 로깅 및 처리가 있을 것이므로 여기서는 일반적인 실패로 간주
            raise Exception(language_resources.get(task.current_ui_language, {}).get("error_stage1_translation_failed", "Text/Image translation stage failed"))

        # 2단계: 차트 번역
        # 임시 파일 저장 경로 (app.config['UPLOAD_FOLDER'] 사용)
        temp_chart_input_path = os.path.join(app.config['UPLOAD_FOLDER'], f"temp_chart_input_{task_id}.pptx")
        prs.save(temp_chart_input_path) # 차트 처리 전 중간 저장

        final_path = chart_processor.translate_charts_in_pptx(
            temp_chart_input_path, src_lang_name, tgt_lang_name, model,
            output_path=output_path, # 최종 출력 경로 지정
            progress_callback_item_completed=task.update_progress,
            stop_event=task.stop_event,
            task_log_filepath=task.log_filepath
        )
        
        if os.path.exists(temp_chart_input_path): # 임시 파일 삭제
            try: os.remove(temp_chart_input_path)
            except Exception as e_remove: logger.warning(f"Could not remove temporary chart input file {temp_chart_input_path}: {e_remove}")

        if task.stop_event.is_set():
            task.status = language_resources.get(task.current_ui_language, {}).get("status_stopped_during_charts", "Stopped (during chart processing)")
            # 차트 처리 중 중단 시 output_path에 저장된 파일이 최종 결과물이 될 수 있음 (일부 차트만 번역)
            raise InterruptedError("Translation stopped by user during chart processing.")

        if final_path and os.path.exists(final_path):
            task.output_file = final_path # 최종 파일 경로 업데이트
            task.progress = 100
            task.status = language_resources.get(task.current_ui_language, {}).get("status_completed", "Completed")
        else:
            raise Exception(language_resources.get(task.current_ui_language, {}).get("error_chart_translation_failed", "Chart translation failed or output file not created"))

    except InterruptedError:
        logger.info(f"Task {task_id} was interrupted by user. Status: {task.status}")
    except Exception as e:
        logger.error(f"Error in translate_worker (task {task_id}): {str(e)}", exc_info=True)
        task.error = str(e) # 오류 메시지 저장
        task.status = language_resources.get(task.current_ui_language, {}).get("status_error", "Error")
    finally:
        task.completed = True
        task.completion_time = time.time()
        if task.stop_event.is_set() and "중지됨" not in task.status and "Stopped" not in task.status.capitalize(): # 다국어 고려
             task.status = language_resources.get(task.current_ui_language, {}).get("status_stopped", "Stopped")

        final_update_data = {'progress': min(task.progress, 100), 'status': task.status, 'completed': task.completed, 'error': task.error, 'current_task': task.current_task}
        # 오류가 발생했더라도, 부분적으로라도 완료된 파일이 있고, 사용자가 중지한 경우 다운로드 URL 제공 가능
        if task.output_file and os.path.exists(task.output_file) and (not task.error or task.stop_event.is_set()):
            final_update_data['download_url'] = f'/api/download/{task_id}'
        task.queue.put(final_update_data)

        save_translation_history(
            os.path.basename(filepath),
            os.path.basename(task.output_file) if task.output_file else "",
            src_lang, tgt_lang, model, task.status, task_id
        )

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
def get_progress_route(task_id): # 함수 이름 변경
    task = tasks.get(task_id)
    if not task:
        # 클라이언트 UI 언어에 맞춰 에러 메시지 반환 시도
        ui_lang_code = request.args.get('lang', config.DEFAULT_UI_LANGUAGE)
        error_msg = language_resources.get(ui_lang_code, {}).get("error_task_not_found", "Task not found.")
        return jsonify({'error': error_msg}), 404

    def generate_sse_for_task():
        # 초기 현재 상태 전송
        initial_data = {'progress': task.progress, 'status': task.status, 'current_task': task.current_task, 'completed': task.completed, 'error': task.error}
        if task.output_file and os.path.exists(task.output_file) and not task.error : initial_data['download_url'] = f'/api/download/{task_id}'
        yield f"data: {json.dumps(initial_data)}\n\n"

        while not task.completed:
            try:
                data = task.queue.get(timeout=1) # 큐에서 데이터 가져오기 (1초 타임아웃)
                if task.output_file and os.path.exists(task.output_file) and not data.get('error'): # 성공적으로 파일 생성 시 다운로드 URL 추가
                    data['download_url'] = f'/api/download/{task_id}'
                yield f"data: {json.dumps(data)}\n\n"
            except queue.Empty:
                # 타임아웃 시 현재 상태 다시 보내거나 heartbeat 전송
                current_heartbeat_data = {'progress': task.progress, 'status': task.status, 'current_task': task.current_task, 'completed': task.completed, 'error': task.error}
                yield f"data: {json.dumps(current_heartbeat_data)}\n\n" # 현재 상태를 heartbeat처럼 사용
            except Exception as e:
                logger.error(f"Error during SSE generation for task {task_id}: {e}", exc_info=True)
                error_stream_data = {'progress': task.progress, 'status': "SSE Error", 'completed': True, 'error': str(e)}
                yield f"data: {json.dumps(error_stream_data)}\n\n"
                break
        
        # 작업 완료 후 최종 상태 한 번 더 전송
        final_data = {'progress': min(task.progress,100), 'status': task.status, 'completed': task.completed, 'error': task.error, 'current_task': task.current_task}
        if task.output_file and os.path.exists(task.output_file) and (not task.error or task.stop_event.is_set()):
            final_data['download_url'] = f'/api/download/{task_id}'
        yield f"data: {json.dumps(final_data)}\n\n"
        logger.info(f"SSE stream for task {task_id} ended.")

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
    ui_lang_code = request.args.get('lang', config.DEFAULT_UI_LANGUAGE) # 또는 request.form.get('ui_language')
    if 'file' not in request.files:
        error_msg = language_resources.get(ui_lang_code, {}).get("error_no_file_provided", "No file provided.")
        return jsonify({'error': error_msg}), 400
    file = request.files['file']
    if file.filename == '':
        error_msg = language_resources.get(ui_lang_code, {}).get("error_no_file_selected", "No file selected.")
        return jsonify({'error': error_msg}), 400

    if file and allowed_file(file.filename):
        original_filename = secure_filename(file.filename)
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S_%f') # 밀리초까지 추가하여 유니크 확보
        # 저장될 파일명 (한글 등 유니코드 문제 방지 위해 secure_filename 한번 더 또는 UUID 사용)
        # 여기서는 타임스탬프와 원본 파일명을 조합하되, 최종 secure_filename 처리
        server_filename = secure_filename(f"{timestamp}_{original_filename}")
        filepath = os.path.join(app.config['UPLOAD_FOLDER'], server_filename)
        
        try:
            file.save(filepath)
            info = pptx_handler.get_file_info(filepath)
            if "error" in info: # pptx_handler에서 오류 반환 시
                 raise Exception(info["error"])

            return jsonify({'filename': original_filename, 'filepath': filepath, 'info': info })
        except Exception as e:
            if os.path.exists(filepath): # 오류 발생 시 저장된 파일 삭제
                try: os.remove(filepath)
                except Exception as e_remove: logger.error(f"Error removing uploaded file on failure: {e_remove}")
            logger.error(f"Error processing file info for {original_filename}: {e}", exc_info=True)
            error_msg = language_resources.get(ui_lang_code, {}).get("error_file_processing_failed", "Failed to process file.")
            return jsonify({'error': error_msg, 'details': str(e)}), 500
    
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
    # Flask 앱 실행 전 필요한 디렉토리 생성 확인
    os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
    if hasattr(config, 'LOGS_DIR') and config.LOGS_DIR: os.makedirs(config.LOGS_DIR, exist_ok=True)
    else: os.makedirs(os.path.join(app.config['UPLOAD_FOLDER'], 'task_logs'), exist_ok=True) # 임시 로그 폴더
    if hasattr(config, 'HISTORY_DIR') and config.HISTORY_DIR: os.makedirs(config.HISTORY_DIR, exist_ok=True)


    # 스케줄러 설정 및 시작
    cleanup_hour = app.config.get('CLEANUP_HOUR', 3)
    if not scheduler.running: # 스케줄러가 이미 실행 중이지 않을 때만 추가 및 시작
        scheduler.add_job(cleanup_old_files_and_tasks, 'cron', hour=cleanup_hour, minute=0, id="cleanup_job", replace_existing=True)
        try:
            scheduler.start()
            logger.info(f"File and task cleanup scheduler started (runs daily at {cleanup_hour:02}:00).")
        except Exception as e:
            logger.error(f"Failed to start scheduler: {e}")


    # Flask 앱 실행 (디버그 모드는 개발 시에만 True)
    app.run(debug=app.config.get('DEBUG', False), host=app.config.get('HOST', '0.0.0.0'), port=app.config.get('PORT', 5001))