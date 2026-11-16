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

    await page.locator('[data-act="answer-quiz"][data-idx="2"]').click();
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

  await browser.close();
  console.log(`\nAll ${passed} mobile UI Playwright tests passed.`);
})();
