# Vitract

**유튜브·SNS 영상은 내 파일로, 영상 속 이야기는 스크립트로.**

Vitract는 링크를 입력해 영상을 다운로드하고, 제공되는 자막을 읽기 쉬운 텍스트 스크립트로 저장하는 프로젝트입니다. macOS 네이티브 앱과 브라우저에서 사용하는 웹 버전을 함께 제공합니다.

## 주요 기능

- **링크로 영상 다운로드** — 유튜브 영상·Shorts, Instagram·Threads·X·Facebook 공개 영상 지원
- **MP4 저장** — 영상과 오디오를 합치고 필요하면 H.264/AAC로 변환
- **스크립트 추출** — macOS 앱에서 제작자 자막·자동 생성 자막을 언어별로 선택해 TXT 저장
- **영상 없이 텍스트만 저장** — 스크립트를 만들기 위해 영상 전체를 내려받을 필요 없음
- **내 폴더에 보관** — 저장 위치 선택, 동일 파일명 보존, 작업 취소 지원

## 버전별 지원 범위

| 기능 | macOS 앱 | 웹 버전 |
|---|---|---|
| YouTube 영상·Shorts 다운로드 | 지원 | 지원 |
| Instagram·Threads·X·Facebook | 공개 단일 영상 지원 | 공개 단일 영상 지원 |
| 자막 → TXT 스크립트 | 지원 | 미지원 |
| 자막 없는 영상의 음성 인식 | 미지원 | 미지원 |

플랫폼의 접근 제한이나 서비스 변경에 따라 다운로드가 실패할 수 있습니다. 스크립트는 영상에 제공되는 자막을 추출하며, 자동 자막에는 오인식이 있을 수 있습니다. 새 대본을 창작하거나 번역하는 기능은 아닙니다. 자막 추출에는 유료 AI API가 필요하지 않습니다.

## 프로젝트 구성

- `macOS버전/` — SwiftUI 앱, 다운로드·자막 추출 엔진, 빌드 도구와 테스트
- `HTML버전/` — 웹 화면, Python 서버, 플랫폼별 다운로드 처리, Docker 배포 설정

이 저장소는 소스 코드 배포본입니다. 빌드된 앱, 다운로드한 영상, 사용자 작업 데이터, 인증 정보는 포함하지 않습니다. 테스트용 MP4는 로컬에서 생성한 합성 영상입니다.

## macOS 앱 빌드

Apple Silicon Mac, Xcode Command Line Tools, Python 3, Homebrew가 필요합니다. 포함하는 미디어 도구에 따라 macOS 최소 버전이 결정되며 현재 로컬 빌드는 macOS 26 이상입니다.

```sh
brew install ffmpeg node
cd macOS버전
curl -fL https://github.com/yt-dlp/yt-dlp/releases/download/2026.08.19/yt-dlp_macos -o Tools/yt-dlp
chmod +x Tools/yt-dlp
python3 build_app.py
```

빌드 도구는 저장소의 공식 체크섬과 yt-dlp 실행 파일을 비교하고 필요한 라이브러리를 묶습니다. 결과는 `build/Vitract.app`입니다. 로컬용 임시 서명을 사용하며 Apple 공증 배포본은 아닙니다.

앱에서 **링크 입력 → 영상 확인 → MP4 저장** 또는 **자막 언어 선택 → 스크립트 저장 (TXT)** 순서로 사용합니다.

```sh
python3 Tests/run_tests.py
```

스크립트 추출·파일 충돌 방지·MP4 변환·취소 등 합성 데이터 기반 백엔드 테스트 12개를 통과했습니다. 모든 외부 플랫폼의 실제 다운로드 성공을 보장하는 테스트는 아닙니다.

## 웹 버전 실행

Python 3.10 이상과 yt-dlp, FFmpeg/FFprobe, Node.js가 필요합니다.

```sh
cd HTML버전
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python3 launch.py
```

서버 실행 및 Docker/HTTPS 설정은 [웹 배포 안내](HTML버전/서버배포안내.txt)를 참고하세요. 웹 버전 일부 내부 명칭은 기존 이름인 `clipsave`를 사용합니다.

## 사용 및 외부 도구

본인 소유 또는 다운로드·복제가 허용된 콘텐츠에 사용하세요. 로그인·DRM·유료 콘텐츠 접근 제한을 우회하지 않습니다.

yt-dlp, FFmpeg, Node.js 등 외부 도구의 라이선스는 각각 적용됩니다. [외부 도구 안내](macOS버전/THIRD_PARTY_NOTICES.md)를 참고하세요. 이 저장소의 자체 코드에는 별도의 오픈소스 라이선스를 아직 지정하지 않았습니다.

### 1.2.0 수정

macOS 앱의 유튜브 전용 URL 검사를 플랫폼별 검사로 교체하고 Threads 공개 영상 추출기를 포함했습니다. 영상 크기를 제공하지 않는 SNS 게시물은 원본 화질로 처리합니다. 프로필·목록·단축 공유 링크 대신 개별 게시물 주소를 사용하세요.
