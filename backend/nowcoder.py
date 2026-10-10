"""
nowcoder.py —— 查询牛客上的公司校招信息：官网投递入口、校招公告、网申时间

接口不需要登录；公司 ID 取自岗位上的牛客公司主页链接（/enterprise/数字）。
各公司「限投几个岗位」牛客没有提供，公告也多是长图，只能由用户看了公告后手动填写。
"""

import logging
import re
import threading
import time
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional, Tuple

import httpx

import database as db

logger = logging.getLogger("nowcoder")

COMPANY_INFO_API = "https://nowpick.nowcoder.com/u/company/v1/school/schedule/company-info"
SEARCH_API = "https://www.nowcoder.com/np-api/u/job/square-search"
REFRESH_AFTER = timedelta(hours=24)
REQUEST_GAP = 0.3
MAX_PER_RUN = 60
FAIL_COOLDOWN = 600  # 有公司查询失败时，10 分钟内不再自动重查

_lock = threading.Lock()
_failed_at = 0.0


def enterprise_id(link: str) -> str:
    m = re.search(r"/enterprise/(\d+)", link or "")
    return m.group(1) if m else ""


def _http_url(url: str) -> str:
    return url if (url or "").startswith("http") else ""


def fetch_company_info(company_id: str) -> Dict[str, str]:
    resp = httpx.get(
        COMPANY_INFO_API,
        params={"companyId": company_id, "creditCode": "", "where": "zz_enterprise"},
        headers={"User-Agent": "Mozilla/5.0", "Referer": "https://www.nowcoder.com/"},
        timeout=10,
    )
    resp.raise_for_status()
    data = (resp.json() or {}).get("data") or {}
    company = data.get("companyData") or {}
    official = _http_url((data.get("buttonInfo") or {}).get("url") or "")
    if not official:
        official = next((_http_url(s.get("url") or "") for s in company.get("schedules") or [] if s.get("url")), "")
    return {
        "official_url": official,
        "notice_url": _http_url(company.get("sourceInformation") or ""),
        "apply_period": (company.get("timeArrangement") or "").strip(),
        "batch_name": (company.get("batchName") or "").strip(),
    }


def deadline_of(period: str, today: Optional[date] = None) -> Tuple[str, Optional[int]]:
    """从「2026/08/12 ~ 2026/10/12」取截止日，返回 ("10/12", 剩余天数)；没有日期时返回 ("", None)"""
    dates = re.findall(r"(\d{4})[/.-](\d{1,2})[/.-](\d{1,2})", period or "")
    if not dates:
        return "", None
    y, m, d = (int(x) for x in dates[-1])
    try:
        end = date(y, m, d)
    except ValueError:
        return "", None
    return f"{m}/{d}", (end - (today or date.today())).days


def resolve_company_id(name: str) -> str:
    """早期采的岗位没有公司主页链接：按公司名搜岗位，取公司名对得上、出现最多的 companyId"""
    resp = httpx.post(
        SEARCH_API,
        data={"page": 1, "pageSize": 20, "query": name, "recruitType": 1, "requestFrom": 1, "order": 0, "pageSource": 5001},
        headers={"User-Agent": "Mozilla/5.0", "Referer": "https://www.nowcoder.com/jobs/school/jobs"},
        timeout=10,
    )
    resp.raise_for_status()
    votes: Dict[int, int] = {}
    for item in ((resp.json() or {}).get("data") or {}).get("datas") or []:
        job = item.get("data") or {}
        company = job.get("recommendInternCompany") or {}
        names = [n for n in (company.get("companyName"), company.get("companyShortName")) if n]
        if job.get("companyId") and any(n.startswith(name) or name.startswith(n) for n in names):
            votes[job["companyId"]] = votes.get(job["companyId"], 0) + 1
    return str(max(votes, key=votes.get)) if votes else ""


def apply_status(batch_name: str, days_left: Optional[int]) -> str:
    """
    open 网申中 / closing 3 天内截止 / closed 秋招网申已截止 /
    other 牛客上只有实习、补录或往年批次的信息（过期不代表今年秋招结束）/ 空串 未知
    """
    if days_left is None:
        return ""
    if days_left >= 0:
        return "closing" if days_left <= 3 else "open"
    if re.search(r"秋招|校招", batch_name or "") and not re.search(r"实习|补录", batch_name) and days_left > -180:
        return "closed"
    return "other"


def companies_to_refresh() -> Dict[str, Tuple[str, List[str]]]:
    """{公司归一名: (牛客公司 ID 或空, [这家公司在库里的各种写法])}：一天内没查过的"""
    from recommend import company_key

    extras = db.get_company_extras()
    stale_before = (datetime.now() - REFRESH_AFTER).strftime("%Y-%m-%d %H:%M:%S")
    result: Dict[str, Tuple[str, List[str]]] = {}
    for job in db.list_jobs():
        name = job.get("company") or ""
        key = company_key(name)
        if ((extras.get(key) or {}).get("checked_at") or "") >= stale_before:
            continue
        cid, names = result.get(key, ("", []))
        if name and name not in names:
            names.append(name)
        result[key] = (cid or enterprise_id(job.get("enterprise_link") or ""), names)
    return result


def refresh_company_info() -> int:
    global _failed_at
    if not _lock.acquire(blocking=False):
        return 0
    done = 0
    try:
        for key, (cid, names) in list(companies_to_refresh().items())[:MAX_PER_RUN]:
            try:
                if not cid and names:
                    cid = resolve_company_id(min(names, key=len))
                    time.sleep(REQUEST_GAP)
                    if cid:
                        db.fill_enterprise_link(names, f"https://www.nowcoder.com/enterprise/{cid}")
                db.save_company_info(key, fetch_company_info(cid) if cid else {})
                done += 1
            except Exception as e:
                _failed_at = time.time()
                logger.warning("查询牛客公司信息失败（%s / %s）：%s", key, cid, e)
            time.sleep(REQUEST_GAP)
    finally:
        _lock.release()
    if done:
        logger.info("更新了 %d 家公司的牛客校招信息", done)
    return done


def is_refreshing() -> bool:
    return _lock.locked()


def refresh_in_background() -> bool:
    """有需要查询的公司时在后台查，返回是否启动了"""
    if _lock.locked() or time.time() - _failed_at < FAIL_COOLDOWN or not companies_to_refresh():
        return False
    threading.Thread(target=refresh_company_info, daemon=True).start()
    return True
