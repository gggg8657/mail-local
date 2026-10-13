#!/usr/bin/env python3
"""mail local — 업무 메일 작성·회신·윤문. 로컬 LLM, 외부 의존성 없음(stdlib), 외부 전송 없음.

  python3 app.py                               # http://localhost:8780
  LLM_API=openai LLM_BASE_URL=http://gpu:8000/v1 LLM_MODEL=Qwen3-32B python3 app.py
  python3 app.py --cli polish natural < mail.txt    # 윤문 결과를 표준 출력으로

작성(write)·회신(reply): 제목 후보 3개 + 본문 (+ 회신은 원문 요청사항 체크리스트).
윤문(polish): 메일을 문장 단위로 번호를 붙여 고치게 하고, 문장마다 diff·숫자 보존 검사 → 숫자·날짜·금액이 바뀐 문장은 원문 유지.
"""
import datetime
import difflib
import json
import os
import re
import secrets
import sys
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.abspath(__file__))
WS = os.environ.get("WORKSPACE") or os.path.join(ROOT, "_workspace")  # 포털이 AGENT_DATA/<도구> 로 모아 줌
LLM_API = os.environ.get("LLM_API", "ollama")            # ollama | openai (vLLM·LM Studio·llama.cpp 등)
LLM_BASE = os.environ.get("LLM_BASE_URL", "http://localhost:8000/v1" if LLM_API == "openai" else "http://localhost:11434").rstrip("/")
MODEL = os.environ.get("LLM_MODEL", "qwen3:8b")
LLM_KEY = os.environ.get("LLM_API_KEY", "")
NUM_CTX = int(os.environ.get("NUM_CTX", "16384"))
PORT = int(os.environ.get("PORT", "8780"))
MAX_IN = 12000  # 붙여넣은 메일 원문 최대 글자 수
LOCK = threading.Lock()


def read(p):
    with open(p, encoding="utf-8") as f:
        return f.read()


# ── 선택지 ──────────────────────────────────────────────────────────────
RELATIONS = ["상급자", "동료", "후배·팀원", "타 부서", "외부기관", "협력업체", "교수·학계", "고객·민원인"]
PURPOSES = {
    "요청": "무엇을·언제까지·왜 필요한지와 회신 방법을 분명히. 상대가 해야 할 일을 한눈에 보이게.",
    "보고": "결론·현황을 첫 문단에(두괄식). 세부는 항목으로, 다음 계획·결정 필요 사항을 끝에.",
    "안내": "일시·장소·대상·준비물 등 핵심 정보를 항목으로 정리. 문의처를 끝에.",
    "감사": "무엇에 대한 감사인지 구체적으로. 그 덕분에 생긴 결과를 한 줄. 과장된 찬사는 피한다.",
    "사과": "사실을 인정하고, 원인은 짧게, 조치·재발 방지책을 분명히. 변명조·책임 전가 금지.",
    "회신": "상대 메일에 대한 답이라는 점을 첫 문장에서 밝히고 물어본 것에 순서대로 답한다.",
    "독촉": "정중하되 무엇이 언제까지 필요한지 분명히. 상대를 탓하지 않고 사정이 있을 수 있음을 인정한다. 이전 요청 일자가 있으면 언급.",
    "일정 조율": "목적·소요 시간·후보 일시를 항목으로 제시하고, 가능한 시간을 회신해 달라고 요청.",
    "자료 송부": "보내는 자료가 무엇인지(첨부 목록)·용도·확인 요청 사항을 적는다.",
    "협조 요청": "배경·요청 내용·기한·협조가 주는 의미를 차례로. 상대 부담을 덜어 주는 표현을 쓴다.",
    "문의": "궁금한 점을 번호 매긴 질문으로. 답이 필요한 이유와 희망 회신일.",
    "축하": "무엇을 축하하는지 구체적으로, 짧고 따뜻하게.",
    "거절": "제안에 대한 감사 → 정중한 거절과 짧은 사유 → 가능하면 대안이나 다음 기회.",
    "기타": "메모의 의도를 살려 업무 메일로 정리한다.",
}
TONES = {"격식": "가장 격식 있는 합쇼체(…습니다/…십시오). 공문에 가까운 단정한 문장.",
         "정중": "정중한 합쇼체 위주, 부드러운 쿠션어(바쁘신 중에, 괜찮으시다면)를 적당히.",
         "간결": "정중하되 짧은 문장, 군더더기 인사 최소화, 항목 위주.",
         "친근": "예의는 지키되 해요체를 섞은 따뜻한 말투. 지나친 친밀감 표현은 금지."}
LENGTHS = {"짧게": "본문 3~5문장 안팎.", "보통": "본문 6~10문장 안팎.", "자세히": "배경·세부·후속 조치까지 충분히, 항목 활용."}
POLISH_MODES = {
    "spell": ("맞춤법·띄어쓰기만", "맞춤법·띄어쓰기·문장부호 오류만 고친다. 단어 선택·어순·어조·문장 길이는 바꾸지 않는다. 틀린 곳이 없으면 그대로 둔다."),
    "natural": ("자연스럽게", "어색한 표현·번역투·중복을 고쳐 읽기 쉽게 한다. 뜻·어조·정중함의 수준은 그대로. 문장 순서는 바꾸지 않는다. 이미 자연스러운 문장은 그대로 둔다."),
    "polite": ("더 정중하게", "업무 메일에 맞게 한 단계 더 정중하게: 명령조·단정조를 부탁·제안 표현으로, 해요체는 합쇼체로, 필요한 곳에만 쿠션어(바쁘신 중에, 번거로우시겠지만). 아부·과한 존칭(~하시옵니다)·이중 사과는 금지."),
    "concise": ("더 간결하게", "같은 뜻을 더 짧게: 군더더기·중복 인사·반복 설명을 덜어 낸다. 꼭 필요 없는 문장은 비워서 삭제할 수 있다(번호만 남기고 내용을 비움). 요청·기한·숫자·첨부 언급은 반드시 남긴다."),
    "english": ("영문 다듬기", "영어 문장의 문법·관사·시제·어휘를 비즈니스 이메일에 맞게 자연스럽게 고친다. 뜻은 바꾸지 않는다. 지나치게 장황하게 만들지 않는다. 한국어 문장은 그대로 둔다."),
}

# ── 숫자·고유 표기 보존 검사 ───────────────────────────────────────────────
UNIT = r"(?:\s?(?:조|억|천만|백만|십만|만|천|백)+)?\s?(?:원|달러|유로|엔|%|퍼센트|명|건|개|부|매|쪽|페이지|장|일|월|년|시|분|초|주|회|차|호|층|억원|만원|kg|g|mm|cm|m|km|MW|kW|MWh|kWh|Sv|mSv|Bq|GB|MB|TB)?"
NUM = re.compile(r"\d[\d,]*(?:\.\d+)?" + UNIT)
LITERAL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+|https?://\S+|「[^」]*」|『[^』]*』|“[^”]*”|\"[^\"]*\"")
ACRONYM = re.compile(r"\b[A-Z][A-Z0-9&\-]{1,}\b")
LATIN = re.compile(r"[A-Za-z][A-Za-z0-9&.+\-]*")


def _norm_num(s):
    return re.sub(r"[\s,]", "", s).replace("퍼센트", "%")


def keep_tokens(text, english=False):
    """바뀌면 안 되는 조각: 숫자(+단위)·이메일·URL·따옴표 인용·영문(국문 모드)/약어(영문 모드)"""
    lit = LITERAL.findall(text)
    rest = LITERAL.sub(" ", text)
    toks = [_norm_num(m) for m in NUM.findall(rest)] + lit
    toks += ACRONYM.findall(rest) if english else LATIN.findall(NUM.sub(" ", rest))
    return toks


def changed_tokens(a, b, english=False):
    """(사라진 것, 생긴 것) — 다중집합 비교"""
    ka, kb = keep_tokens(a, english), keep_tokens(b, english)
    lost, new = list(ka), []
    for t in kb:
        if t in lost:
            lost.remove(t)
        else:
            new.append(t)
    return lost, new


MONTHS = "January February March April May June July August September October November December".split()


def numbers_of(text):
    """작성·회신 결과 검사용: 숫자 값(단위·쉼표 무시) 집합. 영문 달 이름은 숫자로 본다."""
    text = re.sub(r"\b(%s)\b" % "|".join(MONTHS), lambda m: f" {MONTHS.index(m.group(1)) + 1} ", text or "")
    return {re.sub(r"[^\d.]", "", m).strip(".") for m in re.findall(r"\d[\d,]*(?:\.\d+)?", text or "")} - {""}


# ── diff ────────────────────────────────────────────────────────────────
TOK = re.compile(r"\s+|[가-힣]+|[A-Za-z]+|\d+|.", re.S)


def diff_ops(a, b):
    """어절·숫자·기호 단위 diff → [[op, text]] (op: '=' 같음, '-' 삭제, '+' 추가). 인접한 같은 op 는 합친다."""
    ta, tb = TOK.findall(a), TOK.findall(b)
    ops = []

    def add(op, s):
        if not s:
            return
        if ops and ops[-1][0] == op:
            ops[-1][1] += s
        else:
            ops.append([op, s])
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, ta, tb, autojunk=False).get_opcodes():
        if tag == "equal":
            add("=", "".join(ta[i1:i2]))
        else:
            add("-", "".join(ta[i1:i2]))
            add("+", "".join(tb[j1:j2]))
    return ops


# ── 문장 나누기 (윤문 단위) ────────────────────────────────────────────────
SENT_END = re.compile(r"[.?!。][\"'”’)\]]*(\s+)(?=\S)")  # group(1) = 문장 사이 공백
SKIP_LINE = re.compile(r"^\s*(?:-{2,}|_{2,}|={2,}|>|(?:보낸|받는)\s?사람|From:|To:|Cc:|Sent:|Subject:|참조:|날짜:)", re.I)
FREEZE = re.compile(r"^\s*(?:--\s*$|-{3,}\s*(?:Original|원본|전달)|(?:보낸|받는)\s?사람\s*:|From:\s)", re.I)
CONTACT = re.compile(r"[\w.+-]+@[\w-]+\.|https?://|(?:Tel|TEL|Fax|FAX|Mobile|전화|휴대폰|팩스|H\.?P)\s*[.:)]|\d{2,4}-\d{3,4}-\d{4}")


def split_units(text):
    """메일 → (parts, units). parts 는 문자열(그대로 둘 조각)과 int(윤문 단위 번호)의 목록.
    줄 구조·빈 줄·서명(연락처 줄)·인용(>)은 그대로 두고, 한 줄 안의 여러 문장은 문장마다 나눈다."""
    parts, units, frozen = [], [], False
    for k, line in enumerate(text.split("\n")):
        if k:
            parts.append("\n")
        s = line.strip()
        frozen = frozen or bool(FREEZE.match(line))  # 서명 구분선·전달/인용 원문 아래는 손대지 않는다
        if frozen or not s or SKIP_LINE.match(line) or CONTACT.search(line) or not re.search(r"[가-힣A-Za-z]{2}", s):
            parts.append(line)
            continue
        lead = line[:len(line) - len(line.lstrip())]
        m = re.match(r"(\s*(?:[-*•·▪◦○●□■※]|\d+[.)]|[가-하][.)]|\(\d+\))\s+)", line)
        if m:
            lead = m.group(1)
        parts.append(lead)
        body = line[len(lead):].rstrip()
        pos = 0
        for sm in SENT_END.finditer(body):
            units.append(body[pos:sm.start(1)])
            parts += [len(units) - 1, sm.group(1)]
            pos = sm.end(1)
        units.append(body[pos:])
        parts.append(len(units) - 1)
        parts.append(line[len(lead) + len(body):])
    return [p for p in parts if p != ""], units


def join_units(parts, texts):
    return "".join(texts[p] if isinstance(p, int) else p for p in parts)


# ── LLM ────────────────────────────────────────────────────────────────
def _clean(out):
    out = re.sub(r"<think>.*?</think>", "", out or "", flags=re.S).strip()
    out = re.sub(r"^```\w*\s*\n", "", out)
    return re.sub(r"\n?```\s*$", "", out).strip()


def _openai(msgs, model, temperature, on_token):
    body = {"model": model, "stream": True, "temperature": temperature, "messages": msgs}
    hdr = {"Content-Type": "application/json", **({"Authorization": f"Bearer {LLM_KEY}"} if LLM_KEY else {})}
    req = urllib.request.Request(LLM_BASE + "/chat/completions", json.dumps(body).encode(), hdr)
    buf = []
    try:
        with urllib.request.urlopen(req, timeout=1800) as r:
            for line in r:
                line = line.decode().strip()
                if not line.startswith("data:") or line == "data: [DONE]":
                    continue
                tok = (json.loads(line[5:])["choices"][0].get("delta") or {}).get("content") or ""
                if tok:
                    buf.append(tok)
                    on_token(tok)
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"LLM HTTP {e.code}: {e.read().decode(errors='replace')[:300]}")
    return "".join(buf)


def llm(system, user, model=None, temperature=0.4, on_token=lambda t: None):
    """스트리밍 채팅. Ollama /api/chat (think:false 미지원 모델이면 자동 재시도) 또는 OpenAI 호환."""
    model = model or MODEL
    msgs = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    try:
        if LLM_API == "openai":
            return _openai(msgs, model, temperature, on_token)
        body = {"model": model, "stream": True, "think": False, "messages": msgs,
                "options": {"temperature": temperature, "num_ctx": NUM_CTX}}
        for attempt in (0, 1):
            try:
                req = urllib.request.Request(LLM_BASE + "/api/chat", json.dumps(body).encode(), {"Content-Type": "application/json"})
                buf = []
                with urllib.request.urlopen(req, timeout=1800) as r:
                    for line in r:
                        if not line.strip():
                            continue
                        j = json.loads(line)
                        if "error" in j:
                            raise RuntimeError(j["error"])
                        tok = j.get("message", {}).get("content", "")
                        if tok:
                            buf.append(tok)
                            on_token(tok)
                        if j.get("done"):
                            break
                return "".join(buf)
            except urllib.error.HTTPError as e:
                msg = e.read().decode(errors="replace")
                if attempt == 0 and "think" in msg:
                    body.pop("think")
                    continue
                raise RuntimeError(f"LLM HTTP {e.code}: {msg[:300]}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"LLM 서버 연결 실패: {e.reason}")  # 내부 주소는 응답에 넣지 않음


def models():
    try:
        if LLM_API == "openai":
            req = urllib.request.Request(LLM_BASE + "/models", headers={"Authorization": f"Bearer {LLM_KEY}"} if LLM_KEY else {})
            with urllib.request.urlopen(req, timeout=3) as r:
                return [m["id"] for m in json.load(r)["data"]]
        with urllib.request.urlopen(LLM_BASE + "/api/tags", timeout=3) as r:
            return [m["name"] for m in json.load(r)["models"]]
    except Exception:
        return []


# ── 프롬프트 ────────────────────────────────────────────────────────────
WD_KO = "월화수목금토일"


def fmt_date(s, english=False):
    """'2026-10-15' → '2026년 10월 15일(목)' / 'Thursday, October 15, 2026'. 형식이 다르면 그대로."""
    try:
        d = datetime.date.fromisoformat((s or "").strip())
    except ValueError:
        return (s or "").strip()
    return d.strftime("%A, %B ") + f"{d.day}, {d.year}" if english else f"{d.year}년 {d.month}월 {d.day}일({WD_KO[d.weekday()]})"


COMMON = """너는 한국원자력연구원(KAERI) 연구자의 업무 메일을 대신 써 주는 비서다. 실무에서 바로 보낼 수 있는 메일을 쓴다.
원칙:
- 사실은 입력에 있는 것만 쓴다. 입력에 없는 사실·숫자·날짜·이름·약속을 지어내지 않는다. 꼭 필요한데 정보가 없으면 [○○] 처럼 대괄호 빈칸으로 남긴다.
- 숫자·날짜·금액·시간·고유명사(사람·기관·과제·장비 이름)는 입력에 적힌 그대로 옮긴다. 요일은 입력에 있을 때만 쓴다.
- 용건을 앞에 둔다. 항목이 셋 이상이면 '- ' 목록으로 정리한다. 마감·요청 사항은 분명하게.
- 상투적인 AI 문구(“~에 대해 말씀드리고자 합니다”의 반복, “도움이 되셨기를 바랍니다”, 이모지, 과한 감탄)는 쓰지 않는다. 마크다운 굵게(**)·제목(#)도 쓰지 않는다(메일 본문은 일반 텍스트).
- 연락처·소속 서명 블록은 쓰지 않는다(프로그램이 붙인다)."""

KO_RULES = """국문 메일 형식:
- 첫 줄: 호칭 + 인사 (예: "○○○ 책임님, 안녕하십니까." / 외부: "○○기관 ○○○ 과장님께," 다음 줄 "안녕하십니까.").
- 둘째 문장: 보내는 사람 소개(소속 + 이름, 예: "인공지능응용연구실 이연구입니다.") — 상급자·동료처럼 잘 아는 사이면 생략 가능.
- 본문 → 맺음 인사(예: "바쁘신 중에 검토 부탁드립니다. 감사합니다.") → 마지막 줄 "<보내는 사람 이름> 드림" (이름이 없으면 생략).
- 호칭: 직함에 '님'을 붙인다(부장님, 책임님, 교수님). '귀하'·'당신'은 쓰지 않는다."""

EN_RULES = """English email format (write the whole email in natural business English even though the notes are Korean):
- Greeting: "Dear Dr./Mr./Ms. <Last name>," (if the name or title is unknown: "Dear Sir or Madam,").
- First paragraph states who you are (affiliation) only if the recipient may not know you, then the purpose immediately.
- Clear asks with dates written as given. Close with "Best regards," on its own line and the sender's name on the next line.
- Korean organization names: use the official English name if obvious (한국원자력연구원 = Korea Atomic Energy Research Institute (KAERI)); otherwise romanize and keep the Korean in parentheses is NOT needed — just keep it consistent.
- Subject lines in English."""

OUT_FMT = """출력 형식 (정확히 이 순서와 표시만, 앞뒤 설명 없이):
[제목]
1. 제목 후보 1
2. 제목 후보 2
3. 제목 후보 3
[본문]
(메일 본문)"""

REPLY_FMT = OUT_FMT + """
[체크리스트]
- [반영] 원문의 요청·질문 1 → 답장에서 어떻게 답했는지 한 줄
- [확인 필요] 원문의 요청·질문 2 → 답장 요지에 정보가 없어 빈칸([○○])으로 둠 / 답하지 않음
(원문 메일이 요청·질문·확인을 구한 것을 하나도 빠짐없이 모두 적는다. 단순 인사는 제외.)"""

SUBJ_KO = "제목은 30자 이내, 용건이 드러나게. 필요하면 앞에 [요청]/[보고]/[안내]/[회신] 같은 머리표를 붙인 후보를 하나 이상 포함."


def sender_line(inp, english=False):
    s = inp.get("sender") or {}
    bits = [s.get("dept"), s.get("name"), s.get("title")]
    line = " ".join(b.strip() for b in bits if b and b.strip())
    if english and (s.get("sig_en") or "").strip():  # 영문 이름·소속 표기는 사용자가 쓴 영문 서명을 따른다
        line += " / 영문 표기(이 철자 그대로): " + " ".join(l.strip() for l in s["sig_en"].splitlines() if l.strip() and l.strip() != "--")
    return line


def signature(inp, english):
    s = inp.get("sender") or {}
    return (s.get("sig_en") if english else s.get("sig_ko") or "").strip()


def build_write(inp):
    en = inp.get("lang") == "en"
    purpose = inp.get("purpose") or "기타"
    rcpt = inp.get("to") or {}
    lines = [f"[오늘] {fmt_date(datetime.date.today().isoformat(), en)}",
             f"[받는 사람] {' '.join(x for x in (rcpt.get('org'), rcpt.get('name'), rcpt.get('title')) if x) or '(미상)'} — 관계: {rcpt.get('relation') or '미상'}",
             f"[보내는 사람] {sender_line(inp, en) or '(미상)'}",
             f"[목적] {purpose} — {PURPOSES.get(purpose, PURPOSES['기타'])}",
             f"[핵심 내용]\n{(inp.get('points') or '').strip()}"]
    if (inp.get("attach") or "").strip():
        lines.append(f"[첨부] {inp['attach'].strip()} — 본문에 첨부 사실과 목록을 언급")
    if (inp.get("due") or "").strip():
        lines.append(f"[마감·기한] {fmt_date(inp['due'], en)} — 본문에 분명히 쓴다")
    lines += [f"[어조] {inp.get('tone') or '정중'} — {TONES.get(inp.get('tone') or '정중', TONES['정중'])}",
              f"[길이] {inp.get('length') or '보통'} — {LENGTHS.get(inp.get('length') or '보통', LENGTHS['보통'])}"]
    system = "\n\n".join([COMMON, EN_RULES if en else KO_RULES + "\n" + SUBJ_KO, OUT_FMT])
    return system, "\n".join(lines)


def build_reply(inp):
    en = inp.get("lang") == "en" or (inp.get("lang") == "auto" and not re.search(r"[가-힣]", inp.get("original") or ""))
    lines = [f"[오늘] {fmt_date(datetime.date.today().isoformat(), en)}",
             f"[보내는 사람(나)] {sender_line(inp, en) or '(미상)'}",
             f"[원문 보낸 사람과의 관계] {(inp.get('to') or {}).get('relation') or '미상'}",
             f"[받은 메일 원문]\n<<<\n{(inp.get('original') or '').strip()[:MAX_IN]}\n>>>",
             f"[답장 요지 — 이 내용대로 답한다]\n{(inp.get('points') or '').strip() or '(요지 없음: 받았다는 확인과 검토 후 회신하겠다는 답)'}"]
    if (inp.get("attach") or "").strip():
        lines.append(f"[첨부] {inp['attach'].strip()}")
    lines += [f"[어조] {inp.get('tone') or '정중'} — {TONES.get(inp.get('tone') or '정중', TONES['정중'])}",
              f"[길이] {inp.get('length') or '보통'} — {LENGTHS.get(inp.get('length') or '보통', LENGTHS['보통'])}"]
    rules = ("답장 규칙: 원문 보낸 사람에게 답한다. 원문의 요청·질문에 원문 순서대로 답하고, 답장 요지에 없는 내용은 약속하지 않는다"
             "(정보가 없으면 [○○] 빈칸 또는 '확인 후 회신드리겠습니다'). 원문을 길게 되풀이하지 않는다. "
             "제목 후보 1은 'RE: <원문 제목>'(원문 제목을 알 수 있을 때).")
    system = "\n\n".join([COMMON, EN_RULES if en else KO_RULES, rules, REPLY_FMT])
    return system, "\n".join(lines), en


POLISH_SYS = """너는 업무 메일 교정 전문가다. 사용자가 쓴 메일을 [n] 번호가 붙은 문장 단위로 받는다. 각 문장을 고친 결과를 같은 번호로 돌려준다.
절대 규칙:
- 모든 번호를 빠짐없이, 같은 순서로, 한 줄에 하나씩 "[n] 고친 문장" 형식으로 출력한다. 문장을 합치거나 나누거나 순서를 바꾸지 않는다.
- 숫자·날짜·시간·금액·수량·단위·전화번호·이메일·URL·고유명사(사람 이름·기관명·과제명·장비명·영문 약어)는 한 글자도 바꾸지 않는다. 표기 형식(10/15 ↔ 10월 15일, 1,500만 원 ↔ 1천5백만 원)도 바꾸지 않는다.
- 단, 숫자 뒤 띄어쓰기·조사는 고칠 수 있다(예: "10/20 까지"→"10/20까지", "2,350만원 이고"→"2,350만원이고"). 숫자·단위 글자는 그대로.
- 사실·요청·약속 내용을 더하거나 빼지 않는다. 어림 표현(약·정도·이상·이내)과 말의 강도(…것 같습니다·…할 수 있습니다 같은 추측·완곡을 단정으로, 또는 그 반대로)는 바꾸지 않는다. "낮추다"를 "조정하다"처럼 뜻의 방향을 흐리지 않는다.
- 고칠 것이 없으면 원문 그대로 쓴다.
- 설명·머리말·따옴표·마크다운 없이 번호 줄만 출력한다."""


def build_polish(units, mode, instruction=""):
    label, rule = POLISH_MODES.get(mode, POLISH_MODES["natural"])
    if instruction:
        label, rule = "추가 지시", instruction.strip() + " (위 절대 규칙은 그대로 지킨다. 지시에 따라 필요 없는 문장은 번호만 남기고 비울 수 있다.)"
    system = POLISH_SYS + f"\n\n[이번 교정: {label}]\n{rule}"
    user = "[메일 문장]\n" + "\n".join(f"[{i + 1}] {u}" for i, u in enumerate(units))
    return system, user


REFINE_SYS = """아래 [직전 메일]을 [수정 지시]대로 고친다. 지시와 관계없는 내용·숫자·날짜·고유명사는 그대로 둔다. 입력에 없는 사실을 더하지 않는다."""


# ── 결과 파싱 ───────────────────────────────────────────────────────────
def parse_mail(raw):
    """LLM 출력 → {subjects, body, checklist}. 형식이 어긋나도 최대한 살린다."""
    raw = _clean(raw)
    sec = {"제목": "", "본문": "", "체크리스트": ""}
    cur = None
    buf = {k: [] for k in sec}
    pre = []
    for line in raw.split("\n"):
        m = re.match(r"^\s*\**\s*\[(제목|본문|체크리스트)\]\s*\**\s*:?\s*(.*)$", line)
        if m:
            cur = m.group(1)
            if m.group(2).strip():
                buf[cur].append(m.group(2))
            continue
        (buf[cur] if cur else pre).append(line)
    subjects = []
    for line in buf["제목"]:
        s = re.sub(r"^\s*(?:\d+[.)]|[-*•])\s*", "", line).strip().strip('"“”*')
        s = re.sub(r"^(?:제목|Subject)\s*:\s*", "", s, flags=re.I)
        if s:
            subjects.append(s)
    body = "\n".join(buf["본문"]).strip() or ("\n".join(pre).strip() if not cur else "")
    body = re.sub(r"\*\*(.+?)\*\*", r"\1", body)
    checklist = []
    for line in buf["체크리스트"]:
        m = re.match(r"^\s*[-*•]?\s*\[(반영|확인\s?필요|미반영)\]\s*(.+)$", line)
        if m:
            checklist.append({"ok": m.group(1) == "반영", "text": m.group(2).strip()})
        elif line.strip().lstrip("-*• ").strip():
            checklist.append({"ok": None, "text": line.strip().lstrip("-*• ").strip()})
    return {"subjects": subjects[:3], "body": body, "checklist": checklist}


def parse_numbered(raw, n):
    got = {}
    for m in re.finditer(r"^\s*\[(\d+)\]\s?(.*)$", _clean(raw), flags=re.M):
        k = int(m.group(1)) - 1
        if 0 <= k < n and k not in got:
            got[k] = m.group(2).strip()
    return got


def number_notes(must, body, allowed=""):
    """작성·회신: 내가 적은 숫자(must)가 본문에서 빠졌거나, 입력(must+allowed)에 없던 숫자가 생겼으면 알림"""
    a, b = numbers_of(must), numbers_of(body)
    lost = sorted(a - b, key=len, reverse=True)
    new = sorted(b - a - numbers_of(allowed), key=len, reverse=True)
    notes = []
    if lost:
        notes.append("입력에 있던 숫자가 본문에 없음: " + ", ".join(lost[:6]))
    if new:
        notes.append("입력에 없던 숫자가 본문에 생김(지어낸 것인지 확인): " + ", ".join(new[:6]))
    return notes


# ── 실행 ────────────────────────────────────────────────────────────────
def with_signature(body, inp, english):
    sig = signature(inp, english)
    return body.rstrip() + ("\n\n" + sig if sig and sig not in body else "")


def run_mail(kind, inp, model, emit, prev=None, instruction=""):
    """작성·회신 (prev+instruction 이면 이어서 다듬기)"""
    if kind == "write":
        if not (inp.get("points") or "").strip():
            raise ValueError("핵심 내용을 적어 주세요")
        system, user = build_write(inp)
        en = inp.get("lang") == "en"
    else:
        if not (inp.get("original") or "").strip():
            raise ValueError("받은 메일 원문을 붙여 넣어 주세요")
        system, user, en = build_reply(inp)
    if prev and instruction:
        system = system + "\n\n" + REFINE_SYS
        old = prev.get("body") or ""
        sig = signature(inp, en)
        if sig and old.rstrip().endswith(sig):
            old = old.rstrip()[:-len(sig)].rstrip()
        user += ("\n\n[직전 메일]\n[제목]\n" + "\n".join(f"{i + 1}. {s}" for i, s in enumerate(prev.get("subjects") or []))
                 + f"\n[본문]\n{old}\n\n[수정 지시] {instruction.strip()}\n위 출력 형식으로 전체를 다시 쓴다.")
    emit({"stage": "llm", "msg": f"{model} · {'회신' if kind == 'reply' else '작성'}{' · ' + instruction if instruction else ''}"})
    raw = llm(system, user, model, 0.5 if not instruction else 0.4, on_token=lambda t: emit({"token": t}))
    res = parse_mail(raw)
    if not res["body"]:
        raise RuntimeError("모델 응답에서 본문을 찾지 못했습니다. 다시 생성해 주세요.")
    must = "\n".join(str(inp.get(k) or "") for k in ("points", "attach", "due"))
    allowed = (inp.get("original") or "") + json.dumps(inp.get("to") or {}, ensure_ascii=False) + (prev.get("body", "") if prev else "")
    res["notes"] = number_notes(must, res["body"], allowed)
    res["body"] = with_signature(res["body"], inp, en)
    res["lang"] = "en" if en else "ko"
    if prev and prev.get("body"):
        res["diff"] = diff_ops(prev["body"], res["body"])
    res["raw"] = raw
    return res


def run_polish(inp, model, emit, instruction=""):
    text = (inp.get("text") or "").replace("\r\n", "\n")
    if not text.strip():
        raise ValueError("다듬을 메일을 붙여 넣어 주세요")
    if len(text) > MAX_IN:
        raise ValueError(f"메일이 너무 깁니다 ({len(text)}자 > {MAX_IN}자). 나눠서 다듬어 주세요.")
    mode = inp.get("mode") if inp.get("mode") in POLISH_MODES else "natural"
    english = mode == "english" or (not re.search(r"[가-힣]", text) and bool(re.search(r"[A-Za-z]", text)))
    parts, units = split_units(text)
    if not units:
        raise ValueError("다듬을 문장이 없습니다")
    system, user = build_polish(units, mode, instruction)
    emit({"stage": "llm", "msg": f"{model} · {instruction or POLISH_MODES[mode][0]} · {len(units)}문장"})
    raw = llm(system, user, model, 0.2, on_token=lambda t: emit({"token": t}))
    got = parse_numbered(raw, len(units))
    if len(got) < len(units) * 0.5:  # 형식을 못 지킴 → 한 번 더
        emit({"stage": "llm", "msg": "형식 재시도", "reset": True})
        raw = llm(system, user + "\n\n반드시 [1] 부터 [%d] 까지 번호 줄만 출력한다." % len(units), model, 0.1,
                  on_token=lambda t: emit({"token": t}))
        got = parse_numbered(raw, len(units))
    out, warns = [], []
    for i, u in enumerate(units):
        after = got.get(i, u)
        item = {"i": i, "before": u, "after": after, "changed": after != u, "use": after != u, "warn": ""}
        if i not in got:
            item["warn"] = "모델이 이 문장을 돌려주지 않아 원문 유지"
        elif item["changed"]:
            lost, new = changed_tokens(u, after, english)
            if lost or new:
                item["warn"] = ("숫자·고유 표기 변경" + (f" — 사라짐: {', '.join(lost[:4])}" if lost else "")
                                + (f" — 생김: {', '.join(new[:4])}" if new else "") + " → 원문 유지(적용하려면 클릭)")
                item["use"] = False
            elif after and mode != "concise" and not instruction and not 0.4 <= len(after) / max(1, len(u)) <= 2.2:
                item["warn"] = f"길이 변화 큼 ({len(after) / max(1, len(u)):.0%}) — 확인"
        if item["changed"]:
            item["ops"] = diff_ops(u, after)
        if item["warn"]:
            warns.append(f"{i + 1}번 문장: {item['warn']}")
        out.append(item)
    final = join_units(parts, [x["after"] if x["use"] else x["before"] for x in out])
    final = re.sub(r"[ \t]+\n", "\n", final)
    return {"mode": mode, "label": instruction or POLISH_MODES[mode][0], "parts": parts, "units": out, "text": final,
            "source": text, "changed": sum(x["use"] for x in out), "total": len(out), "warnings": warns, "raw": raw}


# ── 이력 (WORKSPACE/history/<id>.json — 한 메일 작업 = 한 파일, 판(version)이 쌓임) ─────
def hist_path(hid, ws=None):
    if not re.fullmatch(r"\d{8}-\d{6}-[0-9a-f]{4}", hid or ""):
        raise ValueError("잘못된 이력 id")
    return os.path.join(ws or WS, "history", hid + ".json")


def hist_load(hid, ws=None):
    return json.loads(read(hist_path(hid, ws)))


def hist_save(rec, ws=None):
    os.makedirs(os.path.join(ws or WS, "history"), exist_ok=True)
    p = hist_path(rec["id"], ws)
    with LOCK:
        with open(p + ".tmp", "w", encoding="utf-8") as f:
            json.dump(rec, f, ensure_ascii=False, indent=1)
        os.replace(p + ".tmp", p)


def hist_title(kind, inp, res):
    if kind == "polish":
        first = next((l.strip() for l in (inp.get("text") or "").split("\n") if l.strip()), "")
        return first[:40]
    return ((res.get("subjects") or [""])[0] or (inp.get("points") or "")[:40]).strip()


def hist_list(limit=200, ws=None):
    d = os.path.join(ws or WS, "history")
    if not os.path.isdir(d):
        return []
    out = []
    for fn in sorted(os.listdir(d), reverse=True)[:limit]:
        if not fn.endswith(".json"):
            continue
        try:
            r = json.loads(read(os.path.join(d, fn)))
            out.append({"id": r["id"], "kind": r["kind"], "title": r.get("title", ""), "ts": r["ts"], "versions": len(r["versions"])})
        except Exception:
            pass
    return out


def hist_delete(hid, ws=None):
    p = hist_path(hid, ws)
    if os.path.exists(p):
        os.remove(p)


def run(req, emit=lambda ev: None, ws=None):
    """POST /api/run 본체: kind=write|reply|polish, inputs, [id, instruction, prev, regen] → 이력에 판 추가 후 결과"""
    kind = req.get("kind")
    if kind not in ("write", "reply", "polish"):
        raise ValueError("kind 는 write|reply|polish")
    inp = req.get("inputs") or {}
    model = req.get("model") or MODEL
    instruction = (req.get("instruction") or "").strip()
    rec = None
    if req.get("id"):
        try:
            rec = hist_load(req["id"], ws)
        except FileNotFoundError:
            rec = None
    if kind == "polish":
        if instruction and req.get("prev"):  # 이어서 다듬기: 현재 결과 글을 원문 삼아 다시
            inp = dict(inp, text=req["prev"])
        res = run_polish(inp, model, emit, instruction)
    else:
        res = run_mail(kind, inp, model, emit, req.get("prev") if instruction else None, instruction)
    now = datetime.datetime.now()
    if not rec:
        rec = {"id": f"{now:%Y%m%d-%H%M%S}-{secrets.token_hex(2)}", "kind": kind, "versions": [],
               "title": hist_title(kind, inp, res)}
    rec.update(ts=now.isoformat(timespec="seconds"), inputs=req.get("inputs") or {}, model=model)
    res["instruction"] = instruction or ("다시 생성" if rec["versions"] else "")
    res["ts"] = rec["ts"]
    rec["versions"].append(res)
    hist_save(rec, ws)
    return {"id": rec["id"], "kind": kind, "version": len(rec["versions"]), "result": res}


def meta():
    return {"relations": RELATIONS, "purposes": list(PURPOSES), "tones": list(TONES), "lengths": list(LENGTHS),
            "polish_modes": [{"id": k, "label": v[0]} for k, v in POLISH_MODES.items()],
            "model": MODEL, "today": datetime.date.today().isoformat()}


# ── HTTP ───────────────────────────────────────────────────────────────
def user_ws(h):
    """포털이 넘긴 사용자별 작업 폴더. owner(서버 주인)·헤더 없음(단독 실행) = 예전 WORKSPACE 그대로"""
    u = (h.headers.get("X-Agent-User") or "owner").strip()
    if u == "owner":
        return WS
    d = os.path.join(WS, "u", re.sub(r"[^A-Za-z0-9_.-]", "_", u)[:64] or "_")
    os.makedirs(d, exist_ok=True)
    return d


HTML = read(os.path.join(ROOT, "ui.html")) if os.path.exists(os.path.join(ROOT, "ui.html")) else "ui.html 없음"

# ── 저작권 표기 (LICENSE·NOTICE 참고) ─────────────────────────────────────
_SIG = __import__("base64").b64decode("wqkgMjAyNiBnZ2dnODY1NyDCtyBkb25nanVraW0uZGV2QGdtYWlsLmNvbQ==").decode()
_SIG_A = __import__("base64").b64decode("Z2dnZzg2NTcgPGRvbmdqdWtpbS5kZXZAZ21haWwuY29tPg==").decode()


def signed(html):
    """화면에 저작권 표기를 붙인다. ui.html 에서 지워져도 서버가 내보낼 때 다시 붙는다."""
    name, mail = _SIG.split(" · ")
    if 'name="author"' not in html:
        meta = f'<meta name="author" content="{name[7:]} <{mail}>">'
        html = html.replace("<head>", "<head>" + meta, 1) if "<head>" in html else meta + html
    if "data-sig" not in html:
        tag = (f'<!-- {_SIG} --><div data-sig title="{mail}" style="text-align:center;font-size:11px;color:#9aa0a6;'
               f'opacity:.55;margin:28px 0 8px">{name} · 내부 시험용 · 배포용 아님</div>')
        html = html.replace("</body>", tag + "</body>", 1) if "</body>" in html else html + tag
    return html


class H(BaseHTTPRequestHandler):
    def log_message(self, fmt, *a):
        if "/api/run" in (a[0] if a else ""):
            super().log_message(fmt, *a)

    def _send(self, body, ctype="application/json", code=200):
        b = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("X-Author", _SIG_A)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        path = self.path.split("?")[0]
        try:
            if path == "/api/health":
                return self._send({"ok": True})
            if path == "/api/meta":
                return self._send(meta())
            if path == "/api/models":
                return self._send(models())
            if path == "/api/history":
                return self._send(hist_list(ws=user_ws(self)))
            m = re.fullmatch(r"/api/history/([\w-]+)", path)
            if m:
                return self._send(hist_load(m.group(1), user_ws(self)))
            self._send(signed(HTML).encode(), "text/html; charset=utf-8")
        except (FileNotFoundError, ValueError):
            self._send({"error": "없음"}, code=404)
        except Exception as e:
            self._send({"error": f"{type(e).__name__}: {e}"}, code=500)

    def do_POST(self):
        try:
            req = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        except ValueError:
            return self._send({"error": "잘못된 요청"}, code=400)
        if self.path == "/api/history/delete":
            try:
                hist_delete(req.get("id"), user_ws(self))
                return self._send({"ok": True})
            except ValueError as e:
                return self._send({"error": str(e)}, code=400)
        if self.path != "/api/run":
            return self._send({"error": "없는 경로"}, code=404)
        self.send_response(200)
        self.send_header("X-Author", _SIG_A)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()

        def emit(ev):
            self.wfile.write(f"data: {json.dumps(ev, ensure_ascii=False)}\n\n".encode())
            self.wfile.flush()

        try:
            emit({"done": run(req, emit, user_ws(self))})
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            try:
                emit({"error": str(e) if isinstance(e, (ValueError, RuntimeError)) else f"{type(e).__name__}: {e}"})
            except OSError:
                pass


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--cli":
        kind = sys.argv[2] if len(sys.argv) > 2 else "polish"
        tok = lambda ev: print(ev["token"], end="", file=sys.stderr, flush=True) if "token" in ev else None
        if kind == "polish":
            r = run_polish({"text": sys.stdin.read(), "mode": sys.argv[3] if len(sys.argv) > 3 else "natural"}, MODEL, tok)
            print("\n\n" + r["text"] + ("\n\n[경고]\n" + "\n".join(r["warnings"]) if r["warnings"] else ""))
        else:
            r = run_mail(kind, json.load(sys.stdin), MODEL, tok)
            print("\n\n" + "\n".join(r["subjects"]) + "\n\n" + r["body"])
        sys.exit(0)
    print(f"mail local → http://localhost:{PORT}  (llm={LLM_API} {LLM_BASE} {MODEL}, workspace={WS})  {_SIG}")
    ThreadingHTTPServer((os.environ.get("BIND", ""), PORT), H).serve_forever()
