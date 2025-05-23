# web_app.py (주요 개선사항)
from flask import Flask, render_template, request, jsonify, send_file, Response
from flask_cors import CORS
import os
import json
import threading
import queue
import uuid
from datetime import datetime, timedelta
import tempfile
from werkzeug.utils import secure_filename
import time
import traceback
from functools import wraps

from apscheduler.schedulers.background import BackgroundScheduler

# 기존 모듈 import
from pptx import Presentation
from ollama_service import OllamaService
from translator import OllamaTranslator
from pptx_handler import PptxHandler
from chart_xml_handler import ChartXmlHandler
from ocr_handler import OcrHandlerFactory
import config
import utils

app = Flask(__name__)
CORS(app)

# 전역 변수 및 설정
UPLOAD_FOLDER = os.path.join(tempfile.gettempdir(), 'ppt_translator')
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
ALLOWED_EXTENSIONS = {'pptx'}
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['MAX_CONTENT_LENGTH'] = 100 * 1024 * 1024  # 100MB

# 작업 관리를 위한 스레드 안전 딕셔너리
tasks_lock = threading.Lock()
tasks = {}

def load_language_resources():
    """다국어 리소스 로드 (에러 처리 강화)"""
    languages = {}
    for lang_code in config.UI_SUPPORTED_LANGUAGES.keys():
        lang_file = os.path.join(config.LANGUAGES_DIR, f"{lang_code}.json")
        if os.path.exists(lang_file):
            try:
                with open(lang_file, 'r', encoding='utf-8') as f:
                    languages[lang_code] = json.load(f)
            except Exception as e:
                print(f"Error loading language file {lang_file}: {e}")
                languages[lang_code] = {}
    return languages

language_resources = load_language_resources()

# 전역 서비스 인스턴스
ollama_service = OllamaService()
translator = OllamaTranslator()
pptx_handler = PptxHandler()
chart_processor = ChartXmlHandler(translator, ollama_service)
ocr_handler_factory = OcrHandlerFactory()

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def error_handler(func):
    """API 에러 처리 데코레이터"""
    @wraps(func)
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except Exception as e:
            logger.error(f"Error in {func.__name__}: {str(e)}", exc_info=True)
            return jsonify({'error': str(e)}), 500
    return wrapper

class TaskProgress:
    def __init__(self, task_id, ui_language='ko'):
        self.task_id = task_id
        self.progress = 0
        self.status = language_resources.get(ui_language, {}).get("status_preparing", "준비 중")
        self.current_task = ""
        self.queue = queue.Queue()
        self.completed = False
        self.output_file = None
        self.error = None
        self.total_estimated_work = 0
        self.current_completed_work = 0
        self.current_ui_language = ui_language
        self.creation_time = time.time()
        self.completion_time = None
        self.original_filepath = None
        self.log_filepath = None
        self.stop_event = threading.Event()
        self.lock = threading.Lock()  # 스레드 안전성을 위한 락

    def update_progress(self, location, task_type, weighted_work, text_snippet):
        """진행률 업데이트 (스레드 안전)"""
        with self.lock:
            if self.stop_event.is_set() and "stopping" not in self.status.lower():
                self.status = language_resources.get(self.current_ui_language, {}).get("status_stopping", "중지 중...")
            else:
                self.status = f"{location} - {task_type}"
            
            self.current_task = text_snippet[:50] if text_snippet else ""
            self.current_completed_work += weighted_work
            
            if self.total_estimated_work > 0:
                self.progress = min(int((self.current_completed_work / self.total_estimated_work) * 100), 99)
            
            update_data = {
                'progress': self.progress,
                'status': self.status,
                'current_task': self.current_task,
                'completed': self.completed,
                'error': self.error
            }
            
            try:
                self.queue.put_nowait(update_data)
            except queue.Full:
                # 큐가 가득 찬 경우 가장 오래된 항목 제거
                try:
                    self.queue.get_nowait()
                    self.queue.put_nowait(update_data)
                except queue.Empty:
                    pass

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/check_ollama')
@error_handler
def check_ollama():
    """Ollama 상태 확인"""
    installed = ollama_service.is_installed()
    running, port = ollama_service.is_running()
    models = []

    if running:
        models = ollama_service.get_text_models()

    return jsonify({
        'installed': installed,
        'running': running,
        'port': port,
        'models': models
    })

@app.route('/api/start_ollama', methods=['POST'])
@error_handler
def start_ollama():
    """Ollama 시작"""
    success = ollama_service.start_ollama()
    return jsonify({'success': success})

@app.route('/api/translate', methods=['POST'])
@error_handler
def start_translation():
    """번역 시작"""
    data = request.json
    required_fields = ['filepath', 'src_lang', 'tgt_lang', 'model']
    
    # 필수 필드 검증
    missing_fields = [field for field in required_fields if not data.get(field)]
    if missing_fields:
        ui_lang = data.get('ui_language', config.DEFAULT_UI_LANGUAGE)
        error_msg = language_resources.get(ui_lang, {}).get("error_missing_params", "필수 매개변수가 누락되었습니다")
        return jsonify({'error': f"{error_msg}: {', '.join(missing_fields)}"}), 400

    # 파일 존재 확인
    filepath = data['filepath']
    if not os.path.exists(filepath):
        return jsonify({'error': 'File not found'}), 404

    # 작업 생성
    task_id = str(uuid.uuid4())
    task_progress = TaskProgress(task_id, ui_language=data.get('ui_language', config.DEFAULT_UI_LANGUAGE))
    task_progress.original_filepath = filepath

    with tasks_lock:
        tasks[task_id] = task_progress

    # 백그라운드 작업 시작
    thread = threading.Thread(
        target=translate_worker,
        args=(task_id, filepath, data['src_lang'], data['tgt_lang'], data['model'],
              data.get('image_translation', True), 
              data.get('ocr_temperature', 0.4),
              data.get('ocr_use_gpu', False))
    )
    thread.daemon = True
    thread.start()

    return jsonify({'task_id': task_id})

def translate_worker(task_id, filepath, src_lang, tgt_lang, model,
                    image_translation, ocr_temperature, ocr_use_gpu):
    """번역 작업 워커 (개선된 에러 처리)"""
    task = tasks.get(task_id)
    if not task:
        return

    # 로그 파일 설정
    task_log_filename = f"task_{task_id}.log"
    os.makedirs(config.LOGS_DIR, exist_ok=True)
    task.log_filepath = os.path.join(config.LOGS_DIR, task_log_filename)
    
    # 출력 파일 설정
    original_filename_base = os.path.splitext(os.path.basename(filepath))[0]
    output_filename = f"{original_filename_base}_{tgt_lang}_translated_{task_id}.pptx"
    output_path = os.path.join(app.config['UPLOAD_FOLDER'], output_filename)
    task.output_file = output_path

    try:
        # 파일 정보 분석
        file_info = pptx_handler.get_file_info(filepath)
        if "error" not in file_info:
            task.total_estimated_work = (
                file_info.get("total_text_char_count", 0) * config.WEIGHT_TEXT_CHAR +
                file_info.get("image_elements_count", 0) * config.WEIGHT_IMAGE +
                file_info.get("chart_elements_count", 0) * config.WEIGHT_CHART
            )
            if task.total_estimated_work == 0:
                task.total_estimated_work = 1

        # OCR 핸들러 준비
        ocr_handler = None
        if image_translation:
            src_lang_display = config.TRANSLATION_LANGUAGES_MAP.get(src_lang, src_lang)
            ocr_handler = ocr_handler_factory.get_ocr_handler(
                src_lang_display, ocr_use_gpu, debug_enabled=False
            )

        # 프레젠테이션 로드
        prs = Presentation(filepath)
        tgt_lang_ui_name = config.TRANSLATION_LANGUAGES_MAP.get(tgt_lang, tgt_lang)
        font_code = config.UI_LANG_TO_FONT_CODE_MAP.get(tgt_lang_ui_name, 'en')

        # 1단계: 텍스트 및 OCR 번역
        success_stage1 = pptx_handler.translate_presentation_stage1(
            prs,
            config.TRANSLATION_LANGUAGES_MAP.get(src_lang, src_lang),
            tgt_lang_ui_name,
            translator, ocr_handler,
            model, ollama_service, font_code, task.log_filepath,
            task.update_progress, task.stop_event,
            image_translation, ocr_temperature
        )

        if task.stop_event.is_set():
            task.status = language_resources.get(task.current_ui_language, {}).get("status_stopped_before_charts", "중지됨 (차트 처리 전)")
            prs.save(output_path)
            raise InterruptedError("Translation stopped by user during stage 1.")

        if not success_stage1:
            raise Exception("Stage 1 translation failed")

        # 2단계: 차트 번역
        temp_path = os.path.join(app.config['UPLOAD_FOLDER'], f"temp_{task_id}.pptx")
        prs.save(temp_path)

        final_path = chart_processor.translate_charts_in_pptx(
            temp_path,
            config.TRANSLATION_LANGUAGES_MAP.get(src_lang, src_lang),
            tgt_lang_ui_name,
            model,
            output_path=output_path,
            progress_callback_item_completed=task.update_progress,
            stop_event=task.stop_event,
            task_log_filepath=task.log_filepath
        )

        # 임시 파일 정리
        if os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except Exception:
                pass

        if task.stop_event.is_set():
            task.status = language_resources.get(task.current_ui_language, {}).get("status_stopped_during_charts", "중지됨 (차트 처리 중)")
            raise InterruptedError("Translation stopped by user during chart processing.")

        if final_path and os.path.exists(final_path):
            task.output_file = final_path
            task.progress = 100
            task.status = language_resources.get(task.current_ui_language, {}).get("status_completed", "완료")
        else:
            raise Exception("Chart translation failed or output file not created")

    except InterruptedError:
        # 사용자 중단
        print(f"Task {task_id} was interrupted by user")
    except Exception as e:
        # 오류 처리
        logger.error(f"Error in translate_worker (task {task_id}): {str(e)}", exc_info=True)
        task.error = str(e)
        task.status = language_resources.get(task.current_ui_language, {}).get("status_error", "오류")
    finally:
        # 작업 완료 처리
        task.completed = True
        task.completion_time = time.time()
        
        if task.stop_event.is_set() and "중지" not in task.status:
            task.status = language_resources.get(task.current_ui_language, {}).get("status_stopped", "중지됨")

        # 최종 업데이트
        final_update = {
            'progress': min(task.progress, 100),
            'status': task.status,
            'completed': task.completed,
            'error': task.error,
            'current_task': task.current_task
        }
        
        if task.output_file and os.path.exists(task.output_file) and (not task.error or task.stop_event.is_set()):
            final_update['download_url'] = f'/api/download/{task_id}'

        task.queue.put(final_update)

        # 이력 저장
        save_translation_history(
            os.path.basename(filepath),
            os.path.basename(task.output_file) if task.output_file else "",
            src_lang, tgt_lang, model,
            task.status, task_id
        )

# 스케줄러 설정
scheduler = BackgroundScheduler(daemon=True)

def cleanup_old_files_and_tasks():
    """오래된 파일 및 작업 정리"""
    with app.app_context():
        try:
            current_time = time.time()
            
            # 파일 정리
            retention_days = getattr(config, 'FILE_RETENTION_DAYS', 7)
            cutoff_time_files = current_time - (retention_days * 24 * 60 * 60)
            
            if os.path.exists(app.config['UPLOAD_FOLDER']):
                for filename in os.listdir(app.config['UPLOAD_FOLDER']):
                    filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
                    if os.path.isfile(filepath) and os.path.getmtime(filepath) < cutoff_time_files:
                        try:
                            os.remove(filepath)
                            print(f"Removed old file: {filepath}")
                        except Exception as e:
                            print(f"Error removing file {filepath}: {e}")

            # 작업 정리
            retention_hours = getattr(config, 'TASK_RETENTION_HOURS', 24)
            cutoff_time_tasks = current_time - (retention_hours * 60 * 60)
            
            with tasks_lock:
                tasks_to_delete = []
                for task_id, task in tasks.items():
                    if task.completed and task.completion_time and task.completion_time < cutoff_time_tasks:
                        tasks_to_delete.append(task_id)
                
                for task_id in tasks_to_delete:
                    del tasks[task_id]
                    print(f"Removed old task: {task_id}")
                    
        except Exception as e:
            print(f"Error in cleanup: {e}")

@app.route('/api/progress/<task_id>')
def get_progress(task_id):
    """Server-Sent Events로 진행률 전송"""
    task = tasks.get(task_id)
    if not task:
        return jsonify({'error': '작업을 찾을 수 없습니다'}), 404

    def generate():
        while not task.completed:
            try:
                data = task.queue.get(timeout=1)
                yield f"data: {json.dumps(data)}\n\n"
            except queue.Empty:
                # 현재 상태 전송
                current_state = {
                    'progress': task.progress,
                    'status': task.status,
                    'current_task': task.current_task,
                    'completed': task.completed,
                    'error': task.error
                }
                yield f"data: {json.dumps(current_state)}\n\n"

        # 최종 상태
        final_data = {
            'progress': task.progress,
            'status': task.status,
            'completed': task.completed,
            'error': task.error
        }
        if task.output_file and os.path.exists(task.output_file):
            final_data['download_url'] = f'/api/download/{task_id}'

        yield f"data: {json.dumps(final_data)}\n\n"

    return Response(generate(), mimetype='text/event-stream')

@app.route('/api/download/<task_id>')
def download_file(task_id):
    """번역된 파일 다운로드"""
    task = tasks.get(task_id)
    if not task or not task.output_file or not os.path.exists(task.output_file):
        error_message = language_resources.get(
            task.current_ui_language if task else config.DEFAULT_UI_LANGUAGE, {}
        ).get("error_file_not_found_for_download", "다운로드할 파일을 찾을 수 없습니다")
        return jsonify({'error': error_message}), 404

    return send_file(
        task.output_file,
        as_attachment=True,
        download_name=os.path.basename(task.output_file)
    )

@app.route('/api/stop_translation/<task_id>', methods=['POST'])
@error_handler
def stop_translation_route(task_id):
    """번역 작업 중지"""
    task = tasks.get(task_id)
    if not task:
        return jsonify({'error': 'Task not found'}), 404
        
    if task.completed:
        return jsonify({'error': 'Task already completed'}), 400
        
    task.stop_event.set()
    return jsonify({'message': f'Stop signal sent to task {task_id}'}), 200

@app.route('/api/history')
@error_handler
def get_history():
    """번역 이력 조회"""
    history_file = os.path.join(config.HISTORY_DIR, "translation_history.json")
    if os.path.exists(history_file):
        try:
            with open(history_file, 'r', encoding='utf-8') as f:
                return jsonify(json.load(f))
        except Exception:
            return jsonify([])
    return jsonify([])

@app.route('/api/delete_history', methods=['POST'])
@error_handler
def delete_history_route():
    """번역 이력 삭제"""
    history_file = os.path.join(config.HISTORY_DIR, "translation_history.json")
    if os.path.exists(history_file):
        os.remove(history_file)
    return jsonify({'message': 'Translation history deleted.'}), 200

@app.route('/api/open_log_folder', methods=['GET'])
@error_handler
def open_log_folder_route():
    """로그 폴더 열기"""
    os.makedirs(config.LOGS_DIR, exist_ok=True)
    utils.open_folder(config.LOGS_DIR)
    return jsonify({'message': f'Log folder open command issued.'}), 200

@app.route('/api/file_info', methods=['POST'])
@error_handler
def get_file_info():
    """업로드된 파일 정보 분석"""
    if 'file' not in request.files:
        return jsonify({'error': 'No file provided'}), 400

    file = request.files['file']
    if file.filename == '':
        return jsonify({'error': 'No file selected'}), 400

    if file and allowed_file(file.filename):
        filename = secure_filename(file.filename)
        # 파일명에 타임스탬프 추가하여 중복 방지
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        filename = f"{timestamp}_{filename}"
        filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
        file.save(filepath)

        try:
            info = pptx_handler.get_file_info(filepath)
            return jsonify({
                'filename': filename,
                'filepath': filepath,
                'info': info
            })
        except Exception as e:
            # 파일 분석 실패 시 업로드된 파일 삭제
            if os.path.exists(filepath):
                os.remove(filepath)
            raise

    return jsonify({'error': 'Invalid file type'}), 400

@app.route('/api/languages')
def get_languages():
    """지원 언어 목록"""
    return jsonify({
        'languages': list(config.TRANSLATION_LANGUAGES_MAP.keys()),
        'language_names': config.TRANSLATION_LANGUAGES_MAP
    })

@app.route('/api/ui_languages')
def get_ui_languages():
    """UI 언어 목록 및 현재 언어 리소스"""
    current_lang = request.args.get('lang', config.DEFAULT_UI_LANGUAGE)
    if current_lang not in language_resources:
        current_lang = config.DEFAULT_UI_LANGUAGE

    return jsonify({
        'supported_languages': config.UI_SUPPORTED_LANGUAGES,
        'current_language': current_lang,
        'resources': language_resources.get(current_lang, {})
    })

history_lock = threading.Lock()

def save_translation_history(original_filename, translated_filename, src_lang, tgt_lang, model, status, task_id):
    """번역 이력 저장"""
    history_file = os.path.join(config.HISTORY_DIR, "translation_history.json")
    os.makedirs(config.HISTORY_DIR, exist_ok=True)
    
    new_entry = {
        "id": task_id,
        "name": original_filename,
        "translated_name": translated_filename,
        "src": src_lang,
        "tgt": tgt_lang,
        "model": model,
        "status": status,
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }

    with history_lock:
        history = []
        if os.path.exists(history_file):
            try:
                with open(history_file, 'r', encoding='utf-8') as f:
                    history = json.load(f)
            except Exception:
                history = []

        history.insert(0, new_entry)
        history = history[:config.MAX_HISTORY_ITEMS]

        try:
            with open(history_file, 'w', encoding='utf-8') as f:
                json.dump(history, f, ensure_ascii=False, indent=4)
        except Exception as e:
            print(f"Error saving translation history: {e}")

# 로깅 설정
import logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

if __name__ == '__main__':
    # 필요한 디렉토리 생성
    os.makedirs(UPLOAD_FOLDER, exist_ok=True)
    os.makedirs(config.LOGS_DIR, exist_ok=True)
    os.makedirs(config.HISTORY_DIR, exist_ok=True)

    # 스케줄러 시작
    cleanup_hour = getattr(config, 'CLEANUP_HOUR', 3)
    scheduler.add_job(cleanup_old_files_and_tasks, 'cron', hour=cleanup_hour, minute=0)
    scheduler.start()
    print(f"파일 및 작업 자동 정리 스케줄러 시작됨 (매일 {cleanup_hour}:00 실행).")

    # Flask 앱 실행
    app.run(debug=False, host='0.0.0.0', port=5001)