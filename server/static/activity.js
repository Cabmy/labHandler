// Presentation only: stages and tool results remain driven by runtime events.
class LabActivity {
  constructor(root, label) {
    this.root = root;
    this.label = label;
    this.stages = [];
    this.pending = new Map();
    this.frame = null;
    this.timer = null;
  }

  reset() {
    cancelAnimationFrame(this.frame);
    clearInterval(this.timer);
    this.pending.clear();
    this.stages = [];
    this.root.replaceChildren();
    this.label.textContent = '';
  }

  element(tag, className, text) {
    const element = document.createElement(tag);
    element.className = className;
    if (text != null) element.textContent = text;
    return element;
  }

  describe(node) {
    const [phase, ...parts] = String(node || 'activity').split(':');
    const titles = { remember_judge: '检查偏好', spec: '规划', dispatch: '安排执行', flash: '执行', judge: '检查结果', summary: '整理结果', takeover: '调整执行', activity: '处理中' };
    return { phase, title: titles[phase] || node, detail: parts.join(':') };
  }

  start(node) {
    const existing = this.stages.findLast(stage => stage.node === node && stage.state === 'running');
    if (existing) return existing;
    // Pro stages are sequential. Flash stages can remain active in parallel.
    const description = this.describe(node);
    if (description.phase !== 'flash') {
      for (const stage of this.stages.filter(stage => stage.state === 'running')) {
        this.settle(stage, 'done');
      }
    }
    const box = this.element('details', 'activity-stage');
    box.open = true;
    box.dataset.state = 'running';
    const header = this.element('summary', 'activity-header');
    const icon = this.element('span', 'stage-indicator');
    icon.setAttribute('aria-hidden', 'true');
    const heading = this.element('span', 'stage-heading');
    heading.append(this.element('span', 'stage-title', description.title));
    const meta = this.element('span', 'stage-meta', '进行中');
    const duration = this.element('span', 'stage-duration');
    heading.append(meta);
    const chevron = this.element('span', 'stage-chevron', '›');
    header.append(icon, heading, duration, chevron);
    const body = this.element('div', 'activity-body');
    box.append(header, body);
    this.root.append(box);
    const stage = { node, phase: description.phase, box, body, meta, duration, state: 'running', started: Date.now(), tools: 0, blocks: new Map() };
    this.stages.push(stage);
    // Native details keeps keyboard handling and lets users inspect prior stages.
    header.addEventListener('click', () => { stage.manuallyToggled = true; });
    header.title = description.detail || description.title;
    if (!this.timer) this.timer = setInterval(() => this.tick(), 1000);
    this.label.textContent = description.title;
    return stage;
  }

  stageFor(data = {}) {
    const node = data.assignment_id ? `flash:${data.assignment_id}` : data.node;
    if (node && !['pro', 'flash'].includes(node)) {
      return this.stages.findLast(stage => stage.node === node) || this.start(node);
    }
    const phase = data.phase === 'worker' ? 'flash' : data.phase;
    if (!node && !phase && this.stages.length) return this.stages.at(-1);
    const stage = this.stages.findLast(stage => stage.state === 'running' &&
      (phase ? stage.phase === phase : node === 'flash' ? stage.phase === 'flash' : stage.phase !== 'flash'));
    return stage || this.start(phase || (node === 'flash' ? 'flash' : 'activity'));
  }

  tick() {
    for (const stage of this.stages) {
      if (stage.state === 'running') stage.duration.textContent = this.elapsed(stage);
    }
  }

  elapsed(stage) {
    const seconds = Math.max(0, Math.floor((Date.now() - stage.started) / 1000));
    return seconds < 60 ? `${seconds}s` : `${Math.floor(seconds / 60)}m ${seconds % 60}s`;
  }

  settle(stage, state = 'done', caption) {
    if (!stage) return;
    stage.state = state;
    stage.box.dataset.state = state;
    stage.meta.textContent = caption || ({ done: '已完成', paused: '已暂停', error: '出错', blocked: '需要补充' }[state]);
    stage.duration.textContent = this.elapsed(stage);
    if (state === 'done' && !stage.manuallyToggled) stage.box.open = false;
    if (state === 'error' || state === 'blocked') stage.box.open = true;
  }

  complete(data) {
    const stage = this.stageFor(data);
    const entries = data.log || [];
    if (entries.some(entry => entry.halted)) { this.settle(stage, 'blocked'); return; }
    const count = entries.find(entry => entry.milestones != null)?.milestones;
    const decision = entries.find(entry => entry.decision)?.decision;
    const captions = { continue: '继续', finish: '已完成', revise_spec: '调整规划', takeover: '接手执行' };
    this.settle(stage, 'done', count != null ? `${count} 个步骤` : captions[decision]);
  }

  stream(data) {
    const stage = this.stageFor(data);
    const key = data.reasoning ? 'reasoning' : 'content';
    let block = stage.blocks.get(key);
    // Separate text on either side of a tool call, without one DOM node per token.
    if (!block) {
      if (data.reasoning) {
        const details = this.element('details', 'thinking-block');
        details.append(this.element('summary', '', '思考'));
        block = this.element('div', 'thinking-text');
        details.append(block);
        stage.body.append(details);
      } else {
        block = this.element('div', 'activity-text');
        stage.body.append(block);
      }
      stage.blocks.set(key, block);
    }
    this.pending.set(block, (this.pending.get(block) || '') + (data.text || ''));
    if (!this.frame) this.frame = requestAnimationFrame(() => this.flush());
  }

  flush() {
    const following = this.root.scrollHeight - this.root.scrollTop - this.root.clientHeight < 56;
    for (const [block, text] of this.pending) {
      // Bound rendered text without touching the runtime transcript.
      block.textContent = (block.textContent + text).slice(-16000);
    }
    this.pending.clear();
    this.frame = null;
    if (following) this.root.scrollTop = this.root.scrollHeight;
  }

  tool(data) {
    const stage = this.stageFor(data);
    stage.blocks.clear();
    const names = { read_file: '读取文件', list_dir: '查看文件', glob_files: '查找文件', grep_files: '搜索文件', write_file: '写入文件', patch_file: '修改文件', write_acceptance: '准备检查', run_command: '运行命令', execute_command: '运行命令', load_skill: '加载技能', load_skill_reference: '读取参考', notes_read: '读取笔记', notes_write: '更新笔记', submit_remember: '确认偏好', submit_spec: '提交规划', submit_dispatch: '分配执行', submit_brief: '提交结果', submit_judge: '确认结果', submit_summary: '整理总结', submit_halt: '请求补充' };
    const failed = data.error_class && data.error_class !== 'ok';
    const details = this.element('details', 'tool-action' + (failed ? ' tool-failed' : ''));
    const summary = this.element('summary', 'tool-heading');
    summary.append(this.element('span', 'tool-indicator', failed ? '!' : '✓'));
    summary.append(this.element('span', 'tool-title', names[data.name] || data.name));
    let args;
    try { args = JSON.parse(data.args || '{}'); } catch { args = {}; }
    const path = args.path || args.filename || args.task_id;
    if (typeof path === 'string') summary.append(this.element('span', 'tool-path', path));
    summary.append(this.element('span', 'tool-chevron', '›'));
    details.append(summary);
    const info = this.element('div', 'tool-details');
    info.append(this.element('code', 'tool-name', data.name));
    if (data.args) info.append(this.element('pre', '', data.args));
    if (data.result) info.append(this.element('pre', 'tool-result', data.result));
    details.append(info);
    stage.body.append(details);
    stage.tools += 1;
    if (stage.state === 'running') stage.meta.textContent = `${stage.tools} 个操作`;
    // Do not style a failed tool as a successful operation or completed stage.
    if (failed) this.note(data.result || '操作未完成', 'warning', data);
  }

  note(text, tone = '', data = {}) {
    const stage = this.stageFor(data);
    stage.blocks.clear();
    stage.body.append(this.element('div', 'activity-note ' + tone, text));
  }

  brief(data) {
    const id = data.assignment_id || data.task_id;
    const stage = this.stageFor({ node: id ? `flash:${id}` : 'flash' });
    const blocked = data.outcome === 'blocked';
    const failed = ['failed', 'spec_invalid'].includes(data.outcome) || data.gate === 'fail';
    const gates = { pass: '检查通过', fail: '检查未通过', test_invalid: '检查未执行', no_hard_criteria: '已交付' };
    if (data.brief) this.note(String(data.brief), '', { node: stage.node });
    this.settle(stage, blocked ? 'blocked' : failed ? 'error' : 'done', blocked ? '受阻' : gates[data.gate]);
  }

  finish(verdict) {
    cancelAnimationFrame(this.frame);
    this.flush();
    clearInterval(this.timer);
    this.timer = null;
    const state = verdict === 'pass' ? 'done' : verdict === 'need_user' ? 'blocked' : ['error', 'fail', 'failed'].includes(verdict) ? 'error' : 'paused';
    for (const stage of this.stages.filter(stage => stage.state === 'running')) this.settle(stage, state);
    this.label.textContent = { done: '已完成', blocked: '需要补充', error: '出错', paused: '已暂停' }[state];
  }
}
