// Render docs/user-guide/*.md into one PDF with Chromium.
// Usage (in the Playwright container): node build-pdf.js /guide /out/DDA-User-Guide.pdf
const fs = require('fs');
const path = require('path');
const MarkdownIt = require('markdown-it');
const anchor = require('markdown-it-anchor');
const { chromium } = require('playwright');

const GUIDE = process.argv[2];
const OUT = process.argv[3];

const slug = (s) => s.trim().toLowerCase().replace(/[`*]/g, '').replace(/[^\w\s-]/g, '').replace(/\s+/g, '-').replace(/^-+|-+$/g, '');

const order = ['README.md', ...fs.readdirSync(GUIDE).filter((f) => /^\d\d-.*\.md$/.test(f)).sort()];

// Rewrite cross-file links (02-use-cases.md#anchor -> #02-use-cases--anchor; file -> #file) and
// repo-relative links (../../README.md) to GitHub URLs so the PDF stays navigable.
const REPO = 'https://github.com/awslabs/DefectDetectionApplication/blob/main/';
function rewriteLinks(md, file) {
  return md.replace(/\]\(([^)\s]+)\)/g, (m, href) => {
    if (/^(https?:|mailto:)/.test(href)) return m;
    if (href.startsWith('images/')) return m;
    const [target, hash] = href.split('#');
    if (target === '' && hash) return `](#${slug(file)}--${hash})`;
    if (/^\d\d-.*\.md$/.test(target) || target === 'README.md') {
      return hash ? `](#${slug(target)}--${hash})` : `](#${slug(target)})`;
    }
    // ../../README.md etc. -> GitHub
    const abs = path.posix.normalize(path.posix.join('docs/user-guide', target));
    return `](${REPO}${abs}${hash ? '#' + hash : ''})`;
  });
}

const parts = [];
for (const file of order) {
  let md = fs.readFileSync(path.join(GUIDE, file), 'utf8');
  md = rewriteLinks(md, file);
  const mdIt = new MarkdownIt({ html: false, linkify: false, typographer: false }).use(anchor, {
    slugify: (s) => `${slug(file)}--${slug(s)}`,
  });
  let html = mdIt.render(md);
  // first heading of each file also gets the file anchor
  html = html.replace(/<h1 id="([^"]+)">/, `<h1 id="$1"><a id="${slug(file)}"></a>`);
  parts.push(`<section class="doc">${html}</section>`);
}

const css = `
  @page { size: A4; margin: 18mm 16mm 20mm 16mm; }
  body { font-family: "Liberation Sans", Arial, Helvetica, sans-serif; font-size: 10.5pt; line-height: 1.45; color: #16191f; }
  section.doc { page-break-before: always; }
  section.doc:first-child { page-break-before: auto; }
  h1 { font-size: 22pt; margin: 0 0 10pt; border-bottom: 2px solid #0972d3; padding-bottom: 4pt; }
  h2 { font-size: 15pt; margin: 18pt 0 6pt; color: #0972d3; page-break-after: avoid; }
  h3 { font-size: 12pt; margin: 14pt 0 4pt; page-break-after: avoid; }
  p, li { orphans: 3; widows: 3; }
  img { max-width: 100%; height: auto; display: block; margin: 8pt auto; border: 1px solid #d5dbdb; page-break-inside: avoid; }
  table { border-collapse: collapse; width: 100%; margin: 6pt 0 10pt; font-size: 9.5pt; page-break-inside: auto; }
  th, td { border: 1px solid #c8cdd3; padding: 3pt 6pt; vertical-align: top; text-align: left; }
  th { background: #f2f3f3; }
  tr { page-break-inside: avoid; }
  code { font-family: "Liberation Mono", Menlo, monospace; font-size: 9pt; background: #f2f3f3; padding: 0 3px; border-radius: 2px; }
  pre { background: #f2f3f3; padding: 8pt; font-size: 8.5pt; white-space: pre-wrap; word-break: break-all; }
  a { color: #0972d3; text-decoration: none; }
  blockquote { border-left: 3px solid #0972d3; margin: 6pt 0; padding: 2pt 10pt; color: #414d5c; }
  .cover { text-align: center; padding-top: 220px; }
  .cover h1 { border: none; font-size: 34pt; }
  .cover p { font-size: 13pt; color: #414d5c; }
`;
const today = new Date().toISOString().slice(0, 10);
const cover = `<section class="cover"><h1>Defect Detection Application</h1><p>User Guide</p><p>Portal, device web UI and Quality Station HMI</p><p style="margin-top:60px;font-size:10pt">Generated ${today} from docs/user-guide</p></section>`;
const html = `<!doctype html><html><head><meta charset="utf-8"><title>DDA User Guide</title><style>${css}</style></head><body>${cover}${parts.join('\n')}</body></html>`;
const htmlPath = path.join(GUIDE, '.user-guide.tmp.html');
fs.writeFileSync(htmlPath, html);

(async () => {
  const br = await chromium.launch();
  const page = await br.newPage();
  await page.goto('file://' + htmlPath, { waitUntil: 'load' });
  await page.waitForTimeout(1500);
  await page.pdf({
    path: OUT,
    format: 'A4',
    printBackground: true,
    displayHeaderFooter: true,
    headerTemplate: '<div></div>',
    footerTemplate: '<div style="font-size:8pt;color:#687078;width:100%;text-align:center;font-family:Arial">DDA User Guide · page <span class="pageNumber"></span> of <span class="totalPages"></span></div>',
    margin: { top: '18mm', right: '16mm', bottom: '20mm', left: '16mm' },
  });
  await br.close();
  fs.unlinkSync(htmlPath);
  console.log('wrote', OUT);
})();
