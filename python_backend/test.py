def translate_presentation_stage1(self, prs: Presentation, src_lang_ui_name: str, tgt_lang_ui_name: str,
                                      translator: AbsTranslator, ocr_handler: Optional[AbsOcrHandler],
                                      model_name: str, ollama_service: AbsOllamaService,
                                      font_code_for_render: str, task_log_filepath: str,
                                      progress_callback_item_completed: Optional[Callable[[Any, str, float, str], None]] = None,
                                      stop_event: Optional[Any] = None,
                                      image_translation_enabled: bool = True,
                                      ocr_temperature: Optional[float] = None
                                      ) -> bool:
    initial_log_lines_s1 = ["--- 1단계: 차트 외 요소 번역 시작 (PptxHandler - Stage 1) ---"]
    f_task_log_s1: Optional[IO[str]] = None
    log_func_s1: Optional[Callable[[str], None]] = None

    if task_log_filepath:
        f_task_log_s1, log_func_s1_temp = setup_task_logging(task_log_filepath, initial_log_lines_s1)
        if log_func_s1_temp:
            log_func_s1 = log_func_s1_temp
    
    main_log_prefix = "PPTX Stage 1:"
    logger.info(f"{main_log_prefix} Starting text and image (OCR) content collection.")
    if log_func_s1: log_func_s1("1단계: 텍스트 및 이미지(OCR) 내용 수집 중...")

    translation_jobs: List[TranslationJob] = []
    original_paragraph_styles_map: Dict[Tuple[int, Any, Any], List[Dict[str, Any]]] = {}

    try: 
        for slide_idx, slide in enumerate(prs.slides):
            if stop_event and stop_event.is_set():
                logger.info(f"{main_log_prefix} Stop event detected during slide iteration for job collection.")
                if f_task_log_s1 and not f_task_log_s1.closed: f_task_log_s1.close()
                return False
            for shape_idx, shape in enumerate(slide.shapes): 
                if stop_event and stop_event.is_set(): 
                    logger.info(f"{main_log_prefix} Stop event detected during shape iteration for job collection.")
                    if f_task_log_s1 and not f_task_log_s1.closed: f_task_log_s1.close()
                    return False

                shape_id_for_key = getattr(shape, 'shape_id', f"s{slide_idx}_auto_idx{shape_idx}") 
                element_name_for_log = shape.name or f"Slide{slide_idx+1}_Shape{shape_idx}(ID:{shape_id_for_key})"
                item_base_context = {'slide_idx': slide_idx, 'shape_obj_ref': shape, 'name': element_name_for_log, 'shape_id_log': shape_id_for_key}
                
                if shape.shape_type == MSO_SHAPE_TYPE.CHART:
                    continue

                if shape.has_text_frame and getattr(shape.text_frame, 'text', None) and shape.text_frame.text.strip():
                    original_text = shape.text_frame.text
                    if not should_skip_translation(original_text):
                        style_key = (slide_idx, shape_id_for_key, 'shape_text')
                        translation_jobs.append({
                            'original_text': original_text,
                            'context': {**item_base_context, 'item_type_internal': 'text_shape', 'style_unique_key': style_key},
                            'is_ocr': False, 'char_count': len(original_text)
                        })
                elif shape.has_table:
                    for r_idx, row in enumerate(shape.table.rows):
                        for c_idx, cell in enumerate(row.cells):
                            if getattr(cell.text_frame, 'text', None) and cell.text_frame.text.strip():
                                original_text = cell.text_frame.text
                                if not should_skip_translation(original_text):
                                    style_key = (slide_idx, shape_id_for_key, ('table_cell', r_idx, c_idx))
                                    cell_log_name = f"{element_name_for_log}_R{r_idx}C{c_idx}"
                                    translation_jobs.append({
                                        'original_text': original_text,
                                        'context': {**item_base_context, 'name': cell_log_name, 
                                                    'item_type_internal': 'table_cell', 'row_idx': r_idx, 
                                                    'col_idx': c_idx, 'style_unique_key': style_key},
                                        'is_ocr': False, 'char_count': len(original_text)
                                    })
        
        if log_func_s1: log_func_s1(f"1단계 텍스트/표 번역 작업 수집 완료: {len(translation_jobs)}개 항목.")
        
        if not translation_jobs and not (image_translation_enabled and ocr_handler):
            is_any_chart_present = any(s.shape_type == MSO_SHAPE_TYPE.CHART for slide_obj_check in prs.slides for s in slide_obj_check.shapes)
            if not is_any_chart_present:
                if log_func_s1: log_func_s1("1단계: 처리 대상(텍스트/표/OCR/차트) 없음. 건너뜀.")
                if f_task_log_s1 and not f_task_log_s1.closed: f_task_log_s1.close()
                return True

        texts_for_batch_translation = [job['original_text'] for job in translation_jobs if not job['is_ocr']]
        translated_texts_batch: List[str] = []

        if texts_for_batch_translation:
            if log_func_s1: log_func_s1(f"일반 텍스트 {len(texts_for_batch_translation)}개 일괄 번역 시작...")
            translated_texts_batch = translator.translate_texts_batch(
                texts_for_batch_translation, src_lang_ui_name, tgt_lang_ui_name,
                model_name, ollama_service, is_ocr_text=False, stop_event=stop_event
            )
            if stop_event and stop_event.is_set():
                if f_task_log_s1 and not f_task_log_s1.closed: f_task_log_s1.close()
                return False
            if len(texts_for_batch_translation) != len(translated_texts_batch):
                logger.warning(f"{main_log_prefix} 텍스트 수와 번역 결과 수 불일치!")
                if f_task_log_s1 and not f_task_log_s1.closed: f_task_log_s1.close()
                return False
        
        current_batch_text_idx = 0
        for job_data in translation_jobs:
            if stop_event and stop_event.is_set(): break
            if job_data['is_ocr']: continue

            context = job_data['context']
            slide_idx_job = context['slide_idx']
            item_name_for_log_job = context['name']
            item_type_internal_job = context['item_type_internal']
            ui_feedback_task_type = "텍스트/표 적용"
            if item_type_internal_job == 'text_shape': ui_feedback_task_type = "텍스트 요소 적용"
            elif item_type_internal_job == 'table_cell': ui_feedback_task_type = "표 셀 내용 적용"
            
            text_frame_to_process: Optional[Any] = None
            if item_type_internal_job == 'text_shape':
                shape_obj_ref = context.get('shape_obj_ref')
                if shape_obj_ref and shape_obj_ref.has_text_frame:
                    text_frame_to_process = shape_obj_ref.text_frame
            elif item_type_internal_job == 'table_cell':
                table_shape_obj_ref = context.get('shape_obj_ref') 
                r_idx_job, c_idx_job = context['row_idx'], context['col_idx']
                if table_shape_obj_ref and table_shape_obj_ref.has_table:
                    try:
                        text_frame_to_process = table_shape_obj_ref.table.cell(r_idx_job, c_idx_job).text_frame
                    except IndexError:
                        logger.error(f"{main_log_prefix} 테이블 셀 접근 오류: '{item_name_for_log_job}' at R{r_idx_job}C{c_idx_job}.")
                        if progress_callback_item_completed:
                            progress_callback_item_completed(f"슬라이드 {slide_idx_job + 1} 오류", "테이블 셀 접근 실패", float(job_data['char_count'] * config.WEIGHT_TEXT_CHAR), f"셀 ({r_idx_job},{c_idx_job})")
                        continue 

            if text_frame_to_process:
                style_key_job = context['style_unique_key']
                if style_key_job not in original_paragraph_styles_map:
                    collected_para_styles: List[Dict[str, Any]] = []
                    try:
                        for para in text_frame_to_process.paragraphs:
                            para_default_style = self._get_style_properties(para.font)
                            runs_info_list: List[Dict[str, Any]] = []
                            if para.runs:
                                for run in para.runs:
                                    runs_info_list.append({'text': run.text, 'style': self._get_text_style(run)})
                            elif para.text and para.text.strip(): 
                                 runs_info_list.append({'text': para.text, 'style': self._get_text_style(para.runs[0] if para.runs else para)}) 
                            collected_para_styles.append({
                                'runs': runs_info_list,
                                'alignment': para.alignment, 'level': para.level,
                                'space_before': para.space_before, 'space_after': para.space_after,
                                'line_spacing': para.line_spacing,
                                'paragraph_default_run_style': para_default_style
                            })
                        original_paragraph_styles_map[style_key_job] = collected_para_styles
                    except Exception as e_style_collect:
                         logger.error(f"{main_log_prefix} Error collecting styles for '{item_name_for_log_job}': {e_style_collect}", exc_info=True)
                         original_paragraph_styles_map[style_key_job] = [] 

                translated_text_for_job = translated_texts_batch[current_batch_text_idx] if current_batch_text_idx < len(translated_texts_batch) else job_data['original_text']
                current_batch_text_idx +=1
                
                if "오류:" not in translated_text_for_job: 
                    self._apply_translated_text_to_frame(
                        text_frame_to_process,
                        translated_text_for_job,
                        original_paragraph_styles_map.get(style_key_job, []), 
                        item_name_for_log_job,
                        log_func_s1
                    )
            
            if progress_callback_item_completed and not (stop_event and stop_event.is_set()):
                progress_text_snippet = translated_text_for_job.strip().replace(chr(10), ' ')[:30] if "오류:" not in translated_text_for_job else job_data['original_text'].strip().replace(chr(10), ' ')[:30]
                progress_callback_item_completed(
                    f"슬라이드 {slide_idx_job + 1}",
                    ui_feedback_task_type,
                    float(job_data['char_count'] * config.WEIGHT_TEXT_CHAR), 
                    progress_text_snippet
                )
        
        if stop_event and stop_event.is_set(): # 일괄 번역 적용 후 중단 체크
            if f_task_log_s1 and not f_task_log_s1.closed: f_task_log_s1.close()
            return False

        if image_translation_enabled and ocr_handler:
            if log_func_s1: log_func_s1("\n--- 이미지 OCR 및 번역 적용 시작 (슬라이드별) ---")
            logger.info(f"{main_log_prefix} Starting OCR and translation for images (per slide).")

            for slide_idx_ocr, current_slide_for_ocr in enumerate(prs.slides):
                if stop_event and stop_event.is_set():
                    if f_task_log_s1 and not f_task_log_s1.closed: f_task_log_s1.close()
                    return False
                
                if log_func_s1:
                    log_func_s1(f"  [S{slide_idx_ocr+1}] 이미지 OCR 분석 중. 총 도형 수: {len(current_slide_for_ocr.shapes)}")
                    for shape_idx_debug, shape_debug in enumerate(current_slide_for_ocr.shapes):
                        try:
                            shape_type_str = shape_debug.shape_type.name if hasattr(shape_debug.shape_type, 'name') else str(shape_debug.shape_type)
                            log_func_s1(f"    도형 {shape_idx_debug+1}: 타입='{shape_type_str}', 이름='{shape_debug.name if hasattr(shape_debug, 'name') else 'N/A'}'")
                        except Exception:
                            log_func_s1(f"    도형 {shape_idx_debug+1}: 타입 정보 읽기 오류")
                
                picture_shapes_on_slide = [
                    s for s in current_slide_for_ocr.shapes
                    if hasattr(s, 'shape_type') and s.shape_type == MSO_SHAPE_TYPE.PICTURE
                ]

                if not picture_shapes_on_slide:
                    if log_func_s1: log_func_s1(f"  [S{slide_idx_ocr+1}] OCR 대상 이미지 없음.")
                else:
                    if log_func_s1: log_func_s1(f"  [S{slide_idx_ocr+1}] {len(picture_shapes_on_slide)}개의 OCR 대상 이미지 발견.")

                    for shape_ocr_idx, shape_ocr in enumerate(picture_shapes_on_slide):
                        if stop_event and stop_event.is_set():
                            if f_task_log_s1 and not f_task_log_s1.closed: f_task_log_s1.close()
                            return False

                        item_name_ocr_log = shape_ocr.name or f"Slide{slide_idx_ocr+1}_Image{shape_ocr_idx+1}"
                        ocr_feedback_location = f"슬라이드 {slide_idx_ocr + 1}"
                        ocr_item_total_weight = float(config.WEIGHT_IMAGE)
                        current_ocr_item_processed_weight = 0.0
                        
                        if log_func_s1: log_func_s1(f"\n  [S{slide_idx_ocr+1}] OCR 처리 시도: '{item_name_ocr_log}'")
                        
                        try:
                            img_bytes = shape_ocr.image.blob
                            img_pil_original = Image.open(io.BytesIO(img_bytes))
                            img_pil_rgb = img_pil_original.convert("RGB")
                            original_img_format = img_pil_original.format
                            
                            ocr_detection_weight_portion = ocr_item_total_weight * 0.2
                            if progress_callback_item_completed and not (stop_event and stop_event.is_set()):
                                progress_callback_item_completed(ocr_feedback_location, "ocr_status_detection_start", 0, f"'{item_name_ocr_log}' 감지 시작")

                            ocr_results = ocr_handler.ocr_image(img_pil_rgb)
                            if stop_event and stop_event.is_set():
                                if f_task_log_s1 and not f_task_log_s1.closed: f_task_log_s1.close()
                                return False
                            
                            ocr_result_count = len(ocr_results) if ocr_results else 0
                            if progress_callback_item_completed and not (stop_event and stop_event.is_set()):
                                progress_callback_item_completed(
                                    ocr_feedback_location, 
                                    "ocr_status_detection_complete" if ocr_results else "ocr_status_detection_no_text",
                                    ocr_detection_weight_portion, 
                                    f"'{item_name_ocr_log}' ({ocr_result_count} 블록)"
                                )
                            current_ocr_item_processed_weight += ocr_detection_weight_portion
                            if log_func_s1: log_func_s1(f"        '{item_name_ocr_log}' OCR 분석 완료. {ocr_result_count}개 블록 발견.")

                            ocr_texts_for_translation: List[str] = []
                            ocr_contexts_for_render: List[Dict[str, Any]] = []
                            if ocr_results:
                                for res_item in ocr_results:
                                    if not (isinstance(res_item, (list, tuple)) and len(res_item) >= 2): continue
                                    box_coords, text_conf_pair = res_item[0], res_item[1]
                                    text_angle = res_item[2] if len(res_item) > 2 else None
                                    if not (isinstance(text_conf_pair, (list, tuple)) and len(text_conf_pair) == 2): continue
                                    original_ocr_text, _ = text_conf_pair
                                    if is_ocr_text_valid(original_ocr_text) and not should_skip_translation(original_ocr_text):
                                        ocr_texts_for_translation.append(original_ocr_text)
                                        ocr_contexts_for_render.append({'box': box_coords, 'original_text': original_ocr_text, 'angle': text_angle})
                            
                            if ocr_texts_for_translation:
                                ocr_translation_weight_portion = ocr_item_total_weight * 0.4
                                if progress_callback_item_completed and not (stop_event and stop_event.is_set()):
                                     progress_callback_item_completed(ocr_feedback_location, "ocr_status_translating_texts_start", 0, f"'{item_name_ocr_log}' ({len(ocr_texts_for_translation)}개) 번역 시작")
                                
                                translated_ocr_texts = translator.translate_texts_batch(
                                    ocr_texts_for_translation, src_lang_ui_name, tgt_lang_ui_name,
                                    model_name, ollama_service, is_ocr_text=True,
                                    ocr_temperature=ocr_temperature, stop_event=stop_event
                                )
                                if stop_event and stop_event.is_set():
                                    if f_task_log_s1 and not f_task_log_s1.closed: f_task_log_s1.close()
                                    return False
                                
                                if progress_callback_item_completed and not (stop_event and stop_event.is_set()):
                                    progress_callback_item_completed(
                                        ocr_feedback_location, "ocr_status_translating_texts_complete",
                                        ocr_translation_weight_portion, f"'{item_name_ocr_log}' ({len(translated_ocr_texts)}개 결과)"
                                    )
                                current_ocr_item_processed_weight += ocr_translation_weight_portion

                                if len(ocr_texts_for_translation) == len(translated_ocr_texts):
                                    img_to_render_on = img_pil_original.copy()
                                    any_text_rendered_on_image = False
                                    ocr_rendering_total_alloc_weight = ocr_item_total_weight * 0.4
                                    
                                    if translated_ocr_texts:
                                        render_per_block_weight = ocr_rendering_total_alloc_weight / len(translated_ocr_texts) if translated_ocr_texts else 0
                                        for i, translated_text_render in enumerate(translated_ocr_texts):
                                            if stop_event and stop_event.is_set(): break
                                            render_ctx = ocr_contexts_for_render[i]
                                            if "오류:" not in translated_text_render and translated_text_render.strip():
                                                try:
                                                    img_to_render_on = ocr_handler.render_translated_text_on_image(
                                                        img_to_render_on, render_ctx['box'], translated_text_render,
                                                        font_code_for_render, render_ctx['original_text'], render_ctx['angle']
                                                    )
                                                    any_text_rendered_on_image = True
                                                except Exception as e_render:
                                                    if log_func_s1: log_func_s1(f"              오류: '{item_name_ocr_log}' 텍스트 렌더링 실패: {e_render}")
                                            
                                            if progress_callback_item_completed and not (stop_event and stop_event.is_set()):
                                                progress_callback_item_completed(
                                                    ocr_feedback_location, "ocr_status_rendering_text",
                                                    render_per_block_weight, f"'{item_name_ocr_log}' (블록 {i+1}/{len(translated_ocr_texts)})"
                                                )
                                        if not (stop_event and stop_event.is_set()): # 중단되지 않았을 때만 남은 가중치 처리
                                            current_ocr_item_processed_weight += (render_per_block_weight * len(translated_ocr_texts)) # 실제 처리된 가중치
                                    elif ocr_rendering_total_alloc_weight > 0 :
                                         if progress_callback_item_completed and not (stop_event and stop_event.is_set()):
                                            progress_callback_item_completed(ocr_feedback_location, "ocr_status_rendering_complete_no_text", ocr_rendering_total_alloc_weight, f"'{item_name_ocr_log}'")
                                         current_ocr_item_processed_weight += ocr_rendering_total_alloc_weight
                                    
                                    if stop_event and stop_event.is_set(): break
                                    
                                    if any_text_rendered_on_image:
                                        output_img_stream = io.BytesIO()
                                        save_format = original_img_format if original_img_format and original_img_format.upper() in ['JPEG', 'PNG', 'GIF', 'BMP', 'TIFF'] else 'PNG'
                                        img_to_render_on.save(output_img_stream, format=save_format)
                                        output_img_stream.seek(0)
                                        left, top, width, height = shape_ocr.left, shape_ocr.top, shape_ocr.width, shape_ocr.height
                                        name_orig_img = shape_ocr.name
                                        sp_xml_elem = shape_ocr.element
                                        parent_xml_elem = sp_xml_elem.getparent()
                                        if parent_xml_elem is not None:
                                            parent_xml_elem.remove(sp_xml_elem)
                                            new_pic_shape = current_slide_for_ocr.shapes.add_picture(
                                                output_img_stream, left, top, width=width, height=height
                                            )
                                            if name_orig_img: new_pic_shape.name = name_orig_img
                                            if log_func_s1: log_func_s1(f"        이미지 '{item_name_ocr_log}' (S{slide_idx_ocr+1}) 성공적으로 교체됨.")
                                else:
                                    if log_func_s1: log_func_s1(f"        경고: '{item_name_ocr_log}' OCR 텍스트 수 불일치. 이미지 변경 없음.")
                            
                            if not ocr_texts_for_translation and not (stop_event and stop_event.is_set()): # 번역할 텍스트가 처음부터 없었고 중단되지 않은 경우
                                remaining_weight_after_detection = ocr_item_total_weight - current_ocr_item_processed_weight
                                if remaining_weight_after_detection > 0.01 and progress_callback_item_completed:
                                    progress_callback_item_completed(
                                        ocr_feedback_location, "ocr_status_processing_complete_no_translation_needed",
                                        remaining_weight_after_detection, f"'{item_name_ocr_log}'"
                                    )
                        except Exception as e_ocr_img_proc:
                            logger.error(f"{main_log_prefix} 이미지 OCR 처리 중 예외 '{item_name_ocr_log}': {e_ocr_img_proc}", exc_info=True)
                            if log_func_s1: log_func_s1(f"      오류: '{item_name_ocr_log}' 이미지 OCR 처리 중 예외: {e_ocr_img_proc}. 건너뜀.")
                            error_weight_to_report = ocr_item_total_weight - current_ocr_item_processed_weight
                            if error_weight_to_report > 0.01 and progress_callback_item_completed and not (stop_event and stop_event.is_set()):
                                progress_callback_item_completed(ocr_feedback_location, "ocr_status_processing_error", error_weight_to_report, f"'{item_name_ocr_log}' 오류")
            
            elif image_translation_enabled and not ocr_handler : 
                if log_func_s1: log_func_s1("이미지 번역 활성화되었으나 OCR 핸들러 없어 건너뜁니다.")
        
        except Exception as e_stage1_main: 
            logger.error(f"{main_log_prefix} 1단계 처리 중 심각한 오류: {e_stage1_main}", exc_info=True)
            if log_func_s1: log_func_s1(f"!!! 1단계 처리 중 심각한 오류: {e_stage1_main}\n{traceback.format_exc()}")
            if f_task_log_s1 and not f_task_log_s1.closed: f_task_log_s1.close()
            return False 

        if stop_event and stop_event.is_set():
            if f_task_log_s1 and not f_task_log_s1.closed: f_task_log_s1.close()
            return False

        logger.info(f"{main_log_prefix} 1단계 (텍스트 및 이미지 번역) 성공적 완료.")
        if log_func_s1: log_func_s1(f"--- 1단계: 차트 외 요소 번역 성공적으로 완료 ---\n")
        
        if f_task_log_s1 and not f_task_log_s1.closed:
            try: f_task_log_s1.close()
            except Exception as e_close_log: logger.warning(f"{main_log_prefix} 1단계 작업 로그 파일 닫기 실패: {e_close_log}")
        
        return True