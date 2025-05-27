# 파워포인트 문서 번역기

<div align="center">
  <img src="python_backend/static/LINEstudio2.png" alt="LINE Studio" width="200"/>
  <img src="assets/app_icon.png" alt="appicon" width="200"/>
  
  <h3>PowerPoint Document Translator</h3>
  <p>파워포인트 파일(.pptx)의 모든 텍스트를 AI를 활용하여 자동으로 번역하는 데스크톱 애플리케이션</p>
  
  ![Electron](https://img.shields.io/badge/Electron-191970?style=for-the-badge&logo=Electron&logoColor=white)
  ![JavaScript](https://img.shields.io/badge/javascript-%23323330.svg?style=for-the-badge&logo=javascript&logoColor=%23F7DF1E)
  ![HTML5](https://img.shields.io/badge/html5-%23E34F26.svg?style=for-the-badge&logo=html5&logoColor=white)
  ![CSS3](https://img.shields.io/badge/css3-%231572B6.svg?style=for-the-badge&logo=css3&logoColor=white)
</div>

## 📑 목차
- [개요](#-개요)
- [주요 기능](#-주요-기능)
- [설치 가이드](#-설치-가이드)
  - [MacOS 설치](#macos-설치)
  - [Windows 설치](#windows-설치)
- [사용 방법](#-사용-방법)
- [프로젝트 구조](#-프로젝트-구조)
- [기술 스택](#-기술-스택)
- [설정 옵션](#-설정-옵션)
- [문의](#-문의)

## 📋 개요
LINE Studio에서 개발한 파워포인트 문서 번역기는 Ollama AI 모델을 활용하여 .pptx 파일의 모든 텍스트 및 이미지 자동으로 번역하는 도구입니다. 텍스트 상자, 표, 도형, 차트, 이미지 등 파워포인트 내의 모든 텍스트 요소를 찾아서 번역합니다.

## ✨ 주요 기능
- **다양한 텍스트 요소 번역**: 텍스트 상자, 표, 도형, 차트, 이미지 등 모든 텍스트 번역
- **다국어 지원**: 한국어, 영어, 일본어, 중국어(번체/간체), 태국어, 스페인어 등 지원
- **언어 자동 감지**: 원본 언어를 자동으로 감지하여 번역
- **캐싱 시스템**: 동일한 텍스트 반복 번역 방지로 성능 최적화
- **실시간 진행률 표시**: 번역 진행 상황을 실시간으로 확인
- **번역 중단 기능**: 필요시 번역 프로세스 중단 가능
- **파일 저장 및 열기**: 번역된 파일 즉시 저장 및 열기 기능

## 🚀 설치 가이드

### 사전 요구사항
[Ollama](https://ollama.com/download) 설치 및 실행이 필요합니다:
```bash
# Ollama 설치 후 실행
ollama serve

# 사용할 모델 다운로드 (예: gemma3)
ollama pull gemma3
```

---

### MacOS 설치

#### 방법 1: 미리 빌드된 앱 설치하기

1. [Release 페이지](https://github.com/joohwan38/doc_translator/releases/tag/download)에서 최신 버전의 `ppt_translator.dmg` 파일을 다운로드합니다.
   - *만약 링크가 작동하지 않는다면 화면의 우측 사이드 메뉴 상단에 있으니 직접 저장소로 입장*
2. 다운로드한 DMG 파일을 더블클릭하여 마운트합니다.
3. `doctranslator.app`을 애플리케이션 폴더로 드래그합니다.
4. 최초 실행 시 인터넷에서 다운받은 앱으로 권한을 허용해야 하는지 시스템에서 물어볼 수 있습니다.
   파이썬 코드파일이므로 악성코드가 존재할 수 없기 때문에 안심하고 허용하세요.

--- 
 
### Windows 설치

#### 방법 1: 미리 빌드된 앱 실행하기 (Portable)

1. [Release 페이지](https://github.com/joohwan38/doc_translator/releases/tag/download)에서 최신 버전의 `ppt_translator.exe` 파일을 다운로드합니다.
2. 설치를 원하는 위치에 폴더를 생성하고 exe 파일을 이동합니다.
3. 폴더 내의 `doctranslator.exe` 파일을 실행합니다.
---

## 🔧 사용 방법

### 1. 애플리케이션 실행
- 애플리케이션을 실행하면 자동으로 Ollama 서버 연결 상태를 확인합니다
- Ollama가 설치되어 있지 않거나 실행되지 않은 경우 안내 메시지가 표시됩니다

### 2. 파일 업로드
- 드래그 앤 드롭 또는 '파일 선택하기' 버튼으로 .pptx 파일을 업로드합니다
- 업로드된 파일의 슬라이드 수와 텍스트 요소 개수가 표시됩니다

### 3. 번역 설정
- **원본 언어**: 원본문서의 언어 선택
- **번역 언어**: 번역이 될 언어 선택
- **Ollama 모델**: 사용할 AI 모델 선택 (추천 gemma3)

### 4. 번역 실행
- '번역 시작' 버튼을 클릭하여 번역을 시작합니다
- 진행률 바와 상태 메시지로 진행 상황을 확인할 수 있습니다
- 필요시 '번역 중지' 버튼으로 프로세스를 중단할 수 있습니다

### 5. 결과 저장
- 번역이 완료되면 '번역된 파일 다운로드' 버튼으로 파일을 저장합니다
- '결과 폴더 열기' 버튼으로 임시저장 파일이 있는 폴더를 바로 열 수 있습니다

## 💻 기술 스택
- **프론트엔드**: HTML5, CSS3, JavaScript
- **백엔드**: Electron
- **AI/ML**: Ollama (llama2, mistral 등)
- **파일 처리**: JSZip (PPTX 파일 파싱 및 생성)

## 📝 설정 옵션

### Ollama URL
기본값은 `http://localhost:11434`입니다. 

### 다국어 지원
- 한국어 (ko)
- 영어 (en)
- 일본어 (ja)
- 중국어 간체 (zh)
- 중국어 번체 (zh-TW)
- 태국어 (th)


## 📄 라이선스
이 프로젝트는 라인스튜디오 라이선스 하에 배포됩니다. 저작권자의 허가가 없는 무단 복제 사용을 금지합니다.


## 📞 버그리포트 및 문의
- Email: joohwan.son@linecorp.com
- Website: https://linestudio.com

---
<div align="center">
  Made with ❤️ by LINE Studio
</div>
