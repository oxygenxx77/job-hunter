"""
recommend.py —— 按公司分组推荐岗位

每个岗位在 AI 匹配分之外再算一个「推荐分」，综合五项：
    AI 匹配分 35% + 岗位方向 25% + 城市 20% + 公司规模 12% + 岗位 title 8%
岗位方向和城市按偏好里的优先级分档（如「运营>营销=产品」「深圳>广州=上海>北京」），
不在优先级里的城市不会被排除，只是分数低一些，匹配度高的外地岗位仍然能排上来。

同一家公司的「待确认」岗位按推荐分排序，前端每次展示前 3 个；
用户拒绝其中一个后，下一个自动补位，也可以「换一批」看后面的。
"""

import logging
import re
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

import ai_match
import database as db
import kanban
import nowcoder

logger = logging.getLogger("recommend")

WEIGHTS = {"match": 0.35, "role": 0.25, "city": 0.20, "company": 0.12, "title": 0.08}

# 秋招找的是全职：实习 / 社招岗直接从推荐分里扣
JOB_TYPE_PENALTY = {"实习": 10, "社招": 12}

PER_COMPANY = 3

# 岗位方向 → 岗位名关键词；优先级里写了这里没有的方向时，直接用方向名本身做关键词
ROLE_KEYWORDS = {
    "运营": ["运营"],
    "营销": ["营销", "市场", "品牌", "推广", "增长", "广告", "投放", "公关", "媒介"],
    "市场": ["市场", "营销", "品牌", "推广"],
    "产品": ["产品"],
    "数据": ["数据分析", "数据运营", "分析师", "BI"],
}
# 关键词命中但其实是技术岗的情况，如「产品工程师」「产品信息工程师」
ROLE_EXCLUDE = {"产品": ["工程师", "开发", "研发", "测试", "算法", "硬件", "交付"]}

BROAD_CITY_WORDS = ("全国", "不限", "多地", "远程")

# 牛客卡片上的规模档位 → 分数
SCALE_SCORES = {
    "10000人以上": 100,
    "1000-9999人": 88,
    "500-999人": 72,
    "100-499人": 58,
    "20-99人": 42,
    "0-20人": 28,
}
SCALE_CHOICES = list(SCALE_SCORES) + ["未知"]
KNOWN_BIG_LABEL = "知名大厂"

# 没采到规模时的兜底：校招里常见的大厂 / 知名企业（按公司归一名做包含匹配）
KNOWN_BIG = (
    "腾讯", "阿里", "淘天", "蚂蚁", "字节", "抖音", "美团", "京东", "拼多多", "百度", "网易", "小米",
    "华为", "荣耀", "快手", "滴滴", "哔哩哔哩", "携程", "OPPO", "vivo", "大疆", "比亚迪", "蔚来",
    "小鹏", "理想汽车", "米哈游", "莉莉丝", "三七互娱", "虾皮", "Shopee", "SHEIN", "希音", "顺丰",
    "招商银行", "平安", "中兴", "联想", "TCL", "金山", "唯品会", "小红书", "得物", "微众", "富途",
    "宝洁", "联合利华", "欧莱雅", "雀巢", "玛氏", "百事", "可口可乐", "海尔", "美的", "格力",
    "安克", "传音", "极兔", "货拉拉", "汇丰", "迪卡侬", "泡泡玛特", "巨人网络", "盛趣", "完美世界",
)

# 岗位 title 的加减分（基础 60）
TITLE_RULES: List[Tuple[str, int, str]] = [
    (r"管培|管理培训生|储备干部|领航|星计划|培养计划", 25, "管培生"),
    (r"经理|策划|主管", 10, "经理 / 策划"),
    (r"校招|秋招|应届|\d{2}届|20\d{2}届", 8, "校招"),
    (r"助理", -10, "助理"),
    (r"主播|主持|销售|客服|地推|电话|电销|校园大使|校园代理|兼职|招募", -35, "非核心岗"),
    (r"无责|保底|免费培训|日结", -30, "招聘话术"),
]

_CITY_PREFIX = re.compile(
    r"^(北京|上海|广州|深圳|杭州|成都|武汉|南京|西安|苏州|天津|重庆|长沙|郑州|厦门|合肥|青岛|济南|"
    r"东莞|珠海|佛山|宁波|无锡|福州|大连|沈阳|昆明|南昌|贵阳|宿州)(市)?"
)
_NAME_SUFFIX = re.compile(
    r"(股份有限公司|有限责任公司|有限公司|集团股份|控股|集团|股份|公司|"
    r"信息技术|信息科技|网络科技|网络技术|电子商务|文化传媒|科技|技术|信息|网络|传媒|通信)$"
)


# ---------------------------------------------------------------------------
# 公司名归一
# ---------------------------------------------------------------------------

def company_key(name: str) -> str:
    """把「北京车之家信息技术有限公司」「小米集团」归一成「车之家」「小米」，用来合并同一家公司"""
    s = re.sub(r"[（(][^）)]*[）)]", "", name or "").strip()
    s = re.sub(r"\s*[-－—]\s*[A-Za-z0-9 .&+]+$", "", s)
    if len(s) > 4:
        s = _CITY_PREFIX.sub("", s)
    while True:
        stripped = _NAME_SUFFIX.sub("", s).strip()
        if stripped == s or len(stripped) < 2:
            break
        s = stripped
    return s or (name or "").strip() or "未知公司"


def _known_big(key: str) -> bool:
    lower = key.lower()
    return any(k.lower() in lower or (len(lower) >= 2 and lower in k.lower()) for k in KNOWN_BIG)


# ---------------------------------------------------------------------------
# 单项打分：每项返回 (分数, 说明)
# ---------------------------------------------------------------------------

def _tier_score(index: int) -> int:
    """第 0 档 100，之后每档 -15，最低 55"""
    return max(100 - 15 * index, 55)


def _role_hits(name: str, text: str) -> bool:
    if any(x in text for x in ROLE_EXCLUDE.get(name, [])):
        return False
    return any(k.lower() in text.lower() for k in ROLE_KEYWORDS.get(name, [name]))


def filter_target_roles(jobs: List[Dict[str, Any]], prefs: Dict[str, Any]) -> List[Dict[str, Any]]:
    """只留岗位名或职位类别命中意向方向的岗位；没设置方向时全部保留。
    扩展按公司拉取的岗位，jd_text 第一行是「职位类别：xxx」（牛客的岗位分类）"""
    names = [n for tier in prefs.get("role_tiers") or [] for n in tier] or list(prefs.get("target_roles") or [])
    if not names:
        return jobs

    def hit(job: Dict[str, Any]) -> bool:
        first = (job.get("jd_text") or "").split("\n", 1)[0]
        category = first[len("职位类别："):] if first.startswith("职位类别：") else ""
        return any(_role_hits(n, job.get("title") or "") or (category and _role_hits(n, category)) for n in names)

    return [j for j in jobs if hit(j)]


def role_factor(job: Dict[str, Any], tiers: List[List[str]]) -> Tuple[int, str]:
    if not tiers:
        return 60, "未设置方向优先级"
    title = job.get("title") or ""
    jd = (job.get("jd_text") or "")[:500]
    for i, names in enumerate(tiers):
        for name in names:
            if _role_hits(name, title):
                return _tier_score(i), name
    for i, names in enumerate(tiers):
        for name in names:
            if _role_hits(name, jd):
                return _tier_score(i) - 20, f"JD 涉及{name}"
    return 30, "非目标方向"


def effective_city(job: Dict[str, Any]) -> str:
    """城市字段写「上海/深圳」但岗位名写明「-上海」时，以岗位名为准"""
    city = job.get("city") or ""
    cities = [c for c in re.split(r"[/、,，\s]+", city) if c]
    in_title = [c for c in cities if c in (job.get("title") or "")]
    return "/".join(in_title) if 0 < len(in_title) < len(cities) else city


def city_factor(city: str, tiers: List[List[str]]) -> Tuple[int, str]:
    if not tiers:
        return 60, city or "城市未知"
    if not city:
        return 55, "城市未知"
    best: Optional[Tuple[int, str]] = None
    for i, names in enumerate(tiers):
        for name in names:
            if name in city and (best is None or i < best[0]):
                best = (i, name)
    if best:
        return _tier_score(best[0]), best[1]
    if any(w in city for w in BROAD_CITY_WORDS):
        return 70, "全国多地"
    return 40, f"{city}（非优先城市）"


def company_factor(key: str, scale: str) -> Tuple[int, str]:
    if scale in SCALE_SCORES:
        return SCALE_SCORES[scale], scale
    if _known_big(key):
        return 95, KNOWN_BIG_LABEL
    return 50, "规模未知"


def job_type_of(job: Dict[str, Any]) -> str:
    """校招 / 实习 / 社招；扩展没采到时从岗位名推断"""
    jt = (job.get("job_type") or "").strip()
    if jt:
        return jt
    text = f"{job.get('title') or ''} {job.get('salary') or ''}"
    if "实习" in text or "元/天" in text:
        return "实习"
    if "社招" in text:
        return "社招"
    return ""


def title_factor(job: Dict[str, Any]) -> Tuple[int, List[str]]:
    title = job.get("title") or ""
    score, labels = 60, []
    for pattern, delta, label in TITLE_RULES:
        if re.search(pattern, title):
            score += delta
            labels.append(label)
    return max(0, min(100, score)), labels


# ---------------------------------------------------------------------------
# 推荐分与分组
# ---------------------------------------------------------------------------

def score_job(job: Dict[str, Any], prefs: Dict[str, Any], key: str, scale: str) -> Dict[str, Any]:
    """给单个岗位算推荐分，返回前端需要的字段"""
    match = job.get("match_score")
    analyzing = match is None
    m = 60 if analyzing else int(match)
    role, role_label = role_factor(job, prefs.get("role_tiers") or [])
    city, city_label = city_factor(effective_city(job), prefs.get("city_tiers") or [])
    comp, comp_label = company_factor(key, scale)
    title, title_labels = title_factor(job)
    job_type = job_type_of(job)
    penalty = JOB_TYPE_PENALTY.get(job_type, 0)

    priority = round(
        m * WEIGHTS["match"] + role * WEIGHTS["role"] + city * WEIGHTS["city"]
        + comp * WEIGHTS["company"] + title * WEIGHTS["title"]
    ) - penalty
    # 非优先城市但岗位本身很好：明确标出来，提醒「好岗位也值得考虑」
    away_but_good = city <= 40 and m >= 70 and role >= 70

    return {
        "id": job["id"],
        "title": job.get("title") or "",
        "city": job.get("city") or "",
        "salary": job.get("salary") or "",
        "job_type": job_type,
        "penalty": penalty,
        "link": job.get("link") or "",
        "apply_link": job.get("apply_link") or "",
        "status": job.get("status"),
        "match_score": match,
        "match_advice": job.get("match_advice"),
        "analyzing": analyzing,
        "priority": priority,
        "away_but_good": away_but_good,
        "factors": {
            "match": m,
            "role": {"score": role, "label": role_label},
            "city": {"score": city, "label": city_label},
            "company": {"score": comp, "label": comp_label},
            "title": {"score": title, "labels": title_labels},
        },
    }


def _group_scale(key: str, jobs: List[Dict[str, Any]], profiles: Dict[str, Dict[str, str]]) -> Tuple[str, str]:
    """返回 (规模, 来源)：牛客卡片 > 缓存（牛客 / AI 估算）> 知名大厂名单"""
    for j in jobs:
        if (j.get("company_scale") or "") in SCALE_SCORES:
            return j["company_scale"], "牛客"
    prof = profiles.get(key)
    if prof and prof["scale"] in SCALE_SCORES:
        return prof["scale"], prof["source"]
    if _known_big(key):
        return KNOWN_BIG_LABEL, "内置名单"
    return "", ""


def by_company(per_company: int = PER_COMPANY) -> Dict[str, Any]:
    """
    按公司分组推荐。每家公司返回：
      picked      已选中（待投递 / 已投递）的岗位
      candidates  还没处理的岗位，按推荐分从高到低（前端每次展示 per_company 个）
      more_link   去牛客补采这家公司更多岗位的入口
    """
    prefs = db.get_preferences()
    profiles = db.get_company_scales()
    extras = db.get_company_extras()
    cards = kanban.load_cards() if kanban.is_available() else []
    groups: Dict[str, List[Dict[str, Any]]] = {}
    for job in db.list_jobs():
        groups.setdefault(company_key(job.get("company") or ""), []).append(job)

    companies = []
    for key, jobs in groups.items():
        scale, scale_source = _group_scale(key, jobs, profiles)
        scored = []
        rejected = blacklisted = 0
        for job in jobs:
            if ai_match.check_blacklist(job, prefs):
                blacklisted += 1
                continue
            if job["status"] == "rejected":
                rejected += 1
                continue
            scored.append(score_job(job, prefs, key, scale))
        scored.sort(key=lambda j: (-j["priority"], j["id"]))

        picked = [j for j in scored if j["status"] in ("approved", "applied")]
        candidates = [j for j in scored if j["status"] == "pending"]
        enterprise = next((j["enterprise_link"] for j in jobs if j.get("enterprise_link")), "")
        names = sorted({j.get("company") or "" for j in jobs if j.get("company")}, key=len)
        top = candidates[0]["priority"] if candidates else (picked[0]["priority"] if picked else 0)
        extra = extras.get(key) or {}
        deadline, days_left = nowcoder.deadline_of(extra.get("apply_period") or "")
        card = next((c for n in names if (c := kanban.find_card(n, cards))), None)

        companies.append({
            "key": key,
            "name": names[0] if names else key,
            "aliases": names[1:],
            "scale": scale,
            "scale_source": scale_source,
            "industry": next((j["company_industry"] for j in jobs if j.get("company_industry")), ""),
            "enterprise_link": enterprise,
            "more_link": enterprise or next((j["link"] for j in jobs if j.get("link")), ""),
            "total": len(jobs),
            "picked": picked,
            "candidates": candidates,
            "rejected": rejected,
            "blacklisted": blacklisted,
            "top_priority": top,
            "need_more": len(candidates) + len(picked) < per_company,
            "apply_limit": extra.get("limit"),
            "limit_note": extra.get("note") or "",
            "official_url": extra.get("official_url") or "",
            "notice_url": extra.get("notice_url") or "",
            "apply_period": extra.get("apply_period") or "",
            "batch_name": extra.get("batch_name") or "",
            "deadline": deadline,
            "days_left": days_left,
            "apply_status": nowcoder.apply_status(extra.get("batch_name") or "", days_left),
            "kanban": {
                "company": card.get("company", ""),
                "status_label": kanban.STATUS_LABELS.get(card.get("status"), card.get("status", "")),
                "positions": card.get("positions", ""),
            } if card else None,
        })

    # 还有待选岗位的公司在前，按最高推荐分排序；网申已截止、已经处理完的放后面
    companies.sort(key=lambda c: (
        not c["candidates"],
        c["apply_status"] == "closed",
        -c["top_priority"],
        c["name"],
    ))
    # 秋招看板里有卡片的公司都已经投过（看板没有「未投递」状态），不再推荐，单独列出；
    # 还有「待投递」岗位没投完的先留在上面，投完最后一个再移走
    def done(c: Dict[str, Any]) -> bool:
        return bool(c["kanban"]) and not any(j["status"] == "approved" for j in c["picked"])

    return {
        "per_company": per_company,
        "weights": WEIGHTS,
        "role_priority": prefs.get("role_priority") or "",
        "city_priority": prefs.get("city_priority") or "",
        "companies": [c for c in companies if not done(c)],
        "applied_companies": [c for c in companies if done(c)],
        "estimating_scale": _estimate_lock.locked(),
    }


# ---------------------------------------------------------------------------
# AI 估算公司规模（牛客详情页、旧数据里没有规模时用）
# ---------------------------------------------------------------------------

_estimate_lock = threading.Lock()
_estimate_failed_at = 0.0
ESTIMATE_BATCH = 8
ESTIMATE_COOLDOWN = 600  # 失败后 10 分钟内不再自动重试


def companies_missing_scale() -> List[str]:
    """还不知道规模、也没被 AI 估算过的公司（归一名）"""
    profiles = db.get_company_scales()
    missing = []
    groups: Dict[str, List[Dict[str, Any]]] = {}
    for job in db.list_jobs():
        groups.setdefault(company_key(job.get("company") or ""), []).append(job)
    for key, jobs in groups.items():
        if key in profiles or _group_scale(key, jobs, profiles)[0]:
            continue
        missing.append(key)
    return missing


def _estimate_batch(names: List[str]) -> Dict[str, str]:
    prompt = (
        "估算下面每家中国公司（含其集团）的员工总人数规模，只能从这些档位中选一个："
        + "、".join(SCALE_CHOICES)
        + "。不确定就选「未知」，不要猜。\n只输出一个 JSON 对象，键为公司名（与输入完全一致），值为档位。\n\n公司：\n"
        + "\n".join(names)
    )
    data = ai_match.parse_ai_json(
        ai_match.call_text("你是熟悉中国企业的招聘顾问，只输出 JSON。", prompt, temperature=0.1, timeout=150)
    )
    return {
        name: (str(data.get(name)).strip() if str(data.get(name)).strip() in SCALE_CHOICES else "未知")
        for name in names
    }


def estimate_scales() -> int:
    """让 AI 分批估算缺规模公司的员工规模，写入缓存；返回写入数量"""
    global _estimate_failed_at
    if not ai_match.is_ai_configured() or not _estimate_lock.acquire(blocking=False):
        return 0
    done = 0
    try:
        names = companies_missing_scale()[:40]
        for i in range(0, len(names), ESTIMATE_BATCH):
            result = _estimate_batch(names[i:i + ESTIMATE_BATCH])
            db.save_company_scales(result, "AI估算")
            done += len(result)
        if done:
            logger.info("AI 估算了 %d 家公司的规模", done)
    except Exception as e:
        _estimate_failed_at = time.time()
        logger.warning("AI 估算公司规模失败（已完成 %d 家）：%s", done, e)
    finally:
        _estimate_lock.release()
    return done


def estimate_scales_in_background() -> bool:
    """有缺规模的公司时在后台估算，返回是否启动了"""
    if _estimate_lock.locked() or not ai_match.is_ai_configured():
        return False
    if time.time() - _estimate_failed_at < ESTIMATE_COOLDOWN or not companies_missing_scale():
        return False
    threading.Thread(target=estimate_scales, daemon=True).start()
    return True


def remember_scales(jobs: List[Dict[str, Any]]) -> None:
    """扩展采到的规模写进缓存，之后同一家公司从详情页采的岗位也能用上"""
    found = {
        company_key(j.get("company") or ""): j.get("company_scale")
        for j in jobs
        if (j.get("company_scale") or "") in SCALE_SCORES
    }
    if found:
        db.save_company_scales(found, "牛客")
