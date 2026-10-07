"""研究助手 LLM 接缝（generate）的契约验证：未配置时须显式抛错、不静默。

用法：.venv/bin/python tests/test_assistant_generate.py
本测试不触网：验证"未配置"自愈契约，并用假客户端验证预算 / 超时 / 截断标记。
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server import assistant

failures = []


def check(name, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {name}  {detail}")
    if not ok:
        failures.append(name)


# 强制未配置态：清掉可能存在的密钥
os.environ.pop("ASSISTANT_API_KEY", None)

# --- 1. 存在专用异常类型 ---
check("有 AssistantNotConfigured 异常类",
      isinstance(getattr(assistant, "AssistantNotConfigured", None), type))

# --- 2. 未配置密钥 → 抛 AssistantNotConfigured（不返回、不静默、不抛别的）---
raised = None
try:
    assistant.generate("system prompt", "user question")
except Exception as e:  # noqa: BLE001
    raised = e
check("未配置密钥时抛 AssistantNotConfigured",
      isinstance(raised, getattr(assistant, "AssistantNotConfigured", ())),
      f"got={type(raised).__name__ if raised else None}")

# --- 3. 错误信息可指导接入（提到 .env / 密钥 / 配置）---
msg = str(raised) if raised else ""
check("错误信息含接入指引", any(k in msg for k in ("ASSISTANT_API_KEY", ".env", "配置", "未配置")),
      f"msg={msg!r}")


# --- 4. 已配置（假客户端，不触网）：预算 / 超时 / 截断标记 ---
import types  # noqa: E402

import anthropic  # noqa: E402
import httpx  # noqa: E402

seen = []
_429 = anthropic.RateLimitError(
    "UID rate limit reached for TPD", body=None,
    response=httpx.Response(429, request=httpx.Request("POST", "http://relay.test/v1/messages")))


def _fake_client(stop, limited=None):
    """假 anthropic.Anthropic：记下每次 create 的参数；model == limited 时抛 429。"""
    def create(**kw):
        seen.append(kw)
        if limited and kw["model"] == limited:
            raise _429
        return types.SimpleNamespace(
            stop_reason=stop, usage=types.SimpleNamespace(input_tokens=1, output_tokens=2),
            content=[types.SimpleNamespace(type="thinking", thinking="…"),
                     types.SimpleNamespace(type="text", text="正文")])
    return lambda **_: types.SimpleNamespace(messages=types.SimpleNamespace(create=create))


_orig_cls = anthropic.Anthropic
os.environ["ASSISTANT_API_KEY"] = "test-key"
try:
    anthropic.Anthropic = _fake_client("end_turn")
    out = assistant.generate("s", "u")
    kw = seen[-1] if seen else {}
    check("默认 max_tokens ≥ 32000（thinking+正文共用，16000 实测吃满）", kw.get("max_tokens", 0) >= 32000,
          f"max_tokens={kw.get('max_tokens')}")
    t = kw.get("timeout")
    check("显式传 timeout（>21k 非流式否则被 SDK 拒）：读 ≥900s、连接仍 ≤10s",
          getattr(t, "read", 0) >= 900 and 0 < (getattr(t, "connect", None) or 999) <= 10, f"timeout={t!r}")
    check("正常结束 → 原样返回正文", out == "正文", repr(out))

    anthropic.Anthropic = _fake_client("max_tokens")
    out = assistant.generate("s", "u")
    check("stop_reason=max_tokens → 正文末尾标注截断", out.startswith("正文") and "截断" in out, repr(out))

    # --- 5. 429（中转余额不足）→ 直接转 AssistantError，不换模型重试（用户定：失败就失败）---
    anthropic.Anthropic = _fake_client("end_turn", limited=assistant.ASSISTANT_MODEL)
    seen.clear()
    raised = None
    try:
        assistant.generate("s", "u")
    except Exception as e:  # noqa: BLE001
        raised = e
    check("429 → AssistantError", isinstance(raised, assistant.AssistantError),
          f"got={type(raised).__name__ if raised else None}")
    check("429 后不换模型重试：只打主模型一次", [k["model"] for k in seen] == [assistant.ASSISTANT_MODEL],
          f"models={[k['model'] for k in seen]}")
finally:
    anthropic.Anthropic = _orig_cls
    os.environ.pop("ASSISTANT_API_KEY", None)


if failures:
    print(f"\n{len(failures)} 项失败: {failures}")
    sys.exit(1)
print("\n全部通过。")
