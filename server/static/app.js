const $ = (id) => document.getElementById(id);
let toastTimer;
function notify(message, error = false) {
  clearTimeout(toastTimer);
  $('toast').textContent = message;
  $('toast').className = 'toast' + (error ? ' error' : '');
  toastTimer = setTimeout(() => $('toast').classList.add('hidden'), error ? 8000 : 4500);
}
// A single request boundary gives every action readable HTTP and network errors.
const nativeFetch = window.fetch.bind(window);
async function apiFetch(url, options) {
  try {
    const response = await nativeFetch(url, options);
    if (!response.ok) {
      const data = await response.json().catch(() => ({}));
      throw new Error(typeof data.detail === 'string' ? data.detail : `请求失败（${response.status}）`);
    }
    return response;
  } catch (error) {
    notify(error.message === 'Failed to fetch' ? '无法连接服务，请检查项目是否已启动。' : error.message, true);
    throw error;
  }
}
function updateState(label, active = false) {
  $('stateLabel').textContent = label;
  $('stateBadge').classList.toggle('running', active);
}
function applyTheme(theme) {
  document.documentElement.dataset.theme = theme;
  $('themeToggle').querySelector('span').textContent = theme === 'dark' ? '切换浅色外观' : '切换深色外观';
}
try { applyTheme(localStorage.getItem('lab-theme') || 'light'); } catch { applyTheme('light'); }
$('themeToggle').onclick = () => {
  const theme = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
  applyTheme(theme);
  try { localStorage.setItem('lab-theme', theme); } catch {}
};
let es = null;          // EventSource
let resumeAvailable = false;
let running = false;    // 任务运行态（提交成功 → true；SSE final/error → false）

function setResumeAvailable(value) {
  resumeAvailable = Boolean(value);
  $('resumeNotice').classList.toggle('hidden', !resumeAvailable || running);
  $('run').innerHTML = running ? '执行中…' : (resumeAvailable ? '继续' : '开始') + '<svg><use href="#i-arrow"/></svg>';
  $('run').disabled = running;
  $('question').placeholder = resumeAvailable ? '补充指令（可选）' : '想在这个 lab 中做什么？';
}

// ─── 运行态统一切换（tab 置灰 / 上传禁用 / 开始按钮禁用） ──
function setRunning(v) {
  running = v;
  $('run').disabled = v;
  $('stopbtn').disabled = !v;
  $('dreambtn').disabled = v;
  $('status').textContent = v ? '运行中…' : '⌘ / Ctrl + Enter';
  document.body.dataset.running = String(v);
  $('stopbtn').classList.toggle('hidden', !v);
  $('drop').disabled = v;
  $('question').disabled = v;
  setResumeAvailable(resumeAvailable);
  document.querySelectorAll('.del-btn').forEach(button => button.disabled = v);
  updateState(v ? '执行中' : '就绪', v);
  $('drop').classList.toggle('disabled', v);
  $('fileinput').disabled = v;
  $('tabSkill').disabled = v;
  $('tabRemember').disabled = v;
  $('skillNotice').classList.toggle('hidden', !v);
  $('secSkill').classList.toggle('hidden', v);
  if (v && (!$('viewSkill').classList.contains('hidden') || !$('viewRemember').classList.contains('hidden'))) {
    switchTab('task');
  }
}

// ─── tab 切换 ───────────────────────────────────────────────
function switchTab(name) {
  $('viewTask').classList.toggle('hidden', name !== 'task');
  $('viewRemember').classList.toggle('hidden', name !== 'remember');
  $('viewSkill').classList.toggle('hidden', name !== 'skill');
  $('tabTask').classList.toggle('active', name === 'task');
  $('tabRemember').classList.toggle('active', name === 'remember');
  $('tabSkill').classList.toggle('active', name === 'skill');
  $('pageName').textContent = { task: 'Lab', remember: '偏好', skill: '技能' }[name];
  for (const [key, id] of Object.entries({ task: 'tabTask', remember: 'tabRemember', skill: 'tabSkill' })) {
    if (key === name) $(id).setAttribute('aria-current', 'page');
    else $(id).removeAttribute('aria-current');
  }
  if (name === 'remember') {
    loadRules();
    const input = $('ruleInput');
    if (input) setTimeout(() => input.focus(), 0);
  }
}
$('tabTask').onclick = () => switchTab('task');
$('tabRemember').onclick = () => { if (!running) switchTab('remember'); };
$('tabSkill').onclick = () => { if (!running) switchTab('skill'); };

// ─── 文件上传 / 删除 ────────────────────────────────────────
async function refreshFiles(listEl) {
  const files = await (await apiFetch('/api/files')).json();
  listEl.replaceChildren();
  $('fileCount').textContent = files.length;

  for (const file of files) {
    const li = document.createElement('li');
    const link = document.createElement('a');
    link.href = '/api/files/' + encodeURIComponent(file.name);
    link.download = file.name;
    link.innerHTML = '<svg aria-hidden="true"><use href="#i-file"/></svg>';
    const name = document.createElement('span');
    name.textContent = file.name;
    link.append(name);
    const meta = document.createElement('span');
    meta.className = 'file-meta';
    meta.textContent = file.size < 1024 ? file.size + ' B' : file.size < 1048576 ? (file.size / 1024).toFixed(1) + ' KB' : (file.size / 1048576).toFixed(1) + ' MB';
    // Result files are download-only; workspace edits remain in materials.
    if (listEl.id === 'filelist') {
      const button = document.createElement('button');
      button.className = 'del-btn';
      button.textContent = '×';
      button.setAttribute('aria-label', '删除 ' + file.name);
      button.title = '移至回收站';
      button.disabled = running;
      button.onclick = () => deleteFile(file.name).catch(error => console.error(error));
      meta.append(button);
    }
    li.append(link, meta);
    listEl.append(li);
  }
}

async function deleteFile(name) {
  if (!confirm(`删除 ${name}？（会移入 .trash/，不真删）`)) return;
  const resp = await apiFetch('/api/files/' + encodeURIComponent(name), { method: 'DELETE' });
  await refreshFiles($('filelist'));
  if (!$('secResult').classList.contains('hidden')) await refreshFiles($('filelist2'));
}
async function uploadFiles(files) {
  if (!files.length || running) return;
  const fd = new FormData();
  for (const f of files) fd.append('files', f);
  const resp = await apiFetch('/api/files', { method: 'POST', body: fd });
  await refreshFiles($('filelist'));
  $('fileinput').value = '';
  notify(`已上传 ${files.length} 个文件`);
}
$('drop').onclick = () => { if (!running) $('fileinput').click(); };
$('fileinput').onchange = (e) => uploadFiles([...e.target.files]);
$('drop').ondragover = (e) => { e.preventDefault(); if (!running) $('drop').classList.add('hover'); };
$('drop').ondragleave = () => $('drop').classList.remove('hover');
$('drop').ondrop = (e) => {
  e.preventDefault(); $('drop').classList.remove('hover');
  uploadFiles([...e.dataTransfer.files]);
};

const activity = new LabActivity($('log'), $('progressLabel'));

function attachStream() {
  if (es) es.close();
  $('secProgress').classList.remove('hidden');
  $('secResult').classList.add('hidden');
  es = new EventSource('/api/task/stream');
  es.onopen = () => { $('status').textContent = '运行中…'; updateState('执行中', true); };
  const listen = (kind, handler) => es.addEventListener(kind, event => {
    try { handler(JSON.parse(event.data)); }
    catch (error) { console.error('Invalid activity event', kind, error); }
  });
  listen('node_start', data => activity.start(data.node));
  listen('content', data => activity.stream(data));
  listen('tool', data => activity.tool(data));
  listen('node_done', data => activity.complete(data));
  listen('cards', data => {
    // Retrieval diagnostics only appear when something needs attention.
    if (data.errors?.length) activity.note('部分记忆未能读取：' + data.errors.join(' · '), 'warning');
  });
  listen('error_class', data => {
    if (data.error_class && data.error_class !== 'ok') activity.note(data.detail || '正在处理异常，尝试恢复…', 'warning', data);
  });
  listen('recovery', data => activity.note('已恢复执行', '', data));
  listen('worker_brief', data => activity.brief(data));
  listen('pro_takeover', data => { activity.start('takeover'); if (data.reason) activity.note(data.reason, '', { node: 'takeover' }); });
  listen('halt', data => {
    const stage = activity.stageFor(data);
    activity.note(data.need_from_user || data.reason || '需要补充信息', 'warning', { node: stage.node });
    activity.settle(stage, 'blocked');
  });
  es.addEventListener('error', event => {
    // A transport disconnection is not a failed lab: EventSource reconnects.
    if (!event.data) {
      $('status').textContent = '正在重连…';
      updateState('重新连接', true);
      $('progressLabel').textContent = '正在重连';
      return;
    }
    let detail;
    try { detail = JSON.parse(event.data).detail; } catch { detail = event.data; }
    activity.note(detail || '运行出错', 'warning');
    finish(null, 'error').catch(error => console.error(error));
  });
  listen('final', data => { finish(data).catch(error => console.error(error)); });
}

// ─── 任务提交 ───────────────────────────────────────────────
$('run').onclick = async () => {
  if (running) return;
  if (resumeAvailable) return resumeTask(true);
  const q = $('question').value.trim();
  if (!q) { $('question').focus(); notify('请输入 lab 指令。'); return; }
  if (running) return;
  $('run').disabled = true;
  try {
    const resp = await apiFetch('/api/task', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ question: q }),
    });
    $('question').value = '';
    setRunning(true);
    activity.reset();
    attachStream();
  } finally { $('run').disabled = running; }
};

async function finish(final, reason) {
  if (es) { es.close(); es = null; }
  setRunning(false);
  activity.finish(reason || final?.verdict);
  if (!final) {
    updateState(reason === 'error' ? '出错' : '已暂停');
    $('run').disabled = true;
    const state = await (await apiFetch('/api/state')).json();
    setResumeAvailable(state.resumable);
    return;
  }
  setResumeAvailable(['paused', 'stopped'].includes(final.verdict));
  updateState(final.verdict === 'pass' ? '已完成' : final.verdict === 'need_user' ? '需要补充' : ['fail', 'failed'].includes(final.verdict) ? '未完成' : '已暂停');
  $('secResult').classList.remove('hidden');
  const v = $('verdict');
  const labels = { pass: '✓ 已完成', need_user: '需要补充信息', stopped: '已停止', paused: '已暂停', fail: '未完成' };
  v.textContent = (labels[final.verdict] || final.verdict || '执行已结束') + (final.elapsed != null ? ` · ${final.elapsed}s` : '');
  v.className = final.verdict === 'pass' ? 'pass' : 'fail';
  await refreshFiles($('filelist2'));
  await refreshFiles($('filelist'));
  const s = await (await apiFetch('/api/summary')).json();
  $('summary').textContent = s.summary || '（无 SUMMARY.md）';
}

$('stopbtn').onclick = async () => {
  $('stopbtn').disabled = true;
  try { await apiFetch('/api/stop', { method: 'POST' }); $('status').textContent = '正在停止，等待当前操作结束…'; }
  catch { $('stopbtn').disabled = false; }
};

// ─── 归档结束 ───────────────────────────────────────────────
$('donebtn').onclick = async () => {
  if (!confirm('归档当前 lab？文件将移至回收站，沙箱会重建。')) return;
  const resp = await apiFetch('/api/done', { method: 'POST' });
  const d = await resp.json();
  notify('Lab 已归档。');
  setResumeAvailable(false);
  updateState('就绪');
  $('question').value = '';
  $('status').textContent = '⌘ / Ctrl + Enter';
  $('secResult').classList.add('hidden');
  $('secProgress').classList.add('hidden');
  await refreshFiles($('filelist'));
};

// ─── 知识治理（/dream） ─────────────────────────────────────
$('dreambtn').onclick = async () => {
  if (!confirm('对归档知识卡片做一轮 LLM 合并/去重/淘汰？（可能需要几分钟）')) return;
  $('dreambtn').disabled = true;
  try {
    const resp = await apiFetch('/api/dream', { method: 'POST' });
    const d = await resp.json();
    notify(`治理完成：共 ${d.total_cards} 张卡 / 判定 ${d.judged} 组 / 新建合并卡 ${d.merged_created} / 淘汰 ${d.retired}`);
  } finally { $('dreambtn').disabled = false; }
};

// ─── Skill 编辑（提案 → diff → 确认/取消） ──────────────────
let editId = null;   // 当前 pending 提案 id

async function loadSkills() {
  try {
    const skills = await (await apiFetch('/api/skills')).json();
    $('skillSel').replaceChildren(...skills.map(skill => new Option(({ coding: '编程', essay: '写作', lab_report: '实验报告' }[skill.name] || skill.name), skill.name)));
  } catch (e) { /* 服务未就绪时静默 */ }
}
function renderDiffs(diffs) {
  $('skillDiffs').innerHTML = '';
  for (const d of diffs) {
    const title = document.createElement('div');
    title.className = 'diff-file'; title.textContent = '📄 ' + d.file;
    const pre = document.createElement('pre');
    for (const ln of d.diff.split('\n')) {
      const span = document.createElement('span');
      if (ln.startsWith('+') && !ln.startsWith('+++')) span.className = 'add';
      else if (ln.startsWith('-') && !ln.startsWith('---')) span.className = 'del';
      span.textContent = ln + '\n';
      pre.appendChild(span);
    }
    $('skillDiffs').append(title, pre);
  }
}
$('skillPropose').onclick = async () => {
  const instr = $('skillInstr').value.trim();
  if (!instr) { notify('请填写编辑指令'); return; }
  $('skillPropose').disabled = true;
  $('skillStatus').textContent = 'LLM 分析中…';
  $('skillMsg').textContent = ''; $('skillDiffs').innerHTML = '';
  $('skillActions').classList.add('hidden'); editId = null;
  try {
    const resp = await apiFetch('/api/edit_skill', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ skill_name: $('skillSel').value, instruction: instr }),
    });
    const d = await resp.json();
    let msg = d.summary || '';
    if (d.style_samples && d.style_samples.length)
      msg += `　（已读取文风样本：${d.style_samples.join('、')}）`;
    if (d.sample_failures && d.sample_failures.length)
      msg += `　⚠️ 文风样本未用上：${d.sample_failures.join('；')}`;
    $('skillMsg').textContent = msg;
    if (!d.n_operations) {
      $('skillMsg').textContent += '　—— 判官认为无需改动';
      return;
    }
    renderDiffs(d.diffs);
    editId = d.edit_id;
    $('skillActions').classList.remove('hidden');
  } finally {
    $('skillPropose').disabled = false;
    $('skillStatus').textContent = '';
  }
};
async function applyEdit(confirm) {
  if (!editId) return;
  const resp = await apiFetch('/api/edit_skill/apply', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ edit_id: editId, confirm }),
  });
  const d = await resp.json();
  $('skillMsg').textContent = confirm ? ('✓ 已应用：' + (d.applied || []).join('、')) : '已取消，未落盘';
  $('skillDiffs').innerHTML = '';
  $('skillActions').classList.add('hidden');
  editId = null;
}
$('skillApply').onclick = () => applyEdit(true);
$('skillCancel').onclick = () => applyEdit(false);

// ─── 长期偏好规则（/api/profile + /api/remember） ──────────
async function loadRules() {
  try {
    const prof = await (await apiFetch('/api/profile')).json();
    const rules = (prof.preferences || {}).style_rules || [];
    const ul = $('ruleList');
    ul.innerHTML = '';
    if (!rules.length) {
      const li = document.createElement('li');
      li.style.color = 'var(--dim)'; li.textContent = '暂无偏好';
      ul.appendChild(li); return;
    }
    for (const r of rules) {
      const li = document.createElement('li'); li.textContent = r; ul.appendChild(li);
    }
  } catch (e) { /* 服务未就绪时静默 */ }
}
$('ruleAdd').onclick = async () => {
  const rule = $('ruleInput').value.trim();
  if (!rule) return;
  const resp = await apiFetch('/api/remember', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ rule }),
  });
  const d = await resp.json();
  $('ruleInput').value = '';
  await loadRules();
};

// Resume is explicit: opening a page never archives an unfinished task.
async function resumeTask(continueLab) {
  if (running || $('resumeBtn').disabled) return;
  if (!continueLab && !confirm('归档上次未完成的 lab？')) return;
  $('run').disabled = true;
  $('resumeBtn').disabled = $('discardBtn').disabled = true;
  try {
    await apiFetch('/api/resume', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ continue_lab: continueLab, instruction: continueLab ? $('question').value.trim() : '' }),
    });
    $('question').value = '';
    setResumeAvailable(false);
    if (continueLab) { setRunning(true); activity.reset(); attachStream(); }
    else { updateState('就绪'); await refreshFiles($('filelist')); notify('Lab 已归档。'); }
  } finally { $('resumeBtn').disabled = $('discardBtn').disabled = false; $('run').disabled = running; }
}
$('resumeBtn').onclick = () => resumeTask(true);
$('discardBtn').onclick = () => resumeTask(false);
$('question').onkeydown = event => {
  if ((event.ctrlKey || event.metaKey) && event.key === 'Enter' && !event.isComposing) { event.preventDefault(); if (!running) $('run').click(); }
};
$('ruleInput').onkeydown = event => { if (event.key === 'Enter' && !event.isComposing) $('ruleAdd').click(); };
async function init() {
  const results = await Promise.allSettled([refreshFiles($('filelist')), loadSkills(), loadRules(), apiFetch('/api/state').then(response => response.json())]);
  const result = results[3];
  if (result.status !== 'fulfilled') { updateState('连接失败'); return; }
  const st = result.value;
  if (st.running) { setRunning(true); attachStream(); }
  else if (st.resumable) { setResumeAvailable(true); updateState('待继续'); }
  else if (st.status === 'FINISHED') { await finish({ verdict: st.verdict }); }
  else { setResumeAvailable(false); }
}
// Keep action failures at the UI boundary, with a visible message.
document.querySelectorAll('button, input, textarea').forEach(element => {
  for (const property of ['onclick', 'onchange', 'ondrop', 'onkeydown']) {
    const action = element[property];
    if (action) element[property] = event => {
      try { Promise.resolve(action(event)).catch(error => console.error(error)); }
      catch (error) { console.error(error); notify(error.message, true); }
    };
  }
});
init().catch(error => console.error(error));
