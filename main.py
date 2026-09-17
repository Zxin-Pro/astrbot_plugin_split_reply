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
from astrbot.core.message.message_event_result import (
    MessageChain,
    ResultContentType,
)
from astrbot.api.star import Context, Star, register

# 拆分模式常量
MODE_NEWLINE = "newline"        # 按单个换行符拆分
MODE_BLANK_LINE = "blank_line"  # 按空行（\n\s*\n）拆分，保留段内换行


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
    "把 LLM 回复按换行拆分成多条消息分段发送（非流式）",
    "1.0.0",
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

        logger.info(
            f"[split_reply] 已加载 split_mode={self.split_mode} "
            f"delay={self.delay_seconds}s max_length={self.max_length}"
        )

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
