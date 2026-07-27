"""安全回归测试（2026-07 三层审查 P0 批次）。

覆盖：静态兜底路径穿越 / 登录鉴权（fail-open·恒定时间·限速）/ 会话不落 body
      / symbol 白名单 / SQL 参数化 / SQLite WAL。

脚本风格，与其它测试一致：
    .venv/bin/python tests/test_security.py
"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# 必填配置须在 import server.api 之前钉死（load_dotenv 不覆盖已存在的环境变量）
os.environ["SESSION_SECRET"] = "test-secret-for-regression"
os.environ["DASHBOARD_PASSWORD"] = "test-password"
os.environ["COOKIE_SECURE"] = "0"          # TestClient 走 http，secure cookie 发不出去

from fastapi.testclient import TestClient   # noqa: E402
from rnd import db, timeseries              # noqa: E402
from server import api                      # noqa: E402

WEB_ROOT = api.WEB.resolve()   # 不用 api 的私有常量，好让这套测试也能跑在修复前的代码上

_failed = []


def check(name, cond, extra=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}  {extra}")
    if not cond:
        _failed.append(name)


client = TestClient(api.app)


# ---------- S1 静态兜底路径穿越 ----------
def test_path_traversal():
    print("\n[S1] 静态兜底路径穿越")
    sentinel = Path(tempfile.gettempdir()) / "rnd_sec_sentinel.txt"
    sentinel.write_text("SENTINEL-LEAKED\n")
    try:
        depth = len(WEB_ROOT.parts) - 1        # 从 web/ 走到 / 需要几层 ..
        rel = str(sentinel).lstrip("/")
        variants = {
            "百分号编码 ..": "/" + "%2e%2e/" * depth + rel,
            "编码斜杠 ..%2f": "/" + "..%2f" * depth + rel.replace("/", "%2f"),
            "裸 ..": "/" + "../" * depth + rel,
        }
        for name, url in variants.items():
            r = client.get(url)
            check(f"{name} 读不到 web/ 外的文件", "SENTINEL-LEAKED" not in r.text,
                  f"HTTP {r.status_code}")
        # 直接喂已解码的恶意路径给 spa()：绕过客户端/框架的一切规范化，
        # 只考验包含性校验本身（真实 uvicorn 会把 %2e%2e 解码后原样交进来）。
        up = "../" * depth
        for raw in [f"{up}{rel}", f"{up}etc/hosts", str(sentinel), "/etc/hosts",
                    f"..{os.sep}" * depth + rel, "./../" * depth + rel]:
            served = Path(api.spa(raw).path).resolve()
            check(f"spa() 挡住越界路径 {raw[:28]!r}…",
                  served == (WEB_ROOT / "index.html").resolve(), served.name)

        # 越界被挡的同时，正常静态资源必须还能取到
        r = client.get("/app.js")
        check("正常静态资源仍可访问", r.status_code == 200 and "createApp" in r.text)
        r = client.get("/不存在的路由")
        check("未知路由仍回落 index.html", r.status_code == 200 and "<!DOCTYPE" in r.text)
    finally:
        sentinel.unlink(missing_ok=True)


# ---------- S2 登录 ----------
def test_login():
    print("\n[S2] 登录鉴权")
    api._login_fails.clear()

    r = client.post("/api/login", json={"password": "wrong"})
    check("错误口令被拒", r.status_code == 401, f"HTTP {r.status_code}")

    api._login_fails.clear()
    r = client.post("/api/login", json={})
    check("空 body 被拒", r.status_code == 401, f"HTTP {r.status_code}")

    # fail-open 回归：未配 DASHBOARD_PASSWORD 时，旧代码 None != None 为假 → 空口令登录成功
    api._login_fails.clear()
    saved = os.environ["DASHBOARD_PASSWORD"]
    os.environ["DASHBOARD_PASSWORD"] = ""
    try:
        r = client.post("/api/login", json={})
        check("未配口令时不放行（fail-open 回归）", r.status_code == 401, f"HTTP {r.status_code}")
    finally:
        os.environ["DASHBOARD_PASSWORD"] = saved

    api._login_fails.clear()
    r = client.post("/api/login", json={"password": "test-password"})
    check("正确口令可登录", r.status_code == 200, f"HTTP {r.status_code}")
    check("响应体不再回传 token", "token" not in r.json(), str(r.json()))
    set_cookie = r.headers.get("set-cookie", "")
    check("会话 cookie 带 HttpOnly", "httponly" in set_cookie.lower())
    check("会话 cookie 带 SameSite", "samesite" in set_cookie.lower())

    # 限速：窗口内失败到上限后一律 429
    api._login_fails.clear()
    codes = [client.post("/api/login", json={"password": "x"}).status_code
             for _ in range(api.LOGIN_FAIL_MAX + 2)]
    check("失败达上限后触发 429", codes[-1] == 429, f"最后几次={codes[-3:]}")
    check("429 之前是 401", codes[0] == 401)
    api._login_fails.clear()


# ---------- S5 symbol 白名单 ----------
def test_symbol_whitelist():
    print("\n[S5] symbol 白名单")
    api._login_fails.clear()
    client.post("/api/login", json={"password": "test-password"})   # 拿会话 cookie

    # 注意：带 / 的输入（裸 ../ 或 %2f 编码）解码后不再匹配 /api/symbol/{symbol}，
    # 会落到 SPA 兜底而不是走校验器——那条路由由 S1 的包含性校验兜底，此处单独断言不泄露。
    for bad in ["X' OR '1'='1", "TOOOOOLONGSYM", "spy;drop", "SPY%00"]:
        r = client.get(f"/api/symbol/{bad}")
        check(f"非法 symbol 被拒 {bad!r}", r.status_code == 400, f"HTTP {r.status_code}")
    r = client.get("/api/symbol/%2e%2e%2f%2e%2e%2fetc%2fpasswd")
    check("含编码斜杠的 symbol 落到 SPA 兜底且不泄露",
          "root:" not in r.text and "<!DOCTYPE" in r.text, f"HTTP {r.status_code}")
    for bad in ["", "  ", None, "1SPY", "SPY/../X"]:
        try:
            api.valid_symbol(bad)
            check(f"校验器拒绝 {bad!r}", False, "竟然放行了")
        except Exception as e:                       # noqa: BLE001
            check(f"校验器拒绝 {bad!r}", getattr(e, "status_code", None) == 400, type(e).__name__)
    r = client.post("/api/pool/add", json={"symbol": "X' OR '1'='1"})
    check("pool/add 非法 symbol 被拒", r.status_code == 400, f"HTTP {r.status_code}")

    for good in ["SPY", "BRK.B", "RDS-A"]:
        check(f"合法 symbol 通过校验 {good}", api.valid_symbol(good) == good)


# ---------- S4 SQL 参数化 ----------
def test_sql_parameterized():
    print("\n[S4] SQL 参数化（postprocess_symbol）")
    with tempfile.TemporaryDirectory() as td:
        conn = db.get_conn(Path(td) / "t.sqlite")
        rows = [("2026-07-01", "SPY", "2026-07-31", 30, 0.15),
                ("2026-07-01", "X' OR '1'='1", "2026-08-29", 59, 0.30),
                ("2026-07-01", "X' OR '1'='1", "2026-07-31", 30, 0.25)]
        conn.executemany(
            "INSERT INTO rnd_indicators (date, symbol, expiry, dte, atm_iv) VALUES (?,?,?,?,?)",
            rows)
        conn.commit()

        timeseries.postprocess_symbol(conn, "X' OR '1'='1")

        spy_pinned = conn.execute(
            "SELECT COALESCE(pinned, 0) FROM rnd_indicators WHERE symbol = 'SPY'").fetchone()[0]
        evil_pinned = conn.execute(
            "SELECT COUNT(*) FROM rnd_indicators WHERE symbol = ? AND pinned = 1",
            ("X' OR '1'='1",)).fetchone()[0]
        check("带引号的 symbol 不会污染其它标的的行", spy_pinned == 0, f"SPY.pinned={spy_pinned}")
        check("目标标的自身仍被正确钉住", evil_pinned == 1, f"命中 {evil_pinned} 行")
        conn.close()


# ---------- S6 SQLite 并发加固 ----------
def test_sqlite_hardening():
    print("\n[S6] SQLite 并发加固")
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "t.sqlite"
        conn = db.get_conn(p)
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        timeout = conn.execute("PRAGMA busy_timeout").fetchone()[0]
        check("journal_mode = WAL", mode.lower() == "wal", mode)
        check("busy_timeout 已设", timeout >= 30000, str(timeout))
        conn.close()
        check("同库二次连接跳过 DDL 重跑", str(p.resolve()) in db._SCHEMA_READY)


for fn in (test_path_traversal, test_login, test_symbol_whitelist,
           test_sql_parameterized, test_sqlite_hardening):
    fn()

print()
if _failed:
    print(f"❌ {len(_failed)} 项失败: {_failed}")
    sys.exit(1)
print("全部通过。")
