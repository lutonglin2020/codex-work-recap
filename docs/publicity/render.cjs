/* Export the three fixed-size posters. No production chat data is read. */
const fs = require('node:fs');
const path = require('node:path');
const { pathToFileURL } = require('node:url');
const { chromium } = require('playwright');

async function main() {
  const root = __dirname;
  for (const asset of ['collapsed.png', 'expanded.png']) {
    if (!fs.existsSync(path.join(root, 'assets', asset))) {
      throw new Error(`Missing synthetic preview asset: assets/${asset}. See README.md.`);
    }
  }
  const options = { headless: true };
  if (process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH) {
    options.executablePath = process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH;
  } else if (process.env.PLAYWRIGHT_CHANNEL) {
    options.channel = process.env.PLAYWRIGHT_CHANNEL;
  }
  const browser = await chromium.launch(options);
  try {
    const page = await browser.newPage({ viewport: { width: 1080, height: 1440 }, deviceScaleFactor: 1 });
    await page.goto(pathToFileURL(path.join(root, 'posters.html')).href, { waitUntil: 'load' });
    await page.evaluate(() => document.fonts.ready);
    const errors = await page.locator('.poster').evaluateAll(posters => posters.flatMap(poster => {
      const result = [];
      const bounds = poster.getBoundingClientRect();
      for (const element of poster.querySelectorAll('h1, p, .footer, .repo-card, .requirements, .scene, .demo, .features')) {
        const rect = element.getBoundingClientRect();
        if (rect.left < bounds.left || rect.right > bounds.right || rect.bottom > bounds.bottom) {
          result.push(`${poster.id}: element extends beyond poster (${element.className || element.tagName})`);
        }
      }
      return result;
    }));
    const imagesReady = await page.locator('img').evaluateAll(images => images.every(image => image.complete && image.naturalWidth > 0));
    if (!imagesReady || errors.length) throw new Error(`Layout checks failed: ${errors.join('; ') || 'image not loaded'}`);
    for (const [id, name] of [['cover', '01-cover.png'], ['product', '02-product.png'], ['release', '03-open-source.png']]) {
      await page.locator(`#${id}`).screenshot({ path: path.join(root, name) });
      process.stdout.write(`Exported ${name} (1080 x 1440)\n`);
    }
    // A small overview for visual review; the original PNGs remain full size.
    await page.addStyleTag({ content: 'body{display:flex;align-items:flex-start;width:3240px;background:#d8ddd3}.poster{flex:0 0 1080px;margin:0}' });
    await page.setViewportSize({ width: 3240, height: 1440 });
    await page.screenshot({ path: path.join(root, 'contact-sheet.png') });
    process.stdout.write('Exported contact-sheet.png (3240 x 1440)\n');
  } finally {
    await browser.close();
  }
}
main().catch(error => { console.error(error); process.exitCode = 1; });
