# 更新日志

## v1.2.0 (2026-09-17)

### 新增

- **流式回复分段支持**：新增 `streaming_strategy` 配置，两种策略二选一：
  - `force_non_streaming`（默认）：强制本条消息走非流式，LLM 完整返回后按换行分段（最稳定）
  - `streaming_split`：**保持流式打字机效果**，插件接管 `event.send_streaming`，边接收流式增量边按换行实时分段发送——每写完一行就立刻发出该行，空行自动过滤，条间保留 delay_seconds 延迟
- 流式分段同样支持：跨 chunk 文本累积（增量把一行切在多个 chunk 里也能正确拼接）、CRLF 兼容、max_length 截断
- 思考过程（reasoning）与运行统计（agent_stats）不作为回复发送；图片等非文本组件单独发送并保持顺序
- 同一事件防重复接管；接管异常时保留原实现收尾副作用
- 新增 13 项流式分段单测（总计 55 项全部通过）

### 变更

- 原 `force_non_streaming` 布尔配置替换为 `streaming_strategy` 字符串配置（旧配置键自动忽略，用默认值）

## v1.1.0 (2026-09-17)

### 新增

- **流式输出兼容**：新增 `force_non_streaming` 配置（默认开启）。开启流式输出时，多行内容会被流式缓冲策略合并成一条消息且发出时机早于插件钩子；现在插件会在消息事件入口把本条消息标记为非流式（利用 AstrBot 事件级 `enable_streaming` 覆盖，internal.py:167），保证分段生效，无需手动关闭 WebUI 的「流式回复」
- 新增 3 项监听器单测（总计 42 项全部通过）

## v1.0.0 (2026-09-17)

首个正式版本。

### 功能

- 监听 LLM 回复（`@filter.on_llm_response()`），等待完整返回后按换行符拆分成多条消息逐条发送
- 支持两种拆分模式：`newline`（按单个换行符）/ `blank_line`（按空行，保留段内换行）
- 自动过滤空行与纯空白行，去除每条消息首尾空白，保留段内正常空格与标点
- 每条消息之间加入可配置延迟（默认 0.5 秒），防止触发平台风控
- 拆分后仅一条消息时正常发送该条
- 单条消息可配置最大长度截断（默认不限制）

### 防重复发送机制

基于 AstrBot v4.28 源码核实：钩子触发时 assistant 消息已写入会话历史，插件分段发送后清空 `resp.completion_text`，后续管线拿到空 chain 自动跳过默认发送，整段原文不会重发，且多轮对话记忆不受影响。

### 兼容性

- AstrBot 默认即非流式输出，无需改配置；若开启了「流式回复」，插件会自动检测并跳过分段（日志提示），避免内容重复
- `MessageChain` 从 `astrbot.core.message.message_event_result` 导入，兼容 v4.28+

### 异常兜底

- 所有分段发送失败 → 保留原始回复，回退默认整段发送
- 部分失败 → 清空原文防止重复
- 全程 try/except，不影响主流程

### 修复

- 清空 requirements.txt，修复「Failed to install dependencies」（中文说明被误当 pip 包名）
- MessageChain 导入路径改用内核模块，修复 `No module named 'astrbot.api.message'`

### 已知限制

- metadata.yaml 的 `name` 必须保持与目录名一致的英文标识，改中文会导致插件无法加载（中文名可放 desc 展示）

### 测试

- 39 项单元测试全部通过（stub 模式，无需 AstrBot 环境），覆盖拆分规则、钩子行为、配置项、延迟与异常兜底
