# python_backend/excel_handler.py
import openpyxl
import os
from openpyxl.cell import Cell
from openpyxl.styles import Font, PatternFill, Border, Alignment, Side, Protection
from copy import copy
import logging
import shutil
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
            workbook = openpyxl.load_workbook(file_path, read_only=True, data_only=True, keep_vba=False)
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
        temp_input_path = None
        try:
            # 1. 원본 파일을 임시 파일로 복사하여 작업 (읽기/쓰기 동시 수행 위함)
            temp_input_path = os.path.join(os.path.dirname(output_path), f"temp_{os.path.basename(file_path)}")
            shutil.copy2(file_path, temp_input_path)

            workbook = openpyxl.load_workbook(temp_input_path)
            
            # 2. 번역할 텍스트 추출 (병합 셀 고려)
            texts_to_translate = []
            cell_map = [] # (sheet_name, cell_coordinate)
            merged_cell_ranges_map = {sheet.title: [str(merged_range) for merged_range in sheet.merged_cells.ranges] for sheet in workbook.worksheets}

            for sheet_name in workbook.sheetnames:
                sheet = workbook[sheet_name]
                merged_cells_in_sheet = set()
                for merged_range in merged_cell_ranges_map.get(sheet_name, []):
                    for row in sheet[merged_range]:
                        for cell in row:
                            merged_cells_in_sheet.add(cell.coordinate)

                for row in sheet.iter_rows():
                    for cell in row:
                        if cell.coordinate in merged_cells_in_sheet and not any(cell.coordinate in r for r in sheet.merged_cells.ranges if sheet[r.split(':')[0]].coordinate == cell.coordinate):
                            continue # 병합된 셀의 첫번째 셀이 아니면 건너뛰기

                        if cell.value and isinstance(cell.value, str) and cell.value.strip():
                            texts_to_translate.append(cell.value)
                            cell_map.append((sheet_name, cell.coordinate))

            if not texts_to_translate:
                logger.info("엑셀 파일에 번역할 텍스트가 없습니다. 원본을 복사합니다.")
                shutil.copy2(file_path, output_path)
                return output_path

            # 3. 텍스트 일괄 번역
            if progress_callback:
                progress_callback("Excel", "status_task_translating_cells", 0, f"Translating {len(texts_to_translate)} cells...")

            translated_texts = translator.translate_texts(
                texts_to_translate, src_lang_ui_name, tgt_lang_ui_name,
                model_name, ollama_service, stop_event=stop_event,
                progress_callback=progress_callback,
                base_location_key="status_key_excel_file",
                base_task_key="status_task_translating_cells"
            )

            if stop_event and stop_event.is_set():
                logger.info("엑셀 번역 작업이 중단되었습니다.")
                # 중단 시 현재까지 번역된 내용으로 저장 시도
                # ... (중단 시 저장 로직은 아래에서 통합)

            if len(texts_to_translate) != len(translated_texts):
                logger.error("원본 텍스트와 번역된 텍스트의 개수가 일치하지 않습니다.")
                return None

            translated_map = {cell_map[i]: translated_texts[i] for i in range(len(translated_texts))}

            # 4. 번역된 내용을 워크북에 직접 쓰기
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
                            
                            if progress_callback:
                                progress_callback(
                                    f"Sheet: {sheet_name}", 
                                    "Applying translation",
                                    weight_per_cell,
                                    f"Cell {cell.coordinate}"
                                )
            
            workbook.save(output_path)
            logger.info(f"번역된 엑셀 파일 저장 완료: {output_path}")
            return output_path

        except KeyError as ke:
            # 'xl/drawings/NULL'과 같은 특정 KeyError를 처리
            if "xl/drawings/NULL" in str(ke):
                logger.error(f"엑셀 파일 번역 중 오류: 파일 내 드로잉 요소 문제로 로드 실패. 다른 엑셀 파일을 시도하거나, 파일에서 드로잉 요소를 제거 후 다시 시도해주세요. 오류: {ke}", exc_info=True)
            else:
                logger.error(f"엑셀 파일 번역 중 알 수 없는 KeyError 발생: {ke}", exc_info=True)
            return None
        finally:
            if temp_input_path and os.path.exists(temp_input_path):
                try:
                    os.remove(temp_input_path)
                except OSError as e:
                    logger.warning(f"임시 엑셀 파일 삭제 실패: {e}")

    