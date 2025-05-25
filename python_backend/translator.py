# translator.py
import logging
import time
from typing import Optional, List, Dict, Any, Tuple
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

    def translate_texts_batch(self, texts_to_translate: List[str], src_lang_ui_name: str, tgt_lang_ui_name: str,
                              model_name: str, ollama_service_instance: AbsOllamaService,
                              is_ocr_text: bool = False, ocr_temperature: Optional[float] = None,
                              stop_event: Optional[threading.Event] = None) -> List[str]:

        if not texts_to_translate:
            return []

        translated_results = [""] * len(texts_to_translate)
        tasks_to_process_indices: List[int] = [] # 실제 번역이 필요한 항목의 인덱스 저장

        for i, text in enumerate(texts_to_translate):
            if stop_event and stop_event.is_set(): # 중단 요청 확인
                # 현재까지 번역된 결과와 원본 텍스트로 나머지 채우기
                for j in range(len(texts_to_translate)):
                    if not translated_results[j]: # 아직 결과가 없는 경우
                        translated_results[j] = texts_to_translate[j] # 원본으로 채움
                return translated_results

            if not text or not text.strip():
                translated_results[i] = text if text else ""
                continue

            cache_key = self._get_cache_key(text, src_lang_ui_name, tgt_lang_ui_name, model_name)
            with self.cache_lock:
                if cache_key in self.translation_cache:
                    self.translation_cache.move_to_end(cache_key) # 캐시 히트 시 LRU 업데이트
                    translated_results[i] = self.translation_cache[cache_key]
                else:
                    tasks_to_process_indices.append(i) # 번역 필요한 인덱스 추가

        if not tasks_to_process_indices: # 모든 텍스트가 캐시된 경우
            return translated_results

        # 실제 번역이 필요한 텍스트만 추출
        actual_texts_to_translate_api = [texts_to_translate[i] for i in tasks_to_process_indices]
        
        # 병렬 처리 설정
        num_workers = min(config.MAX_TRANSLATION_WORKERS, len(actual_texts_to_translate_api))
        if num_workers == 0 : # 번역할 것이 없는 경우 (이론상 위에서 걸러짐)
             return translated_results

        with ThreadPoolExecutor(max_workers=num_workers) as executor:
            future_to_original_index: Dict[Any, int] = {}

            for i, text_idx in enumerate(tasks_to_process_indices):
                if stop_event and stop_event.is_set():
                    break # 새 작업 제출 중단

                text_content = texts_to_translate[text_idx]
                future = executor.submit(
                    self.translate_text, # 개별 translate_text 호출 (내부적으로 캐시 처리)
                    text_content,
                    src_lang_ui_name,
                    tgt_lang_ui_name,
                    model_name,
                    ollama_service_instance,
                    is_ocr_text,
                    ocr_temperature
                )
                future_to_original_index[future] = text_idx # Future 객체와 원본 리스트의 인덱스 매핑

            for future in as_completed(future_to_original_index):
                original_idx = future_to_original_index[future]
                if stop_event and stop_event.is_set() and not translated_results[original_idx]: # 이미 처리된건 놔둠
                     translated_results[original_idx] = texts_to_translate[original_idx] # 중단 시 원본으로
                     continue

                try:
                    result = future.result()
                    translated_results[original_idx] = result
                except Exception as e:
                    logger.error(f"배치 번역 중 인덱스 {original_idx} 처리 오류: {e}")
                    text_snippet = texts_to_translate[original_idx][:20].replace('\n', ' ') + "..."
                    translated_results[original_idx] = f"오류: 배치 처리 중 예외 - \"{text_snippet}\""
        
        # 모든 작업 완료 후, 중단 요청으로 인해 처리되지 못한 항목이 있다면 원본으로 채우기
        if stop_event and stop_event.is_set():
            for i in range(len(texts_to_translate)):
                if not translated_results[i]: # 아직 결과가 없는 경우
                    translated_results[i] = texts_to_translate[i]

        return translated_results


    def clear_translation_cache(self):
        with self.cache_lock:
            count = len(self.translation_cache)
            self.translation_cache.clear()
        logger.info(f"번역 캐시 비움 ({count}개 항목)")