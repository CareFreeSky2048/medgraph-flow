/* MedGraph Flow - Deterministic Graph Routing & Memory Engine (Node + Browser UMD) */
(function (root, factory) {
  if (typeof module === 'object' && module.exports) {
    module.exports = factory();
  } else {
    root.MedRouter = factory();
  }
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  const TIER_WEIGHT = {
    core_underlined: 3.5,
    master: 3.0,
    familiar: 2.0,
    understand: 1.0,
  };

  const TIER_LABELS = {
    core_underlined: '★ 大纲下划线/重点掌握',
    master: '掌握',
    familiar: '熟悉',
    understand: '了解（拓展）',
  };

  const SUBJECT_LABELS = {
    anatomy: '系统解剖',
    histology: '组胚学',
    embryology: '胚胎学',
    physiology: '生理学',
    biochem: '生物化学',
    pathophys: '病理生理',
    pathology: '病理学',
    pharmacology: '药理学',
  };

  const RELATION_META = {
    causal_next: { icon: '⬇️', label: '顺流推演' },
    prerequisite: { icon: '⬅️', label: '先修基础' },
    structural_basis: { icon: '🧬', label: '结构/分子基础' },
    clinical_manifestation: { icon: '🩺', label: '病理/临床表征' },
    contrast_with: { icon: '🔍', label: '易混鉴别' },
    cross_discipline: { icon: '⚡', label: '跨学科桥接' },
  };

  const STATUS_META = {
    unvisited: { dot: '⚪', label: '未学', color: '#94a3b8' },
    again: { dot: '🔴', label: '陌路/重来', color: '#e11d48' },
    fuzzy: { dot: '🟡', label: '模糊/需巩固', color: '#d97706' },
    mastered: { dot: '🟢', label: '通透', color: '#059669' },
  };

  const DAY_MS = 86400000;

  function clone(v) {
    return JSON.parse(JSON.stringify(v));
  }

  function indexGraph(graph) {
    const nodeMap = new Map();
    const edgeMap = new Map();
    const outEdges = new Map();
    const inEdges = new Map();
    const threadMap = new Map();
    const syllabusMap = new Map();

    for (const s of graph.syllabus || []) syllabusMap.set(s.id, s);
    for (const n of graph.nodes || []) {
      nodeMap.set(n.id, n);
      outEdges.set(n.id, []);
      inEdges.set(n.id, []);
    }
    for (const e of graph.edges || []) {
      edgeMap.set(e.id, e);
      if (outEdges.has(e.source)) outEdges.get(e.source).push(e);
      if (inEdges.has(e.target)) inEdges.get(e.target).push(e);
    }
    for (const t of graph.threads || []) threadMap.set(t.id, t);

    return { nodeMap, edgeMap, outEdges, inEdges, threadMap, syllabusMap };
  }

  function findConnectingEdge(idx, fromId, toId) {
    if (!fromId || !toId) return null;
    const direct = (idx.outEdges.get(fromId) || []).find(e => e.target === toId);
    if (direct) return direct;
    // contrast_with edges act symmetrically for bridging
    const reverseContrast = (idx.outEdges.get(toId) || []).find(
      e => e.target === fromId && e.relation_type === 'contrast_with'
    );
    return reverseContrast || null;
  }

  function createEmptyProgress(nowMs = Date.now()) {
    return {
      schema_version: 1,
      updated_at: new Date(nowMs).toISOString(),
      node_states: {},
      session: {
        mode: 'frontier',
        active_thread_id: null,
        active_queue: [], // [{ node_id, incoming_edge_id }]
        cursor: 0,
        detour_stack: [], // [{ return_node_id, return_cursor, return_queue, return_mode, return_thread_id, detour_node_id, via_edge_id }]
        recent_trail: [], // [{ node_id, edge_id, status, ts }]
        last_active_at: null,
      },
    };
  }

  function getNodeState(progress, nodeId) {
    const st = progress?.node_states?.[nodeId];
    return st || {
      status: 'unvisited',
      stability: 0,
      review_count: 0,
      lapse_count: 0,
      last_reviewed_at: null,
      implicit_signals: null,
    };
  }

  function computeRetrievability(nodeState, nowMs = Date.now()) {
    if (!nodeState || nodeState.status === 'unvisited' || !nodeState.last_reviewed_at) {
      return null;
    }
    const lastMs = Date.parse(nodeState.last_reviewed_at);
    if (!Number.isFinite(lastMs)) return 0.5;
    const dtDays = Math.max(0, (nowMs - lastMs) / DAY_MS);
    const stability = Math.max(0.25, Number(nodeState.stability) || 1.0);
    // FSRS power-law forgetting approximation: R(t) = (1 + t / (9 * S))^-1
    return 1 / (1 + dtDays / (9 * stability));
  }

  function computeUrgency(node, nodeState, nowMs = Date.now()) {
    const w = TIER_WEIGHT[node.syllabus_tier] || 2.0;
    const r = computeRetrievability(nodeState, nowMs);
    if (r === null) return 0;
    const statusPenalty =
      nodeState.status === 'again' ? 1.5 : nodeState.status === 'fuzzy' ? 0.7 : 0.0;
    return w * ((1 - r) + statusPenalty);
  }

  function recommendImplicitRating(node, interaction = {}) {
    if (!node) return 'fuzzy';
    if (node.node_type === 'comparison_matrix') {
      if (interaction.quizAttempted) {
        return interaction.quizFirstTryCorrect ? 'mastered' : 'again';
      }
      return 'fuzzy';
    }
    if (node.node_type === 'mechanism_flow') {
      const total = node.payload?.steps?.length || 1;
      const revealed = interaction.stepsRevealed || 1;
      if (revealed >= total && (interaction.dwellMs || 0) <= 45000) return 'mastered';
      return 'fuzzy';
    }
    if (node.node_type === 'structure_cloze') {
      const totalClozes = interaction.totalClozes || 1;
      const revealedClozes = interaction.revealedClozes || 0;
      if (revealedClozes >= totalClozes && (interaction.clozeToggleCount || 0) <= totalClozes + 2) {
        return 'mastered';
      }
      return 'fuzzy';
    }
    return 'fuzzy';
  }

  function getCurrentStep(progress) {
    const sess = progress?.session;
    if (!sess) return null;
    if (sess.detour_stack && sess.detour_stack.length > 0) {
      const top = sess.detour_stack[sess.detour_stack.length - 1];
      return {
        node_id: top.detour_node_id,
        incoming_edge_id: top.via_edge_id || null,
        in_detour: true,
        detour_depth: sess.detour_stack.length,
        is_planned_detour: Boolean(top.planned),
        detour_reason: top.reason || null,
      };
    }
    if (sess.active_queue && sess.cursor < sess.active_queue.length) {
      const item = sess.active_queue[sess.cursor];
      return {
        node_id: item.node_id,
        incoming_edge_id: item.incoming_edge_id || null,
        in_detour: false,
        detour_depth: 0,
        is_planned_detour: Boolean(item.is_planned_detour),
        detour_reason: item.detour_reason || null,
      };
    }
    return null;
  }

  function applyReview(graph, progress, nodeId, rating, implicitSignals = null, nowMs = Date.now()) {
    if (!['again', 'fuzzy', 'mastered'].includes(rating)) {
      throw new Error('Invalid review rating: ' + rating);
    }
    const next = clone(progress);
    const prev = getNodeState(next, nodeId);
    const prevS = Number(prev.stability) || 0;

    let stability;
    if (rating === 'again') {
      stability = prevS > 0 ? Math.max(0.4, prevS * 0.45) : 0.5;
    } else if (rating === 'fuzzy') {
      stability = prevS > 0 ? Math.max(1.2, prevS * 1.45) : 1.5;
    } else {
      stability = prevS > 0 ? Math.min(90, prevS * 2.4 + 1.0) : 4.0;
    }

    const isoNow = new Date(nowMs).toISOString();
    next.node_states[nodeId] = {
      status: rating,
      stability: Number(stability.toFixed(2)),
      review_count: (prev.review_count || 0) + 1,
      lapse_count: (prev.lapse_count || 0) + (rating === 'again' ? 1 : 0),
      last_reviewed_at: isoNow,
      implicit_signals: implicitSignals || prev.implicit_signals || null,
    };

    const curStep = getCurrentStep(next);
    const idx = indexGraph(graph);
    let trailEdgeId = curStep && curStep.node_id === nodeId ? curStep.incoming_edge_id : null;
    if (!trailEdgeId && next.session.recent_trail.length > 0) {
      const prevTrailId = next.session.recent_trail[next.session.recent_trail.length - 1].node_id;
      trailEdgeId = findConnectingEdge(idx, prevTrailId, nodeId)?.id || null;
    }

    next.session.recent_trail.push({
      node_id: nodeId,
      edge_id: trailEdgeId,
      status: rating,
      ts: isoNow,
    });
    if (next.session.recent_trail.length > 24) {
      next.session.recent_trail = next.session.recent_trail.slice(-24);
    }

    // Advance mainline cursor only if not inside a detour stack
    if (
      (!next.session.detour_stack || next.session.detour_stack.length === 0) &&
      next.session.active_queue &&
      next.session.cursor < next.session.active_queue.length &&
      next.session.active_queue[next.session.cursor].node_id === nodeId
    ) {
      next.session.cursor += 1;
    }

    next.session.last_active_at = isoNow;
    next.updated_at = isoNow;
    return next;
  }

  function prependPrerequisites(idx, progress, queue, maxPrepends = 2) {
    const result = [...queue];
    const inQueue = new Set(result.map(s => s.node_id));
    let added = 0;

    // Inspect prerequisites of the first few planned nodes so learners don't hit walls
    for (let i = 0; i < Math.min(3, result.length) && added < maxPrepends; i++) {
      const targetId = result[i].node_id;
      const incoming = idx.inEdges.get(targetId) || [];
      const prereqEdges = incoming
        .filter(e => e.relation_type === 'prerequisite' || (e.relation_type === 'structural_basis' && (e.strength || 0.8) >= 0.9))
        .sort((a, b) => (b.strength || 0.8) - (a.strength || 0.8));

      for (const pe of prereqEdges) {
        if (added >= maxPrepends) break;
        const pNode = idx.nodeMap.get(pe.source);
        if (!pNode || inQueue.has(pNode.id)) continue;
        const pState = getNodeState(progress, pNode.id);
        if (pState.status === 'unvisited' || pState.status === 'again') {
          result.splice(i, 0, { node_id: pNode.id, incoming_edge_id: null, gated_for: targetId });
          // Update the gated node's incoming edge so the transition bridge_text displays!
          result[i + 1] = { ...result[i + 1], incoming_edge_id: pe.id };
          inQueue.add(pNode.id);
          added++;
          i++;
        }
      }
    }
    return result;
  }

  function weavePlannedDetours(idx, progress, queue, maxWeaves = 1) {
    if (!queue.length || maxWeaves <= 0) return queue;
    const result = [...queue];
    const inQueue = new Set(result.map(s => s.node_id));

    const allCandidates = [];
    for (let i = 0; i < result.length; i++) {
      const step = result[i];
      if (step.is_planned_detour || step.gated_for) continue;
      const links = [];
      for (const e of idx.outEdges.get(step.node_id) || []) {
        if (['cross_discipline', 'contrast_with'].includes(e.relation_type)) links.push({ nbId: e.target, edge: e });
      }
      for (const e of idx.inEdges.get(step.node_id) || []) {
        if (['cross_discipline', 'contrast_with'].includes(e.relation_type)) links.push({ nbId: e.source, edge: e });
      }
      for (const c of links) {
        const nb = idx.nodeMap.get(c.nbId);
        if (!nb || inQueue.has(nb.id) || nb.syllabus_tier === 'understand') continue;
        const st = getNodeState(progress, nb.id);
        if (st.status === 'mastered') continue;
        const weakBoost = st.status === 'again' ? 4 : st.status === 'fuzzy' ? 2.5 : 0;
        const tierBoost = TIER_WEIGHT[nb.syllabus_tier] || 2;
        const score = weakBoost + tierBoost + (c.edge.relation_type === 'cross_discipline' ? 1 : 0);
        if (score >= 3.5) {
          allCandidates.push({ ...c, node: nb, parentId: step.node_id, insertAfterId: step.node_id, score });
        }
      }
    }

    allCandidates.sort((a, b) => b.score - a.score);
    let woven = 0;
    for (const pick of allCandidates) {
      if (woven >= maxWeaves) break;
      if (inQueue.has(pick.node.id)) continue;
      const idxPos = result.findIndex(s => s.node_id === pick.insertAfterId);
      if (idxPos === -1) continue;
      const rLabel = RELATION_META[pick.edge.relation_type]?.label || '关联';
      result.splice(idxPos + 1, 0, {
        node_id: pick.node.id,
        incoming_edge_id: pick.edge.id,
        is_planned_detour: true,
        planned_detour_parent: pick.parentId,
        detour_reason: `算法规划顺道岔路（${rLabel}）：从「${idx.nodeMap.get(pick.parentId)?.title || ''}」顺手打通，完后自动续接主线`,
      });
      inQueue.add(pick.node.id);
      woven++;
    }
    return result;
  }

  function planUnvisitedFrontier(graph, progress, options = {}) {
    const idx = indexGraph(graph);
    const nowMs = options.nowMs || Date.now();
    const limit = options.limit || 7;
    const subjectFilter = options.subject || null;
    const threadId = options.threadId || null;
    const includeUnderstand = Boolean(options.includeUnderstand);
    const autoDetour = options.autoDetour !== false;

    const finalizeQueue = rawQueue => {
      const gated = prependPrerequisites(idx, progress, rawQueue);
      return autoDetour ? weavePlannedDetours(idx, progress, gated, 1) : gated;
    };

    // Case 1: Specific thread explicitly requested
    if (threadId && idx.threadMap.has(threadId)) {
      const th = idx.threadMap.get(threadId);
      let steps = th.ordered_steps
        .filter(s => {
          const st = getNodeState(progress, s.node_id);
          return st.status !== 'mastered';
        })
        .map(s => ({ node_id: s.node_id, incoming_edge_id: s.incoming_edge_id || null }));
      if (!steps.length) {
        steps = th.ordered_steps.map(s => ({ node_id: s.node_id, incoming_edge_id: s.incoming_edge_id || null }));
      }
      const clean = steps.slice(0, limit).map((step, i, arr) => ({
        node_id: step.node_id,
        incoming_edge_id: i === 0 ? null : (findConnectingEdge(idx, arr[i - 1].node_id, step.node_id)?.id || null),
      }));
      return {
        mode: 'frontier',
        thread_id: th.id,
        rationale: `按主干逻辑线「${th.title}」推进未巩固/未学节点`,
        queue: finalizeQueue(clean),
      };
    }

    // Case 2: Automatic Frontier discovery weighted by syllabus tier + causal out-degree + recent slide bonus
    let candidates = (graph.nodes || []).filter(n => {
      if (subjectFilter && n.subject !== subjectFilter) return false;
      const st = getNodeState(progress, n.id);
      if (st.status !== 'unvisited') return false;
      if (!includeUnderstand && n.syllabus_tier === 'understand') return false;
      return true;
    });

    // Fallback to understand-tier if all higher-tier nodes are already visited
    if (!candidates.length) {
      candidates = (graph.nodes || []).filter(n => {
        if (subjectFilter && n.subject !== subjectFilter) return false;
        return getNodeState(progress, n.id).status === 'unvisited';
      });
    }

    if (!candidates.length) {
      return { mode: 'frontier', thread_id: null, rationale: '当前筛选范围内所有节点均已涉猎，自动转为邻域复习', queue: [] };
    }

    const scored = candidates.map(n => {
      const w = TIER_WEIGHT[n.syllabus_tier] || 2.0;
      const outCausal = (idx.outEdges.get(n.id) || []).filter(e =>
        ['causal_next', 'clinical_manifestation', 'cross_discipline'].includes(e.relation_type)
      ).length;
      const addedMs = Date.parse(n.added_at || '');
      const recentBonus = Number.isFinite(addedMs) && (nowMs - addedMs) <= 2 * DAY_MS ? 0.8 : 0;
      return { node: n, score: w + 0.5 * outCausal + recentBonus };
    });
    scored.sort((a, b) => b.score - a.score);
    const seed = scored[0].node;

    // Prefer an unvisited contiguous slice from a LogicThread containing the top-scored seed
    for (const th of graph.threads || []) {
      const pos = th.ordered_steps.findIndex(s => s.node_id === seed.id);
      if (pos !== -1) {
        const slice = [];
        for (let i = pos; i < th.ordered_steps.length && slice.length < limit; i++) {
          const step = th.ordered_steps[i];
          const nd = idx.nodeMap.get(step.node_id);
          const st = getNodeState(progress, step.node_id);
          if (!nd || (subjectFilter && nd.subject !== subjectFilter)) continue;
          if (!includeUnderstand && nd.syllabus_tier === 'understand') continue;
          if (st.status === 'unvisited' || st.status === 'again') {
            slice.push({
              node_id: nd.id,
              incoming_edge_id: slice.length === 0 ? null : (findConnectingEdge(idx, slice[slice.length - 1].node_id, nd.id)?.id || null),
            });
          }
        }
        if (slice.length >= 3) {
          return {
            mode: 'frontier',
            thread_id: th.id,
            rationale: `大纲高权重「${seed.title}」领衔 · 沿主线连续开荒 ${slice.length} 站`,
            queue: finalizeQueue(slice),
          };
        }
      }
    }

    // Directed greedy beam walk if no matching multi-step thread slice
    const queue = [{ node_id: seed.id, incoming_edge_id: null }];
    const visitedInWalk = new Set([seed.id]);
    let cur = seed;

    while (queue.length < limit) {
      const nextEdges = (idx.outEdges.get(cur.id) || [])
        .filter(e => {
          const target = idx.nodeMap.get(e.target);
          if (!target || visitedInWalk.has(target.id)) return false;
          if (subjectFilter && target.subject !== subjectFilter) return false;
          if (!includeUnderstand && target.syllabus_tier === 'understand') return false;
          return getNodeState(progress, target.id).status === 'unvisited';
        })
        .sort((a, b) => {
          const prio = r => (r === 'causal_next' ? 3 : r === 'clinical_manifestation' ? 2 : r === 'cross_discipline' ? 1.5 : 1);
          const ta = idx.nodeMap.get(a.target);
          const tb = idx.nodeMap.get(b.target);
          const sa = prio(a.relation_type) + (TIER_WEIGHT[ta.syllabus_tier] || 2);
          const sb = prio(b.relation_type) + (TIER_WEIGHT[tb.syllabus_tier] || 2);
          return sb - sa;
        });

      if (!nextEdges.length) break;
      const chosen = nextEdges[0];
      queue.push({ node_id: chosen.target, incoming_edge_id: chosen.id });
      visitedInWalk.add(chosen.target);
      cur = idx.nodeMap.get(chosen.target);
    }

    return {
      mode: 'frontier',
      thread_id: null,
      rationale: `大纲优先开荒：从「${seed.title}」沿因果/跨学科脉络规划 ${queue.length} 站`,
      queue: finalizeQueue(queue),
    };
  }

  function planClusterReview(graph, progress, nowMs = Date.now(), options = {}) {
    const idx = indexGraph(graph);
    const limit = options.limit || 5;
    const subjectFilter = options.subject || null;

    const reviewed = (graph.nodes || []).filter(n => {
      if (subjectFilter && n.subject !== subjectFilter) return false;
      return getNodeState(progress, n.id).status !== 'unvisited';
    });

    if (!reviewed.length) {
      return planUnvisitedFrontier(graph, progress, { ...options, nowMs });
    }

    const ranked = reviewed
      .map(n => ({ node: n, urgency: computeUrgency(n, getNodeState(progress, n.id), nowMs) }))
      .sort((a, b) => b.urgency - a.urgency);

    const epicenter = ranked[0].node;
    const selectedIds = new Set([epicenter.id]);
    const ordered = [];

    // 1. Pull 1 upstream parent node as logical context primer
    const upstreamCandidates = (idx.inEdges.get(epicenter.id) || [])
      .filter(e => ['causal_next', 'structural_basis', 'prerequisite'].includes(e.relation_type))
      .sort((a, b) => (b.strength || 0.8) - (a.strength || 0.8));

    let primerEdge = null;
    if (upstreamCandidates.length > 0) {
      primerEdge = upstreamCandidates[0];
      ordered.push({ node_id: primerEdge.source, incoming_edge_id: null, role: 'primer' });
      selectedIds.add(primerEdge.source);
      ordered.push({ node_id: epicenter.id, incoming_edge_id: primerEdge.id, role: 'epicenter' });
    } else {
      ordered.push({ node_id: epicenter.id, incoming_edge_id: null, role: 'epicenter' });
    }

    // 2. Collect 1-hop neighbors prioritizing weak/forgotten nodes and cross-discipline/contrast links
    const neighborLinks = [];
    for (const e of idx.outEdges.get(epicenter.id) || []) {
      neighborLinks.push({ neighborId: e.target, edge: e, dir: 'out' });
    }
    for (const e of idx.inEdges.get(epicenter.id) || []) {
      neighborLinks.push({ neighborId: e.source, edge: e, dir: 'in' });
    }

    const scoredNeighbors = [];
    for (const link of neighborLinks) {
      const nb = idx.nodeMap.get(link.neighborId);
      if (!nb || selectedIds.has(nb.id)) continue;
      const st = getNodeState(progress, nb.id);
      let priority = st.status === 'unvisited'
        ? (nb.syllabus_tier === 'understand' ? 0.3 : (TIER_WEIGHT[nb.syllabus_tier] || 2) * 0.55)
        : computeUrgency(nb, st, nowMs) + 1.0;

      if (link.edge.relation_type === 'cross_discipline' || link.edge.relation_type === 'contrast_with') {
        priority += 0.6;
      }
      scoredNeighbors.push({ node: nb, edge: link.edge, dir: link.dir, priority });
    }
    scoredNeighbors.sort((a, b) => b.priority - a.priority);

    let lastId = epicenter.id;
    for (const item of scoredNeighbors) {
      if (ordered.length >= limit) break;
      if (selectedIds.has(item.node.id)) continue;
      const conn = findConnectingEdge(idx, lastId, item.node.id) || (item.dir === 'out' ? item.edge : null);
      ordered.push({
        node_id: item.node.id,
        incoming_edge_id: conn ? conn.id : null,
        role: 'co_review',
      });
      selectedIds.add(item.node.id);
      lastId = item.node.id;
    }

    return {
      mode: 'cluster_review',
      thread_id: null,
      epicenter_id: epicenter.id,
      rationale: `围绕薄弱/遗忘震中「${epicenter.title}」，联动上游引子与 ${Math.max(0, ordered.length - (primerEdge ? 2 : 1))} 个邻近知识簇`,
      queue: ordered,
    };
  }

  function startSession(progress, plan, nowMs = Date.now()) {
    const next = clone(progress);
    const isoNow = new Date(nowMs).toISOString();
    next.session = {
      ...next.session,
      mode: plan.mode,
      active_thread_id: plan.thread_id || null,
      active_queue: plan.queue || [],
      cursor: 0,
      detour_stack: [],
      last_active_at: isoNow,
    };
    next.updated_at = isoNow;
    return next;
  }

  function pushDetour(graph, progress, targetNodeId, viaEdgeId = null, nowMs = Date.now(), options = {}) {
    const idx = indexGraph(graph);
    if (!idx.nodeMap.has(targetNodeId)) throw new Error('Unknown detour target: ' + targetNodeId);
    const next = clone(progress);
    const cur = getCurrentStep(next);
    const isoNow = new Date(nowMs).toISOString();

    const edgeId = viaEdgeId || (cur ? findConnectingEdge(idx, cur.node_id, targetNodeId)?.id : null) || null;
    next.session.detour_stack = next.session.detour_stack || [];
    next.session.detour_stack.push({
      return_node_id: options.overrideReturnNodeId !== undefined ? options.overrideReturnNodeId : (cur ? cur.node_id : null),
      return_cursor: typeof options.overrideReturnCursor === 'number' ? options.overrideReturnCursor : (next.session.cursor || 0),
      detour_node_id: targetNodeId,
      via_edge_id: edgeId,
      planned: Boolean(options.planned),
      reason: options.reason || null,
      ts: isoNow,
    });
    next.session.last_active_at = isoNow;
    next.updated_at = isoNow;
    return next;
  }

  function planDiagnosticDetour(graph, progress, nodeId, rating) {
    if (rating !== 'again') return null;
    const idx = indexGraph(graph);
    const node = idx.nodeMap.get(nodeId);
    if (!node) return null;
    const stackIds = new Set((progress?.session?.detour_stack || []).map(d => d.detour_node_id));
    if (stackIds.size >= 2) return null; // avoid deep automated detour chains

    const incoming = (idx.inEdges.get(nodeId) || [])
      .filter(e => e.relation_type === 'prerequisite' || (e.relation_type === 'structural_basis' && (e.strength || 0.8) >= 0.85))
      .sort((a, b) => (b.strength || 0.8) - (a.strength || 0.8));

    for (const edge of incoming) {
      const parent = idx.nodeMap.get(edge.source);
      if (!parent || parent.id === nodeId || stackIds.has(parent.id)) continue;
      const pState = getNodeState(progress, parent.id);
      if (['unvisited', 'again', 'fuzzy'].includes(pState.status)) {
        return {
          target_node_id: parent.id,
          target_title: parent.title,
          via_edge_id: edge.id,
          return_node_id: nodeId,
          reason: `在「${node.title}」遇到卡点，算法自动规划先绕道 1 站补齐基础「${parent.title}」`,
        };
      }
    }
    return null;
  }

  function popDetour(progress, nowMs = Date.now()) {
    const next = clone(progress);
    if (!next.session.detour_stack || next.session.detour_stack.length === 0) return next;
    const popped = next.session.detour_stack.pop();
    if (next.session.detour_stack.length === 0 && typeof popped.return_cursor === 'number') {
      next.session.cursor = popped.return_cursor;
    }
    const isoNow = new Date(nowMs).toISOString();
    next.session.last_active_at = isoNow;
    next.updated_at = isoNow;
    return next;
  }

  function resumeSession(graph, progress, nowMs = Date.now()) {
    const idx = indexGraph(graph);
    const cur = getCurrentStep(progress);
    const trail = (progress?.session?.recent_trail || []).slice(-3);

    const breadcrumbs = trail.map((entry, i) => {
      const node = idx.nodeMap.get(entry.node_id);
      const edge = entry.edge_id ? idx.edgeMap.get(entry.edge_id) : (i > 0 ? findConnectingEdge(idx, trail[i - 1].node_id, entry.node_id) : null);
      return {
        node_id: entry.node_id,
        title: node?.title || entry.node_id,
        status: entry.status,
        bridge_text: edge?.bridge_text || null,
      };
    });

    const curNode = cur ? idx.nodeMap.get(cur.node_id) : null;
    const lastVisited = trail.length > 0 ? idx.nodeMap.get(trail[trail.length - 1].node_id) : null;
    const incomingEdge = cur?.incoming_edge_id
      ? idx.edgeMap.get(cur.incoming_edge_id)
      : (lastVisited && curNode && lastVisited.id !== curNode.id ? findConnectingEdge(idx, lastVisited.id, curNode.id) : null);

    const lastActiveMs = Date.parse(progress?.session?.last_active_at || '');
    const idleMinutes = Number.isFinite(lastActiveMs) ? Math.max(0, Math.round((nowMs - lastActiveMs) / 60000)) : 0;

    const topDetour = cur?.in_detour && progress?.session?.detour_stack?.length
      ? progress.session.detour_stack[progress.session.detour_stack.length - 1]
      : null;

    return {
      has_active: Boolean(curNode),
      current_node_id: curNode?.id || null,
      in_detour: Boolean(cur?.in_detour),
      detour_depth: cur?.detour_depth || 0,
      is_planned_detour: Boolean(cur?.is_planned_detour),
      detour_reason: cur?.detour_reason || null,
      mainline_progress: {
        cursor: progress?.session?.cursor || 0,
        total: progress?.session?.active_queue?.length || 0,
        return_node_title: topDetour?.return_node_id
          ? idx.nodeMap.get(topDetour.return_node_id)?.title
          : null,
      },
      breadcrumbs,
      recap: curNode ? {
        prev_title: lastVisited?.title || null,
        prev_anchor: lastVisited?.anchor_summary || null,
        bridge_text: incomingEdge?.bridge_text || null,
        relation_label: incomingEdge ? RELATION_META[incomingEdge.relation_type]?.label : null,
        idle_minutes: idleMinutes,
      } : null,
    };
  }

  function getSortedNeighbors(graph, progress, nodeId, nowMs = Date.now()) {
    const idx = indexGraph(graph);
    const seen = new Map();

    const addCandidate = (neighborId, edge, direction) => {
      const nb = idx.nodeMap.get(neighborId);
      if (!nb) return;
      const st = getNodeState(progress, nb.id);
      const r = computeRetrievability(st, nowMs);
      const isWeak = st.status === 'again' || st.status === 'fuzzy' || (r !== null && r < 0.72);
      const urgency = st.status === 'unvisited' ? 0 : computeUrgency(nb, st, nowMs);

      let rank = 0;
      if (isWeak) rank += 100 + urgency * 10;
      if (edge.relation_type === 'cross_discipline') rank += 35;
      else if (edge.relation_type === 'contrast_with') rank += 28;
      else if (edge.relation_type === 'causal_next' || edge.relation_type === 'clinical_manifestation') rank += 20;
      else if (edge.relation_type === 'prerequisite' || edge.relation_type === 'structural_basis') rank += 15;
      if (nb.syllabus_tier === 'understand') rank -= 12;
      rank += (TIER_WEIGHT[nb.syllabus_tier] || 2);

      const existing = seen.get(nb.id);
      if (!existing || rank > existing.rank) {
        seen.set(nb.id, {
          node: nb,
          edge,
          direction,
          status: st.status,
          retrievability: r,
          is_weak: isWeak,
          rank,
        });
      }
    };

    for (const e of idx.outEdges.get(nodeId) || []) addCandidate(e.target, e, 'outgoing');
    for (const e of idx.inEdges.get(nodeId) || []) addCandidate(e.source, e, 'incoming');

    return [...seen.values()].sort((a, b) => b.rank - a.rank);
  }

  function getSyllabusProgress(graph, progress) {
    const bySubject = {};
    const nodeRefsBySyl = new Map();
    for (const n of graph.nodes || []) {
      for (const sid of n.syllabus_refs || []) {
        if (!nodeRefsBySyl.has(sid)) nodeRefsBySyl.set(sid, []);
        nodeRefsBySyl.get(sid).push(n);
      }
    }

    for (const item of graph.syllabus || []) {
      const subj = item.subject;
      if (!bySubject[subj]) {
        bySubject[subj] = {
          subject: subj,
          label: SUBJECT_LABELS[subj] || subj,
          total_syllabus: 0,
          covered_syllabus: 0,
          mastered_syllabus: 0,
          core_total: 0,
          core_mastered: 0,
          nodes: [],
        };
      }
      const bucket = bySubject[subj];
      bucket.total_syllabus += 1;
      if (item.tier === 'core_underlined' || item.tier === 'master') bucket.core_total += 1;
      const linked = nodeRefsBySyl.get(item.id) || [];
      if (linked.length > 0) {
        bucket.covered_syllabus += 1;
        const allMastered = linked.every(n => getNodeState(progress, n.id).status === 'mastered');
        if (allMastered) {
          bucket.mastered_syllabus += 1;
          if (item.tier === 'core_underlined' || item.tier === 'master') bucket.core_mastered += 1;
        }
      }
    }

    for (const n of graph.nodes || []) {
      if (!bySubject[n.subject]) {
        bySubject[n.subject] = {
          subject: n.subject,
          label: SUBJECT_LABELS[n.subject] || n.subject,
          total_syllabus: 0,
          covered_syllabus: 0,
          mastered_syllabus: 0,
          core_total: 0,
          core_mastered: 0,
          nodes: [],
        };
      }
      const st = getNodeState(progress, n.id);
      bySubject[n.subject].nodes.push({
        id: n.id,
        title: n.title,
        syllabus_tier: n.syllabus_tier,
        status: st.status,
      });
    }

    return Object.values(bySubject);
  }

  function getLocalSubgraph(graph, progress, centerId) {
    const idx = indexGraph(graph);
    const center = idx.nodeMap.get(centerId);
    if (!center) return { center: null, nodes: [], edges: [] };

    const neighbors = getSortedNeighbors(graph, progress, centerId).slice(0, 8);
    const subNodes = [
      { ...center, status: getNodeState(progress, center.id).status, is_center: true },
      ...neighbors.map(nb => ({ ...nb.node, status: nb.status, is_center: false, via_relation: nb.edge.relation_type })),
    ];
    const ids = new Set(subNodes.map(n => n.id));
    const subEdges = (graph.edges || []).filter(e => ids.has(e.source) && ids.has(e.target));
    return { center, nodes: subNodes, edges: subEdges };
  }

  function getGalaxyLayout(graph, progress, centerId, nowMs = Date.now(), maxNeighbors = 6) {
    const idx = indexGraph(graph);
    const center = idx.nodeMap.get(centerId);
    if (!center) return { center: null, items: [], extra_neighbors: [], height_px: 260 };

    const allNeighbors = getSortedNeighbors(graph, progress, centerId, nowMs);
    const picked = allNeighbors.slice(0, maxNeighbors);
    const extra = allNeighbors.slice(maxNeighbors);

    // Semantic rows (max 2 cards per row at x=26% / x=74%, width<=44%) to guarantee zero overlap on 390px screens
    const upstream = [];
    const lateral = [];
    const downstream = [];

    for (const nb of picked) {
      const rel = nb.edge.relation_type;
      if ((nb.direction === 'incoming' && ['causal_next', 'prerequisite', 'structural_basis'].includes(rel)) ||
          (nb.direction === 'outgoing' && ['prerequisite', 'structural_basis'].includes(rel))) {
        upstream.push(nb);
      } else if (['cross_discipline', 'contrast_with'].includes(rel)) {
        lateral.push(nb);
      } else {
        downstream.push(nb);
      }
    }

    // Balance overflow gently so each band stays <= 2 cards (up to 6 total across 3 neighbor bands)
    const overflow = (src, dst1, dst2) => {
      while (src.length > 2) {
        if (dst1.length < 2) dst1.push(src.pop());
        else if (dst2.length < 2) dst2.push(src.pop());
        else break;
      }
    };
    overflow(upstream, lateral, downstream);
    overflow(downstream, lateral, upstream);
    overflow(lateral, downstream, upstream);

    const bands = [];
    if (upstream.length) bands.push({ kind: 'upstream', nodes: upstream });
    bands.push({ kind: 'center', nodes: [center] });
    if (lateral.length) bands.push({ kind: 'lateral', nodes: lateral });
    if (downstream.length) bands.push({ kind: 'downstream', nodes: downstream });

    const rowHeight = 108;
    const padY = 54;
    const totalHeight = Math.max(250, padY * 2 + (bands.length - 1) * rowHeight);

    const items = [];
    let centerY = 50;

    bands.forEach((band, rowIdx) => {
      const yPct = Number((((padY + rowIdx * rowHeight) / totalHeight) * 100).toFixed(1));
      if (band.kind === 'center') {
        centerY = yPct;
        return;
      }
      const xs = band.nodes.length === 1 ? [50] : [26, 74];
      band.nodes.forEach((nb, i) => {
        items.push({ ...nb, x: xs[i], y: yPct, slot: band.kind });
      });
    });

    return {
      center: {
        ...center,
        status: getNodeState(progress, center.id).status,
        x: 50,
        y: centerY,
      },
      items,
      extra_neighbors: extra,
      height_px: totalHeight,
    };
  }

  return {
    TIER_WEIGHT,
    TIER_LABELS,
    SUBJECT_LABELS,
    RELATION_META,
    STATUS_META,
    indexGraph,
    findConnectingEdge,
    createEmptyProgress,
    getNodeState,
    computeRetrievability,
    computeUrgency,
    recommendImplicitRating,
    getCurrentStep,
    applyReview,
    planUnvisitedFrontier,
    planClusterReview,
    planDiagnosticDetour,
    startSession,
    pushDetour,
    popDetour,
    resumeSession,
    getSortedNeighbors,
    getSyllabusProgress,
    getLocalSubgraph,
    getGalaxyLayout,
  };
});
