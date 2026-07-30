"""AutoLoop — Chat 模块

提供多轮交互式聊天能力 (`autoloop chat`)。

模块结构：
    events.py        — ChatEvent 事件类型定义（Session ↔ Renderer 协议）
    session.py       — ChatSession SDK 会话封装（Phase 2 过渡态）
    renderer.py      — ChatRenderer 终端渲染器
    commands.py      — 斜杠命令系统
    repl.py          — ChatCLI REPL 主循环
    session_store.py — FileSessionStore 会话存储实现
"""
