"""配置加载：.env、symbols.yaml、管线超参数。"""
import os
from pathlib import Path

import yaml
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = PROJECT_ROOT / "rnd.sqlite"
OUTPUT_DIR = PROJECT_ROOT / "output"

load_dotenv(PROJECT_ROOT / ".env")

# Clash TUN 环境下 gRPC 直连握手失败，须走混合端口（spec §1 实测结论）
_proxy = os.getenv("RND_GRPC_PROXY")
if _proxy:
    os.environ.setdefault("grpc_proxy", _proxy)

# 到期日窗口（spec §1）。**唯一事实来源**：symbols.yaml 的 expiry 段。
# 2026-09-16 前这里没有读取器——symbols.yaml 的 expiry 段纯属装饰，真正决定拉取
# 窗口的是 backfill/eod_update 里写死的 `dt.timedelta(days=7)`。改 yaml 不生效，
# 是 term_slope 在到期周失效这件事一直没人改对的原因。现在统一收口到这里。
_EXPIRY_DEFAULTS = {"monthly_only": True, "dte_min": 2, "dte_max": 60, "count": 2}


def _load_expiry() -> dict:
    """读 symbols.yaml 的 expiry 段；文件缺失/损坏时退回默认值（不让配置问题炸掉管线）。"""
    out = dict(_EXPIRY_DEFAULTS)
    try:
        cfg = yaml.safe_load((PROJECT_ROOT / "symbols.yaml").read_text()) or {}
        out.update({k: v for k, v in (cfg.get("expiry") or {}).items() if k in out})
    except (OSError, yaml.YAMLError):
        pass
    return out


# dte_min=2（2026-09-16 从 7 下调）：7 这个门槛是为保护**翼部外推**（短 DTE 陡翼
# 的线性总方差延伸会产生负密度）而设的，但它同时挡掉了只需要 ATM 一个报价点的
# term_slope——整条链上报价最密、最不需要外推的地方。代价是到期周（DTE<7）事件
# 压力计全瞎，实测 2023-07~2026-07 有 18.6% 的标的-交易日 term_slope 为 NULL，
# 其中 93% 正是短腿被这个门槛滤掉所致。密度层的保护仍在（闸门 gate_pass），
# 且短 DTE 行不会被钉（pinned 取最接近 30 DTE），不污染状态层分位。
EXPIRY = _load_expiry()

# 管线超参数（spec §5）。平滑参数是核心超参，随 fit_meta 全程落库。
PIPELINE = {
    "day_count": 365.0,             # ACT/365
    "rel_spread_max": 0.25,         # 清洗：相对点差 (ask-bid)/mid 超此值剔除
    "wide_spread_flag": 0.10,       # 入库打标 wide_spread 的阈值（不剔除）
    "parity_pairs": 11,             # F 反推：ATM 邻域取几个行权价对
    "spline_k": 3,                  # cubic
    "spline_resid_iv": 2e-3,        # 平滑因子 s = N * resid²（IV 单位的预期残差）
    "grid_points": 801,             # 细网格点数
    "grid_pad_x": 0.15,             # 网格越出报价区的 log-moneyness 余量
    "extrapolation": "linear_total_variance",  # 翼部外推：w=σ²T 对 x 线性（C1，渐进对数正态尾）
    "density_neg_rel_tol": 1e-3,    # density ≥ 0 检查容差（相对密度峰值；深度 OTM
                                    # tick 量化噪声在 1e-4 峰值量级，绝对容差不可用）
    "integral_tol": 0.03,           # ∫density ≈ 1 容差（尾部截断损失）
}
