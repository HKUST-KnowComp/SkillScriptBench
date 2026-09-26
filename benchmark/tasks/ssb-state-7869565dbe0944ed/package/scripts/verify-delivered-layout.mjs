import { chromium } from 'playwright';

export async function verifyDeliveredLayout(url) {
  const browser = await chromium.launch();
  try {
    const page = await browser.newPage();
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    const response = await page.goto(url, { waitUntil: 'networkidle' });
    if (!response || response.status() !== 200) return { ok: false, why: 'rendered page unavailable' };
    const result = await page.evaluate(() => {
      const blocks = [...document.querySelectorAll('main [data-block-name]')];
      const sections = [...document.querySelectorAll('main .section')];
      const layouts = [];
      function collect(rules) {
        for (const rule of rules) {
          if (rule.style && ['grid', 'flex', 'inline-grid', 'inline-flex'].includes(rule.style.display)) {
            layouts.push({ selector: rule.selectorText || '', display: rule.style.display });
          }
          if (rule.cssRules) collect(rule.cssRules);
        }
      }
      for (const sheet of document.styleSheets) {
        try { collect(sheet.cssRules); } catch { /* Cross-origin rules are not inspectable. */ }
      }
      const inconsistent = blocks.some(block => {
        const name = block.dataset.blockName;
        const escaped = name.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
        const token = new RegExp('\\.' + escaped + '(?![\\w-])');
        const required = layouts.filter(rule => token.test(rule.selector));
        return required.length > 0 && !required.some(rule => getComputedStyle(block).display === rule.display);
      });
      return {
        structure: sections.length > 0 && blocks.length > 0,
        layout: !inconsistent,
        images: [...document.images].every(image => image.complete && image.naturalWidth > 0),
      };
    });
    const failed = Object.entries(result).filter(([, value]) => !value).map(([name]) => name);
    if (errors.length) failed.push('pageerror');
    return { ok: failed.length === 0, why: failed.join(', ') };
  } finally {
    await browser.close();
  }
}
