# web_app.py
from flask import Flask, render_template, request, jsonify, send_file, Response
from flask_cors import CORS
import os
import json
import threading
import queue
import uuid
from datetime import datetime, timedelta # timedelta 추가
import tempfile
from werkzeug.utils import secure_filename
import time # time 모듈 import

# APScheduler 추가
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

# 다국어 리소스 로드
def load_language_resources():
    languages = {}
    for lang_code in config.UI_SUPPORTED_LANGUAGES.keys():
        lang_file = os.path.join(config.LANGUAGES_DIR, f"{lang_code}.json") #
        if os.path.exists(lang_file):
            try:
                with open(lang_file, 'r', encoding='utf-8') as f:
                    languages[lang_code] = json.load(f)
            except Exception as e:
                print(f"Error loading language file {lang_file}: {e}") #
                languages[lang_code] = {}
    return languages

language_resources = load_language_resources()

# 파일 업로드 설정
UPLOAD_FOLDER = tempfile.mkdtemp() #
ALLOWED_EXTENSIONS = {'pptx'} #
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER #
app.config['MAX_CONTENT_LENGTH'] = 100 * 1024 * 1024  # 100MB

# 전역 서비스 인스턴스
ollama_service = OllamaService()
translator = OllamaTranslator()
pptx_handler = PptxHandler()
chart_processor = ChartXmlHandler(translator, ollama_service)
ocr_handler_factory = OcrHandlerFactory()

# 진행 중인 작업 저장
tasks = {} #

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS #

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
        self.stop_event = threading.Event() # stop_event 초기화


@app.route('/')
def index():
    return render_template('index.html') #

@app.route('/api/check_ollama')
def check_ollama():
    """Ollama 상태 확인"""
    installed = ollama_service.is_installed() #
    running, port = ollama_service.is_running() #
    models = [] #

    if running:
        models = ollama_service.get_text_models() #

    return jsonify({
        'installed': installed, #
        'running': running, #
        'port': port, #
        'models': models #
    })

@app.route('/api/start_ollama', methods=['POST'])
def start_ollama():
    """Ollama 시작"""
    success = ollama_service.start_ollama() #
    return jsonify({'success': success}) #

@app.route('/api/languages')
def get_languages():
    """지원 언어 목록"""
    return jsonify({
        'languages': list(config.TRANSLATION_LANGUAGES_MAP.keys()), #
        'language_names': config.TRANSLATION_LANGUAGES_MAP #
    })

@app.route('/api/ui_languages')
def get_ui_languages():
    """UI 언어 목록 및 현재 언어 리소스"""
    current_lang = request.args.get('lang', config.DEFAULT_UI_LANGUAGE) # # 기본값 수정
    if current_lang not in language_resources:
        current_lang = config.DEFAULT_UI_LANGUAGE #

    return jsonify({
        'supported_languages': config.UI_SUPPORTED_LANGUAGES, #
        'current_language': current_lang, #
        'resources': language_resources.get(current_lang, {}) #
    })

@app.route('/api/file_info', methods=['POST'])
def get_file_info():
    """업로드된 파일 정보 분석"""
    if 'file' not in request.files: #
        return jsonify({'error': language_resources.get(request.args.get('lang', config.DEFAULT_UI_LANGUAGE), {}).get("error_no_file", "파일이 없습니다")}), 400 # 다국어

    file = request.files['file'] #
    if file.filename == '': #
        return jsonify({'error': language_resources.get(request.args.get('lang', config.DEFAULT_UI_LANGUAGE), {}).get("error_no_file_selected", "파일이 선택되지 않았습니다")}), 400 # 다국어

    if file and allowed_file(file.filename): #
        filename = secure_filename(file.filename) #
        filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename) #
        file.save(filepath) #

        try:
            info = pptx_handler.get_file_info(filepath) #
            return jsonify({
                'filename': filename, #
                'filepath': filepath, #
                'info': info #
            })
        except Exception as e:
            return jsonify({'error': str(e)}), 500 #

    return jsonify({'error': language_resources.get(request.args.get('lang', config.DEFAULT_UI_LANGUAGE), {}).get("error_disallowed_file_type", "허용되지 않은 파일 형식")}), 400 # 다국어

@app.route('/api/translate', methods=['POST'])
def start_translation():
    """번역 시작"""
    data = request.json #
    filepath = data.get('filepath') #
    src_lang = data.get('src_lang') #
    tgt_lang = data.get('tgt_lang') #
    model = data.get('model') #
    image_translation = data.get('image_translation', True) #
    ocr_temperature = data.get('ocr_temperature', 0.4) #
    ocr_use_gpu = data.get('ocr_use_gpu', False) #
    ui_language_from_request = data.get('ui_language', config.DEFAULT_UI_LANGUAGE) #

    if not all([filepath, src_lang, tgt_lang, model]): #
        return jsonify({'error': language_resources.get(ui_language_from_request, {}).get("error_missing_params", "필수 매개변수가 누락되었습니다")}), 400 # 다국어

    task_id = str(uuid.uuid4()) #
    task_progress = TaskProgress(task_id, ui_language=ui_language_from_request) #
    task_progress.original_filepath = filepath # 원본 파일 경로 저장

    tasks[task_id] = task_progress #

    thread = threading.Thread( #
        target=translate_worker,
        args=(task_id, filepath, src_lang, tgt_lang, model,
              image_translation, ocr_temperature, ocr_use_gpu)
    )
    thread.start() #

    return jsonify({'task_id': task_id}) #

history_lock = threading.Lock() #

def save_translation_history(original_filename, translated_filename, src_lang, tgt_lang, model, status, task_id): #
    history_file = os.path.join(config.HISTORY_DIR, "translation_history.json") #
    new_entry = { #
        "id": task_id, #
        "name": original_filename, #
        "translated_name": translated_filename, #
        "src": src_lang, #
        "tgt": tgt_lang, #
        "model": model, #
        "status": status, #
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), #
    }

    with history_lock: #
        history = [] #
        if os.path.exists(history_file): #
            try:
                with open(history_file, 'r', encoding='utf-8') as f: #
                    history = json.load(f) #
            except json.JSONDecodeError: #
                history = [] #

        history.insert(0, new_entry) #
        history = history[:config.MAX_HISTORY_ITEMS] #

        try:
            with open(history_file, 'w', encoding='utf-8') as f: #
                json.dump(history, f, ensure_ascii=False, indent=4) #
        except Exception as e:
            print(f"Error saving translation history: {e}") #

@app.route('/api/open_log_folder', methods=['GET'])
def open_log_folder_route():
    """로그 폴더 열기"""
    try:
        folder_to_open = config.LOGS_DIR # 수정: config.LOGS_DIR 사용
        if not os.path.exists(folder_to_open):
             os.makedirs(folder_to_open, exist_ok=True)
        print(f"Opening log folder: {folder_to_open}") # 디버깅 로그
        utils.open_folder(folder_to_open)
        return jsonify({'message': f'Log folder ({folder_to_open}) open command issued.'}), 200
    except Exception as e:
        print(f"Error opening log folder: {e}") # 디버깅 로그
        return jsonify({'error': str(e)}), 500

@app.route('/api/delete_history', methods=['POST'])
def delete_history_route():
    """번역 이력 삭제"""
    try:
        history_file = os.path.join(config.HISTORY_DIR, "translation_history.json")
        if os.path.exists(history_file):
            os.remove(history_file)
        # tasks 딕셔너리 자체를 여기서 건드리지는 않음. 오래된 작업 정리는 별도 스케줄러 담당.
        return jsonify({'message': 'Translation history deleted.'}), 200
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/stop_translation/<task_id>', methods=['POST'])
def stop_translation_route(task_id):
    """번역 작업 중지 요청"""
    task = tasks.get(task_id)
    if task and not task.completed: # 이미 완료된 작업은 중지 불가
        if hasattr(task, 'stop_event') and task.stop_event:
            task.stop_event.set()
            # UI에서 즉각적인 피드백을 위해 상태 변경은 여기서도 가능하나,
            # SSE를 통해 작업자 스레드가 실제 상태를 보내는 것이 더 정확함.
            # task.status = language_resources.get(task.current_ui_language, {}).get("status_stopping", "중지 중...")
            # task.queue.put(...) # 즉각적 UI 업데이트 원하면 여기서도 큐에 넣을 수 있음
            print(f"Stop signal set for task {task_id}")
            return jsonify({'message': f'Stop signal sent to task {task_id}.'}), 200
        else:
            return jsonify({'error': 'Task stop event not available.'}), 500
    elif task and task.completed:
        return jsonify({'error': 'Task already completed.'}), 400
    else:
        return jsonify({'error': 'Task not found.'}), 404


def translate_worker(task_id, filepath, src_lang, tgt_lang, model,
                    image_translation, ocr_temperature, ocr_use_gpu):
    task = tasks.get(task_id)
    if not task: return

    # 로그 파일 경로 수정
    task_log_filename = f"task_{task_id}.log" # 파일 이름에 "task_" 접두사 추가하여 일반 로그와 구분
    # config.LOGS_DIR 사용 보장
    if not os.path.exists(config.LOGS_DIR):
        try:
            os.makedirs(config.LOGS_DIR, exist_ok=True)
        except OSError as e:
            print(f"Error creating LOGS_DIR {config.LOGS_DIR}: {e}")
            # LOGS_DIR 생성 실패 시 fallback으로 UPLOAD_FOLDER 사용 또는 에러 처리
            # 여기서는 간단히 UPLOAD_FOLDER로 fallback
            task_log_filepath = os.path.join(app.config['UPLOAD_FOLDER'], task_log_filename)
            print(f"Fallback: Task log will be saved to UPLOAD_FOLDER: {task_log_filepath}")
    else:
        task_log_filepath = os.path.join(config.LOGS_DIR, task_log_filename)
    
    task.log_filepath = task_log_filepath
    print(f"Task {task_id} log path set to: {task_log_filepath}")
    
    # output_path는 초기에 task_id를 포함하도록 설정
    original_filename_base = os.path.splitext(os.path.basename(filepath))[0]
    output_filename = f"{original_filename_base}_{tgt_lang}_translated_{task_id}.pptx"
    output_path = os.path.join(app.config['UPLOAD_FOLDER'], output_filename)
    task.output_file = output_path # 미리 설정해두면 중지 시에도 이 경로 사용 가능

    try:
        file_initial_info = pptx_handler.get_file_info(filepath) #
        if "error" not in file_initial_info: #
            task.total_estimated_work = ( #
                file_initial_info.get("total_text_char_count", 0) * config.WEIGHT_TEXT_CHAR +
                file_initial_info.get("image_elements_count", 0) * config.WEIGHT_IMAGE +
                file_initial_info.get("chart_elements_count", 0) * config.WEIGHT_CHART
            )
            if task.total_estimated_work == 0: #
                task.total_estimated_work = 1 #
    except Exception as e_info:
        print(f"Error estimating total work for task {task_id}: {e_info}") #
        task.total_estimated_work = 1 #

    try:
        def progress_callback(location, task_type, weighted_work, text_snippet):
            # ... (기존과 동일, task.stop_event.is_set()이면 업데이트 최소화 또는 중단 메시지 표시 가능) ...
            if task.stop_event.is_set() and "stopping" not in task.status.lower() and "중지" not in task.status: # 중복 상태 업데이트 방지
                 task.status = language_resources.get(task.current_ui_language, {}).get("status_stopping", "중지 중...")
            # ...
            if task: # task 객체가 여전히 유효한지 확인
                task.status = f"{location} - {task_type}" if not task.stop_event.is_set() else language_resources.get(task.current_ui_language, {}).get("status_stopping", "중지 중...")
                task.current_task = text_snippet[:50] if text_snippet else ""
                task.current_completed_work += weighted_work
                if task.total_estimated_work > 0:
                    task.progress = int((task.current_completed_work / task.total_estimated_work) * 100)
                task.progress = min(task.progress, 99) # 완료 전까지는 99%

                task.queue.put({
                    'progress': task.progress,
                    'status': task.status,
                    'current_task': task.current_task,
                    'completed': task.completed, # False
                    'error': task.error
                })


        ocr_handler = None
        if image_translation:
            src_lang_display_name = config.TRANSLATION_LANGUAGES_MAP.get(src_lang, src_lang)
            ocr_handler = ocr_handler_factory.get_ocr_handler(
               src_lang_display_name, ocr_use_gpu, debug_enabled=False # 디버그는 필요시 True
            )

        prs = Presentation(filepath)
        tgt_lang_ui_name = config.TRANSLATION_LANGUAGES_MAP.get(tgt_lang, tgt_lang)
        font_code = config.UI_LANG_TO_FONT_CODE_MAP.get(tgt_lang_ui_name, 'en')

        # --- 1단계: 텍스트 및 OCR 번역 ---
        # stop_event는 pptx_handler로 전달됨
        success_stage1 = pptx_handler.translate_presentation_stage1(
            prs,
            config.TRANSLATION_LANGUAGES_MAP.get(src_lang, src_lang),
            tgt_lang_ui_name,
            translator, ocr_handler,
            model, ollama_service, font_code, task_log_filepath,
            progress_callback, task.stop_event, # task.stop_event 전달
            image_translation, ocr_temperature
        )

        if task.stop_event.is_set():
            # 1단계 중 또는 직후 중지 요청됨
            task.status = language_resources.get(task.current_ui_language, {}).get("status_stopped_before_charts", "중지됨 (차트 처리 전)")
            print(f"Task {task_id} stopped during or after stage 1. Saving current presentation.")
            prs.save(output_path) # 현재까지의 prs 객체 저장
            task.output_file = output_path # 이미 위에서 설정됨
            # 진행률은 현재까지 계산된 값 사용
            save_translation_history(
                original_filename=os.path.basename(filepath),
                translated_filename=os.path.basename(output_path),
                src_lang=src_lang, tgt_lang=tgt_lang, model=model,
                status=task.status, task_id=task_id
            )
            # 여기서 finally로 바로 넘어감 (명시적으로 오류를 발생시키거나 return)
            raise InterruptedError("Translation stopped by user during stage 1.")


        if not success_stage1: # 1단계 실패 (중지 아님)
            task.error = task.error or language_resources.get(task.current_ui_language, {}).get("error_stage1_translation_failed", "1단계 번역 실패")
            # 실패 시에도 finally로 이동하여 task.completed=True 처리
            raise Exception(task.error) # 작업자 스레드 내 오류 발생으로 finally로 이동


        # --- 2단계: 차트 번역 ---
        # 1단계가 성공했고, 중지 요청이 없었을 때만 진행
        print(f"Task {task_id} stage 1 completed. Proceeding to chart translation.")
        temp_filename_for_charts = f"{original_filename_base}_temp_charts_{task_id}.pptx"
        temp_path_for_charts = os.path.join(app.config['UPLOAD_FOLDER'], temp_filename_for_charts)
        prs.save(temp_path_for_charts) # 차트 핸들러는 파일 경로를 입력으로 받음

        # chart_processor는 내부적으로 stop_event를 확인하고, 중지 시 output_path에 현재까지 작업물을 저장 시도해야 함.
        # output_path를 직접 전달하여 chart_processor가 해당 경로에 최종본(또는 중지된 버전)을 저장하도록 유도
        final_path_from_charts = chart_processor.translate_charts_in_pptx(
            temp_path_for_charts,
            config.TRANSLATION_LANGUAGES_MAP.get(src_lang, src_lang),
            tgt_lang_ui_name,
            model,
            output_path=output_path, # 최종 저장 경로를 chart_processor에 알려줌
            progress_callback_item_completed=progress_callback, # 이름 수정된 콜백
            stop_event=task.stop_event, # task.stop_event 전달
            task_log_filepath=task_log_filepath
        )

        if os.path.exists(temp_path_for_charts):
            try: os.remove(temp_path_for_charts)
            except Exception as e_remove_temp: print(f"Error removing temp chart file {temp_path_for_charts}: {e_remove_temp}")

        if task.stop_event.is_set():
            task.status = language_resources.get(task.current_ui_language, {}).get("status_stopped_during_charts", "중지됨 (차트 처리 중)")
            print(f"Task {task_id} stopped during chart processing. Output at: {output_path}")
            # chart_processor가 output_path에 저장했기를 기대. task.output_file은 이미 output_path.
            # 진행률은 현재까지 계산된 값 사용.
            save_translation_history( # ... status=task.status ...
                original_filename=os.path.basename(filepath),
                translated_filename=os.path.basename(output_path), # output_path 사용
                src_lang=src_lang, tgt_lang=tgt_lang, model=model,
                status=task.status, task_id=task_id
            )
            raise InterruptedError("Translation stopped by user during chart processing.")


        if final_path_from_charts and os.path.exists(final_path_from_charts): # 성공적으로 차트까지 완료
            task.output_file = final_path_from_charts # 일반적으로 output_path와 동일할 것
            task.progress = 100
            task.status = language_resources.get(task.current_ui_language, {}).get("status_completed", "완료")
            save_translation_history( # ... status=task.status ...
                original_filename=os.path.basename(filepath),
                translated_filename=os.path.basename(final_path_from_charts),
                src_lang=src_lang, tgt_lang=tgt_lang, model=model,
                status=task.status, task_id=task_id
            )
        else: # 차트 처리 실패 (중지 아님)
            task.error = task.error or language_resources.get(task.current_ui_language, {}).get("error_chart_translation_failed", "차트 번역 실패 (또는 파일 저장 실패)")
            # 1단계 결과라도 output_path에 저장 시도 (이미 temp_path_for_charts로 저장된 prs 사용 가능)
            # 하지만 이미 prs는 1단계 내용이므로, output_path에 덮어쓰기.
            # prs.save(output_path) # 이미 1단계 내용을 담고 있는 prs를 최종 output_path에 저장
            # task.output_file = output_path
            # task.status = language_resources.get(task.current_ui_language, {}).get("status_completed_stage1_only", "1단계만 완료 (차트 오류)")
            # save_translation_history(...)
            # 더 나은 방법은 chart_processor가 실패해도 원본 pptx_path에서 output_path로 복사라도 하는 것.
            # 현재는 실패 시 task.error만 설정하고 넘어감.
            raise Exception(task.error)

    except InterruptedError as e_interrupt: # 사용자에 의한 중지 처리
        print(f"Task {task_id} was interrupted: {e_interrupt}")
        # task.status는 이미 설정되었을 것임. task.output_file도.
        # finally 블록에서 task.completed = True 처리.
    except Exception as e:
        print(f"Error in translate_worker (task {task_id}): {e}")
        task.error = str(e)
        task.status = language_resources.get(task.current_ui_language, {}).get("status_error", "오류")
    finally:
        task.completed = True
        task.completion_time = time.time()
        
        # 중지 상태가 명확히 설정되지 않았다면, 여기서 일반적인 '중지됨'으로 설정
        if task.stop_event.is_set() and "중지됨" not in task.status and "stopped" not in task.status.lower():
            task.status = language_resources.get(task.current_ui_language, {}).get("status_stopped", "중지됨")

        final_update = {
            'progress': task.progress if task.progress <=100 else 100 , # 진행률 100 넘지 않게
            'status': task.status,
            'completed': task.completed,
            'error': task.error,
            'current_task': task.current_task
        }
        # output_file이 실제로 존재하고, task에 오류가 없거나 중지된 경우에만 다운로드 URL 제공
        if task.output_file and os.path.exists(task.output_file) and (not task.error or task.stop_event.is_set()):
            final_update['download_url'] = f'/api/download/{task_id}'
        elif task.error and not task.stop_event.is_set(): # 오류가 있고 중지된게 아니라면 다운로드 버튼 제공 안함
             final_update['download_url'] = None


        task.queue.put(final_update)
        print(f"Task {task_id} finished with status: {task.status}, output: {task.output_file}, error: {task.error}")


@app.route('/api/progress/<task_id>')
def get_progress(task_id): #
    """Server-Sent Events로 진행률 전송"""
    task = tasks.get(task_id) #
    if not task: #
        return jsonify({'error': '작업을 찾을 수 없습니다'}), 404 #

    def generate(): #
        while not task.completed: #
            try:
                data = task.queue.get(timeout=1) #
                yield f"data: {json.dumps(data)}\n\n" #
            except queue.Empty: #
                yield f"data: {json.dumps({'progress': task.progress, 'status': task.status, 'current_task': task.current_task, 'completed': task.completed, 'error': task.error})}\n\n" # 현재 상태 전송 시 모든 정보 포함

        final_data = { #
            'progress': task.progress, #
            'status': task.status, #
            'completed': task.completed, #
            'error': task.error #
        }
        if task.output_file: #
            final_data['download_url'] = f'/api/download/{task_id}' #

        yield f"data: {json.dumps(final_data)}\n\n" #

    return Response(generate(), mimetype='text/event-stream') #

@app.route('/api/download/<task_id>')
def download_file(task_id): #
    """번역된 파일 다운로드"""
    task = tasks.get(task_id) #
    if not task or not task.output_file or not os.path.exists(task.output_file): # 파일 존재 여부 확인 추가
        # 파일이 없다는 메시지를 UI 언어에 맞게 표시 (task.current_ui_language 사용)
        error_message = language_resources.get(task.current_ui_language if task else config.DEFAULT_UI_LANGUAGE, {}).get("error_file_not_found_for_download", "다운로드할 파일을 찾을 수 없습니다")
        return jsonify({'error': error_message}), 404 #

    return send_file( #
        task.output_file, #
        as_attachment=True, #
        download_name=os.path.basename(task.output_file) #
    )

@app.route('/api/history')
def get_history(): #
    """번역 이력 조회"""
    history_file = os.path.join(config.HISTORY_DIR, "translation_history.json") #
    if os.path.exists(history_file): #
        with open(history_file, 'r', encoding='utf-8') as f: #
            history_data = json.load(f) #
        return jsonify(history_data) #
    return jsonify([]) #

# --- 주기적인 정리를 위한 함수 ---
def cleanup_old_files_and_tasks(): #
    # Flask 앱 컨텍스트 내에서 실행되어야 app.config에 접근 가능
    with app.app_context():
        current_time_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print(f"[{current_time_str}] 주기적인 오래된 파일 및 작업 정리 시작...")

        # 1. 오래된 파일 정리 (UPLOAD_FOLDER 내)
        retention_days_files = config.FILE_RETENTION_DAYS if hasattr(config, 'FILE_RETENTION_DAYS') else 7 # 설정값 없으면 기본 7일
        cutoff_time_files = time.time() - (retention_days_files * 24 * 60 * 60)

        upload_dir_path = app.config['UPLOAD_FOLDER']
        if os.path.exists(upload_dir_path):
            for filename in os.listdir(upload_dir_path):
                file_path = os.path.join(upload_dir_path, filename)
                try:
                    if os.path.isfile(file_path):
                        file_mod_time = os.path.getmtime(file_path)
                        if file_mod_time < cutoff_time_files:
                            os.remove(file_path)
                            print(f"오래된 파일 삭제 (수정 시간 기준): {file_path}")
                except Exception as e:
                    print(f"파일 삭제 중 오류 ({file_path}): {e}")

        # 2. 오래된 작업 정보 정리 (tasks 딕셔너리 내)
        retention_hours_tasks = config.TASK_RETENTION_HOURS if hasattr(config, 'TASK_RETENTION_HOURS') else 24 # 설정값 없으면 기본 24시간
        cutoff_time_tasks = time.time() - (retention_hours_tasks * 60 * 60)
        tasks_to_delete_ids = []

        for task_id, task_obj in list(tasks.items()):
            if task_obj.completed and task_obj.completion_time and task_obj.completion_time < cutoff_time_tasks:
                tasks_to_delete_ids.append(task_id)
                # 이미 위에서 파일 수정 시간 기준으로 삭제되었을 수 있으나, task 객체에 저장된 경로로 한 번 더 시도
                paths_to_check_delete = [
                    getattr(task_obj, 'original_filepath', None),
                    getattr(task_obj, 'log_filepath', None),
                    getattr(task_obj, 'output_file', None)
                ]
                for p_to_delete in paths_to_check_delete:
                    if p_to_delete and os.path.exists(p_to_delete) and os.path.isfile(p_to_delete):
                        # 파일 경로가 UPLOAD_FOLDER 내에 있는지 확인 (보안 강화)
                        if os.path.commonpath([upload_dir_path, os.path.abspath(p_to_delete)]) == upload_dir_path:
                            try:
                                os.remove(p_to_delete)
                                print(f"오래된 작업 관련 파일 삭제 (Task 객체 기반): {p_to_delete}")
                            except Exception as e_task_file:
                                print(f"작업 관련 파일 삭제 중 오류 ({p_to_delete}): {e_task_file}")
                        else:
                            print(f"경고: 작업 관련 파일 경로가 UPLOAD_FOLDER 외부에 있어 삭제하지 않음: {p_to_delete}")


        for task_id in tasks_to_delete_ids:
            if task_id in tasks:
                del tasks[task_id]
                print(f"오래된 작업 정보 삭제 (tasks dict): {task_id}")

        current_time_str_end = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print(f"[{current_time_str_end}] 주기적인 정리 작업 완료.")


if __name__ == '__main__':
    os.makedirs(UPLOAD_FOLDER, exist_ok=True) #
    os.makedirs(config.LOGS_DIR, exist_ok=True) #
    os.makedirs(config.HISTORY_DIR, exist_ok=True) #

    # 스케줄러 시작
    scheduler = BackgroundScheduler(daemon=True) #
    # 예: 매일 새벽 3시에 cleanup_old_files_and_tasks 함수 실행
    cleanup_hour = config.CLEANUP_HOUR if hasattr(config, 'CLEANUP_HOUR') else 3
    scheduler.add_job(cleanup_old_files_and_tasks, 'cron', hour=cleanup_hour, minute=0) #
    # 또는 테스트용으로 짧은 간격 (예: 매 1시간 마다)
    # scheduler.add_job(cleanup_old_files_and_tasks, 'interval', hours=1)
    scheduler.start() #
    print(f"파일 및 작업 자동 정리 스케줄러 시작됨 (매일 {cleanup_hour}:00 실행).")

    # Flask 앱 실행
    # debug=True 일 때 use_reloader=False 옵션은 스케줄러가 두 번 실행되는 것을 방지할 수 있습니다.
    # 프로덕션 환경에서는 debug=False 로 실행합니다.
    app.run(debug=True, host='0.0.0.0', port=5001, use_reloader=False if os.environ.get("WERKZEUG_RUN_MAIN") == "true" else True)