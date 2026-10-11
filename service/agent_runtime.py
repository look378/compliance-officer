#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
数字员工对话运行时（本项目自有）

解决什么问题：
    此前"控制数字员工"只能通过合规审查这条固定流水线（提交合同 → 审批）。
    但数字员工的价值之一是**接受自由指令**：查规则、检索法条、改条款、解释判定依据、
    总结某个 run 的过程。本模块把运行时框架的 Agent 能力以"按会话隔离"的方式暴露给工作台。

为什么自己组装而不是直接用框架的 AgentService（实读结论）：
    `AgentService` 在构造时把 thread_id 固定为 `session-default`，且把输出硬绑定到终端通道，
    无法多会话并发、也拿不到结构化回复。因此这里直接用框架公开的构件：
        create_openai_llm（模型） + create_tool_list（工具，含 mcp__compliance__* 岗位工具）
        + langchain.agents.create_agent（ReAct 图） + MemorySaver（按 thread_id 隔离会话）
    这样既不修改框架源码，又能按会话隔离、可并发、可拿到最终回复与工具调用轨迹。

对外接口：
    chat(task, session_id, actor) -> {reply, tool_calls, elapsed_ms, trace_id}
    reset(session_id)
    history(session_id)
"""

import threading
import time

from langchain.agents import create_agent
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.checkpoint.memory import MemorySaver

from service import observability

PROJECT_ROOT_SYSTEM_PROMPT = """你是持证合规专员「小合」，一款岗位化智能合规数字员工的工作台助手。

你的职责范围：
- 合同与条款的合规审查、风险标注、法条检索、修改建议
- 解释某次审查的判定依据（命中哪条规则、依据哪条法条）
- 规则库与法规库的查询、规则演进说明
- 待办与审批状态的说明

工作纪律（硬约束）：
1. **无引用不判风险**：给出任何风险结论时，必须说明规则编号或法条编号；给不出依据就明说"证据不足"。
2. **不下最终结论**：高风险与中风险一律提示需人工签核，你不替人做合规决策。
3. **不编造**：不确定就说不确定，禁止编造法条编号、规则编号或案件事实。
4. 需要扫描合同原文时优先使用 MCP 岗位工具（parse_contract / annotate_clause / search_regulations 等），
   不要凭记忆回答法条内容。
5. 回答用中文，简洁、结论先行，必要时给表格。
"""


class AgentRuntime:
    """按会话隔离的数字员工运行时。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._agent = None
        self._checkpointer = None
        self._init_error: str | None = None
        # 【审查修正】同一 thread_id 的会话状态由 MemorySaver 持有，
        # 并发 invoke 同一 thread_id 会交错读写历史（可能丢消息或抛错）。
        # 因此按 session 加锁串行化；不同会话仍可并发。
        self._session_locks: dict[str, threading.Lock] = {}
        self._session_locks_guard = threading.Lock()

    def _session_lock(self, session_id: str) -> threading.Lock:
        with self._session_locks_guard:
            lock = self._session_locks.get(session_id)
            if lock is None:
                lock = threading.Lock()
                self._session_locks[session_id] = lock
            return lock

    # ---------------------------------------------------------------- 初始化

    def _ensure(self):
        if self._agent is not None:
            return self._agent
        with self._lock:
            if self._agent is not None:
                return self._agent
            try:
                from agent.llm import create_openai_llm
                from agent.tools import create_tool_list
                llm = create_openai_llm()
                tools = create_tool_list()
                self._checkpointer = MemorySaver()
                self._agent = create_agent(
                    model=llm, tools=tools,
                    system_prompt=SystemMessage(content=PROJECT_ROOT_SYSTEM_PROMPT),
                    checkpointer=self._checkpointer)
                observability.log_event("info", "agent.ready",
                                        tool_count=len(tools))
            except Exception as exc:
                self._init_error = f"{type(exc).__name__}: {exc}"
                observability.log_event("error", "agent.init_failed", error=self._init_error)
                raise
        return self._agent

    @property
    def ready(self) -> bool:
        return self._agent is not None

    @property
    def init_error(self) -> str | None:
        return self._init_error

    def describe(self) -> dict:
        try:
            agent = self._ensure()
            from agent.tools import create_tool_list
            tools = [t.name for t in create_tool_list()]
            mcp = [t for t in tools if t.startswith("mcp__")]
            return {"ok": True, "tool_total": len(tools), "mcp_tools": mcp,
                    "system_prompt": PROJECT_ROOT_SYSTEM_PROMPT}
        except Exception as exc:
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    # ---------------------------------------------------------------- 对话

    def chat(self, task: str, session_id: str = "workbench", actor: str = "",
             max_iterations: int = 12) -> dict:
        task = (task or "").strip()
        if not task:
            return {"ok": False, "errors": ["任务内容不能为空"]}
        trace = observability.new_trace_id()
        observability.set_context(trace, actor=actor, session=session_id, channel="workbench")
        started = time.time()
        try:
            agent = self._ensure()
        except Exception as exc:
            return {"ok": False, "errors": [f"数字员工未就绪：{type(exc).__name__}: {exc}"],
                    "trace_id": trace}

        cfg = {"configurable": {"thread_id": session_id},
               "recursion_limit": max(4, int(max_iterations)) * 2}
        try:
            # 同一会话串行执行（见 _session_lock 的说明）
            with self._session_lock(session_id):
                result = agent.invoke({"messages": [HumanMessage(content=task)]}, config=cfg)
        except Exception as exc:
            observability.log_event("error", "agent.invoke_failed", error=str(exc)[:300])
            return {"ok": False, "errors": [f"执行失败：{type(exc).__name__}: {str(exc)[:300]}"],
                    "trace_id": trace}

        msgs = result.get("messages", [])
        tool_calls = []
        for m in msgs:
            if isinstance(m, AIMessage):
                for tc in (m.tool_calls or []):
                    tool_calls.append({"name": tc.get("name"),
                                       "args": str(tc.get("args"))[:200]})
        last = msgs[-1] if msgs else None
        reply = last.content if isinstance(last, AIMessage) else str(getattr(last, "content", ""))
        elapsed = int((time.time() - started) * 1000)
        observability.log_event("info", "agent.task_done", elapsed_ms=elapsed,
                                tool_calls=len(tool_calls), reply_chars=len(reply or ""))
        return {"ok": True, "reply": reply, "tool_calls": tool_calls,
                "messages": len(msgs), "elapsed_ms": elapsed, "trace_id": trace,
                "session_id": session_id}

    def reset(self, session_id: str = "workbench") -> dict:
        """清空某会话的上下文（删除该 thread 的历史消息）。"""
        agent = self._ensure()
        # MemorySaver 的按 thread_id 隔离：删除该线程的历史消息
        from langchain_core.messages import RemoveMessage
        cfg = {"configurable": {"thread_id": session_id}}
        try:
            with self._session_lock(session_id):
                state = agent.get_state(cfg)
                msgs = (state.values or {}).get("messages", []) if state else []
                if msgs:
                    agent.update_state(cfg, {"messages": [RemoveMessage(id=m.id) for m in msgs
                                                          if getattr(m, "id", None)]})
        except Exception as exc:
            return {"ok": False, "errors": [f"清空失败：{type(exc).__name__}: {exc}"]}
        observability.log_event("info", "agent.session_reset", session=session_id)
        return {"ok": True, "session_id": session_id}

    def history(self, session_id: str = "workbench", limit: int = 50) -> dict:
        try:
            agent = self._ensure()
            cfg = {"configurable": {"thread_id": session_id}}
            with self._session_lock(session_id):
                state = agent.get_state(cfg)
                msgs = (state.values or {}).get("messages", []) if state else []
        except Exception as exc:
            return {"ok": False, "errors": [f"{type(exc).__name__}: {exc}"]}
        out = []
        for m in msgs[-limit:]:
            role = ("user" if isinstance(m, HumanMessage) else
                    "tool" if isinstance(m, ToolMessage) else "assistant")
            content = m.content if isinstance(m.content, str) else str(m.content)
            item = {"role": role, "content": content[:2000]}
            if isinstance(m, ToolMessage):
                item["tool"] = getattr(m, "name", "")
            out.append(item)
        return {"ok": True, "session_id": session_id, "messages": out}


_runtime: AgentRuntime | None = None
_runtime_lock = threading.Lock()


def runtime() -> AgentRuntime:
    global _runtime
    if _runtime is None:
        with _runtime_lock:
            if _runtime is None:
                _runtime = AgentRuntime()
    return _runtime
