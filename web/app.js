/* MedGraph Flow - Mobile-First Adaptive Micro-Card & Graph UI Controller */
(function () {
  'use strict';

  const R = window.MedRouter;
  const S = window.MedSync;
  const $ = id => document.getElementById(id);

  const esc = v => String(v ?? '').replace(/[&<>"']/g, c => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]));

  const stripMd = v => String(v ?? '').replace(/\*\*([^*\n]+?)\*\*/g, '$1');

  const ACRO_STOP = new Set(['VS', 'OR', 'AND', 'THE', 'FOR', 'NO', 'OK', 'IN', 'ON', 'AT', 'TO', 'OF', 'BY', 'IS', 'ARE', 'AS']);

  function collectRuleMatches(text, rules) {
    const candidates = [];
    for (const rule of rules) {
      const re = new RegExp(rule.re.source, rule.re.flags);
      let m;
      while ((m = re.exec(text)) !== null) {
        if (!m[0]) {
          re.lastIndex += 1;
          continue;
        }
        const sub = rule.group ? m[rule.group] : m[0];
        if (!sub) continue;
        const offset = rule.group ? m[0].lastIndexOf(sub) : 0;
        const start = m.index + Math.max(0, offset);
        const end = start + sub.length;
        if (rule.acroCheck && ACRO_STOP.has(sub.toUpperCase())) continue;
        candidates.push({ start, end, cls: rule.cls, prio: rule.prio, token: sub });
      }
    }
    return candidates;
  }

  function highlightPlainSegment(text, opts = {}) {
    if (!text) return '';
    const mode = opts.mode || 'prose';
    const baseMaxSpans = opts.maxSpans ?? (mode === 'matrix_cell' ? 2 : (mode === 'cloze_outside' ? 1 : 3));
    const maxRatio = opts.maxRatio ?? (mode === 'matrix_cell' ? 0.38 : 0.30);

    const primaryRules = [
      {
        cls: 'kw-contrast',
        prio: 1,
        re: /(?:而非|区别于|并非|唯独|绝非|恰恰相反|失代偿|不可逆|禁止|绝对不应期|假阳性|假阴性)/g,
      },
      {
        cls: 'kw-label',
        prio: 2,
        group: 1,
        re: /(?:^|[。；;\n①②③④⑤⑥⑦⑧⑨⑩])\s*([^\n，,。；;：:=＝（）()]{2,14}[：:])/g,
      },
      {
        cls: 'kw-core',
        prio: 2,
        group: 1,
        re: /(?:^|[。；;\n])\s*([^\n，,。；;=＝（）()]{2,14})\s*(?==|＝)/g,
      },
      {
        cls: 'kw-metric',
        prio: 3,
        re: /(?:pH\s*\d+(?:\.\d+)?(?:\s*[-–~]\s*\d+(?:\.\d+)?)?|[><≥≤＜＞≈]\s*\d+(?:\.\d+)?(?:\s*[-–~～/]\s*\d+(?:\.\d+)?)?(?:\s*(?:%|‰|g\/L|mg\/dL|mmol\/L|μmol\/L|mEq\/L|mV|ms|nm|μm|um|mm|cm|kDa|bp|aa|℃|mmHg|kPa|mL|L|ATP|NADH|NADPH|h|d|min|s|周|天|小时|倍|个|分子|条))?|\d+(?:\.\d+)?(?:\s*[-–~～至]\s*\d+(?:\.\d+)?)?\s*(?:%|‰|g\/L|mg\/dL|mmol\/L|μmol\/L|mEq\/L|mV|ms|nm|μm|um|mm|cm|kDa|bp|aa|℃|mmHg|kPa|mL|ATP|NADH|NADPH|高能磷酸键|天|周|小时)|[^\s，,。；;（）()、→：:=＋+/]{1,8}[↑↓])/g,
      },
    ];

    if (mode !== 'cloze_outside' && !opts.hasExplicit) {
      primaryRules.push({
        cls: 'kw-core',
        prio: 4,
        group: 2,
        acroCheck: true,
        re: /(^|[^A-Za-z0-9])([A-Za-z][A-Za-z0-9\-α-ωΑ-Ω]*[⁺⁻²³₁₂]+[A-Za-z0-9\-⁺⁻²³₁₂]*|[A-Z]{2,7}(?:[-/][A-Z0-9α-ωΑ-ΩⅠⅡⅢⅣⅤ]{1,5})?|cAMP|cGMP|mRNA|rRNA|tRNA|miRNA|eNOS|iNOS|nNOS)(?=[^A-Za-z0-9]|$)/g,
      });
    }

    let cands = collectRuleMatches(text, primaryRules);
    if (!cands.length && mode !== 'cloze_outside' && !opts.hasExplicit && text.length >= 22) {
      cands = collectRuleMatches(text, [
        {
          cls: 'kw-core',
          prio: 5,
          re: /[^\s，,。；;（）()、→：:“”"'为系呈致使得而并因故或与及将把被在于对由是从则]{2,5}(?:升高|降低|受阻|衰竭|扩张|增高|下降|坏死|凋亡|渗出|变性|化生|转移|增生)/g,
        },
      ]);
    }

    if (!cands.length) return esc(text);
    cands.sort((a, b) => a.prio - b.prio || a.start - b.start || (b.end - b.start) - (a.end - a.start));

    const kept = [];
    const seenTokens = new Set();
    let usedChars = 0;
    const totalLen = Math.max(text.length, 1);

    for (const c of cands) {
      if (kept.length >= baseMaxSpans) break;
      if (kept.some(k => c.start < k.end && c.end > k.start)) continue;
      if (c.prio >= 3 && seenTokens.has(c.token)) continue;
      const len = c.end - c.start;
      const nextRatio = (usedChars + len) / totalLen;
      if (kept.length > 0 && nextRatio > maxRatio) continue;
      if (kept.length === 0 && nextRatio > 0.65 && len > 14) continue;
      kept.push(c);
      seenTokens.add(c.token);
      usedChars += len;
    }

    if (!kept.length) return esc(text);
    kept.sort((a, b) => a.start - b.start);

    let out = '';
    let pos = 0;
    for (const k of kept) {
      if (k.start > pos) out += esc(text.slice(pos, k.start));
      out += `<span class="${k.cls}">${esc(text.slice(k.start, k.end))}</span>`;
      pos = k.end;
    }
    if (pos < text.length) out += esc(text.slice(pos));
    return out;
  }

  function renderRichText(rawText, opts = {}) {
    const src = String(rawText ?? '');
    if (!src) return '';
    if (settings.highlightKeywords === false) return esc(stripMd(src));

    const parts = src.split(/(\*\*[^*\n]{1,48}?\*\*)/g);
    const hasExplicit = parts.some(p => p.startsWith('**') && p.endsWith('**') && p.length > 4);
    if (opts.mode === 'stem') {
      return parts.map(part => {
        if (part.startsWith('**') && part.endsWith('**') && part.length > 4) {
          return `<span class="kw-core">${esc(part.slice(2, -2))}</span>`;
        }
        return esc(part);
      }).join('');
    }

    const segOpts = hasExplicit
      ? { ...opts, hasExplicit: true, maxSpans: Math.min(opts.maxSpans ?? 1, 1) }
      : opts;

    return parts.map(part => {
      if (part.startsWith('**') && part.endsWith('**') && part.length > 4) {
        return `<span class="kw-core">${esc(part.slice(2, -2))}</span>`;
      }
      return highlightPlainSegment(part, segOpts);
    }).join('');
  }

  let graph = null;
  let progress = null;
  let settings = S.loadSettings();
  let syncTimer = null;
  let toastTimer = null;
  let fileHandle = null;
  let atlasTab = 'galaxy';

  const cardState = {
    nodeId: null,
    enteredAt: 0,
    stepsRevealed: 1,
    openClozes: new Set(),
    totalClozes: 0,
    clozeToggleCount: 0,
    quizAttempted: false,
    quizFirstTryCorrect: false,
    quizSelectedIdx: null,
    diagramOpen: false,
    selectedGalaxyNodeId: null,
    selectedGalaxyEdgeId: null,
  };

  function showToast(msg) {
    const el = $('toast');
    el.textContent = msg;
    el.classList.add('show');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => el.classList.remove('show'), 3200);
  }

  function loadInitialGraph() {
    const override = S.loadCachedGraphOverride();
    if (override && override.schema_version === 1 && Array.isArray(override.nodes)) {
      return override;
    }
    const inlineEl = document.getElementById('medgraph-data');
    if (inlineEl && inlineEl.textContent.trim() && !inlineEl.textContent.includes('__GRAPH_DATA__')) {
      return JSON.parse(inlineEl.textContent);
    }
    return null;
  }

  function persistProgress() {
    S.saveLocalProgress(progress);
    if (fileHandle) {
      writeLocalFile(progress).catch(() => {});
    }
    if (settings.autoSync && settings.githubToken && settings.gistId) {
      clearTimeout(syncTimer);
      syncTimer = setTimeout(() => {
        S.syncWithGist(settings, progress)
          .then(res => {
            progress = res.progress;
            S.saveLocalProgress(progress);
          })
          .catch(() => {});
      }, 2200);
    }
  }

  async function writeLocalFile(dataObj) {
    if (!fileHandle) return;
    const writable = await fileHandle.createWritable();
    await writable.write(JSON.stringify(dataObj, null, 2));
    await writable.close();
  }

  function resetCardInteraction(node) {
    cardState.nodeId = node ? node.id : null;
    cardState.enteredAt = Date.now();
    cardState.stepsRevealed = 1;
    cardState.openClozes = new Set();
    cardState.totalClozes = 0;
    cardState.clozeToggleCount = 0;
    cardState.quizAttempted = false;
    cardState.quizFirstTryCorrect = false;
    cardState.quizSelectedIdx = null;
    cardState.diagramOpen = false;
    cardState.selectedGalaxyNodeId = null;
    cardState.selectedGalaxyEdgeId = null;
    if (node && node.node_type === 'structure_cloze') {
      let count = 0;
      for (const b of node.payload?.bullets || []) {
        const matches = String(b).match(/\{\{.+?\}\}/g);
        if (matches) count += matches.length;
      }
      cardState.totalClozes = count;
    }
  }

  function ensureActiveSession() {
    const step = R.getCurrentStep(progress);
    if (step) return;
    // Check if there are reviewed nodes and queue finished -> default to frontier if unvisited exist, else cluster review
    const hasUnvisited = graph.nodes.some(
      n => n.syllabus_tier !== 'understand' && R.getNodeState(progress, n.id).status === 'unvisited'
    );
    const plan = hasUnvisited
      ? R.planUnvisitedFrontier(graph, progress, { nowMs: Date.now() })
      : R.planClusterReview(graph, progress, Date.now());
    if (plan.queue.length > 0) {
      progress = R.startSession(progress, plan, Date.now());
      persistProgress();
    }
  }

  function updateImplicitHint() {
    const idx = R.indexGraph(graph);
    const node = idx.nodeMap.get(cardState.nodeId);
    const rec = R.recommendImplicitRating(node, {
      stepsRevealed: cardState.stepsRevealed,
      dwellMs: Date.now() - cardState.enteredAt,
      totalClozes: cardState.totalClozes,
      revealedClozes: cardState.openClozes.size,
      clozeToggleCount: cardState.clozeToggleCount,
      quizAttempted: cardState.quizAttempted,
      quizFirstTryCorrect: cardState.quizFirstTryCorrect,
    });
    for (const btn of document.querySelectorAll('#rating-bar .rate-btn')) {
      btn.classList.toggle('recommended', btn.dataset.rating === rec);
    }
  }

  function renderClozeBullet(text, bulletIdx) {
    let localIdx = 0;
    const segments = String(text ?? '').split(/(\{\{.+?\}\})/g);
    return segments.map(seg => {
      if (seg.startsWith('{{') && seg.endsWith('}}') && seg.length > 4) {
        const inner = seg.slice(2, -2);
        const cid = `${bulletIdx}_${localIdx++}`;
        const isOpen = cardState.openClozes.has(cid);
        return `<button type="button" class="cloze-pill ${isOpen ? 'open' : ''}" data-act="toggle-cloze" data-cid="${cid}" aria-expanded="${isOpen}">${esc(stripMd(inner))}</button>`;
      }
      return renderRichText(seg, { mode: 'cloze_outside', maxSpans: 1 });
    }).join('');
  }

  function renderQuizBlock(quiz, labelPrefix = '🎯 高阶反转诊断') {
    if (!quiz || !quiz.stem) return '';
    const opts = (quiz.options || []).map((opt, idx) => {
      let cls = 'quiz-opt';
      if (cardState.quizAttempted) {
        if (idx === quiz.answer_idx) cls += ' correct';
        else if (idx === cardState.quizSelectedIdx) cls += ' wrong';
      }
      return `<button type="button" class="${cls}" data-act="answer-quiz" data-idx="${idx}">${String.fromCharCode(65 + idx)}. ${esc(stripMd(opt))}</button>`;
    }).join('');
    const rescueTip = cardState.quizAttempted && !cardState.quizFirstTryCorrect && quiz.remedial_node_id
      ? '<div class="quiz-rescue-tip">🧭 已定位前置根因节点：点下方 🔴 可自动触发诊断补漏岔路</div>'
      : '';
    return `
      <div class="quiz-card">
        <p class="quiz-stem">${labelPrefix}：${renderRichText(quiz.stem, { mode: 'stem' })}</p>
        <div class="quiz-options">${opts}</div>
        ${cardState.quizAttempted ? `<div class="quiz-explain"><strong>解析：</strong>${renderRichText(quiz.explain)}${rescueTip}</div>` : ''}
      </div>
    `;
  }

  function renderDiagramAssets(node) {
    const assets = Array.isArray(node.diagram_assets) ? node.diagram_assets : [];
    if (!assets.length) return '';
    const figures = assets.map(a => `
      <figure class="diagram-figure">
        <button type="button" class="diagram-thumb-btn" data-act="open-diagram" data-src="${esc(a.path)}" data-cap="${esc(a.caption)}">
          <img src="./${esc(a.path)}" alt="${esc(a.caption)}" loading="lazy">
          <span class="diagram-zoom-hint">🔍 点击放大原图</span>
        </button>
        <figcaption>${esc(a.caption)}${a.source_page ? ` · P${esc(a.source_page)}` : ''}</figcaption>
      </figure>
    `).join('');
    return `
      <details class="diagram-drawer"${cardState.diagramOpen ? ' open' : ''}>
        <summary>🖼️ 查看原课件机理图 (${assets.length} 张 · 点击展开对照)</summary>
        <div class="diagram-grid">${figures}</div>
      </details>
    `;
  }

  function renderNodeBody(node) {
    let html = '';
    if (node.node_type === 'mechanism_flow') {
      const steps = node.payload.steps || [];
      const shown = Math.min(cardState.stepsRevealed, steps.length);
      const stepCards = steps.slice(0, shown).map((st, i) => `
        <div class="flow-step revealed">
          <div class="flow-step-head">
            <span>第 ${i + 1} 步 · ${esc(st.cue)}</span>
            ${st.mechanism_tag ? `<span class="flow-tag">${esc(st.mechanism_tag)}</span>` : ''}
          </div>
          <p class="flow-reveal-text">${renderRichText(st.reveal)}</p>
        </div>
      `).join('');

      const nextBtn = shown < steps.length
        ? `<button type="button" class="flow-next-btn" data-act="next-flow-step">👇 推演第 ${shown + 1}/${steps.length} 步：${esc(stripMd(steps[shown].cue))}</button>`
        : `<div class="takeaway-card"><strong>💡 核心结论：</strong>${renderRichText(node.payload.takeaway)}</div>`;

      const expandAll = shown < steps.length
        ? `<div class="cloze-toolbar"><button type="button" class="mini-btn" data-act="expand-all-flow">展开全部 ${steps.length} 步</button></div>`
        : '';

      html = `${expandAll}<div class="flow-list">${stepCards}${nextBtn}</div>`;
      if (node.payload.quiz) html += renderQuizBlock(node.payload.quiz, '🎯 机制反转诊断');
    } else if (node.node_type === 'structure_cloze') {
      const bullets = (node.payload.bullets || []).map((b, i) => `<li>${renderClozeBullet(b, i)}</li>`).join('');
      const allOpen = cardState.totalClozes > 0 && cardState.openClozes.size >= cardState.totalClozes;
      html = `
        <div class="cloze-toolbar">
          <button type="button" class="mini-btn" data-act="toggle-all-cloze">${allOpen ? '全部遮罩练习' : '全部翻开核对'}</button>
        </div>
        <ul class="cloze-list">${bullets}</ul>
        ${node.payload.trap_note ? `<div class="trap-box"><strong>⚠️ 临床陷阱 / 易混点：</strong>${renderRichText(node.payload.trap_note)}</div>` : ''}
      `;
      if (node.payload.quiz) html += renderQuizBlock(node.payload.quiz, '🎯 结构功能反转测');
    } else if (node.node_type === 'comparison_matrix') {
      const cols = (node.payload.columns || []).map(c => `<th>${esc(stripMd(c))}</th>`).join('');
      const rows = (node.payload.rows || []).map(row => `<tr>${row.map(cell => `<td>${renderRichText(cell, { mode: 'matrix_cell', maxSpans: 2 })}</td>`).join('')}</tr>`).join('');
      html = `
        <div class="matrix-wrap"><table class="matrix-table"><thead><tr>${cols}</tr></thead><tbody>${rows}</tbody></table></div>
        ${renderQuizBlock(node.payload.quiz, '⚡ 秒测')}
      `;
    }
    return html + renderDiagramAssets(node);
  }

  function renderReconnectAndBridge(resumed, curNode) {
    const crumbsHtml = resumed.breadcrumbs.length > 0
      ? `<div class="breadcrumb-bar" aria-label="最近学习轨迹">
          ${resumed.breadcrumbs.map((b, i) => `
            ${i > 0 ? '<span class="crumb-arrow">→</span>' : ''}
            <button type="button" class="crumb-chip" data-act="jump-detour" data-id="${esc(b.node_id)}" title="${esc(b.title)}">
              <span>${R.STATUS_META[b.status]?.dot || '⚪'}</span>
              <span>${esc(b.title.slice(0, 12))}${b.title.length > 12 ? '…' : ''}</span>
            </button>
          `).join('')}
          ${curNode ? `<span class="crumb-arrow">→</span><span class="crumb-chip current">📍 ${esc(curNode.title.slice(0, 12))}${curNode.title.length > 12 ? '…' : ''}</span>` : ''}
        </div>`
      : '';

    const bridgeHtml = resumed.recap?.bridge_text
      ? `<div class="bridge-box">
           <span class="bridge-tag">${esc(resumed.recap.relation_label || '逻辑衔接')}</span>
           <span>${renderRichText(resumed.recap.bridge_text, { maxSpans: 2 })}</span>
         </div>`
      : '';

    const recapHtml = resumed.recap?.prev_anchor
      ? `<details class="recap-details" ${resumed.recap.idle_minutes >= 20 ? 'open' : ''}>
           <summary>⏱️ 10秒前情提要（接续自「${esc(resumed.recap.prev_title)}」）</summary>
           <p>${renderRichText(resumed.recap.prev_anchor, { maxSpans: 2 })}</p>
         </details>`
      : '';

    $('reconnect-strip').innerHTML = crumbsHtml + bridgeHtml + recapHtml;
  }

  function scrollToStageTop() {
    window.scrollTo(0, 0);
    document.documentElement.scrollTop = 0;
    document.body.scrollTop = 0;
  }

  function renderStage(opts = {}) {
    const idx = R.indexGraph(graph);
    const curStep = R.getCurrentStep(progress);
    const resumed = R.resumeSession(graph, progress, Date.now());
    const curNode = curStep ? idx.nodeMap.get(curStep.node_id) : null;
    const prevNodeId = cardState.nodeId;
    const nextNodeId = curNode ? curNode.id : null;
    const nodeChanged = Boolean(prevNodeId && nextNodeId !== prevNodeId);

    if (curNode && cardState.nodeId !== curNode.id) {
      resetCardInteraction(curNode);
    } else if (!curNode) {
      cardState.nodeId = null;
    }

    // Mode pill & detour banner
    const modeLabel = progress.session.mode === 'cluster_review'
      ? '🔁 邻域复习'
      : (progress.session.active_thread_id ? '🧭 专题主线' : '✨ 智能开荒');
    $('btn-mode-pill').textContent = modeLabel;

    if (resumed.in_detour) {
      $('detour-banner').classList.remove('hidden');
      const prefix = resumed.is_planned_detour ? '🤖 算法补漏岔路' : `🌿 岔路探索 (${resumed.detour_depth}层)`;
      $('detour-label').textContent = `${prefix} · 原断点「${resumed.mainline_progress.return_node_title || '主干'}」`;
    } else {
      $('detour-banner').classList.add('hidden');
    }

    if (!curNode) {
      $('reconnect-strip').innerHTML = '';
      $('inline-galaxy-section').innerHTML = '';
      $('inline-galaxy-section').classList.add('hidden');
      $('card-stage').innerHTML = `
        <div class="card-meta-row"><span class="subj-badge">🎉 本段逻辑线已完成</span></div>
        <h1 class="card-title">这一串知识点已走完！</h1>
        <p class="anchor-summary">系统已持久化记录每个节点的掌握度。你可以趁热用“邻域连坐”扫除标红/标黄点，或继续大纲高权重开荒。</p>
        <div class="btn-row">
          <button type="button" class="primary-btn" data-act="start-cluster">🔁 启动邻域连坐复习</button>
          <button type="button" class="mini-btn" data-act="start-frontier">✨ 规划下一条未学开荒线</button>
        </div>
      `;
      $('btn-jump-galaxy').textContent = '🧭 邻域星系 (0)';
      $('queue-progress').textContent = '已完成本轮';
      $('rating-bar').classList.add('hidden');
      if (nodeChanged || opts.scrollTop) scrollToStageTop();
      return;
    }

    $('rating-bar').classList.remove('hidden');
    $('inline-galaxy-section').classList.remove('hidden');
    renderReconnectAndBridge(resumed, curNode);
    if (resumed.is_planned_detour && resumed.detour_reason) {
      $('reconnect-strip').insertAdjacentHTML(
        'beforeend',
        `<div class="planned-detour-pill">🤖 ${esc(resumed.detour_reason)}</div>`
      );
    }

    const curState = R.getNodeState(progress, curNode.id);
    const hasOptQuiz = Boolean(curNode.payload?.quiz) && curNode.node_type !== 'comparison_matrix';
    const typeLabel = ({
      mechanism_flow: '🔄 递进推演流',
      structure_cloze: '💊 胶囊遮罩卡',
      comparison_matrix: '⚖️ 对比表 + 秒测',
    }[curNode.node_type] || '') + (hasOptQuiz ? ' + 🎯反转测' : '');

    const kwOn = settings.highlightKeywords !== false;
    $('card-stage').innerHTML = `
      <div class="card-meta-row">
        <span class="subj-badge subj-${esc(curNode.subject)}">${esc(R.SUBJECT_LABELS[curNode.subject] || curNode.subject)}</span>
        <span class="tier-badge tier-${esc(curNode.syllabus_tier)}">${esc(R.TIER_LABELS[curNode.syllabus_tier] || curNode.syllabus_tier)}</span>
        <span>${R.STATUS_META[curState.status]?.dot || '⚪'}</span>
        <span class="card-meta-right">
          <span class="type-badge">${typeLabel}</span>
          <button type="button" class="kw-toggle-btn ${kwOn ? 'on' : ''}" data-act="toggle-keywords" aria-pressed="${kwOn}" title="切换非胶囊句关键词高亮">${kwOn ? '✨ 重点' : '纯文本'}</button>
        </span>
      </div>
      <h1 class="card-title">${esc(curNode.title)}</h1>
      <p class="anchor-summary">${renderRichText(curNode.anchor_summary, { maxSpans: 3 })}</p>
      ${renderNodeBody(curNode)}
      <div class="source-chip">📄 课件溯源：${esc((curNode.source_slides || []).join(' · ') || '教学大纲核心条目')}</div>
    `;

    // Progress indicator & inline full-title Galaxy navigation
    const qTotal = progress.session.active_queue?.length || 0;
    const qPos = Math.min(qTotal, (progress.session.cursor || 0) + 1);
    $('queue-progress').textContent = resumed.in_detour
      ? '🌿 岔路中（打卡或点上方返回）'
      : `主线第 ${qPos} / ${qTotal} 站`;

    const allNeighbors = R.getSortedNeighbors(graph, progress, curNode.id, Date.now());
    $('btn-jump-galaxy').textContent = `🧭 邻域星系 (${allNeighbors.length})`;
    $('inline-galaxy-section').innerHTML = `
      <div class="galaxy-section-head">
        <span class="galaxy-section-title">🧭 邻域星系 · 邻近节点逻辑拓扑</span>
        <span class="galaxy-section-sub">点选节点可先看衔接逻辑，再决定走岔路</span>
      </div>
      ${renderGalaxyBoard(curNode.id)}
    `;

    updateImplicitHint();
    if (nodeChanged || opts.scrollTop) {
      scrollToStageTop();
    }
  }

  function renderGalaxyBoard(centerId) {
    const layout = R.getGalaxyLayout(graph, progress, centerId, Date.now(), 6);
    if (!layout.center) return '<p>暂无中心节点</p>';
    const cy = layout.center.y;

    const linesSvg = layout.items.map(item => {
      const rel = item.edge.relation_type;
      const isCross = rel === 'cross_discipline';
      const isContrast = rel === 'contrast_with';
      const color = isCross ? '#d97706' : isContrast ? '#7c3aed' : (item.slot === 'upstream' ? '#64748b' : '#0f766e');
      return `<line x1="50" y1="${cy}" x2="${item.x}" y2="${item.y}" stroke="${color}" stroke-width="${isCross ? '0.85' : '0.6'}" ${isContrast ? 'stroke-dasharray="1.8 1.2"' : ''} />`;
    }).join('');

    const edgeTags = layout.items.map(item => {
      const rMeta = R.RELATION_META[item.edge.relation_type] || { icon: '🔗', label: '关联' };
      const mx = (50 + item.x) / 2;
      const my = (cy + item.y) / 2;
      return `<span class="galaxy-edge-tag" style="left:${mx}%;top:${my}%">${rMeta.icon} ${esc(rMeta.label)}</span>`;
    }).join('');

    const centerDot = R.STATUS_META[layout.center.status]?.dot || '⚪';
    const centerPill = `
      <div class="galaxy-node-pill center" style="left:50%;top:${cy}%">
        <div class="galaxy-node-top"><span>${centerDot}</span><span>${esc(R.SUBJECT_LABELS[layout.center.subject] || layout.center.subject)} · 当前</span></div>
        <div class="galaxy-node-title">${esc(layout.center.title)}</div>
      </div>
    `;

    const neighborPills = layout.items.map(item => {
      const sDot = R.STATUS_META[item.status]?.dot || '⚪';
      const subj = R.SUBJECT_LABELS[item.node.subject] || item.node.subject;
      const isSel = cardState.selectedGalaxyNodeId === item.node.id;
      return `
        <button type="button" class="galaxy-node-pill ${item.is_weak ? 'weak' : ''} ${isSel ? 'selected' : ''}"
          style="left:${item.x}%;top:${item.y}%"
          data-act="select-galaxy-node" data-id="${esc(item.node.id)}" data-edge="${esc(item.edge.id)}">
          <div class="galaxy-node-top"><span>${sDot}</span><span>${esc(subj)}</span></div>
          <div class="galaxy-node-title">${esc(item.node.title)}</div>
        </button>
      `;
    }).join('');

    // Preview bar for selected neighbor (defaults to top recommended neighbor if none tapped yet)
    const activePreviewItem = layout.items.find(i => i.node.id === cardState.selectedGalaxyNodeId)
      || layout.extra_neighbors.find(i => i.node.id === cardState.selectedGalaxyNodeId)
      || layout.items[0]
      || null;

    let previewHtml = '';
    if (activePreviewItem) {
      const rMeta = R.RELATION_META[activePreviewItem.edge.relation_type] || { icon: '🔗', label: '关联' };
      const sMeta = R.STATUS_META[activePreviewItem.status] || R.STATUS_META.unvisited;
      const isExplicit = cardState.selectedGalaxyNodeId === activePreviewItem.node.id;
      previewHtml = `
        <div class="galaxy-preview-bar">
          <div class="galaxy-preview-title">
            ${sMeta.dot} ${isExplicit ? '已选岔路：' : '推荐邻域：'}${esc(activePreviewItem.node.title)}
            <span style="font-size:11px;font-weight:600;color:#b45309">（${rMeta.icon} ${esc(rMeta.label)} · ${esc(sMeta.label)}）</span>
          </div>
          <div class="galaxy-preview-bridge">${renderRichText(activePreviewItem.edge.bridge_text, { maxSpans: 2 })}</div>
          <div class="galaxy-preview-actions">
            <button type="button" class="primary-btn" data-act="jump-detour" data-id="${esc(activePreviewItem.node.id)}" data-edge="${esc(activePreviewItem.edge.id)}">🌿 进入此岔路探索</button>
          </div>
        </div>
      `;
    }

    const extraHtml = layout.extra_neighbors.length
      ? `<div class="galaxy-extra-row"><span class="galaxy-section-sub" style="width:100%">其余关联点：</span>${layout.extra_neighbors.map(nb => `
          <button type="button" class="galaxy-extra-btn" data-act="select-galaxy-node" data-id="${esc(nb.node.id)}" data-edge="${esc(nb.edge.id)}">
            ${R.STATUS_META[nb.status]?.dot || '⚪'} ${esc(nb.node.title)}
          </button>`).join('')}</div>`
      : '';

    return `
      <div class="galaxy-board" style="height:${layout.height_px}px" role="region" aria-label="局部邻域拓扑图">
        <svg class="galaxy-lines-svg" viewBox="0 0 100 100" preserveAspectRatio="none">${linesSvg}</svg>
        ${edgeTags}
        ${centerPill}
        ${neighborPills}
      </div>
      ${previewHtml}
      ${extraHtml}
      <p style="font-size:11.5px;color:#64748b;margin:8px 0 0">上层=先修/结构基础 · 侧翼=跨学科/易混鉴别 · 下层=顺流机制/临床后果 · 再次点击已选节点可直接进入</p>
    `;
  }

  function renderAtlasModal() {
    const curStep = R.getCurrentStep(progress);
    const centerId = curStep?.node_id || graph.nodes[0]?.id;
    const sylList = R.getSyllabusProgress(graph, progress);

    const tabs = `
      <div class="tab-row">
        <button type="button" class="tab-btn ${atlasTab === 'galaxy' ? 'active' : ''}" data-act="atlas-tab" data-tab="galaxy">🧭 局部星系图</button>
        <button type="button" class="tab-btn ${atlasTab === 'syllabus' ? 'active' : ''}" data-act="atlas-tab" data-tab="syllabus">📊 大纲掌握热力</button>
      </div>
    `;

    let body = '';
    if (atlasTab === 'galaxy') {
      body = renderGalaxyBoard(centerId);
    } else {
      body = sylList.map(s => {
        const masteredNodes = s.nodes.filter(n => n.status === 'mastered').length;
        const pct = s.nodes.length ? Math.round((masteredNodes / s.nodes.length) * 100) : 0;
        const pills = s.nodes.map(n => `
          <button type="button" class="node-dot-pill" data-act="svg-jump" data-id="${esc(n.id)}">
            ${R.STATUS_META[n.status]?.dot || '⚪'} ${esc(n.title)}
          </button>
        `).join('');
        return `
          <div class="syl-subject-card">
            <div class="syl-head">
              <span>${esc(s.label)} (${masteredNodes}/${s.nodes.length} 节点通透)</span>
              <span style="font-size:11.5px;color:#64748b">覆盖考纲 ${s.covered_syllabus}/${s.total_syllabus}</span>
            </div>
            <div class="progress-track"><div class="progress-fill" style="width:${pct}%"></div></div>
            <div class="btn-row" style="margin:0 0 8px">
              <button type="button" class="mini-btn" data-act="frontier-subj" data-subj="${esc(s.subject)}">✨ 开荒本科</button>
              <button type="button" class="mini-btn" data-act="cluster-subj" data-subj="${esc(s.subject)}">🔁 复习本科</button>
            </div>
            <div class="node-dot-list">${pills || '<span style="font-size:12px;color:#94a3b8">该科待导入课件</span>'}</div>
          </div>
        `;
      }).join('');
    }

    openModal('图谱导航与大纲热力', tabs + body);
  }

  function renderModesModal() {
    const threadCards = (graph.threads || []).map(th => {
      const done = th.ordered_steps.filter(s => R.getNodeState(progress, s.node_id).status === 'mastered').length;
      return `
        <button type="button" class="route-card" data-act="pick-thread" data-id="${esc(th.id)}">
          <h3>🧭 ${esc(th.title)}（${done}/${th.ordered_steps.length} 通透）</h3>
          <p>${esc(th.description || '')}</p>
        </button>
      `;
    }).join('');

    const html = `
      <button type="button" class="route-card" data-act="start-frontier">
        <h3>✨ 智能大纲开荒（推荐新学）</h3>
        <p>优先挑选大纲“下划线掌握/重点掌握”未学节点，自动检查并前置缺失的先修概念，折叠“了解”枝节。</p>
      </button>
      <button type="button" class="route-card" data-act="start-cluster">
        <h3>🔁 邻域连坐簇复习（推荐碎片巩固）</h3>
        <p>根据遗忘衰减与 🔴/🟡 标记锁定位点，自动打包“1个上游引子 + 震中 + 薄弱邻近节点”连贯扫盲。</p>
      </button>
      <h3 style="font-size:13.5px;margin:14px 0 8px;color:#334155">跨学科 / 章节专题逻辑线</h3>
      ${threadCards}
    `;
    openModal('选择逻辑行进路线', html);
  }

  function renderSyncModal() {
    const canFSA = typeof window.showOpenFilePicker === 'function';
    const html = `
      <div class="syl-subject-card">
        <div class="syl-head"><span>☁️ GitHub Gist 手机-电脑双端云同步</span></div>
        <p style="font-size:12px;color:#64748b;margin:0 0 8px">无需自建服务器：用仅含 <code>gist</code> 权限的 GitHub Token 绑定私有 Gist，每次打卡自动跨端合并掌握度与断点。</p>
        <label class="form-field">GitHub Token (仅保存在本地浏览器)
          <input id="sync-token" type="password" value="${esc(settings.githubToken || '')}" placeholder="ghp_... 或 github_pat_...">
        </label>
        <label class="form-field">Gist ID（首次留空点同步将自动新建私有 Gist）
          <input id="sync-gist" type="text" value="${esc(settings.gistId || '')}" placeholder="留空自动创建">
        </label>
        <label class="form-field">远端图谱更新 URL（可选，用于手机拉取 PC 新编译课件）
          <input id="sync-graph-url" type="url" value="${esc(settings.remoteGraphUrl || '')}" placeholder="https://.../graph.json">
        </label>
        <div class="btn-row">
          <button type="button" class="primary-btn" data-act="gist-sync-now">☁️ 保存配置并立即同步</button>
          <button type="button" class="mini-btn" data-act="pull-remote-graph">⬇️ 拉取远端图谱</button>
        </div>
      </div>

      <div class="syl-subject-card">
        <div class="syl-head"><span>💾 本地文件持久化与备份</span></div>
        <div class="btn-row">
          ${canFSA ? '<button type="button" class="mini-btn" data-act="connect-local-progress">📂 绑定本地 progress.json</button>' : ''}
          <button type="button" class="mini-btn" data-act="export-progress">📤 导出学习进度 JSON</button>
          <button type="button" class="mini-btn" data-act="import-progress-btn">📥 导入进度 JSON</button>
          <button type="button" class="mini-btn" data-act="import-graph-btn">🧩 导入增量 graph.json</button>
        </div>
        <input id="file-import-progress" type="file" accept=".json" class="hidden">
        <input id="file-import-graph" type="file" accept=".json" class="hidden">
      </div>

      <div class="syl-subject-card">
        <div class="syl-head"><span>🤖 Antigravity + Gemini 每日增量入库</span></div>
        <p style="font-size:12px;color:#475569;margin:0">
          在工作区已安装 <code>medgraph-slide-ingest</code> Skill：只需把新课件直接发给 Antigravity 里的 Gemini，即可自动提取大纲匹配、增量合入 <code>graph.json</code> 并重新构建 PWA。
        </p>
      </div>
    `;
    openModal('云同步与每日课件导入', html);
  }

  function openModal(title, html) {
    $('modal-title').textContent = title;
    $('modal-body').innerHTML = html;
    if (!$('modal').open) $('modal').showModal();
  }

  function closeModal() {
    if ($('modal').open) $('modal').close();
  }

  function handleAction(target) {
    const act = target.dataset.act;
    if (!act) return;

    if (act === 'close-modal') {
      closeModal();
      return;
    }
    if (act === 'open-atlas') {
      atlasTab = 'galaxy';
      renderAtlasModal();
      return;
    }
    if (act === 'atlas-tab') {
      atlasTab = target.dataset.tab;
      renderAtlasModal();
      return;
    }
    if (act === 'open-modes') {
      renderModesModal();
      return;
    }
    if (act === 'open-sync') {
      renderSyncModal();
      return;
    }
    if (act === 'toggle-keywords') {
      settings.highlightKeywords = (settings.highlightKeywords === false);
      S.saveSettings(settings);
      renderStage();
      showToast(settings.highlightKeywords === false ? '已切换为纯文本无提示模式' : '已开启知识句关键词突出显示');
      return;
    }
    if (act === 'next-flow-step') {
      cardState.stepsRevealed += 1;
      renderStage();
      return;
    }
    if (act === 'expand-all-flow') {
      const node = R.indexGraph(graph).nodeMap.get(cardState.nodeId);
      cardState.stepsRevealed = node?.payload?.steps?.length || 1;
      renderStage();
      return;
    }
    if (act === 'toggle-cloze') {
      const cid = target.dataset.cid;
      if (cardState.openClozes.has(cid)) cardState.openClozes.delete(cid);
      else cardState.openClozes.add(cid);
      cardState.clozeToggleCount += 1;
      renderStage();
      return;
    }
    if (act === 'toggle-all-cloze') {
      const node = R.indexGraph(graph).nodeMap.get(cardState.nodeId);
      const allOpen = cardState.totalClozes > 0 && cardState.openClozes.size >= cardState.totalClozes;
      cardState.openClozes.clear();
      if (!allOpen && node) {
        (node.payload?.bullets || []).forEach((b, bi) => {
          const matches = String(b).match(/\{\{.+?\}\}/g) || [];
          matches.forEach((_, li) => cardState.openClozes.add(`${bi}_${li}`));
        });
      }
      renderStage();
      return;
    }
    if (act === 'answer-quiz') {
      const chosen = Number(target.dataset.idx);
      const node = R.indexGraph(graph).nodeMap.get(cardState.nodeId);
      const ans = node?.payload?.quiz?.answer_idx;
      if (!cardState.quizAttempted) {
        cardState.quizAttempted = true;
        cardState.quizFirstTryCorrect = (chosen === ans);
      }
      cardState.quizSelectedIdx = chosen;
      renderStage();
      return;
    }
    if (act === 'open-diagram') {
      const src = target.dataset.src;
      const cap = target.dataset.cap || '原课件机理图';
      if (!src) return;
      openModal('原课件机理与形态图', `
        <div class="diagram-lightbox">
          <img src="./${esc(src)}" alt="${esc(cap)}" class="diagram-zoom-img">
          <p class="diagram-lightbox-cap">${esc(cap)}</p>
        </div>
      `);
      return;
    }
    if (act === 'focus-galaxy') {
      $('inline-galaxy-section').scrollIntoView({ behavior: 'smooth', block: 'center' });
      return;
    }
    if (act === 'select-galaxy-node') {
      const id = target.dataset.id;
      const edgeId = target.dataset.edge || null;
      if (!id) return;
      // Second tap on an already-selected node enters detour directly
      if (cardState.selectedGalaxyNodeId === id) {
        progress = R.pushDetour(graph, progress, id, edgeId, Date.now());
        persistProgress();
        closeModal();
        renderStage();
        return;
      }
      cardState.selectedGalaxyNodeId = id;
      cardState.selectedGalaxyEdgeId = edgeId;
      if ($('modal').open && atlasTab === 'galaxy') renderAtlasModal();
      else renderStage();
      return;
    }
    if (act === 'rate') {
      const rating = target.dataset.rating;
      const curStep = R.getCurrentStep(progress);
      if (!curStep) return;
      const signals = {
        dwellMs: Date.now() - cardState.enteredAt,
        stepsRevealed: cardState.stepsRevealed,
        clozeRatio: cardState.totalClozes ? cardState.openClozes.size / cardState.totalClozes : null,
        quizFirstTryCorrect: cardState.quizAttempted ? cardState.quizFirstTryCorrect : null,
      };
      const originalCursor = progress.session.cursor || 0;
      const quizWrong = cardState.quizAttempted && !cardState.quizFirstTryCorrect;
      const diag = R.planDiagnosticDetour(graph, progress, curStep.node_id, rating, { quizWrong });
      progress = R.applyReview(graph, progress, curStep.node_id, rating, signals, Date.now());
      if (diag) {
        progress = R.pushDetour(graph, progress, diag.target_node_id, diag.via_edge_id, Date.now(), {
          planned: true,
          reason: diag.reason,
          overrideReturnNodeId: curStep.node_id,
          overrideReturnCursor: !curStep.in_detour ? originalCursor : undefined,
        });
        persistProgress();
        showToast(`🤖 已自动规划补漏岔路：先补「${diag.target_title}」`);
        renderStage({ scrollTop: true });
        return;
      }
      persistProgress();
      const label = R.STATUS_META[rating]?.label || rating;
      showToast(curStep.in_detour ? `已记录「${label}」· 可继续探邻或点上方返回主线` : `已记录「${label}」`);
      renderStage({ scrollTop: true });
      return;
    }
    if (act === 'jump-detour' || act === 'svg-jump') {
      const id = target.dataset.id;
      const edgeId = target.dataset.edge || null;
      if (!id || id === cardState.nodeId) return;
      progress = R.pushDetour(graph, progress, id, edgeId, Date.now());
      persistProgress();
      closeModal();
      renderStage();
      return;
    }
    if (act === 'pop-detour') {
      progress = R.popDetour(progress, Date.now());
      persistProgress();
      renderStage();
      return;
    }
    if (act === 'start-frontier' || act === 'frontier-subj') {
      const subj = target.dataset.subj || null;
      const plan = R.planUnvisitedFrontier(graph, progress, { nowMs: Date.now(), subject: subj });
      if (!plan.queue.length) {
        showToast('该范围内节点均已学过，改用邻域复习');
        const rev = R.planClusterReview(graph, progress, Date.now(), { subject: subj });
        progress = R.startSession(progress, rev, Date.now());
      } else {
        progress = R.startSession(progress, plan, Date.now());
        showToast(plan.rationale);
      }
      persistProgress();
      closeModal();
      renderStage();
      return;
    }
    if (act === 'start-cluster' || act === 'cluster-subj') {
      const subj = target.dataset.subj || null;
      const plan = R.planClusterReview(graph, progress, Date.now(), { subject: subj });
      progress = R.startSession(progress, plan, Date.now());
      persistProgress();
      closeModal();
      showToast(plan.rationale);
      renderStage();
      return;
    }
    if (act === 'pick-thread') {
      const tid = target.dataset.id;
      const plan = R.planUnvisitedFrontier(graph, progress, { nowMs: Date.now(), threadId: tid });
      progress = R.startSession(progress, plan, Date.now());
      persistProgress();
      closeModal();
      showToast(plan.rationale);
      renderStage();
      return;
    }
    if (act === 'gist-sync-now') {
      settings.githubToken = $('sync-token').value.trim();
      settings.gistId = $('sync-gist').value.trim();
      settings.remoteGraphUrl = $('sync-graph-url').value.trim();
      S.saveSettings(settings);
      S.syncWithGist(settings, progress)
        .then(res => {
          settings.gistId = res.gistId;
          S.saveSettings(settings);
          progress = res.progress;
          S.saveLocalProgress(progress);
          $('sync-gist').value = res.gistId;
          showToast(res.action === 'created' ? `已新建私有 Gist (${res.gistId.slice(0, 8)}…) 并同步` : '云端双向合并同步成功');
          renderStage();
        })
        .catch(err => showToast(err.message));
      return;
    }
    if (act === 'pull-remote-graph') {
      const url = $('sync-graph-url').value.trim();
      if (!url) { showToast('请先填写远端 graph.json URL'); return; }
      settings.remoteGraphUrl = url;
      S.saveSettings(settings);
      S.fetchRemoteGraph(url)
        .then(remoteGraph => {
          graph = remoteGraph;
          S.saveCachedGraphOverride(graph);
          showToast(`图谱已更新：共 ${graph.nodes.length} 概念节点，原有掌握度完好保留`);
          renderStage();
        })
        .catch(err => showToast(err.message));
      return;
    }
    if (act === 'connect-local-progress') {
      window.showSaveFilePicker({
        suggestedName: 'medgraph_progress.json',
        types: [{ description: 'JSON', accept: { 'application/json': ['.json'] } }],
      }).then(async h => {
        fileHandle = h;
        await writeLocalFile(progress);
        showToast('已绑定本地进度文件，打卡自动保存');
      }).catch(() => {});
      return;
    }
    if (act === 'export-progress') {
      const blob = new Blob([JSON.stringify(progress, null, 2)], { type: 'application/json' });
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = `medgraph_progress_${new Date().toISOString().slice(0, 10)}.json`;
      a.click();
      setTimeout(() => URL.revokeObjectURL(url), 10000);
      return;
    }
    if (act === 'import-progress-btn') {
      $('file-import-progress').click();
      return;
    }
    if (act === 'import-graph-btn') {
      $('file-import-graph').click();
    }
  }

  function wireFileInputs() {
    document.addEventListener('change', e => {
      if (e.target.id === 'file-import-progress' && e.target.files[0]) {
        e.target.files[0].text().then(txt => {
          const parsed = JSON.parse(txt);
          progress = S.mergeProgress(progress, parsed);
          persistProgress();
          closeModal();
          renderStage();
          showToast('已合并导入的学习进度');
        }).catch(() => showToast('进度 JSON 格式无效'));
      }
      if (e.target.id === 'file-import-graph' && e.target.files[0]) {
        e.target.files[0].text().then(txt => {
          const parsed = JSON.parse(txt);
          if (parsed.schema_version !== 1 || !Array.isArray(parsed.nodes)) throw new Error();
          graph = parsed;
          S.saveCachedGraphOverride(graph);
          closeModal();
          renderStage();
          showToast(`图谱已升级至 ${graph.nodes.length} 个概念节点`);
        }).catch(() => showToast('图谱 JSON 格式无效'));
      }
    });
  }

  async function boot() {
    graph = S.loadCachedGraphOverride();
    if (!graph && location.protocol.startsWith('http')) {
      try {
        const res = await fetch('./graph.json', { cache: 'no-store' });
        if (res.ok) graph = await res.json();
      } catch {
        // Offline fallback to embedded seed JSON
      }
    }
    if (!graph) {
      graph = loadInitialGraph();
    }
    progress = S.loadLocalProgress(() => R.createEmptyProgress(Date.now()));
    ensureActiveSession();
    renderStage();

    document.addEventListener('click', e => {
      const target = e.target.closest('[data-act]');
      if (target) handleAction(target);
    });
    document.addEventListener('toggle', e => {
      if (e.target && e.target.isConnected && e.target.classList?.contains('diagram-drawer')) {
        cardState.diagramOpen = Boolean(e.target.open);
      }
    }, true);
    wireFileInputs();

    if ('serviceWorker' in navigator && location.protocol.startsWith('http')) {
      navigator.serviceWorker.register('./sw.js').catch(() => {});
    }

    window.MedApp = {
      getGraph: () => graph,
      getProgress: () => progress,
      resetProgress: () => {
        progress = R.createEmptyProgress(Date.now());
        ensureActiveSession();
        persistProgress();
        renderStage();
      },
    };
  }

  boot();
})();
