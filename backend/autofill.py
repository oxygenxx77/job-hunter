"""
与「简历自动填充」浏览器扩展联动：

  1. 简历同步：扩展把自己保存的简历发过来，转换成 resume.json 的格式后合并写入，
     只取白名单字段（手机号、身份证号、住址等隐私信息不会进入 resume.json，也就不会发给 AI）
  2. 岗位识别：扩展弹窗打开时告诉后端当前网页地址，后端找出对应的岗位（含秋招看板进度）
  3. AI 文书：按岗位 JD 为扩展生成定制的自我评价、开放题回答
"""

import json
import logging
import os
import re
import time
from datetime import datetime
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import ai_match
import database as db
import kanban

logger = logging.getLogger("autofill")

RESUME_BACKUP_DIR = ai_match.BASE_DIR / "resume_backups"
MAX_RESUME_BACKUPS = 20

# 在求职助手里点「投递」后，这么长时间内打开的网申页都默认关联到该岗位（秒）
INTENT_TTL = 3 * 3600
# 弹窗下拉框里最多列出的候选岗位数
MAX_CANDIDATES = 15

# 最近一次在求职助手里点「投递」的岗位：网申页往往跳转到公司官网，网址对不上时靠它关联
_last_intent: Dict[str, Any] = {"job_id": None, "at": 0.0}


# ---------------------------------------------------------------------------
# 简历同步
# ---------------------------------------------------------------------------

def _split(text: Any, pattern: str = r"[,，、;；/\n]+") -> List[str]:
    return [s.strip() for s in re.split(pattern, str(text or "")) if s.strip()]


def _lines(text: Any) -> List[str]:
    """把一段描述拆成要点：按换行拆，去掉行首的序号和项目符号"""
    items = [re.sub(r"^\s*(\d+[.、)）]|[•·●▪\-*])\s*", "", s).strip() for s in str(text or "").splitlines()]
    return [s for s in items if s]


def _period(item: Dict[str, Any]) -> str:
    start, end = str(item.get("start") or "").strip(), str(item.get("end") or "").strip()
    return f"{start}-{end}" if start or end else ""


def convert_resume(src: Dict[str, Any]) -> Dict[str, Any]:
    """把扩展里的简历转换成 resume.json 的结构；扩展里为空的部分不出现在结果里（合并时保留旧值）"""
    out: Dict[str, Any] = {}
    if src.get("name"):
        out["name"] = str(src["name"]).strip()

    edus = [e for e in src.get("educationList") or [] if e.get("school")]
    if edus:
        out["education"] = "；".join(
            " ".join(p for p in (e.get("school"), e.get("degree"), e.get("major")) if p)
            + (f"（{_period(e)}）" if _period(e) else "")
            for e in edus
        )

    intention = {}
    if src.get("expectedJob"):
        intention["roles"] = _split(src["expectedJob"])
    if src.get("expectedCity"):
        intention["cities"] = _split(src["expectedCity"])
    if intention:
        out["job_intention"] = intention

    if src.get("summary"):
        out["summary"] = _lines(src["summary"]) or [str(src["summary"]).strip()]

    exps = [e for e in src.get("experienceList") or [] if e.get("company") or e.get("title")]
    if exps:
        out["experiences"] = [
            {
                "company": e.get("company", ""),
                "role": " ".join(p for p in (e.get("department"), e.get("title")) if p),
                "period": _period(e),
                "highlights": _lines(e.get("description")),
            }
            for e in exps
        ]
        out["internships"] = [" ".join(p for p in (e.get("company"), e.get("title")) if p) for e in exps]

    projects = [p for p in src.get("projectList") or [] if p.get("name")]
    if projects:
        out["projects"] = [
            {"name": " ".join(x for x in (p.get("name"), p.get("title")) if x), "period": _period(p), "highlights": _lines(p.get("description"))}
            for p in projects
        ]

    campus = [c for c in src.get("campusList") or [] if c.get("company") or c.get("title")]
    if campus:
        out["campus"] = [
            {"organization": c.get("company", ""), "role": c.get("title", ""), "period": _period(c), "highlights": _lines(c.get("description"))}
            for c in campus
        ]

    awards = [a.get("name") for a in src.get("awardList") or [] if a.get("name")]
    if awards:
        out["honors"] = awards

    langs = [
        " ".join(str(x) for x in (l.get("language"), l.get("cert") or l.get("level"), l.get("score")) if x)
        for l in src.get("languageList") or [] if l.get("language") or l.get("cert")
    ]
    if langs:
        out["languages"] = langs

    certs = [c.get("name") for c in src.get("certList") or [] if c.get("name")]
    if certs:
        out["certificates"] = certs

    skills = _split(src.get("skills"))
    if skills:
        out["skills"] = skills
    return out


def _same_name(a: str, b: str) -> bool:
    a, b = kanban.normalize_company(a), kanban.normalize_company(b)
    return bool(a and b) and (a in b or b in a)


def _keep_highlights(items: List[Dict[str, Any]], old_items: List[Dict[str, Any]], key: str) -> List[Dict[str, Any]]:
    """扩展里某段经历没写描述时，沿用 resume.json 里同一家公司 / 同一项目的要点"""
    out = []
    for item in items:
        if not item.get("highlights"):
            same = next((o for o in old_items if _same_name(o.get(key, ""), item.get(key, ""))), None)
            if same and same.get("highlights"):
                item = {**item, "highlights": same["highlights"]}
        out.append(item)
    return out


def merge_resume(old: Dict[str, Any], new: Dict[str, Any]) -> Dict[str, Any]:
    """扩展里有的部分覆盖旧值；技能取并集（旧的技能关键词用于规则打分，不能丢）"""
    merged = dict(old)
    for key, value in new.items():
        if key == "skills":
            merged["skills"] = list(dict.fromkeys([*old.get("skills", []), *value]))
        elif key == "experiences":
            merged[key] = _keep_highlights(value, old.get(key, []), "company")
        elif key == "projects":
            merged[key] = _keep_highlights(value, old.get(key, []), "name")
        else:
            merged[key] = value
    return merged


def _backup_resume() -> Optional[str]:
    if not ai_match.RESUME_PATH.exists():
        return None
    RESUME_BACKUP_DIR.mkdir(exist_ok=True)
    name = f"resume_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}.json"
    (RESUME_BACKUP_DIR / name).write_bytes(ai_match.RESUME_PATH.read_bytes())
    for old in sorted(RESUME_BACKUP_DIR.glob("resume_*.json"))[:-MAX_RESUME_BACKUPS]:
        old.unlink()
    return name


def sync_resume(src: Dict[str, Any]) -> Dict[str, Any]:
    """接收扩展的简历，合并进 resume.json（先备份），返回变化摘要"""
    new = convert_resume(src)
    if not new.get("name") and not new.get("experiences"):
        raise ValueError("扩展里的简历是空的，请先在扩展选项页上传 PDF 或填写简历")

    old = ai_match.load_resume()
    merged = merge_resume(old, new)
    if merged == old:
        return {"changed": False, "summary": "求职助手里的简历已是最新，无需同步", "backup": None}

    backup = _backup_resume()
    tmp = ai_match.RESUME_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, ai_match.RESUME_PATH)

    parts = [
        f"经历 {len(merged.get('experiences', []))} 段",
        f"项目 {len(merged.get('projects', []))} 个",
        f"奖项 {len(merged.get('honors', []))} 项",
        f"技能 {len(merged.get('skills', []))} 个",
    ]
    updated = [k for k in new if new[k] != old.get(k)]
    logger.info("简历已从扩展同步，更新字段：%s", updated)
    return {"changed": True, "summary": "已同步到求职助手：" + "、".join(parts), "updated": updated, "backup": backup}


# ---------------------------------------------------------------------------
# 岗位识别
# ---------------------------------------------------------------------------

def record_intent(job_id: int) -> None:
    """求职助手里点了「投递」：记下岗位，稍后扩展在网申页打开时用来关联"""
    _last_intent.update(job_id=job_id, at=time.time())


def _intent_job_id() -> Optional[int]:
    if _last_intent["job_id"] and time.time() - _last_intent["at"] < INTENT_TTL:
        return _last_intent["job_id"]
    return None


def _norm_url(url: str) -> str:
    """网址归一化：忽略协议、www、查询参数、锚点和末尾斜杠"""
    try:
        p = urlparse(url.strip())
    except ValueError:
        return ""
    host = (p.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    return host + p.path.rstrip("/") if host else ""


def find_job_by_url(url: str, jobs: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    target = _norm_url(url or "")
    if not target:
        return None
    for job in jobs:
        if target in {_norm_url(job.get("link") or ""), _norm_url(job.get("apply_link") or "")} - {""}:
            return job
    return None


def _brief(job: Dict[str, Any]) -> Dict[str, Any]:
    """给扩展弹窗用的岗位摘要（不含 JD 全文）"""
    keys = ("id", "company", "title", "city", "status", "match_score", "match_advice", "kanban")
    return {k: job.get(k) for k in keys}


def context_for(url: str) -> Dict[str, Any]:
    """
    扩展弹窗打开时调用：
    - job：按网址匹配到的岗位，匹配不到时用最近一次点「投递」的岗位
    - matched_by：url / recent / None
    - candidates：可手动选择的岗位（待投递优先，其次匹配分高的待确认岗位）
    """
    jobs = db.list_jobs()
    job = find_job_by_url(url, jobs)
    matched_by = "url" if job else None
    if not job and _intent_job_id():
        job = next((j for j in jobs if j["id"] == _intent_job_id()), None)
        matched_by = "recent" if job else None

    picks = [j for j in jobs if j["status"] == "approved"] + [j for j in jobs if j["status"] == "pending"]
    candidates = picks[:MAX_CANDIDATES]
    if job and job not in candidates:
        candidates.insert(0, job)

    kanban.attach_progress(candidates)
    return {
        "job": _brief(job) if job else None,
        "matched_by": matched_by,
        "candidates": [_brief(j) for j in candidates],
        "ai_configured": ai_match.is_ai_configured(),
    }


# ---------------------------------------------------------------------------
# AI 文书
# ---------------------------------------------------------------------------

WRITER_SYSTEM = (
    "你是一名校招求职文书助手，根据求职者的简历和目标岗位撰写网申表单里的文字。"
    "要求：第一人称、真诚具体，优先引用简历中与岗位要求最相关的经历和数据；"
    "严禁编造简历里没有的经历、数据、证书；"
    "只输出可以直接粘贴进表单的正文，不要标题、不要引号、不要 Markdown 符号、不要解释。"
)


def _job_text(job: Dict[str, Any]) -> str:
    return (
        f"公司：{job.get('company') or '未知'}\n岗位：{job.get('title') or '未知'}\n城市：{job.get('city') or '未知'}\n"
        f"岗位描述：\n{(job.get('jd_text') or '（未采集到 JD，请根据公司和岗位名称推断常见要求）')[:3000]}"
    )


def write_text(job_id: int, kind: str, question: str = "", max_chars: int = 300) -> str:
    """
    为指定岗位生成文书：
    - kind = summary：自我评价 / 个人优势
    - kind = answer：回答网申表单里的开放题（question 为题目）
    """
    if not ai_match.is_ai_configured():
        raise ValueError("AI 未配置，请先在 .env 填写 OPENAI_API_KEY")
    job = db.get_job(job_id)
    if not job:
        raise LookupError("岗位不存在")
    max_chars = max(50, min(int(max_chars or 300), 2000))

    resume = json.dumps(ai_match.load_resume(), ensure_ascii=False, indent=1)
    if kind == "summary":
        task = f"请为这个岗位写一段「自我评价」，突出与岗位最匹配的 2-3 个优势，{max_chars} 字以内。"
    elif kind == "answer":
        if not question.strip():
            raise ValueError("请先填写题目")
        task = f"网申表单里的题目是：「{question.strip()[:500]}」\n请写出回答，{max_chars} 字以内。"
    else:
        raise ValueError(f"不支持的类型：{kind}")

    prompt = f"## 目标岗位\n{_job_text(job)}\n\n## 我的简历\n{resume}\n\n## 任务\n{task}"
    text = ai_match.call_text(WRITER_SYSTEM, prompt)
    return re.sub(r"\n{3,}", "\n\n", text.strip().strip("\"“”「」"))
