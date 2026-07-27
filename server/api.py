"""RND 仪表盘 API（spec §8.5）。FastAPI + 签名 cookie 会话（单用户）。

启动：.venv/bin/uvicorn server.api:app --port 8600
"""
import hmac
import os
import re
import time
from collections import deque
from pathlib import Path

from fastapi import Body, Cookie, Depends, FastAPI, Header, HTTPException, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from itsdangerous import BadSignature, URLSafeTimedSerializer

from rnd.config import PROJECT_ROOT  # 顺带加载 .env
from . import queries as q

SESSION_MAX_AGE = 30 * 24 * 3600
# 会话 cookie 默认只走 HTTPS；本机 http 调试可设 COOKIE_SECURE=0
COOKIE_SECURE = os.getenv("COOKIE_SECURE", "1") != "0"
LOGIN_FAIL_WINDOW = 300     # 登录失败计数窗口（秒）
LOGIN_FAIL_MAX = 8          # 窗口内失败上限，超过一律 429
SYMBOL_RE = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")


def _require_env(*names: str):
    """启动即校验必填配置：缺了就给可读报错，而不是 KeyError 或半初始化的 app。"""
    missing = [n for n in names if not os.environ.get(n)]
    if missing:
        raise RuntimeError(
            f"缺少必填环境变量: {', '.join(missing)}。"
            "复制 .env.example 为 .env 并填写后再启动。")


_require_env("SESSION_SECRET", "DASHBOARD_PASSWORD")
_signer = URLSafeTimedSerializer(os.environ["SESSION_SECRET"], salt="rnd-dash")

app = FastAPI(title="RND Dashboard", docs_url=None, redoc_url=None)
WEB = PROJECT_ROOT / "web"
_WEB_ROOT = WEB.resolve()   # 静态兜底的包含性边界，见 spa()


def valid_symbol(symbol: str) -> str:
    """标的代码白名单。挡住 SQL/路径类脏输入，也避免脏 symbol 污染标的池。"""
    s = (symbol or "").upper()
    if not SYMBOL_RE.match(s):
        raise HTTPException(400, f"非法标的代码: {symbol!r}")
    return s


def require_auth(rnd_session: str | None = Cookie(default=None),
                 x_auth: str | None = Header(default=None)):
    token = x_auth or rnd_session
    if not token:
        raise HTTPException(401, "未登录")
    try:
        _signer.loads(token, max_age=SESSION_MAX_AGE)
    except BadSignature:
        raise HTTPException(401, "会话失效")


_login_fails: deque[float] = deque()


def _login_rate_gate():
    """登录失败限速。全局计数而非按 IP 分桶：反代后所有请求源都是 127.0.0.1，
    按 IP 分桶既无意义又能被换 IP 绕过；单用户面板全局窗口足够，且不可绕。"""
    now = time.time()
    while _login_fails and now - _login_fails[0] > LOGIN_FAIL_WINDOW:
        _login_fails.popleft()
    if len(_login_fails) >= LOGIN_FAIL_MAX:
        retry = int(LOGIN_FAIL_WINDOW - (now - _login_fails[0])) + 1
        raise HTTPException(429, f"登录失败过多，请 {retry} 秒后再试",
                            headers={"Retry-After": str(retry)})


@app.post("/api/login")
def login(response: Response, payload: dict = Body(...)):
    _login_rate_gate()
    expected = os.environ.get("DASHBOARD_PASSWORD") or ""
    supplied = str(payload.get("password") or "")
    # 恒定时间比较；expected 为空直接拒绝——否则未配口令时 None != None 为假，空口令即可登录
    if not expected or not hmac.compare_digest(supplied, expected):
        _login_fails.append(time.time())
        raise HTTPException(401, "口令错误")
    token = _signer.dumps({"u": "owner"})
    response.set_cookie("rnd_session", token, max_age=SESSION_MAX_AGE,
                        httponly=True, samesite="lax", secure=COOKIE_SECURE, path="/")
    # 不回传 token：前端只依赖 HttpOnly cookie，避免落 localStorage 被 XSS 偷走 30 天会话
    return {"ok": True}


@app.post("/api/logout")
def logout(response: Response):
    response.delete_cookie("rnd_session", path="/")
    return {"ok": True}


@app.get("/api/overview", dependencies=[Depends(require_auth)])
def overview():
    return q.overview()


@app.get("/api/symbol/{symbol}", dependencies=[Depends(require_auth)])
def symbol_detail(symbol: str):
    return q.symbol_detail(valid_symbol(symbol))


@app.get("/api/symbol/{symbol}/fan", dependencies=[Depends(require_auth)])
def fan(symbol: str, days: int = 120):
    return q.fan(valid_symbol(symbol), days)


@app.get("/api/symbol/{symbol}/density", dependencies=[Depends(require_auth)])
def density(symbol: str, date: str | None = None, expiry: str | None = None):
    return q.density(valid_symbol(symbol), date, expiry)


@app.get("/api/symbol/{symbol}/heatmap", dependencies=[Depends(require_auth)])
def heatmap(symbol: str):
    return q.heatmap(valid_symbol(symbol))


@app.get("/api/events", dependencies=[Depends(require_auth)])
def events(symbol: str | None = None, days: int = 90):
    return q.events(valid_symbol(symbol) if symbol else None, days)


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
    return q.dcdf(valid_symbol(symbol))


@app.get("/api/symbol/{symbol}/pit", dependencies=[Depends(require_auth)])
def pit(symbol: str):
    return q.pit(valid_symbol(symbol))


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
    return {"candidate": check_candidate(valid_symbol(symbol)), "pool": pool_reference()}


@app.post("/api/pool/add", dependencies=[Depends(require_auth)])
def pool_add(payload: dict = Body(...)):
    from rnd.admission import check_candidate
    from . import pool
    sym = valid_symbol(payload.get("symbol", ""))
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
    return q.diagnostics(valid_symbol(symbol), date, expiry)


@app.post("/api/assistant", dependencies=[Depends(require_auth)])
def assistant(payload: dict = Body(...)):
    """研究助手单次请求（research-assistant-framework §4）：装配→组提示→LLM→文本。

    数值全部由服务端装配层钉死（§3），LLM 只组织语言。未配模型 → 503。
    """
    from . import assistant as asst
    symbol = valid_symbol(payload.get("symbol") or "")
    question = (payload.get("question") or "").strip()
    if not question:
        raise HTTPException(400, "缺少 question")
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


@app.post("/api/assistant/outlook", dependencies=[Depends(require_auth)])
def assistant_outlook():
    """全量市场展望（C++）：对所有自选标的出一版有净判断的 outlook（framework §2.3 精神）。"""
    from . import assistant as asst
    syms = q.get_symbols()
    msg, asof = asst.build_outlook_messages(syms)
    if not asof:
        raise HTTPException(404, "无可用数据")
    try:
        answer = asst.generate(msg["system"], msg["user"])
    except asst.AssistantNotConfigured as e:
        raise HTTPException(503, str(e))
    except asst.AssistantError as e:
        raise HTTPException(502, str(e))
    return {"symbols": syms, "asof": asof, "answer": answer}


app.mount("/vendor", StaticFiles(directory=WEB / "vendor"), name="vendor")


@app.get("/{path:path}", include_in_schema=False)
def spa(path: str):
    # 必须做包含性校验：URL 里的 %2e%2e 解码后就是 ..，Path 拼接不拦，
    # 否则任何未登录请求都能读到 web/ 之外的文件（实测可读 /etc/hosts、.env）。
    target = (WEB / path).resolve()
    if path and target.is_file() and target.is_relative_to(_WEB_ROOT):
        resp = FileResponse(target)
        # 业务前端资源禁止强缓存，避免 VPS 发版后仍命中旧 app.js 导致标的错位
        if target.suffix.lower() in {".js", ".css", ".html"}:
            resp.headers["Cache-Control"] = "no-cache"
        return resp
    resp = FileResponse(WEB / "index.html")
    resp.headers["Cache-Control"] = "no-cache"
    return resp
