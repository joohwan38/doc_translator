# config.py
import os
import logging
import platform

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
DEFAULT_OLLAMA_MODEL = "gemma3:12b"
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