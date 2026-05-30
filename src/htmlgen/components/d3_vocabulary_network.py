"""
D3 vocabulary network component — force-directed concept graph with 3-tier node model.

Extracted from html-interface/STANDARDS.md §6.
Phase 1: replicates existing behavior exactly.
"""

COMPONENT_ID = "d3-vocabulary-network"
COMPONENT_VERSION = "1.0.0"

CONFIG_SCHEMA = {
    "type": "object",
    "properties": {
        "nodes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "tier": {"type": "string", "enum": ["register", "param", "cross"]},
                    "register": {"type": "string"},
                    "color": {"type": "string"},
                    "def": {"type": "string"},
                    "examples": {"type": "array"},
                },
                "required": ["id", "tier", "color", "def"],
            },
        },
        "links": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "source": {"type": "string"},
                    "target": {"type": "string"},
                    "type": {"type": "string", "enum": ["param", "cross", "hub"]},
                },
                "required": ["source", "target", "type"],
            },
        },
    },
    "required": ["nodes", "links"],
}

_CSS = """
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
"""

_HTML_TEMPLATE = """<div class="graph-container">
  <div class="graph-controls">
    <span class="graph-controls-label">Show:</span>
    <button class="graph-btn active" id="vn-filter-all" onclick="vnSetFilter('all')">All layers</button>
    <button class="graph-btn" id="vn-filter-register" onclick="vnSetFilter('register')">Concepts only</button>
    <button class="graph-btn" id="vn-filter-param" onclick="vnSetFilter('param')">+ Parameters</button>
  </div>
  <svg id="vocabulary-graph" style="width:100%;min-height:600px;display:block;background:var(--surface);border-radius:10px;"></svg>
  <div id="graph-tooltip" class="graph-tooltip"></div>
  <div class="graph-legend" id="graph-legend"></div>
</div>"""

_JS_TEMPLATE = """
(function() {{
  var VN_NODES = {nodes_json};
  var VN_LINKS = {links_json};

  var COLORS = {{
    param: '#7dd3fc',
    cross: '#86efac',
    link: {{
      param: 'rgba(125,211,252,.45)',
      cross: 'rgba(134,239,172,.35)',
      hub: 'rgba(255,255,255,.09)'
    }}
  }};

  function initGraph() {{
    var container = document.getElementById('vocabulary-graph');
    if (!container || typeof d3 === 'undefined') return;
    var width = container.clientWidth || 760;
    var height = 600;

    var svg = d3.select('#vocabulary-graph')
      .attr('viewBox', '0 0 ' + width + ' ' + height)
      .attr('preserveAspectRatio', 'xMidYMid meet');

    svg.append('defs').append('marker')
      .attr('id', 'arrowhead').attr('viewBox', '-0 -5 10 10')
      .attr('refX', 18).attr('refY', 0).attr('orient', 'auto')
      .attr('markerWidth', 6).attr('markerHeight', 6)
      .append('svg:path').attr('d', 'M 0,-5 L 10,0 L 0,5')
      .attr('fill', '#555').style('stroke', 'none');

    var linkG = svg.append('g').attr('class', 'links');
    var nodeG = svg.append('g').attr('class', 'nodes');

    var nodesData = VN_NODES.map(function(n) {{ return Object.assign({{}}, n); }});
    var linksData = VN_LINKS.map(function(l) {{ return Object.assign({{}}, l); }});

    var simulation = d3.forceSimulation(nodesData)
      .force('link', d3.forceLink(linksData).id(function(d) {{ return d.id; }})
        .distance(function(d) {{
          if (d.type === 'hub') return 130;
          if (d.type === 'param') return 95;
          return 110;
        }})
        .strength(function(d) {{ return d.type === 'hub' ? 0.08 : 0.45; }})
      )
      .force('charge', d3.forceManyBody().strength(function(d) {{
        return d.tier === 'register' ? -320 : -180;
      }}))
      .force('center', d3.forceCenter(width / 2, height / 2))
      .force('collision', d3.forceCollide().radius(function(d) {{
        return d.tier === 'register' ? 52 : 38;
      }}))
      .force('y', d3.forceY(function(d) {{
        if (d.tier === 'register') return height * 0.38;
        if (d.tier === 'param') return height * 0.70;
        return height * 0.82;
      }}).strength(function(d) {{
        return d.tier === 'register' ? 0.25 : 0.18;
      }}));

    var links = linkG.selectAll('line').data(linksData).enter().append('line')
      .attr('stroke', function(d) {{ return COLORS.link[d.type] || COLORS.link.hub; }})
      .attr('stroke-width', function(d) {{
        if (d.type === 'hub') return 1;
        if (d.type === 'param') return 1.5;
        return 2;
      }})
      .attr('stroke-dasharray', function(d) {{ return d.type === 'cross' ? '5,3' : null; }});

    var nodeGroups = nodeG.selectAll('g.node-group').data(nodesData).enter()
      .append('g').attr('class', 'node-group')
      .style('cursor', 'pointer')
      .call(d3.drag()
        .on('start', function(event, d) {{
          if (!event.active) simulation.alphaTarget(0.3).restart();
          d.fx = d.x; d.fy = d.y;
        }})
        .on('drag', function(event, d) {{ d.fx = event.x; d.fy = event.y; }})
        .on('end', function(event, d) {{
          if (!event.active) simulation.alphaTarget(0);
          d.fx = null; d.fy = null;
        }})
      )
      .on('mouseover', function(event, d) {{ vnShowTooltip(event, d); }})
      .on('mousemove', function(event, d) {{ vnShowTooltip(event, d); }})
      .on('mouseout', vnHideTooltip)
      .on('click', function(event, d) {{ vnShowTooltip(event, d); event.stopPropagation(); }});

    svg.on('click', vnHideTooltip);

    nodeGroups.append('circle')
      .attr('r', function(d) {{ return d.tier === 'register' ? 34 : 22; }})
      .attr('fill', function(d) {{ return d.color + (d.tier === 'register' ? '22' : '18'); }})
      .attr('stroke', function(d) {{ return d.color; }})
      .attr('stroke-width', function(d) {{ return d.tier === 'register' ? 2 : 1.5; }});

    nodeGroups.filter(function(d) {{ return d.tier !== 'register'; }})
      .append('circle')
        .attr('r', 22).attr('fill', 'none')
        .attr('stroke', function(d) {{ return d.color; }})
        .attr('stroke-width', 0.5)
        .attr('stroke-dasharray', function(d) {{ return d.tier === 'cross' ? '3,2' : null; }})
        .attr('opacity', 0.4);

    nodeGroups.append('text')
      .attr('text-anchor', 'middle').attr('dominant-baseline', 'central')
      .attr('fill', function(d) {{ return d.color; }})
      .attr('font-size', function(d) {{ return d.tier === 'register' ? '9.5' : '8'; }})
      .attr('font-weight', '700')
      .attr('font-family', 'system-ui, -apple-system, sans-serif')
      .attr('pointer-events', 'none')
      .each(function(d) {{
        var el = d3.select(this);
        var words = d.id.split(' ');
        if (words.length === 1 || d.tier === 'register') {{
          el.text(d.id.length > 11 ? d.id.slice(0, 10) + '\\u2026' : d.id);
        }} else {{
          el.append('tspan').attr('x', 0).attr('dy', '-0.55em').text(words[0]);
          el.append('tspan').attr('x', 0).attr('dy', '1.1em').text(words.slice(1).join(' '));
        }}
      }});

    simulation.on('tick', function() {{
      links
        .attr('x1', function(d) {{ return d.source.x; }})
        .attr('y1', function(d) {{ return d.source.y; }})
        .attr('x2', function(d) {{ return d.target.x; }})
        .attr('y2', function(d) {{ return d.target.y; }});
      nodeGroups.attr('transform', function(d) {{
        return 'translate(' + d.x + ',' + d.y + ')';
      }});
    }});

    vnBuildLegend(nodesData);
  }}

  function vnShowTooltip(event, d) {{
    var tt = document.getElementById('graph-tooltip');
    var html = '<div class="tt-name">' + d.id + '</div>';
    html += '<div class="tt-type" style="color:' + d.color + '">';
    if (d.tier === 'register') html += 'Concept' + (d.badge ? ' \\u00b7 ' + d.badge : '');
    else if (d.tier === 'param') html += 'Parameter \\u2192 ' + (d.register || '');
    else html += 'Cross-register Measurement';
    html += '</div>';
    html += '<div class="tt-def">' + d.def + '</div>';
    if (d.examples && d.examples.length) {{
      html += '<div class="tt-examples">';
      d.examples.slice(0, 3).forEach(function(ex) {{
        html += '<div class="tt-ex"><strong>' + ex.domain + ':</strong> ' + ex.text + '</div>';
      }});
      html += '</div>';
    }}
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
  }}

  function vnHideTooltip() {{
    var tt = document.getElementById('graph-tooltip');
    if (tt) tt.classList.remove('visible');
  }}

  function vnBuildLegend(nodes) {{
    var legend = document.getElementById('graph-legend');
    if (!legend) return;
    var seen = {{}};
    nodes.forEach(function(n) {{
      if (!seen[n.id]) {{
        seen[n.id] = true;
        var item = document.createElement('div');
        item.className = 'legend-item';
        item.innerHTML = '<div class="legend-dot" style="background:' + n.color + '"></div>' +
          '<span>' + n.id + '</span>';
        legend.appendChild(item);
      }}
    }});
  }}

  function vnSetFilter(tier) {{
    // Update button states
    ['all', 'register', 'param'].forEach(function(f) {{
      var btn = document.getElementById('vn-filter-' + f);
      if (btn) btn.classList.toggle('active', f === tier);
    }});
    // Filter node groups and links by tier
    d3.selectAll('g.node-group').each(function(d) {{
      var show = tier === 'all' ||
        d.tier === tier ||
        (tier === 'param' && (d.tier === 'register' || d.tier === 'param'));
      d3.select(this).style('display', show ? null : 'none');
    }});
  }}

  window.vnSetFilter = vnSetFilter;

  if (document.readyState === 'loading') {{
    document.addEventListener('DOMContentLoaded', initGraph);
  }} else {{
    initGraph();
  }}
}})();
"""


def render(config: dict) -> str:
    """
    Returns D3.js force-directed vocabulary network HTML+CSS+JS fragment.

    3-tier node model (register, param, cross). Full spec: html-interface/STANDARDS.md §6.
    Requires D3 CDN script tag — renderer must inject:
    <script src="https://d3js.org/d3.v7.min.js"></script> before </body>.
    """
    import json

    nodes = config["nodes"]
    links = config["links"]

    nodes_json = json.dumps(nodes)
    links_json = json.dumps(links)

    js = _JS_TEMPLATE.format(nodes_json=nodes_json, links_json=links_json)

    return (
        f"<style>{_CSS}</style>\n"
        f"{_HTML_TEMPLATE}\n"
        f"<script>{js}</script>"
    )
