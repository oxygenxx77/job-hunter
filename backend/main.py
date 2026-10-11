"""
main.py —— 求职助手 FastAPI 主服务

启动方式（在 backend 目录下）：
    uvicorn main:app --reload --port 8001
或：
    python main.py

启动后访问 http://127.0.0.1:8001/ 即可打开 Web 页面，
http://127.0.0.1:8001/docs 可查看自动生成的接口文档。
（8000 端口留给秋招看板）
"""

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field

import actions
import ai_match
import autofill
import chat
import database as db
import kanban
import nowcoder
import recommend

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("main")

FRONTEND_FILE = Path(__file__).resolve().parent.parent / "frontend" / "index.html"


# ---------------------------------------------------------------------------
# 应用生命周期
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    """启动时初始化数据库；如有上次未分析完的岗位，继续在后台分析"""
    db.init_db()
    if not ai_match.is_ai_configured():
        logger.warning("未配置有效的 OPENAI_API_KEY，岗位将使用本地关键词规则估算匹配度")
    if db.list_unscored_jobs(limit=1):
        actions.start_background_matching()
    yield


app = FastAPI(title="求职助手 API", version="1.0.0", lifespan=lifespan)

# 允许浏览器扩展（chrome-extension://）和本地文件（file://）跨域访问
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# 请求体模型
# ---------------------------------------------------------------------------

class JobIn(BaseModel):
    """扩展采集到的单个岗位"""
    company: str = ""
    title: str = ""
    city: str = ""
    link: str = ""
    apply_link: str = ""
    jd_text: str = ""
    source: str = "牛客"
    salary: str = ""
    job_type: str = ""
    company_scale: str = ""
    company_industry: str = ""
    enterprise_link: str = ""


class JobsIn(BaseModel):
    """扩展批量上报的岗位列表"""
    jobs: List[JobIn] = Field(default_factory=list)
    source: Optional[str] = None
    only_target_roles: bool = False


class CompanyLimitIn(BaseModel):
    """手动填写某家公司限投几个岗位"""
    key: str = Field(..., min_length=1, max_length=100)
    apply_limit: Optional[int] = Field(None, ge=0, le=20)
    note: str = Field("", max_length=200)


class CompanyAppliedIn(BaseModel):
    """这家公司投完了（key 为按公司推荐里的公司归一名）"""
    key: str = Field(..., min_length=1, max_length=100)


class StatusIn(BaseModel):
    """修改单个岗位状态"""
    status: str


class BatchStatusIn(BaseModel):
    """批量修改岗位状态"""
    ids: List[int]
    status: str


class PreferencesIn(BaseModel):
    """用户偏好"""
    target_roles: List[str] = Field(default_factory=list)
    target_cities: List[str] = Field(default_factory=list)
    target_industries: List[str] = Field(default_factory=list)
    avoid_companies: List[str] = Field(default_factory=list)
    avoid_roles: List[str] = Field(default_factory=list)
    daily_limit: int = Field(10, ge=1, le=200)
    role_priority: Optional[str] = Field(None, max_length=200)
    city_priority: Optional[str] = Field(None, max_length=200)


class RematchIn(BaseModel):
    """重新分析：不传 ids 表示重新分析所有「待确认」岗位"""
    ids: Optional[List[int]] = None


class ChatMessage(BaseModel):
    """单条对话消息；images 为前端压缩后的 data URL，只有最新一条用户消息会带"""
    role: str
    content: str = Field("", max_length=8000)
    images: List[str] = []


MAX_CHAT_IMAGES = 4
MAX_IMAGE_CHARS = 6_000_000
IMAGE_PREFIXES = tuple(f"data:image/{fmt};base64," for fmt in ("png", "jpeg", "webp", "gif"))


class ChatIn(BaseModel):
    """AI 对话请求：历史消息 + 当前正在查看的岗位（可选）"""
    messages: List[ChatMessage]
    job_id: Optional[int] = None


class ResumeSyncIn(BaseModel):
    """「简历自动填充」扩展保存的简历（原样传入，后端只取白名单字段）"""
    resume: Dict[str, Any]


class WriteIn(BaseModel):
    """为岗位生成网申文书：summary = 自我评价，answer = 回答开放题"""
    job_id: int
    kind: str = "summary"
    question: str = Field("", max_length=500)
    max_chars: int = Field(300, ge=50, le=2000)


class FillReportsIn(BaseModel):
    """扩展上报的填充报告（一次可以带上之前没发出去的几份）"""
    reports: List[Dict[str, Any]] = Field(default_factory=list, max_length=50)


class ResolveReportsIn(BaseModel):
    """把报告标记为已处理：给 ids，或者给 up_to_id（这个 id 及之前的全部）"""
    ids: List[int] = Field(default_factory=list)
    up_to_id: Optional[int] = None


def _dump(model: BaseModel) -> Dict[str, Any]:
    """兼容 pydantic v1 / v2 的模型转 dict"""
    return model.model_dump() if hasattr(model, "model_dump") else model.dict()


def _validate_status(status: str) -> None:
    """校验状态值是否合法"""
    if status not in db.VALID_STATUSES:
        raise HTTPException(400, f"无效状态：{status}，可选值：{', '.join(db.VALID_STATUSES)}")


# ---------------------------------------------------------------------------
# 风险与投递（逻辑在 actions.py，与 AI 顾问共用）
# ---------------------------------------------------------------------------

build_risk_status = actions.build_risk_status


def apply_job(job_id: int) -> Dict[str, Any]:
    """把岗位标记为已投递，业务错误转换为 HTTP 错误"""
    try:
        return actions.apply_job(job_id)
    except actions.ActionError as e:
        raise HTTPException(e.status_code, str(e))


# ---------------------------------------------------------------------------
# 前端页面
# ---------------------------------------------------------------------------

@app.get("/", include_in_schema=False)
def index():
    """返回 Web 前端页面"""
    if not FRONTEND_FILE.exists():
        raise HTTPException(404, "未找到 frontend/index.html")
    return FileResponse(FRONTEND_FILE)


# ---------------------------------------------------------------------------
# 岗位相关接口
# ---------------------------------------------------------------------------

@app.get("/api/jobs")
def list_jobs(
    status: Optional[str] = Query(None, description="状态筛选：pending/approved/applied/rejected"),
    min_score: Optional[int] = Query(None, ge=0, le=100, description="最低匹配分"),
    city: Optional[str] = Query(None, description="城市（模糊匹配）"),
):
    """返回岗位列表（按匹配度从高到低），附带该公司在秋招看板中的进度"""
    if status:
        _validate_status(status)
    jobs = db.list_jobs(status=status, min_score=min_score, city=(city or "").strip() or None)
    return kanban.attach_progress(jobs)


@app.post("/api/jobs")
def create_jobs(payload: JobsIn):
    """接收扩展发来的岗位列表，去重入库，并在后台触发 AI 分析"""
    jobs = []
    for job in payload.jobs:
        item = _dump(job)
        if payload.source and not job.source:
            item["source"] = payload.source
        jobs.append(item)

    received = len(jobs)
    recommend.remember_scales(jobs)
    if payload.only_target_roles:
        jobs = recommend.filter_target_roles(jobs, db.get_preferences())

    result = db.insert_jobs(jobs)
    # 用守护线程而不是 BackgroundTasks：后者会让服务重启 / 退出时一直等到全部分析完
    if result["inserted_ids"]:
        actions.start_background_matching()

    return {
        "received": received,
        "filtered": received - len(jobs),
        "inserted": len(result["inserted_ids"]),
        "duplicated": result["duplicated"],
        "invalid": result["invalid"],
        "ids": result["inserted_ids"],
    }


@app.post("/api/jobs/batch_status")
def batch_status(payload: BatchStatusIn):
    """批量修改状态（「已投递」需逐个通过投递接口标记，以便受每日上限约束）"""
    _validate_status(payload.status)
    if payload.status == "applied":
        raise HTTPException(400, "批量操作不支持直接标记为已投递，请逐个投递")
    updated = db.batch_update_status(payload.ids, payload.status)
    return {"ok": True, "updated": updated}


@app.get("/api/jobs/{job_id}")
def get_job(job_id: int):
    """返回单个岗位详情，附带该公司在秋招看板中的进度"""
    job = db.get_job(job_id)
    if not job:
        raise HTTPException(404, "岗位不存在")
    return kanban.attach_progress([job])[0]


@app.post("/api/jobs/{job_id}/status")
def update_status(job_id: int, payload: StatusIn):
    """修改单个岗位状态；改为 applied 时走投递逻辑（受每日上限约束）"""
    _validate_status(payload.status)
    if payload.status == "applied":
        return apply_job(job_id)
    if not db.update_job_status(job_id, payload.status):
        raise HTTPException(404, "岗位不存在")
    return {"ok": True, "job": db.get_job(job_id)}


@app.get("/api/recommendations")
def recommendations(per_company: int = Query(recommend.PER_COMPANY, ge=1, le=10)):
    """按公司分组推荐：每家公司的待选岗位按推荐分排序；有公司缺规模时顺带在后台让 AI 估算"""
    started = recommend.estimate_scales_in_background()
    fetching = nowcoder.refresh_in_background()
    data = recommend.by_company(per_company)
    data["estimating_scale"] = data["estimating_scale"] or started
    data["fetching_info"] = fetching or nowcoder.is_refreshing()
    return data


@app.post("/api/companies/limit")
def set_company_limit(payload: CompanyLimitIn):
    """手动填写某家公司限投几个岗位（0 = 不限）；apply_limit 为空表示清除"""
    db.save_company_limit(payload.key, payload.apply_limit, payload.note.strip())
    return {"ok": True}


@app.post("/api/companies/applied")
def mark_company_applied(payload: CompanyAppliedIn):
    """
    这家公司投完了：「待投递」的岗位全部标记为已投递（受每日上限约束，逐个同步看板）；
    一个待投递都没有时直接在秋招看板建一张「已投递」卡片。
    有看板卡片的公司会移到「已完成投递」栏，不再推荐。
    """
    if not kanban.is_available():
        raise HTTPException(400, "本机没有找到秋招看板，没法同步")
    jobs = [j for j in db.list_jobs() if recommend.company_key(j.get("company") or "") == payload.key]
    if not jobs:
        raise HTTPException(404, "没有找到这家公司")
    name = min((j["company"] for j in jobs if j.get("company")), key=len, default=payload.key)

    applied, error, failed_sync = 0, None, None
    for job in sorted((j for j in jobs if j["status"] == "approved"), key=lambda j: j["id"]):
        try:
            result = actions.apply_job(job["id"])
        except actions.ActionError as e:
            error = str(e)
            break
        applied += 1
        if "失败" in (result.get("kanban_sync") or ""):
            failed_sync = result["kanban_sync"]
    sync = failed_sync or (f"已同步到秋招看板（{applied} 个岗位）" if applied else None)

    if not applied and not error:
        card = next((c for n in {j.get("company") or "" for j in jobs}
                     if n and (c := kanban.find_card(n, kanban.load_cards()))), None)
        if card:
            sync = f"秋招看板里已经有「{card['company']}」了"
        else:
            titles = [j["title"] for j in jobs if j["status"] == "applied" and j.get("title")]
            sync = kanban.upsert_card(name, note="来自求职助手", add_position="、".join(titles) or None,
                                      history_note="求职助手：这家公司投完了")
    return {"ok": error is None, "company": name, "applied": applied, "kanban_sync": sync, "error": error}


@app.post("/api/rematch")
def rematch(payload: RematchIn):
    """清空匹配结果并在后台重新分析（修改偏好或简历后使用）"""
    count = db.reset_matches(payload.ids)
    if count:
        actions.start_background_matching()
    return {"ok": True, "reset": count}


# ---------------------------------------------------------------------------
# 偏好相关接口
# ---------------------------------------------------------------------------

@app.get("/api/preferences")
def get_preferences():
    """读取用户偏好"""
    return db.get_preferences()


@app.post("/api/preferences")
def save_preferences(payload: PreferencesIn):
    """保存用户偏好"""
    return db.save_preferences(_dump(payload))


# ---------------------------------------------------------------------------
# 投递相关接口
# ---------------------------------------------------------------------------

@app.get("/api/next_approved")
def next_approved():
    """返回一个已确认待投递的岗位；没有时 job 为 null"""
    risk = build_risk_status()
    return {
        "job": db.get_next_approved(),
        "can_apply": risk["can_apply"],
        "remaining": risk["remaining"],
    }


@app.post("/api/mark_applied/{job_id}")
def mark_applied(job_id: int):
    """标记岗位为已投递"""
    return apply_job(job_id)


@app.post("/api/jobs/{job_id}/apply_intent")
def apply_intent(job_id: int):
    """网页上点了「投递」：记下岗位，简历填充扩展在网申页上据此关联岗位"""
    if not db.get_job(job_id):
        raise HTTPException(404, "岗位不存在")
    autofill.record_intent(job_id)
    return {"ok": True}


# ---------------------------------------------------------------------------
# 简历自动填充扩展联动
# ---------------------------------------------------------------------------

@app.post("/api/resume/sync")
def resume_sync(payload: ResumeSyncIn):
    """扩展把简历同步过来，合并进 resume.json"""
    try:
        return autofill.sync_resume(payload.resume)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/api/autofill/context")
def autofill_context(url: str = ""):
    """扩展弹窗打开时调用：识别当前网申页对应的岗位"""
    return autofill.context_for(url)


@app.post("/api/autofill/write")
def autofill_write(payload: WriteIn):
    """按岗位 JD 生成自我评价 / 开放题回答"""
    try:
        return {"text": autofill.write_text(payload.job_id, payload.kind, payload.question, payload.max_chars)}
    except LookupError as e:
        raise HTTPException(404, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))
    except RuntimeError as e:
        raise HTTPException(502, str(e))


@app.post("/api/autofill/report")
def autofill_report(payload: FillReportsIn):
    """扩展每次填完上报：哪些字段没填上、哪些标签不认识（不含简历内容）"""
    return {"saved": autofill.save_reports(payload.reports)}


@app.get("/api/autofill/reports/digest")
def autofill_report_digest(include_resolved: bool = False):
    """按问题汇总未处理的填充报告，改填充规则时先看这个"""
    return autofill.report_digest(include_resolved)


@app.get("/api/autofill/reports")
def autofill_reports(limit: int = Query(20, ge=1, le=200), include_resolved: bool = False):
    """最近的原始填充报告"""
    return {"reports": db.list_fill_reports(include_resolved, limit)}


@app.post("/api/autofill/reports/resolve")
def autofill_reports_resolve(payload: ResolveReportsIn):
    """规则改完后把对应报告标记为已处理"""
    return {"resolved": db.resolve_fill_reports(payload.ids, payload.up_to_id)}


# ---------------------------------------------------------------------------
# AI 对话接口
# ---------------------------------------------------------------------------

@app.post("/api/chat")
def chat_stream(payload: ChatIn):
    """与 AI 求职顾问对话。返回 NDJSON 流，每行一个事件：text（回复文字）/ action（执行的操作）/ error"""
    images = [url for m in payload.messages for url in m.images]
    if len(images) > MAX_CHAT_IMAGES:
        raise HTTPException(400, f"一次最多发 {MAX_CHAT_IMAGES} 张图片")
    for url in images:
        if not url.startswith(IMAGE_PREFIXES):
            raise HTTPException(400, "图片格式不支持，请用 PNG / JPG / WebP / GIF")
        if len(url) > MAX_IMAGE_CHARS:
            raise HTTPException(400, "图片太大了，请截小一点再发")

    messages = []
    for m in payload.messages:
        text = m.content.strip()
        if m.role not in ("user", "assistant") or not (text or m.images):
            continue
        if m.role == "user" and m.images:
            content: Any = [{"type": "text", "text": text or "请看这张图片。"}]
            content += [{"type": "image_url", "image_url": {"url": url}} for url in m.images]
            messages.append({"role": "user", "content": content})
        else:
            messages.append({"role": m.role, "content": text})
    if not messages or messages[-1]["role"] != "user":
        raise HTTPException(400, "最后一条消息必须是用户消息")
    return StreamingResponse(
        chat.stream_chat(messages, payload.job_id),
        media_type="application/x-ndjson; charset=utf-8",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ---------------------------------------------------------------------------
# 风险 & 统计接口
# ---------------------------------------------------------------------------

@app.get("/api/risk_status")
def risk_status():
    """返回当前账号风险状态"""
    return build_risk_status()


@app.get("/api/stats")
def stats():
    """返回统计数据"""
    data = db.get_stats()
    data["ai_configured"] = ai_match.is_ai_configured()
    data["model"] = ai_match.MODEL
    data["kanban_available"] = kanban.is_available()
    return data


if __name__ == "__main__":
    import uvicorn

    # 秋招看板占用 8000 端口，求职助手使用 8001
    uvicorn.run("main:app", host="127.0.0.1", port=8001, reload=True)
