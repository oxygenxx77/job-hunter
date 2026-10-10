/**
 * content.js —— 注入牛客网页面，扫描岗位卡片
 *
 * 设计要点：
 * 1. 牛客是 React 渲染 + 频繁改版，class 名不稳定，因此采用多种策略组合识别岗位卡片：
 *    - 策略 A：找到所有「岗位详情链接」，向上回溯到只包含这一个岗位的最外层容器
 *    - 策略 B：按 class 名关键词（job-card / job-item ...）匹配卡片
 *    - 字段提取同样是「class 关键词优先，文本行兜底」
 * 2. 使用 MutationObserver 监听 DOM 变化（滚动加载、翻页、React 重渲染），
 *    自动把识别到的岗位缓存起来，避免虚拟列表回收 DOM 后丢失已滚过的岗位
 * 3. 只在用户点击扩展弹窗的「采集本页岗位」时才把数据交给 popup 上报，不会自动上传
 */
(() => {
  // ---------------------------------------------------------------------------
  // 防重复注入：popup 只在页面上的脚本不响应时才用 chrome.scripting 再注入，
  // 说明旧脚本已失效（如扩展重新加载过）。此时新脚本总是接管，并继承旧脚本已识别的岗位。
  // 不能用 chrome.runtime.id 判断旧脚本是否存活：两次注入共用同一个 isolated world，
  // 旧脚本读到的是新注入后的 chrome 对象，会被误判为存活。
  // ---------------------------------------------------------------------------
  const existing = window.__JOB_HUNTER__;
  const inherited = existing && typeof existing.jobs === 'function' ? existing.jobs() : [];
  if (existing) {
    try {
      existing.destroy();
    } catch (e) {
      /* 旧脚本清理失败不影响接管 */
    }
  }

  const TAG = '[求职助手]';

  // 常见城市列表，用于从文本中识别城市
  const CITY_LIST = [
    '北京', '上海', '广州', '深圳', '杭州', '成都', '武汉', '南京', '西安', '苏州',
    '天津', '重庆', '长沙', '郑州', '厦门', '合肥', '青岛', '济南', '东莞', '珠海',
    '佛山', '宁波', '无锡', '福州', '大连', '沈阳', '哈尔滨', '昆明', '南昌', '贵阳',
    '石家庄', '太原', '南宁', '海口', '常州', '南通', '温州', '嘉兴', '绍兴', '徐州', '中山', '惠州',
    '烟台', '泉州', '长春', '兰州', '香港', '澳门', '台北', '新加坡', '海外', '远程', '全国',
  ];

  // 岗位详情链接特征。/school/schedule/ 是公司的校招日程页，不是岗位，不能算在内：
  // 「为你推荐」卡片里职位链接旁边就挂着它，算进去会把一张卡拆成两条
  const DETAIL_LINK_PATTERNS = [
    /\/jobs\/detail\/\d+/,
    /\/job\/detail\/\d+/,
    /[?&]jobId=\d+/i,
  ];

  // 岗位卡片候选选择器（按 class 名关键词匹配）。
  // 不要加 school-job：那是校招职位页的整块容器，列表为空时会被当成一张卡
  const CARD_SELECTORS = [
    '[class*="job-card"]', '[class*="jobCard"]',
    '[class*="job-item"]', '[class*="jobItem"]',
    '[class*="job-info-item"]', '[class*="recruit-card"]',
    '[class*="position-item"]', '[class*="positionItem"]',
    '[class*="ai-card"]',
  ];
  const CARD_SELECTOR = CARD_SELECTORS.filter((s) => !s.includes('info-item')).join(',');

  // 卡片上的标签行（城市、学历、届别等），不可能是公司名
  const TAG_LINE_RE = /^(本科|硕士|博士|大专|专科|学历不限|不限|\d{2,4}届|\d+[-~]\d+年|经验不限|校招|实习|社招|内推|急招|.{0,6}收藏|HR.*)$/;

  // 各字段的候选选择器（越靠前越精确）
  const TITLE_SELECTORS = [
    '[class*="job-name"]', '[class*="jobName"]', '[class*="job-title"]', '[class*="jobTitle"]',
    '[class*="position-name"]', '[class*="positionName"]', '[class*="title"]', 'h3', 'h4',
  ];
  const COMPANY_SELECTORS = [
    '[class*="company-name"]', '[class*="companyName"]', '[class*="company"]',
    '[class*="corp"]', '[class*="enterprise"]', '[class*="brand"]',
  ];
  const CITY_SELECTORS = [
    '[class*="city"]', '[class*="address"]', '[class*="location"]', '[class*="area"]', '[class*="place"]',
  ];
  const JD_SELECTORS = [
    '[class*="desc"]', '[class*="require"]', '[class*="duty"]', '[class*="content"]', '[class*="detail"]',
  ];

  // 牛客卡片上的公司规模档位，如「1000-9999人」「10000人以上」
  const SCALE_RE = /(\d+-\d+人|\d+人以上)/;
  const ENTERPRISE_RE = /\/enterprise\/(\d+)/;

  // 已识别岗位缓存：key 为链接（无链接时用 公司|岗位|城市）
  const cache = new Map();

  // ---------------------------------------------------------------------------
  // 文本工具
  // ---------------------------------------------------------------------------

  /** 获取元素可见文本，并规整空白 */
  function textOf(el) {
    if (!el) return '';
    return (el.innerText || el.textContent || '')
      .replace(/[ \t\u00a0]+/g, ' ')
      .replace(/\s*\n\s*/g, '\n')
      .trim();
  }

  /** 取第一行非空文本 */
  function firstLine(text) {
    return (text || '').split('\n').map((s) => s.trim()).find(Boolean) || '';
  }

  /** 按行拆分文本 */
  function linesOf(text) {
    return (text || '').split('\n').map((s) => s.trim()).filter(Boolean);
  }

  /** 在 root 内按选择器顺序查找第一个合适的短文本（如标题、公司名） */
  function pickShort(root, selectors, exclude = []) {
    for (const sel of selectors) {
      for (const el of root.querySelectorAll(sel)) {
        const t = firstLine(textOf(el));
        if (t && t.length <= 60 && !exclude.includes(t)) return t;
      }
    }
    return '';
  }

  /** 在 root 内按选择器顺序查找第一个合适的长文本（如 JD） */
  function pickLong(root, selectors, minLength = 10) {
    for (const sel of selectors) {
      for (const el of root.querySelectorAll(sel)) {
        const t = textOf(el);
        if (t.length >= minLength) return t;
      }
    }
    return '';
  }

  /** 从文本中识别城市，返回「北京/上海」形式 */
  function extractCities(text) {
    const found = CITY_LIST.filter((c) => (text || '').includes(c));
    return [...new Set(found)].slice(0, 5).join('/');
  }

  // ---------------------------------------------------------------------------
  // 链接工具
  // ---------------------------------------------------------------------------

  function isDetailLink(href) {
    return DETAIL_LINK_PATTERNS.some((re) => re.test(href || ''));
  }

  /** 规整链接：转绝对地址；详情页去掉追踪参数，便于后端按链接去重 */
  function normalizeLink(href) {
    try {
      const url = new URL(href, location.href);
      url.hash = '';
      // 牛客 HR 卡片等链接形如 /jobs/hr/123?jobId=456，指向的就是岗位 456
      const jobId = url.hostname.endsWith('nowcoder.com') && url.searchParams.get('jobId');
      if (jobId && /^\d+$/.test(jobId)) return `https://www.nowcoder.com/jobs/detail/${jobId}`;
      if (/\/(jobs|job)\/detail\/\d+|\/school\/schedule\/\d+/.test(url.pathname)) {
        url.search = '';
      }
      return url.href;
    } catch (e) {
      return '';
    }
  }

  /** 牛客公司主页链接（列出该公司全部在招职位）；在公司主页上时卡片里的公司链接是空的，用当前地址 */
  function enterpriseLinkIn(root) {
    const anchor = [...root.querySelectorAll('a[href]')].find((a) => ENTERPRISE_RE.test(a.href));
    const m = (anchor ? anchor.href : location.pathname).match(ENTERPRISE_RE);
    return m ? `https://www.nowcoder.com/enterprise/${m[1]}` : '';
  }

  /** 公司主页上的岗位名形如「校招 | 市场管培生」，拆出岗位类型 */
  function splitJobType(title, text) {
    const m = title.match(/^\s*(校招|实习|社招)\s*[|｜]\s*/);
    if (m) return { title: title.slice(m[0].length), jobType: m[1] };
    if (/元\/天/.test(text) || /实习/.test(title)) return { title, jobType: '实习' };
    if (/社招/.test(title)) return { title, jobType: '社招' };
    return { title, jobType: /校招|秋招|应届|\d{2}届/.test(title) ? '校招' : '' };
  }

  /** 公司信息块：行业 / 规模 */
  function companyInfoIn(root) {
    const items = [...root.querySelectorAll('[class*="company-info-item"], [class*="company-tags"] > *')]
      .map((el) => firstLine(textOf(el)))
      .filter(Boolean);
    const scale = items.find((t) => SCALE_RE.test(t)) || (textOf(root).match(SCALE_RE) || [])[1] || '';
    const industry = items.find((t) => !SCALE_RE.test(t) && !/榜|融资|上市|市$|未知|暂无/.test(t) && t.length <= 10) || '';
    return { scale: (scale.match(SCALE_RE) || [''])[0], industry };
  }

  /** 获取元素内所有不同的详情链接 */
  function detailLinksIn(el) {
    const set = new Set();
    el.querySelectorAll('a[href]').forEach((a) => {
      if (isDetailLink(a.href)) set.add(normalizeLink(a.href));
    });
    if (el.tagName === 'A' && isDetailLink(el.href)) set.add(normalizeLink(el.href));
    return set;
  }

  // ---------------------------------------------------------------------------
  // 卡片识别
  // ---------------------------------------------------------------------------

  /** 从详情链接向上回溯，找到只包含这一个岗位的最外层容器 */
  function climbToCard(anchor) {
    let el = anchor;
    for (let i = 0; i < 8; i++) {
      const parent = el.parentElement;
      if (!parent || parent === document.body) break;
      if (detailLinksIn(parent).size > 1) break; // 父元素包含多个岗位，说明当前就是卡片
      if (textOf(parent).length > 1500) break;    // 文本过长，说明已经到了大容器
      el = parent;
      if (el.matches(CARD_SELECTOR)) break;       // class 名已表明是卡片，再往上会混进侧栏其他模块
    }
    return el;
  }

  /** 判断候选元素是否像一张岗位卡片 */
  function looksLikeCard(el) {
    if (!el || !el.isConnected) return false;
    const text = textOf(el);
    if (text.length < 4 || text.length > 2000) return false;
    // 不可见元素（display:none 等）跳过
    if (el.offsetParent === null && getComputedStyle(el).position !== 'fixed') return false;
    return true;
  }

  /** 找到页面上所有岗位卡片 */
  function findCards() {
    const candidates = new Set();

    // 策略 A：详情链接反推
    document.querySelectorAll('a[href]').forEach((a) => {
      if (isDetailLink(a.href)) candidates.add(climbToCard(a));
    });

    // 策略 B：class 名关键词
    CARD_SELECTORS.forEach((sel) => {
      document.querySelectorAll(sel).forEach((el) => candidates.add(el));
    });

    const visible = [...candidates].filter(looksLikeCard);

    // 卡片里的小标签（如 class 为 job-info-item 的「HR刚处理简历」）也会被策略 B 选中：
    // 没有详情链接、又位于某个只含一个详情链接的真卡片里的，算作卡片的一部分
    const linkCount = new Map(visible.map((el) => [el, detailLinksIn(el).size]));
    const list = visible.filter((el) =>
      linkCount.get(el) > 0 || !visible.some((o) => o !== el && linkCount.get(o) === 1 && o.contains(el))
    );

    // 去嵌套：包含 ≥2 个候选的是列表容器，丢弃；一对一嵌套时保留外层（信息更全）
    const innerCount = new Map(list.map((el) => [el, list.filter((o) => o !== el && el.contains(o)).length]));
    return list.filter((el) => {
      if (innerCount.get(el) >= 2) return false;
      const hasSingleOuter = list.some((o) => o !== el && o.contains(el) && innerCount.get(o) < 2);
      return !hasSingleOuter;
    });
  }

  // ---------------------------------------------------------------------------
  // 字段提取
  // ---------------------------------------------------------------------------

  /** 从卡片中提取岗位信息 */
  function extractFromCard(card) {
    const allText = textOf(card);
    const lines = linesOf(allText);

    // 链接：优先详情链接，其次 data-* 上的岗位 ID
    const anchors = [...card.querySelectorAll('a[href]')];
    if (card.tagName === 'A') anchors.unshift(card);
    const detailAnchor = anchors.find((a) => isDetailLink(a.href));
    let link = detailAnchor ? normalizeLink(detailAnchor.href) : '';
    if (!link) {
      const jobId = card.dataset.jobId || card.dataset.id || card.getAttribute('data-jobid');
      if (jobId && /^\d+$/.test(jobId)) link = `https://www.nowcoder.com/jobs/detail/${jobId}`;
    }

    // 申请链接：文字含「投递/申请/网申」的链接
    const applyAnchor = anchors.find((a) => /投递|申请|网申/.test(textOf(a)) && /^https?:/.test(a.href));
    const applyLink = applyAnchor ? normalizeLink(applyAnchor.href) : link;

    // 公司
    let company = pickShort(card, COMPANY_SELECTORS);

    // 岗位名
    let title = pickShort(card, TITLE_SELECTORS, [company]);
    if (!title && detailAnchor) title = firstLine(textOf(detailAnchor));
    if (!title) title = lines[0] || '';

    // 公司兜底：公司主页/校招日程链接的文字，其次含公司特征词的行，再其次标题后面的一行短文本
    if (!company) {
      const companyAnchor = anchors.find((a) => /\/enterprise\/\d+|\/school\/schedule\/\d+/.test(a.href));
      const isTag = (l) => TAG_LINE_RE.test(l) || extractCities(l).replace(/\//g, '').length >= l.replace(/[\s,，/、]/g, '').length;
      company =
        firstLine(textOf(companyAnchor)) ||
        lines.find((l) => l !== title && /公司|集团|科技|有限|银行|股份|网络|信息/.test(l) && l.length <= 30) ||
        lines.find((l) => l !== title && l.length <= 20 && !/\d+[kK]|薪|元/.test(l) && !isTag(l)) ||
        '';
    }

    // 城市
    let city = pickShort(card, CITY_SELECTORS, [title, company]);
    if (city && city.length > 20) city = extractCities(city) || city.slice(0, 20);
    // 公司名里常带地名（如「小派创新科技（杭州）」），不能算作工作城市
    if (!city) city = extractCities(lines.filter((l) => l !== company).join('\n')) || extractCities(allText);

    // JD 摘要：优先描述区块，否则用卡片剩余文本
    let jd = pickLong(card, JD_SELECTORS);
    if (!jd) {
      jd = lines.filter((l) => l !== title && l !== company && l !== city).join(' | ');
    }

    const typed = splitJobType(title, allText);
    const info = companyInfoIn(card);

    return {
      company: company.slice(0, 60),
      title: typed.title.slice(0, 100),
      city,
      link,
      apply_link: applyLink,
      jd_text: jd.slice(0, 2000),
      source: '牛客',
      salary: pickShort(card, ['[class*="salary"]']).slice(0, 40),
      job_type: typed.jobType,
      company_scale: info.scale,
      company_industry: info.industry,
      enterprise_link: enterpriseLinkIn(card),
    };
  }

  /** 岗位详情页：直接提取当前页面的完整 JD */
  function extractFromDetailPage() {
    if (!/\/(jobs|job)\/detail\/\d+/.test(location.pathname)) return null;

    // document.title 通常形如「岗位名_公司名招聘_牛客网」，作为兜底
    const titleParts = document.title.split(/[_|\-—]/).map((s) => s.trim()).filter(Boolean);

    const title = pickShort(document, ['[class*="job-name"]', '[class*="jobName"]', '[class*="job-title"]', 'h1']) || titleParts[0] || '';
    const company =
      pickShort(document, ['[class*="company-name"]', '[class*="companyName"]'], [title]) ||
      (titleParts[1] || '').replace(/招聘$/, '');
    const cityText = pickShort(document, CITY_SELECTORS, [title, company]);
    const city = (cityText && cityText.length <= 20 ? cityText : extractCities(cityText)) ||
      extractCities(textOf(document.querySelector('main') || document.body).slice(0, 3000));
    const jd = pickLong(document, [
      '[class*="job-detail"]', '[class*="jobDetail"]', '[class*="job-desc"]',
      '[class*="jobDesc"]', '[class*="description"]', '[class*="desc"]',
    ], 30);

    if (!title) return null;
    const link = normalizeLink(location.href);
    const typed = splitJobType(title, document.title);
    const companyCard = document.querySelector('[class*="company-card"]');
    const info = companyInfoIn(companyCard || document.createElement('div'));
    return {
      company: company.slice(0, 60),
      title: typed.title.slice(0, 100),
      city,
      link,
      apply_link: link,
      jd_text: jd.slice(0, 5000),
      source: '牛客',
      salary: pickShort(document, ['[class*="salary"]']).slice(0, 40),
      job_type: typed.jobType || (/实习/.test(document.title) ? '实习' : /校招/.test(document.title) ? '校招' : ''),
      company_scale: info.scale,
      company_industry: info.industry,
      enterprise_link: enterpriseLinkIn(companyCard || document),
    };
  }

  // ---------------------------------------------------------------------------
  // 扫描与缓存
  // ---------------------------------------------------------------------------

  function cacheKey(job) {
    return job.link || `${job.company}|${job.title}|${job.city}`;
  }

  /** 给识别到的卡片加一个短暂的高亮描边，让用户直观看到采集范围 */
  function highlight(cards) {
    if (!document.getElementById('jh-style')) {
      const style = document.createElement('style');
      style.id = 'jh-style';
      style.textContent = '.jh-scanned{outline:2px dashed #2383e2 !important;outline-offset:2px;transition:outline-color .3s}';
      document.head.appendChild(style);
    }
    cards.forEach((c) => c.classList.add('jh-scanned'));
    setTimeout(() => cards.forEach((c) => c.classList.remove('jh-scanned')), 1500);
  }

  /**
   * 扫描当前 DOM，把岗位写入缓存。
   * @param {boolean} withHighlight 是否高亮识别到的卡片（手动触发时为 true）
   * @returns {Array} 本次在 DOM 中识别到的岗位
   */
  function scanIntoCache(withHighlight) {
    const found = [];

    const detail = extractFromDetailPage();
    if (detail) found.push(detail);

    const cards = findCards();
    cards.forEach((card) => {
      try {
        const job = extractFromCard(card);
        // 没有岗位链接的多半是筛选栏、订阅框之类被误识别的区块
        if (job.title && job.link) found.push(job);
      } catch (e) {
        console.warn(TAG, '解析卡片失败', e);
      }
    });

    found.forEach((job) => {
      const key = cacheKey(job);
      const old = cache.get(key);
      // 同一岗位保留 JD 更完整的版本
      if (!old || (job.jd_text || '').length > (old.jd_text || '').length) cache.set(key, job);
    });

    if (withHighlight) highlight(cards);
    return found;
  }

  /** 页面还没渲染出岗位时，最多等待 timeout 毫秒 */
  function waitForJobs(timeout) {
    return new Promise((resolve) => {
      const start = Date.now();
      const timer = setInterval(() => {
        const found = scanIntoCache(false);
        if (found.length || Date.now() - start > timeout) {
          clearInterval(timer);
          resolve(found);
        }
      }, 300);
    });
  }

  // ---------------------------------------------------------------------------
  // MutationObserver：监听 React 渲染 / 滚动加载带来的 DOM 变化
  // ---------------------------------------------------------------------------

  let debounceTimer = null;
  const observer = new MutationObserver(() => {
    clearTimeout(debounceTimer);
    debounceTimer = setTimeout(() => {
      try {
        scanIntoCache(false);
      } catch (e) {
        console.warn(TAG, '自动扫描失败', e);
      }
    }, 600);
  });
  observer.observe(document.body, { childList: true, subtree: true });

  // ---------------------------------------------------------------------------
  // 公司维度采集：识别页面上的公司，用牛客自己的接口拉取每家公司的全部岗位
  // （公司卡片只有「N 个在招职位」，没有具体岗位，DOM 里抓不到）
  // ---------------------------------------------------------------------------

  const RECRUIT_TYPES = { 1: '校招', 2: '实习', 3: '社招' };
  const JOB_LIST_API = 'https://nowpick.nowcoder.com/u/company/job/list/v2';
  const SEARCH_API = 'https://www.nowcoder.com/np-api/u/job/square-search';
  const PAGE_SIZE = 50;
  const MAX_PAGES = 10;
  const REQUEST_GAP = 300; // 请求间隔，避免短时间内大量请求触发牛客风控

  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

  function cleanCompanyName(name) {
    return (name || '').replace(/(\.{3}|…)$/, '').trim();
  }

  /** 页面上的公司：{ id, name }，id 可能为空（只有名字的卡片，采集时再按名字反查） */
  function findCompanies() {
    const found = new Map();
    const add = (id, name) => {
      name = cleanCompanyName(name);
      if (!id && !name) return;
      if (id) {
        found.delete(`name:${name}`);
        if (!found.has(`id:${id}`)) found.set(`id:${id}`, { id, name });
      } else if (![...found.values()].some((c) => c.name === name)) {
        found.set(`name:${name}`, { id: '', name });
      }
    };

    // 公司主页只采这一家，页面上推荐的其他公司不算
    const here = location.pathname.match(ENTERPRISE_RE);
    if (here) {
      add(here[1], document.title.replace(/\d{4}年.*$|[-_].*$/, ''));
      return [...found.values()];
    }

    document.querySelectorAll('a[href*="/enterprise/"]').forEach((a) => {
      const id = (a.href.match(ENTERPRISE_RE) || [])[1];
      add(id, pickShort(a, ['[class*="company-name"]', '[class*="companyName"]', '[class*="name"]']) || firstLine(textOf(a)));
    });

    // 「N 个 在招职位 / 立即投递」这类公司卡片：从「在招职位」往上找到只含一家公司的最外层
    const count = (el) => (el.textContent || '').split('在招职位').length - 1;
    [...document.querySelectorAll('body *')]
      .filter((el) => !el.children.length && el.textContent.includes('在招职位'))
      .forEach((leaf) => {
        let card = leaf;
        while (card.parentElement && card.parentElement !== document.body && count(card.parentElement) === 1) {
          card = card.parentElement;
        }
        const id = (enterpriseLinkIn(card).match(ENTERPRISE_RE) || [])[1] || '';
        const name =
          pickShort(card, ['[class*="company-name"]', '[class*="companyName"]', '[class*="name"]']) ||
          linesOf(textOf(card)).find((l) => !/^\d|在招职位|处理率|投递/.test(l)) ||
          '';
        add(id, name);
      });

    return [...found.values()];
  }

  async function postForm(url, params) {
    const resp = await fetch(url, {
      method: 'POST',
      credentials: 'include',
      headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
      body: new URLSearchParams(params).toString(),
    });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const data = await resp.json();
    if (data.code !== 0) throw new Error(data.msg || '牛客接口返回错误');
    return data.data || {};
  }

  /** 只有公司名的卡片：用岗位搜索接口按名字搜，取公司名对得上的 companyId */
  async function resolveCompanyId(name) {
    for (const recruitType of [1, 2, 3]) {
      const data = await postForm(SEARCH_API, {
        page: 1, pageSize: 20, query: name, recruitType, requestFrom: 1, order: 0, pageSource: 5001,
      });
      await sleep(REQUEST_GAP);
      const votes = new Map();
      (data.datas || []).forEach(({ data: job }) => {
        const c = (job && job.recommendInternCompany) || {};
        const same = [c.companyName, c.companyShortName].some((n) => n && (n.startsWith(name) || name.startsWith(n)));
        if (same) votes.set(job.companyId, (votes.get(job.companyId) || 0) + 1);
      });
      const best = [...votes].sort((a, b) => b[1] - a[1])[0];
      if (best) return String(best[0]);
    }
    return '';
  }

  function salaryOf(x) {
    if (x.salaryShow) return x.salaryShow;
    if (!x.salaryMax || x.salaryMax >= 999999) return '薪资面议';
    if (x.salaryType === 1) return `${x.salaryMin}-${x.salaryMax}元/天`;
    if (x.salaryType === 2) return `${x.salaryMin}-${x.salaryMax}K${x.salaryMonth ? `·${x.salaryMonth}薪` : ''}`;
    return '';
  }

  /** 第一行固定为「职位类别：xxx」，后端按意向方向过滤时会读它 */
  function jdOf(x) {
    let ext = {};
    try {
      ext = JSON.parse(x.ext || '{}');
    } catch (e) {
      /* ext 不是 JSON 时只用结构化字段 */
    }
    const section = (label, text, own) => text && (own.test(text) ? text.trim() : `${label}：\n${text.trim()}`);
    return [
      `职位类别：${x.careerJobName || ''}`,
      x.graduationYear && `毕业年份：${x.graduationYear}`,
      x.jobKeys && `关键词：${x.jobKeys}`,
      section('岗位职责', ext.infos, /^\s*(岗位职责|工作职责|职位描述|工作内容)/),
      section('任职要求', ext.requirements, /^\s*(任职要求|职位要求|岗位要求|任职资格)/),
    ].filter(Boolean).join('\n');
  }

  function jobFromApi(x, company) {
    const c = x.recommendInternCompany || {};
    const link = `https://www.nowcoder.com/jobs/detail/${x.id}`;
    const cities = x.jobCityList && x.jobCityList.length ? x.jobCityList : [x.jobCity];
    return {
      company: (c.companyName || company.name || '').slice(0, 60),
      title: (x.jobName || '').slice(0, 100),
      city: cities.filter(Boolean).join('/'),
      link,
      apply_link: link,
      jd_text: jdOf(x).slice(0, 5000),
      source: '牛客',
      salary: salaryOf(x).slice(0, 40),
      job_type: RECRUIT_TYPES[x.recruitType] || '',
      company_scale: c.personScales || '',
      company_industry: ((c.industryTagNameList || [])[0] || '').trim(),
      enterprise_link: `https://www.nowcoder.com/enterprise/${x.companyId || company.id}`,
    };
  }

  /** 牛客把多城市的同一职位拆成多条，合并成一条；链接取最想去的城市那条 */
  function mergeCities(jobs, preferredCities) {
    const rank = (city) => {
      const i = preferredCities.findIndex((c) => (city || '').includes(c));
      return i < 0 ? preferredCities.length : i;
    };
    const groups = new Map();
    jobs.forEach((job) => {
      const key = `${job.company}|${job.title}|${job.job_type}`;
      const group = groups.get(key);
      if (!group) {
        groups.set(key, { ...job, cities: job.city.split('/').filter(Boolean) });
        return;
      }
      job.city.split('/').forEach((c) => c && !group.cities.includes(c) && group.cities.push(c));
      if (rank(job.city) < rank(group.city)) Object.assign(group, { link: job.link, apply_link: job.apply_link, city: job.city });
    });
    return [...groups.values()].map(({ cities, ...job }) => ({
      ...job,
      city: cities.sort((a, b) => rank(a) - rank(b)).slice(0, 8).join('/'),
    }));
  }

  function reportProgress(done, total, name) {
    try {
      chrome.runtime.sendMessage({ type: 'JH_PROGRESS', done, total, name }).catch(() => {});
    } catch (e) {
      /* 弹窗已关闭 */
    }
  }

  async function fetchCompanyJobs(companies, recruitTypes, preferredCities) {
    const jobs = [];
    const failed = [];
    const empty = [];
    const types = recruitTypes.includes(0) ? [0] : recruitTypes;
    for (let i = 0; i < companies.length; i++) {
      const company = { ...companies[i] };
      reportProgress(i, companies.length, company.name);
      try {
        if (!company.id) company.id = await resolveCompanyId(company.name);
        if (!company.id) {
          failed.push(`${company.name}（牛客上没搜到）`);
          continue;
        }
        const raw = [];
        for (const recruitType of types) {
          for (let page = 1; page <= MAX_PAGES; page++) {
            const data = await postForm(JOB_LIST_API, {
              jobCity: '', recruitType, page, pageSize: PAGE_SIZE, companyId: company.id,
            });
            raw.push(...(data.datas || []).map((d) => d.data).filter(Boolean));
            await sleep(REQUEST_GAP);
            if (page >= (data.totalPage || 1)) break;
          }
        }
        const merged = mergeCities(raw.map((x) => jobFromApi(x, company)), preferredCities);
        if (!merged.length) empty.push(company.name);
        jobs.push(...merged);
      } catch (e) {
        failed.push(`${company.name}（${e.message}）`);
      }
    }
    reportProgress(companies.length, companies.length, '');
    return { jobs, failed, empty };
  }

  // ---------------------------------------------------------------------------
  // 与 popup 通信
  // ---------------------------------------------------------------------------

  function onMessage(msg, sender, sendResponse) {
    if (!msg || typeof msg.type !== 'string') return;

    // 心跳 + 当前已识别数量
    if (msg.type === 'JH_PING') {
      let companies = 0;
      try {
        companies = findCompanies().length;
      } catch (e) {
        console.warn(TAG, '识别公司失败', e);
      }
      sendResponse({ ok: true, count: cache.size, companies });
      return;
    }

    // 公司维度采集：拉取本页每家公司的全部岗位
    if (msg.type === 'JH_FETCH_COMPANIES') {
      (async () => {
        try {
          const companies = findCompanies();
          const result = await fetchCompanyJobs(companies, msg.recruitTypes || [1], msg.preferredCities || []);
          sendResponse({ ok: true, companies: companies.length, ...result });
        } catch (e) {
          sendResponse({ ok: false, error: e.message });
        }
      })();
      return true;
    }

    // 手动扫描：返回当前页 + 缓存中的全部岗位
    if (msg.type === 'JH_SCAN') {
      (async () => {
        try {
          let found = scanIntoCache(true);
          if (!found.length && !cache.size) found = await waitForJobs(4000);
          sendResponse({
            ok: true,
            pageCount: found.length,
            jobs: [...cache.values()],
            url: location.href,
          });
        } catch (e) {
          sendResponse({ ok: false, error: e.message });
        }
      })();
      return true; // 异步响应
    }

    // 清空缓存
    if (msg.type === 'JH_CLEAR') {
      cache.clear();
      sendResponse({ ok: true });
    }
  }

  chrome.runtime.onMessage.addListener(onMessage);

  // 暴露给下一次注入用于判断存活 / 清理
  window.__JOB_HUNTER__ = {
    jobs() {
      return [...cache.values()];
    },
    destroy() {
      observer.disconnect();
      clearTimeout(debounceTimer);
      try {
        chrome.runtime.onMessage.removeListener(onMessage);
      } catch (e) {
        /* 扩展已失效时忽略 */
      }
    },
  };

  // 首次加载先接上旧脚本识别过的岗位，再扫一遍
  inherited.forEach((job) => cache.set(cacheKey(job), job));
  scanIntoCache(false);
  console.log(TAG, '已注入，当前识别岗位数：', cache.size);
})();
