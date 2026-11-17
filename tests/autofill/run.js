/* Runs the Job Engine autofill bookmark on a page in Chromium (tests/test_autofill_browser.py).
   Usage: node run.js <page.html> <bookmarklet.txt> <code.txt> [screenshot.png] */
const { chromium } = require('playwright');
const fs = require('fs');
(async () => {
  const [page_, markFile, codeFile, shot] = process.argv.slice(2);
  const code = fs.readFileSync(codeFile, 'utf8');
  const js = decodeURIComponent(fs.readFileSync(markFile, 'utf8').replace(/^javascript:/, ''));
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 900, height: 1400 } });
  const dialogs = [];
  page.on('dialog', async d => {
    dialogs.push({ type: d.type(), message: d.message() });
    if (d.type() === 'prompt') { await d.accept('Autofill: paste this whole message.\n' + code); }
    else { await d.accept(); }
  });
  const errors = [];
  page.on('pageerror', e => errors.push(e.message));
  await page.goto('file://' + require('path').resolve(page_));
  await page.evaluate(js);
  for (let n = 0; n < 300 && !dialogs.some(d => d.type === 'alert'); n++) {
    await page.waitForTimeout(100);
  }
  await page.waitForTimeout(600);
  const state = await page.evaluate(() => {
    const out = {};
    for (const el of document.querySelectorAll('input, textarea, select')) {
      let v = el.value;
      if (el.type === 'radio' || el.type === 'checkbox') { v = el.checked; }
      if (el.tagName === 'SELECT') { v = el.options[el.selectedIndex].text; }
      out[el.id || (el.name + ':' + el.value)] = v;
    }
    for (const el of document.querySelectorAll('.select__single-value')) { out[el.id] = el.textContent; }
    out.SUBMITTED = !!window.SUBMITTED; out.CLICKED = !!window.CLICKED;
    out.ORANGE = [...document.querySelectorAll('*')].filter(e => e.style.outline).length;
    return out;
  });
  if (shot) { await page.screenshot({ path: shot, fullPage: true }); }
  await browser.close();
  console.log(JSON.stringify({ dialogs, errors, state }));
})().catch(e => { console.error(e); process.exit(1); });
