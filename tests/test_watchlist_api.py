"""自选清单路由（watchlist spec §6）：机器接口 token 鉴权与契约 + 面板管理路由。临时库，无网络。

    .venv/bin/python tests/test_watchlist_api.py
"""
import os
import sys
import tempfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ["SESSION_SECRET"] = "test-secret-for-regression"
os.environ["DASHBOARD_PASSWORD"] = "test-password"
os.environ["COOKIE_SECURE"] = "0"
os.environ["CONTEXT_API_TOKEN"] = "t" * 64

from fastapi.testclient import TestClient       # noqa: E402

from rnd import db, holdings_sync, watchlist as wl, watchlist_worker   # noqa: E402
from server import api, pool, queries as q      # noqa: E402

failures = []


def check(name, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {name}  {detail}")
    if not ok:
        failures.append(name)


tmp = Path(tempfile.mkdtemp()) / "wl.sqlite"
db.get_conn(tmp).close()
q.conn = lambda: db.get_conn(tmp)
holdings_sync.entry_dates = lambda conn, *a, **k: {"NVDA": {"qty": 1.0}, "FLNC": {"qty": 2.0}}
spawned = []
watchlist_worker.spawn = lambda: spawned.append(1)
AUTH = {"Authorization": "Bearer " + "t" * 64}

print("[机器接口 GET /api/watchlist]")
anon = TestClient(api.app)
check("不带 token → 401", anon.get("/api/watchlist").status_code == 401)
check("错 token → 401", anon.get("/api/watchlist", headers={"Authorization": "Bearer nope"}).status_code == 401)
with mock.patch.dict(os.environ, {"CONTEXT_API_TOKEN": ""}):
    check("token 未配置 → 503", anon.get("/api/watchlist", headers=AUTH).status_code == 503)
r = anon.get("/api/watchlist", headers=AUTH)
check("空清单 → 200、entries 为空", r.status_code == 200 and r.json()["entries"] == [], r.text[:200])

print("[面板路由鉴权]")
for method, url in (("get", "/api/watchlist/manage"), ("post", "/api/watchlist/add")):
    resp = getattr(anon, method)(url, **({"json": {}} if method == "post" else {}))
    check(f"{url} 未登录 → 401", resp.status_code == 401, str(resp.status_code))
check("旧后门 /api/pool/add 已下线", anon.post("/api/pool/add", json={}).status_code in (404, 405))
r = anon.get("/api/admission/AMD")   # 未知 GET 落到 SPA 兜底（返回首页 HTML），不再跑准入
check("旧后门 /api/admission 已下线", "text/html" in r.headers.get("content-type", "")
      and "candidate" not in r.text, r.headers.get("content-type"))

client = TestClient(api.app)
check("前提：登录", client.post("/api/login", json={"password": "test-password"}).status_code == 200)
check("登录 cookie 不能访问机器接口", client.get("/api/watchlist").status_code == 401)

print("[未迁移保护]")
r = client.post("/api/watchlist/add", json={"ticker": "AMD", "role": "speculative"})
check("清单没初始化时拒绝写入（免得池突然只剩一个待检查标的）", r.status_code == 400 and "初始化" in r.text
      and spawned == [], r.text[:160])
c = db.get_conn(tmp)
wl.add(c, "QQQ", "index", stage="ready", source="migration")   # 等价于跑过迁移
c.close()

print("[增删改]")
r = client.post("/api/watchlist/add", json={"ticker": "nvda", "role": "conviction"})
check("加入 → 200 + 起 worker", r.status_code == 200 and r.json()["entry"]["ticker"] == "NVDA"
      and len(spawned) == 1, r.text[:200])
r = client.post("/api/watchlist/add", json={"ticker": "SPY", "role": "index", "binance": "spyusdt"})
check("SPY 指数，合约名转大写", r.status_code == 200 and r.json()["entry"]["binance"] == "SPYUSDT", r.text[:200])
for bad, why in (({"ticker": "NVDA", "role": "conviction"}, "重复"),
                 ({"ticker": "AMD", "role": "index"}, "指数只许 SPY/QQQ"),
                 ({"ticker": "AMD"}, "缺角色"),
                 ({"ticker": "X' OR 1=1", "role": "speculative"}, "非法代码"),
                 ({"ticker": "AMD", "role": "speculative", "binance": "AMD/PERP"}, "非法合约名")):
    resp = client.post("/api/watchlist/add", json=bad)
    check(f"{why} → 400", resp.status_code == 400, f"{resp.status_code} {resp.text[:120]}")
check("未知操作 → 404", client.post("/api/watchlist/nuke", json={}).status_code == 404)

c = db.get_conn(tmp)
wl.set_stage(c, "NVDA", "ready")
wl.add(c, "FLNC", "speculative")
wl.set_stage(c, "FLNC", "rejected", reason="ATM 点差 16.2%（上限 10%）")
c.close()
r = client.post("/api/watchlist/update", json={"ticker": "NVDA", "role": "speculative"})
check("改角色", r.status_code == 200 and r.json()["entry"]["role"] == "speculative", r.text[:200])
spawned.clear()
r = client.post("/api/watchlist/recheck", json={"ticker": "FLNC"})
check("重新检查 → queued + 起 worker", r.status_code == 200 and r.json()["entry"]["stage"] == "queued"
      and spawned == [1], r.text[:200])
check("ready 的不能重新检查", client.post("/api/watchlist/recheck", json={"ticker": "NVDA"}).status_code == 400)

print("[管理视图]")
m = client.get("/api/watchlist/manage").json()
check("持仓里有、清单里没有 = FLNC 之外的持仓（FLNC 已在清单）", m["holdings_missing"] == [], str(m["holdings_missing"]))
check("带指数可选标的与准入门槛", m["index_tickers"] == ["SPY", "QQQ"] and m["thresholds"]["min_two_sided"] >= 1)
check("条目带 stage / data_latest", {"stage", "data_latest", "progress", "check"} <= set(m["entries"][0]))
r = client.post("/api/watchlist/remove", json={"ticker": "FLNC"})
check("移除 → 200", r.status_code == 200)
m = client.get("/api/watchlist/manage").json()
check("移除后 FLNC 回到「持仓未入清单」", m["holdings_missing"] == ["FLNC"], str(m["holdings_missing"]))

print("[机器接口契约]")
p = anon.get("/api/watchlist", headers=AUTH).json()
check("顶层字段", set(p) == {"version", "updated_at", "entries"}, str(sorted(p)))
e = {x["ticker"]: x for x in p["entries"]}
check("entry 字段", set(e["NVDA"]) == {"ticker", "role", "binance", "status", "reason", "added_at",
                                      "role_changed_at"}, str(sorted(e["NVDA"])))
check("NVDA active、无 reason", e["NVDA"]["status"] == "active" and e["NVDA"]["reason"] is None)
check("SPY pending、reason 说明在等检查", e["SPY"]["status"] == "pending" and e["SPY"]["reason"], str(e["SPY"]))
check("与 wl.payload 一致", p == wl.payload(db.get_conn(tmp)))

print("[标的页改身份也推进 updated_at]")
import time as _t   # noqa: E402
_t.sleep(1.1)
before = anon.get("/api/watchlist", headers=AUTH).json()
q.get_symbols = lambda: ["QQQ", "NVDA"]
r = client.post("/api/holdings/identity", json={"symbol": "NVDA", "identity": "conviction"})
after = anon.get("/api/watchlist", headers=AUTH).json()
check("持仓卡改身份 → version 与 updated_at 都变", r.status_code == 200 and after["version"] != before["version"]
      and after["updated_at"] > before["updated_at"], f"{before['updated_at']} → {after['updated_at']}")
c = db.get_conn(tmp)
ev = c.execute("SELECT action, detail FROM watchlist_events ORDER BY event_id DESC LIMIT 1").fetchone()
c.close()
check("并补一条 role 流水", ev[0] == "role" and "symbol_card" in ev[1], str(ev))

print("[spawn 失败不报 500]")
def _boom():
    raise OSError("output/ 不可写")
watchlist_worker.spawn = _boom
r = client.post("/api/watchlist/add", json={"ticker": "AMD", "role": "speculative"})
check("已写入 → 200 带 warning", r.status_code == 200 and "warning" in r.json(), r.text[:160])
watchlist_worker.spawn = lambda: spawned.append(1)

print("[池 = ready；从没写过清单 → 退回 symbols.yaml]")
_real_get_conn = db.get_conn
with mock.patch.object(db, "get_conn", lambda *a, **k: _real_get_conn(tmp)):
    check("effective_symbols 只含 ready（QQQ 排前）", pool.effective_symbols() == ["QQQ", "NVDA"], str(pool.effective_symbols()))
fresh = Path(tempfile.mkdtemp()) / "fresh.sqlite"
with mock.patch.object(db, "get_conn", lambda *a, **k: _real_get_conn(fresh)), \
        mock.patch.object(pool, "yaml_symbols", return_value=["SPY", "QQQ", "MU"]):
    check("未迁移 → 退回 yaml 池", pool.effective_symbols() == ["SPY", "QQQ", "MU"], str(pool.effective_symbols()))

print()
if failures:
    print(f"{len(failures)} 项失败: {failures}")
    sys.exit(1)
print("全部通过。")
