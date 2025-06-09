# python_backend/excel_handler.py
import openpyxl
from openpyxl.cell import Cell
from openpyxl.styles import Font, PatternFill, Border, Alignment, Side, Protection
from copy import copy
import logging
import traceback
from typing import Dict, Any, Optional, Callable, List

from interfaces import AbsExcelProcessor, AbsTranslator, AbsOllamaService
import config


logger = logging.getLogger(__name__)

def copy_cell_style(source_cell: Cell, target_cell: Cell):
    """openpyxl 셀 스타일을 복사합니다."""
    if source_cell.has_style:
        target_cell.font = copy(source_cell.font)
        target_cell.border = copy(source_cell.border)
        target_cell.fill = copy(source_cell.fill)
        target_cell.number_format = source_cell.number_format
        target_cell.protection = copy(source_cell.protection)
        target_cell.alignment = copy(source_cell.alignment)

class ExcelHandler(AbsExcelProcessor):
    def get_file_info(self, file_path: str) -> Dict[str, Any]:
        """엑셀 파일의 정보를 분석하여 반환합니다."""
        print(f"[DEBUG] excel_handler.py: get_file_info 진입. 파일 경로: {file_path}") # [!INFO] 디버깅 print 1
        info = {
            "sheet_count": 0,
            "translatable_cell_count": 0,
            "total_text_char_count": 0,
            "error": None
        }
        try:
            print("[DEBUG] excel_handler.py: openpyxl.load_workbook 호출 직전") # [!INFO] 디버깅 print 2
            workbook = openpyxl.load_workbook(file_path, read_only=True)
            print("[DEBUG] excel_handler.py: 워크북 로드 성공") # [!INFO] 디버깅 print 3

            info["sheet_count"] = len(workbook.sheetnames)

            total_chars = 0
            cell_count = 0

            for sheet_name in workbook.sheetnames:
                sheet = workbook[sheet_name]
                for row in sheet.iter_rows():
                    for cell in row:
                        if cell.value and isinstance(cell.value, str) and cell.value.strip():
                            total_chars += len(cell.value)
                            cell_count += 1

            info["translatable_cell_count"] = cell_count
            info["total_text_char_count"] = total_chars
            print(f"[DEBUG] excel_handler.py: 분석 완료. 시트: {info['sheet_count']}, 셀: {cell_count}, 문자수: {total_chars}") # [!INFO] 디버깅 print 4

        except Exception as e:
            print(f"[DEBUG] excel_handler.py: get_file_info의 try-except 블록에서 오류 발생!") # [!INFO] 디버깅 print 5
            print("--- TRACEBACK START ---")
            traceback.print_exc()
            print("--- TRACEBACK END ---")

            logger.error(f"엑셀 파일 정보 분석 중 오류: {e}", exc_info=True)
            info["error"] = str(e)

        return info

    def translate_workbook(self, file_path: str, output_path: str, translator: AbsTranslator,
                           src_lang_ui_name: str, tgt_lang_ui_name: str, model_name: str,
                           ollama_service: 'AbsOllamaService',
                           progress_callback: Optional[Callable[[Any, str, float, str], None]] = None,
                           stop_event: Optional[Any] = None) -> Optional[str]:
        """워크북의 모든 텍스트를 번역하고 새 파일로 저장합니다."""
        try:
            original_workbook = openpyxl.load_workbook(file_path)
            
            # 1. 번역할 텍스트 추출
            texts_to_translate = []
            cell_map = [] # (sheet_name, cell_coordinate)
            for sheet_name in original_workbook.sheetnames:
                sheet = original_workbook[sheet_name]
                for row in sheet.iter_rows():
                    for cell in row:
                        if cell.value and isinstance(cell.value, str) and cell.value.strip():
                            texts_to_translate.append(cell.value)
                            cell_map.append((sheet_name, cell.coordinate))

            if not texts_to_translate:
                logger.info("엑셀 파일에 번역할 텍스트가 없습니다. 원본을 복사합니다.")
                original_workbook.save(output_path)
                return output_path

            # 2. 텍스트 일괄 번역 (기존 번역기 재사용)
            if progress_callback:
                progress_callback("Excel", "Translating Cells", 0, f"Translating {len(texts_to_translate)} cells...")

            translated_texts = translator.translate_texts_batch(
                texts_to_translate, src_lang_ui_name, tgt_lang_ui_name,
                model_name, ollama_service, stop_event=stop_event
            )

            if stop_event and stop_event.is_set():
                logger.info("엑셀 번역 작업이 중단되었습니다.")
                # 중단 시 현재까지 번역된 내용으로 저장 시도
                translated_map = {cell_map[i]: translated_texts[i] for i in range(len(translated_texts))}
                self._write_translated_data(original_workbook, translated_map, output_path, stop_event)
                return output_path

            if len(texts_to_translate) != len(translated_texts):
                logger.error("원본 텍스트와 번역된 텍스트의 개수가 일치하지 않습니다.")
                return None

            translated_map = {cell_map[i]: translated_texts[i] for i in range(len(translated_texts))}

            # 3. 번역된 내용을 새 워크북에 쓰기
            self._write_translated_data(original_workbook, translated_map, output_path, stop_event, progress_callback, len(texts_to_translate))

            return output_path

        except Exception as e:
            logger.error(f"엑셀 파일 번역 중 오류: {e}", exc_info=True)
            return None

    def _write_translated_data(self, workbook, translated_map, output_path, stop_event, progress_callback=None, total_cells=1):
        """번역된 데이터를 워크북에 기록합니다."""
        processed_cells = 0
        weight_per_cell = config.WEIGHT_EXCEL_CELL if hasattr(config, 'WEIGHT_EXCEL_CELL') else 1

        for sheet_name in workbook.sheetnames:
            if stop_event and stop_event.is_set(): break
            sheet = workbook[sheet_name]
            for row in sheet.iter_rows():
                if stop_event and stop_event.is_set(): break
                for cell in row:
                    if (sheet_name, cell.coordinate) in translated_map:
                        translated_text = translated_map[(sheet_name, cell.coordinate)]
                        if "오류:" not in translated_text:
                            cell.value = translated_text
                        
                        processed_cells += 1
                        if progress_callback:
                            progress_callback(
                                f"Sheet: {sheet_name}", 
                                "Applying translation",
                                weight_per_cell,
                                f"Cell {cell.coordinate}"
                            )
        
        workbook.save(output_path)
        logger.info(f"번역된 엑셀 파일 저장 완료: {output_path}")