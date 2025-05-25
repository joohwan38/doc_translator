# utils.py
import subprocess
import os
import platform
import sys
import logging
from typing import Optional, Callable, IO, Tuple, List

logger = logging.getLogger(__name__)

def check_paddleocr():
    """PaddleOCR 설치 여부를 확인합니다."""
    try:
        import paddleocr
        logger.debug("paddleocr 모듈 import 성공.")
        return True
    except ImportError as e:
        logger.warning(f"paddleocr 모듈을 찾을 수 없습니다: {e}")
        return False
    except Exception as e:
        logger.error(f"PaddleOCR 확인 중 예상치 못한 오류: {e}", exc_info=True)
        return False

def install_paddleocr():
    """PaddleOCR을 pip를 사용하여 설치합니다."""
    try:
        logger.info("PaddleOCR 자동 설치 시도 중...")
        
        # PaddlePaddle 설치 (플랫폼별 최적화)
        system = platform.system()
        if system == "Windows":
            paddle_package = "paddlepaddle"
        elif system == "Darwin":  # macOS
            paddle_package = "paddlepaddle"
        else:  # Linux
            # GPU 지원 여부 확인
            try:
                import torch
                if torch.cuda.is_available():
                    paddle_package = "paddlepaddle-gpu"
                else:
                    paddle_package = "paddlepaddle"
            except ImportError:
                paddle_package = "paddlepaddle"
        
        # 패키지 설치
        subprocess.check_call([sys.executable, "-m", "pip", "install", paddle_package], 
                            stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
        subprocess.check_call([sys.executable, "-m", "pip", "install", "paddleocr"], 
                            stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
        
        logger.info("PaddleOCR 설치 성공.")
        return True
    except subprocess.CalledProcessError as e:
        logger.error(f"PaddleOCR 설치 실패 (pip 오류): {e}")
        return False
    except Exception as e:
        logger.error(f"PaddleOCR 설치 중 예기치 않은 오류: {e}", exc_info=True)
        return False

def open_folder(path: str):
    """주어진 경로의 폴더를 엽니다."""
    try:
        if not os.path.isdir(path):
            path = os.path.dirname(path)
            if not os.path.isdir(path):
                logger.warning(f"폴더 열기 실패: 유효한 디렉토리 경로가 아님 - {path}")
                return
                
        logger.info(f"폴더 열기: {path}")
        
        system = platform.system()
        if system == "Windows":
            os.startfile(path)
        elif system == "Darwin":  # macOS
            subprocess.Popen(["open", path])
        else:  # Linux
            # 다양한 파일 관리자 시도
            file_managers = ['xdg-open', 'gnome-open', 'kde-open', 'nautilus', 'dolphin']
            for fm in file_managers:
                try:
                    subprocess.Popen([fm, path])
                    break
                except FileNotFoundError:
                    continue
                    
    except Exception as e:
        logger.error(f"폴더 열기 중 오류: {e}", exc_info=True)

def setup_task_logging(task_log_filepath: str,
                       initial_message_lines: Optional[List[str]] = None
                       ) -> Tuple[Optional[IO[str]], Optional[Callable[[str], None]]]:
    """
    작업별 로그 파일을 설정하고 로깅 함수를 반환합니다.

    Args:
        task_log_filepath: 작업 로그 파일의 전체 경로.
        initial_message_lines: 로그 파일 생성 시 초기에 기록할 메시지 목록.

    Returns:
        Tuple (파일 객체, 로그 함수). 파일 열기 실패 시 (None, None).
    """
    f_task_log = None
    log_func = None
    
    try:
        # 디렉토리 생성
        log_dir = os.path.dirname(task_log_filepath)
        if log_dir and not os.path.exists(log_dir):
            os.makedirs(log_dir, exist_ok=True)

        # 로그 파일 열기
        f_task_log = open(task_log_filepath, 'a', encoding='utf-8', buffering=1)  # 라인 버퍼링
        
        # 초기 메시지 작성
        if initial_message_lines:
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            f_task_log.write(f"\n=== Task Log Started at {timestamp} ===\n")
            for line in initial_message_lines:
                f_task_log.write(f"{line}\n")
            f_task_log.write("=" * 50 + "\n")
            f_task_log.flush()

        def write_log(message: str):
            """스레드 안전한 로그 작성 함수"""
            if f_task_log and not f_task_log.closed:
                try:
                    timestamp = datetime.now().strftime("%H:%M:%S")
                    f_task_log.write(f"[{timestamp}] {message}\n")
                    f_task_log.flush()
                except Exception as e:
                    logger.error(f"로그 작성 중 오류: {e}")
                    
        log_func = write_log
        logger.info(f"작업 로그 파일 설정 완료: {task_log_filepath}")

    except Exception as e:
        logger.error(f"작업 로그 파일 ({task_log_filepath}) 열기/설정 실패: {e}")
        if f_task_log:
            try:
                f_task_log.close()
            except Exception:
                pass
        f_task_log = None
        log_func = None

    return f_task_log, log_func

def safe_file_operation(operation: Callable, *args, **kwargs) -> Tuple[bool, Optional[str]]:
    """
    파일 작업을 안전하게 수행하는 헬퍼 함수
    
    Returns:
        Tuple (성공 여부, 오류 메시지)
    """
    try:
        operation(*args, **kwargs)
        return True, None
    except PermissionError:
        return False, "권한 없음"
    except FileNotFoundError:
        return False, "파일을 찾을 수 없음"
    except Exception as e:
        return False, str(e)

def get_file_size_mb(filepath: str) -> float:
    """파일 크기를 MB 단위로 반환"""
    try:
        size_bytes = os.path.getsize(filepath)
        return size_bytes / (1024 * 1024)
    except Exception:
        return 0.0

def ensure_directory_exists(directory: str) -> bool:
    """디렉토리가 존재하는지 확인하고 없으면 생성"""
    try:
        os.makedirs(directory, exist_ok=True)
        return True
    except Exception as e:
        logger.error(f"디렉토리 생성 실패 ({directory}): {e}")
        return False

# datetime import 추가
from datetime import datetime