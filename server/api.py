"""RND 仪表盘 API（spec §8.5）。FastAPI + 签名 cookie 会话（单用户）。

启动：.venv/bin/uvicorn server.api:app --port 8600
"""
import os
from pathlib import Path

from fastapi import Body, Cookie, Depends, FastAPI, Header, HTTPException, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from itsdangerous import BadSignature, URLSafeTimedSerializer

from rnd.config import PROJECT_ROOT  # 顺带加载 .env
from . import queries as q

SESSION_MAX_AGE = 30 * 24 * 3600
_signer = URLSafeTimedSerializer(os.environ["SESSION_SECRET"], salt="rnd-dash")

app = FastAPI(title="RND Dashboard", docs_url=None, redoc_url=None)
WEB = PROJECT_ROOT / "web"


def require_auth(rnd_session: str | None = Cookie(default=None),
                 x_auth: str | None = Header(default=None)):
    token = x_auth or rnd_session
    if not token:
        raise HTTPException(401, "未登录")
    try:
        _signer.loads(token, max_age=SESSION_MAX_AGE)
    except BadSignature:
        raise HTTPException(401, "会话失效")


@app.post("/api/login")
def login(response: Response, payload: dict = Body(...)):
    if payload.get("password") != os.environ.get("DASHBOARD_PASSWORD"):
        raise HTTPException(401, "口令错误")
    token = _signer.dumps({"u": "owner"})
    response.set_cookie("rnd_session", token, max_age=SESSION_MAX_AGE,
                        httponly=True, samesite="lax")
    return {"ok": True, "token": token}


@app.post("/api/logout")
def logout(response: Response):
    response.delete_cookie("rnd_session")
    return {"ok": True}


@app.get("/api/overview", dependencies=[Depends(require_auth)])
def overview():
    return q.overview()


@app.get("/api/symbol/{symbol}", dependencies=[Depends(require_auth)])
def symbol_detail(symbol: str):
    return q.symbol_detail(symbol.upper())


@app.get("/api/symbol/{symbol}/fan", dependencies=[Depends(require_auth)])
def fan(symbol: str, days: int = 120):
    return q.fan(symbol.upper(), days)


@app.get("/api/symbol/{symbol}/density", dependencies=[Depends(require_auth)])
def density(symbol: str, date: str | None = None, expiry: str | None = None):
    return q.density(symbol.upper(), date, expiry)


@app.get("/api/symbol/{symbol}/heatmap", dependencies=[Depends(require_auth)])
def heatmap(symbol: str):
    return q.heatmap(symbol.upper())


@app.get("/api/events", dependencies=[Depends(require_auth)])
def events(symbol: str | None = None, days: int = 90):
    return q.events(symbol.upper() if symbol else None, days)


@app.get("/api/journal", dependencies=[Depends(require_auth)])
def journal_list():
    return q.journal_list()


@app.post("/api/journal/open", dependencies=[Depends(require_auth)])
def journal_open(payload: dict = Body(...)):
    return q.journal_open(payload)


@app.post("/api/journal/close", dependencies=[Depends(require_auth)])
def journal_close(payload: dict = Body(...)):
    return q.journal_close(payload)


@app.get("/api/symbol/{symbol}/dcdf", dependencies=[Depends(require_auth)])
def dcdf(symbol: str):
    return q.dcdf(symbol.upper())


@app.get("/api/symbol/{symbol}/pit", dependencies=[Depends(require_auth)])
def pit(symbol: str):
    return q.pit(symbol.upper())


@app.post("/api/journal/roll_check", dependencies=[Depends(require_auth)])
def roll_check():
    from rnd.journal import roll_repin_check
    from rnd import db
    conn = db.get_conn()
    repinned = roll_repin_check(conn)
    conn.close()
    return {"ok": True, "repinned": repinned}


@app.get("/api/admission/{symbol}", dependencies=[Depends(require_auth)])
def admission(symbol: str):
    from rnd.admission import check_candidate, pool_reference
    return {"candidate": check_candidate(symbol), "pool": pool_reference()}


@app.post("/api/pool/add", dependencies=[Depends(require_auth)])
def pool_add(payload: dict = Body(...)):
    from rnd.admission import check_candidate
    from . import pool
    sym = payload.get("symbol", "").upper()
    if not sym:
        raise HTTPException(400, "缺少 symbol")
    # 换入前强制准入（spec §2）——服务端复核，不信任前端结论
    chk = check_candidate(sym)
    if not chk.get("ok") or not chk.get("verdict"):
        return {"ok": False, "error": "准入检查未通过", "check": chk}
    return pool.swap_in(sym, payload.get("replace"))


@app.get("/api/pool/status", dependencies=[Depends(require_auth)])
def pool_status():
    from . import pool
    return pool.status()


@app.get("/api/diagnostics/{symbol}/{date}/{expiry}", dependencies=[Depends(require_auth)])
def diagnostics(symbol: str, date: str, expiry: str):
    return q.diagnostics(symbol.upper(), date, expiry)


@app.post("/api/assistant", dependencies=[Depends(require_auth)])
def assistant(payload: dict = Body(...)):
    """研究助手单次请求（research-assistant-framework §4）：装配→组提示→LLM→文本。

    数值全部由服务端装配层钉死（§3），LLM 只组织语言。未配模型 → 503。
    """
    from . import assistant as asst
    symbol = (payload.get("symbol") or "").upper()
    question = (payload.get("question") or "").strip()
    if not symbol or not question:
        raise HTTPException(400, "缺少 symbol 或 question")
    ctx = asst.build_context(symbol, payload.get("asof"))
    if not ctx["meta"].get("asof"):
        raise HTTPException(404, f"{symbol} 无可用数据")
    msg = asst.build_messages(ctx, question)
    try:
        answer = asst.generate(msg["system"], msg["user"])
    except asst.AssistantNotConfigured as e:
        raise HTTPException(503, str(e))
    except asst.AssistantError as e:
        raise HTTPException(502, str(e))
    # 附装配包：前端可据此渲染脚注（样本量/闸门/外推区）并审计"数据描述非编造"
    return {"symbol": symbol, "asof": ctx["meta"]["asof"], "answer": answer, "context": ctx}


app.mount("/vendor", StaticFiles(directory=WEB / "vendor"), name="vendor")


@app.get("/{path:path}", include_in_schema=False)
def spa(path: str):
    target = WEB / path
    if path and target.is_file():
        return FileResponse(target)
    return FileResponse(WEB / "index.html")
