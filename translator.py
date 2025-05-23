# translator.py
import logging
import time
from typing import Optional, List, Dict, Any, Tuple
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading
import hashlib
import json

import config
from interfaces import AbsTranslator, AbsOllamaService

logger = logging.getLogger(__name__)

class OllamaTranslator(AbsTranslator):
    def __init__(self):
        self.translation_cache: Dict[str, str] = {}
        self.cache_lock = threading.Lock()
        logger.info("OllamaTranslator 초기화됨.")

    def _get_cache_key(self, text_to_translate: str, src_lang_ui_name: str, tgt_lang_ui_name: str, model_name: str) -> str:
        key_string = f"{src_lang_ui_name}|{tgt_lang_ui_name}|{model_name}|{text_to_translate}"
        return hashlib.md5(key_string.encode('utf-8')).hexdigest()

    def _create_prompt(self, text: str, src_lang: str, tgt_lang: str) -> str:
        """번역 프롬프트 생성"""
        return f"You are a professional translator. Translate the following text from {src_lang} to {tgt_lang}. Provide only the translated text itself, without any additional explanations, introductory phrases, or quotation marks around the translation. Text to translate:\n\n{text}"

    def _call_ollama_api(self, prompt: str, model_name: str, temperature: float, 
                        ollama_service: AbsOllamaService) -> Tuple[Optional[str], Optional[str]]:
        """Ollama API 호출 및 오류 처리"""
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

        # 캐시 확인
        cache_key = self._get_cache_key(text_to_translate, src_lang_ui_name, tgt_lang_ui_name, model_name)
        with self.cache_lock:
            if cache_key in self.translation_cache:
                return self.translation_cache[cache_key]

        # API 호출
        prompt = self._create_prompt(text_to_translate, src_lang_ui_name, tgt_lang_ui_name)
        temperature = ocr_temperature if is_ocr_text and ocr_temperature else config.TRANSLATOR_TEMPERATURE_GENERAL
        
        result, error = self._call_ollama_api(prompt, model_name, temperature, ollama_service_instance)
        
        if result:
            with self.cache_lock:
                self.translation_cache[cache_key] = result
            return result
        else:
            text_snippet = text_to_translate[:20].replace('\n', ' ') + "..."
            return f"오류: {error} - \"{text_snippet}\""

    def translate_texts_batch(self, texts_to_translate: List[str], src_lang_ui_name: str, tgt_lang_ui_name: str,
                              model_name: str, ollama_service_instance: AbsOllamaService,
                              is_ocr_text: bool = False, ocr_temperature: Optional[float] = None,
                              stop_event: Optional[threading.Event] = None) -> List[str]:
        
        if not texts_to_translate:
            return []

        translated_results = [""] * len(texts_to_translate)
        tasks_to_process = []
        
        # 캐시 확인 및 작업 필터링
        for i, text in enumerate(texts_to_translate):
            if stop_event and stop_event.is_set():
                return [t if t else texts_to_translate[j] for j, t in enumerate(translated_results)]
                
            if not text or not text.strip():
                translated_results[i] = text if text else ""
                continue
                
            cache_key = self._get_cache_key(text, src_lang_ui_name, tgt_lang_ui_name, model_name)
            with self.cache_lock:
                if cache_key in self.translation_cache:
                    translated_results[i] = self.translation_cache[cache_key]
                else:
                    tasks_to_process.append({'text': text, 'index': i})

        if not tasks_to_process:
            return translated_results

        # 병렬 처리
        with ThreadPoolExecutor(max_workers=config.MAX_TRANSLATION_WORKERS) as executor:
            future_to_task = {}
            
            for task in tasks_to_process:
                if stop_event and stop_event.is_set():
                    break
                    
                future = executor.submit(
                    self.translate_text,
                    task['text'],
                    src_lang_ui_name,
                    tgt_lang_ui_name,
                    model_name,
                    ollama_service_instance,
                    is_ocr_text,
                    ocr_temperature
                )
                future_to_task[future] = task

            for future in as_completed(future_to_task):
                task = future_to_task[future]
                try:
                    result = future.result()
                    translated_results[task['index']] = result
                except Exception as e:
                    logger.error(f"배치 번역 중 오류: {e}")
                    text_snippet = task['text'][:20].replace('\n', ' ') + "..."
                    translated_results[task['index']] = f"오류: 배치 처리 중 예외 - \"{text_snippet}\""

        # 중단된 작업 처리
        if stop_event and stop_event.is_set():
            for i, result in enumerate(translated_results):
                if not result:
                    translated_results[i] = texts_to_translate[i]

        return translated_results

    def clear_translation_cache(self):
        with self.cache_lock:
            count = len(self.translation_cache)
            self.translation_cache.clear()
        logger.info(f"번역 캐시 비움 ({count}개 항목)")