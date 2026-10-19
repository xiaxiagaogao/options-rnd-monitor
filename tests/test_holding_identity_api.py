"""身份登记路由 + 装配层（holding-identity spec §6.2–§6.3）。临时库 + 假持仓，无网络。

    .venv/bin/python tests/test_holding_identity_api.py
"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ["SESSION_SECRET"] = "test-secret-for-regression"
os.environ["DASHBOARD_PASSWORD"] = "test-password"
os.environ["COOKIE_SECURE"] = "0"

from fastapi.testclient import TestClient       # noqa: E402

from rnd import db, holdings_sync               # noqa: E402
from server import api, queries as q            # noqa: E402

failures = []


def check(name, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {name}  {detail}")
    if not ok:
        failures.append(name)


tmp = Path(tempfile.mkdtemp()) / "ident.sqlite"
db.get_conn(tmp).close()
q.conn = lambda: db.get_conn(tmp)
HELD = {"AAA": {"qty": 2.0}, "SSS": {"qty": -1.0}}      # 合成：一多一空
holdings_sync.entry_dates = lambda conn, *a, **k: HELD
URL = "/api/holdings/identity"

print("[鉴权] 未登录 → 401")
anon = TestClient(api.app)
check("POST 未登录 401", anon.post(URL, json={"symbol": "AAA", "identity": "conviction"}).status_code == 401)

client = TestClient(api.app)
check("前提：登录成功", client.post("/api/login", json={"password": "test-password"}).status_code == 200)

print("[校验] 非法输入 → 400，不写库")
r = client.post(URL, json={"symbol": "AAA", "identity": "yolo"})
check("非法身份 400", r.status_code == 400, f"{r.status_code} {r.text}")
r = client.post(URL, json={"symbol": "ZZZ", "identity": "conviction"})
check("无币安持仓 400 且提示", r.status_code == 400 and "无币安持仓" in r.text, f"{r.status_code} {r.text}")
c = q.conn()
check("两次失败都没写行", c.execute("SELECT COUNT(*) FROM holding_identity").fetchone()[0] == 0)
c.close()

print("[写入] 登记 / 重复 / 改动")
r = client.post(URL, json={"symbol": "aaa", "identity": "conviction"}).json()
check("登记成功", r["ok"] and r["changed"] and r["identity"] == "conviction"
      and r["symbol"] == "AAA" and len(r["identity_history"]) == 1, str(r))
r = client.post(URL, json={"symbol": "AAA", "identity": "conviction"}).json()
check("相同身份 changed=False", r["ok"] and r["changed"] is False, str(r))
r = client.post(URL, json={"symbol": "AAA", "identity": "speculative"}).json()
check("改动记 prev、历史两条", r["changed"] and r["prev_identity"] == "conviction"
      and [h["identity"] for h in r["identity_history"]] == ["speculative", "conviction"], str(r))

print("[装配] identity_info")
c = q.conn()
info = q.identity_info(c, "AAA", True)
check("多头投机 → q25、note 写改动", info["identity"] == "speculative" and info["stop_q"] == "q25"
      and "由信念改为投机" in (info["identity_note"] or ""), str(info))
client.post(URL, json={"symbol": "SSS", "identity": "conviction"})
info = q.identity_info(c, "SSS", False)
check("空头信念 → q95（镜像）", info["stop_q"] == "q95", str(info))
info = q.identity_info(c, "BBB", True)
check("未登记 → 全 None、历史为空", info == {"identity": None, "stop_q": None,
                                         "identity_note": None, "identity_history": []}, str(info))
c.close()

print()
if failures:
    print(f"{len(failures)} 项失败: {failures}")
    sys.exit(1)
print("全部通过。")
