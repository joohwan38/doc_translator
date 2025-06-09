# config.py
import os
import logging
import platform
import tempfile

PROJECT_ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
APP_NAME_FOR_PATHS = "PowerpointDocumentTranslator"

LANGUAGES_DIR_NAME = "locales"
LANGUAGES_DIR = os.path.join(PROJECT_ROOT_DIR, LANGUAGES_DIR_NAME)
UI_SUPPORTED_LANGUAGES = {
    "en": "English",
    "ko": "한국어",
    "ja": "日本語",
    "zh-CN": "简体中文",  # 중국어(간체)
    "zh-TW": "繁體中文",  # 중국어(번체)
    "th": "ไทย"         # 태국어
}
DEFAULT_UI_LANGUAGE = "en"

# UPLOAD_FOLDER 설정 추가 (tempfile과 APP_NAME_FOR_PATHS를 사용하여 경로 구성)
# uploads 폴더를 앱 데이터 폴더 하위로 옮기거나, 기존처럼 temp 디렉토리 사용 가능
# 여기서는 앱 데이터 폴더 하위에 두는 예시입니다.
def get_app_data_dir_for_config(): # config.py 내에서 사용하기 위한 간단한 함수
    system = platform.system()
    if system == "Darwin":
        return os.path.join(os.path.expanduser("~"), "Library", "Application Support", APP_NAME_FOR_PATHS)
    elif system == "Windows":
        return os.path.join(os.getenv("APPDATA"), APP_NAME_FOR_PATHS)
    else: # Linux and other OS
        return os.path.join(os.path.expanduser("~"), ".config", APP_NAME_FOR_PATHS)

APP_DATA_DIR_CONFIG = get_app_data_dir_for_config()
UPLOAD_FOLDER = os.path.join(APP_DATA_DIR_CONFIG, 'uploads') # 예: ~/.config/PowerpointDocumentTranslator/uploads

# LOGS_DIR, HISTORY_DIR 등도 APP_DATA_DIR_CONFIG 기반으로 경로 일관성 확보 가능
# 예시:
# LOGS_DIR = os.path.join(APP_DATA_DIR_CONFIG, "logs" if platform.system() != "Darwin" else "Logs")
# HISTORY_DIR = os.path.join(APP_DATA_DIR_CONFIG, "hist")

# DEFAULT_OLLAMA_MODEL 설정 (이미 있다면 값 확인)
DEFAULT_OLLAMA_MODEL = "gemma3:latest" # index.html에서 사용하는 기본 모델명과 일치

ALLOWED_EXTENSIONS = {'pptx', 'xlsx'}

MAX_TRANSLATION_CACHE_SIZE = 1000

def get_app_data_dir():
    system = platform.system()
    if system == "Darwin":
        return os.path.join(os.path.expanduser("~"), "Library", "Application Support", APP_NAME_FOR_PATHS)
    elif system == "Windows":
        return os.path.join(os.getenv("APPDATA"), APP_NAME_FOR_PATHS)
    else:
        return os.path.join(os.path.expanduser("~"), ".config", APP_NAME_FOR_PATHS)

def get_logs_dir():
    system = platform.system()
    if system == "Darwin":
        return os.path.join(get_app_data_dir(), "Logs")
    else:
        return os.path.join(get_app_data_dir(), "logs")

def get_history_dir():
    return os.path.join(get_app_data_dir(), "hist")

APP_NAME = "Powerpoint Document Translator"
DEFAULT_OLLAMA_MODEL = "gemma3:latest"
TRANSLATION_LANGUAGES_MAP = {
    "ko": "Korean",  # 내부적으로 사용할 코드와 기본 영문 이름
    "ja": "Japanese",
    "en": "English",
    "zh-CN": "Chinese (Simplified)",
    "zh-TW": "Chinese (Traditional)",
    "th": "Thai",
    "es": "Spanish"
}
USER_SETTINGS_FILENAME = "user_settings.json"

ASSETS_DIR_NAME = "assets"
FONTS_DIR_NAME = "fonts"
ASSETS_DIR = os.path.join(PROJECT_ROOT_DIR, ASSETS_DIR_NAME)
FONTS_DIR = os.path.join(PROJECT_ROOT_DIR, FONTS_DIR_NAME)

LOGS_DIR = get_logs_dir()
HISTORY_DIR = get_history_dir()
APP_DATA_DIR = get_app_data_dir()

DEFAULT_LOG_LEVEL = logging.INFO
DEBUG_LOG_LEVEL = logging.DEBUG

WEIGHT_TEXT_CHAR = 1
WEIGHT_IMAGE = 100
WEIGHT_CHART = 15
WEIGHT_EXCEL_CELL = 1

UI_LANG_TO_PADDLEOCR_CODE_MAP = {
    "한국어": "korean", "영어": "en",
    "중국어": "ch_doc",
    "대만어": "chinese_cht",
    "일본어": "japan",
    "태국어": "th",
    "스페인어": "es",
}
DEFAULT_PADDLE_OCR_LANG = "korean"

OCR_LANGUAGE_FONT_MAP = {
    'korean': 'NotoSansCJK-Regular.ttc', 'japan': 'NotoSansCJK-Regular.ttc',
    'ch': 'NotoSansCJK-Regular.ttc', 'chinese_cht': 'NotoSansCJK-Regular.ttc',
    'en': 'NotoSansCJK-Regular.ttc', 'th': 'NotoSansThai-VariableFont_wdth,wght.ttf',
    'es': 'NotoSansCJK-Regular.ttc',
    'korean_bold': 'NotoSansCJK-Bold.ttc', 'japan_bold': 'NotoSansCJK-Bold.ttc',
    'ch_bold': 'NotoSansCJK-Bold.ttc', 'chinese_cht_bold': 'NotoSansCJK-Bold.ttc',
    'en_bold': 'NotoSansCJK-Bold.ttc', 'th_bold': 'NotoSansThai-VariableFont_wdth,wght.ttf',
    'es_bold': 'NotoSansCJK-Bold.ttc',
}
OCR_DEFAULT_FONT_FILENAME = 'NotoSansCJK-Regular.ttc'
OCR_DEFAULT_BOLD_FONT_FILENAME = 'NotoSansCJK-Bold.ttc'

DEFAULT_OLLAMA_URL = "http://localhost:11434"
OLLAMA_CONNECT_TIMEOUT = 5
OLLAMA_READ_TIMEOUT = 180
OLLAMA_PULL_READ_TIMEOUT = None
MODELS_CACHE_TTL_SECONDS = 300

TRANSLATOR_TEMPERATURE_GENERAL = 0.2
MAX_TRANSLATION_WORKERS = 4
MAX_OCR_WORKERS = MAX_TRANSLATION_WORKERS

MIN_MEANINGFUL_CHAR_RATIO_SKIP = 0.1
MIN_MEANINGFUL_CHAR_RATIO_OCR = 0.1

UI_LANG_TO_FONT_CODE_MAP = {
    "한국어": "korean", "일본어": "japan", "영어": "en",
    "중국어": "ch_doc", "대만어": "chinese_cht", "태국어": "th", "스페인어": "es",
}
MAX_HISTORY_ITEMS = 50
UI_PROGRESS_UPDATE_INTERVAL = 0.2

DEFAULT_ADVANCED_SETTINGS = {
    "ocr_temperature": 0.4,
    "image_translation_enabled": True,
    "ocr_use_gpu": False
}

# 파일 및 작업 정리 관련 설정
FILE_RETENTION_DAYS = 7  # 업로드된 파일 보관 기간 (일)
TASK_RETENTION_HOURS = 24 # 완료된 작업 정보 보관 기간 (시간)
CLEANUP_HOUR = 3         # 매일 정리 작업 실행 시간 (0-23시)