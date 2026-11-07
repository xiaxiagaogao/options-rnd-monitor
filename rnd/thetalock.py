"""ThetaData 免费档只允许单会话：准入检查、回填、每日增量都要拉链，用一把文件锁串行。

eod_update 整个运行期间持锁；自选清单 worker 处理准入 + 回填时持锁。谁后到谁等。
"""
import contextlib
import fcntl
import time
from pathlib import Path

from .config import OUTPUT_DIR

LOCK_PATH = OUTPUT_DIR / "thetadata.lock"


@contextlib.contextmanager
def thetadata_lock(path=None, *, blocking: bool = True, timeout: float | None = None, poll: float = 15.0):
    """排他锁。进程退出自动释放。

    blocking=False：拿不到立即抛 BlockingIOError。
    timeout：最多等这么多秒，等不到抛 TimeoutError（eod_update 用，免得 worker 卡死时把每日更新拖没）。
    """
    p = Path(path or LOCK_PATH)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a+") as fh:
        if timeout is None:
            fcntl.flock(fh, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        else:
            deadline = time.monotonic() + timeout
            while True:
                try:
                    fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError(f"等 ThetaData 锁超过 {timeout:.0f} 秒") from None
                    time.sleep(min(poll, max(0.0, deadline - time.monotonic())))
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)
