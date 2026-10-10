"""
chat.py —— 实时对话的 AI 求职顾问

每次对话都会把以下信息作为系统上下文发给模型：
  - 简历（resume.json）
  - 求职偏好与今日投递情况
  - 匹配度最高的若干岗位摘要
  - 用户正在查看的岗位详情（可选，含完整 JD 和匹配分析）
模型可以通过工具调用直接修改岗位状态、偏好等（工具定义见 actions.py）。
回复以 NDJSON 流返回，每行一个事件：
  {"type": "text", "content": "..."}                        回复文字片段
  {"type": "action", "ok": true, "summary": "...", "refresh": true}   执行了一个操作
  {"type": "error", "message": "..."}                       出错
"""

import json
import logging
import time
from datetime import datetime
from typing import Any, Dict, Iterator, List, Optional

import actions
import ai_match
import database as db
import kanban

logger = logging.getLogger("chat")

# 上下文中最多列出的岗位数
MAX_CONTEXT_JOBS = 30
# 发送给模型的历史消息上限
MAX_HISTORY_MESSAGES = 20
MAX_HISTORY_CHARS = 12000
# 建立连接阶段的最大尝试次数（开始输出后不再重试）
MAX_RETRIES = 3
# 单次对话中「调用工具 → 拿结果 → 继续回答」的最大轮数
MAX_TOOL_ROUNDS = 5

STATUS_LABELS = actions.STATUS_LABELS

SYSTEM_PROMPT = """你是「求职助手」里的 AI 求职顾问，正在和求职者本人实时对话。

你的职责：
- 结合求职者的真实简历和岗位信息，帮他判断投递优先级、分析岗位、优化简历表述
- 撰写打招呼语、自我介绍、求职邮件，进行模拟面试并点评回答
- 回答校招 / 实习求职相关的问题

回答要求：
- 用中文，直接对求职者说话（用"你"），具体、务实，不说空话套话
- 只使用简历中真实存在的经历和数据，绝不编造经历；信息不足时直接说明或追问
- 引用岗位时带上编号，例如「#12 字节跳动 · 电商运营」
- 可以使用 Markdown 的标题、加粗、列表和代码块，不要使用表格
- 默认简洁，用户要求展开时再详细说明
- 用户发来截图（JD、面试或测评通知、网申页面、简历片段等）时，先认真读图，再结合简历和岗位回答；看不清就直接说

操作规则（你可以通过工具直接修改岗位状态和求职偏好）：
- 用户明确要求修改时，直接调用工具执行，不要再问"确定吗"；执行后用一句话说明结果
- 用户没有要求时不要擅自改数据；可以给建议，等用户同意后再执行
- 指代不明确时（比如"那个岗位"可能对应多个）先问清楚再执行
- 按公司、城市、关键词、分数等条件批量操作时，必须先调用 search_jobs 拿到完整名单，不要凭下方列表自己筛选（容易漏）；用户提到的岗位不在列表中时同样先搜索
- 涉及"全部""所有"这类超过 10 个岗位的批量修改，先列出数量和部分名单请用户确认，再执行
- 只有用户明确说已经投递完成时，才把岗位标记为 applied（已投递）
- 修改偏好后，询问用户是否需要重新分析待确认岗位的匹配度（rematch_jobs）
- 用户讲测评、面试、offer 等投递后的进度时，用 update_kanban 更新秋招看板；求职助手里标记已投递的岗位会自动同步到看板，不需要再手动调用
- 推荐岗位时留意看板：看板里已经投过或已结束的公司要提醒用户，避免重复投递
- 对话历史中「[已执行操作]」开头的内容是之前已经完成的操作记录

以下是当前的背景信息（由系统自动提供，求职者看不到这部分内容）：
"""


def _job_line(job: Dict) -> str:
    """岗位摘要：一行文本"""
    score = job.get("match_score")
    score_text = f"{score}分" if score is not None else "分析中"
    parts = [
        f"#{job['id']}",
        job.get("company") or "未知公司",
        job.get("title") or "",
        job.get("city") or "城市未知",
        score_text,
        job.get("match_advice") or "",
        STATUS_LABELS.get(job.get("status"), job.get("status") or ""),
    ]
    if job.get("kanban"):
        parts.append(f"看板：{job['kanban']['company']} {job['kanban']['status_label']}")
    return "｜".join(p for p in parts if p)


def build_context(job_id: Optional[int] = None) -> str:
    """组装系统上下文"""
    resume = ai_match.load_resume()
    prefs = db.get_preferences()
    stats = db.get_stats()
    today_applied = db.count_applied_today()

    sections = [
        f"## 今天\n{datetime.now().strftime('%Y-%m-%d %A')}",
        "## 简历\n" + json.dumps(resume, ensure_ascii=False, indent=1),
        (
            "## 求职偏好\n"
            f"- 目标岗位：{'、'.join(prefs['target_roles']) or '未设置'}\n"
            f"- 目标城市：{'、'.join(prefs['target_cities']) or '未设置'}\n"
            f"- 目标行业：{'、'.join(prefs['target_industries']) or '未设置'}\n"
            f"- 岗位方向优先级：{prefs['role_priority'] or '未设置'}\n"
            f"- 城市优先级：{prefs['city_priority'] or '未设置'}（不在其中的城市岗位够好也可以考虑）\n"
            f"- 黑名单公司：{'、'.join(prefs['avoid_companies']) or '无'}\n"
            f"- 黑名单岗位：{'、'.join(prefs['avoid_roles']) or '无'}\n"
            f"- 每日投递上限：{prefs['daily_limit']}，今日已投递：{today_applied}"
        ),
        (
            "## 岗位库统计\n"
            f"共 {stats['total']} 个岗位：待确认 {stats['pending']}，待投递 {stats['approved']}，"
            f"已投递 {stats['applied']}，已拒绝 {stats['rejected']}，高匹配（≥75）{stats['high_match']}"
        ),
    ]

    jobs = kanban.attach_progress(db.list_jobs()[:MAX_CONTEXT_JOBS])
    if jobs:
        sections.append(
            f"## 岗位列表（按匹配度排序，最多 {MAX_CONTEXT_JOBS} 个）\n"
            "格式：编号｜公司｜岗位｜城市｜匹配分｜建议｜状态｜看板中该公司的进度（如有）\n"
            + "\n".join(_job_line(j) for j in jobs)
        )
    else:
        sections.append("## 岗位列表\n暂无岗位，可提醒求职者先用浏览器扩展采集。")

    kanban_text = kanban.summary_for_ai()
    if kanban_text:
        sections.append(
            "## 秋招看板（求职者另一个工具，记录已投公司的测评 / 面试进度，可用 update_kanban 修改）\n"
            + kanban_text
        )

    if job_id:
        job = db.get_job(job_id)
        if job:
            kanban.attach_progress([job])
            if job.get("kanban"):
                k = job["kanban"]
                sections.append(
                    f"## 当前岗位公司在秋招看板中的进度\n{k['company']}｜{k['positions']}｜{k['status_label']}｜{k['note'] or '无备注'}"
                )
            sections.append(
                "## 求职者当前正在查看的岗位（问题默认围绕这个岗位）\n"
                f"{_job_line(job)}\n"
                f"链接：{job.get('link') or '无'}\n"
                f"AI 匹配分析：{job.get('match_reason') or '暂无'}\n"
                f"JD：\n{(job.get('jd_text') or '列表页未提供 JD')[:4000]}"
            )

    return "\n\n".join(sections)


def _text_length(content: Any) -> int:
    """消息文字长度；带图片的消息 content 是分段列表，只统计文字段"""
    if isinstance(content, str):
        return len(content)
    return sum(len(part.get("text", "")) for part in content if part.get("type") == "text")


def _trim_history(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """只保留最近的消息，并控制总字数"""
    recent = messages[-MAX_HISTORY_MESSAGES:]
    kept: List[Dict[str, Any]] = []
    total = 0
    for msg in reversed(recent):
        total += _text_length(msg["content"])
        if kept and total > MAX_HISTORY_CHARS:
            break
        kept.append(msg)
    return list(reversed(kept))


def _event(event_type: str, **fields: Any) -> str:
    """序列化一个 NDJSON 事件"""
    return json.dumps({"type": event_type, **fields}, ensure_ascii=False) + "\n"


class ChatError(Exception):
    """对话无法继续的错误，消息直接展示给用户"""


def _open_stream(convo: List[Dict[str, Any]]):
    """
    发起一次流式请求。连接失败（超时、限流、5xx）时退避重试；
    鉴权、模型不存在等不可恢复错误立即抛出 ChatError。
    """
    from openai import AuthenticationError, BadRequestError, NotFoundError, PermissionDeniedError

    fatal_errors = (AuthenticationError, BadRequestError, NotFoundError, PermissionDeniedError)
    last_error: Optional[Exception] = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            return ai_match.get_client().chat.completions.create(
                model=ai_match.MODEL,
                messages=convo,
                tools=actions.TOOLS,
                temperature=0.6,
                stream=True,
            )
        except fatal_errors as e:
            raise ChatError(f"AI 请求被拒绝（{type(e).__name__}），请检查 .env 中的 Key 和模型名：{e}") from e
        except Exception as e:
            last_error = e
            logger.warning("对话请求第 %d/%d 次失败：%s", attempt, MAX_RETRIES, e)
            if attempt < MAX_RETRIES:
                time.sleep(attempt * 1.5)
    raise ChatError(f"AI 暂时连不上（已重试 {MAX_RETRIES} 次）：{last_error}")


def stream_chat(messages: List[Dict[str, str]], job_id: Optional[int] = None) -> Iterator[str]:
    """
    流式生成回复（NDJSON 事件）。
    模型请求调用工具时：执行工具 → 推送 action 事件 → 把结果交回模型继续回答，最多 MAX_TOOL_ROUNDS 轮。
    """
    if not ai_match.is_ai_configured():
        yield _event("text", content="还没有配置 AI Key。请在项目根目录的 `.env` 中填写 `OPENAI_API_KEY`，然后重启后端。")
        return

    convo: List[Dict[str, Any]] = [{"role": "system", "content": SYSTEM_PROMPT + build_context(job_id)}]
    convo += _trim_history(messages)

    for round_no in range(MAX_TOOL_ROUNDS):
        try:
            stream = _open_stream(convo)
        except ChatError as e:
            yield _event("error", message=str(e))
            return

        text_parts: List[str] = []
        # 工具调用以增量形式到达，按 index 拼接 id / 名称 / 参数
        calls: Dict[int, Dict[str, str]] = {}
        try:
            for chunk in stream:
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta
                if getattr(delta, "content", None):
                    text_parts.append(delta.content)
                    yield _event("text", content=delta.content)
                for tc in getattr(delta, "tool_calls", None) or []:
                    slot = calls.setdefault(tc.index if tc.index is not None else 0, {"id": "", "name": "", "arguments": ""})
                    if tc.id:
                        slot["id"] = tc.id
                    if tc.function is not None:
                        if tc.function.name and not slot["name"]:
                            slot["name"] = tc.function.name
                        if tc.function.arguments:
                            slot["arguments"] += tc.function.arguments
        except Exception as e:
            logger.warning("对话流式输出中断：%s", e)
            yield _event("error", message=f"回复中断：{e}")
            return

        if not calls:
            return

        ordered = [calls[i] for i in sorted(calls)]
        for i, call in enumerate(ordered):
            call["id"] = call["id"] or f"call_{round_no}_{i}"
        convo.append({
            "role": "assistant",
            "content": "".join(text_parts) or None,
            "tool_calls": [
                {"id": c["id"], "type": "function", "function": {"name": c["name"], "arguments": c["arguments"] or "{}"}}
                for c in ordered
            ],
        })

        for call in ordered:
            try:
                args = json.loads(call["arguments"] or "{}")
            except json.JSONDecodeError:
                result = {"ok": False, "summary": f"操作参数解析失败：{call['arguments'][:100]}", "data": None, "refresh": False}
            else:
                result = actions.execute_tool(call["name"], args)
            logger.info("AI 执行操作 %s(%s)：%s", call["name"], call["arguments"], result["summary"])
            yield _event("action", ok=result["ok"], summary=result["summary"], refresh=result["refresh"])
            convo.append({
                "role": "tool",
                "tool_call_id": call["id"],
                "content": json.dumps(
                    {"ok": result["ok"], "summary": result["summary"], "data": result["data"]},
                    ensure_ascii=False,
                ),
            })

    yield _event("text", content="\n\n（操作步骤过多，已停止，请把需求拆开再试）")
