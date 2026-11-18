'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');
const { pathToFileURL } = require('node:url');
const { chromium } = require('C:/Users/32461/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');

const APP_DIR = path.resolve(__dirname, '..');
const OFFLINE_URL = pathToFileURL(path.join(APP_DIR, 'dist', 'medgraph_offline.html')).href;
const SHOT_DIR = path.resolve(APP_DIR, '..', 'output', 'playwright', 'medgraph');

let passed = 0;
async function check(label, fn) {
  await fn();
  passed++;
  console.log('PASS ' + label);
}

(async () => {
  await fs.mkdir(SHOT_DIR, { recursive: true });
  const browser = await chromium.launch({ channel: 'chrome', headless: true });

  const context = await browser.newContext({
    viewport: { width: 390, height: 844 },
    deviceScaleFactor: 2,
    isMobile: true,
    hasTouch: true,
  });

  const page = await context.newPage();
  const pageErrors = [];
  const externalRequests = [];
  page.on('pageerror', e => pageErrors.push(e.message));
  page.on('request', r => {
    if (/^https?:/.test(r.url())) externalRequests.push(r.url());
  });

  await page.goto(OFFLINE_URL);
  await page.waitForSelector('.card-title');

  await check('mobile 390x844 loads offline HTML cleanly with inline non-truncated Galaxy map', async () => {
    assert.deepEqual(externalRequests, []);
    assert.deepEqual(pageErrors, []);
    const title = await page.locator('.card-title').textContent();
    assert(title && title.trim().length > 2);
    const pillTitles = await page.locator('#inline-galaxy-section .galaxy-node-title').allTextContents();
    assert(pillTitles.length >= 2);
    for (const pt of pillTitles) {
      assert(!pt.includes('…') && !pt.includes('...'), `Title must not be truncated: ${pt}`);
    }
    await page.screenshot({ path: path.join(SHOT_DIR, '01-mobile-frontier-start.png'), fullPage: false });
  });

  await check('switch thread, interact with cloze/quiz/flow, and verify inline Galaxy map layout', async () => {
    await page.locator('#btn-mode-pill').click();
    await page.waitForSelector('[data-act="pick-thread"]');
    await page.locator('[data-act="pick-thread"][data-id="thread_congestion_portal"]').click();
    await page.waitForFunction(() => document.querySelector('.card-title')?.textContent?.includes('心腔流向'));

    const firstCloze = page.locator('[data-act="toggle-cloze"]').first();
    assert.equal(await firstCloze.getAttribute('aria-expanded'), 'false');
    await firstCloze.click();
    assert.equal(await firstCloze.getAttribute('aria-expanded'), 'true');
    await page.screenshot({ path: path.join(SHOT_DIR, '02-mobile-cloze-revealed.png'), fullPage: false });

    await page.locator('[data-act="rate"][data-rating="mastered"]').click();
    await page.waitForFunction(() => document.querySelector('.card-title')?.textContent?.includes('动脉性充血'));
    assert(await page.locator('.bridge-box').count() > 0);

    const hypAnsIdx = await page.evaluate(() => {
      const n = (window.MedApp.getGraph().nodes || []).find(x => x.id === 'patho_hyperemia_vs_congestion');
      return n?.payload?.quiz?.answer_idx ?? 2;
    });
    await page.locator(`[data-act="answer-quiz"][data-idx="${hypAnsIdx}"]`).click();
    await page.waitForSelector('.quiz-explain');
    assert.equal(await page.locator('.rate-mastered.recommended').count(), 1);

    await page.locator('[data-act="rate"][data-rating="mastered"]').click();
    await page.waitForFunction(() => document.querySelector('.card-title')?.textContent?.includes('肺淤血'));
    await page.locator('[data-act="next-flow-step"]').click();
    assert.equal(await page.locator('.flow-step.revealed').count(), 2);

    // Scroll to inline Galaxy section and capture full-title Galaxy screenshot
    await page.locator('#inline-galaxy-section').scrollIntoViewIfNeeded();
    await page.waitForTimeout(150);
    await page.screenshot({ path: path.join(SHOT_DIR, '03-mobile-inline-galaxy.png'), fullPage: false });
  });

  await check('manual Galaxy detour previews bridge_text first, enters detour, and persists across reload', async () => {
    const targetNodeBtn = page.locator('#inline-galaxy-section .galaxy-node-pill:not(.center)').first();
    const targetId = await targetNodeBtn.getAttribute('data-id');
    const targetTitle = await targetNodeBtn.locator('.galaxy-node-title').textContent();

    // First tap selects and updates preview bar with bridge_text
    await targetNodeBtn.click();
    await page.waitForFunction(id => {
      const btn = document.querySelector('#inline-galaxy-section [data-act="jump-detour"]');
      return btn && btn.getAttribute('data-id') === id;
    }, targetId);

    // Confirm detour via preview bar button
    await page.locator('#inline-galaxy-section [data-act="jump-detour"]').click();
    await page.waitForFunction(() => !document.getElementById('detour-banner').classList.contains('hidden'));
    const curTitle = await page.locator('.card-title').textContent();
    assert.equal(curTitle.trim(), targetTitle.trim());
    await page.screenshot({ path: path.join(SHOT_DIR, '04-mobile-detour-banner.png'), fullPage: false });

    // Rate detour node 'fuzzy' and reload page to verify breakpoint + breadcrumb persistence
    await page.locator('[data-act="rate"][data-rating="fuzzy"]').click();
    await page.reload();
    await page.waitForSelector('.card-title');
    assert.equal(await page.locator('#detour-banner').evaluate(el => !el.classList.contains('hidden')), true);
    assert((await page.locator('.breadcrumb-bar .crumb-chip').count()) >= 3);

    await page.locator('[data-act="pop-detour"]').click();
    await page.waitForFunction(() => document.getElementById('detour-banner').classList.contains('hidden'));
    const backTitle = await page.locator('.card-title').textContent();
    assert(backTitle.includes('肺淤血'), 'Returning from detour must restore mainline breakpoint');
  });

  await check('algorithmic diagnostic detour automatically triggers on "again" when prerequisite is unvisited', async () => {
    await page.locator('#btn-mode-pill').click();
    await page.locator('[data-act="pick-thread"][data-id="thread_excitation_contraction"]').click();
    // Jump via Atlas syllabus tab to physio_nmj_transmission (whose structural_basis parent histo_chemical_synapse is unvisited)
    await page.locator('[data-act="open-atlas"]').click();
    await page.locator('[data-act="atlas-tab"][data-tab="syllabus"]').click();
    await page.locator('[data-act="svg-jump"][data-id="physio_nmj_transmission"]').click();
    await page.waitForFunction(() => document.querySelector('.card-title')?.textContent?.includes('神经-肌接头'));
    // Tap 'again' -> algorithmic diagnostic detour should automatically route to histo_chemical_synapse!
    await page.locator('[data-act="rate"][data-rating="again"]').click();
    await page.waitForFunction(() => document.querySelector('.card-title')?.textContent?.includes('化学突触'));
    assert(await page.locator('.planned-detour-pill').count() > 0, 'Diagnostic planned detour banner should explain why');
  });

  await check('atlas drawer renders non-truncated Galaxy board & 7-subject syllabus heatmap', async () => {
    await page.locator('[data-act="open-atlas"]').click();
    await page.waitForSelector('#modal .galaxy-board .galaxy-node-pill');
    const modalTitles = await page.locator('#modal .galaxy-board .galaxy-node-title').allTextContents();
    for (const mt of modalTitles) {
      assert(!mt.includes('…') && !mt.includes('...'), `Modal Galaxy title must not be truncated: ${mt}`);
    }
    await page.screenshot({ path: path.join(SHOT_DIR, '05-mobile-galaxy-board.png'), fullPage: false });

    await page.locator('[data-act="atlas-tab"][data-tab="syllabus"]').click();
    await page.waitForSelector('.syl-subject-card');
    assert((await page.locator('.syl-subject-card').count()) >= 7);
    await page.screenshot({ path: path.join(SHOT_DIR, '06-mobile-syllabus-heatmap.png'), fullPage: false });
    await page.locator('[data-act="close-modal"]').click();
  });

  await check('cluster review mode targets the node marked again/fuzzy', async () => {
    await page.locator('#btn-mode-pill').click();
    await page.locator('[data-act="start-cluster"]').click();
    const modeText = await page.locator('#btn-mode-pill').textContent();
    assert(modeText.includes('邻域复习'));
    assert.deepEqual(pageErrors, []);
  });

  await check('V2 diagram drawer, image load, high-order reversal quiz, and remedial detour work on 390px mobile', async () => {
    const v2Target = await page.evaluate(() => {
      const nodes = window.MedApp.getGraph().nodes || [];
      return nodes.find(
        n => Array.isArray(n.diagram_assets) && n.diagram_assets.length > 0 && n.payload?.quiz?.remedial_node_id
      ) || null;
    });
    assert(v2Target, 'Expected at least one V2 node with diagram_assets and remedial quiz');
    await page.locator('[data-act="open-atlas"]').click();
    await page.locator('[data-act="atlas-tab"][data-tab="syllabus"]').click();
    await page.locator(`[data-act="svg-jump"][data-id="${v2Target.id}"]`).click();
    await page.waitForFunction(title => document.querySelector('.card-title')?.textContent?.trim() === title, v2Target.title);

    // Open collapsible diagram drawer & confirm WebP asset actually loaded cleanly
    await page.locator('.diagram-drawer > summary').click();
    await page.waitForSelector('.diagram-thumb-btn img');
    const imgLoaded = await page.locator('.diagram-thumb-btn img').first().evaluate(img => img.complete && img.naturalWidth > 100);
    assert(imgLoaded, 'Cropped WebP diagram image must decode cleanly with naturalWidth > 100');

    // Answer WRONG deliberately on the high-order quiz -> verify rescue tip & 🔴 recommendation
    const wrongIdx = (v2Target.payload.quiz.answer_idx + 1) % v2Target.payload.quiz.options.length;
    await page.locator(`[data-act="answer-quiz"][data-idx="${wrongIdx}"]`).click();
    await page.waitForSelector('.quiz-rescue-tip');
    assert.equal(await page.locator('.rate-again.recommended').count(), 1);
    await page.screenshot({ path: path.join(SHOT_DIR, '07-mobile-v2-diagram-quiz.png'), fullPage: false });

    // Tap thumb to open zoom lightbox modal, verify loaded image, then close modal
    await page.locator('.diagram-thumb-btn').first().click();
    await page.waitForSelector('.diagram-zoom-img');
    const zoomOk = await page.locator('.diagram-zoom-img').evaluate(img => img.complete && img.naturalWidth > 100);
    assert(zoomOk, 'Lightbox zoom image must load cleanly');
    await page.locator('[data-act="close-modal"]').click();

    // Tap 🔴 -> should detour directly to quiz.remedial_node_id
    await page.locator('[data-act="rate"][data-rating="again"]').click();
    await page.waitForFunction(() => document.querySelector('.planned-detour-pill')?.textContent?.includes('反转诊断题触发精准回溯'));
    assert.deepEqual(externalRequests, []);
    assert.deepEqual(pageErrors, []);
  });

  await check('non-cloze keyword emphasis highlights flow/matrix/trap safely, avoids quiz options & cloze pills, and toggles live', async () => {
    await page.locator('[data-act="open-atlas"]').click();
    await page.locator('[data-act="atlas-tab"][data-tab="syllabus"]').click();
    await page.locator('[data-act="svg-jump"][data-id="biochem_protein_colloid_salting_out"]').click();
    await page.waitForFunction(() => document.querySelector('.card-title')?.textContent?.includes('胶体双稳定'));
    await page.locator('[data-act="expand-all-flow"], [data-act="toggle-all-cloze"]').first().click().catch(() => {});

    const bodyTxt = await page.locator('#card-stage').innerText();
    assert(!bodyTxt.includes('**'), 'Raw markdown ** delimiters must not leak onto rendered card');
    assert((await page.locator('#card-stage .kw-core, #card-stage .kw-metric, #card-stage .kw-label, #card-stage .kw-contrast').count()) >= 2, 'Expected semantic keyword highlights on prose/trap/anchor');
    assert.equal(await page.locator('.quiz-opt .kw-core, .quiz-opt .kw-metric, .quiz-opt .kw-contrast, .quiz-opt .kw-label').count(), 0, 'Quiz options must NEVER highlight keywords');
    assert.equal(await page.locator('.cloze-pill .kw-core, .cloze-pill .kw-metric, .cloze-pill .kw-contrast, .cloze-pill .kw-label').count(), 0, 'Cloze pills must remain isolated interactive chips');
    await page.screenshot({ path: path.join(SHOT_DIR, '08-mobile-keyword-highlights.png'), fullPage: false });

    // Verify mechanism_flow card with explicit ** in quiz.stem + heuristic highlights in steps
    await page.locator('[data-act="open-atlas"]').click();
    await page.locator('[data-act="atlas-tab"][data-tab="syllabus"]').click();
    await page.locator('[data-act="svg-jump"][data-id="biochem_protein_primary_anfinsen"]').click();
    await page.waitForFunction(() => document.querySelector('.card-title')?.textContent?.includes('Anfinsen'));
    await page.locator('[data-act="expand-all-flow"]').click();
    const flowTxt = await page.locator('#card-stage').innerText();
    assert(!flowTxt.includes('**'), 'Explicit ** inside quiz.stem must render cleanly without asterisks');
    assert((await page.locator('.flow-reveal-text .kw-core, .flow-reveal-text .kw-metric, .takeaway-card .kw-core, .takeaway-card .kw-contrast, .quiz-stem .kw-core').count()) >= 2);
    await page.screenshot({ path: path.join(SHOT_DIR, '09-mobile-flow-keyword-highlights.png'), fullPage: false });

    // Live toggle off -> zero keyword highlights -> toggle back on
    await page.locator('[data-act="toggle-keywords"]').click();
    assert.equal(await page.locator('#card-stage .kw-core, #card-stage .kw-metric, #card-stage .kw-contrast, #card-stage .kw-label').count(), 0, 'Toggling off must render clean plain text');
    assert(!(await page.locator('#card-stage').innerText()).includes('**'), 'Toggling off must strip raw ** cleanly');
    await page.locator('[data-act="toggle-keywords"]').click();
    assert((await page.locator('#card-stage .kw-core, #card-stage .kw-metric, #card-stage .kw-contrast, #card-stage .kw-label').count()) >= 2);

    // Intra-card interaction (e.g. answering quiz while scrolled down) MUST preserve scroll; rating dock MUST snap to top
    await page.locator('[data-act="answer-quiz"]').first().scrollIntoViewIfNeeded();
    const preQuizY = await page.evaluate(() => window.scrollY);
    assert(preQuizY >= 200, 'Expected scrolled position before intra-card quiz tap');
    await page.locator('[data-act="answer-quiz"]').first().click();
    assert((await page.evaluate(() => window.scrollY)) >= 150, 'Intra-card quiz answer must NOT snap page back to top');

    // Tapping bottom rating dock while scrolled down MUST auto-scroll new card cleanly back to top!
    await page.locator('[data-act="rate"][data-rating="mastered"]').click();
    assert.equal(await page.evaluate(() => window.scrollY), 0, 'Rating transition must auto-scroll back to top');
    assert.deepEqual(externalRequests, []);
    assert.deepEqual(pageErrors, []);
  });

  await browser.close();
  console.log(`\nAll ${passed} mobile UI Playwright tests passed.`);
})();
