const express = require('express');
const fs = require('fs');
const os = require('os');
const path = require('path');
const { marked } = require('marked');

function expandHome(p) {
  if (!p) return p;
  if (p === '~') return os.homedir();
  if (p.startsWith('~/')) return path.join(os.homedir(), p.slice(2));
  return p;
}

function loadConfig() {
  const configPath = process.env.MARKDOWN_SERVER_CONFIG
    || path.join(os.homedir(), '.config', 'markdown-server', 'config.json');
  if (fs.existsSync(configPath)) {
    try {
      return { ...JSON.parse(fs.readFileSync(configPath, 'utf8')), _path: configPath };
    } catch (e) {
      console.error(`Failed to parse config at ${configPath}: ${e.message}`);
      process.exit(1);
    }
  }
  return { _path: null };
}

const config = loadConfig();
const app = express();
const PORT = process.env.PORT || config.port || 3000;
const CONTENT_DIR = path.resolve(
  expandHome(process.env.CONTENT_DIR || config.contentDir || path.join(__dirname, 'content'))
);

if (!fs.existsSync(CONTENT_DIR)) fs.mkdirSync(CONTENT_DIR, { recursive: true });

function escapeHtml(s) {
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

marked.use({
  gfm: true,
  renderer: {
    code(codeOrToken, infoString) {
      // marked v12 calls (code, infostring, escaped); v15+ passes a token object.
      const text = typeof codeOrToken === 'object' ? codeOrToken.text : codeOrToken;
      const lang = typeof codeOrToken === 'object' ? codeOrToken.lang : (infoString || '').split(/\s+/)[0];
      if (lang === 'mermaid') {
        return `<pre class="mermaid">${escapeHtml(text)}</pre>`;
      }
      if (lang === 'drawio') {
        const config = {
          highlight: '#5b4636',
          nav: true,
          resize: true,
          toolbar: 'zoom layers lightbox',
          edit: '_blank',
          xml: text,
        };
        const attr = escapeHtml(JSON.stringify(config));
        return `<div class="mxgraph" data-mxgraph="${attr}"></div>`;
      }
      return false;
    },
  },
});

function safePath(rel) {
  if (!rel) return CONTENT_DIR;
  const decoded = decodeURIComponent(rel);
  const full = path.resolve(CONTENT_DIR, decoded);
  if (full !== CONTENT_DIR && !full.startsWith(CONTENT_DIR + path.sep)) return null;
  return full;
}

function buildFileIndex() {
  const byPath = new Map();    // "jovialiki/wiki/entities/kerala" -> "jovialiki/wiki/entities/kerala.md"
  const byBasename = new Map(); // "kerala" -> ["jovialiki/wiki/entities/kerala.md", ...]
  function walk(dir, prefix = '') {
    for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
      if (entry.name.startsWith('.')) continue;
      const rel = prefix ? `${prefix}/${entry.name}` : entry.name;
      const full = path.join(dir, entry.name);
      if (entry.isDirectory()) walk(full, rel);
      else if (entry.name.endsWith('.md')) {
        byPath.set(rel.replace(/\.md$/, ''), rel);
        const base = entry.name.replace(/\.md$/, '');
        if (!byBasename.has(base)) byBasename.set(base, []);
        byBasename.get(base).push(rel);
      }
    }
  }
  walk(CONTENT_DIR);
  return { byPath, byBasename };
}

function resolveWikilinkTarget(target, currentDir, index) {
  if (index.byPath.has(target)) return index.byPath.get(target);

  // Walk up from current dir, trying to anchor the target there (Obsidian-style vault search)
  let dir = currentDir;
  while (true) {
    const candidate = dir ? `${dir}/${target}` : target;
    if (index.byPath.has(candidate)) return index.byPath.get(candidate);
    if (!dir) break;
    const slash = dir.lastIndexOf('/');
    dir = slash === -1 ? '' : dir.slice(0, slash);
  }

  // Basename fallback for targets without slashes
  if (!target.includes('/')) {
    const matches = index.byBasename.get(target);
    if (matches && matches.length) {
      if (matches.length === 1) return matches[0];
      // Prefer the file that shares the most leading path segments with currentDir
      const cParts = currentDir.split('/');
      const score = (p) => {
        const parts = p.split('/');
        let i = 0;
        while (i < parts.length - 1 && i < cParts.length && parts[i] === cParts[i]) i++;
        return -i;
      };
      return [...matches].sort((a, b) => score(a) - score(b))[0];
    }
  }
  return null;
}

function slugifyAnchor(s) {
  return s.toLowerCase().trim().replace(/[^\w\- ]+/g, '').replace(/\s+/g, '-');
}

function preprocessLinks(md, currentFileRel) {
  const index = buildFileIndex();
  const currentDir = currentFileRel.includes('/')
    ? currentFileRel.replace(/\/[^/]+$/, '')
    : '';

  // Skip code fences when substituting wikilinks
  const lines = md.split('\n');
  let inFence = false;
  for (let i = 0; i < lines.length; i++) {
    if (/^\s*```/.test(lines[i])) { inFence = !inFence; continue; }
    if (inFence) continue;
    lines[i] = lines[i].replace(
      /\[\[([^\]|#]+)(?:#([^\]|]+))?(?:\|([^\]]+))?\]\]/g,
      (_, target, heading, alias) => {
        target = target.trim();
        const display = (alias || target.split('/').pop()).trim();
        const resolved = resolveWikilinkTarget(target, currentDir, index);
        if (!resolved) {
          return `<span class="broken-link" title="No file matching &quot;${escapeHtml(target)}&quot;">${escapeHtml(display)}</span>`;
        }
        const url = '/view/' + encodeURI(resolved) + (heading ? '#' + slugifyAnchor(heading) : '');
        return `[${display}](${url})`;
      }
    );
  }
  return lines.join('\n');
}

function buildTree(dir, prefix = '') {
  const entries = fs.readdirSync(dir, { withFileTypes: true })
    .filter(e => !e.name.startsWith('.'))
    .sort((a, b) => {
      if (a.isDirectory() !== b.isDirectory()) return a.isDirectory() ? -1 : 1;
      return a.name.localeCompare(b.name);
    });

  const items = [];
  for (const entry of entries) {
    const rel = prefix ? `${prefix}/${entry.name}` : entry.name;
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) {
      items.push({ type: 'dir', name: entry.name, path: rel, children: buildTree(full, rel) });
    } else if (entry.name.endsWith('.md')) {
      items.push({ type: 'file', name: entry.name, path: rel });
    }
  }
  return items;
}

function renderTree(nodes, activePath) {
  if (!nodes.length) return '<div class="empty">No markdown files</div>';
  const html = nodes.map(node => {
    if (node.type === 'dir') {
      const isAncestor = activePath && (activePath === node.path || activePath.startsWith(node.path + '/'));
      return `<details class="dir" data-path="${escapeHtml(node.path)}"${isAncestor ? ' open' : ''}>
        <summary><span class="caret"></span><span class="folder-icon"></span>${escapeHtml(node.name)}</summary>
        <div class="dir-children">${renderTree(node.children, activePath)}</div>
      </details>`;
    }
    const active = node.path === activePath ? ' active' : '';
    return `<a class="file${active}" href="/view/${encodeURI(node.path)}"><span class="file-icon"></span>${escapeHtml(node.name)}</a>`;
  }).join('');
  return html;
}

const PAGE = ({ title, body, activePath }) => {
  const tree = buildTree(CONTENT_DIR);
  const sidebar = renderTree(tree, activePath);
  return `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>${escapeHtml(title)}</title>
<style>
  :root {
    --bg: #f4ecd8;
    --bg-alt: #ebe1c5;
    --bg-deep: #e4d8b4;
    --ink: #5b4636;
    --ink-soft: #7a6650;
    --ink-faint: #a08c70;
    --accent: #8b5a2b;
    --rule: #d9caa3;
    --code-bg: #ebe1c5;
  }
  * { box-sizing: border-box; }
  html, body { margin: 0; padding: 0; height: 100%; }
  body {
    background: var(--bg);
    color: var(--ink);
    font-family: "Iowan Old Style", "Palatino Linotype", Palatino, Georgia, serif;
    font-size: 17px;
    line-height: 1.65;
  }
  .layout { display: flex; min-height: 100vh; }

  /* Sidebar */
  .sidebar {
    width: 280px;
    flex-shrink: 0;
    background: var(--bg-alt);
    border-right: 1px solid var(--rule);
    overflow-y: auto;
    overflow-x: hidden;
    padding: 1.25rem 0.75rem;
    position: sticky;
    top: 0;
    height: 100vh;
    font-size: 14px;
    font-family: -apple-system, system-ui, "Segoe UI", sans-serif;
    min-width: 40px;
    max-width: 600px;
  }
  .sidebar.collapsed {
    width: 40px !important;
    min-width: 40px;
    padding: 0.75rem 0;
    overflow: hidden;
  }
  .sidebar.collapsed .sidebar-header {
    justify-content: center;
    padding: 0.5rem 0;
    border-bottom: none;
  }
  .sidebar.collapsed .sidebar-title,
  .sidebar.collapsed nav.tree { display: none; }

  .sidebar-header {
    padding: 0 0.5rem 0.75rem;
    margin-bottom: 0.5rem;
    border-bottom: 1px solid var(--rule);
    display: flex;
    align-items: center;
    justify-content: space-between;
  }
  .sidebar-title { color: var(--ink); text-decoration: none; font-weight: 600; white-space: nowrap; overflow: hidden; }
  .sidebar-toggle {
    display: none;
    background: none; border: none; cursor: pointer; color: var(--ink); font-size: 1.2rem;
  }
  .sidebar-collapse-btn {
    background: none; border: none; cursor: pointer;
    color: var(--ink-soft); font-size: 0.95rem;
    padding: 0.2rem 0.35rem; border-radius: 3px;
    line-height: 1; flex-shrink: 0;
  }
  .sidebar-collapse-btn:hover { background: var(--bg-deep); color: var(--ink); }

  .sidebar-resizer {
    width: 5px;
    flex-shrink: 0;
    cursor: col-resize;
    background: transparent;
    transition: background 0.15s;
    position: relative;
    z-index: 1;
  }
  .sidebar-resizer:hover, .sidebar-resizer.dragging { background: var(--accent); opacity: 0.5; }
  .sidebar-resizer.hidden { pointer-events: none; opacity: 0; }

  details.dir { margin: 0; }
  details.dir > summary {
    list-style: none;
    cursor: pointer;
    padding: 0.3rem 0.5rem;
    border-radius: 4px;
    display: flex;
    align-items: center;
    color: var(--ink);
    user-select: none;
  }
  details.dir > summary::-webkit-details-marker { display: none; }
  details.dir > summary:hover { background: var(--bg-deep); }
  .caret {
    display: inline-block;
    width: 0; height: 0;
    border-left: 5px solid var(--ink-soft);
    border-top: 4px solid transparent;
    border-bottom: 4px solid transparent;
    margin-right: 6px;
    transition: transform 0.15s;
  }
  details.dir[open] > summary > .caret { transform: rotate(90deg); }
  .folder-icon::before { content: "📁"; margin-right: 6px; opacity: 0.8; }
  details.dir[open] > summary > .folder-icon::before { content: "📂"; }
  .file-icon::before { content: "📄"; margin-right: 6px; opacity: 0.65; }
  .dir-children { margin-left: 0.85rem; border-left: 1px dashed var(--rule); padding-left: 0.4rem; }

  a.file {
    display: flex;
    align-items: center;
    padding: 0.3rem 0.5rem;
    border-radius: 4px;
    color: var(--ink);
    text-decoration: none;
  }
  a.file:hover { background: var(--bg-deep); }
  a.file.active {
    background: var(--accent);
    color: var(--bg);
  }
  a.file.active .file-icon { filter: brightness(2); }

  .empty { padding: 1rem 0.5rem; color: var(--ink-faint); font-style: italic; }

  /* Main content */
  .content {
    flex: 1;
    padding: 2.5rem 3rem;
    max-width: 1200px;
    margin: 0 auto;
    width: 100%;
  }
  .content h1, .content h2, .content h3, .content h4 {
    color: var(--ink);
    font-family: "Iowan Old Style", Georgia, serif;
    line-height: 1.25;
    margin-top: 1.6em;
    margin-bottom: 0.5em;
  }
  .content h1 { border-bottom: 1px solid var(--rule); padding-bottom: 0.3em; }
  .content a { color: var(--accent); }
  .content hr { border: none; border-top: 1px solid var(--rule); margin: 2em 0; }
  .content a.broken-link, .content .broken-link {
    color: var(--ink-faint);
    text-decoration: line-through;
    cursor: help;
  }
  .content blockquote {
    border-left: 3px solid var(--accent);
    margin: 1em 0;
    padding: 0.2em 1em;
    color: var(--ink-soft);
    background: var(--bg-alt);
  }
  .content table { border-collapse: collapse; margin: 1em 0; }
  .content th, .content td { border: 1px solid var(--rule); padding: 0.5em 0.9em; }
  .content th { background: var(--bg-alt); }
  .content img { max-width: 100%; }

  code {
    background: var(--code-bg);
    padding: 0.1em 0.35em;
    border-radius: 3px;
    font-family: ui-monospace, "SF Mono", Menlo, Consolas, monospace;
    font-size: 0.9em;
  }
  pre {
    background: var(--code-bg);
    padding: 1em;
    border-radius: 5px;
    overflow-x: auto;
    border: 1px solid var(--rule);
  }
  pre code { background: none; padding: 0; font-size: 0.88em; }

  pre.mermaid, .mxgraph {
    background: var(--bg);
    border: 1px solid var(--rule);
    border-radius: 5px;
    padding: 1em;
    text-align: center;
    overflow-x: auto;
  }
  pre.mermaid { font-family: ui-monospace, monospace; }
  pre.mermaid[data-processed="true"] { cursor: zoom-in; }
  pre.mermaid[data-processed="true"]:hover { box-shadow: 0 0 0 2px var(--accent); }

  /* Diagram modal */
  .diagram-modal {
    display: none;
    position: fixed;
    inset: 0;
    z-index: 1000;
    background: rgba(60, 40, 20, 0.85);
    align-items: center;
    justify-content: center;
    padding: 2rem;
    cursor: zoom-out;
  }
  .diagram-modal.open { display: flex; }
  .diagram-modal-inner {
    background: var(--bg);
    border-radius: 6px;
    padding: 2.5rem 1.5rem 1.5rem;
    width: 96vw;
    height: 96vh;
    cursor: default;
    position: relative;
    box-shadow: 0 10px 40px rgba(0,0,0,0.4);
    display: flex;
    align-items: center;
    justify-content: center;
  }
  .diagram-modal-content {
    width: 100%;
    height: 100%;
    display: flex;
    align-items: center;
    justify-content: center;
  }
  .diagram-modal-content svg {
    width: 100% !important;
    height: 100% !important;
    max-width: none !important;
    max-height: none !important;
    display: block;
  }
  .diagram-modal-close {
    position: absolute;
    top: 0.5rem; right: 0.75rem;
    background: none; border: none;
    font-size: 1.6rem; line-height: 1;
    color: var(--ink-soft); cursor: pointer;
    padding: 0.25rem 0.5rem;
  }
  .diagram-modal-close:hover { color: var(--accent); }

  .index-list { list-style: none; padding: 0; }
  .index-list li { padding: 0.4em 0; }

  @media (max-width: 800px) {
    .sidebar {
      position: fixed;
      left: 0; top: 0;
      transform: translateX(-100%);
      transition: transform 0.2s;
      z-index: 10;
      box-shadow: 2px 0 8px rgba(0,0,0,0.1);
    }
    .sidebar.open { transform: translateX(0); }
    .sidebar-toggle { display: block; }
    .content { padding: 1.5rem; }
    .mobile-bar {
      display: flex; align-items: center; gap: 0.6rem;
      padding: 0.5rem 1rem;
      background: var(--bg-alt);
      border-bottom: 1px solid var(--rule);
      position: sticky; top: 0; z-index: 5;
    }
  }
  @media (min-width: 801px) { .mobile-bar { display: none; } }
</style>
</head>
<body>
<div class="mobile-bar">
  <button class="sidebar-toggle" aria-label="Toggle sidebar" onclick="document.querySelector('.sidebar').classList.toggle('open')">☰</button>
  <strong>${escapeHtml(title)}</strong>
</div>
<div class="diagram-modal" id="diagram-modal" role="dialog" aria-modal="true">
  <div class="diagram-modal-inner">
    <button class="diagram-modal-close" aria-label="Close">×</button>
    <div class="diagram-modal-content"></div>
  </div>
</div>
<div class="layout">
  <aside class="sidebar" id="sidebar">
    <div class="sidebar-header">
      <a class="sidebar-title" href="/">📚 Library</a>
      <button class="sidebar-collapse-btn" id="sidebar-collapse-btn" title="Collapse sidebar">◀</button>
    </div>
    <nav class="tree">${sidebar}</nav>
  </aside>
  <div class="sidebar-resizer" id="sidebar-resizer"></div>
  <main class="content">
    ${body}
  </main>
</div>
<script type="module">
  import mermaid from 'https://cdn.jsdelivr.net/npm/mermaid@10/dist/mermaid.esm.min.mjs';
  mermaid.initialize({
    startOnLoad: true,
    theme: 'base',
    themeVariables: {
      background: '#5e4d23',
      primaryColor: '#d6cbae',
      primaryTextColor: '#5b4636',
      primaryBorderColor: '#8b5a2b',
      lineColor: '#7a6650',
      secondaryColor: '#e4d8b4',
      tertiaryColor: '#f4ecd8',
      fontFamily: 'Iowan Old Style, Georgia, serif',
    },
  });
</script>
<script src="https://viewer.diagrams.net/js/viewer-static.min.js"></script>
<script>
  // Persist folder open/close state across navigations
  (function() {
    const KEY = 'mdserver:openDirs';
    let open;
    try { open = new Set(JSON.parse(localStorage.getItem(KEY) || '[]')); } catch (e) { open = new Set(); }

    document.querySelectorAll('details.dir').forEach(d => {
      const p = d.dataset.path;
      if (open.has(p)) d.open = true;
      d.addEventListener('toggle', () => {
        if (d.open) open.add(p); else open.delete(p);
        localStorage.setItem(KEY, JSON.stringify([...open]));
      });
    });
  })();

  // Sidebar: collapse toggle + drag-to-resize
  (function() {
    const COLLAPSED_KEY = 'mdserver:sidebarCollapsed';
    const WIDTH_KEY = 'mdserver:sidebarWidth';
    const sidebar = document.getElementById('sidebar');
    const resizer = document.getElementById('sidebar-resizer');
    const btn = document.getElementById('sidebar-collapse-btn');

    function setCollapsed(yes) {
      if (yes) {
        sidebar.classList.add('collapsed');
        btn.textContent = '▶';
        btn.title = 'Expand sidebar';
        resizer.classList.add('hidden');
      } else {
        sidebar.classList.remove('collapsed');
        btn.textContent = '◀';
        btn.title = 'Collapse sidebar';
        resizer.classList.remove('hidden');
      }
      localStorage.setItem(COLLAPSED_KEY, yes ? '1' : '0');
    }

    // Restore saved state
    const savedWidth = parseInt(localStorage.getItem(WIDTH_KEY), 10);
    if (savedWidth && savedWidth >= 150 && savedWidth <= 600) sidebar.style.width = savedWidth + 'px';
    if (localStorage.getItem(COLLAPSED_KEY) === '1') setCollapsed(true);

    btn.addEventListener('click', () => setCollapsed(!sidebar.classList.contains('collapsed')));

    // Drag to resize
    let dragging = false, startX = 0, startW = 0;
    resizer.addEventListener('mousedown', e => {
      if (sidebar.classList.contains('collapsed')) return;
      dragging = true;
      startX = e.clientX;
      startW = sidebar.getBoundingClientRect().width;
      resizer.classList.add('dragging');
      document.body.style.cursor = 'col-resize';
      document.body.style.userSelect = 'none';
      e.preventDefault();
    });
    document.addEventListener('mousemove', e => {
      if (!dragging) return;
      const w = Math.min(600, Math.max(150, startW + (e.clientX - startX)));
      sidebar.style.width = w + 'px';
    });
    document.addEventListener('mouseup', () => {
      if (!dragging) return;
      dragging = false;
      resizer.classList.remove('dragging');
      document.body.style.cursor = '';
      document.body.style.userSelect = '';
      localStorage.setItem(WIDTH_KEY, Math.round(sidebar.getBoundingClientRect().width));
    });
  })();

  // Click-to-fullscreen modal for mermaid diagrams
  (function() {
    const modal = document.getElementById('diagram-modal');
    const content = modal.querySelector('.diagram-modal-content');
    const closeBtn = modal.querySelector('.diagram-modal-close');

    function open(svg) {
      content.innerHTML = '';
      const clone = svg.cloneNode(true);
      // Strip intrinsic sizing so CSS can scale the SVG up to fill the modal.
      clone.removeAttribute('width');
      clone.removeAttribute('height');
      clone.removeAttribute('style');
      if (!clone.getAttribute('preserveAspectRatio')) {
        clone.setAttribute('preserveAspectRatio', 'xMidYMid meet');
      }
      content.appendChild(clone);
      modal.classList.add('open');
      document.body.style.overflow = 'hidden';
    }
    function close() {
      modal.classList.remove('open');
      content.innerHTML = '';
      document.body.style.overflow = '';
    }

    // Click outside inner panel closes; clicks inside don't bubble
    modal.addEventListener('click', (e) => { if (e.target === modal) close(); });
    closeBtn.addEventListener('click', close);
    document.addEventListener('keydown', (e) => { if (e.key === 'Escape' && modal.classList.contains('open')) close(); });

    // Mermaid renders asynchronously after page load; poll briefly to attach handlers
    function attach() {
      document.querySelectorAll('pre.mermaid[data-processed="true"]').forEach(pre => {
        if (pre.dataset.modalBound) return;
        pre.dataset.modalBound = '1';
        pre.addEventListener('click', () => {
          const svg = pre.querySelector('svg');
          if (svg) open(svg);
        });
      });
    }
    const obs = new MutationObserver(attach);
    obs.observe(document.body, { subtree: true, attributes: true, attributeFilter: ['data-processed'] });
    attach();
  })();
</script>
</body>
</html>`;
};

app.get('/', (req, res) => {
  const tree = buildTree(CONTENT_DIR);
  const flatten = (nodes, out = []) => {
    for (const n of nodes) {
      if (n.type === 'file') out.push(n.path);
      else flatten(n.children, out);
    }
    return out;
  };
  const files = flatten(tree);
  const body = `
    <h1>📚 Markdown Library</h1>
    <p>Welcome. Choose a document from the tree on the left, or pick one below.</p>
    <ul class="index-list">
      ${files.map(f => `<li>📄 <a href="/view/${encodeURI(f)}">${escapeHtml(f)}</a></li>`).join('')}
    </ul>
    ${files.length === 0 ? '<p><em>No markdown files yet — drop some <code>.md</code> files into the content directory.</em></p>' : ''}
  `;
  res.send(PAGE({ title: 'Library', body, activePath: null }));
});

app.get('/view/*', (req, res) => {
  const rel = req.params[0];
  const full = safePath(rel);
  if (!full || !fs.existsSync(full) || !fs.statSync(full).isFile()) {
    return res.status(404).send(PAGE({ title: 'Not Found', body: '<h1>Not found</h1>', activePath: null }));
  }
  const md = fs.readFileSync(full, 'utf8');
  const decodedRel = decodeURIComponent(rel);
  const processed = preprocessLinks(md, decodedRel);
  const html = marked.parse(processed);
  res.send(PAGE({ title: rel, body: html, activePath: rel }));
});

app.get('/raw/*', (req, res) => {
  const full = safePath(req.params[0]);
  if (!full || !fs.existsSync(full) || !fs.statSync(full).isFile()) return res.status(404).end();
  res.sendFile(full);
});

app.listen(PORT, () => {
  console.log(`Markdown server on http://localhost:${PORT}`);
  console.log(`Serving content from: ${CONTENT_DIR}`);
  if (config._path) console.log(`Config loaded from: ${config._path}`);
});
