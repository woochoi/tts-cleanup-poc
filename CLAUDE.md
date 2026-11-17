# CLAUDE.md

이 저장소에서 작업하는 Claude Code를 위한 안내입니다.

## 프로젝트 소개

복제한 목소리로 한국어 문장을 읽고, 음성 생성 모델이 마지막 단어 뒤에 덧붙이는 소리(잡음, 웃음, 대본에 없는 말)를 자동으로 잘라 내는 PoC입니다.

파이프라인: Chatterbox Multilingual(목소리 복제) → Demucs htdemucs(목소리 성분만 남김) → faster-whisper small(마지막 단어가 끝나는 시각 찾기) → 첫 쉼에서 자르기

이 파이프라인을 쓰는 '낭독 부스' 웹 화면(문장 읽기 + 진진가 게임)도 함께 들어 있습니다.

문서는 `docs/`에 있습니다: 문제 정의서, 모델 선정, 검증 결과, 한계, 개발 기록, 아키텍처, 사용자 매뉴얼. 구성 요소끼리 주고받는 방식을 바꾸기 전에 `docs/06-architecture.md`를 먼저 읽으세요.

## 명령어

```bash
python3.11 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

python src/clean_tts.py --ref refs/my_voice.wav --text "문장" --out out.wav --keep-stages   # 한 문장 만들기
python eval/run_eval.py --ref refs/my_voice.wav      # 20문장 평가 (약 5분). eval/out/와 eval/results.csv 생성
python eval/listen_server.py                         # 듣기 판정 페이지, http://127.0.0.1:8770/
python web/serve.py                                  # 낭독 부스 웹사이트, http://127.0.0.1:8780/ (포트 변경은 PORT=..., 끄기는 Ctrl+C)
```

테스트 코드는 없습니다. 바꾼 내용은 위 명령을 실제로 실행해서 확인합니다.
- **파이프라인을 바꿨을 때**: `clean_tts.py`로 한 문장, 그리고 `run_eval.py --only s01 s11`
- **서버나 화면을 바꿨을 때**: `web/serve.py`를 켜고 `/api/call`로 해당 기능(list_voices, speak, add_voice, remove_voice)을 직접 실행

## 폴더 구성

- `src/worker.py`: 세 모델을 한 번만 불러 두고, Unix 소켓(`/tmp/localvoice-<해시>.sock`)으로 요청을 받습니다. 5분 동안 요청이 없거나 메모리가 12GB를 넘으면 종료합니다.
- `src/server.py`: stdio MCP 서버입니다(도구: `list_voices`, `speak`, `add_voice`, `remove_voice`). 모델을 직접 불러오지 않고, 만든 음성은 `src/cache/`에 저장합니다.
- `src/clean_tts.py`: `worker.py`를 같은 프로세스에서 바로 쓰는 명령줄 도구입니다.
- `src/typecast_server.mjs`: 같은 도구 모양을 가진 Typecast MCP 서버입니다(선택).
- `web/nangdok-booth.html`: 낭독 부스 화면 전체가 들어 있는 파일 하나입니다. `window.claude.use('mcp' | 'sample')`를 호출합니다.
- `web/serve.py`: 화면에 `window.claude` 대역을 끼워 보내고, `/api/call`은 stdio로 MCP 서버에, `/api/sample`은 Claude API에 전달합니다.
- `eval/`: 평가 문장, 스크립트, 결과, 사람 판정(`listening.csv`), 처리 전·후 대표 사례 3개(`eval/samples/`)

## 규칙

- **커밋하지 않습니다**: `.env`, `refs/`, `src/cache/`, `eval/out/`, `custom_voices.json`, 로그, `.venv/`. 모두 `.gitignore`에 있으니 그대로 둡니다. wav 중에서는 `eval/samples/*.wav`만 커밋할 수 있습니다.
- **저장소에 개인정보를 넣지 않습니다.** `web/nangdok-booth.html`의 진진가 항목은 가상의 예시 인물이니 계속 가상으로 둡니다. 커밋하는 파일에 실명, 내 컴퓨터 경로, 이메일, API 키를 넣지 않습니다.
- **동의 없이 다른 사람 목소리를 복제하지 않고**, 상용 TTS 캐릭터 목소리 녹음을 저장소에 넣지 않습니다.
- **사용자에게 보이는 문구는 한국어로** 씁니다(화면, 서버 오류 메시지, 문서). 코드 주석은 영어로 씁니다.
- **화면과 두 서버를 함께 맞춥니다.** 화면은 Claude 앱에서도, `web/serve.py`를 통해서도 같은 도구 이름과 데이터 모양으로 호출합니다. 새 도구를 넣으려면 `src/server.py`의 `TOOLS` 항목과 처리 함수를 만들고, Claude 앱 아티팩트에서 쓸 경우 아티팩트의 `mcp` 권한에도 도구 이름을 추가합니다.
- **`worker.py`의 후처리를 바꾸면 `src/server.py`의 `POST` 값을 올립니다.** 그래야 예전 방식으로 만든 캐시 음성이 다시 재생되지 않습니다.
- **측정한 숫자만 문서에 씁니다.** 측정하지 않은 내용은 측정하지 않았다고 적습니다.

## 실제로 겪은 문제 (다시 겪지 않도록)

- Chatterbox 0.1.7은 `generate()`를 부를 때마다 attention hook을 추가하고 지우지 않습니다. `worker.py`가 매번 새로 생긴 hook을 지웁니다. 이 코드를 유지하세요.
- 문장마다 참조 녹음을 다시 처리하면 MPS 메모리가 문장마다 약 1GB씩 늘어납니다. 그래서 (참조 경로, 수정 시각, exaggeration)별로 처리 결과를 캐시합니다.
- Demucs를 Chatterbox와 함께 MPS에서 돌려도 메모리가 늘어납니다. Demucs와 Whisper는 일부러 CPU에서 돌립니다.
- Whisper는 `temperature=0.0, beam_size=1`과 자모(NFD) 단위 비교를 유지해야 합니다. 그렇지 않으면 실행할 때마다 자르는 위치가 달라집니다(사투리 단어를 매번 다르게 알아들음).
- Unix 소켓 경로는 약 104바이트까지만 됩니다. 그래서 소켓을 코드 옆이 아니라 `/tmp` 아래에 둡니다.
- Python의 `wave` 모듈은 WAVE_FORMAT_EXTENSIBLE 형식(macOS `afconvert`가 만드는 형식)을 읽지 못합니다. `add_voice`는 일반 16비트 PCM만 받고, 화면이 브라우저에서 녹음을 그 형식으로 바꿔 보냅니다.
- 처음 실행하면 Hugging Face에서 모델을 수 GB 내려받습니다. `HF_HUB_OFFLINE=1`은 모델을 받아 둔 뒤에만 설정합니다.
- `web/serve.py`의 Claude 호출은 `output_config.effort`로 생각 깊이를 정하고(thinking 예산 없음), 답을 JSON 스키마로 고정합니다. 스키마의 `answer`/`item`은 화면의 `buildPrompt`가 요구하는 형식과 맞춰 둡니다.
