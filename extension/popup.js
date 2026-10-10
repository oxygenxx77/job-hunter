/**
 * popup.js —— 扩展弹窗逻辑
 *
 * 流程：点击「采集本页岗位」→ 让 content.js 扫描页面 → 把岗位 POST 到本地后端
 *       → 显示「本次采集到 X 个岗位」
 */

// 本地后端地址（8000 端口留给秋招看板）
const API_BASE = 'http://127.0.0.1:8001';

const $ = (id) => document.getElementById(id);
const collectBtn = $('collectBtn');
const companyBtn = $('companyBtn');

// 偏好里的城市顺序：多城市岗位合并时，跳转链接取排在最前的城市
let preferredCities = [];

// ---------------------------------------------------------------------------
// 工具函数
// ---------------------------------------------------------------------------

/** 获取当前激活的标签页 */
async function getActiveTab() {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  return tab;
}

/** 判断是否为牛客网页面 */
function isNowcoder(url) {
  return /^https:\/\/www\.nowcoder\.com\//.test(url || '');
}

/** 向 content script 发送消息（Promise 封装） */
function sendMessage(tabId, message) {
  return new Promise((resolve, reject) => {
    chrome.tabs.sendMessage(tabId, message, (resp) => {
      if (chrome.runtime.lastError) {
        reject(new Error(chrome.runtime.lastError.message));
      } else {
        resolve(resp);
      }
    });
  });
}

/**
 * 确保 content script 已注入。
 * 安装扩展前就打开的页面不会自动注入，这里用 scripting API 补注入一次。
 */
async function ensureContentScript(tabId) {
  try {
    return await sendMessage(tabId, { type: 'JH_PING' });
  } catch (e) {
    await chrome.scripting.executeScript({ target: { tabId }, files: ['content.js'] });
    return sendMessage(tabId, { type: 'JH_PING' });
  }
}

/** 显示结果提示 */
function showResult(html, type = 'success') {
  const el = $('result');
  el.className = `result show ${type}`;
  el.innerHTML = html;
}

/** 设置后端状态显示 */
function setBackendStatus(ok, text) {
  $('backendDot').className = `dot ${ok ? 'ok' : 'bad'}`;
  $('backendText').textContent = text;
}

/** 渲染上次采集记录 */
function renderLast(last) {
  if (!last) return;
  $('last').textContent = `上次采集：${last.time}，共 ${last.count} 个，新增 ${last.inserted} 个`;
}

// ---------------------------------------------------------------------------
// 初始化：检查后端和当前页面
// ---------------------------------------------------------------------------

async function checkBackend() {
  try {
    const resp = await fetch(`${API_BASE}/api/stats`, { cache: 'no-store' });
    if (!resp.ok) throw new Error(resp.status);
    const stats = await resp.json();
    setBackendStatus(true, `已连接 · 共 ${stats.total} 个岗位`);
    return true;
  } catch (e) {
    setBackendStatus(false, '未连接');
    return false;
  }
}

async function checkPage() {
  const tab = await getActiveTab();
  if (!tab || !isNowcoder(tab.url)) {
    $('pageText').textContent = '非牛客页面';
    collectBtn.disabled = true;
    showResult('请先打开 <b>牛客网</b> 的校招岗位列表页，再点击采集。', 'info');
    return;
  }
  try {
    const resp = await ensureContentScript(tab.id);
    $('pageText').textContent = `已识别 ${resp?.count ?? 0} 个岗位`;
    $('companyCount').textContent = resp?.companies ?? 0;
    $('companyBox').hidden = !resp?.companies;
  } catch (e) {
    $('pageText').textContent = '脚本注入失败，请刷新页面';
  }
}

/** 读取偏好：用于「只要意向方向」的提示和城市排序 */
async function loadPreferences() {
  try {
    const resp = await fetch(`${API_BASE}/api/preferences`, { cache: 'no-store' });
    const prefs = await resp.json();
    preferredCities = (prefs.city_tiers || []).flat();
    if (!preferredCities.length) preferredCities = prefs.target_cities || [];
    const roles = prefs.role_priority || (prefs.target_roles || []).join('、');
    $('targetHint').textContent = roles
      ? `意向方向：${roles}（在管理面板「设置偏好」里改）`
      : '还没设置意向方向，会保留全部岗位';
    $('onlyTarget').disabled = !roles;
  } catch (e) {
    $('targetHint').textContent = '';
  }
}

async function init() {
  const { lastResult } = await chrome.storage.local.get('lastResult');
  renderLast(lastResult);
  await Promise.all([checkBackend(), checkPage(), loadPreferences()]);
}

/** 把岗位上报到后端，返回后端统计 */
async function postJobs(jobs, onlyTargetRoles = false) {
  let resp;
  try {
    resp = await fetch(`${API_BASE}/api/jobs`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ jobs, source: '牛客', only_target_roles: onlyTargetRoles }),
    });
  } catch (e) {
    setBackendStatus(false, '未连接');
    throw new Error('无法连接本地服务，请先双击桌面的「求职助手.command」启动');
  }
  if (!resp.ok) throw new Error(`后端返回错误：HTTP ${resp.status}`);
  return resp.json();
}

async function saveLast(count, inserted) {
  const lastResult = {
    time: new Date().toLocaleString('zh-CN', { hour12: false }),
    count,
    inserted,
  };
  await chrome.storage.local.set({ lastResult });
  renderLast(lastResult);
  checkBackend();
}

// ---------------------------------------------------------------------------
// 采集
// ---------------------------------------------------------------------------

async function collect() {
  collectBtn.disabled = true;
  collectBtn.textContent = '采集中…';
  try {
    const tab = await getActiveTab();
    if (!tab || !isNowcoder(tab.url)) throw new Error('当前不是牛客网页面');

    // 1. 让 content script 扫描页面
    await ensureContentScript(tab.id);
    const scan = await sendMessage(tab.id, { type: 'JH_SCAN' });
    if (!scan || !scan.ok) throw new Error(scan?.error || '扫描失败');

    const jobs = scan.jobs || [];
    $('pageText').textContent = `已识别 ${jobs.length} 个岗位`;
    if (!jobs.length) {
      showResult(
        $('companyBox').hidden
          ? '未在本页识别到岗位。<br>请确认是岗位列表页，并向下滚动加载后重试。'
          : '本页是公司列表，没有具体岗位。<br>请用下方「采集这些公司的全部岗位」。',
        'info'
      );
      return;
    }

    // 2. 上报到后端
    const data = await postJobs(jobs);

    // 3. 展示结果并记录
    showResult(
      `本次采集到 <strong>${jobs.length}</strong> 个岗位<br>` +
        `新增 ${data.inserted} 个，重复 ${data.duplicated} 个` +
        (data.invalid ? `，无效 ${data.invalid} 个` : '') +
        (data.inserted ? '<br>AI 正在后台分析匹配度…' : ''),
      'success'
    );
    await saveLast(jobs.length, data.inserted);
  } catch (e) {
    showResult(`采集失败：${e.message}`, 'error');
  } finally {
    collectBtn.disabled = false;
    collectBtn.textContent = '采集本页岗位';
  }
}

function escapeHtml(text) {
  return String(text).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]);
}

/** 公司列表页：拉取本页每家公司的全部岗位并上报 */
async function collectCompanies() {
  companyBtn.disabled = true;
  collectBtn.disabled = true;
  companyBtn.textContent = '拉取中…';
  showResult('正在逐家拉取岗位，公司多时需要几十秒，<b>请不要关闭这个弹窗</b>。', 'info');
  try {
    const tab = await getActiveTab();
    if (!tab || !isNowcoder(tab.url)) throw new Error('当前不是牛客网页面');
    await ensureContentScript(tab.id);

    const recruitTypes = $('recruitType').value.split(',').map(Number);
    const resp = await sendMessage(tab.id, { type: 'JH_FETCH_COMPANIES', recruitTypes, preferredCities });
    if (!resp || !resp.ok) throw new Error(resp?.error || '拉取失败');

    const failedNote = resp.failed.length
      ? `<br>未拉到：${resp.failed.slice(0, 5).map(escapeHtml).join('、')}${resp.failed.length > 5 ? ` 等 ${resp.failed.length} 家` : ''}`
      : '';
    if (!resp.jobs.length) {
      showResult(`${resp.companies} 家公司都没有符合所选类型的岗位。${failedNote}`, 'info');
      return;
    }

    const data = await postJobs(resp.jobs, $('onlyTarget').checked && !$('onlyTarget').disabled);
    showResult(
      `从 ${resp.companies} 家公司拉到 <strong>${resp.jobs.length}</strong> 个岗位<br>` +
        (data.filtered ? `不在意向方向、已跳过 ${data.filtered} 个<br>` : '') +
        `新增 ${data.inserted} 个，重复 ${data.duplicated} 个` +
        (data.inserted ? '<br>AI 正在后台分析匹配度，可在管理面板「按公司推荐」里看' : '') +
        failedNote,
      'success'
    );
    await saveLast(data.received, data.inserted);
  } catch (e) {
    showResult(`采集失败：${e.message}`, 'error');
  } finally {
    companyBtn.disabled = false;
    collectBtn.disabled = false;
    companyBtn.textContent = '采集这些公司的全部岗位';
  }
}

chrome.runtime.onMessage.addListener((msg) => {
  if (msg && msg.type === 'JH_PROGRESS' && msg.done < msg.total) {
    companyBtn.textContent = `拉取中 ${msg.done + 1}/${msg.total}：${msg.name}`;
  }
});

// ---------------------------------------------------------------------------
// 事件绑定
// ---------------------------------------------------------------------------

collectBtn.addEventListener('click', collect);
companyBtn.addEventListener('click', collectCompanies);

$('openPanelBtn').addEventListener('click', () => {
  chrome.tabs.create({ url: `${API_BASE}/` });
});

$('clearBtn').addEventListener('click', async () => {
  const tab = await getActiveTab();
  if (!tab || !isNowcoder(tab.url)) return;
  try {
    await ensureContentScript(tab.id);
    await sendMessage(tab.id, { type: 'JH_CLEAR' });
    $('pageText').textContent = '已识别 0 个岗位';
    showResult('已清空本页识别缓存。', 'info');
  } catch (e) {
    showResult(`清空失败：${e.message}`, 'error');
  }
});

init();
