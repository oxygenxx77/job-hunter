"""
kanban.py —— 与「秋招看板」（qiuzhao-kanban）联动

看板是一个独立的本地项目，数据存放在它目录下的 JSON 文件中：
  - kanban_data.json：公司卡片 {id, company, positions, status, deadline, note}
  - interviews.json：面试记录
  - history.json：状态变更历史（看板周报用）
看板每次请求都会重新读取这些文件，所以这里直接读写文件即可，不依赖看板服务是否在运行。
写入时与看板自身保持一致：先备份到 backups/，再原子替换，并追加 history。
"""

import json
import os
import re
import shutil
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

KANBAN_DIR = Path(os.getenv("KANBAN_DIR") or Path.home() / "qiuzhao-kanban")
MAX_BACKUPS = 30

STATUS_LABELS = {"pending": "待测评", "interview": "面试中", "waiting": "等结果",
                 "submitted": "已投递", "offer": "Offer", "ended": "已结束"}

# 公司名中不影响识别的部分，用于把「北京小米科技有限责任公司」和「小米」认作同一家
_CITY_PREFIX = re.compile(r"^(北京|上海|广州|深圳|杭州|成都|武汉|南京|西安|苏州|天津|重庆|长沙|厦门|珠海|东莞|佛山)(市)?")
_SUFFIX = re.compile(r"(（.*?）|\(.*?\)|股份有限公司|有限责任公司|有限公司|集团|控股|股份|公司|信息技术|信息科技|网络科技|科技|技术|网络|信息|中国)")

_write_lock = threading.Lock()


def _path(name: str) -> Path:
    return KANBAN_DIR / name


def is_available() -> bool:
    """本机是否装有秋招看板"""
    return _path("kanban_data.json").exists()


def _read_json(name: str) -> List[Dict[str, Any]]:
    try:
        data = json.loads(_path(name).read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def load_cards() -> List[Dict[str, Any]]:
    return _read_json("kanban_data.json")


def load_interviews() -> List[Dict[str, Any]]:
    return _read_json("interviews.json")


# ---------------------------------------------------------------------------
# 公司名匹配
# ---------------------------------------------------------------------------

def normalize_company(name: str) -> str:
    """规整公司名：去城市前缀、公司后缀、空白，统一小写"""
    s = re.sub(r"\s+", "", name or "").lower()
    s = _CITY_PREFIX.sub("", s)
    s = _SUFFIX.sub("", s)
    return s or re.sub(r"\s+", "", name or "").lower()


def find_card(company: str, cards: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """
    在看板卡片中找到对应公司：
    规整后完全相同优先；否则取名字被岗位公司名包含的卡片中最长的（如「小鹏汽车」→「小鹏」）。
    只做单向包含：「腾讯」的岗位不能算到「腾讯音乐」的卡片上
    """
    key = normalize_company(company)
    if not key:
        return None
    exact = [c for c in cards if normalize_company(c.get("company", "")) == key]
    if exact:
        return exact[0]
    partial = [
        c for c in cards
        if (ck := normalize_company(c.get("company", ""))) and len(ck) >= 2 and ck in key
    ]
    return max(partial, key=lambda c: len(c.get("company", ""))) if partial else None


def _interviews_for(company: str, interviews: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    key = normalize_company(company)
    return [
        {k: iv.get(k, "") for k in ("company", "position", "round", "date", "time", "result")}
        for iv in interviews
        if key and normalize_company(iv.get("company", "")) == key
    ]


def _progress(card: Dict[str, Any], interviews: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {
        "company": card.get("company", ""),
        "positions": card.get("positions", ""),
        "status": card.get("status", ""),
        "status_label": STATUS_LABELS.get(card.get("status"), card.get("status", "")),
        "note": card.get("note", ""),
        "deadline": card.get("deadline", ""),
        "interviews": _interviews_for(card.get("company", ""), interviews),
    }


def attach_progress(jobs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """给岗位列表附加 kanban 字段（这家公司在看板中的进度），看板里没有的为 None"""
    if not is_available():
        return jobs
    cards = load_cards()
    interviews = load_interviews()
    cache: Dict[str, Optional[Dict[str, Any]]] = {}
    for job in jobs:
        company = job.get("company") or ""
        if company not in cache:
            card = find_card(company, cards)
            cache[company] = _progress(card, interviews) if card else None
        job["kanban"] = cache[company]
    return jobs


# ---------------------------------------------------------------------------
# 写入
# ---------------------------------------------------------------------------

def _write_cards(cards: List[Dict[str, Any]], history_entry: Dict[str, Any]) -> None:
    """备份 → 原子写入 kanban_data.json → 追加 history.json"""
    data_file = _path("kanban_data.json")
    backup_dir = _path("backups")
    if data_file.exists():
        backup_dir.mkdir(exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        shutil.copy2(data_file, backup_dir / f"kanban_data_{ts}.json")
        backups = sorted(p for p in backup_dir.iterdir() if p.name.startswith("kanban_data_"))
        for old in backups[:-MAX_BACKUPS]:
            old.unlink()

    tmp = data_file.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cards, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, data_file)

    history = _read_json("history.json")
    history.append({"timestamp": datetime.now().isoformat(), **history_entry})
    _path("history.json").write_text(json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8")


def _merge_positions(existing: str, new_position: str) -> str:
    """把岗位追加到看板卡片的岗位列表（顿号分隔），已存在则不重复"""
    items = [p.strip() for p in re.split(r"[、,，]", existing or "") if p.strip() and p.strip() not in ("待补充", "（待补充）")]
    if new_position and new_position not in items:
        items.append(new_position)
    return "、".join(items) or "（待补充）"


def upsert_card(
    company: str,
    status: Optional[str] = None,
    note: Optional[str] = None,
    add_position: Optional[str] = None,
    default_status: str = "submitted",
    history_note: str = "",
) -> str:
    """
    新增或更新看板卡片，返回一句结果描述。
    - status：为 None 时不改状态（新建卡片时用 default_status）
    - note：为 None 时不改备注
    - add_position：追加到岗位列表
    """
    if not is_available():
        raise RuntimeError(f"未找到秋招看板数据：{_path('kanban_data.json')}")
    if status is not None and status not in STATUS_LABELS:
        raise ValueError(f"无效的看板状态：{status}")

    with _write_lock:
        cards = load_cards()
        card = find_card(company, cards)
        if card is None:
            next_id = max((int(c["id"]) for c in cards if str(c.get("id", "")).isdigit()), default=0) + 1
            card = {
                "id": str(next_id),
                "company": company,
                "positions": add_position or "（待补充）",
                "status": status or default_status,
                "deadline": "",
                "note": note or "",
                "applied_at": datetime.now().strftime("%Y-%m-%d"),
                "updated_at": datetime.now().isoformat(timespec="seconds"),
            }
            cards.append(card)
            _write_cards(cards, {"company": company, "old_status": "", "new_status": card["status"], "note": history_note or card["note"]})
            return f"秋招看板新增「{company}」（{STATUS_LABELS[card['status']]}）"

        old_status = card.get("status", "")
        changes = []
        if add_position:
            merged = _merge_positions(card.get("positions", ""), add_position)
            if merged != card.get("positions"):
                card["positions"] = merged
                changes.append(f"岗位 +{add_position}")
        if status and status != old_status:
            card["status"] = status
            changes.append(f"{STATUS_LABELS.get(old_status, old_status)} → {STATUS_LABELS[status]}")
        if note is not None and note != card.get("note", ""):
            card["note"] = note
            changes.append(f"备注改为「{note}」" if note else "清空备注")
        if not changes:
            return f"秋招看板「{card['company']}」无需改动"
        card["updated_at"] = datetime.now().isoformat(timespec="seconds")

        _write_cards(cards, {"company": card["company"], "old_status": old_status, "new_status": card["status"], "note": history_note or card.get("note", "")})
        return f"秋招看板「{card['company']}」：" + "，".join(changes)


def push_applied_job(job: Dict[str, Any]) -> str:
    """
    求职助手中岗位标记为已投递后同步到看板：
    已有该公司卡片时只追加岗位（不覆盖看板里更靠后的进度，如等结果、已结束）；
    没有时新建一张「已投递」卡片。
    """
    title = (job.get("title") or "").strip()
    note = f"来自求职助手（匹配分 {job['match_score']}）" if job.get("match_score") is not None else "来自求职助手"
    cards = load_cards()
    is_new = find_card(job.get("company") or "", cards) is None
    return upsert_card(
        job.get("company") or "未知公司",
        status=None,
        note=note if is_new else None,
        add_position=title,
        history_note=f"求职助手投递：{title}",
    )


# ---------------------------------------------------------------------------
# 给 AI 的摘要
# ---------------------------------------------------------------------------

def summary_for_ai() -> str:
    """看板卡片与面试记录的文字摘要，供求职助手的 AI 顾问参考"""
    if not is_available():
        return ""
    cards = load_cards()
    lines = [
        "｜".join(p for p in (
            c.get("company", ""),
            c.get("positions", ""),
            STATUS_LABELS.get(c.get("status"), c.get("status", "")),
            c.get("note", ""),
            f"截止 {c['deadline']}" if c.get("deadline") else "",
        ) if p)
        for c in cards
    ]
    counts: Dict[str, int] = {}
    for c in cards:
        counts[c.get("status", "")] = counts.get(c.get("status", ""), 0) + 1
    stat = "，".join(f"{STATUS_LABELS[s]} {counts.get(s, 0)}" for s in STATUS_LABELS)
    text = f"共 {len(cards)} 家公司：{stat}\n格式：公司｜岗位｜状态｜备注｜截止时间\n" + "\n".join(lines)

    interviews = load_interviews()[-10:]
    if interviews:
        text += "\n\n最近面试记录：\n" + "\n".join(
            f"- {iv.get('company', '')} {iv.get('position', '')} {iv.get('round', '')}｜{iv.get('date', '') or '日期未填'}｜{iv.get('result', '待定')}"
            for iv in interviews
        )
    return text
