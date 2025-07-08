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
                                 task_log_filepath: Optional[str] = None) -> Optional[str>:

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

        temp_dir_for_xml_processing = tempfile.mkdtemp(prefix="chart_xml_")

        try:
            unique_texts_to_translate_all_charts: Dict[str, None] = {}
            chart_xml_contents_map: Dict[str, bytes] = {}
            
            with zipfile.ZipFile(pptx_path, 'r') as zip_ref:
                chart_files = [f for f in zip_ref.namelist() if f.startswith('ppt/charts/') and f.endswith('.xml')]
                if not chart_files:
                    if log_func: log_func("번역할 차트가 없습니다. 2단계를 건너뜁니다.")
                    # shutil.copy2(pptx_path, output_path) # 이미 1단계 결과물이 output_path에 임시 저장되어 있을 것임
                    return pptx_path # 원본(1단계 결과물) 경로를 그대로 반환

                if log_func: log_func(f"총 {len(chart_files)}개 차트 XML 발견. 텍스트 수집 중...")

                # 1. 텍스트 수집
                total_chars_in_charts = 0
                for chart_xml_path_in_zip in chart_files:
                    if stop_event and stop_event.is_set(): raise InterruptedError("Chart text collection stopped")
                    xml_content_bytes = zip_ref.read(chart_xml_path_in_zip)
                    chart_xml_contents_map[chart_xml_path_in_zip] = xml_content_bytes
                    try:
                        root = ET.fromstring(xml_content_bytes.decode('utf-8', errors='ignore'))
                        for elem in root.iter():
                            if elem.tag.endswith('}t') or elem.tag.endswith('}v'):
                                if elem.text and elem.text.strip() and not self._is_numeric_or_simple_symbols(elem.text.strip()):
                                    unique_texts_to_translate_all_charts[elem.text.strip()] = None
                                    total_chars_in_charts += len(elem.text.strip())
                    except ET.ParseError as e_parse: 
                        if log_func: log_func(f"경고: 차트 XML 파싱 오류 '{chart_xml_path_in_zip}': {e_parse}. 건너뜠니다.")

                # 2. 번역
                texts_list_for_batch = list(unique_texts_to_translate_all_charts.keys())
                translation_map: Dict[str, str] = {}
                if texts_list_for_batch:
                    if log_func: log_func(f"차트 내 고유 텍스트 {len(texts_list_for_batch)}개 일괄 번역 시작...")
                    
                    # 차트 번역에 할당된 가중치 계산 (전체 작업량의 일부)
                    # 이 부분은 web_app.py에서 file_info를 통해 계산된 전체 가중치를 기반으로 해야 더 정확함
                    # 여기서는 근사치로 캐릭터 수 기반 가중치 사용
                    chart_translation_weight = total_chars_in_charts * config.WEIGHT_TEXT_CHAR

                    translated_texts_batch = self.translator.translate_texts(
                        texts_list_for_batch, src_lang_ui_name, tgt_lang_ui_name, model_name, 
                        self.ollama_service, is_ocr_text=False, stop_event=stop_event,
                        progress_callback=progress_callback_item_completed,
                        base_location_key="status_key_chart_translation",
                        base_task_key="status_task_translating_chart_text"
                    )
                    if stop_event and stop_event.is_set(): raise InterruptedError("Chart translation stopped")
                    if len(texts_list_for_batch) != len(translated_texts_batch): raise ValueError("Chart translation result count mismatch")
                    translation_map = {original: translated for original, translated in zip(texts_list_for_batch, translated_texts_batch)}

                # 3. 번역 적용 및 파일 재조립
                modified_charts_data: Dict[str, bytes] = {}
                if log_func: log_func("번역된 차트 내용 적용 시작...")

                for chart_xml_path_in_zip in chart_files:
                    if stop_event and stop_event.is_set(): raise InterruptedError("Chart application stopped")
                    xml_content_bytes = chart_xml_contents_map[chart_xml_path_in_zip]
                    num_translated_in_chart = 0
                    try:
                        root = ET.fromstring(xml_content_bytes.decode('utf-8', errors='ignore'))
                        for elem in root.iter():
                            if elem.text and elem.text.strip() in translation_map:
                                translated = translation_map[elem.text.strip()]
                                if "오류:" not in translated and translated.strip() != elem.text.strip():
                                    elem.text = translated
                                    num_translated_in_chart += 1
                        
                        if num_translated_in_chart > 0:
                            xml_declaration = b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
                            modified_xml_bytes = xml_declaration + ET.tostring(root, encoding='utf-8')
                            modified_charts_data[chart_xml_path_in_zip] = modified_xml_bytes

                    except ET.ParseError as e_parse_apply:
                        if log_func: log_func(f"경고: 차트 XML 적용 중 파싱 오류 '{chart_xml_path_in_zip}': {e_parse_apply}. 원본 유지.")

                # 4. 최종 PPTX 파일 생성
                if log_func: log_func("최종 PPTX 파일 생성 중...")
                shutil.copy2(pptx_path, output_path) # 1단계 결과물을 최종 출력 경로로 복사
                with zipfile.ZipFile(output_path, 'a') as zip_out: # 'a' (append) 모드로 열기
                    for chart_path, modified_data in modified_charts_data.items():
                        zip_out.writestr(chart_path, modified_data)

            if log_func: log_func(f"\nPPTX 차트 XML 번역 완료! 최종 파일: {output_path}")
            return output_path

        except InterruptedError as e_interrupt:
            if log_func: log_func(f"차트 번역 작업 중단됨: {e_interrupt}")
            return None # 중단 시 None 반환
        except Exception as e_general:
            if log_func: log_func(f"PPTX 차트 XML 번역 중 예기치 않은 오류: {e_general}\n{traceback.format_exc()}")
            return None
        finally:
            if os.path.exists(temp_dir_for_xml_processing):
                shutil.rmtree(temp_dir_for_xml_processing, ignore_errors=True)
            if f_task_log_chart_local and not f_task_log_chart_local.closed:
                try: f_task_log_chart_local.close()
                except Exception: pass