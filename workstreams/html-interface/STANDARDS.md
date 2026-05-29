# HTML Canon Standards

These standards apply to every HTML file Lobster generates for Dan — bisque-hosted documents, reports, design docs, dashboards, and interactive tools.

**Updated:** 2026-05-24. Canonicalized with learnings from generative-resistance.html v2.1 and WOS Dashboard v5.

---

## 0. Global Desires (What Makes HTML Artifacts Valuable to Dan)

Before applying specific patterns, understand what Dan values in an HTML artifact:

1. **Dark-first, readable typography.** The default palette is dark (`--bg: #0b0d14`). Light mode is available on demand but dark is the primary reading context.
2. **Structured navigation.** Sections are numbered taxonomically (§1, §2, §1.1) so Dan can orient quickly and reference by ID in conversation ("the §3.2 framing").
3. **Clipboard-first feedback loop.** For document-class artifacts, Dan reads and annotates inline; Copy Comments assembles his notes in `[§1] text | [§2] text` format for pasting back into Telegram.
4. **Bisque hosting, not local paths.** Every HTML artifact is served at `http://5.78.201.64:9101/files/<filename>`. Links sent to Dan must be bisque URLs, never filesystem paths.
5. **Interactive vocabulary/concept graphs.** When a document introduces a taxonomy or vocabulary system (concepts with relationships), a D3.js force-directed network is the canonical way to render it. This is established form — not a novelty to be justified each time.
6. **Self-contained.** No external fonts, no CDN dependencies except D3.js (from `https://d3js.org/d3.v7.min.js`). All CSS and JS inline.
7. **Version metadata.** Include `<meta name="doc-version" content="X.Y">` and `<meta name="doc-updated" content="ISO8601 timestamp">` in the head.
8. **Visible version stamp.** Every HTML file must include a visible `<div class="version-stamp">` in the page header area (below the title) showing the version and updated timestamp. See §12 below.

---

## 1. HTML Class Distinction: Document vs. Dashboard

Every HTML artifact is one of two classes. The class determines which features apply.

### Document-class

**Use for:** reports, design docs, audits, philosophy/synthesis docs, structured presentations, UX enumerations, proposals.

Features:
- Light/dark toggle (required)
- Taxonomic section IDs (required)
- Clipboard comment widget (required — place above footer)
- Bisque URL in send_reply (required)
- Canon badge in header (optional, for documents that define a canonical position)
- D3.js vocabulary network (use whenever document introduces a taxonomy with defined relationships)
- `doc-version` and `doc-updated` meta tags

### Dashboard-class

**Use for:** WOS dashboard, interactive tools with live data or action buttons.

Features:
- Light/dark toggle (apply if dashboard has readable sections)
- Taxonomic section IDs (apply if dashboard has readable sections)
- Clipboard comment widget: **omit by default** — dashboards have their own interaction surface (action buttons, filter bars, drilldown links). Add only if Dan explicitly requests precision feedback capability.
- Filter bars, status badges, action buttons: include as needed per dashboard requirements
- `@media (prefers-color-scheme: dark)` alternative to toggle: acceptable in dashboards (v5 pattern)
- CC quota widget: include in WOS dashboard (top of page, compact inline display)

---

## 2. Color Token System

Dark mode is the default. Light mode is a CSS class override.

```css
:root {
  --bg: #0b0d14;
  --surface: #141720;
  --surface2: #1c2030;
  --surface3: #232840;
  --border: #2a2f45;
  --border2: #363d5a;
  --text: #e4e6f0;
  --text2: #9aa0bb;
  --text3: #5c6480;
  --accent: #6ea8fe;
  --accent2: #a78bfa;
  --accent3: #34d399;
  --warn: #fb923c;
}

body.light-mode {
  --bg: #f8f9fb;
  --surface: #ffffff;
  --surface2: #f0f2f7;
  --surface3: #e8ecf4;
  --border: #dde1ee;
  --border2: #c8cedf;
  --text: #1a1d2e;
  --text2: #4a5068;
  --text3: #8890a8;
  --accent: #2563eb;
  --accent2: #7c3aed;
  --accent3: #059669;
  --warn: #ea580c;
}
```

Note: Light mode overrides the accent colors to higher-contrast values (blue at `#2563eb`, not `#6ea8fe`) so text remains legible against the light surface.

---

## 3. Light/Dark Mode Toggle

Place a toggle button fixed in the top-right corner. Persists via `localStorage`.

### CSS

```css
#theme-toggle {
  position: fixed;
  top: 14px;
  right: 18px;
  z-index: 1000;
  background: var(--surface2);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 6px 12px;
  font-size: 12px;
  font-weight: 600;
  color: var(--text2);
  cursor: pointer;
  transition: background .15s, color .15s;
}
#theme-toggle:hover { background: var(--surface3); color: var(--text); }
```

### HTML (just before `</body>`)

```html
<button id="theme-toggle" onclick="toggleTheme()">&#9788; Light</button>
```

### JS

```js
function toggleTheme() {
  const isLight = document.body.classList.toggle('light-mode');
  localStorage.setItem('theme', isLight ? 'light' : 'dark');
  document.getElementById('theme-toggle').textContent = isLight ? '☾ Dark' : '☀ Light';
}
(function() {
  if (localStorage.getItem('theme') === 'light') {
    document.body.classList.add('light-mode');
    const btn = document.getElementById('theme-toggle');
    if (btn) btn.textContent = '☾ Dark';
  }
})();
```

---

## 4. Taxonomic Section IDs

Every major section gets a short, stable numeric ID displayed in the rendered HTML.

### Convention

- Top-level sections: `§1`, `§2`, `§3`, …
- Subsections: `§1.1`, `§1.2`, `§2.1`, …
- Sub-subsections: `§1.1.1`, `§1.1.2`, …

### HTML

```html
<div class="section" id="s1">
  <div class="section-header">
    <span class="section-id">§1</span> Section Title
  </div>
  <!-- content -->
</div>

<div class="section" id="s1-1">
  <div class="subsection-header">
    <span class="subsection-id">§1.1</span> Subsection Title
  </div>
</div>
```

### CSS

```css
.section { margin-bottom: 56px; }
.section-header {
  font-size: 12px;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: .1em;
  color: var(--text2);
  margin-bottom: 20px;
  padding-bottom: 10px;
  border-bottom: 1px solid var(--border);
  display: flex;
  align-items: center;
  gap: 8px;
}
.section-id {
  display: inline-block;
  font-size: 11px;
  font-weight: 700;
  color: var(--text3);
  font-family: 'SF Mono', 'Fira Code', monospace;
  user-select: none;
  flex-shrink: 0;
}
.subsection { margin-bottom: 32px; }
.subsection-header {
  font-size: 13px;
  font-weight: 700;
  color: var(--text);
  margin-bottom: 12px;
  display: flex;
  align-items: baseline;
  gap: 8px;
}
.subsection-id {
  font-size: 10px;
  font-weight: 700;
  font-family: 'SF Mono', 'Fira Code', monospace;
  color: var(--text3);
  user-select: none;
}
```

---

## 5. Clipboard Comment Widget (Document-class only)

Place at the bottom of every document-class artifact, above the footer. One text input per major section. The "Copy comments" button assembles `[§1] text | [§2] text` and writes to clipboard. No API calls, no backend.

### HTML

```html
<!-- COMMENT WIDGET -->
<div class="comment-widget" id="comment-widget">
  <div class="comment-widget-title">Section Comments</div>
  <div class="comment-inputs" id="comment-inputs">
    <!-- populated by JS from sections[] -->
  </div>
  <button class="copy-comments-btn" onclick="copyComments()">Copy comments</button>
  <div class="copy-feedback" id="copy-feedback"></div>
</div>
```

### CSS

```css
.comment-widget {
  margin-top: 48px;
  padding: 20px 24px;
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 10px;
}
.comment-widget-title {
  font-size: 11px;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: .09em;
  color: var(--text3);
  margin-bottom: 14px;
}
.comment-row { display: flex; align-items: center; gap: 10px; margin-bottom: 8px; }
.comment-label {
  font-size: 11px;
  font-weight: 700;
  font-family: 'SF Mono', 'Fira Code', monospace;
  color: var(--text3);
  min-width: 36px;
}
.comment-input {
  flex: 1;
  background: var(--surface2);
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 6px 10px;
  font-size: 12px;
  color: var(--text);
  outline: none;
}
.comment-input:focus { border-color: var(--accent); }
.copy-comments-btn {
  margin-top: 12px;
  padding: 8px 18px;
  background: var(--accent);
  color: #fff;
  border: none;
  border-radius: 6px;
  font-size: 12px;
  font-weight: 700;
  cursor: pointer;
  transition: opacity .15s;
}
.copy-comments-btn:hover { opacity: .85; }
.copy-feedback { margin-top: 8px; font-size: 11px; color: var(--accent3); min-height: 16px; }
```

### JS (define `sections` to match the document)

```js
const sections = [
  { id: 's1', label: '§1' },
  { id: 's2', label: '§2' },
  { id: 's3', label: '§3' },
  // add more as needed
];

(function buildCommentWidget() {
  const container = document.getElementById('comment-inputs');
  if (!container) return;
  sections.forEach(function(s) {
    const row = document.createElement('div');
    row.className = 'comment-row';
    row.innerHTML =
      '<span class="comment-label">' + s.label + '</span>' +
      '<input class="comment-input" type="text" id="ci-' + s.id +
      '" placeholder="Comment on ' + s.label + '…">';
    container.appendChild(row);
  });
})();

function copyText(text) {
  if (navigator.clipboard && window.isSecureContext) {
    navigator.clipboard.writeText(text).catch(function() { fallbackCopy(text); });
  } else {
    fallbackCopy(text);
  }
}
function fallbackCopy(text) {
  var el = document.createElement('textarea');
  el.value = text;
  el.style.position = 'fixed'; el.style.opacity = '0';
  document.body.appendChild(el);
  el.focus(); el.select();
  try { document.execCommand('copy'); }
  catch(e) { alert('Copy failed — paste manually:\n\n' + text); }
  document.body.removeChild(el);
}

function copyComments() {
  const parts = sections
    .map(function(s) {
      const val = (document.getElementById('ci-' + s.id) || {}).value || '';
      return val.trim() ? '[' + s.label + '] ' + val.trim() : null;
    })
    .filter(Boolean);
  if (!parts.length) {
    document.getElementById('copy-feedback').textContent = 'Nothing to copy.';
    return;
  }
  copyText(parts.join(' | '));
  document.getElementById('copy-feedback').textContent = 'Copied to clipboard.';
  setTimeout(function() {
    document.getElementById('copy-feedback').textContent = '';
  }, 2500);
}
```

---

## 6. D3.js Vocabulary / Concept Network (Document-class)

When a document introduces a taxonomy or vocabulary with defined relationships between concepts, use a D3.js force-directed graph. This is the canonical approach — established in generative-resistance.html v2.1. Do not substitute a static diagram or table when relationships between concepts are the point.

### When to use

- The document defines named concepts that relate to each other (parent/child, peer, cross-cutting)
- The vocabulary has more than ~6 terms with non-trivial relationships
- Understanding the system requires seeing the topology, not just the definitions

### 3-tier node model (from generative-resistance v2.1)

Organize nodes into three tiers with distinct visual weight:

| Tier | Role | Visual | Force charge |
|------|------|--------|-------------|
| `register` (top) | Core concepts / categories | Larger circle (r=34), full color stroke | -320 |
| `param` (middle) | Parameters / sub-concepts belonging to one register | Medium circle (r=22), lighter fill | -180 |
| `cross` (bottom) | Cross-cutting measurements / spans multiple registers | Medium circle (r=22), dashed outer ring | -180 |

If the document vocabulary is simpler (no parameterization tier), use just top-level concept nodes with peer links. The 3-tier model is for rich taxonomies.

### Link types

```js
var LINKS = [
  { source: 'ConceptA', target: 'CategoryX', type: 'param' },  // param → its register
  { source: 'CrossMeasure', target: 'CategoryY', type: 'cross' }, // cross → spans
  { source: 'CategoryX', target: 'CategoryZ', type: 'hub' },   // weak adjacency between registers
];
```

### Canonical D3 initialization pattern

```js
// Load D3 from CDN (before closing </body>)
// <script src="https://d3js.org/d3.v7.min.js"></script>

(function initGraph() {
  var COLORS = {
    register: { /* concept → hex color */ },
    param: '#7dd3fc',
    cross: '#86efac',
    link: { param: 'rgba(125,211,252,.45)', cross: 'rgba(134,239,172,.35)', hub: 'rgba(255,255,255,.09)' }
  };

  // NODES: each has { id, tier, register (if param/cross), color, def, examples[] }
  var NODES = [ /* ... */ ];
  // LINKS: each has { source, target, type }
  var LINKS = [ /* ... */ ];

  function initGraph() {
    var container = document.getElementById('vocabulary-graph');
    var width = container.clientWidth || 760;
    var height = 600;

    var svg = d3.select('#vocabulary-graph')
      .attr('viewBox', '0 0 ' + width + ' ' + height)
      .attr('preserveAspectRatio', 'xMidYMid meet');

    // Arrow marker
    svg.append('defs').append('marker')
      .attr('id', 'arrowhead').attr('viewBox', '-0 -5 10 10')
      .attr('refX', 18).attr('refY', 0).attr('orient', 'auto')
      .attr('markerWidth', 6).attr('markerHeight', 6)
      .append('svg:path').attr('d', 'M 0,-5 L 10,0 L 0,5')
      .attr('fill', '#555').style('stroke', 'none');

    var linkG = svg.append('g').attr('class', 'links');
    var nodeG = svg.append('g').attr('class', 'nodes');

    var nodesData = NODES.map(function(n) { return Object.assign({}, n); });
    var linksData = LINKS.map(function(l) { return Object.assign({}, l); });

    var simulation = d3.forceSimulation(nodesData)
      .force('link', d3.forceLink(linksData).id(function(d) { return d.id; })
        .distance(function(d) {
          if (d.type === 'hub') return 130;
          if (d.type === 'param') return 95;
          return 110;
        })
        .strength(function(d) { return d.type === 'hub' ? 0.08 : 0.45; })
      )
      .force('charge', d3.forceManyBody().strength(function(d) {
        return d.tier === 'register' ? -320 : -180;
      }))
      .force('center', d3.forceCenter(width / 2, height / 2))
      .force('collision', d3.forceCollide().radius(function(d) {
        return d.tier === 'register' ? 52 : 38;
      }))
      .force('y', d3.forceY(function(d) {
        if (d.tier === 'register') return height * 0.38;
        if (d.tier === 'param') return height * 0.70;
        return height * 0.82;
      }).strength(function(d) {
        return d.tier === 'register' ? 0.25 : 0.18;
      }));

    var links = linkG.selectAll('line').data(linksData).enter().append('line')
      .attr('stroke', function(d) { return COLORS.link[d.type] || COLORS.link.hub; })
      .attr('stroke-width', function(d) {
        if (d.type === 'hub') return 1;
        if (d.type === 'param') return 1.5;
        return 2;
      })
      .attr('stroke-dasharray', function(d) { return d.type === 'cross' ? '5,3' : null; });

    var nodeGroups = nodeG.selectAll('g.node-group').data(nodesData).enter()
      .append('g').attr('class', 'node-group')
      .style('cursor', 'pointer')
      .call(d3.drag()
        .on('start', function(event, d) {
          if (!event.active) simulation.alphaTarget(0.3).restart();
          d.fx = d.x; d.fy = d.y;
        })
        .on('drag', function(event, d) { d.fx = event.x; d.fy = event.y; })
        .on('end', function(event, d) {
          if (!event.active) simulation.alphaTarget(0);
          d.fx = null; d.fy = null;
        })
      )
      .on('mouseover', function(event, d) { showTooltip(event, d); })
      .on('mousemove', function(event, d) { showTooltip(event, d); })
      .on('mouseout', hideTooltip)
      .on('click', function(event, d) { showTooltip(event, d); event.stopPropagation(); });

    svg.on('click', hideTooltip);

    // Circles
    nodeGroups.append('circle')
      .attr('r', function(d) { return d.tier === 'register' ? 34 : 22; })
      .attr('fill', function(d) { return d.color + (d.tier === 'register' ? '22' : '18'); })
      .attr('stroke', function(d) { return d.color; })
      .attr('stroke-width', function(d) { return d.tier === 'register' ? 2 : 1.5; });

    // Dashed outer ring for cross-register nodes
    nodeGroups.filter(function(d) { return d.tier !== 'register'; })
      .append('circle')
        .attr('r', 22).attr('fill', 'none')
        .attr('stroke', function(d) { return d.color; })
        .attr('stroke-width', 0.5)
        .attr('stroke-dasharray', function(d) { return d.tier === 'cross' ? '3,2' : null; })
        .attr('opacity', 0.4);

    // Labels
    nodeGroups.append('text')
      .attr('text-anchor', 'middle').attr('dominant-baseline', 'central')
      .attr('fill', function(d) { return d.color; })
      .attr('font-size', function(d) { return d.tier === 'register' ? '9.5' : '8'; })
      .attr('font-weight', '700')
      .attr('font-family', 'system-ui, -apple-system, sans-serif')
      .attr('pointer-events', 'none')
      .each(function(d) {
        var el = d3.select(this);
        var words = d.id.split(' ');
        if (words.length === 1 || d.tier === 'register') {
          el.text(d.id.length > 11 ? d.id.slice(0, 10) + '…' : d.id);
        } else {
          el.append('tspan').attr('x', 0).attr('dy', '-0.55em').text(words[0]);
          el.append('tspan').attr('x', 0).attr('dy', '1.1em').text(words.slice(1).join(' '));
        }
      });

    simulation.on('tick', function() {
      links
        .attr('x1', function(d) { return d.source.x; })
        .attr('y1', function(d) { return d.source.y; })
        .attr('x2', function(d) { return d.target.x; })
        .attr('y2', function(d) { return d.target.y; });
      nodeGroups.attr('transform', function(d) {
        return 'translate(' + d.x + ',' + d.y + ')';
      });
    });

    buildLegend();
  }

  // Tooltip: position relative to SVG container, flip if near edge
  function showTooltip(event, d) {
    var tt = document.getElementById('graph-tooltip');
    var html = '<div class="tt-name">' + d.id + '</div>';
    html += '<div class="tt-type" style="color:' + d.color + '">';
    if (d.tier === 'register') html += 'Concept · ' + (d.badge || '');
    else if (d.tier === 'param') html += 'Parameter → ' + d.register;
    else html += 'Cross-register Measurement';
    html += '</div>';
    html += '<div class="tt-def">' + d.def + '</div>';
    if (d.examples && d.examples.length) {
      html += '<div class="tt-examples">';
      d.examples.slice(0, 3).forEach(function(ex) {
        html += '<div class="tt-ex"><strong>' + ex.domain + ':</strong> ' + ex.text + '</div>';
      });
      html += '</div>';
    }
    tt.innerHTML = html;
    tt.classList.add('visible');
    var container = document.getElementById('vocabulary-graph').getBoundingClientRect();
    var mouseX = event.clientX - container.left;
    var mouseY = event.clientY - container.top;
    var ttW = 280, ttH = 200;
    var left = mouseX + 14;
    var top = mouseY - 20;
    if (left + ttW > container.width) left = mouseX - ttW - 14;
    if (top + ttH > container.height) top = container.height - ttH - 10;
    if (top < 4) top = 4;
    tt.style.left = left + 'px';
    tt.style.top = top + 'px';
  }
  function hideTooltip() {
    document.getElementById('graph-tooltip').classList.remove('visible');
  }

  initGraph();
})();
```

### Required HTML structure for graph section

```html
<!-- Graph controls (optional, for tier filtering) -->
<div class="graph-controls">
  <span class="graph-controls-label">Show:</span>
  <button class="graph-btn active" id="filter-all" onclick="setFilter('all')">All layers</button>
  <button class="graph-btn" id="filter-register" onclick="setFilter('register')">Concepts only</button>
  <button class="graph-btn" id="filter-param" onclick="setFilter('param')">+ Parameters</button>
</div>

<!-- SVG target -->
<svg id="vocabulary-graph" style="width:100%;min-height:600px;display:block;background:var(--surface);border-radius:10px;"></svg>

<!-- Tooltip (position:absolute, z-index:100) -->
<div id="graph-tooltip" class="graph-tooltip"></div>

<!-- Legend -->
<div class="graph-legend" id="graph-legend"></div>
```

### Tooltip and graph CSS

```css
.graph-container { position: relative; margin-top: 20px; }
.graph-controls {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 12px;
  flex-wrap: wrap;
}
.graph-controls-label { font-size: 11px; font-weight: 600; color: var(--text3); }
.graph-btn {
  font-size: 11px;
  font-weight: 600;
  padding: 4px 10px;
  border-radius: 6px;
  border: 1px solid var(--border);
  background: var(--surface2);
  color: var(--text2);
  cursor: pointer;
}
.graph-btn.active, .graph-btn:hover { background: var(--surface3); color: var(--text); }
.graph-tooltip {
  position: absolute;
  z-index: 100;
  background: var(--surface);
  border: 1px solid var(--border2);
  border-radius: 10px;
  padding: 12px 14px;
  pointer-events: none;
  max-width: 280px;
  box-shadow: 0 4px 16px rgba(0,0,0,.4);
  display: none;
}
.graph-tooltip.visible { display: block; }
.tt-name { font-size: 13px; font-weight: 700; color: var(--text); margin-bottom: 4px; }
.tt-type { font-size: 10px; font-weight: 600; text-transform: uppercase; letter-spacing: .06em; margin-bottom: 8px; }
.tt-def { font-size: 12px; color: var(--text2); line-height: 1.5; margin-bottom: 8px; }
.tt-examples { font-size: 11px; color: var(--text3); line-height: 1.5; }
.tt-ex { margin-bottom: 4px; }
.tt-ex strong { color: var(--text2); margin-right: 4px; }
.graph-legend { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 12px; }
.legend-item { display: flex; align-items: center; gap: 6px; font-size: 11px; color: var(--text2); }
.legend-dot { width: 10px; height: 10px; border-radius: 50%; flex-shrink: 0; }
```

---

## 7. Document Header Pattern (Document-class)

Every document-class artifact should have a structured header with title, subtitle, and optional canon badge.

```html
<div class="doc-header">
  <!-- Optional: canon badge for docs that establish a canonical position -->
  <div class="canon-badge">Canon Entry — [Domain]</div>
  <div class="doc-title">Document Title</div>
  <div class="doc-subtitle">One-sentence summary of what this document establishes and why it matters.</div>
</div>
```

```css
.doc-header { margin-bottom: 56px; border-bottom: 1px solid var(--border); padding-bottom: 32px; }
.doc-title { font-size: 26px; font-weight: 700; color: var(--text); letter-spacing: -.02em; margin-bottom: 10px; line-height: 1.3; }
.doc-subtitle { font-size: 14px; color: var(--text3); line-height: 1.5; }
.canon-badge {
  display: inline-block;
  font-size: 10px; font-weight: 700;
  text-transform: uppercase; letter-spacing: .1em;
  color: var(--accent3);
  background: rgba(52,211,153,.1);
  border: 1px solid rgba(52,211,153,.25);
  border-radius: 4px;
  padding: 2px 8px;
  margin-bottom: 14px;
}
```

---

## 8. WOS Dashboard Patterns (Dashboard-class)

Established in WOS Dashboard v5. Reference for new dashboard iterations.

### CSS class conventions for dashboard components

```css
/* Status badges */
.badge { display:inline-block; padding:2px 7px; border-radius:10px; font-size:.72rem; font-weight:600; white-space:nowrap; }
.bd { color:var(--done-col); background:var(--done-bg); } /* done */
.bp { color:var(--pend-col); background:var(--pend-bg); } /* pending */
.bf { color:var(--fail-col); background:var(--fail-bg); } /* failed */
.ba { color:var(--act-col);  background:var(--act-bg);  } /* active */
.bc { color:var(--cl-col);   background:var(--cl-bg);   } /* closed */

/* Action buttons (retry / escalate) */
.act-btn {
  display: inline-block; padding: 2px 7px; border-radius: 6px;
  font-size: .7rem; font-weight: 600; text-decoration: none;
  color: var(--accent); border: 1px solid var(--accent);
  margin-right: 3px; white-space: nowrap;
}
.act-btn:hover { background: var(--act-bg); }
.act-esc { color: var(--pend-col); border-color: var(--pend-col); }
.act-esc:hover { background: var(--pend-bg); }

/* Filter bar */
.filter-bar {
  display: flex; align-items: center; gap: 12px; flex-wrap: wrap;
  margin-bottom: 10px; padding: 8px 10px;
  background: var(--surface2); border-radius: 8px; font-size: .8rem;
}
.filter-bar select {
  font-size: .78rem; padding: 2px 6px; border-radius: 5px;
  border: 1px solid var(--border); background: var(--surface); color: var(--text);
}

/* CC quota widget (top of dashboard) */
/* Inline div with strong label + span for quota value */
```

### Action button pattern (Telegram /start callbacks)

Action buttons in the WOS dashboard use Telegram deep-link URLs for in-context actions (retry, escalate). The URL scheme is:

```
https://t.me/LobsterBot?start=<base64-encoded-JSON>
```

Where the JSON is `{"a": "retry"|"escalate", "u": "<uow_id>"}`.

### Filter bar JS (status filter + stalled checkbox)

```js
function applyFilters() {
  var statusVal = document.getElementById('status-filter').value;
  var stalledOnly = document.getElementById('stalled-only').checked;
  document.querySelectorAll('#uow-table tbody tr').forEach(function(row) {
    var status = row.dataset.status || '';
    var stalled = row.dataset.stalled === 'true';
    var show = true;
    if (statusVal && status !== statusVal) show = false;
    if (stalledOnly && !stalled) show = false;
    row.style.display = show ? '' : 'none';
  });
}
```

---

## 9. Combined Document-class Skeleton

Minimal self-contained document-class artifact with all required features:

```html
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta name="doc-version" content="1.0">
<meta name="doc-updated" content="YYYY-MM-DDTHH:MM:SS-05:00">
<title>Document Title</title>
<style>
:root {
  --bg:#0b0d14; --surface:#141720; --surface2:#1c2030; --surface3:#232840;
  --border:#2a2f45; --border2:#363d5a;
  --text:#e4e6f0; --text2:#9aa0bb; --text3:#5c6480;
  --accent:#6ea8fe; --accent2:#a78bfa; --accent3:#34d399; --warn:#fb923c;
}
body.light-mode {
  --bg:#f8f9fb; --surface:#ffffff; --surface2:#f0f2f7; --surface3:#e8ecf4;
  --border:#dde1ee; --border2:#c8cedf;
  --text:#1a1d2e; --text2:#4a5068; --text3:#8890a8;
  --accent:#2563eb; --accent2:#7c3aed; --accent3:#059669; --warn:#ea580c;
}
* { box-sizing:border-box; margin:0; padding:0; }
body { font-family:system-ui,-apple-system,sans-serif; background:var(--bg); color:var(--text); font-size:15px; line-height:1.7; }
.wrap { max-width:820px; margin:0 auto; padding:56px 28px 120px; }
/* Header */
.doc-header { margin-bottom:56px; border-bottom:1px solid var(--border); padding-bottom:32px; }
.doc-title { font-size:26px; font-weight:700; color:var(--text); letter-spacing:-.02em; margin-bottom:10px; line-height:1.3; }
.doc-subtitle { font-size:14px; color:var(--text3); line-height:1.5; }
.canon-badge { display:inline-block; font-size:10px; font-weight:700; text-transform:uppercase; letter-spacing:.1em; color:var(--accent3); background:rgba(52,211,153,.1); border:1px solid rgba(52,211,153,.25); border-radius:4px; padding:2px 8px; margin-bottom:14px; }
/* Sections */
.section { margin-bottom:56px; }
.section-header { font-size:12px; font-weight:700; text-transform:uppercase; letter-spacing:.1em; color:var(--text2); margin-bottom:20px; padding-bottom:10px; border-bottom:1px solid var(--border); display:flex; align-items:center; gap:8px; }
.section-id { font-size:11px; font-weight:700; color:var(--text3); font-family:'SF Mono','Fira Code',monospace; user-select:none; flex-shrink:0; }
.subsection { margin-bottom:32px; }
.subsection-header { font-size:13px; font-weight:700; color:var(--text); margin-bottom:12px; display:flex; align-items:baseline; gap:8px; }
.subsection-id { font-size:10px; font-weight:700; font-family:'SF Mono','Fira Code',monospace; color:var(--text3); user-select:none; }
/* Toggle */
#theme-toggle { position:fixed; top:14px; right:18px; z-index:1000; background:var(--surface2); border:1px solid var(--border); border-radius:8px; padding:6px 12px; font-size:12px; font-weight:600; color:var(--text2); cursor:pointer; transition:background .15s,color .15s; }
#theme-toggle:hover { background:var(--surface3); color:var(--text); }
/* Comment widget */
.comment-widget { margin-top:48px; padding:20px 24px; background:var(--surface); border:1px solid var(--border); border-radius:10px; }
.comment-widget-title { font-size:11px; font-weight:700; text-transform:uppercase; letter-spacing:.09em; color:var(--text3); margin-bottom:14px; }
.comment-row { display:flex; align-items:center; gap:10px; margin-bottom:8px; }
.comment-label { font-size:11px; font-weight:700; font-family:monospace; color:var(--text3); min-width:36px; }
.comment-input { flex:1; background:var(--surface2); border:1px solid var(--border); border-radius:6px; padding:6px 10px; font-size:12px; color:var(--text); outline:none; }
.comment-input:focus { border-color:var(--accent); }
.copy-comments-btn { margin-top:12px; padding:8px 18px; background:var(--accent); color:#fff; border:none; border-radius:6px; font-size:12px; font-weight:700; cursor:pointer; transition:opacity .15s; }
.copy-comments-btn:hover { opacity:.85; }
.copy-feedback { margin-top:8px; font-size:11px; color:var(--accent3); min-height:16px; }
</style>
</head>
<body>
<button id="theme-toggle" onclick="toggleTheme()">&#9788; Light</button>
<div class="wrap">

<div class="doc-header">
  <div class="doc-title">Document Title</div>
  <div class="doc-subtitle">One-sentence framing of what this document establishes.</div>
</div>

<div class="section" id="s1">
  <div class="section-header"><span class="section-id">§1</span> First Section</div>
  <p>Content here.</p>
</div>

<div class="section" id="s2">
  <div class="section-header"><span class="section-id">§2</span> Second Section</div>
  <p>Content here.</p>
</div>

<!-- COMMENT WIDGET -->
<div class="comment-widget" id="comment-widget">
  <div class="comment-widget-title">Section Comments</div>
  <div class="comment-inputs" id="comment-inputs"></div>
  <button class="copy-comments-btn" onclick="copyComments()">Copy comments</button>
  <div class="copy-feedback" id="copy-feedback"></div>
</div>

</div><!-- /wrap -->
<script>
// Theme toggle
function toggleTheme() {
  const isLight = document.body.classList.toggle('light-mode');
  localStorage.setItem('theme', isLight ? 'light' : 'dark');
  document.getElementById('theme-toggle').textContent = isLight ? '☾ Dark' : '☀ Light';
}
(function() {
  if (localStorage.getItem('theme') === 'light') {
    document.body.classList.add('light-mode');
    const btn = document.getElementById('theme-toggle');
    if (btn) btn.textContent = '☾ Dark';
  }
})();

// Comment widget
const sections = [
  { id: 's1', label: '§1' },
  { id: 's2', label: '§2' },
];
(function buildCommentWidget() {
  const container = document.getElementById('comment-inputs');
  if (!container) return;
  sections.forEach(function(s) {
    const row = document.createElement('div');
    row.className = 'comment-row';
    row.innerHTML = '<span class="comment-label">' + s.label + '</span>' +
      '<input class="comment-input" type="text" id="ci-' + s.id +
      '" placeholder="Comment on ' + s.label + '…">';
    container.appendChild(row);
  });
})();
function copyText(text) {
  if (navigator.clipboard && window.isSecureContext) {
    navigator.clipboard.writeText(text).catch(function() { fallbackCopy(text); });
  } else {
    fallbackCopy(text);
  }
}
function fallbackCopy(text) {
  var el = document.createElement('textarea');
  el.value = text;
  el.style.position = 'fixed'; el.style.opacity = '0';
  document.body.appendChild(el);
  el.focus(); el.select();
  try { document.execCommand('copy'); }
  catch(e) { alert('Copy failed — paste manually:\n\n' + text); }
  document.body.removeChild(el);
}
function copyComments() {
  const parts = sections.map(function(s) {
    const val = (document.getElementById('ci-' + s.id) || {}).value || '';
    return val.trim() ? '[' + s.label + '] ' + val.trim() : null;
  }).filter(Boolean);
  if (!parts.length) { document.getElementById('copy-feedback').textContent = 'Nothing to copy.'; return; }
  copyText(parts.join(' | '));
  document.getElementById('copy-feedback').textContent = 'Copied to clipboard.';
  setTimeout(function() { document.getElementById('copy-feedback').textContent = ''; }, 2500);
}
</script>
</body>
</html>
```

---

## 10. Bisque Hosting

Every document-class HTML artifact must be served at bisque and the URL sent to Dan — never a local path.

**Base URL:** `http://5.78.201.64:9101/files/<filename>`

When sending the link in a Telegram reply, format it as:
```
[Document Title](http://5.78.201.64:9101/files/your-document.html)
```

The IFTTT rule `when-generating-document-class-html-for-dan` encodes this requirement. The rule also clarifies that dashboards do not require the clipboard comment widget unless Dan explicitly requests it.

---

## 11. Considerations Checklist

Before finalizing any HTML artifact, confirm:

- [ ] Dark/light palette tokens applied (`--bg`, `--surface`, `--text`, `--accent` family)
- [ ] Light mode overrides use higher-contrast accent values (`#2563eb` not `#6ea8fe`)
- [ ] Version meta tags present (`doc-version`, `doc-updated`)
- [ ] Visible version stamp present in page header (`<div class="version-stamp">vN.N · Updated: YYYY-MM-DDTHH:MMZ</div>`)
- [ ] Section IDs are taxonomic (§1, §2, §1.1) not technical slugs
- [ ] For document-class: clipboard comment widget present and sections[] matches actual sections
- [ ] For document-class with taxonomy: D3.js network present (not a static diagram)
- [ ] All JS is inline; no external CSS except D3 CDN link
- [ ] Bisque URL in send_reply; local path not sent to Dan
- [ ] For dashboard-class: filter bar, status badges, action buttons tested
- [ ] Telegram send_reply states version: "Updated to vN.N (Updated: YYYY-MM-DDTHH:MMZ)"
- [ ] All browser API calls verified against plain HTTP (Bisque) context — navigator.clipboard, Notification API, etc.

---

## 12. Version + Timestamp Standard

Every HTML file delivered to Dan MUST include in the visible page:
- A version number in `vN.N` format (increment minor on each update, major on structural changes)
- An "Updated at" timestamp in ISO format (UTC), e.g. `2026-05-29T18:51Z`
- Location: visible at the top of the page, in the header area below the title

### CSS

```css
.version-stamp {
  font-size: 0.78rem;
  color: var(--muted, var(--text3));
  text-align: right;
  margin-bottom: 1.5rem;
  opacity: 0.7;
}
```

### HTML placement

```html
<h1>Document Title</h1>
<div class="version-stamp">v1.0 &middot; Updated: 2026-05-29T18:51Z</div>
```

Or for the dark-theme skeleton with `.doc-header`:

```html
<div class="doc-header">
  <div class="doc-title">Document Title</div>
  <div class="doc-subtitle">One-sentence summary.</div>
  <div class="version-stamp">v1.0 &middot; Updated: 2026-05-29T18:51Z</div>
</div>
```

### Telegram reporting convention

When sending a Telegram reply about a delivered HTML update, always state:
```
Updated to vN.N (Updated: YYYY-MM-DDTHH:MMZ)
```

This allows Dan to distinguish browser cache issues from real delivery failures.

---

## 13. Browser API Requirements — Plain HTTP (Bisque) Context

Bisque (`http://5.78.201.64:9101`) is served over plain HTTP, not HTTPS. This means `window.isSecureContext` is `false` and secure-context-only browser APIs (notably the Clipboard API) are unavailable. All HTML delivered to Bisque must be tested against this constraint.

### Clipboard API — canonical HTTP-safe pattern

Never call `navigator.clipboard.writeText()` unconditionally — it is `undefined` on plain HTTP (non-localhost). Always guard with `navigator.clipboard && window.isSecureContext`, and always provide a `fallbackCopy` function using `document.execCommand('copy')` via a temporary textarea. If `execCommand` also fails, show an alert so the text is never silently lost.

**Reference implementation — use this pattern verbatim in all future HTML:**

```js
function copyText(text) {
  if (navigator.clipboard && window.isSecureContext) {
    navigator.clipboard.writeText(text).catch(function() { fallbackCopy(text); });
  } else {
    fallbackCopy(text);
  }
}
function fallbackCopy(text) {
  var el = document.createElement('textarea');
  el.value = text;
  el.style.position = 'fixed'; el.style.opacity = '0';
  document.body.appendChild(el);
  el.focus(); el.select();
  try { document.execCommand('copy'); }
  catch(e) { alert('Copy failed — paste manually:\n\n' + text); }
  document.body.removeChild(el);
}
```

All clipboard operations (comment widget `copyComments`, action-copy buttons, any other "copy to clipboard" feature) must delegate to `copyText(text)` rather than calling `navigator.clipboard` directly.

### Other secure-context APIs to avoid without fallbacks

- **Notification API** (`Notification.requestPermission()`) — unavailable on plain HTTP
- **Web Crypto API** (`crypto.subtle.*`) — unavailable on plain HTTP
- **Service Workers** — unavailable on plain HTTP

When any of these are needed, guard with `window.isSecureContext` and provide a graceful degradation path.

### Discovery note

This standard was encoded following the v1.2 fix to the copy-comments widget, where `navigator.clipboard` being `undefined` on Bisque caused silent copy failures. The `fallbackCopy` pattern using `execCommand` is the established workaround for this deployment context.
