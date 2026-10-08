import { chromium } from 'playwright';

// Public runtime obligations only: derive layout expectations from active CSS,
// not benchmark block names, URLs, profiles, or expected outputs.
export async function verifyDeliveredLayout(url) {
  let browser;
  try {
    browser = await chromium.launch();
    const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    const response = await page.goto(url, { waitUntil: 'networkidle', timeout: 30000 });
    if (!response || response.status() !== 200) return { ok: false, why: 'delivered page unavailable' };
    await page.evaluate(async () => {
      for (let y = 0; y < document.body.scrollHeight; y += 600) {
        window.scrollTo(0, y);
        await new Promise(resolve => setTimeout(resolve, 40));
      }
      window.scrollTo(0, 0);
    });
    const checks = await page.evaluate(() => {
      const rules = [];
      let unreadable = false;
      function collect(list) {
        for (const rule of list) {
          if (rule.type === CSSRule.MEDIA_RULE && !matchMedia(rule.conditionText).matches) continue;
          if (rule.type === CSSRule.SUPPORTS_RULE && !CSS.supports(rule.conditionText)) continue;
          if (rule.style && rule.selectorText && ['grid', 'flex', 'inline-grid', 'inline-flex'].includes(rule.style.display)) {
            rules.push({ selector: rule.selectorText, display: rule.style.display });
          }
          if (rule.cssRules) collect(rule.cssRules);
        }
      }
      for (const sheet of document.styleSheets) {
        if (sheet.disabled || (sheet.media.mediaText && !matchMedia(sheet.media.mediaText).matches)) continue;
        try { collect(sheet.cssRules); } catch { unreadable = true; }
      }
      const blocks = [...document.querySelectorAll('main [data-block-name]')];
      const faults = [];
      for (const block of blocks) {
        const rect = block.getBoundingClientRect();
        if (block.dataset.blockStatus !== 'loaded' || !block.childElementCount || rect.height <= 5) faults.push('undecorated or empty block');
        const name = block.dataset.blockName;
        for (const rule of rules) {
          for (const selector of rule.selector.split(',')) {
            // A compound class selector naming the block itself must apply.
            // Descendant layout rules are checked on their actual matched nodes.
            const tail = selector.trim().split(/[\s>+~]+/).pop();
            const classTokens = [...tail.matchAll(/\.([\w-]+)/g)].map(match => match[1]);
            if (classTokens.includes(name) && !tail.includes(':')) {
              if (!['grid', 'flex', 'inline-grid', 'inline-flex'].includes(getComputedStyle(block).display)) faults.push('declared block layout not applied');
            }
          }
        }
      }
      return {
        sections: document.querySelectorAll('main .section').length > 0,
        blocks: blocks.length > 0,
        layout: faults.length === 0,
        readable_styles: !unreadable,
        images: [...document.images].every(image => image.complete && image.naturalWidth > 0),
      };
    });
    checks.page_errors = errors.length === 0;
    const failed = Object.keys(checks).filter(key => !checks[key]);
    return { ok: failed.length === 0, why: failed.join(', '), checks };
  } catch (error) {
    return { ok: false, why: `layout verification failed: ${error.message}` };
  } finally {
    if (browser) await browser.close();
  }
}
