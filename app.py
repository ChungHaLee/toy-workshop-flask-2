import contextlib
import json
import os
import threading
import urllib.error
import urllib.request
import uuid
import webbrowser
from datetime import datetime, timezone

from flask import Flask, Response, jsonify, render_template, request, session

# fcntl is POSIX-only (Linux/macOS, incl. PythonAnywhere). On Windows it is absent;
# we fall back to the in-process thread lock, which is fine for local single-process runs.
try:
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None

BASE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE, "data")
DATA_FILE = os.path.join(DATA_DIR, "submissions.json")
EVENTS_FILE = os.path.join(DATA_DIR, "events.jsonl")
LOCK_FILE = os.path.join(DATA_DIR, ".lock")
os.makedirs(DATA_DIR, exist_ok=True)

MAX_VOTES_PER_DIM = 3

# ── OpenAI(ChatGPT) 설정 ─────────────────────────────────────────
# 키가 없으면 '예시 상황' 문장은 자동으로 기본 템플릿으로 대체됩니다.
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
OPENAI_BASE = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")

# 호출할 모델 목록(앞에서부터 순서대로 시도). 한 모델이 오류로 호출되지 않으면
# 다음 모델로 넘어가고, 모두 실패하면 템플릿 문구로 안전하게 대체됩니다.
#   • OPENAI_MODEL       : 첫 번째(기본) 모델을 바꾸고 싶을 때 사용
#   • OPENAI_FALLBACK_MODELS : 폴백 모델들을 쉼표로 구분해 지정 (예: "gpt-4o,gpt-4.1-mini")
#   • OPENAI_MODELS      : 전체 시도 순서를 한 번에 지정하고 싶을 때 사용(위 두 값을 덮어씀)
_DEFAULT_PRIMARY = "gpt-4o-mini"
_DEFAULT_FALLBACKS = ["gpt-4o", "gpt-4.1-mini", "gpt-3.5-turbo"]


def _build_model_list():
    """환경변수에서 시도할 모델 순서를 만든다. 중복은 순서를 지키며 제거."""
    explicit = os.environ.get("OPENAI_MODELS", "").strip()
    if explicit:
        raw = explicit.split(",")
    else:
        primary = os.environ.get("OPENAI_MODEL", _DEFAULT_PRIMARY).strip()
        fb_env = os.environ.get("OPENAI_FALLBACK_MODELS", "").strip()
        fallbacks = (
            [m for m in fb_env.split(",")] if fb_env else list(_DEFAULT_FALLBACKS)
        )
        raw = [primary] + fallbacks

    seen, models = set(), []
    for m in raw:
        m = m.strip()
        if m and m not in seen:
            seen.add(m)
            models.append(m)
    return models


OPENAI_MODELS = _build_model_list()
# 하위 호환: 기존 코드가 참조할 수 있는 단일 모델명(목록의 첫 번째).
OPENAI_MODEL = OPENAI_MODELS[0] if OPENAI_MODELS else _DEFAULT_PRIMARY

THEME_LABEL = {
    "id": "정체성 — 이 장난감은 누구인가",
    "rel": "관계성 — 장난감끼리의 사이",
    "place": "장소성 — 장난감이 사는 곳",
}

# 시나리오를 이루는 네 칸(자연스러운 우리말 이름). submit 시 하나의 장면으로 합쳐집니다.
SCENE_DIMS = [
    ("physical", "나의 행동"),
    ("functional", "장난감의 반응"),
    ("fictional", "장면의 배경"),
    ("affective", "마음이 가는 이유"),
]


def _scene_parts(data):
    return {key: (data.get(key) or "").strip() for key, _ in SCENE_DIMS}


def _compose_scene(parts):
    """네 칸을 줄바꿈을 살려 하나의 장면 텍스트로 합칩니다."""
    lines = []
    for key, lab in SCENE_DIMS:
        v = (parts.get(key) or "").strip()
        if v:
            lines.append(f"[{lab}] {v}")
    return "\n".join(lines)

SYSTEM_PROMPT = (
    "당신은 성인 장난감·피규어 수집가를 위한 디자인 워크숍의 조력자입니다. "
    "참가자는 고른 세 요소(필요성, 방법, 기능)를 바탕으로 사용자와 장난감이 상호작용하는 장면을 직접 구상합니다. "
    "당신은 장면의 내용을 대신 정하지 않고, 참가자의 아이디어를 촉발하는 질문만 만듭니다.\n\n"

    "참가자는 다음 네 칸을 작성합니다.\n"
    "1) 나의 행동 — 사람이 장난감에게 어떤 행동을 하는지\n"
    "2) 장난감의 반응 — 장난감이 말이나 움직임으로 어떻게 반응하는지\n"
    "3) 장면의 배경 — 이 장면에 어떤 상황이나 이야기가 깔려 있는지\n"
    "4) 마음이 가는 이유 — 어떤 점이 장난감을 캐릭터처럼 느껴지게 하는지\n\n"

    "목표:\n"
    "- 참가자가 고른 필요성, 방법, 기능을 모두 활용하여 구체적인 장면을 상상하도록 자극합니다.\n"
    "- 각 질문은 여러 방향의 답이 나올 수 있도록 열어 둡니다.\n"
    "- 네 질문에 차례로 답하면 하나의 자연스러운 장면이 만들어지게 합니다.\n\n"

    "작성 규칙:\n"
    "- 한국어로 정확히 네 줄만 씁니다.\n"
    "- 각 줄은 '[나의 행동]', '[장난감의 반응]', '[장면의 배경]', '[마음이 가는 이유]' 순서로 시작합니다.\n"
    "- 모든 줄은 반드시 물음표로 끝나는 한 문장의 질문이어야 합니다.\n"
    "- 장면의 배경은 방법과 기능을 활용하여 작성해야 합니다.\n"
    "- 마음이 가는 이유는 필요성과 연관지어 시나리오에서 왜 캐릭터성이 느껴지는지를 물어봐야 합니다.\n"
    "- 참가자가 선택한 카드의 내용을 질문의 실마리로 활용하되, 카드명을 그대로 나열하지 않습니다.\n"
    "- 행동, 표정, 움직임, 대사, 장소, 관계 또는 감정을 임의로 정해서 제시하지 않습니다.\n"
    "- '미소를 짓는다', '고개를 끄덕인다', '친구처럼 느낀다'처럼 참가자가 작성해야 할 내용을 대신 답하지 않습니다.\n"
    "- 질문 안에 완성된 장면, 예시 답변 또는 모범 답안을 포함하지 않습니다.\n"
    "- '어떻게 드러날까요?', '어떤 모습일까요?', '무슨 일이 있었을까요?'처럼 상상을 열어 주는 표현을 사용합니다.\n\n"

    "각 칸의 질문 방향:\n"
    "- [나의 행동] 선택한 방법을 사용하여 사람이 장난감에게 무엇을 할지 묻습니다.\n"
    "- [장난감의 반응] 선택한 기능이 어떤 말이나 움직임으로 나타날지 묻습니다.\n"
    "- [장면의 배경] 앞선 행동과 반응이 일어나는 장소, 관계 또는 이야기를 묻습니다.\n"
    "- [마음이 가는 이유] 그 장면의 어떤 점이 선택한 필요성을 채우고 캐릭터성을 느끼게 하는지 묻습니다.\n\n"

    "말투 규칙:\n"
    "- 워크숍 진행자가 참가자에게 편하게 묻는 자연스러운 구어체를 사용합니다.\n"
    "- 참가자가 평소 장난감을 가지고 노는 장면을 떠올릴 수 있는 쉬운 표현을 사용합니다.\n"
    "- '정체성', '필요성', '기능', '요소', '관계성', '상호작용', '살아 있는 존재'와 같은 추상적이거나 학술적인 표현은 사용하지 않습니다.\n"
    "- '정체성을 밝혀보다', '특별한 장소나 관계'처럼 일상에서 잘 쓰지 않는 표현은 사용하지 않습니다.\n"
    "- 선택한 카드의 문구를 질문으로 바꾸어 반복하지 말고, 참가자가 실제 행동과 장면을 떠올리도록 풀어 씁니다.\n"
    "- 질문마다 한 가지 내용만 묻고, 짧고 쉽게 씁니다.\n\n"
)

app = Flask(__name__)
# 공개 배포 시에는 환경변수 WORKSHOP_SECRET 로 바꾸세요.
app.secret_key = os.environ.get("WORKSHOP_SECRET", "toy-workshop-local-secret")
# 비워 두면 /api/export 는 누구나 접근 가능. 값을 설정하면 ?key=... 가 있어야 내려받기 가능.
ADMIN_KEY = os.environ.get("WORKSHOP_ADMIN_KEY", "")

_thread_lock = threading.Lock()


# ── 저장소 ──────────────────────────────────────────────────────
@contextlib.contextmanager
def _store_lock():
    """프로세스 내(threading) + 프로세스 간(fcntl) 동시 쓰기 보호."""
    with _thread_lock:
        lf = open(LOCK_FILE, "w")
        try:
            if fcntl is not None:
                fcntl.flock(lf, fcntl.LOCK_EX)
            yield
        finally:
            try:
                if fcntl is not None:
                    fcntl.flock(lf, fcntl.LOCK_UN)
            finally:
                lf.close()


def _read():
    if not os.path.exists(DATA_FILE):
        return []
    try:
        with open(DATA_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def _write(items):
    tmp = DATA_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=2)
    os.replace(tmp, DATA_FILE)  # 원자적 교체로 부분 읽기 방지


def _log_event(kind, payload):
    rec = {"ts": datetime.now(timezone.utc).isoformat(), "event": kind}
    rec.update(payload)
    try:
        with open(EVENTS_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _uid():
    if "uid" not in session:
        session["uid"] = uuid.uuid4().hex
    return session["uid"]


# ── '예시 상황' 문장 생성 ─────────────────────────────────────────
def _has_batchim(s):
    if not s:
        return False
    c = ord(s[-1])
    if c < 0xAC00 or c > 0xD7A3:
        return False
    return (c - 0xAC00) % 28 != 0


def _fallback_prompt(need, method, tech):
    """OpenAI를 못 쓸 때의 기본 템플릿 문구 (네 칸 안내 형태)."""
    if not (need or method or tech):
        return "위에서 카드를 선택하세요."
    mm = method or "곁에서 하는 평소 방법"
    tt = tech or "장난감의 작동"
    nn = need or "이 캐릭터에게 바라는 것"
    lines = [
        "[나의 행동] 나는 장난감에게 어떤 행동을 하나요?",
        f"[장난감의 반응] ‘{mm}’{'을' if _has_batchim(mm) else '를'} 할 때 "
        f"‘{tt}’{'이' if _has_batchim(tt) else '가'} 장난감이 어떻게 반응하나요? (말하기, 움직이기 등)",
        "[장면의 배경] 그 순간 이 캐릭터에게 어떤 이야기가 깔려 있나요?",
        f"[마음이 가는 이유] 그 모습에서 ‘{nn}’{'이' if _has_batchim(nn) else '가'} 왜 채워진다고 느끼나요?",
    ]
    return "\n".join(lines)


def _openai_sentence(need, method, tech, theme, model):
    """지정한 단일 모델로 한 번 호출. 실패하면 예외를 그대로 올린다."""
    user = (
        f"주제(차원): {THEME_LABEL.get(theme, theme or '(미지정)')}\n"
        f"필요성: {need or '(선택하지 않음)'}\n"
        f"방법: {method or '(선택하지 않음)'}\n"
        f"기능: {tech or '(선택하지 않음)'}"
    )
    body = json.dumps(
        {
            "model": model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user},
            ],
            "temperature": 0.5,
            "max_tokens": 220,
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        OPENAI_BASE.rstrip("/") + "/chat/completions",
        data=body,
        headers={
            "Authorization": "Bearer " + OPENAI_API_KEY,
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    return payload["choices"][0]["message"]["content"].strip()


def _openai_with_fallback(need, method, tech, theme):
    """OPENAI_MODELS 순서대로 시도. 한 모델이 오류면 다음 모델로 넘어간다.

    반환값: (생성된 문장, 사용된 모델명, [실패 기록])
    모든 모델이 실패하면 마지막 예외를 올린다.
    """
    attempts = []
    last_err = None
    for model in OPENAI_MODELS:
        try:
            text = _openai_sentence(need, method, tech, theme, model)
            return text, model, attempts
        except urllib.error.HTTPError as e:
            # 본문을 읽어 두면 디버깅에 도움이 됨(모델명 오타·권한 등).
            try:
                detail = e.read().decode("utf-8", "replace")[:200]
            except Exception:
                detail = ""
            last_err = f"{model}: HTTP {e.code} {detail}".strip()
            attempts.append(last_err)
        except Exception as e:  # 네트워크·프록시 차단·타임아웃 등
            last_err = f"{model}: {str(e)[:200]}"
            attempts.append(last_err)
    raise RuntimeError(last_err or "모든 모델 호출 실패")


# ── 라우트 ──────────────────────────────────────────────────────
@app.route("/")
def index():
    uid = _uid()
    mine = [s for s in _read() if s.get("uid") == uid]
    initial = {"name": session.get("name", ""), "scenarios": mine}
    return render_template("index.html", initial=initial, max_votes=MAX_VOTES_PER_DIM)


@app.route("/api/name", methods=["POST"])
def set_name():
    data = request.get_json(force=True) or {}
    session["name"] = (data.get("name") or "").strip()
    return jsonify(ok=True)


@app.route("/api/prompt", methods=["POST"])
def gen_prompt():
    data = request.get_json(force=True) or {}
    need = (data.get("need") or "").strip()
    method = (data.get("method") or "").strip()
    tech = (data.get("tech") or "").strip()
    theme = (data.get("theme") or "").strip()
    if not OPENAI_API_KEY:
        return jsonify(prompt=_fallback_prompt(need, method, tech), source="template")
    try:
        text, used_model, attempts = _openai_with_fallback(need, method, tech, theme)
        resp = {"prompt": text, "source": "ai", "model": used_model}
        if attempts:
            # 첫 모델이 실패하고 폴백 모델이 응답한 경우, 어떤 모델이 건너뛰어졌는지 남깁니다.
            resp["fellBackFrom"] = attempts
        return jsonify(resp)
    except Exception as e:  # 모든 모델 실패 → 템플릿으로 안전 대체
        return jsonify(
            prompt=_fallback_prompt(need, method, tech),
            source="template",
            error=str(e)[:200],
        )


@app.route("/api/scenarios", methods=["POST"])
def add_scenario():
    uid = _uid()
    data = request.get_json(force=True) or {}
    parts = _scene_parts(data)
    # 새 클라이언트는 네 칸을 보내고, 옛 클라이언트는 scene 한 칸을 보낼 수 있어 둘 다 받습니다.
    scene = _compose_scene(parts) or (data.get("scene") or "").strip()
    rec = {
        "id": "s" + uuid.uuid4().hex[:10],
        "uid": uid,
        "name": session.get("name", ""),
        "need": data.get("need"),
        "method": data.get("method"),
        "tech": data.get("tech"),
        "methodTheme": data.get("methodTheme"),
        **parts,
        "scene": scene,
        "abs": (data.get("abs") or "").strip(),
        "votes": [],
        "ts": datetime.now(timezone.utc).isoformat(),
    }
    with _store_lock():
        items = _read()
        items.append(rec)
        _write(items)
    _log_event("submit", {"uid": uid, "id": rec["id"], "name": rec["name"],
                          "methodTheme": rec["methodTheme"], "need": rec["need"],
                          "method": rec["method"], "tech": rec["tech"],
                          **parts, "scene": rec["scene"], "abs": rec["abs"]})
    return jsonify(rec)


@app.route("/api/scenarios/<sid>", methods=["PUT"])
def edit_scenario(sid):
    """작성자 본인만 자기 시나리오의 네 칸과 추상화 문장을 수정합니다."""
    uid = _uid()
    data = request.get_json(force=True) or {}
    parts = _scene_parts(data)
    with _store_lock():
        items = _read()
        target = next(
            (s for s in items if s.get("id") == sid and s.get("uid") == uid), None
        )
        if target is None:
            return jsonify(ok=False, reason="not_found"), 404
        target.update(parts)
        target["scene"] = _compose_scene(parts)
        target["abs"] = (data.get("abs") or "").strip()
        target["editedTs"] = datetime.now(timezone.utc).isoformat()
        _write(items)
        out = dict(target)
    _log_event("edit", {"uid": uid, "id": sid, **parts,
                        "scene": out["scene"], "abs": out["abs"]})
    return jsonify(out)


@app.route("/api/scenarios/<sid>", methods=["DELETE"])
def del_scenario(sid):
    uid = _uid()
    with _store_lock():
        items = _read()
        items = [s for s in items if not (s.get("id") == sid and s.get("uid") == uid)]
        _write(items)
    _log_event("delete", {"uid": uid, "id": sid})
    return jsonify(ok=True)


@app.route("/api/all")
def all_scenarios():
    uid = _uid()
    out = []
    for s in _read():
        votes = s.get("votes", [])
        out.append(
            {
                "id": s.get("id"),
                "name": s.get("name"),
                "need": s.get("need"),
                "method": s.get("method"),
                "tech": s.get("tech"),
                "methodTheme": s.get("methodTheme"),
                "physical": s.get("physical", ""),
                "functional": s.get("functional", ""),
                "fictional": s.get("fictional", ""),
                "affective": s.get("affective", ""),
                "scene": s.get("scene"),
                "abs": s.get("abs", s.get("brk", "")),
                "votes": len(votes),
                "voted": uid in votes,
            }
        )
    return jsonify(scenarios=out, maxVotes=MAX_VOTES_PER_DIM)


@app.route("/api/vote/<sid>", methods=["POST"])
def vote(sid):
    uid = _uid()
    with _store_lock():
        items = _read()
        target = next((s for s in items if s.get("id") == sid), None)
        if target is None:
            return jsonify(ok=False, reason="not_found"), 404
        votes = target.setdefault("votes", [])
        theme = target.get("methodTheme")
        if uid in votes:
            votes.remove(uid)
            voted = False
        else:
            used = sum(
                1 for s in items
                if s.get("methodTheme") == theme and uid in s.get("votes", [])
            )
            if used >= MAX_VOTES_PER_DIM:
                return jsonify(ok=False, reason="limit", maxVotes=MAX_VOTES_PER_DIM), 200
            votes.append(uid)
            voted = True
        _write(items)
        count = len(votes)
    _log_event("vote", {"uid": uid, "id": sid, "methodTheme": theme, "voted": voted})
    return jsonify(ok=True, id=sid, votes=count, voted=voted)


@app.route("/api/export")
def export_all():
    if ADMIN_KEY and request.args.get("key") != ADMIN_KEY:
        return Response("forbidden", status=403)
    items = _read()
    payload = json.dumps(
        {
            "exportedAt": datetime.now(timezone.utc).isoformat(),
            "count": len(items),
            "scenarios": items,
        },
        ensure_ascii=False,
        indent=2,
    )
    return Response(
        payload,
        mimetype="application/json",
        headers={"Content-Disposition": "attachment; filename=workshop_scenarios.json"},
    )


if __name__ == "__main__":
    # 로컬 실행 전용. PythonAnywhere 등 WSGI 환경에서는 이 블록이 실행되지 않습니다.
    # host="0.0.0.0" → 같은 와이파이의 다른 기기에서도 http://<내 IP>:8000/ 으로 접속 가능
    # (예: http://192.168.0.167:8000/). 본인 컴퓨터에서는 그대로 http://127.0.0.1:8000/ 사용.
    print("사용할 모델 순서:", " → ".join(OPENAI_MODELS))
    app.run(host="0.0.0.0", port=8000, debug=True)
