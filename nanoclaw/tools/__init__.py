"""Tools module - built-in tools for the agent."""
"""
工具调用时序（单轮）:
1、用户消息进入 Gateway，调用 handle_incoming() 组装 session_id。
代码：gateway.py (line 109)
2、Gateway 调 agent.run() 进入 ReAct 主循环。
代码：agent.py (line 101)
3、Agent 拉取历史和记忆，构建 messages。
代码：agent.py (line 126)、context.py (line 72)
4、Agent 动态筛选本轮工具 schema（不是全量工具）。
代码：context.py (line 177)
5、Agent 调 llm.chat(messages, tools=...)。
代码：agent.py (line 193)、llm.py (line 142)
6、若 LLM 返回 tool_calls，先把 assistant tool-call 消息写回上下文。
代码：agent.py (line 217)
7、Agent 并行执行工具（asyncio.gather）。
代码：agent.py (line 276)、agent.py (line 309)
8、每个工具调用统一走 ToolRegistry.execute(name, args)，再分发到具体工具函数。
代码：registry.py (line 166)
9、工具返回字符串结果（成功/失败），Agent 对结果做“压缩 + 注入防护包装”。
代码：context.py (line 218)、agent.py (line 226)
10、工具结果以 role=tool 形式回填到 messages，再次调用 LLM 生成最终答复。
代码：agent.py (line 233)、agent.py (line 191)
11、最终文本落历史、写审计、返回给通道。
代码：agent.py (line 252)、agent.py (line 264)
"""