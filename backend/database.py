"""
database.py —— SQLite 数据库操作封装

所有 SQL 语句都集中在本文件中，main.py / ai_match.py 只调用这里暴露的函数，
不直接拼写 SQL。数据库文件 jobs.db 会在首次启动时自动创建。
"""

import json
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

# 数据库文件路径：与本文件同目录下的 jobs.db
DB_PATH = Path(__file__).resolve().parent / "jobs.db"

# 岗位允许的状态
VALID_STATUSES = ("pending", "approved", "applied", "rejected")

# 偏好表中以 JSON 数组存储的字段
PREF_LIST_FIELDS = (
    "target_roles",
    "target_cities",
    "target_industries",
    "avoid_companies",
    "avoid_roles",
)

# 默认每日投递上限
DEFAULT_DAILY_LIMIT = 10

# 偏好表中的优先级字段：形如「运营>营销=产品」，> 分档、= 同档
PREF_TIER_FIELDS = ("role_priority", "city_priority")

# 扩展从牛客卡片上额外采集的字段（旧库启动时自动补列）
JOB_EXTRA_COLUMNS = {
    "salary": "TEXT DEFAULT ''",
    "job_type": "TEXT DEFAULT ''",
    "company_scale": "TEXT DEFAULT ''",
    "company_industry": "TEXT DEFAULT ''",
    "enterprise_link": "TEXT DEFAULT ''",
}

# 公司档案的补充列：
#   apply_limit 限投几个岗位（用户手动填写；NULL 未知，0 不限）
#   official_url / notice_url / apply_period / batch_name 来自牛客公司校招信息，info_checked_at 为上次查询时间
PROFILE_EXTRA_COLUMNS = {
    "apply_limit": "INTEGER",
    "limit_note": "TEXT DEFAULT ''",
    "limit_source": "TEXT DEFAULT ''",
    "official_url": "TEXT DEFAULT ''",
    "notice_url": "TEXT DEFAULT ''",
    "apply_period": "TEXT DEFAULT ''",
    "batch_name": "TEXT DEFAULT ''",
    "info_checked_at": "TEXT DEFAULT ''",
}
PROFILE_INFO_FIELDS = ("official_url", "notice_url", "apply_period", "batch_name")


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------

def now_str() -> str:
    """返回当前本地时间字符串，格式：YYYY-MM-DD HH:MM:SS"""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def today_str() -> str:
    """返回今天的日期字符串，格式：YYYY-MM-DD"""
    return datetime.now().strftime("%Y-%m-%d")


@contextmanager
def get_conn():
    """
    获取数据库连接的上下文管理器。
    - 正常结束自动 commit，异常自动 rollback
    - row_factory 设为 sqlite3.Row，方便转成 dict
    """
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _row_to_dict(row: Optional[sqlite3.Row]) -> Optional[Dict[str, Any]]:
    """把 sqlite3.Row 转为普通 dict，None 原样返回"""
    return dict(row) if row is not None else None


def _clean(value: Any) -> str:
    """把任意值转成去掉首尾空白的字符串"""
    return str(value or "").strip()


def _ensure_columns(conn: sqlite3.Connection, table: str, columns: Dict[str, str]) -> None:
    """给已有的表补上缺失的列（CREATE TABLE IF NOT EXISTS 不会改旧表）"""
    existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
    for name, decl in columns.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")


# ---------------------------------------------------------------------------
# 初始化
# ---------------------------------------------------------------------------

def init_db() -> None:
    """创建所有数据表和索引，并确保偏好表有一条默认记录"""
    with get_conn() as conn:
        # WAL 模式：读写并发更友好（后台 AI 分析线程与 API 请求同时访问）
        conn.execute("PRAGMA journal_mode=WAL")

        # 岗位信息表
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS jobs (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                company      TEXT NOT NULL DEFAULT '',
                title        TEXT NOT NULL DEFAULT '',
                city         TEXT DEFAULT '',
                link         TEXT DEFAULT '',
                apply_link   TEXT DEFAULT '',
                jd_text      TEXT DEFAULT '',
                source       TEXT DEFAULT '牛客',
                match_score  INTEGER,
                match_reason TEXT,
                match_advice TEXT,
                status       TEXT NOT NULL DEFAULT 'pending',
                created_at   TEXT NOT NULL,
                applied_at   TEXT
            )
            """
        )
        _ensure_columns(conn, "jobs", JOB_EXTRA_COLUMNS)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_link ON jobs(link)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_applied_at ON jobs(applied_at)")

        # 用户偏好表（全局只使用 id=1 这一条记录）
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS preferences (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                target_roles      TEXT DEFAULT '[]',
                target_cities     TEXT DEFAULT '[]',
                target_industries TEXT DEFAULT '[]',
                avoid_companies   TEXT DEFAULT '[]',
                avoid_roles       TEXT DEFAULT '[]',
                daily_limit       INTEGER DEFAULT 10,
                updated_at        TEXT
            )
            """
        )
        _ensure_columns(conn, "preferences", {f: "TEXT DEFAULT ''" for f in PREF_TIER_FIELDS})

        # 公司规模缓存：牛客卡片上采到的优先，其次是 AI 估算
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS company_profiles (
                name       TEXT PRIMARY KEY,
                scale      TEXT DEFAULT '',
                source     TEXT DEFAULT '',
                updated_at TEXT
            )
            """
        )
        _ensure_columns(conn, "company_profiles", PROFILE_EXTRA_COLUMNS)

        # 风险记录表
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS risk_logs (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                event_type  TEXT NOT NULL,
                description TEXT DEFAULT '',
                created_at  TEXT NOT NULL
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_risk_created ON risk_logs(created_at)")

        # 确保存在默认偏好记录
        row = conn.execute("SELECT id FROM preferences WHERE id = 1").fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO preferences (id, daily_limit, updated_at) VALUES (1, ?, ?)",
                (DEFAULT_DAILY_LIMIT, now_str()),
            )


# ---------------------------------------------------------------------------
# 岗位相关
# ---------------------------------------------------------------------------

def insert_jobs(jobs: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    """
    批量插入岗位，自动去重。
    去重规则：
      1. link 非空且已存在 → 视为重复
      2. 公司 + 岗位名 + 城市 完全相同 → 视为重复
    重复的岗位如果这次带来了旧记录缺少的薪资 / 规模等信息，会补到旧记录上。
    返回：{"inserted_ids": [...], "duplicated": n, "invalid": n}
    """
    inserted_ids: List[int] = []
    duplicated = 0
    invalid = 0
    created_at = now_str()

    with get_conn() as conn:
        for job in jobs:
            company = _clean(job.get("company"))
            title = _clean(job.get("title"))
            city = _clean(job.get("city"))
            link = _clean(job.get("link"))
            apply_link = _clean(job.get("apply_link")) or link
            jd_text = _clean(job.get("jd_text"))
            source = _clean(job.get("source")) or "牛客"
            extra = {col: _clean(job.get(col))[:200] for col in JOB_EXTRA_COLUMNS}

            # 没有岗位名的数据视为无效
            if not title:
                invalid += 1
                continue

            # 规则 1：按链接去重；规则 2：按 公司+岗位+城市 去重
            exists = None
            if link:
                exists = conn.execute("SELECT id FROM jobs WHERE link = ?", (link,)).fetchone()
            if not exists:
                exists = conn.execute(
                    "SELECT id FROM jobs WHERE company = ? AND title = ? AND city = ?",
                    (company, title, city),
                ).fetchone()
            if exists:
                duplicated += 1
                _backfill_extra(conn, exists["id"], extra)
                continue

            cols = ", ".join(JOB_EXTRA_COLUMNS)
            marks = ", ".join("?" for _ in JOB_EXTRA_COLUMNS)
            cur = conn.execute(
                f"""
                INSERT INTO jobs
                    (company, title, city, link, apply_link, jd_text, source, status, created_at, {cols})
                VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, {marks})
                """,
                (company, title, city, link, apply_link, jd_text, source, created_at, *extra.values()),
            )
            inserted_ids.append(cur.lastrowid)

    return {"inserted_ids": inserted_ids, "duplicated": duplicated, "invalid": invalid}


def _backfill_extra(conn: sqlite3.Connection, job_id: int, extra: Dict[str, str]) -> None:
    """只填旧记录里为空的附加字段，不覆盖已有值"""
    for col, value in extra.items():
        if value:
            conn.execute(
                f"UPDATE jobs SET {col} = ? WHERE id = ? AND COALESCE({col}, '') = ''",
                (value, job_id),
            )


def list_jobs(
    status: Optional[str] = None,
    min_score: Optional[int] = None,
    city: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    查询岗位列表，支持按状态、最低匹配分、城市（模糊匹配）筛选。
    排序：已打分的按匹配分从高到低，未打分（分析中）的排在最后。
    """
    sql = "SELECT * FROM jobs WHERE 1 = 1"
    params: List[Any] = []

    if status:
        sql += " AND status = ?"
        params.append(status)
    if min_score is not None:
        sql += " AND match_score >= ?"
        params.append(min_score)
    if city:
        sql += " AND city LIKE ?"
        params.append(f"%{city}%")

    sql += " ORDER BY (match_score IS NULL) ASC, match_score DESC, created_at DESC, id DESC"

    with get_conn() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def get_job(job_id: int) -> Optional[Dict[str, Any]]:
    """按 id 获取单个岗位，不存在返回 None"""
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    return _row_to_dict(row)


def update_job_status(job_id: int, status: str) -> bool:
    """
    修改单个岗位状态。
    - 改为 applied 时自动记录投递时间（已有则保留）
    - 改为其他状态时清空投递时间
    返回是否有记录被更新。
    """
    if status not in VALID_STATUSES:
        raise ValueError(f"无效状态：{status}")
    with get_conn() as conn:
        cur = conn.execute(
            """
            UPDATE jobs
               SET status = ?,
                   applied_at = CASE WHEN ? = 'applied' THEN COALESCE(applied_at, ?) ELSE NULL END
             WHERE id = ?
            """,
            (status, status, now_str(), job_id),
        )
    return cur.rowcount > 0


def batch_update_status(job_ids: List[int], status: str) -> int:
    """批量修改岗位状态，返回实际更新的条数"""
    if status not in VALID_STATUSES:
        raise ValueError(f"无效状态：{status}")
    if not job_ids:
        return 0
    placeholders = ",".join("?" for _ in job_ids)
    with get_conn() as conn:
        cur = conn.execute(
            f"""
            UPDATE jobs
               SET status = ?,
                   applied_at = CASE WHEN ? = 'applied' THEN COALESCE(applied_at, ?) ELSE NULL END
             WHERE id IN ({placeholders})
            """,
            [status, status, now_str(), *job_ids],
        )
    return cur.rowcount


def update_job_match(job_id: int, score: int, reason: str, advice: str) -> None:
    """写入 AI 匹配分析结果"""
    with get_conn() as conn:
        conn.execute(
            "UPDATE jobs SET match_score = ?, match_reason = ?, match_advice = ? WHERE id = ?",
            (score, reason, advice, job_id),
        )


def list_unscored_jobs(limit: Optional[int] = None) -> List[Dict[str, Any]]:
    """获取尚未进行 AI 分析的岗位（match_score 为空），按采集顺序返回"""
    sql = "SELECT * FROM jobs WHERE match_score IS NULL ORDER BY id ASC"
    params: List[Any] = []
    if limit:
        sql += " LIMIT ?"
        params.append(limit)
    with get_conn() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def reset_matches(job_ids: Optional[List[int]] = None) -> int:
    """
    清空匹配结果，以便重新进行 AI 分析。
    - 传入 job_ids：只重置这些岗位
    - 不传：重置所有「待确认」状态的岗位（常用于修改偏好后重新打分）
    返回被重置的条数。
    """
    with get_conn() as conn:
        if job_ids:
            placeholders = ",".join("?" for _ in job_ids)
            cur = conn.execute(
                f"""
                UPDATE jobs SET match_score = NULL, match_reason = NULL, match_advice = NULL
                 WHERE id IN ({placeholders})
                """,
                job_ids,
            )
        else:
            cur = conn.execute(
                """
                UPDATE jobs SET match_score = NULL, match_reason = NULL, match_advice = NULL
                 WHERE status = 'pending'
                """
            )
    return cur.rowcount


def get_next_approved() -> Optional[Dict[str, Any]]:
    """返回一个「已确认待投递」的岗位：匹配分最高的优先，同分时先采集的优先"""
    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT * FROM jobs
             WHERE status = 'approved'
             ORDER BY (match_score IS NULL) ASC, match_score DESC, id ASC
             LIMIT 1
            """
        ).fetchone()
    return _row_to_dict(row)


def count_applied_today() -> int:
    """统计今天已投递的岗位数量"""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM jobs WHERE status = 'applied' AND applied_at LIKE ?",
            (f"{today_str()}%",),
        ).fetchone()
    return int(row["c"])


def get_stats() -> Dict[str, Any]:
    """汇总统计数据"""
    today_like = f"{today_str()}%"
    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT
                COUNT(*)                                              AS total,
                SUM(CASE WHEN status = 'pending'  THEN 1 ELSE 0 END)  AS pending,
                SUM(CASE WHEN status = 'approved' THEN 1 ELSE 0 END)  AS approved,
                SUM(CASE WHEN status = 'applied'  THEN 1 ELSE 0 END)  AS applied,
                SUM(CASE WHEN status = 'rejected' THEN 1 ELSE 0 END)  AS rejected,
                SUM(CASE WHEN match_score IS NULL THEN 1 ELSE 0 END)  AS unscored,
                SUM(CASE WHEN match_score >= 75   THEN 1 ELSE 0 END)  AS high_match,
                AVG(match_score)                                      AS avg_score,
                SUM(CASE WHEN created_at LIKE ? THEN 1 ELSE 0 END)    AS today_collected,
                SUM(CASE WHEN status = 'applied' AND applied_at LIKE ? THEN 1 ELSE 0 END) AS today_applied
            FROM jobs
            """,
            (today_like, today_like),
        ).fetchone()

    stats = dict(row)
    # SUM 在空表时返回 NULL，统一转成 0
    for key, value in stats.items():
        if key == "avg_score":
            stats[key] = round(value, 1) if value is not None else None
        else:
            stats[key] = int(value or 0)
    return stats


# ---------------------------------------------------------------------------
# 偏好相关
# ---------------------------------------------------------------------------

def _decode_list(value: Optional[str]) -> List[str]:
    """把数据库中的 JSON 字符串解析为字符串列表，解析失败返回空列表"""
    if not value:
        return []
    try:
        data = json.loads(value)
        return [str(x).strip() for x in data if str(x).strip()] if isinstance(data, list) else []
    except (json.JSONDecodeError, TypeError):
        return []


def parse_tiers(text: Optional[str]) -> List[List[str]]:
    """「运营>营销=产品」→ [["运营"], ["营销", "产品"]]；逗号、顿号也当作 > ，/ 也当作 ="""
    tiers = []
    for part in re.split(r"[>＞≥,，、;；]", text or ""):
        names = [n.strip() for n in re.split(r"[=＝/]", part) if n.strip()]
        if names:
            tiers.append(list(dict.fromkeys(names)))
    return tiers


def format_tiers(tiers: List[List[str]]) -> str:
    """parse_tiers 的逆操作，用于规整保存格式"""
    return ">".join("=".join(t) for t in tiers if t)


def get_preferences() -> Dict[str, Any]:
    """读取用户偏好（列表字段已解析为 Python list，优先级额外附带解析后的 *_tiers）"""
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM preferences WHERE id = 1").fetchone()

    if row is None:
        # 理论上 init_db 已创建，这里兜底返回默认值
        prefs: Dict[str, Any] = {field: [] for field in PREF_LIST_FIELDS}
        prefs.update({field: "" for field in PREF_TIER_FIELDS})
        prefs.update({"id": 1, "daily_limit": DEFAULT_DAILY_LIMIT, "updated_at": None})
    else:
        prefs = dict(row)
        for field in PREF_LIST_FIELDS:
            prefs[field] = _decode_list(prefs.get(field))
        prefs["daily_limit"] = int(prefs.get("daily_limit") or DEFAULT_DAILY_LIMIT)

    for field in PREF_TIER_FIELDS:
        prefs[field] = prefs.get(field) or ""
    prefs["role_tiers"] = parse_tiers(prefs["role_priority"])
    prefs["city_tiers"] = parse_tiers(prefs["city_priority"])
    return prefs


def save_preferences(data: Dict[str, Any]) -> Dict[str, Any]:
    """
    保存用户偏好（列表字段以 JSON 数组存储），返回保存后的偏好。
    优先级字段为 None / 缺省时保留原值，避免只改列表字段的调用把它清空。
    """
    values = []
    for field in PREF_LIST_FIELDS:
        items = data.get(field) or []
        # 去空、去重、保持原顺序
        cleaned = list(dict.fromkeys(str(x).strip() for x in items if str(x).strip()))
        values.append(json.dumps(cleaned, ensure_ascii=False))

    daily_limit = int(data.get("daily_limit") or DEFAULT_DAILY_LIMIT)

    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO preferences
                (id, target_roles, target_cities, target_industries,
                 avoid_companies, avoid_roles, daily_limit, updated_at)
            VALUES (1, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                target_roles      = excluded.target_roles,
                target_cities     = excluded.target_cities,
                target_industries = excluded.target_industries,
                avoid_companies   = excluded.avoid_companies,
                avoid_roles       = excluded.avoid_roles,
                daily_limit       = excluded.daily_limit,
                updated_at        = excluded.updated_at
            """,
            (*values, daily_limit, now_str()),
        )
        for field in PREF_TIER_FIELDS:
            if data.get(field) is not None:
                conn.execute(
                    f"UPDATE preferences SET {field} = ? WHERE id = 1",
                    (format_tiers(parse_tiers(data[field])),),
                )
    return get_preferences()


# ---------------------------------------------------------------------------
# 公司规模缓存
# ---------------------------------------------------------------------------

def get_company_scales() -> Dict[str, Dict[str, str]]:
    """{公司归一名: {"scale": "1000-9999人", "source": "牛客" / "AI估算"}}"""
    with get_conn() as conn:
        rows = conn.execute("SELECT name, scale, source FROM company_profiles WHERE scale != ''").fetchall()
    return {r["name"]: {"scale": r["scale"], "source": r["source"]} for r in rows}


def save_company_scales(scales: Dict[str, str], source: str) -> None:
    """写入公司规模；AI 估算不覆盖牛客上采到的真实值"""
    with get_conn() as conn:
        for name, scale in scales.items():
            if not name or not scale:
                continue
            conn.execute(
                """
                INSERT INTO company_profiles (name, scale, source, updated_at) VALUES (?, ?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                    scale = excluded.scale, source = excluded.source, updated_at = excluded.updated_at
                 WHERE company_profiles.source != '牛客' OR excluded.source = '牛客'
                """,
                (name, scale, source, now_str()),
            )


def get_company_extras() -> Dict[str, Dict[str, Any]]:
    """{公司归一名: 限投数 + 牛客校招信息}，limit 为 None 表示未填"""
    with get_conn() as conn:
        rows = conn.execute(
            f"SELECT name, apply_limit, limit_note, info_checked_at, {', '.join(PROFILE_INFO_FIELDS)} FROM company_profiles"
        ).fetchall()
    return {
        r["name"]: {
            "limit": r["apply_limit"],
            "note": r["limit_note"],
            "checked_at": r["info_checked_at"],
            **{f: r[f] for f in PROFILE_INFO_FIELDS},
        }
        for r in rows
    }


def save_company_limit(name: str, limit: Optional[int], note: str = "") -> None:
    """写入用户填写的限投数；limit 为 None 表示清除"""
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO company_profiles (name, apply_limit, limit_note, limit_source, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET
                apply_limit = excluded.apply_limit, limit_note = excluded.limit_note,
                limit_source = excluded.limit_source, updated_at = excluded.updated_at
            """,
            (name, limit, note[:200], "手动" if limit is not None else "", now_str()),
        )


def fill_enterprise_link(companies: List[str], link: str) -> None:
    """给这些公司名下还没有牛客公司主页链接的岗位补上"""
    with get_conn() as conn:
        conn.executemany(
            "UPDATE jobs SET enterprise_link = ? WHERE company = ? AND (enterprise_link IS NULL OR enterprise_link = '')",
            [(link, c) for c in companies],
        )


def save_company_info(name: str, info: Dict[str, str]) -> None:
    """写入牛客公司校招信息（官网、公告、网申时间）；查不到时也记下查询时间，避免反复请求"""
    values = [str(info.get(f) or "")[:500] for f in PROFILE_INFO_FIELDS]
    with get_conn() as conn:
        conn.execute(
            f"""
            INSERT INTO company_profiles (name, {', '.join(PROFILE_INFO_FIELDS)}, info_checked_at, updated_at)
            VALUES (?, {', '.join('?' for _ in PROFILE_INFO_FIELDS)}, ?, ?)
            ON CONFLICT(name) DO UPDATE SET
                {', '.join(f'{f} = excluded.{f}' for f in PROFILE_INFO_FIELDS)},
                info_checked_at = excluded.info_checked_at, updated_at = excluded.updated_at
            """,
            (name, *values, now_str(), now_str()),
        )


# ---------------------------------------------------------------------------
# 风险记录相关
# ---------------------------------------------------------------------------

def add_risk_log(event_type: str, description: str = "") -> None:
    """写入一条风险记录，event_type 取值：投递 / 异常 / 暂停"""
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO risk_logs (event_type, description, created_at) VALUES (?, ?, ?)",
            (event_type, description[:1000], now_str()),
        )


def list_risk_logs(limit: int = 20) -> List[Dict[str, Any]]:
    """获取最近的风险记录（倒序）"""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM risk_logs ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


def count_risk_events_today(event_type: str) -> int:
    """统计今天某类风险事件的次数"""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM risk_logs WHERE event_type = ? AND created_at LIKE ?",
            (event_type, f"{today_str()}%"),
        ).fetchone()
    return int(row["c"])
