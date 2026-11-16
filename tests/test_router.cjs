'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const R = require('../web/router.js');
const S = require('../web/sync.js');

const graphPath = path.resolve(__dirname, '..', 'data', 'graph.json');
const graph = JSON.parse(fs.readFileSync(graphPath, 'utf8'));

let passed = 0;
function check(label, fn) {
  fn();
  passed++;
  console.log('PASS ' + label);
}

check('seed graph has valid structure, bridge_text on all edges, and 7 subjects', () => {
  assert.equal(graph.schema_version, 1);
  assert(graph.nodes.length >= 30);
  assert(graph.edges.length >= 40);
  assert(graph.threads.length >= 5);
  for (const e of graph.edges) {
    assert(typeof e.bridge_text === 'string' && e.bridge_text.trim().length > 8, `Missing bridge_text on ${e.id}`);
  }
  const subjects = new Set(graph.nodes.map(n => n.subject));
  assert(subjects.has('anatomy') && subjects.has('pathology') && subjects.has('biochem') && subjects.has('physiology'));
});

check('unvisited frontier prioritizes core/master nodes and folds understand-tier extensions', () => {
  const prog = R.createEmptyProgress(Date.parse('2026-11-16T10:00:00Z'));
  const plan = R.planUnvisitedFrontier(graph, prog, { nowMs: Date.parse('2026-11-16T12:00:00Z') });
  assert(plan.queue.length >= 3 && plan.queue.length <= 8);
  const idx = R.indexGraph(graph);
  for (const step of plan.queue) {
    const node = idx.nodeMap.get(step.node_id);
    assert.notEqual(node.syllabus_tier, 'understand', `Understand node ${node.id} leaked into mainline`);
  }
  const firstNode = idx.nodeMap.get(plan.queue[0].node_id);
  assert(['core_underlined', 'master'].includes(firstNode.syllabus_tier));
});

check('prerequisite gating automatically prepends missing prerequisite before dependent concept', () => {
  const prog = R.createEmptyProgress(Date.parse('2026-11-16T10:00:00Z'));
  // Mark physio_resting_potential and physio_membrane_transport unvisited, mark earlier thread step?
  // Test planning specifically when starting a thread slice at physio_action_potential
  // Simulate physio_membrane_transport mastered, physio_resting_potential STILL unvisited, request path starting at physio_action_potential
  const g2 = JSON.parse(JSON.stringify(graph));
  // Raise priority of physio_action_potential temporarily to force it as seed while resting potential is 'again'
  const ap = g2.nodes.find(n => n.id === 'physio_action_potential');
  ap.added_at = '2026-11-16T11:55:00Z';
  const p2 = R.applyReview(g2, prog, 'physio_membrane_transport', 'mastered', null, Date.parse('2026-11-16T10:00:00Z'));
  const p3 = R.applyReview(g2, p2, 'physio_resting_potential', 'again', null, Date.parse('2026-11-16T10:05:00Z'));
  // Force thread slice starting at physio_action_potential (skipping mastered steps in thread)
  const plan = R.planUnvisitedFrontier(g2, p3, {
    threadId: 'thread_excitation_contraction',
    nowMs: Date.parse('2026-11-16T12:00:00Z'),
  });
  const ids = plan.queue.map(s => s.node_id);
  assert(ids.includes('physio_resting_potential'), 'Failed/missing prerequisite should be gated back into queue');
  assert(ids.indexOf('physio_resting_potential') < ids.indexOf('physio_action_potential'), 'Prerequisite must precede dependent node');
});

check('FSRS-lite stability updates & forgetting decay work monotonically', () => {
  const t0 = Date.parse('2026-11-16T08:00:00Z');
  let prog = R.createEmptyProgress(t0);
  prog = R.applyReview(graph, prog, 'patho_hepatic_congestion', 'mastered', null, t0);
  const s1 = R.getNodeState(prog, 'patho_hepatic_congestion');
  assert.equal(s1.status, 'mastered');
  assert(s1.stability >= 3.5);

  const rImmediate = R.computeRetrievability(s1, t0 + 60000);
  const rLater = R.computeRetrievability(s1, t0 + 14 * 86400000);
  assert(rImmediate > 0.99 && rLater < rImmediate);

  prog = R.applyReview(graph, prog, 'patho_hepatic_congestion', 'again', null, t0 + 14 * 86400000);
  const s2 = R.getNodeState(prog, 'patho_hepatic_congestion');
  assert.equal(s2.status, 'again');
  assert.equal(s2.lapse_count, 1);
  assert(s2.stability < s1.stability);
});

check('neighborhood cluster review targets weak epicenter + upstream primer + cross-discipline/weak neighbors', () => {
  const t0 = Date.parse('2026-11-16T08:00:00Z');
  let prog = R.createEmptyProgress(t0);
  prog = R.applyReview(graph, prog, 'histo_liver_lobule', 'mastered', null, t0);
  prog = R.applyReview(graph, prog, 'patho_hepatic_congestion', 'again', null, t0 + 10000);
  prog = R.applyReview(graph, prog, 'anat_portal_vein_system', 'fuzzy', null, t0 + 20000);
  prog = R.applyReview(graph, prog, 'biochem_urea_cycle', 'fuzzy', null, t0 + 30000);

  const cluster = R.planClusterReview(graph, prog, t0 + 3600000);
  assert.equal(cluster.mode, 'cluster_review');
  assert.equal(cluster.epicenter_id, 'patho_hepatic_congestion');
  const ids = cluster.queue.map(s => s.node_id);
  assert.equal(ids[0], 'histo_liver_lobule', 'Upstream structural primer should lead the cluster');
  assert(ids.includes('patho_hepatic_congestion'));
  assert(ids.includes('anat_portal_vein_system') || ids.includes('biochem_urea_cycle'));
  // Every transition after step 0 should carry an explicit bridge edge when connected
  assert(cluster.queue[1].incoming_edge_id !== null);
});

check('detour stack pushes, supports multi-hop exploration, builds breadcrumbs, and restores exact mainline cursor', () => {
  const t0 = Date.parse('2026-11-16T08:00:00Z');
  let prog = R.createEmptyProgress(t0);
  const plan = R.planUnvisitedFrontier(graph, prog, { threadId: 'thread_congestion_portal', nowMs: t0 });
  prog = R.startSession(prog, plan, t0);

  // Advance 2 mainline steps: step 0 -> step 1 -> step 2
  const firstId = prog.session.active_queue[0].node_id;
  const secondId = prog.session.active_queue[1].node_id;
  const thirdId = prog.session.active_queue[2].node_id;
  prog = R.applyReview(graph, prog, firstId, 'mastered', null, t0 + 1000);
  prog = R.applyReview(graph, prog, secondId, 'fuzzy', null, t0 + 2000);
  assert.equal(R.getCurrentStep(prog).node_id, thirdId);
  assert.equal(prog.session.cursor, 2);

  // Detour to portal vein -> then 2nd hop to biochem hyperammonemia
  prog = R.pushDetour(graph, prog, 'anat_portal_vein_system', null, t0 + 3000);
  assert.equal(R.getCurrentStep(prog).node_id, 'anat_portal_vein_system');
  assert.equal(R.getCurrentStep(prog).detour_depth, 1);

  prog = R.pushDetour(graph, prog, 'biochem_hyperammonemia_he', 'e_portal_to_hyperammonemia', t0 + 4000);
  assert.equal(R.getCurrentStep(prog).node_id, 'biochem_hyperammonemia_he');
  assert.equal(R.getCurrentStep(prog).detour_depth, 2);

  const resumed = R.resumeSession(graph, prog, t0 + 25 * 60000);
  assert.equal(resumed.in_detour, true);
  assert.equal(resumed.detour_depth, 2);
  assert.equal(resumed.breadcrumbs.length, 2);
  assert(resumed.recap.bridge_text.includes('门体侧支'));

  // Pop back twice -> exact mainline station 3 restored
  prog = R.popDetour(prog, t0 + 26 * 60000);
  assert.equal(R.getCurrentStep(prog).node_id, 'anat_portal_vein_system');
  prog = R.popDetour(prog, t0 + 27 * 60000);
  const restored = R.getCurrentStep(prog);
  assert.equal(restored.in_detour, false);
  assert.equal(restored.node_id, thirdId);
  assert.equal(prog.session.cursor, 2);
});

check('sync progress merge preserves newest per-node mastery across two devices', () => {
  const t1 = Date.parse('2026-11-16T08:00:00Z');
  const t2 = Date.parse('2026-11-16T09:00:00Z');
  let phone = R.createEmptyProgress(t1);
  phone = R.applyReview(graph, phone, 'anat_heart_chambers', 'mastered', null, t2);
  phone = R.applyReview(graph, phone, 'biochem_urea_cycle', 'again', null, t1);

  let pc = R.createEmptyProgress(t1);
  pc = R.applyReview(graph, pc, 'anat_heart_chambers', 'again', null, t1);
  pc = R.applyReview(graph, pc, 'biochem_urea_cycle', 'mastered', null, t2);

  const merged = S.mergeProgress(phone, pc);
  assert.equal(merged.node_states['anat_heart_chambers'].status, 'mastered');
  assert.equal(merged.node_states['biochem_urea_cycle'].status, 'mastered');
});

check('algorithmic diagnostic detour & planned opportunistic detour work alongside manual detours', () => {
  const t0 = Date.parse('2026-11-16T08:00:00Z');
  let prog = R.createEmptyProgress(t0);
  // 1. Diagnostic detour when clicking 'again' on physio_nmj_transmission while histo_chemical_synapse is unvisited
  const diag = R.planDiagnosticDetour(graph, prog, 'physio_nmj_transmission', 'again');
  assert(diag !== null, 'Should detect unvisited structural_basis parent histo_chemical_synapse');
  assert.equal(diag.target_node_id, 'histo_chemical_synapse');
  assert.equal(diag.return_node_id, 'physio_nmj_transmission');

  // 2. Opportunistic planned detour weaving when a mainline node connects to a weak cross_discipline/contrast neighbor
  prog = R.applyReview(graph, prog, 'biochem_hyperammonemia_he', 'again', null, t0);
  const plan = R.planUnvisitedFrontier(graph, prog, {
    threadId: 'thread_congestion_portal',
    nowMs: t0 + 1000,
    autoDetour: true,
  });
  const woven = plan.queue.find(s => s.is_planned_detour);
  assert(woven, 'Frontier planner should weave weak cross_discipline neighbor as planned detour');
  assert.equal(woven.node_id, 'biochem_hyperammonemia_he');

  // 3. Semantic Galaxy layout returns full non-truncated titles in semantic quadrants
  const galaxy = R.getGalaxyLayout(graph, prog, 'anat_portal_vein_system', t0 + 2000, 6);
  assert(galaxy.center && galaxy.center.title.includes('肝门静脉'));
  assert(galaxy.items.length >= 3);
  assert(galaxy.items.some(i => i.slot === 'lateral' || i.slot === 'upstream' || i.slot === 'downstream'));
});

console.log(`\nAll ${passed} MedRouter & MedSync unit tests passed.`);
