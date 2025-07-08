# chart_xml_handler.py
import zipfile
import xml.etree.ElementTree as ET
import os
import tempfile
import shutil
import logging
from typing import Callable, Any, Optional, List, Dict, IO
import traceback
import re

import config
from interfaces import AbsChartProcessor, AbsTranslator, AbsOllamaService
from utils import setup_task_logging 

logger = logging.getLogger(__name__)

class ChartXmlHandler(AbsChartProcessor):
    def __init__(self, translator_instance: AbsTranslator, ollama_service_instance: AbsOllamaService):
        self.translator = translator_instance
        self.ollama_service = ollama_service_instance
        # self.WEIGHT_CHART = config.WEIGHT_CHART # config.WEIGHT_CHART 직접 사용
        self.xml_namespaces_to_register = {
            'c': 'http://schemas.openxmlformats.org/drawingml/2006/chart',
            'a': 'http://schemas.openxmlformats.org/drawingml/2006/main',
            'r': 'http://schemas.openxmlformats.org/officeDocument/2006/relationships',
            'mc': 'http://schemas.openxmlformats.org/markup-compatibility/2006',
            'c14': 'http://schemas.microsoft.com/office/drawing/2007/8/2/chart',
            'c15': 'http://schemas.microsoft.com/office/drawing/2012/chart',
            'c16': 'http://schemas.microsoft.com/office/drawing/2014/chart',
            'c16r2': 'http://schemas.microsoft.com/office/drawing/2015/06/chart',
            'c16r3': 'http://schemas.microsoft.com/office/drawing/2017/03/chart',
            'cx': 'http://schemas.microsoft.com/office/drawing/2014/chartex'
        }
        for prefix, uri in self.xml_namespaces_to_register.items():
            try: ET.register_namespace(prefix, uri)
            except ValueError: pass 

    def _is_numeric_or_simple_symbols(self, text: str) -> bool:
        if not text: return True
        if re.fullmatch(r"[\d.,\s%+\-/*:$€£¥₩#\(\)]+", text): return True
        if len(text) == 1 and not re.search(r'[가-힣一-龠ぁ-んァ-ヶ]', text): return True
        return False


    def translate_charts_in_pptx(self, pptx_path: str, src_lang_ui_name: str, tgt_lang_ui_name: str,
                                 model_name: str, output_path: str = None,
                                 progress_callback_item_completed: Optional[Callable[[Any, str, float, str], None]] = None, # Changed int to float
                                 stop_event: Optional[Any] = None,
                                 task_log_filepath: Optional[str] = None) -> Optional[str]:

        if output_path is None:
            base_name = os.path.splitext(pptx_path)[0]
            output_path = f"{base_name}_chart_translated.pptx"

        initial_log_lines = [
            f"\n--- 2단계: 차트 XML 번역 시작 (ChartXmlHandler) ---",
            f"입력 파일: {os.path.basename(pptx_path)}, 출력 파일: {os.path.basename(output_path)}",
            f"언어: {src_lang_ui_name} -> {tgt_lang_ui_name}, 모델: {model_name}"
        ]
        f_task_log_chart_local: Optional[IO[str]] = None
        log_func: Optional[Callable[[str], None]] = None

        if task_log_filepath:
            f_task_log_chart_local, log_func_temp = setup_task_logging(task_log_filepath, initial_log_lines)
            if log_func_temp:
                log_func = log_func_temp
            else: 
                logger.info(f"PPTX 차트 XML 번역 시작: {os.path.basename(pptx_path)} -> {os.path.basename(output_path)}")
        else:
            logger.info(f"PPTX 차트 XML 번역 시작: {os.path.basename(pptx_path)} -> {os.path.basename(output_path)}")

        temp_dir_for_xml_processing = tempfile.mkdtemp(prefix="chart_xml_")

        try:
            unique_texts_to_translate_all_charts: Dict[str, None] = {}
            chart_xml_contents_map: Dict[str, bytes] = {}
            total_chart_weight = float(config.WEIGHT_CHART) # 전체 차트 작업에 할당된 가중치

            with zipfile.ZipFile(pptx_path, 'r') as zip_ref:
                chart_files = [f for f in zip_ref.namelist() if f.startswith('ppt/charts/') and f.endswith('.xml')]
                if log_func: log_func(f"총 {len(chart_files)}개 차트 XML 발견. 텍스트 수집 중...")

                for chart_xml_path_in_zip in chart_files:
                    # ... (기존 텍스트 수집 로직) ...
                    if stop_event and stop_event.is_set(): break
                    xml_content_bytes = zip_ref.read(chart_xml_path_in_zip)
                    chart_xml_contents_map[chart_xml_path_in_zip] = xml_content_bytes
                    content_str = xml_content_bytes.decode('utf-8', errors='ignore')
                    if content_str.lstrip().startswith('<?xml'): content_str = re.sub(r'^\s*<\?xml[^>]*\?>', '', content_str, count=1).strip()
                    try:
                        root = ET.fromstring(content_str)
                        for elem in root.iter():
                            if stop_event and stop_event.is_set(): break
                            if elem.tag.endswith('}t') or elem.tag.endswith('}v'):
                                original_text = elem.text
                                if original_text and original_text.strip():
                                    original_text_stripped = original_text.strip()
                                    if not self._is_numeric_or_simple_symbols(original_text_stripped):
                                        unique_texts_to_translate_all_charts[original_text_stripped] = None
                    except ET.ParseError as e_parse: # ... (오류 로깅)
                        pass
                
                if stop_event and stop_event.is_set(): # ... (중단 처리)
                    if log_func: log_func("차트 텍스트 수집 중 중단."); return None
                    return None

                # --- 진행률 보고: 텍스트 수집 완료 ---
                collection_weight = total_chart_weight * 0.1 # 전체 차트 가중치의 10%
                if progress_callback_item_completed and not (stop_event and stop_event.is_set()):
                    progress_callback_item_completed(
                        "전체 차트 분석", # location_key (i18n)
                        "chart_status_text_collection_complete", # task_type_key (i18n)
                        collection_weight,
                        f"{len(unique_texts_to_translate_all_charts)}개 고유 텍스트"
                    )

                texts_list_for_batch = list(unique_texts_to_translate_all_charts.keys())
                translation_map: Dict[str, str] = {}

                if texts_list_for_batch:
                    if log_func: log_func(f"차트 내 고유 텍스트 {len(texts_list_for_batch)}개 일괄 번역 시작...")
                    translated_texts_batch = self.translator.translate_texts(
                        texts_list_for_batch, src_lang_ui_name, tgt_lang_ui_name, model_name, 
                        self.ollama_service, is_ocr_text=False, stop_event=stop_event,
                        progress_callback=progress_callback_item_completed,
                        base_location_key="status_key_chart_translation",
                        base_task_key="status_task_translating_chart_text"
                    )
                    if stop_event and stop_event.is_set(): # ... (중단 처리)
                        if log_func: log_func("차트 텍스트 일괄 번역 중 중단."); return None
                        return None
                    
                    # --- 진행률 보고: 일괄 번역 완료 ---
                    batch_translation_weight = total_chart_weight * 0.3 # 전체 차트 가중치의 30%
                    if progress_callback_item_completed and not (stop_event and stop_event.is_set()):
                        progress_callback_item_completed(
                            "전체 차트 번역", # location_key (i18n)
                            "chart_status_batch_translation_complete", # task_type_key (i18n)
                            batch_translation_weight,
                            f"{len(translated_texts_batch)}개 번역됨"
                        )

                    if len(texts_list_for_batch) == len(translated_texts_batch): # ... (매핑 생성)
                        for original, translated in zip(texts_list_for_batch, translated_texts_batch): translation_map[original] = translated
                    else: # ... (오류 처리)
                        if f_task_log_chart_local and not f_task_log_chart_local.closed: f_task_log_chart_local.close()
                        if os.path.exists(temp_dir_for_xml_processing): shutil.rmtree(temp_dir_for_xml_processing)
                        return None

                modified_charts_data: Dict[str, bytes] = {}
                total_charts_count = len(chart_files) # 변수명 명확화
                processed_charts_count = 0
                
                # --- 진행률 보고: 개별 차트 적용 시작 ---
                # 남은 가중치를 개별 차트 적용에 분배
                apply_per_chart_total_weight = total_chart_weight * 0.6 
                weight_per_single_chart_apply = (apply_per_chart_total_weight / total_charts_count) if total_charts_count > 0 else 0


                for chart_xml_idx, chart_xml_path_in_zip in enumerate(chart_files):
                    if stop_event and stop_event.is_set(): break
                    if log_func: log_func(f"\n차트 XML 적용 중 ({chart_xml_idx + 1}/{total_charts_count}): {chart_xml_path_in_zip}")
                    # ... (XML 파싱 및 번역 적용 로직은 기존과 동일) ...
                    xml_content_bytes = chart_xml_contents_map[chart_xml_path_in_zip]
                    content_str = xml_content_bytes.decode('utf-8', errors='ignore')
                    if content_str.lstrip().startswith('<?xml'): content_str = re.sub(r'^\s*<\?xml[^>]*\?>', '', content_str, count=1).strip()
                    num_translated_in_chart = 0
                    try:
                        root = ET.fromstring(content_str)
                        for elem in root.iter():
                            if stop_event and stop_event.is_set(): break
                            if elem.tag.endswith('}t') or elem.tag.endswith('}v'):
                                original_text = elem.text
                                if original_text and original_text.strip():
                                    original_text_stripped = original_text.strip()
                                    if original_text_stripped in translation_map:
                                        translated = translation_map[original_text_stripped]
                                        if "오류:" not in translated and translated.strip() and translated.strip() != original_text_stripped:
                                            elem.text = translated; num_translated_in_chart += 1
                                            log_msg_detail = f"    차트 요소 번역됨 (태그: {elem.tag}): '{original_text_stripped}' -> '{translated}'"
                                            if log_func: log_func(log_msg_detail)
                                        elif "오류:" in translated:
                                            log_msg_err = f"    차트 요소 번역 오류 (태그: {elem.tag}, 원본: '{original_text_stripped}') -> {translated}"
                                            if log_func: log_func(log_msg_err)
                                            else: logger.warning(log_msg_err)
                        if num_translated_in_chart > 0: logger.info(f"  {chart_xml_path_in_zip} 에서 {num_translated_in_chart}개의 텍스트 요소 번역됨.")
                        else: logger.info(f"  {chart_xml_path_in_zip} 에서 번역된 텍스트 요소 없음 (또는 숫자/기호 등으로 스킵됨 / 이미 번역됨).")

                        xml_declaration_bytes = b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
                        xml_string_unicode = ET.tostring(root, encoding='unicode', method='xml') 
                        modified_charts_data[chart_xml_path_in_zip] = xml_declaration_bytes + xml_string_unicode.encode('utf-8')
                    except ET.ParseError as e_parse_apply: # ... (오류 처리 및 원본 사용)
                        modified_charts_data[chart_xml_path_in_zip] = xml_content_bytes

                    processed_charts_count +=1
                    if progress_callback_item_completed:
                        progress_info_text = f"차트 파일 '{os.path.basename(chart_xml_path_in_zip)}' ({num_translated_in_chart}개 번역됨) 적용 완료"
                        progress_callback_item_completed(
                            f"차트 {chart_xml_idx + 1}", 
                            "chart_status_applying_translation", # task_type_key (i18n)
                            weight_per_single_chart_apply, 
                            progress_info_text          
                        )

                if stop_event and stop_event.is_set(): # ... (중단 처리)
                     if log_func: log_func("차트 XML 적용 중 중단."); return None
                     return None

                with zipfile.ZipFile(output_path, 'w', zipfile.ZIP_DEFLATED) as zip_out:
                    # ... (Zip 파일 재작성 로직은 기존과 동일) ...
                    for item_name_in_zip in zip_ref.namelist():
                        if item_name_in_zip in modified_charts_data:
                            zip_out.writestr(item_name_in_zip, modified_charts_data[item_name_in_zip])
                        else:
                            zip_out.writestr(item_name_in_zip, zip_ref.read(item_name_in_zip))


            if log_func: log_func(f"\nPPTX 차트 XML 번역 완료! 최종 파일: {output_path}")
            return output_path

        except Exception as e_general: # ... (일반 오류 처리)
            if log_func: log_func(f"PPTX 차트 XML 번역 중 예기치 않은 오류: {e_general}\n{traceback.format_exc()}")
            return None
        finally: # ... (임시 디렉토리 및 로그 파일 정리)
            if os.path.exists(temp_dir_for_xml_processing):
                try: shutil.rmtree(temp_dir_for_xml_processing)
                except Exception: pass
            if f_task_log_chart_local and not f_task_log_chart_local.closed:
                try: f_task_log_chart_local.close()
                except Exception: pass

