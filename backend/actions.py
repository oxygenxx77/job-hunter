"""
actions.py —— 岗位与偏好的业务操作

网页接口（main.py）和 AI 顾问的工具调用（chat.py）共用这里的逻辑，
保证两边遵守同一套规则（例如每日投递上限）。
"""

import logging
import threading
from typing import Any, Dict, List, Optional

import ai_match
import database as db
import kanban

logger = logging.getLogger("actions")

STATUS_LABELS = {"pending": "待确认", "approved": "待投递", "applied": "已投递", "rejected": "已拒绝"}

PREF_LABELS = {
    "target_roles": "目标岗位",
    "target_cities": "目标城市",
    "target_industries": "目标行业",
    "avoid_companies": "黑名单公司",
    "avoid_roles": "黑名单岗位",
}


class ActionError(Exception):
    """业务操作失败，status_code 供网页接口转换为 HTTP 状态码"""

    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


def job_label(job: Dict[str, Any]) -> str:
    """岗位简称，如「#12 字节跳动 · 电商运营」"""
    return f"#{job['id']} {job.get('company') or '未知公司'} · {job.get('title') or ''}"


def _label_list(jobs: List[Dict[str, Any]], limit: int = 5) -> str:
    """岗位名单，超过 limit 个时省略"""
    names = "、".join(job_label(j) for j in jobs[:limit])
    return names + (f" 等 {len(jobs)} 个岗位" if len(jobs) > limit else "")


def start_background_matching() -> None:
    """在后台线程中分析所有未打分的岗位"""
    threading.Thread(target=ai_match.run_pending_matches, daemon=True).start()


# ---------------------------------------------------------------------------
# 风险与投递
# ---------------------------------------------------------------------------

def build_risk_status() -> Dict[str, Any]:
    """
    计算当前账号风险状态：
    - red：今日投递已达上限，禁止继续投递
    - yellow：今日投递达到上限的 80%，提醒放慢节奏
    - green：状态良好
    """
    prefs = db.get_preferences()
    limit = prefs["daily_limit"]
    today_applied = db.count_applied_today()
    remaining = max(limit - today_applied, 0)

    if today_applied >= limit:
        level, message = "red", "🔴 今日投递已达上限"
    elif today_applied >= limit * 0.8:
        level, message = "yellow", "🟡 接近今日投递上限，注意节奏"
    else:
        level, message = "green", "🟢 账号状态良好"

    return {
        "level": level,
        "message": message,
        "can_apply": today_applied < limit,
        "today_applied": today_applied,
        "daily_limit": limit,
        "remaining": remaining,
        "recent_logs": db.list_risk_logs(limit=10),
    }


def apply_job(job_id: int) -> Dict[str, Any]:
    """把岗位标记为已投递：校验存在性和每日上限，并写风险日志"""
    job = db.get_job(job_id)
    if not job:
        raise ActionError("岗位不存在", 404)
    if job["status"] == "applied":
        return {"ok": True, "already_applied": True, "job": job}

    risk = build_risk_status()
    if not risk["can_apply"]:
        db.add_risk_log(
            "暂停",
            f"今日投递已达上限 {risk['daily_limit']}，已拦截：{job['company']} - {job['title']}",
        )
        raise ActionError(f"今日投递已达上限（{risk['daily_limit']}），请明天再投", 429)

    db.update_job_status(job_id, "applied")
    db.add_risk_log("投递", f"{job['company']} - {job['title']}")
    return {"ok": True, "already_applied": False, "job": db.get_job(job_id), "kanban_sync": sync_to_kanban(job)}


def sync_to_kanban(job: Dict[str, Any]) -> Optional[str]:
    """已投递岗位同步到秋招看板；看板不存在或同步失败都不影响投递本身"""
    if not kanban.is_available():
        return None
    try:
        return kanban.push_applied_job(job)
    except Exception as e:
        logger.warning("同步秋招看板失败：%s", e)
        db.add_risk_log("异常", f"同步秋招看板失败（{job.get('company')} - {job.get('title')}）：{e}")
        return f"同步秋招看板失败：{e}"


# ---------------------------------------------------------------------------
# AI 顾问可调用的工具
# ---------------------------------------------------------------------------

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "update_job_status",
            "description": (
                "修改一个或多个岗位的状态。pending=待确认，approved=待投递（用户决定要投），"
                "rejected=已拒绝（不投），applied=已投递（仅当用户明确说自己已经投递完成时使用，受每日投递上限约束）。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "job_ids": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "description": "岗位编号列表，即背景信息中 # 后面的数字",
                    },
                    "status": {"type": "string", "enum": ["pending", "approved", "rejected", "applied"]},
                },
                "required": ["job_ids", "status"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_jobs",
            "description": (
                "按关键词在岗位库中搜索（匹配公司名、岗位名、城市），返回完整的岗位编号和摘要。"
                "按公司 / 城市 / 关键词 / 分数等条件做批量操作前必须先调用它获取完整名单；"
                "用户提到的岗位不在背景信息列表里时也用它查找。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "keyword": {"type": "string", "description": "关键词，留空表示不按关键词过滤"},
                    "status": {"type": "string", "enum": ["pending", "approved", "applied", "rejected"]},
                    "min_score": {"type": "integer", "description": "最低匹配分"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_preferences",
            "description": (
                "修改求职偏好：对列表字段做增加、删除或整体替换，也可以修改每日投递上限。返回修改后的完整偏好。"
                "用户说「加上 / 增加 / 再加」用 add，说「去掉 / 删除 / 不要」用 remove，只有说「改成 / 设为 / 只要」时才用 set。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "changes": {
                        "type": "array",
                        "description": "列表字段的修改",
                        "items": {
                            "type": "object",
                            "properties": {
                                "field": {"type": "string", "enum": list(PREF_LABELS.keys())},
                                "action": {"type": "string", "enum": ["add", "remove", "set"]},
                                "values": {"type": "array", "items": {"type": "string"}},
                            },
                            "required": ["field", "action", "values"],
                        },
                    },
                    "daily_limit": {"type": "integer", "minimum": 1, "maximum": 200, "description": "每日投递上限"},
                    "role_priority": {
                        "type": "string",
                        "description": "岗位方向优先级（整体替换），> 表示更优先、= 表示同等，如「运营>营销=产品」；传空字符串表示清空",
                    },
                    "city_priority": {
                        "type": "string",
                        "description": "城市优先级（整体替换），如「深圳>广州=上海>北京」；传空字符串表示清空",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_kanban",
            "description": (
                "修改用户「秋招看板」里的公司卡片（记录投递后的测评、面试进度）。"
                "用户说测评做完了、约了面试、面试过了 / 挂了、拿到 offer 等进度时使用；公司不存在时会新建卡片。"
                "看板状态：pending=待测评（收到测评/笔试还没做），interview=面试中（约了面试），waiting=等结果（做完了在等通知），"
                "submitted=已投递，offer=拿到 Offer，ended=已结束（挂了、放弃或流程结束）。"
                "note 会覆盖原备注，需要保留原有信息时请把原备注（见背景信息）合并进新备注。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "company": {"type": "string", "description": "公司名，尽量与看板中的写法一致"},
                    "status": {"type": "string", "enum": list(kanban.STATUS_LABELS.keys())},
                    "note": {"type": "string", "description": "新的备注全文"},
                    "add_position": {"type": "string", "description": "追加到该公司卡片的岗位名"},
                },
                "required": ["company"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "rematch_jobs",
            "description": "让 AI 在后台重新分析岗位匹配度。修改偏好或简历后，在用户同意时调用；不传 job_ids 表示重新分析全部「待确认」岗位。",
            "parameters": {
                "type": "object",
                "properties": {
                    "job_ids": {"type": "array", "items": {"type": "integer"}},
                },
            },
        },
    },
]


def _result(ok: bool, summary: str, data: Any = None, refresh: bool = False) -> Dict[str, Any]:
    """
    统一的工具执行结果：
    - summary：给用户看的一句话
    - data：给模型看的详细数据
    - refresh：前端是否需要刷新列表
    """
    return {"ok": ok, "summary": summary, "data": data, "refresh": refresh}


def _tool_update_job_status(args: Dict[str, Any]) -> Dict[str, Any]:
    status = args.get("status")
    if status not in STATUS_LABELS:
        return _result(False, f"无效状态：{status}")
    ids = list(dict.fromkeys(int(i) for i in args.get("job_ids") or []))
    if not ids:
        return _result(False, "没有指定要修改的岗位")

    jobs = [j for j in (db.get_job(i) for i in ids) if j]
    found_ids = {j["id"] for j in jobs}
    missing = [i for i in ids if i not in found_ids]
    if not jobs:
        return _result(False, f"找不到岗位 {'、'.join('#' + str(i) for i in missing)}")

    label = STATUS_LABELS[status]
    notes = []
    if missing:
        notes.append(f"找不到 {'、'.join('#' + str(i) for i in missing)}")

    if status == "applied":
        # 逐个走投递逻辑，受每日上限约束
        done, blocked, synced = [], [], []
        for job in jobs:
            try:
                result = apply_job(job["id"])
                done.append(job)
                if result.get("kanban_sync"):
                    synced.append(result["kanban_sync"])
            except ActionError as e:
                blocked.append((job, str(e)))
        if blocked:
            notes.append(f"{len(blocked)} 个未标记：{blocked[0][1]}")
        notes.extend(synced)
        if not done:
            return _result(False, "；".join(notes), refresh=True)
        summary = f"已将 {_label_list(done)} 标记为「{label}」"
    else:
        db.batch_update_status([j["id"] for j in jobs], status)
        summary = f"已将 {_label_list(jobs)} 标记为「{label}」"

    if notes:
        summary += "（" + "；".join(notes) + "）"
    return _result(True, summary, refresh=True)


def _tool_search_jobs(args: Dict[str, Any]) -> Dict[str, Any]:
    keyword = str(args.get("keyword") or "").strip().lower()
    status = args.get("status") if args.get("status") in STATUS_LABELS else None
    min_score = args.get("min_score")
    jobs = db.list_jobs(status=status, min_score=int(min_score) if min_score is not None else None)
    if keyword:
        jobs = [
            j for j in jobs
            if keyword in f"{j.get('company', '')} {j.get('title', '')} {j.get('city', '')}".lower()
        ]
    kanban.attach_progress(jobs)
    data = [
        {
            "id": j["id"],
            "company": j["company"],
            "title": j["title"],
            "city": j["city"],
            "match_score": j["match_score"],
            "match_advice": j["match_advice"],
            "status": STATUS_LABELS.get(j["status"], j["status"]),
            "kanban": f"{j['kanban']['company']} {j['kanban']['status_label']}" if j.get("kanban") else None,
        }
        for j in jobs[:50]
    ]
    desc = f"「{args.get('keyword')}」" if keyword else "岗位"
    return _result(True, f"搜索{desc}：找到 {len(jobs)} 个", data={"total": len(jobs), "jobs": data})


def _tool_update_preferences(args: Dict[str, Any]) -> Dict[str, Any]:
    prefs = db.get_preferences()
    new = {field: list(prefs[field]) for field in PREF_LABELS}
    diffs = []

    for change in args.get("changes") or []:
        field = change.get("field")
        action = change.get("action")
        values = [str(v).strip() for v in change.get("values") or [] if str(v).strip()]
        if field not in PREF_LABELS or action not in ("add", "remove", "set"):
            return _result(False, f"无法识别的偏好修改：{change}")
        if action == "add":
            added = [v for v in values if v not in new[field]]
            new[field] += added
            if added:
                diffs.append(f"{PREF_LABELS[field]} +{'、'.join(added)}")
        elif action == "remove":
            removed = [v for v in values if v in new[field]]
            new[field] = [v for v in new[field] if v not in values]
            if removed:
                diffs.append(f"{PREF_LABELS[field]} -{'、'.join(removed)}")
        else:
            new[field] = list(dict.fromkeys(values))
            diffs.append(f"{PREF_LABELS[field]} 改为 {'、'.join(new[field]) or '空'}")

    daily_limit = prefs["daily_limit"]
    if args.get("daily_limit") is not None:
        value = int(args["daily_limit"])
        if not 1 <= value <= 200:
            return _result(False, "每日投递上限需在 1-200 之间")
        if value != daily_limit:
            diffs.append(f"每日上限 {daily_limit} → {value}")
        daily_limit = value

    tiers = {}
    for field, label in (("role_priority", "方向优先级"), ("city_priority", "城市优先级")):
        if args.get(field) is None:
            continue
        value = db.format_tiers(db.parse_tiers(str(args[field])))
        if value != prefs[field]:
            diffs.append(f"{label} 改为 {value or '空'}")
        tiers[field] = value

    if not diffs:
        return _result(True, "偏好没有变化", data=prefs)

    saved = db.save_preferences({**new, **tiers, "daily_limit": daily_limit})
    data = {field: saved[field] for field in PREF_LABELS}
    data.update({"daily_limit": saved["daily_limit"], "role_priority": saved["role_priority"],
                 "city_priority": saved["city_priority"]})
    return _result(True, "已更新偏好：" + "；".join(diffs), data=data, refresh=True)


def _tool_rematch_jobs(args: Dict[str, Any]) -> Dict[str, Any]:
    ids: Optional[List[int]] = [int(i) for i in args.get("job_ids") or []] or None
    count = db.reset_matches(ids)
    if not count:
        return _result(True, "没有需要重新分析的岗位")
    start_background_matching()
    return _result(True, f"已提交 {count} 个岗位重新分析，分数会陆续更新", refresh=True)


def _tool_update_kanban(args: Dict[str, Any]) -> Dict[str, Any]:
    if not kanban.is_available():
        return _result(False, "本机没有找到秋招看板")
    company = str(args.get("company") or "").strip()
    if not company:
        return _result(False, "没有指定公司")
    summary = kanban.upsert_card(
        company,
        status=args.get("status") or None,
        note=args.get("note") if args.get("note") is not None else None,
        add_position=str(args.get("add_position") or "").strip() or None,
        history_note="求职助手 AI 更新",
    )
    return _result(True, summary, refresh=True)


_TOOL_HANDLERS = {
    "update_job_status": _tool_update_job_status,
    "search_jobs": _tool_search_jobs,
    "update_preferences": _tool_update_preferences,
    "update_kanban": _tool_update_kanban,
    "rematch_jobs": _tool_rematch_jobs,
}


def execute_tool(name: str, args: Dict[str, Any]) -> Dict[str, Any]:
    """执行 AI 请求的工具，任何异常都转为失败结果，不中断对话"""
    handler = _TOOL_HANDLERS.get(name)
    if not handler:
        return _result(False, f"未知操作：{name}")
    try:
        return handler(args if isinstance(args, dict) else {})
    except (TypeError, ValueError) as e:
        return _result(False, f"操作参数有误：{e}")
    except Exception as e:
        return _result(False, f"操作失败：{e}")
