#!/usr/bin/env python3
"""LLM 없이 검증 (가짜 LLM): 문장 나누기·되붙이기 → 숫자 보존 비교 → diff → 작성(제목 3·본문·서명·요일·숫자 알림)
→ 회신(체크리스트) → 이어서 다듬기(이전 판 diff) → 윤문(diff·숫자 바뀐 문장은 원문 유지·경고·형식 재시도) → 이력 → HTTP(SSE) → ui.html.
WORKSPACE 는 임시 폴더로 바꿔 실데이터 폴더에 흔적을 남기지 않는다.   python3 selftest.py"""
import json, os, re, shutil, sys, tempfile, threading, urllib.request

TMP = tempfile.mkdtemp(prefix="mail-selftest-")
os.environ["WORKSPACE"] = TMP
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import app  # noqa: E402

SEEN = []


def fake(system, user, model=None, temperature=0.4, on_token=lambda t: None):
    """요청 종류를 프롬프트로 알아보고 정해진 답을 흘려보낸다(토큰 스트리밍 흉내)."""
    SEEN.append((system, user))
    if "[메일 문장]" in user:  # 윤문
        units = re.findall(r"^\[(\d+)\] (.*)$", user, flags=re.M)
        out = []
        for n, t in units:
            t = t.replace("할수", "할 수").replace("되요", "돼요").replace("보내드릴께요", "보내 드리겠습니다")
            t = t.replace("1,500만원", "1,800만원")          # 숫자를 몰래 바꿈 → 원문 유지 + 경고여야 함
            t = t.replace("KAERI", "카에리")                  # 영문 약어 변경 → 원문 유지
            if "FORMAT_BREAK" in user and "반드시" not in user:
                out.append(f"고친 문장: {t}")             # 번호 없이 → 재시도해야 함
            else:
                out.append(f"[{n}] {t}")
        out = "\n".join(out)
    elif "[직전 메일]" in user:
        out = "[제목]\n1. [보고] 3분기 실적 보고서 검토 요청\n2. 실적 보고서 검토 부탁\n3. 검토 요청\n[본문]\n홍길동 부장님, 안녕하십니까.\n10월 17일까지 검토 부탁드립니다.\n이연구 드림"
    elif "[받은 메일 원문]" in user:
        out = ("[제목]\n1. RE: 10월 협의회 일정 문의\n2. 협의회 참석 회신\n3. 일정 회신드립니다\n[본문]\n박 과장님, 안녕하십니까.\n"
               "16일 오후 참석 가능합니다. 발표 자료는 [○○]까지 보내 드리겠습니다.\n이연구 드림\n[체크리스트]\n"
               "- [반영] 참석 가능 여부 → 16일 오후 가능\n- [확인 필요] 발표 자료 제출일 → 요지에 날짜 없음")
    else:
        out = ("**[제목]**\n1. [보고] 3분기 과제 실적 보고서 검토 요청\n2) \"3분기 실적 보고서(초안) 송부\"\n- 제목: 실적 보고서 검토 부탁드립니다\n"
               "[본문]\n홍길동 부장님, 안녕하십니까.\n인공지능응용연구실 이연구입니다.\n**10월 17일**까지 의견 부탁드립니다. 예산은 1,500만 원이며 2027년 계획도 포함했습니다.\n"
               "감사합니다.\n이연구 드림")
    for i in range(0, len(out), 7):
        on_token(out[i:i + 7])
    return out


app.llm = fake
try:
    assert app.WS == TMP, "WORKSPACE 가 임시 폴더가 아님"

    # 1) 문장 나누기: 줄·빈 줄·목록 머리·서명·전달 원문 보존, 되붙이면 원문과 정확히 같음
    mail = ("홍 부장님, 안녕하십니까.\n요청하신 자료 보내드릴께요. 검토 할수 있으면 좋겠어요! 예산은 1,500만원 입니다.\n\n"
            "- 일정: 10/15(수) 14시\n  1) KAERI 회의실 예약 되요\n\n감사합니다.\n이연구 드림\n--\n이연구 | 인공지능응용연구실\n"
            "Tel. 042-000-0000 | yeongu@example.org\n\n-----Original Message-----\n보낸 사람: 박영희\n이 문장은 손대면 안 됩니다.")
    parts, units = app.split_units(mail)
    assert app.join_units(parts, units) == mail
    assert units[1] == "요청하신 자료 보내드릴께요." and units[2] == "검토 할수 있으면 좋겠어요!" and units[3] == "예산은 1,500만원 입니다.", units
    assert "일정: 10/15(수) 14시" in units and "KAERI 회의실 예약 되요" in units
    assert not any("손대면" in u or "인공지능응용연구실" == u or "Tel" in u for u in units), units
    assert "이연구 드림" in units and not any(u.startswith("이연구 |") for u in units)

    # 2) 숫자·고유 표기 비교 (다중집합, 쉼표·띄어쓰기 무시, 단위 포함)
    assert app.changed_tokens("예산 1,500만 원", "예산 1500만원 입니다") == ([], [])
    assert app.changed_tokens("3억 원", "3천만 원") == (["3억원"], ["3천만원"])
    assert app.changed_tokens("10/15 회의", "10월 15일 회의") != ([], [])        # 표기 형식 변경도 잡음
    assert app.changed_tokens("KAERI 회의", "카에리 회의") == (["KAERI"], [])
    assert app.changed_tokens("Please send it by Oct 15.", "Kindly send it by Oct 15.", english=True) == ([], [])
    assert app.changed_tokens("yeongu@example.org 로", "yeongu@example.kr 로")[0] == ["yeongu@example.org"]

    # 3) diff
    ops = app.diff_ops("검토 할수 있으면 되요", "검토할 수 있으면 돼요")
    assert "".join(t for o, t in ops if o != "+") == "검토 할수 있으면 되요" and "".join(t for o, t in ops if o != "-") == "검토할 수 있으면 돼요"
    assert ["-", "되요"] in ops and ["+", "돼요"] in ops

    # 4) 날짜·요일 (요일은 프로그램이 계산해 넘긴다)
    assert app.fmt_date("2026-10-15") == "2026년 10월 15일(목)" and app.fmt_date("2026-10-15", True) == "Thursday, October 15, 2026"
    assert app.fmt_date("다음 주 금요일") == "다음 주 금요일"

    # 5) 작성: 제목 3개(형식이 어긋나도), 본문, 서명 덧붙임, 마크다운 제거, 숫자 알림, 이력
    W = {"to": {"name": "홍길동", "title": "부장", "org": "연구기획부", "relation": "상급자"}, "purpose": "보고",
         "points": "- 3분기 실적 보고서 초안 완성\n- 10/17까지 의견, 예산 1,500만 원", "attach": "실적보고서.hwpx", "due": "2026-10-17",
         "tone": "격식", "length": "보통", "lang": "ko",
         "sender": {"name": "이연구", "title": "선임연구원", "dept": "인공지능응용연구실", "sig_ko": "--\n이연구 선임연구원 | 인공지능응용연구실", "sig_en": ""}}
    toks = []
    r = app.run({"kind": "write", "inputs": W}, lambda ev: toks.append(ev))
    v = r["result"]
    assert any("token" in e for e in toks) and any(e.get("stage") == "llm" for e in toks)
    assert v["subjects"] == ["[보고] 3분기 과제 실적 보고서 검토 요청", "3분기 실적 보고서(초안) 송부", "실적 보고서 검토 부탁드립니다"], v["subjects"]
    assert v["body"].startswith("홍길동 부장님") and v["body"].endswith("--\n이연구 선임연구원 | 인공지능응용연구실") and "**" not in v["body"]
    assert any("2027" in n for n in v["notes"]), v["notes"]                   # 입력에 없던 숫자 → 알림
    sys_, user = SEEN[-1]
    assert "2026년 10월 17일(토)" in user and "[목적] 보고" in user and "실적보고서.hwpx" in user and "두괄식" in user
    assert "인공지능응용연구실 이연구 선임연구원" in user and "[제목]" in sys_ and "지어내지" in sys_
    hid = r["id"]

    # 6) 이어서 다듬기: 같은 이력에 2판, 이전 판과 diff, 서명은 빼고 넘김
    r2 = app.run({"kind": "write", "inputs": W, "id": hid, "instruction": "더 짧게", "prev": {"subjects": v["subjects"], "body": v["body"]}}, lambda ev: None)
    assert r2["id"] == hid and r2["version"] == 2 and r2["result"]["instruction"] == "더 짧게"
    assert r2["result"]["diff"] and any(o == "-" for o, _ in r2["result"]["diff"])
    u = SEEN[-1][1]
    assert "[수정 지시] 더 짧게" in u and "[직전 메일]" in u and "이연구 선임연구원 | 인공지능응용연구실" not in u
    r3 = app.run({"kind": "write", "inputs": W, "id": hid, "regen": True}, lambda ev: None)
    assert r3["version"] == 3 and r3["result"]["instruction"] == "다시 생성"

    # 7) 영문: 영문 규칙·영문 날짜·영문 서명
    E = dict(W, lang="en", sender=dict(W["sender"], sig_en="--\nYeongu Lee | KAERI"))
    app.run({"kind": "write", "inputs": E}, lambda ev: None)
    assert "Best regards" in SEEN[-1][0] and "Saturday, October 17, 2026" in SEEN[-1][1]

    # 8) 회신: 원문·요지 전달, 체크리스트 파싱, 원문 언어 자동
    R = {"original": "제목: 10월 협의회 일정 문의\n이연구 선임님, 협의회 참석 가능 여부와 발표 자료 제출일을 알려 주세요.", "points": "16일 오후 가능",
         "to": {"relation": "외부기관"}, "tone": "정중", "length": "짧게", "lang": "auto", "sender": W["sender"]}
    rr = app.run({"kind": "reply", "inputs": R}, lambda ev: None)["result"]
    assert rr["subjects"][0] == "RE: 10월 협의회 일정 문의" and rr["lang"] == "ko"
    assert rr["checklist"] == [{"ok": True, "text": "참석 가능 여부 → 16일 오후 가능"}, {"ok": False, "text": "발표 자료 제출일 → 요지에 날짜 없음"}], rr["checklist"]
    assert "[받은 메일 원문]" in SEEN[-1][1] and "빠짐없이" in SEEN[-1][0]
    assert app.build_reply({"original": "Dear Dr. Kim, could you send the report?", "lang": "auto"})[2] is True

    # 9) 윤문: 맞춤법 고친 문장은 적용, 숫자·약어 바뀐 문장은 원문 유지 + 경고, 서명·전달 원문 그대로
    p = app.run({"kind": "polish", "inputs": {"text": mail, "mode": "spell"}}, lambda ev: None)["result"]
    assert "[이번 교정: 맞춤법·띄어쓰기만]" in SEEN[-1][0]
    by = {x["before"]: x for x in p["units"]}
    assert by["검토 할수 있으면 좋겠어요!"]["use"] and by["검토 할수 있으면 좋겠어요!"]["after"] == "검토 할 수 있으면 좋겠어요!"
    assert ["+", "할 수"] in by["검토 할수 있으면 좋겠어요!"]["ops"]
    bad = by["예산은 1,500만원 입니다."]
    assert bad["changed"] and not bad["use"] and "1500만원" in bad["warn"] and "1800만원" in bad["warn"], bad
    k = by["KAERI 회의실 예약 되요"]
    assert not k["use"] and "KAERI" in k["warn"]
    assert "예산은 1,500만원 입니다." in p["text"] and "1,800" not in p["text"] and "할 수" in p["text"] and "보내 드리겠습니다" in p["text"]
    assert p["text"].endswith("-----Original Message-----\n보낸 사람: 박영희\n이 문장은 손대면 안 됩니다.")
    assert "Tel. 042-000-0000 | yeongu@example.org" in p["text"]
    assert len(p["warnings"]) == 2 and p["changed"] == sum(x["use"] for x in p["units"])
    assert app.join_units(p["parts"], [x["before"] for x in p["units"]]) == mail

    # 10) 윤문 형식 어긋남 → 재시도, 이어서 다듬기(결과 글을 원문 삼아)
    ev = []
    p2 = app.run({"kind": "polish", "inputs": {"text": "FORMAT_BREAK 문장입니다. 할수 있어요.", "mode": "natural"}}, ev.append)["result"]
    assert any(e.get("msg") == "형식 재시도" for e in ev) and p2["units"][1]["after"] == "할 수 있어요."
    p3 = app.run({"kind": "polish", "inputs": {"text": "원래 글.", "mode": "natural"}, "instruction": "더 정중하게", "prev": "할수 있어요."}, lambda e: None)
    assert p3["result"]["source"] == "할수 있어요." and "[이번 교정: 추가 지시]" in SEEN[-1][0] and "더 정중하게" in SEEN[-1][0]
    try:
        app.run({"kind": "polish", "inputs": {"text": "  "}}); raise AssertionError("빈 글 통과")
    except ValueError:
        pass

    # 11) 이력: 목록·열기·삭제·경로 검사
    hs = app.hist_list()
    assert len(hs) == 6 and {h["kind"] for h in hs} == {"write", "reply", "polish"}
    rec = app.hist_load(hid)
    assert len(rec["versions"]) == 3 and rec["title"].startswith("[보고]") and rec["inputs"]["purpose"] == "보고"
    try:
        app.hist_load("../../etc/passwd"); raise AssertionError("경로 검사 실패")
    except ValueError:
        pass

    # 12) HTTP: 화면(저작권 표기)·메타·SSE 실행·이력 API
    srv = app.ThreadingHTTPServer(("127.0.0.1", 0), app.H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}/"
    resp = urllib.request.urlopen(base)
    html = resp.read().decode()
    assert "메일 작성·윤문" in html and "data-sig" in html and resp.headers["X-Author"]
    meta = json.load(urllib.request.urlopen(base + "api/meta"))
    assert "독촉" in meta["purposes"] and len(meta["polish_modes"]) == 5 and "외부기관" in meta["relations"]
    req = urllib.request.Request(base + "api/run", json.dumps({"kind": "polish", "inputs": {"text": "회의 할수 있어요.", "mode": "spell"}}).encode(),
                                 {"Content-Type": "application/json"})
    evs = [json.loads(l[6:]) for l in urllib.request.urlopen(req).read().decode().split("\n") if l.startswith("data: ")]
    assert any("token" in e for e in evs) and evs[-1]["done"]["result"]["text"] == "회의 할 수 있어요."
    req = urllib.request.Request(base + "api/run", json.dumps({"kind": "write", "inputs": {"points": ""}}).encode(), {"Content-Type": "application/json"})
    evs = [json.loads(l[6:]) for l in urllib.request.urlopen(req).read().decode().split("\n") if l.startswith("data: ")]
    assert evs[-1]["error"] == "핵심 내용을 적어 주세요"
    assert len(json.load(urllib.request.urlopen(base + "api/history"))) == 7
    assert len(json.load(urllib.request.urlopen(base + "api/history/" + hid))["versions"]) == 3
    urllib.request.urlopen(urllib.request.Request(base + "api/history/delete", json.dumps({"id": hid}).encode(), {"Content-Type": "application/json"}))
    assert len(app.hist_list()) == 6
    srv.shutdown()

    # 12-1) 브라우저가 떠나도(첫 이벤트 뒤 쓰기가 BrokenPipeError) 윤문은 끝까지 — 이력에 남고 서버 오류·트레이스백 없음
    import io

    class Cut:
        def __init__(s, f, n):
            s.f, s.n = f, n

        def write(s, b):
            s.n -= 1
            if s.n < 0:
                raise BrokenPipeError(32, "Broken pipe")
            return s.f.write(b)

        def flush(s):
            pass

        def __getattr__(s, k):
            return getattr(s.f, k)

    class CutH(app.H):
        def setup(s):
            super().setup()
            s.wfile = Cut(s.wfile, 2)  # 응답 머리 + 첫 이벤트까지만 닿음

    ERRS = []

    class Srv(app.ThreadingHTTPServer):
        def handle_error(s, rq, addr):
            ERRS.append(sys.exc_info()[1])
    cut = Srv(("127.0.0.1", 0), CutH)
    threading.Thread(target=cut.serve_forever, daemon=True).start()
    err0, sys.stderr = sys.stderr, io.StringIO()
    cap = sys.stderr
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{cut.server_address[1]}/api/run",
                                     json.dumps({"kind": "polish", "inputs": {"text": "떠난 뒤 회의 할수 있어요.", "mode": "spell"}}).encode())
        got = urllib.request.urlopen(req).read().decode()
    finally:
        sys.stderr = err0
    cut.shutdown()
    hs = app.hist_list()
    assert got.count("data: ") == 1 and len(hs) == 7 and app.hist_load(hs[0]["id"])["versions"][-1]["text"] == "떠난 뒤 회의 할 수 있어요.", (got, hs[0])
    assert not ERRS and "Traceback" not in cap.getvalue() and cap.getvalue().count("연결 끊김") == 1, (ERRS, cap.getvalue())

    # 13) 폐쇄망: 외부 CDN·외부 요청 없음, 저작권 표기
    ui = app.read(os.path.join(app.ROOT, "ui.html"))
    assert not re.search(r'<(script|link|img)[^>]+(src|href)="https?://', ui) and "fetch('http" not in ui, "외부 참조 있음"
    _src = open(os.path.join(app.ROOT, "app.py"), encoding="utf-8").read()
    assert "wqkgMjAyNiBnZ2dnODY1NyDCtyBkb25nanVraW0uZGV2QGdtYWlsLmNvbQ==" in _src and "signed(" in _src and "X-Author" in _src, "저작권 표기 누락"
finally:
    shutil.rmtree(TMP, ignore_errors=True)

print("selftest OK")
