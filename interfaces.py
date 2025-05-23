# interfaces.py
from abc import ABC, abstractmethod
from typing import List, Any, Optional, Dict, Callable

from PIL import Image
from pptx import Presentation

class AbsTranslator(ABC):
    @abstractmethod
    def translate_text(self, text_to_translate: str, src_lang_ui_name: str, tgt_lang_ui_name: str,
                       model_name: str, ollama_service_instance: 'AbsOllamaService',
                       is_ocr_text: bool = False, ocr_temperature: Optional[float] = None) -> str:
        pass

    @abstractmethod
    def translate_texts_batch(self, texts_to_translate: List[str], src_lang_ui_name: str, tgt_lang_ui_name: str,
                              model_name: str, ollama_service_instance: 'AbsOllamaService',
                              is_ocr_text: bool = False, ocr_temperature: Optional[float] = None,
                              stop_event: Optional[Any] = None) -> List[str]:
        pass

    @abstractmethod
    def clear_translation_cache(self) -> None:
        pass

class AbsOcrHandler(ABC):
    @abstractmethod
    def ocr_image(self, image_pil_rgb: Image.Image) -> List[Any]:
        pass

    @abstractmethod
    def render_translated_text_on_image(self, image_pil_original: Image.Image, box: List[List[int]],
                                        translated_text: str, font_code_for_render: str = 'en',
                                        original_text: str = "", ocr_angle: Any = None) -> Image.Image:
        pass

    @abstractmethod
    def has_text_in_image_bytes(self, image_bytes: bytes) -> bool:
        pass

    @property
    @abstractmethod
    def ocr_engine(self) -> Any:
        pass

    @property
    @abstractmethod
    def use_gpu(self) -> bool:
        pass

    @property
    @abstractmethod
    def current_lang_codes(self) -> Any:
        pass


class AbsPptxProcessor(ABC):
    @abstractmethod
    def get_file_info(self, file_path: str) -> Dict[str, int]:
        pass

    @abstractmethod
    def translate_presentation_stage1(self, prs: Presentation, src_lang_ui_name: str, tgt_lang_ui_name: str,
                                      translator: AbsTranslator, ocr_handler: Optional[AbsOcrHandler],
                                      model_name: str, ollama_service: 'AbsOllamaService',
                                      font_code_for_render: str, task_log_filepath: str,
                                      progress_callback_item_completed: Optional[Callable[[Any, str, int, str], None]] = None,
                                      stop_event: Optional[Any] = None,
                                      image_translation_enabled: bool = True,
                                      ocr_temperature: Optional[float] = None
                                      ) -> bool:
        pass

class AbsChartProcessor(ABC):
    @abstractmethod
    def translate_charts_in_pptx(self, pptx_path: str, src_lang_ui_name: str, tgt_lang_ui_name: str,
                                 model_name: str, output_path: str = None,
                                 progress_callback_item_completed: Optional[Callable[[Any, str, int, str], None]] = None,
                                 stop_event: Optional[Any] = None,
                                 task_log_filepath: Optional[str] = None) -> Optional[str]:
        pass

class AbsOllamaService(ABC):
    @abstractmethod
    def is_installed(self) -> bool:
        pass

    @abstractmethod
    def is_running(self) -> tuple[bool, Optional[str]]:
        pass

    @abstractmethod
    def start_ollama(self) -> bool:
        pass

    @abstractmethod
    def get_text_models(self) -> List[str]:
        pass

    @abstractmethod
    def invalidate_models_cache(self) -> None:
        pass

    @abstractmethod
    def pull_model_with_progress(self, model_name: str,
                                 progress_callback: Optional[Callable[[str, int, int, bool], None]] = None,
                                 stop_event: Optional[Any] = None) -> bool:
        pass

    @property
    @abstractmethod
    def url(self) -> str:
        pass


# OCR 핸들러 팩토리 인터페이스
class AbsOcrHandlerFactory(ABC):
    @abstractmethod
    def get_ocr_handler(self, lang_code_ui: str, use_gpu: bool, debug_enabled: bool = False) -> Optional[AbsOcrHandler]:
        pass

    @abstractmethod
    def get_engine_name_display(self, lang_code_ui: str) -> str:
        pass

    @abstractmethod
    def get_ocr_lang_code(self, lang_code_ui: str) -> Optional[str]:
        pass