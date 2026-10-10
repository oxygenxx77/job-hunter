"""
生成一套虚构的演示数据（截图 / 试用用），岗位、JD、打分和简历都是编的。

用法：把项目复制到一个新文件夹，再对那个文件夹运行
    cp -R job-hunter /tmp/job-hunter-demo
    python3 demo/seed_demo.py /tmp/job-hunter-demo
目标文件夹里已经有岗位库或简历时拒绝运行，避免覆盖真实数据。
"""
import shutil
import sys
from datetime import datetime, timedelta
from pathlib import Path

# 公司, 规模, 行业, [(岗位, 城市, 薪资, 分数, 状态)]
COMPANIES = [
    ("字节跳动", "10000人以上", "互联网", [
        ("电商运营-抖音电商", "深圳", "12-15K", 92, "applied"),
        ("用户增长运营", "上海", "13-16K", 88, "applied"),
        ("内容生态运营", "北京", "12-14K", 74, "pending"),
    ]),
    ("美团", "10000人以上", "互联网", [
        ("到店事业群-商家运营", "深圳", "11-14K", 89, "applied"),
        ("优选-用户运营", "广州", "10-13K", 81, "approved"),
    ]),
    ("小红书", "1000-9999人", "互联网", [
        ("社区内容运营", "上海", "12-15K", 90, "applied"),
        ("电商商家运营", "上海", "12-15K", 86, "approved"),
    ]),
    ("拼多多", "10000人以上", "电商", [
        ("运营管培生", "上海", "18-22K", 84, "applied"),
    ]),
    ("快手", "10000人以上", "互联网", [
        ("电商主播运营", "北京", "12-15K", 82, "applied"),
        ("用户运营", "深圳", "11-14K", 79, "pending"),
    ]),
    ("京东", "10000人以上", "电商", [
        ("采销管培生", "北京", "13-16K", 87, "applied"),
        ("京东到家-商家运营", "深圳", "10-13K", 77, "pending"),
    ]),
    ("Shopee", "10000人以上", "跨境电商", [
        ("跨境电商运营", "深圳", "12-15K", 91, "approved"),
        ("卖家增长运营", "深圳", "12-15K", 88, "pending"),
        ("市场营销管培生", "深圳", "11-14K", 80, "pending"),
    ]),
    ("SHEIN", "10000人以上", "跨境电商", [
        ("商品运营", "广州", "11-14K", 90, "approved"),
        ("用户运营", "广州", "10-13K", 85, "pending"),
    ]),
    ("名创优品", "1000-9999人", "零售", [
        ("电商运营管培生", "广州", "8-11K", 83, "pending"),
        ("会员运营", "广州", "8-10K", 78, "pending"),
    ]),
    ("OPPO", "10000人以上", "消费电子", [
        ("互联网用户运营", "深圳", "12-15K", 82, "pending"),
        ("海外电商运营", "深圳", "12-16K", 86, "pending"),
    ]),
    ("大疆", "10000人以上", "消费电子", [
        ("电商运营", "深圳", "13-16K", 88, "pending"),
    ]),
    ("vivo", "10000人以上", "消费电子", [
        ("渠道运营", "深圳", "11-14K", 72, "pending"),
    ]),
    ("唯品会", "10000人以上", "电商", [
        ("品类运营", "广州", "10-13K", 84, "pending"),
        ("数据运营", "广州", "10-13K", 76, "pending"),
    ]),
    ("欢聚集团", "1000-9999人", "互联网", [
        ("海外直播运营", "广州", "11-14K", 71, "pending"),
    ]),
    ("某教育科技公司", "100-499人", "教育", [
        ("电话销售", "广州", "6-8K", 38, "rejected"),
    ]),
    ("Anker 安克创新", "1000-9999人", "跨境电商", [
        ("亚马逊运营", "深圳", "12-15K", 89, "pending"),
        ("品牌营销", "深圳", "11-14K", 75, "pending"),
    ]),
]

REASONS = {
    "high": [
        ("两段电商运营实习与岗位高度相关，活动策划和数据复盘经验可以直接迁移；城市和方向都在第一优先级",
         "把第一段实习的转化率数据放到最前面；准备一个大促复盘案例"),
        ("内容运营实习的种草笔记和达人合作经历正好对口，JD 里要求的数据敏感度也有 SQL 佐证",
         "面试前准备 2 个爆款笔记的拆解，讲清选题逻辑和数据结果"),
        ("岗位看重从 0 到 1 的拉新经验，校园二手平台项目是很好的例子；跨境方向和你的实习行业一致",
         "简历里补一句英语 CET-6 的使用场景，跨境岗位会追问"),
        ("管培生岗位看重综合能力，学生会管理经历 + 两段实习的组合有竞争力",
         "准备一个「带团队完成目标」的 STAR 故事，管培面试必问"),
    ],
    "mid": [
        ("运营基础和岗位要求基本吻合，但岗位更偏{tag}，相关经历较少",
         "投递前补一段相关项目描述，面试时强调学习速度和数据能力"),
        ("方向匹配，但城市在第二优先级，且岗位要求有{tag}经验",
         "可以投，但优先级放在同公司的电商运营岗位之后"),
    ],
    "low": [("岗位以销售为主，与你的运营方向和职业规划差距较大", "不建议投递")],
}

JD = """岗位职责：
1. 负责{title}相关的日常运营工作，包括活动策划、执行与复盘；
2. 跟踪核心数据指标，发现问题并提出优化方案；
3. 与产品、市场、商家等多方协作，推动项目落地。
任职要求：
1. 2027 届本科及以上学历，专业不限，市场营销、电子商务、统计等专业优先；
2. 有电商 / 内容 / 用户运营相关实习经历者优先；
3. 熟练使用 Excel，会 SQL 者优先；
4. 逻辑清晰，沟通能力强，对数据敏感。"""


def main(root: Path):
    backend = root / "backend"
    if not (backend / "database.py").exists():
        sys.exit(f"{root} 不是求职助手项目目录")
    if (backend / "jobs.db").exists() or (backend / "resume.json").exists():
        sys.exit(f"{root} 里已经有岗位库或简历，为避免覆盖真实数据，请换一个空的项目副本")

    sys.path.insert(0, str(backend))
    import database as db
    db.DB_PATH = backend / "jobs.db"
    db.init_db()

    shutil.copy(backend / "resume.example.json", backend / "resume.json")

    jobs, plan = [], []
    n = 0
    for company, scale, industry, items in COMPANIES:
        for title, city, salary, score, status in items:
            n += 1
            jobs.append({
                "company": company, "title": title, "city": city, "salary": salary,
                "link": f"https://example.com/demo-job/{n}", "jd_text": JD.format(title=title),
                "source": "牛客", "job_type": "校招", "company_scale": scale, "company_industry": industry,
            })
            plan.append((company, title, score, status))
    db.insert_jobs(jobs)
    db.save_company_scales({c: s for c, s, _, _ in COMPANIES}, "牛客")

    now = datetime.now()
    with db.get_conn() as conn:
        ids = {(r["company"], r["title"]): r["id"] for r in conn.execute("SELECT id, company, title FROM jobs")}
        for k, (company, title, score, status) in enumerate(plan):
            level = "high" if score >= 85 else "mid" if score >= 60 else "low"
            options = REASONS[level]
            reason, advice = options[k % len(options)]
            reason = reason.format(tag="数据分析" if "数据" in title else "渠道 / 直播" if "直播" in title or "渠道" in title else "品牌")
            created = (now - timedelta(days=12 - k % 10, hours=k)).strftime("%Y-%m-%d %H:%M:%S")
            applied = (now - timedelta(days=k % 9 + 1)).strftime("%Y-%m-%d %H:%M:%S") if status == "applied" else None
            conn.execute(
                "UPDATE jobs SET match_score=?, match_reason=?, match_advice=?, status=?, created_at=?, applied_at=? WHERE id=?",
                (score, reason, advice, status, created, applied, ids[(company, title)]),
            )

    db.save_preferences({
        "target_roles": ["电商运营", "用户运营", "内容运营"],
        "target_cities": ["深圳", "广州", "上海"],
        "target_industries": ["互联网", "电商", "跨境电商", "消费电子"],
        "avoid_companies": [],
        "avoid_roles": ["销售", "客服"],
        "daily_limit": 10,
        "role_priority": "电商运营>用户运营=内容运营>营销",
        "city_priority": "深圳>广州=上海>北京",
    })
    print(f"已在 {root} 生成演示数据：{len(jobs)} 个岗位、{len(COMPANIES)} 家公司，简历用 resume.example.json")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    main(Path(sys.argv[1]).resolve())
