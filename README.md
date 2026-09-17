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

- AstrBot **默认就是非流式输出**（`provider_settings.streaming_response = false`），一般无需修改。
- 如果你在 WebUI → 配置 → 其他配置 中开启过「流式回复」，请**关闭**它。
  流式模式下内容已经边生成边推送，插件会检测到并自动跳过分段（避免重复发送），并在日志中给出提示。

## 配置说明

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
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
