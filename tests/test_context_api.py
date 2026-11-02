"""O1 时间点上下文 API + O2 PIT 回放。临时库 + 合成数据，无网络、不碰真实库。

    .venv/bin/python tests/test_context_api.py
"""
import hashlib
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ["SESSION_SECRET"] = "test-secret-for-regression"
os.environ["DASHBOARD_PASSWORD"] = "test-password"
os.environ["COOKIE_SECURE"] = "0"

import numpy as np                              # noqa: E402
from scipy.stats import norm                    # noqa: E402
from fastapi.testclient import TestClient       # noqa: E402

from rnd import db, exit_curves as ec, identity as ident       # noqa: E402
from rnd.state import STATE_INDICATORS                         # noqa: E402
from server import api, assistant, context as ctx_api, queries as q  # noqa: E402

TOKEN = "test-context-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
URL = "/api/context/TST"
D1, D2, D3, D4, D5 = ["2026-08-03", "2026-08-04", "2026-08-05",
                      "2026-08-06", "2026-08-07"]
DATES = [D1, D2, D3, D4, D5]
FWD = [100.0, 101.0, 102.0, 103.0, 104.0]
CLOSE = [100.5, 101.5, 102.5, 103.5, 104.5]
SGT = timezone(timedelta(hours=8))

_failed = []


def check(name, cond, extra=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}  {extra}")
    if not cond:
        _failed.append(name)


client = TestClient(api.app)
_EMPTY_HOLDINGS = lambda: {"entries": {}, "error": None}
_REAL_OPEN_POSITIONS = q.open_positions
_REAL_BINANCE_POSITION = assistant._binance_position
_REAL_CLOSE = assistant._close
_REAL_NOW = ctx_api._now_iso


def grid_for(F, v=0.12):
    s = F * np.exp(np.linspace(-6 * v, 6 * v, 801))
    cdf = norm.cdf((np.log(s / F) + 0.5 * v**2) / v)
    return {"strikes": s.tolist(), "density": np.gradient(cdf, s).tolist(), "cdf": cdf.tolist()}


def add_day(c, sym, i, *, atm_pct=50.0, rr_pct=50.0, roll=0, gate=1, n_modes=1):
    d, F = DATES[i], FWD[i]
    g = grid_for(F)
    db.upsert_curve(c, d, sym, d, g, {"x_quoted_range": [-0.2, 0.2]})
    s, cdf = np.asarray(g["strikes"]), np.asarray(g["cdf"])
    qv = lambda p: float(np.interp(p, cdf, s))
    peaks = ([{"strike": F * 0.9, "mass": 0.5}, {"strike": F * 1.1, "mass": 0.5}]
             if n_modes >= 2 else [])
    db.upsert_indicators(c, {
        "date": d, "symbol": sym, "expiry": d, "dte": 0, "forward": F,
        "sigma1_abs": F * 0.12, "sigma1_pct": 0.12,
        "q05": qv(0.05), "q25": qv(0.25), "q50": qv(0.50),
        "q75": qv(0.75), "q95": qv(0.95),
        **{f"q{int(p * 100):02d}_in_range": 1 for p in (0.05, 0.25, 0.50, 0.75, 0.95)},
        "mode": F, "n_modes": n_modes, "modes_json": json.dumps(peaks),
        "gate_pass": gate, "gate_detail": "{}", "pinned": 1, "roll": roll,
    })
    if sym == "TST":
        db.insert_raw_chain(c, [(d, sym, d, F, "C", 1, 1.1, 0, None, "", CLOSE[i], 0.04)])
        for ind_name in STATE_INDICATORS:
            pct = atm_pct if ind_name == "atm_iv" else (rr_pct if ind_name == "rr25" else 50.0)
            c.execute("INSERT INTO rnd_state VALUES (?,?,?,?,?,?,?)",
                      (d, sym, ind_name, F, pct, 0.0, 100))
    else:
        for ind_name in STATE_INDICATORS:
            c.execute("INSERT INTO rnd_state VALUES (?,?,?,?,?,?,?)",
                      (d, sym, ind_name, 50.0, 50.0, 0.0, 100))


def setup():
    """建临时库：TST 五天合成数据 + QQQ 基准；身份 D2 投机、D4 信念。"""
    tmp = Path(tempfile.mkdtemp()) / "ctx.sqlite"
    c = db.get_conn(tmp)
    for i in range(5):
        # D4/D5 故意制造未来事件：roll / gate FAIL / 双峰 / 分位越阈。
        add_day(c, "TST", i,
                atm_pct=[50.0, 60.0, 70.0, 95.0, 96.0][i],
                rr_pct=[20.0, 15.0, 12.0, 5.0, 4.0][i],
                roll=1 if i in (1, 3) else 0,
                gate=0 if i >= 3 else 1,
                n_modes=2 if i >= 3 else 1)
        add_day(c, "QQQ", i)
    ident.set_identity(c, "TST", "speculative", now=datetime(2026, 8, 4, 22, 30, tzinfo=SGT))   # D2 美东 10:30
    ident.set_identity(c, "TST", "conviction",
                       now=datetime(2026, 8, 6, 22, 30, tzinfo=SGT))   # D4 美东 10:30（+08 晚上）
    # 新加坡 D4 凌晨 = 美东 D3 中午:按美东日期算 D3 当天
    ident.set_identity(c, "QQQ", "conviction", now=datetime(2026, 8, 6, 0, 30, tzinfo=SGT))
    c.commit()
    c.close()
    q.conn = lambda: db.get_conn(tmp)
    q.get_symbols = lambda: ["TST", "QQQ"]
    assistant.load_holdings = _EMPTY_HOLDINGS
    q._pit_cached.cache_clear()
    return tmp


def walk_keys(obj):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield k
            yield from walk_keys(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from walk_keys(v)


def test_auth():
    print("\n[鉴权] 未配置 503 / 缺失或错误 token 401 / 正确 200")
    os.environ.pop("CONTEXT_API_TOKEN", None)
    r = client.get(URL)
    check("未配置 CONTEXT_API_TOKEN → 503", r.status_code == 503, f"{r.status_code} {r.text}")
    check("503 不回显 token", TOKEN not in r.text)
    r = client.get(URL, headers=AUTH)
    check("未配置时即使带 token 也 503（不 fail-open）", r.status_code == 503, f"{r.status_code}")

    os.environ["CONTEXT_API_TOKEN"] = TOKEN
    try:
        r = client.get(URL)
        check("缺 Authorization → 401", r.status_code == 401, f"{r.status_code}")
        r = client.get(URL, headers={"Authorization": "Bearer wrong-token"})
        check("错误 token → 401", r.status_code == 401, f"{r.status_code}")
        check("401 不回显 token", TOKEN not in r.text and "wrong-token" not in r.text)

        setup()
        r = client.get(URL, headers=AUTH)
        check("正确 token → 200", r.status_code == 200, f"{r.status_code} {r.text[:120]}")

        # 只挂在新路由上：同一个 Bearer 头拿去访问面板路由仍然 401（没有 cookie 会话）。
        r = client.get("/api/overview", headers=AUTH)
        check("面板 require_auth 不受影响", r.status_code == 401, f"{r.status_code}")
    finally:
        os.environ["CONTEXT_API_TOKEN"] = TOKEN


def test_pool_and_validation():
    print("\n[路由] 池外/非法 symbol 404、asof 格式错 400、省略 asof = 最新")
    setup()
    r = client.get("/api/context/NOPE", headers=AUTH)
    check("池外 symbol → 404", r.status_code == 404, f"{r.status_code}")
    r = client.get("/api/context/1BAD", headers=AUTH)
    check("非法 symbol → 404（不是面板的 400）", r.status_code == 404, f"{r.status_code}")
    for bad in ("2026-8-5", "20260805", "2026-02-30", "not-a-date", ""):
        r = client.get(f"{URL}?asof={bad}", headers=AUTH)
        check(f"asof={bad!r} → 400", r.status_code == 400, f"{r.status_code}")

    r = client.get(URL, headers=AUTH)
    b = r.json()
    check("省略 asof → asof/date 都是最新 RND 日 D5",
          b["asof"] == D5 and b["date"] == D5, f"{b.get('asof')}/{b.get('date')}")
    check("小写 symbol 也能读", client.get("/api/context/tst", headers=AUTH).json()["symbol"] == "TST")


def test_early_asof_and_identity():
    print("\n[日期/身份] asof 早于首条 RND → 200 null；identity 按 asof 当天结束时取")
    setup()
    r = client.get(f"{URL}?asof=2026-08-01", headers=AUTH)
    b = r.json()
    check("早于首条 RND → 200", r.status_code == 200, f"{r.status_code}")
    check("date / distribution / gate_pass 为 null",
          b["date"] is None and b["distribution"] is None and b["gate_pass"] is None, str(b)[:200])
    check("context 也不回退到最新", b["context"]["meta"]["asof"] is None
          and b["context"]["location"]["available"] is False)
    check("asof 回显请求日", b["asof"] == "2026-08-01")

    got = {}
    for d in (D1, D2, D3, D4, D5):
        got[d] = client.get(f"{URL}?asof={d}", headers=AUTH).json()["identity"]
    check("D1 未登记 → null", got[D1] is None, str(got[D1]))
    check("D2 登记投机 → D2/D3 都是 speculative",
          got[D2]["identity"] == "speculative" and got[D3]["identity"] == "speculative", str(got))
    qqq_d3 = client.get(f"/api/context/QQQ?asof={D3}", headers=AUTH).json()["identity"]
    check("新加坡 D4 凌晨(= 美东 D3 中午)的登记按美东日期算:D3 就看得到",
          qqq_d3 is not None and qqq_d3["identity"] == "conviction", str(qqq_d3))
    check("D4 美东上午的信念登记 → D3 看不到、D4 看得到",
          got[D3]["identity"] == "speculative" and got[D4]["identity"] == "conviction"
          and got[D5]["identity"] == "conviction", str(got))
    check("identity 只回 identity/changed_at",
          set(got[D3]) == {"identity", "changed_at"}, str(got[D3]))


def test_distribution_matches_stop_line_curve():
    print("\n[分布] pinned 曲线 = Q05/Q25/Q75/Q95 失效线同一条；strikes 升序、cdf 等长")
    setup()
    b = client.get(f"{URL}?asof={D3}", headers=AUTH).json()
    dist = b["distribution"]
    check("distribution.asof/expiry/forward 来自 D3 pinned 行",
          dist["asof"] == D3 and dist["expiry"] == D3 and dist["forward"] == FWD[2], str(dist)[:200])
    check("strikes 升序", all(a < z for a, z in zip(dist["strikes"], dist["strikes"][1:])))
    check("cdf 与 strikes 等长", len(dist["cdf"]) == len(dist["strikes"]), str(len(dist["cdf"])))
    c = q.conn()
    ind = q._row(c, "SELECT * FROM rnd_indicators WHERE symbol='TST' AND date=? AND pinned=1", (D3,))
    c.close()
    for qk, p in (("q05", 0.05), ("q25", 0.25), ("q50", 0.50), ("q75", 0.75), ("q95", 0.95)):
        got = ec.quantile(dist["strikes"], dist["cdf"], p)
        check(f"曲线分位 {qk} 与指标行一致", abs(got - ind[qk]) < 1e-9,
              f"got={got} expect={ind[qk]}")
    check("gate_pass 来自 D3 行", b["gate_pass"] is True)


def test_context_has_no_positions_and_no_holdings_io():
    print("\n[无持仓] context 无 position 键；不调用 load_holdings/_binance_position/open_positions")
    setup()

    def boom(*a, **k):
        raise AssertionError("O1 路径不应读持仓")

    assistant.load_holdings = boom
    assistant._binance_position = boom
    assistant._close = boom
    q.open_positions = boom
    try:
        r = client.get(f"{URL}?asof={D3}", headers=AUTH)
    finally:
        assistant.load_holdings = _EMPTY_HOLDINGS
        assistant._binance_position = _REAL_BINANCE_POSITION
        assistant._close = _REAL_CLOSE
        q.open_positions = _REAL_OPEN_POSITIONS
    check("不读持仓仍 200", r.status_code == 200, f"{r.status_code} {r.text[:160]}")
    b = r.json()
    ctx = b["context"]
    check("context 无 position 键", "position" not in ctx, str(sorted(ctx)))
    forbidden = {"position", "entry_price", "qty", "open_date", "stop_lines", "frozen",
                 "offset", "identity_note", "current_close", "current_close_date",
                 "close_basis", "size", "target_price", "direction", "stop_q"}
    found = forbidden & set(walk_keys(ctx))
    check("context 递归无持仓字段", not found, f"found={sorted(found)}")
    check("响应顶层字段固定", set(b) == {"symbol", "asof", "date", "gate_pass", "distribution",
                                          "identity", "context", "context_hash", "generated_at"},
          str(sorted(b)))
    blob = json.dumps(ctx, ensure_ascii=False)
    check("序列化里没有持仓价位/数量字段", "entry_price" not in blob and '"qty"' not in blob)


def test_context_hash_stable():
    print("\n[哈希] 规范化 JSON 可复现；改一个字段哈希就变")
    setup()
    ctx_api._now_iso = lambda: "2026-08-05T23:59:59+00:00"
    try:
        b1 = client.get(f"{URL}?asof={D3}", headers=AUTH).json()
        b2 = client.get(f"{URL}?asof={D3}", headers=AUTH).json()
    finally:
        ctx_api._now_iso = _REAL_NOW
    check("固定 generated_at 后两次响应完全一致", b1 == b2)
    ctx_api._now_iso = lambda: "2026-08-06T10:00:00+00:00"
    try:
        b3 = client.get(f"{URL}?asof={D3}", headers=AUTH).json()
    finally:
        ctx_api._now_iso = _REAL_NOW
    check("generated_at 不同、内容相同时 context_hash 相同",
          b3["generated_at"] != b1["generated_at"] and b3["context_hash"] == b1["context_hash"])
    h = b1.pop("context_hash")
    b1.pop("generated_at")
    canon = json.dumps(b1, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    check("context_hash = sha256(规范化 JSON)",
          h == hashlib.sha256(canon.encode("utf-8")).hexdigest(), h)
    check("hash 是 64 位十六进制", len(h) == 64 and all(c in "0123456789abcdef" for c in h))
    b1["date"] = "1999-01-01"
    canon2 = json.dumps(b1, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    check("字段变化 → 哈希变化", h != hashlib.sha256(canon2.encode("utf-8")).hexdigest())


def test_pit_replay():
    print("\n[回放] asof=D3 不读 D4/D5；删掉 D4/D5 重算深度相等")
    setup()
    c = q.conn()
    check("前提：库里有 D1..D5", q.latest_date(c, "TST") == D5)
    c.close()

    q._pit_cached.cache_clear()
    p3 = q.pit("TST", D3)
    p5 = q.pit("TST")
    check("PIT 缓存键带 asof：D3 样本 3、最新样本 5",
          p3["n_samples"] == 3 and p5["n_samples"] == 5,
          f"D3={p3.get('n_samples')} latest={p5.get('n_samples')}")

    q._pit_cached.cache_clear()
    ctx_before = assistant.build_context("TST", asof=D3)
    ctx_api._now_iso = lambda: "2026-08-05T23:59:59+00:00"
    try:
        o1_before = client.get(f"{URL}?asof={D3}", headers=AUTH).json()
    finally:
        ctx_api._now_iso = _REAL_NOW

    check("build_context meta.asof = D3", ctx_before["meta"]["asof"] == D3,
          str(ctx_before["meta"]))
    check("location 是 D3 的 forward", ctx_before["location"]["forward"] == FWD[2],
          str(ctx_before["location"]["forward"]))
    atm = next(s for s in ctx_before["state"] if s["indicator"] == "atm_iv")
    rr = next(s for s in ctx_before["state"] if s["indicator"] == "rr25")
    check("state 是 D3 分位（atm_iv P70、rr25 P12）",
          atm["pct"] == 70.0 and rr["pct"] == 12.0, f"{atm['pct']}/{rr['pct']}")
    ev_dates = [e["date"] for e in ctx_before["events"]]
    check("events 全部 ≤ D3", all(d <= D3 for d in ev_dates), str(ev_dates))
    check("events 不含 D4/D5", D4 not in ev_dates and D5 not in ev_dates, str(ev_dates))
    check("D2 的 roll 事件仍在", any(e["date"] == D2 and e["kind"] == "roll"
                                    for e in ctx_before["events"]))
    check("PIT 样本只有 D1..D3", ctx_before["summary"]["pit"]["n_samples"] == 3,
          str(ctx_before["summary"]["pit"].get("n_samples")))
    blob = json.dumps(ctx_before, ensure_ascii=False, default=str)
    check("build_context 序列化里没有 D4/D5", D4 not in blob and D5 not in blob)

    check("O1 date = D3", o1_before["date"] == D3 and o1_before["distribution"]["asof"] == D3)
    check("O1 identity = D3 时的 speculative", o1_before["identity"]["identity"] == "speculative")
    check("O1 context 的 PIT 样本只有 3", o1_before["context"]["summary"]["pit"]["n_samples"] == 3)
    oblob = json.dumps(o1_before, ensure_ascii=False, default=str)
    check("O1 序列化里没有 D4/D5", D4 not in oblob and D5 not in oblob)

    # 删掉 D4/D5 的全部数据后重算：正确实现应完全一致。
    c = q.conn()
    for table in ("rnd_indicators", "rnd_curve", "rnd_state", "raw_chain"):
        c.execute(f"DELETE FROM {table} WHERE date > ?", (D3,))
    c.execute("DELETE FROM holding_identity WHERE changed_at >= ?", (D4,))
    c.commit()
    c.close()
    q._pit_cached.cache_clear()

    ctx_after = assistant.build_context("TST", asof=D3)
    ctx_api._now_iso = lambda: "2026-08-05T23:59:59+00:00"
    try:
        o1_after = client.get(f"{URL}?asof={D3}", headers=AUTH).json()
    finally:
        ctx_api._now_iso = _REAL_NOW

    check("删后 latest 回到 D3", q.latest_date(q.conn(), "TST") == D3)
    check("删后 PIT 仍是 3 个样本", q.pit("TST")["n_samples"] == 3)
    check("build_context 两次深度相等", ctx_before == ctx_after)
    check("O1 两次深度相等（含 context_hash）", o1_before == o1_after)


if __name__ == "__main__":
    for fn in (test_auth, test_pool_and_validation, test_early_asof_and_identity,
               test_distribution_matches_stop_line_curve,
               test_context_has_no_positions_and_no_holdings_io,
               test_context_hash_stable, test_pit_replay):
        fn()
    print()
    if _failed:
        print(f"{len(_failed)} 项失败: {_failed}")
        sys.exit(1)
    print("全部通过。")
