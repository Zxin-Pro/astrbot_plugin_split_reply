# -*- coding: utf-8 -*-
"""
astrbot_plugin_split_reply

非流式输出 + 换行截断 + 分段发送插件。

功能：
1. 监听 LLM 回复事件（@filter.on_llm_response()），拿到【完整的】回复文本。
2. 按换行符（或空行）把回复拆分成多个段落，过滤空行 / 纯空白行，并 strip 每段。
3. 逐条发送给用户，每条之间加入可配置的短延迟，避免触发平台风控。
4. 拆分后只有一条消息时正常发送该条。
5. 全程异常兜底，任何一步失败都不会导致机器人崩溃或静默。

工作原理（基于 AstrBot v4.28+ 源码核实）：
- 非流式模式下，Agent 在 step() 结束时的执行顺序是：
    1) _complete_with_assistant_response():
       - 先把 assistant 消息写入会话上下文（历史不受本插件影响）
       - 再触发 on_agent_done -> OnLLMResponseEvent（即本插件的 on_llm_response 钩子）
    2) 然后才会根据 llm_resp.result_chain / completion_text 构建 chain 并 yield，
       最终由 RespondStage 发送给用户。
- 因此在本钩子中：
    a) 先用 event.send() 把分段消息逐条发出；
    b) 再把 resp.completion_text / resp.result_chain 清空，
       这样后续管线拿到空 chain，RespondStage 会直接跳过发送，
       天然避免"分段发完之后又把整段原文再发一遍"的重复问题。
- 若用户在 AstrBot 配置中开启了流式输出（provider_settings.streaming_response = true），
  在本钩子触发时内容已经通过 send_streaming 推送出去了，此时插件会：
    - 跳过分段逻辑（避免重复发送），并在日志中提示关闭流式输出。
  AstrBot 默认 streaming_response = false（非流式），一般无需修改；
  如已手动开启，请在 WebUI → 配置 → 其他配置 中关闭"流式回复"。
"""

import asyncio
import re
from typing import List, Optional

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.event.filter import EventMessageType
from astrbot.core.message.message_event_result import (
    MessageChain,
    ResultContentType,
)
from astrbot.api.star import Context, Star, register

# 拆分模式常量
MODE_NEWLINE = "newline"        # 按单个换行符拆分
MODE_BLANK_LINE = "blank_line"  # 按空行（\n\s*\n）拆分，保留段内换行

# 流式策略常量
STRATEGY_NON_STREAMING = "force_non_streaming"  # 强制非流式，拿完整文本后分段
STRATEGY_STREAMING = "streaming_split"          # 保持流式，实时按换行分段

# 需要跳过、不当回复内容发送的 chain 类型（思考过程 / 运行统计）
_SKIP_CHAIN_TYPES = ("reasoning", "agent_stats")


def split_text(text: str, mode: str = MODE_NEWLINE) -> List[str]:
    """把完整回复文本拆分成段落列表（纯函数，方便单测）。

    Args:
        text: LLM 的完整回复文本
        mode: "newline" 按换行符拆分；"blank_line" 按空行拆分

    Returns:
        过滤掉空行/纯空白行并 strip 后的段落列表；text 为空时返回 []
    """
    if not text or not text.strip():
        return []

    if mode == MODE_BLANK_LINE:
        # 按空行拆分：一个或多个"只含空白字符"的行视为分隔符
        raw_parts = re.split(r"\n\s*\n", text)
    else:
        # 按单个换行符拆分（\r\n / \r / \n 都兼容）
        raw_parts = re.split(r"\r\n|\r|\n", text)

    # 过滤空串与纯空白串，并去除每段首尾空白
    return [part.strip() for part in raw_parts if part and part.strip()]


@register(
    "astrbot_plugin_split_reply",
    "Zxin-Pro",
    "非流式/流式/原生流式三模式换行分段发送插件",
    "1.3.0",
    "https://github.com/Zxin-Pro/astrbot_plugin_split_reply",
)
class SplitReplyPlugin(Star):
    """非流式输出 + 换行截断 + 分段发送插件主体。"""

    def __init__(self, context: Context, config: dict = None):
        super().__init__(context)
        self.config = config or {}

        # 拆分模式：newline（按换行）/ blank_line（按空行）
        self.split_mode: str = self.config.get("split_mode", MODE_NEWLINE)
        if self.split_mode not in (MODE_NEWLINE, MODE_BLANK_LINE):
            logger.warning(
                f"[split_reply] 未知的 split_mode '{self.split_mode}'，回退为 newline"
            )
            self.split_mode = MODE_NEWLINE

        # 每条消息之间的发送延迟（秒），防止触发平台风控
        try:
            self.delay_seconds: float = float(self.config.get("delay_seconds", 0.5))
        except (TypeError, ValueError):
            logger.warning("[split_reply] delay_seconds 配置非法，回退为 0.5")
            self.delay_seconds = 0.5
        if self.delay_seconds < 0:
            self.delay_seconds = 0.0

        # 单条消息最大长度，超过则截断（0 表示不限制）
        try:
            self.max_length: int = int(self.config.get("max_length", 0))
        except (TypeError, ValueError):
            self.max_length = 0

        # 流式策略：
        # - force_non_streaming: 在消息入口把本条消息标记为非流式（internal.py:167
        #   支持事件级 enable_streaming 覆盖），LLM 完整返回后在 on_llm_response 分段。
        # - streaming_split: 保持流式输出，接管 event.send_streaming，
        #   边接收流式增量边按换行实时分段发送（打字机效果保留，按行出消息）。
        self.streaming_strategy: str = self.config.get(
            "streaming_strategy", STRATEGY_NON_STREAMING
        )
        if self.streaming_strategy not in (STRATEGY_NON_STREAMING, STRATEGY_STREAMING):
            logger.warning(
                f"[split_reply] 未知的 streaming_strategy '{self.streaming_strategy}'，"
                f"回退为 {STRATEGY_NON_STREAMING}"
            )
            self.streaming_strategy = STRATEGY_NON_STREAMING

        # 平台原生流式分段：
        # auto = QQ 官方机器人自动启用（C2C 私聊原生流式，客户端逐字蹦出）；
        # on = 所有平台都走原生流式通道；off = 只用普通发送。
        self.native_stream: str = self.config.get("native_stream", "auto")
        if self.native_stream not in ("auto", "on", "off"):
            logger.warning(
                f"[split_reply] 未知的 native_stream '{self.native_stream}'，回退为 auto"
            )
            self.native_stream = "auto"

        logger.info(
            f"[split_reply] 已加载 split_mode={self.split_mode} "
            f"delay={self.delay_seconds}s max_length={self.max_length} "
            f"streaming_strategy={self.streaming_strategy} "
            f"native_stream={self.native_stream}"
        )

    def _should_use_native_stream(self, event: AstrMessageEvent) -> bool:
        """判断分段是否走「平台原生流式」通道。

        auto：QQ 官方机器人（qq_official / qq_official_webhook）自动开启，
              其 C2C 流式协议是一条消息被逐帧刷新，客户端呈现逐字蹦出的效果。
        """
        if self.native_stream == "off":
            return False
        if self.native_stream == "on":
            return True
        try:
            name = str(event.get_platform_name() or "")
        except Exception:
            name = ""
        return "qq_official" in name

    # ------------------------------------------------------------------
    # 消息入口：按策略处理流式
    # ------------------------------------------------------------------
    @filter.event_message_type(EventMessageType.ALL)
    async def on_message_entry(self, event: AstrMessageEvent):
        """消息事件入口，在管线进入 LLM/Respond 阶段之前处理流式策略。"""
        try:
            if self.streaming_strategy == STRATEGY_NON_STREAMING:
                # 强制本条消息走非流式分支
                event.set_extra("enable_streaming", False)
            else:
                # 保持流式，接管 send_streaming 实现按换行实时分段
                self._patch_send_streaming(event)
        except Exception as e:
            logger.warning(f"[split_reply] 消息入口处理失败: {e}")

    # ------------------------------------------------------------------
    # 流式分段核心
    # ------------------------------------------------------------------
    def _patch_send_streaming(self, event: AstrMessageEvent) -> None:
        """把 event.send_streaming 替换为「按换行实时分段」实现。

        只替换当前事件实例的绑定方法，不影响其他事件/插件。
        原实现的 aiocqhttp fallback 只会按句号（。？！~…）切分或不切分，
        均不满足按换行分段的需求，故完全接管发送逻辑。
        """
        if getattr(event, "_split_reply_patched", False):
            return  # 防止同一事件被重复 patch
        orig_send_streaming = getattr(event, "send_streaming", None)

        async def patched_send_streaming(generator, use_fallback=False, *a, **k):
            use_native = self._should_use_native_stream(event)
            try:
                if use_native and orig_send_streaming is not None:
                    # 走平台原生流式：每条分段各自开一条流式消息（QQ 官方逐字蹦出）
                    await self._streaming_native_split_send(
                        event, generator, orig_send_streaming
                    )
                else:
                    await self._streaming_split_send(event, generator)
            except Exception as e:
                logger.error(f"[split_reply] 流式分段发送异常: {e}")
            finally:
                # 普通模式下调用原实现收尾（空生成器），保留指标上报等副作用；
                # 原生模式已逐段调用过原实现，无需再补空帧。
                if not use_native and orig_send_streaming is not None:

                    async def _empty():
                        return
                        yield  # pragma: no cover

                    try:
                        await orig_send_streaming(_empty(), use_fallback)
                    except Exception:
                        pass

        event.send_streaming = patched_send_streaming
        event._split_reply_patched = True

    async def _streaming_native_split_send(
        self, event: AstrMessageEvent, generator, orig_send_streaming
    ) -> None:
        """把流式增量按换行切成若干「原生流式分段」，逐段交给平台自带的流式通道发送。

        与 _streaming_split_send 的区别：普通模式用 event.send() 一次发一条；
        本模式每段自己开一条平台流式消息（例如 QQ 官方 C2C 的 state=1 → state=10 分片
        刷新），客户端呈现官方流式那种逐字蹦出的效果。

        实现：一个 pump 协程持续消费上游增量并把「已完成的整行」放进各段的队列
        （放完立即塞入结束哨兵 None），主流程按顺序对每段调用一次原 send_streaming，
        因此每段都是独立的一条流式消息，且不需要等整轮 LLM 结束才收尾。
        """
        queues: list[asyncio.Queue] = []
        state = {"sent": 0}
        pending = ""

        def new_queue_with(item) -> asyncio.Queue:
            q: asyncio.Queue = asyncio.Queue()
            q.put_nowait(item)
            q.put_nowait(None)  # 该段内容已完整，立即结束这条流式消息
            queues.append(q)
            return q

        async def emit_segment(seg: str) -> None:
            seg = self._truncate(seg)
            if seg:
                new_queue_with(MessageChain().message(seg))

        async def pump() -> None:
            nonlocal pending
            try:
                async for chain in generator:
                    chain_type = getattr(chain, "type", None)
                    if chain_type in _SKIP_CHAIN_TYPES:
                        continue
                    if chain_type == "break":
                        # 工具调用分隔信号：交给平台流式实现收尾当前段
                        new_queue_with(chain)
                        continue

                    comps = list(getattr(chain, "chain", None) or [])
                    texts, others = [], []
                    for comp in comps:
                        if comp.__class__.__name__ == "Plain":
                            texts.append(getattr(comp, "text", "") or "")
                        else:
                            others.append(comp)
                    if others:
                        if pending.strip():
                            await emit_segment(pending.strip())
                            pending = ""
                        new_queue_with(MessageChain(chain=list(others)))
                    pending += "".join(texts)
                    pending = pending.replace("\r\n", "\n").replace("\r", "\n")
                    while "\n" in pending:
                        seg, pending = pending.split("\n", 1)
                        if seg.strip():
                            await emit_segment(seg.strip())
            finally:
                if pending.strip():
                    try:
                        await emit_segment(pending.strip())
                    except Exception as e:
                        logger.error(f"[split_reply] 流式尾段发送失败: {e}")

        pump_task = asyncio.create_task(pump())
        idx = 0
        try:
            while True:
                # 等待 pump 产出下一段队列
                while idx >= len(queues) and not pump_task.done():
                    await asyncio.sleep(0.02)
                if idx >= len(queues):
                    break
                q = queues[idx]
                idx += 1

                async def queue_gen(q=q):
                    while True:
                        item = await q.get()
                        if item is None:
                            return
                        yield item

                if state["sent"] > 0 and self.delay_seconds > 0:
                    await asyncio.sleep(self.delay_seconds)
                try:
                    await orig_send_streaming(queue_gen(), False)
                    state["sent"] += 1
                except Exception as e:
                    logger.error(f"[split_reply] 原生流式分段发送失败: {e}")
        finally:
            if not pump_task.done():
                try:
                    await pump_task
                except Exception as e:
                    logger.error(f"[split_reply] 流式 pump 异常: {e}")

    async def _streaming_split_send(self, event: AstrMessageEvent, generator) -> None:
        """消费流式增量，按换行实时分段发送。

        - 文本增量跨 chunk 累积，遇到换行立即把已完成的行作为独立消息发出
        - 纯空白行不发送
        - 思考过程(reasoning)/运行统计(agent_stats) 不发送
        - 图片等非文本组件单独发送
        """
        state = {"sent": 0}  # 已发送条数，用于控制条间延迟
        pending = ""

        async for chain in generator:
            chain_type = getattr(chain, "type", None)
            if chain_type in _SKIP_CHAIN_TYPES:
                continue

            comps = list(getattr(chain, "chain", None) or [])
            texts, others = [], []
            for comp in comps:
                if comp.__class__.__name__ == "Plain":
                    texts.append(getattr(comp, "text", "") or "")
                else:
                    others.append(comp)

            # 非文本组件：先清空已积压的文本再发送组件，保持顺序
            if others:
                if pending.strip():
                    await self._send_segment(event, pending.strip(), state)
                    pending = ""
                for comp in others:
                    await event.send(MessageChain(chain=[comp]))
                    state["sent"] += 1

            pending += "".join(texts)
            pending = pending.replace("\r\n", "\n").replace("\r", "\n")

            # 每遇到一个换行，就把该行作为独立消息发出
            while "\n" in pending:
                seg, pending = pending.split("\n", 1)
                if seg.strip():
                    await self._send_segment(event, seg.strip(), state)

        # 收尾：发送最后一段（流结束时剩余内容）
        if pending.strip():
            await self._send_segment(event, pending.strip(), state)

    async def _send_segment(self, event: AstrMessageEvent, seg: str, state: dict):
        """发送单条分段消息，条与条之间加延迟。"""
        seg = self._truncate(seg)
        if not seg:
            return
        if state["sent"] > 0 and self.delay_seconds > 0:
            await asyncio.sleep(self.delay_seconds)
        await event.send(MessageChain().message(seg))
        state["sent"] += 1

    # ------------------------------------------------------------------
    # 工具方法
    # ------------------------------------------------------------------
    def _truncate(self, seg: str) -> str:
        """按 max_length 配置截断单条消息（0 表示不截断）。"""
        if self.max_length <= 0 or len(seg) <= self.max_length:
            return seg
        return seg[: self.max_length]

    def _is_streaming(self, event: AstrMessageEvent) -> bool:
        """判断当前是否处于流式输出模式。

        非流式模式下，on_llm_response 触发时 event 还没有设置结果（或结果为空）；
        流式模式下，internal agent stage 在执行 agent 前就已经把结果设置为
        STREAMING_RESULT 并开始通过 send_streaming 推送，据此区分。
        """
        try:
            result = event.get_result()
            return (
                result is not None
                and result.result_content_type == ResultContentType.STREAMING_RESULT
            )
        except Exception:
            # 读取结果失败时保守起见按非流式处理
            return False

    def _extract_text(self, resp) -> Optional[str]:
        """从 LLMResponse 中提取完整回复文本。优先 completion_text，
        其次尝试从 result_chain 提取纯文本。"""
        text = getattr(resp, "completion_text", None)
        if text and text.strip():
            return text

        result_chain = getattr(resp, "result_chain", None)
        if result_chain is not None:
            try:
                text = result_chain.get_plain_text()
                if text and text.strip():
                    return text
            except Exception:
                pass
        return None

    def _clear_resp(self, resp) -> None:
        """清空 LLMResponse 的文本内容，阻止管线把整段原文再发送一遍。

        注意：此时 assistant 消息已经写入会话上下文（见 _complete_with_assistant_response
        的源码顺序），所以清空不影响多轮对话记忆。
        """
        try:
            resp.completion_text = ""
        except Exception:
            logger.warning("[split_reply] 清空 completion_text 失败")
        try:
            resp.result_chain = None
        except Exception:
            pass

    # ------------------------------------------------------------------
    # 核心：LLM 回复钩子（必须是普通协程，不能是 async generator）
    # ------------------------------------------------------------------
    @filter.on_llm_response()
    async def on_llm_response(self, event: AstrMessageEvent, resp):
        """LLM 回复钩子：拿到完整回复后分段发送。

        Args:
            event: 消息事件对象
            resp: LLMResponse 对象，包含完整的 completion_text
        """
        try:
            # 1. 流式模式下内容已经推送出去了，这里不做任何处理，避免重复发送
            if self._is_streaming(event):
                logger.debug(
                    "[split_reply] 检测到流式输出模式，跳过分段发送。"
                    "如需分段请在 WebUI 关闭『流式回复』(streaming_response)"
                )
                return

            # 2. 提取完整回复文本
            text = self._extract_text(resp)
            if text is None:
                return  # 空回复，交给默认流程

            # 3. 按配置拆分，过滤空行 / 纯空白行
            segments = split_text(text, self.split_mode)
            if not segments:
                return  # 拆不出有效内容，交给默认流程

            # 4. 逐条发送，条与条之间加入延迟
            sent_count = 0
            for i, seg in enumerate(segments):
                seg = self._truncate(seg)
                if not seg:
                    continue
                try:
                    await event.send(MessageChain().message(seg))
                    sent_count += 1
                except Exception as e:
                    logger.error(f"[split_reply] 第 {i + 1} 条消息发送失败: {e}")
                # 最后一条之后不需要延迟
                if i < len(segments) - 1 and self.delay_seconds > 0:
                    await asyncio.sleep(self.delay_seconds)

            # 5. 只要成功发出过至少一条，就清空原始回复，避免默认流程再发一遍整段文本
            #    （若一条都没发出去，保留原文让默认流程兜底，保证用户至少能收到回复）
            if sent_count > 0:
                self._clear_resp(resp)
                logger.debug(
                    f"[split_reply] 已分段发送 {sent_count}/{len(segments)} 条消息"
                )
            else:
                logger.error("[split_reply] 所有分段均发送失败，回退为默认整段发送")

        except Exception as e:
            # 任何异常都不能影响主流程；钩子外层虽有 try/except，
            # 但这里再兜一层，保证 resp 未被清空时默认发送仍可进行
            logger.error(f"[split_reply] on_llm_response 处理异常: {e}")

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    async def terminate(self):
        """插件卸载时的清理逻辑（当前无持久化资源需要释放）。"""
        logger.info("[split_reply] 插件已卸载")
