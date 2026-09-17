# -*- coding: utf-8 -*-
"""astrbot_plugin_split_reply 单元测试（stub astrbot 模块，无需 AstrBot 环境）"""
import asyncio
import os
import sys
import types
import time

# ------------------------------------------------------------------
# 构造 stub astrbot 全家桶
# ------------------------------------------------------------------
astrbot = types.ModuleType("astrbot")


class _Logger:
    def info(self, *a, **k):
        pass

    warning = info
    error = info
    debug = info


logger_mod = types.ModuleType("astrbot.api")
logger_mod.logger = _Logger()


class _Filter:
    def on_llm_response(self, *a, **k):
        def deco(fn):
            fn._is_hook = True
            return fn

        return deco


event_mod = types.ModuleType("astrbot.api.event")
event_mod.filter = _Filter()
event_mod.AstrMessageEvent = object


class ResultContentType:
    STREAMING_RESULT = "streaming_result"
    LLM_RESULT = "llm_result"
    GENERAL_RESULT = "general_result"


class MessageChain:
    def __init__(self, *a, **k):
        self.sent_chain = []

    def message(self, text):
        self.sent_chain.append(text)
        return self


mer_mod = types.ModuleType("astrbot.api.message.message_event_result")
mer_mod.MessageChain = MessageChain
mer_mod.ResultContentType = ResultContentType

star_mod = types.ModuleType("astrbot.api.star")


class Context:
    pass


class Star:
    def __init__(self, context):
        self.context = context


star_mod.Context = Context
star_mod.Star = Star


def _register(*a, **k):
    def deco(cls):
        return cls

    return deco


star_mod.register = _register

sys.modules["astrbot"] = astrbot
sys.modules["astrbot.api"] = logger_mod
sys.modules["astrbot.api.event"] = event_mod
sys.modules["astrbot.api.message"] = types.ModuleType("astrbot.api.message")
sys.modules["astrbot.api.message.message_event_result"] = mer_mod
sys.modules["astrbot.api.star"] = star_mod

PLUGIN_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, PLUGIN_DIR)

import main  # noqa: E402

_loop = asyncio.new_event_loop()
asyncio.set_event_loop(_loop)


def run(coro):
    return _loop.run_until_complete(coro)


PASS = 0
FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name}")


class FakeLLMResponse:
    def __init__(self, text="", result_chain=None):
        self.completion_text = text
        self.result_chain = result_chain


class FakeChain:
    """模拟 result_chain（带 get_plain_text）"""

    def __init__(self, text):
        self._text = text

    def get_plain_text(self, *a, **k):
        return self._text


class FakeResult:
    def __init__(self, content_type=None):
        self.result_content_type = content_type


class FakeEvent:
    def __init__(self, streaming=False):
        self.sent = []
        self._result = FakeResult("streaming_result" if streaming else None)

    async def send(self, chain):
        self.sent.extend(chain.sent_chain)

    def get_result(self):
        return self._result


class FakeConfig(dict):
    pass


async def run_hook(text="", mode=None, streaming=False, fail_send=False, config=None):
    """构造插件实例并触发 on_llm_response 钩子，返回 (event, resp)"""
    cfg = FakeConfig()
    if config:
        cfg.update(config)
    if mode:
        cfg["split_mode"] = mode
    plugin = main.SplitReplyPlugin(main.Context(), cfg)
    if fail_send:

        async def bad_send(chain):
            raise RuntimeError("platform blocked")

        plugin_send_target = plugin
        ev = FakeEvent(streaming=streaming)
        ev.send = bad_send
    else:
        ev = FakeEvent(streaming=streaming)
    resp = FakeLLMResponse(text)
    await plugin.on_llm_response(ev, resp)
    return ev, resp


# ------------------------------------------------------------------
# 1. split_text 纯函数测试
# ------------------------------------------------------------------
print("[1] split_text 纯函数")
s = main.split_text("刚在打游戏呢 没看手机\n\n你清理对话干嘛 我不是还在吗")
check("空行拆分为两条", s == ["刚在打游戏呢 没看手机", "你清理对话干嘛 我不是还在吗"])

s = main.split_text("a\n\n\n   \nb", "newline")
check("过滤纯空白行", s == ["a", "b"])

s = main.split_text("第一段\n第二段\n第三段")
check("换行拆分三条", s == ["第一段", "第二段", "第三段"])

s = main.split_text("  首尾空白  ", "newline")
check("strip 首尾空白", s == ["首尾空白"])

s = main.split_text("a\n b c , d! \nb", "newline")
check("保留段内空格标点", s == ["a", "b c , d!", "b"])

s = main.split_text("段1行1\n段1行2\n\n段2", "blank_line")
check("blank_line 模式保留段内换行", s == ["段1行1\n段1行2", "段2"])

s = main.split_text("", "newline")
check("空文本 → []", s == [])

s = main.split_text("   \n\n  \t ", "newline")
check("纯空白文本 → []", s == [])

s = main.split_text("只此一句")
check("单行 → 一条", s == ["只此一句"])

s = main.split_text("a\r\nb\rc")
check("兼容 CRLF / CR", s == ["a", "b", "c"])

s = main.split_text("a\n\n\nb", "unknown_mode")
check("未知模式回退 newline", s == ["a", "b"])

# ------------------------------------------------------------------
# 2. 钩子行为测试
# ------------------------------------------------------------------
print("[2] on_llm_response 钩子行为")

ev, resp = run(run_hook("刚在打游戏呢 没看手机\n\n你清理对话干嘛 我不是还在吗"))
check("逐条发送两条", ev.sent == ["刚在打游戏呢 没看手机", "你清理对话干嘛 我不是还在吗"])
check("发送后清空 completion_text", resp.completion_text == "")
check("清空 result_chain", resp.result_chain is None)

ev, resp = run(run_hook("刚在打游戏呢 没看手机\n\n你清理对话干嘛 我不是还在吗", mode="blank_line"))
check("blank_line 模式下两条一起发", len(ev.sent) == 2)

ev, resp = run(run_hook("只有一条消息"))
check("单条消息正常发送", ev.sent == ["只有一条消息"])
check("单条消息也清空原文", resp.completion_text == "")

ev, resp = run(run_hook(""))
check("空回复不发送", ev.sent == [])
check("空回复不清空 resp", resp.completion_text == "")

ev, resp = run(run_hook("   \n\n  "))
check("纯空白回复不发送", ev.sent == [])

ev, resp = run(run_hook("流式模式的内容", streaming=True))
check("流式模式跳过分段", ev.sent == [])
check("流式模式不清空 resp", resp.completion_text == "流式模式的内容")

ev, resp = run(run_hook("aaa\nbbb", fail_send=True))
check("全部发送失败回退默认流程", resp.completion_text == "aaa\nbbb")

# result_chain 兜底提取
plugin = main.SplitReplyPlugin(main.Context(), FakeConfig())
ev = FakeEvent()
resp = FakeLLMResponse(text="", result_chain=FakeChain("来自result_chain\n第二行"))
run(plugin.on_llm_response(ev, resp))
check("result_chain 兜底提取并分段", ev.sent == ["来自result_chain", "第二行"])

# ------------------------------------------------------------------
# 3. 配置项测试
# ------------------------------------------------------------------
print("[3] 配置项")

plugin = main.SplitReplyPlugin(main.Context(), {"split_mode": "blank_line"})
check("split_mode=blank_line 生效", plugin.split_mode == "blank_line")

plugin = main.SplitReplyPlugin(main.Context(), {"split_mode": "bad"})
check("非法 split_mode 回退 newline", plugin.split_mode == "newline")

plugin = main.SplitReplyPlugin(main.Context(), {"delay_seconds": "1.2"})
check("delay_seconds 字符串转换", plugin.delay_seconds == 1.2)

plugin = main.SplitReplyPlugin(main.Context(), {"delay_seconds": "abc"})
check("非法 delay_seconds 回退 0.5", plugin.delay_seconds == 0.5)

plugin = main.SplitReplyPlugin(main.Context(), {"delay_seconds": -5})
check("负数 delay 回退 0", plugin.delay_seconds == 0.0)

plugin = main.SplitReplyPlugin(main.Context(), {"max_length": 5})
check("max_length 截断生效", plugin._truncate("1234567890") == "12345")

plugin = main.SplitReplyPlugin(main.Context(), {"max_length": 0})
check("max_length=0 不截断", plugin._truncate("1234567890") == "1234567890")

# 截断在钩子中生效
ev, resp = run(run_hook("aaaaa\nbbbbbb", config={"max_length": 4}))
check("钩子中截断生效", ev.sent == ["aaaa", "bbbb"])

# ------------------------------------------------------------------
# 4. 延迟测试
# ------------------------------------------------------------------
print("[4] 发送延迟")
plugin = main.SplitReplyPlugin(main.Context(), {"delay_seconds": 0.15})
ev = FakeEvent()
resp = FakeLLMResponse("a\nb\nc")
t0 = time.monotonic()
run(plugin.on_llm_response(ev, resp))
elapsed = time.monotonic() - t0
check(f"3条消息间隔2次延迟(实测{elapsed:.2f}s >= 0.25s)", elapsed >= 0.25)
check("3条全部发送", ev.sent == ["a", "b", "c"])

plugin = main.SplitReplyPlugin(main.Context(), {"delay_seconds": 0.05})
ev = FakeEvent()
resp = FakeLLMResponse("单条")
t0 = time.monotonic()
run(plugin.on_llm_response(ev, resp))
elapsed = time.monotonic() - t0
check("单条消息无额外延迟", elapsed < 0.1)

# ------------------------------------------------------------------
# 5. 异常兜底测试
# ------------------------------------------------------------------
print("[5] 异常兜底")


class BadResp:
    """属性全部抛异常的 resp"""

    @property
    def completion_text(self):
        raise RuntimeError("boom")

    @property
    def result_chain(self):
        raise RuntimeError("boom")


plugin = main.SplitReplyPlugin(main.Context(), FakeConfig())
ev = FakeEvent()
try:
    run(plugin.on_llm_response(ev, BadResp()))
    check("恶意 resp 不崩溃", True)
except Exception as e:
    check(f"恶意 resp 不崩溃(异常泄漏: {e})", False)

# send 部分失败：第一条成功第二条失败 → 已发的不重复
plugin = main.SplitReplyPlugin(main.Context(), FakeConfig())
ev = FakeEvent()
call_count = {"n": 0}


async def flaky_send(chain):
    call_count["n"] += 1
    if call_count["n"] >= 2:
        raise RuntimeError("second send failed")
    ev.sent.extend(chain.sent_chain)


ev.send = flaky_send
resp = FakeLLMResponse("a\nb\nc")
run(plugin.on_llm_response(ev, resp))
check("部分失败仍清空原文防重复", resp.completion_text == "")
check("失败前已发送的保留", ev.sent == ["a"])

# terminate 不崩溃
run(plugin.terminate())
check("terminate 正常执行", True)

print(f"\n结果: {PASS} 通过, {FAIL} 失败")
sys.exit(1 if FAIL else 0)
