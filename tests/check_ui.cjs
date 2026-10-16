// Optional browser regression: npm's playwright package and an installed Edge.
// Uses synthetic responses only. No recap service, account, or model is used.
const { chromium } = require('playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

(async () => {
  const output = process.argv[2] || fs.mkdtempSync(path.join(os.tmpdir(), 'recap-ui-'));
  fs.mkdirSync(output, { recursive: true });
  const browser = await chromium.launch({ channel: 'msedge', headless: true });
  try {
    const page = await browser.newPage({ viewport: { width: 760, height: 450 }, colorScheme: 'light' });
    const html = fs.readFileSync(path.join(__dirname, '../ui/index.html'), 'utf8');
    const summary = { id: 'synthetic-preview', overview: '理清了任务提交到结果返回的调用链。\n定位了刷新后的状态问题，修复方案还在验证。', generated_at: '2026-01-01T09:24:00+08:00', period_label: '今天' };
    let state = { state: 'ready', paused: false, summary, display_summary: summary, display_summary_retained: false };
    const actions = [], errors = [];
    page.on('pageerror', error => errors.push(error.message));
    page.on('console', message => { if (message.type() === 'error') errors.push(message.text()); });
    await page.route('**/*', async route => {
      const url = new URL(route.request().url());
      if (url.pathname === '/api/state') return route.fulfill({ json: state });
      if (url.pathname === '/api/action') {
        const body = route.request().postDataJSON();
        actions.push(body.action);
        state = { ...state, paused: body.action === 'pause', state: body.action === 'pause' ? 'paused' : 'ready' };
        return route.fulfill({ json: state });
      }
      return route.fulfill({ contentType: 'text/html; charset=utf-8', body: html });
    });
    await page.goto('http://127.0.0.1:18739/#token=synthetic-test-token');
    await page.getByRole('button', { name: '暂停自动总结' }).waitFor({ state: 'visible' });
    await page.waitForFunction(() => !document.querySelector('#pause').disabled);
    assert.equal(await page.title(), '工作回顾');
    assert.equal(await page.locator('#overview').innerText(), summary.overview);
    assert.match(await page.locator('#last-time').innerText(), /1.*1/);
    await page.screenshot({ path: path.join(output, 'panel-light.png') });
    await page.getByRole('button', { name: '暂停自动总结' }).click();
    await page.getByRole('button', { name: '恢复自动总结' }).waitFor();
    assert.equal(await page.locator('#status').innerText(), '自动回顾已暂停');
    await page.getByRole('button', { name: '恢复自动总结' }).click();
    await page.getByRole('button', { name: '暂停自动总结' }).waitFor();
    assert.deepEqual(actions, ['pause', 'resume']);

    // Legacy transient replies retain this page's cache; an explicit new null clears it.
    for (const phase of ['collecting', 'summarizing', 'error']) {
      state = { state: phase, paused: false, summary: null };
      await page.evaluate(() => document.dispatchEvent(new Event('visibilitychange')));
      await page.waitForFunction(expected => document.querySelector('#status').dataset.state === expected, phase);
      assert.equal(await page.locator('#overview').innerText(), summary.overview);
      assert.match(await page.locator('#period').innerText(), /^上次回顾/);
    }
    state = { state: 'collecting', paused: false, summary: null, display_summary: null, display_summary_retained: false };
    await page.evaluate(() => document.dispatchEvent(new Event('visibilitychange')));
    await page.waitForFunction(() => document.querySelector('#overview').classList.contains('empty'));
    assert.equal(await page.locator('#period').innerText(), '');

    // A fresh panel must use the persisted fallback even when summary is null.
    state = { state: 'idle', paused: false, summary: null, display_summary: summary, display_summary_retained: true };
    await page.reload();
    await page.waitForFunction(() => document.querySelector('#period').textContent.startsWith('上次回顾'));
    assert.equal(await page.locator('#overview').innerText(), summary.overview);
    await page.setViewportSize({ width: 375, height: 470 });
    await page.emulateMedia({ colorScheme: 'dark' });
    await page.screenshot({ path: path.join(output, 'panel-retained-mobile.png') });
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));

    state = { ...state, state: 'error' };
    await page.reload();
    await page.locator('#failure-note').waitFor({ state: 'visible' });
    assert.equal(await page.locator('#overview').innerText(), summary.overview);
    assert.match(await page.locator('#failure-note').innerText(), /仍可查看/);
    state = { state: 'summarizing', paused: false, summary: null, display_summary: null, display_summary_retained: false };
    await page.reload();
    await page.waitForFunction(() => document.querySelector('#overview').textContent === '正在整理工作进展…');
    state = { ...state, state: 'idle' };
    await page.reload();
    await page.waitForFunction(() => document.querySelector('#overview').textContent === '新的工作进展会自动整理在这里。');
    assert.equal(await page.locator('#period').innerText(), '');
    assert.deepEqual(errors, []);
    console.log(JSON.stringify({ passed: true, states: ['ready', 'paused', 'retained', 'error', 'summarizing', 'empty'], actions, screenshots: output }));
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
