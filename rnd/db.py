"""SQLite 三表（spec §3）。原始层 append-only；派生层可全量重算。"""
import json
import sqlite3
from pathlib import Path

from .config import DB_PATH

DDL = """
CREATE TABLE IF NOT EXISTS raw_chain (
  date             TEXT NOT NULL,
  symbol           TEXT NOT NULL,
  expiry           TEXT NOT NULL,
  strike           REAL NOT NULL,
  right            TEXT NOT NULL CHECK (right IN ('C', 'P')),
  bid              REAL,
  ask              REAL,
  volume           INTEGER,
  oi               INTEGER,            -- 免费档 NULL（spec §1 实测）
  quality_flags    TEXT NOT NULL DEFAULT '',  -- one_sided/crossed/wide_spread，逗号分隔
  underlying_close REAL,
  sofr             REAL,
  PRIMARY KEY (date, symbol, expiry, strike, right)
);

CREATE TABLE IF NOT EXISTS rnd_curve (
  date      TEXT NOT NULL,
  symbol    TEXT NOT NULL,
  expiry    TEXT NOT NULL,
  grid_json TEXT NOT NULL,   -- {"strikes": [], "density": [], "cdf": []}
  fit_meta  TEXT NOT NULL,   -- 平滑参数、有效行权价数、拟合误差、外推方法与边界
  PRIMARY KEY (date, symbol, expiry)
);

CREATE TABLE IF NOT EXISTS rnd_indicators (
  date    TEXT NOT NULL,
  symbol  TEXT NOT NULL,
  expiry  TEXT NOT NULL,
  dte     INTEGER,
  -- 定位类
  forward REAL, sigma1_abs REAL, sigma1_pct REAL,
  q05 REAL, q25 REAL, q50 REAL, q75 REAL, q95 REAL,
  q05_in_range INTEGER, q25_in_range INTEGER, q50_in_range INTEGER,
  q75_in_range INTEGER, q95_in_range INTEGER,
  mode REAL,
  -- 状态类（水平值；252 日滚动分位在指标层另算）
  skew REAL, ex_kurt REAL, log_skew REAL, atm_iv REAL, rr25 REAL,
  bowley_skew REAL, bf25 REAL,
  term_slope REAL,           -- 需两个到期，skeleton 阶段 NULL
  tail_p_down REAL, tail_p_up REAL,
  -- 事件类
  n_modes INTEGER, modes_json TEXT,
  -- 校验类闸门
  gate_pass INTEGER, gate_detail TEXT,
  -- 时序拼接（spec §1：钉最接近 30 DTE + roll 日打标）
  pinned INTEGER, roll INTEGER,
  PRIMARY KEY (date, symbol, expiry)
);

-- 状态层（spec §4 通则）：pinned 序列上，状态类指标的 252 日滚动分位 + 日环比 Δ。
-- tidy 长表（date × symbol × indicator），指标集迭代时免迁移。纯派生可全量重算。
CREATE TABLE IF NOT EXISTS rnd_state (
  date      TEXT NOT NULL,
  symbol    TEXT NOT NULL,
  indicator TEXT NOT NULL,
  value     REAL,        -- 原始水平值（携带以便 UI 免 join）
  pct       REAL,        -- 252 日滚动分位 [0,100]；样本<60 或当日闸门失败为 NULL
  dpct      REAL,        -- 日环比分位 Δ；roll 日或前值缺失为 NULL
  sample_n  INTEGER,     -- 参与分位的有效样本数（闸门通过且非空）
  PRIMARY KEY (date, symbol, indicator)
);
"""

# 派生层可全量重算，加列走轻量迁移即可
_MIGRATIONS = [
    ("rnd_indicators", "log_skew", "REAL"),
    ("rnd_indicators", "bowley_skew", "REAL"),
    ("rnd_indicators", "bf25", "REAL"),
    ("rnd_indicators", "pinned", "INTEGER"),
    ("rnd_indicators", "roll", "INTEGER"),
]


def get_conn(db_path: Path = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.executescript(DDL)
    for table, col, typ in _MIGRATIONS:
        cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        if col not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
    conn.commit()
    return conn


def insert_raw_chain(conn, rows):
    """原始层 append-only：重复主键直接忽略（不可变，不覆盖）。"""
    conn.executemany(
        """INSERT OR IGNORE INTO raw_chain
           (date, symbol, expiry, strike, right, bid, ask, volume, oi,
            quality_flags, underlying_close, sofr)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        rows,
    )
    conn.commit()


def upsert_curve(conn, date, symbol, expiry, grid, fit_meta):
    conn.execute(
        "INSERT OR REPLACE INTO rnd_curve VALUES (?,?,?,?,?)",
        (date, symbol, expiry, json.dumps(grid), json.dumps(fit_meta)),
    )
    conn.commit()


def upsert_indicators(conn, row: dict):
    cols = ",".join(row.keys())
    ph = ",".join("?" * len(row))
    conn.execute(
        f"INSERT OR REPLACE INTO rnd_indicators ({cols}) VALUES ({ph})",
        list(row.values()),
    )
    conn.commit()


def replace_state(conn, symbol: str, rows):
    """全量重算：删掉该标的旧状态行再写新值。rows: (date,symbol,indicator,value,pct,dpct,sample_n)。"""
    conn.execute("DELETE FROM rnd_state WHERE symbol = ?", (symbol,))
    conn.executemany(
        "INSERT INTO rnd_state VALUES (?,?,?,?,?,?,?)", rows
    )
    conn.commit()
