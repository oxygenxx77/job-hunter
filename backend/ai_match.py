"""
ai_match.py —— AI 匹配分析

流程：
  1. 读取 resume.json（简历）和 preferences 表（求职偏好）
  2. 黑名单公司 / 黑名单岗位 直接判 0 分，不调用 AI
  3. 调用火山引擎 Ark（兼容 OpenAI 协议）获取三个子项分数和理由
  4. 按「简历匹配 50% + 岗位偏好 30% + 城市 20%」加权得到最终匹配分
  5. AI 调用失败（重试后仍失败）或未配置 Key 时，退化为本地关键词规则估算
"""

import json
import logging
import os
import re
import time
from pathlib import Path
from threading import Lock
from typing import Any, Callable, Dict, List, Optional

from dotenv import load_dotenv

import database as db

# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent
# .env 放在项目根目录（backend 的上一级）
load_dotenv(BASE_DIR.parent / ".env")

RESUME_PATH = BASE_DIR / "resume.json"

API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
BASE_URL = os.getenv("OPENAI_BASE_URL", "https://ark.cn-beijing.volces.com/api/coding/v3").strip()
MODEL = os.getenv("OPENAI_MODEL", "kimi-k2.7-code").strip()

# AI 调用最大尝试次数（含首次）
MAX_RETRIES = 3
# 单次请求超时（秒）
REQUEST_TIMEOUT = 60
# 连续分析多个岗位时，每次调用之间的间隔（秒），避免触发限流
CALL_INTERVAL = 0.5

# 匹配度权重：简历 50% + 岗位偏好 30% + 城市 20%
WEIGHTS = {"resume": 0.5, "preference": 0.3, "city": 0.2}

VALID_ADVICES = ("值得投", "谨慎投", "不建议投")

logger = logging.getLogger("ai_match")

# OpenAI 客户端（懒加载，避免未安装/未配置时导入即报错）
_client = None
# 后台批量分析锁，保证同一时间只有一个分析循环在跑
_match_lock = Lock()


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------

def is_ai_configured() -> bool:
    """判断是否已配置有效的 API Key（排除 .env 里的占位文字）"""
    return bool(API_KEY) and "你的" not in API_KEY


def get_client():
    """获取（并缓存）OpenAI 兼容客户端。重试由本模块自己控制，所以关闭 SDK 内置重试"""
    global _client
    if _client is None:
        from openai import OpenAI

        _client = OpenAI(
            api_key=API_KEY,
            base_url=BASE_URL,
            timeout=REQUEST_TIMEOUT,
            max_retries=0,
        )
    return _client


def load_resume() -> Dict[str, Any]:
    """读取简历文件，文件缺失或格式错误时返回空 dict 并打日志"""
    try:
        return json.loads(RESUME_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        logger.warning("未找到简历文件：%s", RESUME_PATH)
    except json.JSONDecodeError as e:
        logger.warning("简历文件 JSON 格式错误：%s", e)
    return {}


def _to_score(value: Any, default: int) -> int:
    """把任意值转换为 0-100 的整数分数，无法转换时使用默认值"""
    try:
        return max(0, min(100, int(round(float(value)))))
    except (TypeError, ValueError):
        return default


def weighted_score(resume_score: int, preference_score: int, city_score: int) -> int:
    """按权重计算最终匹配分"""
    total = (
        resume_score * WEIGHTS["resume"]
        + preference_score * WEIGHTS["preference"]
        + city_score * WEIGHTS["city"]
    )
    return int(round(total))


def advice_from_score(score: int) -> str:
    """根据分数给出投递建议"""
    if score >= 75:
        return "值得投"
    if score >= 55:
        return "谨慎投"
    return "不建议投"


def _hits(text: str, keywords: List[str]) -> List[str]:
    """返回在 text 中出现过的关键词（忽略大小写）"""
    lower = (text or "").lower()
    return [k for k in keywords if k and k.lower() in lower]


def _join(items: List[str]) -> str:
    """列表转顿号分隔字符串，空列表显示「未设置」"""
    return "、".join(items) if items else "未设置"


# ---------------------------------------------------------------------------
# 黑名单与本地规则打分（AI 不可用时的兜底）
# ---------------------------------------------------------------------------

def check_blacklist(job: Dict[str, Any], prefs: Dict[str, Any]) -> Optional[str]:
    """命中黑名单时返回原因，否则返回 None"""
    company = job.get("company") or ""
    title = job.get("title") or ""
    for name in prefs.get("avoid_companies", []):
        if name and name in company:
            return f"公司「{company}」在你的黑名单中"
    for role in prefs.get("avoid_roles", []):
        if role and role in title:
            return f"岗位「{title}」包含黑名单关键词「{role}」"
    return None


def _flat(tiers: List[List[str]]) -> List[str]:
    return [name for tier in tiers for name in tier]


def target_cities_of(prefs: Dict[str, Any]) -> List[str]:
    """目标城市：没填时用城市优先级里的城市"""
    return prefs.get("target_cities") or _flat(prefs.get("city_tiers") or [])


def rule_city_score(city: str, target_cities: List[str]) -> int:
    """城市匹配：命中目标城市 100，全国/远程 75，未设置偏好 70，不匹配 20"""
    if not target_cities:
        return 70
    if not city:
        return 50
    if _hits(city, target_cities):
        return 100
    if any(w in city for w in ("全国", "不限", "远程", "多地")):
        return 75
    return 20


def rule_preference_score(job: Dict[str, Any], prefs: Dict[str, Any]) -> int:
    """岗位偏好匹配：岗位方向占 70%，行业占 30%"""
    title = job.get("title") or ""
    jd = job.get("jd_text") or ""
    company = job.get("company") or ""
    roles = prefs.get("target_roles") or _flat(prefs.get("role_tiers") or [])
    industries = prefs.get("target_industries", [])

    if not roles:
        role_score = 60
    elif _hits(title, roles):
        role_score = 100
    elif _hits(jd, roles):
        role_score = 70
    else:
        role_score = 25

    if not industries:
        industry_score = 60
    elif _hits(f"{company} {title} {jd}", industries):
        industry_score = 100
    else:
        industry_score = 40

    return int(round(role_score * 0.7 + industry_score * 0.3))


def _resume_keywords(resume: Dict[str, Any]) -> List[str]:
    """从简历中提取关键词：技能 + 实习经历中长度≥2 的词"""
    keywords: List[str] = [str(s) for s in resume.get("skills", []) if s]
    for item in resume.get("internships", []):
        keywords.extend(w for w in re.split(r"[\s,，、/|]+", str(item)) if len(w) >= 2)
    return list(dict.fromkeys(keywords))


def rule_resume_score(job: Dict[str, Any], resume: Dict[str, Any]) -> int:
    """简历匹配：基础 40 分，每命中一个简历关键词 +12，封顶 95"""
    text = f"{job.get('title', '')} {job.get('jd_text', '')}"
    hits = set(_hits(text, _resume_keywords(resume)))
    return min(40 + 12 * len(hits), 95)


def rule_based_match(job: Dict[str, Any], resume: Dict[str, Any], prefs: Dict[str, Any], note: str) -> Dict[str, Any]:
    """本地关键词规则估算匹配度"""
    r = rule_resume_score(job, resume)
    p = rule_preference_score(job, prefs)
    c = rule_city_score(job.get("city") or "", target_cities_of(prefs))
    score = weighted_score(r, p, c)
    hits = _hits(f"{job.get('title', '')} {job.get('jd_text', '')}", _resume_keywords(resume))
    reason = (
        f"{note}命中简历关键词：{'、'.join(hits[:6]) if hits else '无'}。"
        f"\n[简历 {r} / 偏好 {p} / 城市 {c}]"
    )
    return {"match_score": score, "match_reason": reason, "match_advice": advice_from_score(score)}


# ---------------------------------------------------------------------------
# AI 调用
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = (
    "你是一名资深的校招 / 实习求职顾问，擅长评估候选人与岗位的匹配程度。"
    "你的评估客观、具体，不说空话。你只输出 JSON，不输出任何其他内容。"
)


def build_prompt(job: Dict[str, Any], resume: Dict[str, Any], prefs: Dict[str, Any]) -> str:
    """构造用户提示词"""
    jd = (job.get("jd_text") or "（列表页未提供 JD，请根据岗位名称和公司推断）")[:3000]
    return f"""请评估下面这个岗位与求职者的匹配程度。

## 求职者简历
{json.dumps(resume, ensure_ascii=False, indent=2)}

## 求职偏好
- 目标岗位：{_join(prefs.get("target_roles", []))}
- 目标城市：{_join(prefs.get("target_cities", []))}
- 目标行业：{_join(prefs.get("target_industries", []))}
- 岗位方向优先级（> 表示更优先，= 表示同等）：{prefs.get("role_priority") or "未设置"}
- 城市优先级：{prefs.get("city_priority") or "未设置"}（不在列表里的城市不直接排除，岗位足够好也可以考虑，只是城市分低一些）

## 岗位信息
- 公司：{job.get("company") or "未知"}
- 岗位：{job.get("title") or "未知"}
- 城市：{job.get("city") or "未知"}
- JD：
{jd}

## 评分规则
分别给出三个子项分数（0-100 的整数）：
1. resume_score：简历匹配度（技能、实习经历、专业背景与 JD 的契合程度）
2. preference_score：岗位偏好匹配度（岗位方向、行业是否符合求职偏好，按方向优先级分档给分；都未设置时给 60）
3. city_score：城市匹配度（按城市优先级分档：第一档 100，之后每档递减约 15；不在优先级里的城市约 40；都未设置时给 70）

总分 match_score = resume_score × 0.5 + preference_score × 0.3 + city_score × 0.2（四舍五入取整）。
match_advice 只能是「值得投」「谨慎投」「不建议投」之一：总分 ≥75 值得投，55-74 谨慎投，<55 不建议投。
match_reason 用 2-3 句中文，直接对求职者说话（用"你"），点出最关键的匹配点和短板。

## 输出格式
只输出一个 JSON 对象，不要输出 Markdown 代码块或任何解释文字：
{{"resume_score": 80, "preference_score": 90, "city_score": 100, "match_score": 87, "match_reason": "你的电商运营经验与岗位高度匹配……", "match_advice": "值得投"}}
"""


def parse_ai_json(text: str) -> Dict[str, Any]:
    """从模型输出中解析 JSON：兼容 ```json 代码块包裹、前后夹杂文字等情况"""
    cleaned = (text or "").strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", cleaned, re.S)
        if not match:
            raise ValueError(f"模型输出中没有 JSON：{cleaned[:200]}")
        data = json.loads(match.group(0))
    if not isinstance(data, dict):
        raise ValueError("模型输出的 JSON 不是对象")
    return data


def _with_retry(request: Callable[[], Any]) -> Any:
    """
    执行一次 AI 请求，带指数退避重试（2s、4s……）。
    鉴权失败、模型不存在、请求参数错误这类重试也无用的错误会立即放弃。
    """
    from openai import AuthenticationError, BadRequestError, NotFoundError, PermissionDeniedError

    fatal_errors = (AuthenticationError, BadRequestError, NotFoundError, PermissionDeniedError)
    last_error: Optional[Exception] = None

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            return request()
        except fatal_errors as e:
            # 不可恢复的错误，直接抛出
            raise RuntimeError(f"AI 请求被拒绝（{type(e).__name__}）：{e}") from e
        except Exception as e:  # 网络超时、限流、5xx、JSON 解析失败等，均可重试
            last_error = e
            logger.warning("AI 调用第 %d/%d 次失败：%s", attempt, MAX_RETRIES, e)
            if attempt < MAX_RETRIES:
                time.sleep(2 ** attempt)

    raise RuntimeError(f"AI 调用失败（已尝试 {MAX_RETRIES} 次）：{last_error}")


def call_ai(prompt: str) -> Dict[str, Any]:
    """调用 AI 做匹配分析并解析 JSON（解析失败也会重试）"""
    def request() -> Dict[str, Any]:
        resp = get_client().chat.completions.create(
            model=MODEL,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            temperature=0.2,
        )
        return parse_ai_json(resp.choices[0].message.content or "")

    return _with_retry(request)


def call_text(system: str, prompt: str, temperature: float = 0.7, timeout: Optional[float] = None) -> str:
    """调用 AI 生成一段纯文本（求职文书等），返回为空也会重试"""
    def request() -> str:
        client = get_client().with_options(timeout=timeout) if timeout else get_client()
        resp = client.chat.completions.create(
            model=MODEL,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": prompt}],
            temperature=temperature,
        )
        content = (resp.choices[0].message.content or "").strip()
        if not content:
            raise ValueError("AI 返回内容为空")
        return content

    return _with_retry(request)


# ---------------------------------------------------------------------------
# 对外接口
# ---------------------------------------------------------------------------

def analyze_job(
    job: Dict[str, Any],
    resume: Optional[Dict[str, Any]] = None,
    prefs: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    分析单个岗位，返回：
    {"match_score": int, "match_reason": str, "match_advice": str}
    """
    resume = resume if resume is not None else load_resume()
    prefs = prefs if prefs is not None else db.get_preferences()

    # 1. 黑名单直接判 0 分
    blacklist_reason = check_blacklist(job, prefs)
    if blacklist_reason:
        return {
            "match_score": 0,
            "match_reason": f"{blacklist_reason}，已自动标记为不建议投。",
            "match_advice": "不建议投",
        }

    # 2. 未配置 Key：使用规则估算
    if not is_ai_configured():
        return rule_based_match(job, resume, prefs, "（未配置 AI Key，以下为关键词规则估算）")

    # 3. 调用 AI
    try:
        data = call_ai(build_prompt(job, resume, prefs))
    except Exception as e:
        logger.error("岗位 %s 的 AI 分析失败：%s", job.get("id"), e)
        db.add_risk_log("异常", f"AI 分析失败（{job.get('company')} - {job.get('title')}）：{e}")
        return rule_based_match(job, resume, prefs, "（AI 调用失败，以下为关键词规则估算）")

    # AI 缺失的子项用规则分兜底，最终分数由代码按权重统一计算，保证口径一致
    r = _to_score(data.get("resume_score"), rule_resume_score(job, resume))
    p = _to_score(data.get("preference_score"), rule_preference_score(job, prefs))
    c = _to_score(data.get("city_score"), rule_city_score(job.get("city") or "", target_cities_of(prefs)))
    score = weighted_score(r, p, c)

    advice = str(data.get("match_advice") or "").strip()
    if advice not in VALID_ADVICES:
        advice = advice_from_score(score)

    reason = str(data.get("match_reason") or "").strip() or "AI 未给出具体理由。"
    reason += f"\n[简历 {r} / 偏好 {p} / 城市 {c}]"

    return {"match_score": score, "match_reason": reason, "match_advice": advice}


def run_pending_matches() -> int:
    """
    后台任务：循环分析所有未打分的岗位，直到没有剩余为止。
    使用非阻塞锁保证同一时间只有一个循环在跑；
    释放锁后再检查一次，防止在释放瞬间新插入的岗位被遗漏。
    返回本次调用分析的岗位数。
    """
    processed = 0
    while True:
        if not _match_lock.acquire(blocking=False):
            # 已有其他循环在处理，它会顺带处理新岗位
            return processed
        try:
            resume = load_resume()
            prefs = db.get_preferences()
            while True:
                jobs = db.list_unscored_jobs(limit=20)
                if not jobs:
                    break
                for job in jobs:
                    try:
                        result = analyze_job(job, resume, prefs)
                    except Exception as e:  # 兜底：任何意外都不能让循环卡死在同一个岗位
                        logger.exception("分析岗位 %s 出现意外错误", job.get("id"))
                        result = {
                            "match_score": 0,
                            "match_reason": f"分析出错：{e}",
                            "match_advice": "谨慎投",
                        }
                    db.update_job_match(job["id"], result["match_score"], result["match_reason"], result["match_advice"])
                    processed += 1
                    if is_ai_configured():
                        time.sleep(CALL_INTERVAL)
        finally:
            _match_lock.release()

        if not db.list_unscored_jobs(limit=1):
            return processed
