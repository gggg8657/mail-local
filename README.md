# mail-local — 업무 메일 작성·회신·윤문

> **한 줄 요약** — 받는 사람·목적·메모만 적으면 업무 메일(제목 후보 3개 + 본문)을 쓰고, 받은 메일을 붙여 넣으면 요청사항 체크리스트와 함께 답장 초안을,
> 내가 쓴 메일은 문장 단위로 다듬어 바뀐 곳을 diff(삭제 빨강·추가 초록)로 보여 줍니다. 문장마다 클릭으로 원문 되돌리기.
> 숫자·날짜·금액·이메일·영문 약어가 바뀐 문장은 서버가 원문과 비교해 **자동으로 원문 유지 + 경고**합니다.
> 로컬 LLM 만 쓰고 외부로 보내지 않습니다. 표준 라이브러리만(설치할 패키지 없음), CDN 없음.

```bash
bash setup.sh                 # Python → LLM 서버 탐색 → selftest → http://localhost:8780
bash setup.sh stop
python3 app.py                # 수동 실행
python3 app.py --cli polish natural < mail.txt     # 윤문 (모드: spell|natural|polite|concise|english)
python3 app.py --cli write < inputs.json           # 작성 (inputs 형식은 selftest.py 의 W 참고)
python3 selftest.py           # 가짜 LLM 으로 검증 (WORKSPACE 를 임시 폴더로 바꿔 돌고 지움)
```

| 환경변수 | 기본 | 설명 |
|---|---|---|
| `PORT` | `8780` | |
| `WORKSPACE` | `./_workspace` | 작성 이력 `history/<id>.json` (한 작업 = 한 파일, 다시 생성·이어서 다듬기가 판으로 쌓임) |
| `LLM_API` / `LLM_BASE_URL` / `LLM_MODEL` | `ollama` / `http://localhost:11434` / `qwen3:8b` | OpenAI 호환 서버면 `LLM_API=openai` |
| `NUM_CTX` | `16384` | Ollama 컨텍스트 (긴 원문 회신용) |

## 기능
- **메일 작성** — 받는 사람(이름·직함·소속·관계), 목적 14종(요청·보고·안내·감사·사과·회신·독촉·일정 조율·자료 송부·협조 요청·문의·축하·거절·기타), 메모, 첨부, 마감일(요일은 프로그램이 계산), 어조 4종, 길이 3종, 국문/영문(국문 메모 → 영문 메일). 입력에 없는 정보는 `[○○]` 빈칸으로 남기게 하고, 입력 숫자가 빠지거나 없던 숫자가 생기면 알림.
- **회신 작성** — 받은 메일 원문 + 답장 요지 → 답장 초안 + 원문의 요청·질문별 `반영 / 확인 필요` 체크리스트. 제목 후보 1은 `RE: 원제목`.
- **메일 윤문** — 맞춤법·띄어쓰기만 / 자연스럽게 / 더 정중하게 / 더 간결하게 / 영문 다듬기. 메일을 문장 단위 `[n]` 으로 나눠 고치게 해 문장별 diff·되돌리기가 정확합니다. 서명 구분선(`--`)·`-----Original Message-----`·`보낸 사람:` 아래와 연락처 줄은 손대지 않습니다.
- 공통 — 결과 복사, 메일 앱에서 열기(mailto), 다시 생성, 이어서 다듬기(빠른 버튼 + 자유 지시), 판(version) 탭, 작성 이력에서 다시 열기·삭제. 서명(국문·영문)은 브라우저에 기억해 본문 끝에 그대로 붙입니다(LLM 이 만들지 않음).

## 숫자 보존 검사 (윤문)
문장마다 원문과 결과에서 `숫자+단위`(쉼표·띄어쓰기 무시), 이메일·URL, 따옴표 인용, 영문 단어(국문 모드)/대문자 약어(영문 모드)를 뽑아 다중집합으로 비교합니다.
다르면 그 문장은 원문을 유지하고 `⚠ 사라짐/생김` 을 보여 줍니다(클릭하면 적용). `10/15 → 10월 15일` 같은 표기 변경도 잡습니다.
한글 고유명사(사람·기관 이름)는 자동 검사 대상이 아니라 프롬프트로만 막습니다 — diff 를 눈으로 확인하세요.

메일 클라이언트(Outlook·그룹웨어)와 연동하지 않습니다. 복사해 붙여 넣으세요.

## 출처·감사 (Credits)

- 파이썬 표준 라이브러리만 씁니다. 윤문 검사 아이디어는 같은 저작자의 [kordoc-local](https://github.com/gggg8657/kordoc-local)·[humanize-kr-local](https://github.com/gggg8657/humanize-kr-local) 을 따름(코드 복사 아님).
- **LLM 실행** — OpenAI 호환 API 로 호출합니다(모델 가중치는 동봉하지 않음). 기본 배포는 [Ollama](https://github.com/ollama/ollama) (MIT) 위의 Google [Gemma](https://ai.google.dev/gemma) `gemma4:31b` — 모델 이용 조건은 Gemma 배포처 참고.
- 이 도구는 [agent-page-portal](https://github.com/gggg8657/agent-page-portal) 에 연결해 쓰도록 만들었습니다(단독 실행도 됨).

저작권 표기·전체 목록은 `NOTICE` 를 보세요.
