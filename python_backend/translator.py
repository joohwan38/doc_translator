# translator.py
import logging
import time
from typing import Optional, List, Dict, Any, Tuple, Callable
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading
import hashlib
import json
from collections import OrderedDict # OrderedDict 사용

import config
from interfaces import AbsTranslator, AbsOllamaService

logger = logging.getLogger(__name__)

# config.py 에 MAX_TRANSLATION_CACHE_SIZE 추가 필요 (예: MAX_TRANSLATION_CACHE_SIZE = 1000)
DEFAULT_MAX_CACHE_SIZE = 1000


class OllamaTranslator(AbsTranslator):
    def __init__(self, max_cache_size: Optional[int] = None):
        # OrderedDict를 사용하여 간단한 FIFO 캐시 구현
        self.max_cache_size = max_cache_size if max_cache_size is not None \
                              else getattr(config, 'MAX_TRANSLATION_CACHE_SIZE', DEFAULT_MAX_CACHE_SIZE)
        self.translation_cache: OrderedDict[str, str] = OrderedDict()
        self.cache_lock = threading.Lock()
        logger.info(f"OllamaTranslator 초기화됨. 번역 캐시 최대 크기: {self.max_cache_size}")

    def _get_cache_key(self, text_to_translate: str, src_lang_ui_name: str, tgt_lang_ui_name: str, model_name: str) -> str:
        key_string = f"{src_lang_ui_name}|{tgt_lang_ui_name}|{model_name}|{text_to_translate}"
        return hashlib.md5(key_string.encode('utf-8')).hexdigest()

    def _create_prompt(self, text: str, src_lang: str, tgt_lang: str) -> str:
        return f"You are a professional translator. Translate the following text from {src_lang} to {tgt_lang}. Provide only the translated text itself, without any additional explanations, introductory phrases, or quotation marks around the translation. Text to translate:\n\n{text}"

    def _call_ollama_api(self, prompt: str, model_name: str, temperature: float,
                        ollama_service: AbsOllamaService) -> Tuple[Optional[str], Optional[str]]:
        try:
            if not ollama_service or not ollama_service.is_running()[0]:
                return None, "Ollama 서버 연결 실패"

            api_url = f"{ollama_service.url}/api/generate"
            payload = {
                "model": model_name,
                "prompt": prompt,
                "stream": False,
                "options": {"temperature": temperature}
            }

            response = requests.post(
                api_url,
                json=payload,
                timeout=(config.OLLAMA_CONNECT_TIMEOUT, config.OLLAMA_READ_TIMEOUT)
            )
            response.raise_for_status()

            data = response.json()
            if data and "response" in data:
                return data["response"].strip(), None
            else:
                return None, "API 응답 형식 이상"

        except requests.exceptions.Timeout:
            return None, "API 시간 초과"
        except requests.exceptions.RequestException as e:
            return None, f"API 요청 실패 ({e.__class__.__name__})"
        except Exception as e:
            logger.error(f"번역 API 호출 중 예외: {e}", exc_info=True)
            return None, f"번역 중 예외 ({e.__class__.__name__})"

    def translate_text(self, text_to_translate: str, src_lang_ui_name: str, tgt_lang_ui_name: str,
                       model_name: str, ollama_service_instance: AbsOllamaService,
                       is_ocr_text: bool = False, ocr_temperature: Optional[float] = None) -> str:

        if not text_to_translate or not text_to_translate.strip():
            return text_to_translate if text_to_translate else ""

        cache_key = self._get_cache_key(text_to_translate, src_lang_ui_name, tgt_lang_ui_name, model_name)
        with self.cache_lock:
            if cache_key in self.translation_cache:
                # 캐시된 항목을 최근 사용으로 이동 (LRU 효과, OrderedDict의 move_to_end 사용)
                self.translation_cache.move_to_end(cache_key)
                return self.translation_cache[cache_key]

        prompt = self._create_prompt(text_to_translate, src_lang_ui_name, tgt_lang_ui_name)
        temperature = ocr_temperature if is_ocr_text and ocr_temperature else config.TRANSLATOR_TEMPERATURE_GENERAL

        result, error = self._call_ollama_api(prompt, model_name, temperature, ollama_service_instance)

        if result:
            with self.cache_lock:
                self.translation_cache[cache_key] = result
                # 캐시 크기 확인 및 가장 오래된 항목 제거 (FIFO)
                if len(self.translation_cache) > self.max_cache_size:
                    self.translation_cache.popitem(last=False) # OrderedDict의 FIFO 제거
            return result
        else:
            text_snippet = text_to_translate[:20].replace('\n', ' ') + "..."
            # 오류 발생 시 캐시에 저장하지 않음
            return f"오류: {error} - \"{text_snippet}\""

    def translate_texts(self, texts_to_translate: List[str], src_lang_ui_name: str, tgt_lang_ui_name: str,
                        model_name: str, ollama_service_instance: AbsOllamaService,
                        is_ocr_text: bool = False, ocr_temperature: Optional[float] = None,
                        stop_event: Optional[threading.Event] = None,
                        progress_callback: Optional[Callable[[str, str, float, str], None]] = None,
                        base_location_key: str = "status_key_translating",
                        base_task_key: str = "status_task_translating_text") -> List[str]:
        if not texts_to_translate:
            return []

        translated_results = [""] * len(texts_to_translate)
        tasks_to_process_indices: List[int] = []

        for i, text in enumerate(texts_to_translate):
            if stop_event and stop_event.is_set():
                for j in range(len(texts_to_translate)): translated_results[j] = texts_to_translate[j]
                return translated_results

            if not text or not text.strip():
                translated_results[i] = text if text else ""
                if progress_callback:
                    progress_callback(base_location_key, "status_task_skipping_empty_text", 0, "")
                continue

            cache_key = self._get_cache_key(text, src_lang_ui_name, tgt_lang_ui_name, model_name)
            with self.cache_lock:
                if cache_key in self.translation_cache:
                    self.translation_cache.move_to_end(cache_key)
                    translated_results[i] = self.translation_cache[cache_key]
                    if progress_callback:
                        work_done = len(text) * config.WEIGHT_TEXT_CHAR
                        snippet = translated_results[i][:30].replace('\n', ' ') + "..."
                        progress_callback(base_location_key, "status_task_using_cache", work_done, snippet)
                else:
                    tasks_to_process_indices.append(i)

        if not tasks_to_process_indices:
            return translated_results

        actual_texts_to_translate_api = [texts_to_translate[i] for i in tasks_to_process_indices]
        num_workers = min(config.MAX_TRANSLATION_WORKERS, len(actual_texts_to_translate_api))
        if num_workers == 0:
            return translated_results

        with ThreadPoolExecutor(max_workers=num_workers) as executor:
            future_to_original_index = {
                executor.submit(
                    self.translate_text, texts_to_translate[text_idx], src_lang_ui_name, tgt_lang_ui_name,
                    model_name, ollama_service_instance, is_ocr_text, ocr_temperature
                ): text_idx
                for text_idx in tasks_to_process_indices
                if not (stop_event and stop_event.is_set())
            }

            for future in as_completed(future_to_original_index):
                original_idx = future_to_original_index[future]
                original_text = texts_to_translate[original_idx]

                if stop_event and stop_event.is_set():
                    if not translated_results[original_idx]:
                        translated_results[original_idx] = original_text
                    continue

                try:
                    result = future.result()
                    translated_results[original_idx] = result
                    if progress_callback:
                        work_done = len(original_text) * config.WEIGHT_TEXT_CHAR
                        snippet = result[:30].replace('\n', ' ') + "..."
                        progress_callback(base_location_key, base_task_key, work_done, snippet)
                except Exception as e:
                    logger.error(f"Batch translation error at index {original_idx}: {e}")
                    text_snippet = original_text[:20].replace('\n', ' ') + "..."
                    error_message = f"Error: Exception in batch processing - \"{text_snippet}\""
                    translated_results[original_idx] = error_message
                    if progress_callback:
                        work_done = len(original_text) * config.WEIGHT_TEXT_CHAR
                        progress_callback(base_location_key, "status_task_error", work_done, error_message)

        if stop_event and stop_event.is_set():
            for i in range(len(texts_to_translate)):
                if not translated_results[i]:
                    translated_results[i] = texts_to_translate[i]

        return translated_results

    def translate_texts_batch(self, texts_to_translate: List[str], src_lang_ui_name: str, tgt_lang_ui_name: str,
                              model_name: str, ollama_service_instance: AbsOllamaService,
                              is_ocr_text: bool = False, ocr_temperature: Optional[float] = None,
                              stop_event: Optional[threading.Event] = None) -> List[str]:
        # This method now simply calls the new translate_texts method for backward compatibility.
        return self.translate_texts(
            texts_to_translate, src_lang_ui_name, tgt_lang_ui_name, model_name,
            ollama_service_instance, is_ocr_text, ocr_temperature, stop_event,
            progress_callback=None # No progress callback in the old batch method
        )


    def clear_translation_cache(self):
        with self.cache_lock:
            count = len(self.translation_cache)
            self.translation_cache.clear()
        logger.info(f"번역 캐시 비움 ({count}개 항목)")