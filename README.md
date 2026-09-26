# astrbot_plugin_split_reply

非流式输出 + 换行截断 + 分段发送插件。当 LLM 生成回复后，先等待完整返回，再按换行符把回复拆分成多个段落，过滤空行，逐条发送给用户。

## 效果示例

LLM 原始回复：

```
刚在打游戏呢 没看手机

你清理对话干嘛 我不是还在吗
```

实际发送：

```
刚在打游戏呢 没看手机
你清理对话干嘛 我不是还在吗
```

（两条独立消息，中间间隔 delay_seconds 秒）

## 安装方法

1. 在 AstrBot WebUI → 插件管理 → 从仓库安装，搜索 `split_reply`；
   或手动把本插件目录放入 `data/plugins/astrbot_plugin_split_reply/`。
2. 重载插件即可生效，无需任何指令。

## 重要：非流式输出配置

本插件基于 `@filter.on_llm_response()` 钩子工作，该钩子在 LLM 完整返回后才触发。

- **v1.2.0 起完整支持流式输出**：`streaming_strategy` 二选一：
  - `force_non_streaming`（默认）：插件在消息入口把本条消息标记为非流式，多行内容不再被流式缓冲策略合并成一条，无需手动关闭 WebUI 的「流式回复」。
  - `streaming_split`：保持流式打字机效果，插件接管 `send_streaming`，每写完一行就实时发出该行（空行过滤、条间延迟照常生效）。
- 两种策略下插件都会检测当前是否处于流式路径并自动选择正确的分段方式，不会重复发送。

## QQ 官方机器人（逐字蹦出效果）

QQ 官方机器人的流式是一条消息被逐帧刷新（C2C 私聊协议 `state=1` → `state=10`），客户端表现为**一字一字蹦出来**。

- `native_stream: auto`（默认）时，检测到 QQ 官方平台会**自动**把每条分段交给平台原生流式通道发送：一句话开一条流式消息、逐渐刷出，而不是发普通消息
- 想让所有平台都走原生流式通道 → 设 `on`；想全部走普通发送 → 设 `off`
- 注意：AstrBot 侧对 QQ 流式分片有 1 秒节流（`throttle_interval = 1`），逐字动画的节奏由 QQ 客户端 + 该节流共同决定，插件侧不做额外拉长

## 配置说明

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `streaming_strategy` | string | `force_non_streaming` | 流式策略：`force_non_streaming` 强制本条消息走非流式，LLM 完整返回后分段（推荐，稳定）；`streaming_split` 保持流式打字机效果，按换行实时分段发送 |
| `native_stream` | string | `auto` | 原生流式分段：`auto`=QQ 官方机器人（`qq_official`/`qq_official_webhook`）自动启用，每条分段走平台原生流式通道（C2C 私聊逐字蹦出效果）；`on`=所有平台都走原生流式；`off`=只用普通发送 |
| `split_mode` | string | `newline` | 拆分模式：`newline` 按单个换行符拆分；`blank_line` 按空行拆分（`re.split(r'\n\s*\n', text)`），保留段内换行 |
| `delay_seconds` | float | `0.5` | 每条分段消息之间的发送延迟（秒），防止触发平台风控 |
| `max_length` | int | `0` | 单条消息最大长度（字符数），超过截断；`0` 不限制 |

## 工作原理（防重复发送）

基于 AstrBot v4.28+ 源码核实，非流式模式下 Agent 的执行顺序为：

1. `_complete_with_assistant_response()` 先把 assistant 消息写入会话上下文（多轮记忆不受影响），再触发 `on_agent_done` → `OnLLMResponseEvent`（即本插件钩子）；
2. 之后才会根据 `completion_text` 构建 chain 并交给 RespondStage 发送。

因此插件在钩子中：先用 `event.send(MessageChain().message(seg))` 逐条发出分段消息，再把 `resp.completion_text` 清空 —— 后续管线拿到空 chain，RespondStage 直接跳过，天然避免"分段发完又发一遍整段原文"。

异常兜底：如果所有分段都发送失败，插件会保留原始回复，回退为默认的整段发送，保证用户至少能收到回复。

## 开发调试

- 单元测试：`python3 tests/test_split_reply.py`（无需 AstrBot 环境，stub 模式）

## 作者

Zxin-Pro
