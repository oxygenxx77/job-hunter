"""
与「简历自动填充」浏览器扩展联动：

  1. 简历同步：扩展把自己保存的简历发过来，转换成 resume.json 的格式后合并写入，
     只取白名单字段（手机号、身份证号、住址等隐私信息不会进入 resume.json，也就不会发给 AI）
  2. 岗位识别：扩展弹窗打开时告诉后端当前网页地址，后端找出对应的岗位（含秋招看板进度）
  3. AI 文书：按岗位 JD 为扩展生成定制的自我评价、开放题回答
  4. 填充报告：扩展每次填完上报没填上的字段和不认识的标签，汇总后用来改进填充规则
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


# ---------------------------------------------------------------------------
# 填充报告
# ---------------------------------------------------------------------------

MAX_REPORT_ITEMS = 80
MAX_REPORT_BYTES = 200_000
REPORT_FIELDS = ("host", "path", "version", "frame", "job_id", "filled", "stats", "failed", "leftover")
# 扩展已经把简历内容换成占位符了；这里再兜底抹掉手机号、邮箱、身份证号格式的字符串
_SECRET_PATTERNS = [
    re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"),
    re.compile(r"[\w.+-]+@[\w-]+(\.[\w-]+)+"),
    re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)"),
]


def _scrub(value: Any) -> Any:
    """递归处理报告：字符串截断并抹掉隐私格式，列表限制条数"""
    if isinstance(value, str):
        for pattern in _SECRET_PATTERNS:
            value = pattern.sub("‹隐私›", value)
        return value[:600]
    if isinstance(value, list):
        return [_scrub(v) for v in value[:MAX_REPORT_ITEMS]]
    if isinstance(value, dict):
        return {str(k)[:40]: _scrub(v) for k, v in list(value.items())[:60]}
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return str(value)[:600]


def save_reports(reports: List[Dict[str, Any]]) -> int:
    """保存扩展上报的填充报告，返回保存的份数；格式不对或过大的直接跳过"""
    saved = 0
    for raw in reports:
        if not isinstance(raw, dict) or not raw.get("host"):
            continue
        report = _scrub({k: raw.get(k) for k in REPORT_FIELDS})
        if len(json.dumps(report, ensure_ascii=False).encode()) > MAX_REPORT_BYTES:
            logger.warning("填充报告过大，已跳过：%s", report.get("host"))
            continue
        created_at = None
        if isinstance(raw.get("at"), (int, float)) and raw["at"] > 0:
            created_at = datetime.fromtimestamp(raw["at"] / 1000).strftime("%Y-%m-%d %H:%M:%S")
        db.add_fill_report(report, created_at)
        saved += 1
    return saved


def _bucket(groups: Dict[Any, Dict[str, Any]], key: Any, host: str, sample: Dict[str, Any]) -> Dict[str, Any]:
    g = groups.setdefault(key, {"count": 0, "sites": set(), "samples": []})
    g["count"] += 1
    g["sites"].add(host)
    if len(g["samples"]) < 3 and host not in {s.get("host") for s in g["samples"]}:
        g["samples"].append(dict(sample, host=host))
    return g


def _ranked(groups: Dict[Any, Dict[str, Any]], extra=None) -> List[Dict[str, Any]]:
    out = []
    for key, g in groups.items():
        item = {"sites": sorted(g["sites"]), "count": g["count"], "samples": g["samples"]}
        item.update(extra(key, g) if extra else {})
        out.append(item)
    out.sort(key=lambda i: (len(i["sites"]), i.get("required", False), i["count"]), reverse=True)
    return out


def report_digest(include_resolved: bool = False) -> Dict[str, Any]:
    """
    把未处理的填充报告按问题汇总，给改规则的人（或 AI）看：
    - unknown：填完仍是空的、标签没被识别的字段 → 多半要往词典里加叫法
    - failed：识别了也尝试填了，但没填上 → 要看 via / trace 修对应组件的填法
    - empty：识别了但没尝试填（区块没定位到、条目不够等）
    - no_data：识别了，但简历里没有这项 → 提醒补简历，不是代码问题
    """
    rows = db.list_fill_reports(include_resolved=include_resolved)
    unknown: Dict[Any, Dict[str, Any]] = {}
    failed: Dict[Any, Dict[str, Any]] = {}
    empty: Dict[Any, Dict[str, Any]] = {}
    no_data: Dict[Any, Dict[str, Any]] = {}
    sites: Dict[str, Dict[str, Any]] = {}

    for row in rows:
        rep = row["report"] or {}
        host = row["host"]
        site = sites.setdefault(host, {"host": host, "reports": 0, "filled": 0, "failed": 0, "unknown": 0,
                                       "versions": set(), "last": row["created_at"]})
        site["reports"] += 1
        site["filled"] += row["filled"] or 0
        site["failed"] += row["failed"] or 0
        site["unknown"] += row["unknown"] or 0
        site["versions"].add(row["version"])

        for f in rep.get("failed") or []:
            g = _bucket(failed, (f.get("key"), f.get("via")), host,
                        {"label": f.get("label"), "want": f.get("want"), "error": f.get("error"),
                         "trace": f.get("trace"), "report_id": row["id"]})
            g.setdefault("labels", set()).add(f.get("label") or "")
        for f in rep.get("leftover") or []:
            kind = f.get("kind")
            sample = {k: f.get(k) for k in ("tag", "type", "ph", "select", "date", "chain", "n")}
            sample["report_id"] = row["id"]
            if kind == "unknown":
                g = _bucket(unknown, f.get("label") or "", host, sample)
                g["required"] = g.get("required", False) or bool(f.get("required"))
            elif kind == "no-data":
                _bucket(no_data, f.get("key") or "", host, {"label": f.get("label")})
            else:
                g = _bucket(empty, (f.get("key"), f.get("label")), host, sample)
                g["required"] = g.get("required", False) or bool(f.get("required"))

    return {
        "reports": len(rows),
        "ids": [r["id"] for r in rows],
        "versions": sorted({r["version"] for r in rows}),
        "since": rows[-1]["created_at"] if rows else None,
        "unknown": _ranked(unknown, lambda label, g: {"label": label, "required": g.get("required", False)}),
        "failed": _ranked(failed, lambda key, g: {"key": key[0], "via": key[1], "labels": sorted(g.get("labels", []))}),
        "empty": _ranked(empty, lambda key, g: {"key": key[0], "label": key[1], "required": g.get("required", False)}),
        "no_data": _ranked(no_data, lambda key, g: {"key": key}),
        "sites": sorted(
            ({**s, "versions": sorted(s["versions"])} for s in sites.values()),
            key=lambda s: s["failed"] + s["unknown"], reverse=True,
        ),
    }
