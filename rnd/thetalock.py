"""ThetaData 免费档只允许单会话：准入检查、回填、每日增量都要拉链，用一把文件锁串行。

eod_update 整个运行期间持锁；自选清单 worker 处理准入 + 回填时持锁。谁后到谁等。
"""
import contextlib
import fcntl
from pathlib import Path

from .config import OUTPUT_DIR

LOCK_PATH = OUTPUT_DIR / "thetadata.lock"


@contextlib.contextmanager
def thetadata_lock(path=None, *, blocking: bool = True):
    """排他锁。blocking=False 时拿不到立即抛 BlockingIOError。进程退出自动释放。"""
    p = Path(path or LOCK_PATH)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a+") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)
