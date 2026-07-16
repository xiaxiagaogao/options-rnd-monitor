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
