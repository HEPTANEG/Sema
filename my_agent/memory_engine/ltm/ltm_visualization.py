"""Read-only LTM debug visualization export."""

import json
import os
import sqlite3
import webbrowser
from datetime import datetime, timezone
from pathlib import Path

from .. import event_stream
from . import ltm_store


VISUALIZATION_DIR_NAME = "visualizations"
EVENT_PREVIEW_CHARS = 240

OBJECT_TYPE_COLORS = {
    "assertion": "#2563eb",
    "episode": "#7c3aed",
    "tag": "#16a34a",
    "event_slot": "#6b7280",
}

LINK_TYPE_COLORS = {
    "sourced_from": "#2563eb",
    "tagged_with": "#16a34a",
    "supports": "#0891b2",
    "contradicts": "#dc2626",
    "supersedes": "#ea580c",
}

STRENGTH_EFFECTS = {
    "sourced_from": "source_gain",
    "tagged_with": "neutral_both",
    "supports": "target_gain",
    "contradicts": "neutral_both",
    "supersedes": "target_loss",
}


def visualization_root(root=None):
    return ltm_store.ltm_dir(root) / VISUALIZATION_DIR_NAME


def generate_ltm_visualization(root=None):
    db_path = ltm_store.ltm_db_path(root)
    if not db_path.exists():
        return {
            "status": "not_initialized",
            "message": "LTM Store is not initialized.",
            "db_path": str(db_path),
            "snapshot_path": None,
            "html_path": None,
        }

    snapshot = build_ltm_visualization_snapshot(root)
    output_dir = visualization_root(root) / _timestamp_for_path()
    output_dir.mkdir(parents=True, exist_ok=False)
    snapshot_path = output_dir / "snapshot.json"
    html_path = output_dir / "graph.html"
    snapshot_path.write_text(
        json.dumps(snapshot, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    html_path.write_text(render_ltm_visualization_html(snapshot), encoding="utf-8")
    return {
        "status": "ok",
        "snapshot_path": str(snapshot_path),
        "html_path": str(html_path),
        "object_count": snapshot["metadata"]["object_count"],
        "link_count": snapshot["metadata"]["link_count"],
        "event_node_count": snapshot["metadata"]["event_node_count"],
    }


def format_ltm_visualization_result(result):
    lines = [
        "LTM Visualization:",
        f"status: {result.get('status')}",
    ]
    if result.get("status") == "not_initialized":
        lines.extend([
            f"db_path: {result.get('db_path')}",
            f"message: {result.get('message')}",
        ])
        return "\n".join(lines)
    lines.extend([
        f"snapshot_path: {result.get('snapshot_path')}",
        f"html_path: {result.get('html_path')}",
        f"object_count: {result.get('object_count', 0)}",
        f"link_count: {result.get('link_count', 0)}",
        f"event_node_count: {result.get('event_node_count', 0)}",
    ])
    if result.get("open_status"):
        lines.insert(4, f"open_status: {result.get('open_status')}")
    if result.get("open_message"):
        lines.append(f"open_message: {result.get('open_message')}")
    return "\n".join(lines)


def open_ltm_visualization_html(result):
    if result.get("status") != "ok" or not result.get("html_path"):
        return {
            **result,
            "open_status": "skipped",
            "open_message": "No generated graph.html to open.",
        }
    try:
        _open_local_file(result["html_path"])
    except Exception as exc:
        return {
            **result,
            "open_status": "failed",
            "open_message": str(exc),
        }
    return {
        **result,
        "open_status": "opened",
    }


def _open_local_file(path):
    resolved = Path(path).resolve()
    if os.name == "nt":
        os.startfile(str(resolved))
        return
    webbrowser.open(resolved.as_uri())


def build_ltm_visualization_snapshot(root=None):
    connection = _connect_ltm_read_only(root)
    try:
        object_nodes = _load_object_nodes(connection)
        links = _load_links(connection)
        event_nodes = _event_nodes_for_links(links, root)
        maintenance = _load_maintenance(connection, root)
        health = _load_health(connection)
        return {
            "metadata": {
                "generated_at": _now(),
                "db_path": str(ltm_store.ltm_db_path(root)),
                "db_schema_version": int(connection.execute("PRAGMA user_version").fetchone()[0]),
                "object_count": len(object_nodes),
                "link_count": len(links),
                "event_node_count": len(event_nodes),
            },
            "nodes": object_nodes + event_nodes,
            "links": _display_links(links),
            "maintenance": maintenance,
            "health": health,
        }
    finally:
        connection.close()


def render_ltm_visualization_html(snapshot):
    data = json.dumps(snapshot, ensure_ascii=False, sort_keys=True).replace("<", "\\u003c")
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>LTM Debug Visualization</title>
  <style>
    :root {{
      color-scheme: light;
      --bg: #f8fafc;
      --panel: #ffffff;
      --text: #111827;
      --muted: #6b7280;
      --border: #d1d5db;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: Arial, Helvetica, sans-serif;
      background: var(--bg);
      color: var(--text);
    }}
    .app {{
      display: grid;
      grid-template-columns: minmax(0, 1fr) 360px;
      min-height: 100vh;
    }}
    header {{
      padding: 16px 20px 10px;
      border-bottom: 1px solid var(--border);
      background: var(--panel);
    }}
    h1 {{ margin: 0 0 8px; font-size: 20px; }}
    .stats {{ display: flex; flex-wrap: wrap; gap: 8px 16px; color: var(--muted); font-size: 13px; }}
    .canvas-wrap {{ min-width: 0; height: calc(100vh - 82px); overflow: hidden; }}
    svg {{ display: block; width: 100%; height: 100%; background: #f8fafc; cursor: grab; }}
    svg.panning {{ cursor: grabbing; }}
    .link {{ fill: none; stroke-width: 2; opacity: 0.82; }}
    .link-direction {{ fill: #475569; opacity: 0.9; pointer-events: none; }}
    .link.inactive {{ stroke: #9ca3af !important; stroke-dasharray: 6 5; opacity: 0.7; }}
    .link-direction.inactive {{ fill: #9ca3af; opacity: 0.8; }}
    .node {{ cursor: pointer; }}
    .node-shape {{ stroke: #111827; stroke-width: 1.25; }}
    .node.selected .node-shape {{ stroke-width: 3; stroke: #020617; }}
    .node-label, .event-label {{
      font-size: 8px;
      font-weight: 700;
      pointer-events: none;
      fill: #0f172a;
      dominant-baseline: middle;
      paint-order: stroke;
      stroke: rgba(255, 255, 255, 0.78);
      stroke-width: 3px;
      stroke-linejoin: round;
    }}
    .event-label {{ font-size: 9px; }}
    aside {{
      border-left: 1px solid var(--border);
      background: var(--panel);
      padding: 16px;
      overflow: auto;
      max-height: 100vh;
    }}
    .muted {{ color: var(--muted); }}
    .section {{ border-top: 1px solid var(--border); padding-top: 12px; margin-top: 12px; }}
    .kv {{ margin: 7px 0; font-size: 13px; overflow-wrap: anywhere; }}
    .kv strong {{ display: block; margin-bottom: 2px; font-size: 11px; color: var(--muted); text-transform: uppercase; }}
    .payload-body {{ white-space: pre-wrap; overflow-wrap: anywhere; font-size: 14px; line-height: 1.45; }}
    details.debug-section, details.raw-payload {{ border-top: 1px solid var(--border); padding-top: 10px; margin-top: 10px; }}
    details.debug-section > summary, details.raw-payload > summary {{ cursor: pointer; color: #374151; font-weight: 700; }}
    pre {{ white-space: pre-wrap; overflow-wrap: anywhere; background: #f3f4f6; padding: 8px; border-radius: 6px; font-size: 12px; }}
    .legend {{ display: grid; gap: 6px; margin-top: 10px; }}
    .legend-row {{ display: flex; flex-wrap: wrap; gap: 8px 12px; }}
    .legend-item {{ display: inline-flex; align-items: center; gap: 5px; font-size: 12px; color: var(--muted); }}
    .swatch {{ width: 12px; height: 12px; border-radius: 50%; border: 1px solid #111827; }}
    .line-swatch {{ width: 24px; height: 0; border-top: 3px solid currentColor; }}
  </style>
</head>
<body>
  <div class="app">
    <main>
      <header>
        <h1>LTM Debug Visualization</h1>
        <div class="stats" id="stats"></div>
        <div class="legend" id="legend"></div>
      </header>
      <div class="canvas-wrap">
        <svg id="graph" role="img" aria-label="LTM debug graph"></svg>
      </div>
    </main>
    <aside id="inspector">
      <h2>Inspector</h2>
      <p class="muted">Click a node to inspect details.</p>
    </aside>
  </div>
  <script id="graph-data" type="application/json">{data}</script>
  <script>
    const snapshot = JSON.parse(document.getElementById('graph-data').textContent);
    const objectColors = {json.dumps(OBJECT_TYPE_COLORS, sort_keys=True)};
    const linkColors = {json.dumps(LINK_TYPE_COLORS, sort_keys=True)};
    const svg = document.getElementById('graph');
    const inspector = document.getElementById('inspector');
    const nodes = snapshot.nodes || [];
    const links = snapshot.links || [];
    const byId = Object.fromEntries(nodes.map(node => [node.id, node]));
    const graphWidth = Math.max(1800, 1000 + Math.ceil(Math.sqrt(Math.max(nodes.length, 1))) * 230);
    const graphHeight = Math.max(1200, 760 + Math.ceil(Math.sqrt(Math.max(nodes.length, 1))) * 190);
    const center = {{ x: graphWidth / 2, y: graphHeight / 2 }};
    const innerRadius = Math.max(150, Math.min(graphWidth, graphHeight) * 0.26);
    const outerRadius = Math.max(innerRadius + 180, Math.min(graphWidth, graphHeight) * 0.44);
    const FORCE_TICKS = 520;
    const TICKS_PER_FRAME = 1;
    const LINK_DISTANCE = 210;
    const EVENT_LINK_DISTANCE = 290;
    const LINK_FORCE = 0.006;
    const REPULSION_FORCE = 22000;
    const COLLISION_PADDING = 20;
    const CENTER_FORCE = 0.0035;
    const EVENT_RADIAL_FORCE = 0.005;
    const DAMPING = 0.72;
    const MAX_VELOCITY = 8;
    const CLICK_MOVE_TOLERANCE = 5;
    const LABEL_CHARS = 6;
    const EVENT_LABEL_CHARS = 12;
    let remainingTicks = FORCE_TICKS;
    let simulationRunning = false;
    let viewport = null;
    let pan = {{ x: 0, y: 0, scale: 1 }};
    let selectedNode = null;
    let dragNode = null;
    let dragStart = null;
    let panning = null;
    const linkElements = [];
    const nodeElements = new Map();

    function strengthColor(type, strength) {{
      const color = objectColors[type] || '#64748b';
      if (type === 'event_slot') return color;
      const value = Math.max(0, Math.min(1, Number(strength ?? 0.5)));
      const alpha = 0.35 + value * 0.65;
      return hexToRgba(color, alpha);
    }}

    function hexToRgba(hex, alpha) {{
      const raw = hex.replace('#', '');
      const r = parseInt(raw.slice(0, 2), 16);
      const g = parseInt(raw.slice(2, 4), 16);
      const b = parseInt(raw.slice(4, 6), 16);
      return `rgba(${{r}}, ${{g}}, ${{b}}, ${{alpha}})`;
    }}

    function labelFor(node) {{
      const text = node.label || node.title || node.id;
      const limit = node.kind === 'event_slot' ? EVENT_LABEL_CHARS : LABEL_CHARS;
      return text.length > limit ? text.slice(0, limit) + '...' : text;
    }}

    function nodeRadius(node) {{
      return node.kind === 'event_slot' ? 54 : 42;
    }}

    function nodeMass(node) {{
      return node.kind === 'event_slot' ? 1.4 : 1;
    }}

    function hashUnit(value) {{
      let hash = 2166136261;
      for (let index = 0; index < String(value).length; index += 1) {{
        hash ^= String(value).charCodeAt(index);
        hash = Math.imul(hash, 16777619);
      }}
      return (hash >>> 0) / 4294967295;
    }}

    function initializeNodePositions() {{
      const eventNodes = nodes
        .filter(node => node.kind === 'event_slot')
        .sort((a, b) => (a.slot_seq_start ?? 0) - (b.slot_seq_start ?? 0) || String(a.id).localeCompare(String(b.id)));
      const objectNodes = nodes
        .filter(node => node.kind !== 'event_slot')
        .sort((a, b) => String(a.created_at || '').localeCompare(String(b.created_at || '')) || String(a.id).localeCompare(String(b.id)));
      objectNodes.forEach((node, index) => {{
        const angle = hashUnit(node.id) * Math.PI * 2;
        const ring = 0.42 + (index % 4) * 0.18;
        const radius = innerRadius * ring;
        node.x = center.x + Math.cos(angle) * radius;
        node.y = center.y + Math.sin(angle) * radius;
        node.vx = 0;
        node.vy = 0;
      }});
      eventNodes.forEach((node, index) => {{
        const angle = eventNodes.length ? (index / eventNodes.length) * Math.PI * 2 : 0;
        node.x = center.x + Math.cos(angle) * outerRadius;
        node.y = center.y + Math.sin(angle) * outerRadius;
        node.vx = 0;
        node.vy = 0;
      }});
    }}

    function applyLinkForce() {{
      for (const link of links) {{
        const source = byId[link.source];
        const target = byId[link.target];
        if (!source || !target) continue;
        let dx = target.x - source.x;
        let dy = target.y - source.y;
        let distance = Math.sqrt(dx * dx + dy * dy) || 1;
        const targetDistance = source.kind === 'event_slot' || target.kind === 'event_slot' ? EVENT_LINK_DISTANCE : LINK_DISTANCE;
        const force = (distance - targetDistance) * LINK_FORCE;
        dx /= distance;
        dy /= distance;
        const sourceMass = nodeMass(source);
        const targetMass = nodeMass(target);
        if (!source.dragging) {{
          source.vx += dx * force / sourceMass;
          source.vy += dy * force / sourceMass;
        }}
        if (!target.dragging) {{
          target.vx -= dx * force / targetMass;
          target.vy -= dy * force / targetMass;
        }}
      }}
    }}

    function applyRepulsionForce() {{
      for (let i = 0; i < nodes.length; i += 1) {{
        for (let j = i + 1; j < nodes.length; j += 1) {{
          const a = nodes[i];
          const b = nodes[j];
          let dx = b.x - a.x;
          let dy = b.y - a.y;
          let distanceSq = dx * dx + dy * dy || 0.01;
          const distance = Math.sqrt(distanceSq);
          const force = REPULSION_FORCE / Math.max(distanceSq, 900);
          dx /= distance;
          dy /= distance;
          if (!a.dragging) {{
            a.vx -= dx * force / nodeMass(a);
            a.vy -= dy * force / nodeMass(a);
          }}
          if (!b.dragging) {{
            b.vx += dx * force / nodeMass(b);
            b.vy += dy * force / nodeMass(b);
          }}
        }}
      }}
    }}

    function applyCollisionForce() {{
      for (let i = 0; i < nodes.length; i += 1) {{
        for (let j = i + 1; j < nodes.length; j += 1) {{
          const a = nodes[i];
          const b = nodes[j];
          let dx = b.x - a.x;
          let dy = b.y - a.y;
          let distance = Math.sqrt(dx * dx + dy * dy) || 1;
          const minDistance = nodeRadius(a) + nodeRadius(b) + COLLISION_PADDING;
          if (distance >= minDistance) continue;
          const push = (minDistance - distance) * 0.08;
          dx /= distance;
          dy /= distance;
          if (!a.dragging) {{
            a.vx -= dx * push;
            a.vy -= dy * push;
          }}
          if (!b.dragging) {{
            b.vx += dx * push;
            b.vy += dy * push;
          }}
        }}
      }}
    }}

    function applyCenterAndRadialForces() {{
      for (const node of nodes) {{
        if (node.dragging) continue;
        const dx = node.x - center.x;
        const dy = node.y - center.y;
        const distance = Math.sqrt(dx * dx + dy * dy) || 1;
        if (node.kind === 'event_slot') {{
          const radialDelta = outerRadius - distance;
          node.vx += (dx / distance) * radialDelta * EVENT_RADIAL_FORCE;
          node.vy += (dy / distance) * radialDelta * EVENT_RADIAL_FORCE;
        }} else {{
          node.vx += (center.x - node.x) * CENTER_FORCE;
          node.vy += (center.y - node.y) * CENTER_FORCE;
        }}
      }}
    }}

    function tickSimulation() {{
      applyLinkForce();
      applyRepulsionForce();
      applyCollisionForce();
      applyCenterAndRadialForces();
      for (const node of nodes) {{
        if (node.dragging && node.fx !== undefined && node.fy !== undefined) {{
          node.x = node.fx;
          node.y = node.fy;
          node.vx = 0;
          node.vy = 0;
          continue;
        }}
        node.vx = clamp((node.vx || 0) * DAMPING, -MAX_VELOCITY, MAX_VELOCITY);
        node.vy = clamp((node.vy || 0) * DAMPING, -MAX_VELOCITY, MAX_VELOCITY);
        node.x = Math.max(40, Math.min(graphWidth - 40, node.x + node.vx));
        node.y = Math.max(40, Math.min(graphHeight - 40, node.y + node.vy));
      }}
    }}

    function simulationFrame() {{
      simulationRunning = true;
      for (let index = 0; index < TICKS_PER_FRAME && remainingTicks > 0; index += 1) {{
        tickSimulation();
        remainingTicks -= 1;
      }}
      updateGraphPositions();
      if (remainingTicks > 0) {{
        requestAnimationFrame(simulationFrame);
      }} else {{
        simulationRunning = false;
      }}
    }}

    function requestSimulation(ticks) {{
      remainingTicks = Math.max(remainingTicks, ticks);
      if (!simulationRunning) requestAnimationFrame(simulationFrame);
    }}

    function draw() {{
      initializeNodePositions();
      svg.setAttribute('viewBox', `0 0 ${{graphWidth}} ${{graphHeight}}`);
      svg.innerHTML = '<defs></defs>';
      viewport = el('g', {{ class: 'viewport' }});
      svg.appendChild(viewport);
      for (const link of links) {{
        const source = byId[link.source];
        const target = byId[link.target];
        if (!source || !target) continue;
        const path = el('path', {{
          class: `link ${{link.link_state === 'inactive' ? 'inactive' : 'active'}}`,
          stroke: link.link_state === 'inactive' ? '#9ca3af' : (linkColors[link.relation_type] || '#64748b')
        }});
        const direction = el('path', {{
          class: `link-direction ${{link.link_state === 'inactive' ? 'inactive' : 'active'}}`,
          fill: link.link_state === 'inactive' ? '#9ca3af' : (linkColors[link.relation_type] || '#64748b')
        }});
        path.appendChild(title(`${{link.relation_type}} · ${{link.link_state}} · ${{link.strength_effect}}`));
        viewport.appendChild(path);
        viewport.appendChild(direction);
        linkElements.push({{ link, path, direction }});
      }}
      for (const node of nodes) {{
        const group = el('g', {{ class: 'node', tabindex: '0' }});
        group.addEventListener('pointerdown', event => startDrag(event, node));
        const type = node.kind === 'event_slot' ? 'event_slot' : node.object_type;
        if (type === 'event_slot') {{
          group.appendChild(el('rect', {{ x: -62, y: -20, width: 124, height: 40, rx: 16, class: 'node-shape event-capsule', fill: objectColors.event_slot }}));
        }} else {{
          group.appendChild(el('circle', {{ cx: 0, cy: 0, r: 42, class: 'node-shape object-circle', fill: strengthColor(type, node.strength) }}));
        }}
        const text = el('text', {{ x: 0, y: 1, 'text-anchor': 'middle', class: type === 'event_slot' ? 'event-label' : 'node-label' }});
        text.textContent = labelFor(node);
        group.appendChild(text);
        group.appendChild(title(labelFor(node)));
        viewport.appendChild(group);
        nodeElements.set(node.id, group);
      }}
      updateGraphPositions();
      setupZoomPan();
      document.getElementById('stats').textContent =
        `objects: ${{snapshot.metadata.object_count}} · links: ${{snapshot.metadata.link_count}} · event nodes: ${{snapshot.metadata.event_node_count}} · generated: ${{snapshot.metadata.generated_at}}`;
      document.getElementById('legend').innerHTML = `
        <div class="legend-row">${{Object.entries(objectColors).map(([key, value]) =>
          `<span class="legend-item"><span class="swatch" style="background:${{value}}"></span>${{escapeHtml(key)}}</span>`
        ).join('')}}</div>
        <div class="legend-row">${{Object.entries(linkColors).map(([key, value]) =>
          `<span class="legend-item" style="color:${{value}}"><span class="line-swatch"></span>${{escapeHtml(key)}}</span>`
        ).join('')}}<span class="legend-item" style="color:#9ca3af"><span class="line-swatch" style="border-top-style:dashed"></span>inactive</span></div>
      `;
      if (nodes.length) inspect(nodes[0]);
      requestSimulation(FORCE_TICKS);
    }}

    function updateGraphPositions() {{
      for (const item of linkElements) {{
        const source = byId[item.link.source];
        const target = byId[item.link.target];
        if (!source || !target) continue;
        const line = clippedLinkEndpoints(source, target);
        item.path.setAttribute('d', `M ${{line.x1}} ${{line.y1}} L ${{line.x2}} ${{line.y2}}`);
        item.direction.setAttribute('d', directionArrowPath(line));
      }}
      for (const [id, group] of nodeElements.entries()) {{
        const node = byId[id];
        group.setAttribute('transform', `translate(${{node.x}}, ${{node.y}})`);
      }}
    }}

    function setupZoomPan() {{
      applyViewportTransform();
      svg.addEventListener('wheel', event => {{
        event.preventDefault();
        const svgPoint = clientToSvg(event.clientX, event.clientY);
        const before = svgToGraph(svgPoint);
        const factor = event.deltaY < 0 ? 1.12 : 0.89;
        pan.scale = Math.max(0.2, Math.min(4, pan.scale * factor));
        pan.x = svgPoint.x - before.x * pan.scale;
        pan.y = svgPoint.y - before.y * pan.scale;
        applyViewportTransform();
      }}, {{ passive: false }});
      svg.addEventListener('pointerdown', event => {{
        if (event.target !== svg) return;
        const point = clientToSvg(event.clientX, event.clientY);
        panning = {{ x: point.x, y: point.y, startX: pan.x, startY: pan.y }};
        svg.classList.add('panning');
        svg.setPointerCapture(event.pointerId);
      }});
      svg.addEventListener('pointermove', event => {{
        if (dragNode) {{
          const point = screenToGraph(event.clientX, event.clientY);
          dragNode.fx = point.x;
          dragNode.fy = point.y;
          dragNode.x = point.x;
          dragNode.y = point.y;
          dragNode.vx = 0;
          dragNode.vy = 0;
          updateGraphPositions();
          return;
        }}
        if (!panning) return;
        const point = clientToSvg(event.clientX, event.clientY);
        pan.x = panning.startX + point.x - panning.x;
        pan.y = panning.startY + point.y - panning.y;
        applyViewportTransform();
      }});
      svg.addEventListener('pointerup', endPointerAction);
      svg.addEventListener('pointercancel', endPointerAction);
    }}

    function startDrag(event, node) {{
      event.stopPropagation();
      dragNode = node;
      dragStart = {{
        clientX: event.clientX,
        clientY: event.clientY,
        node
      }};
      node.dragging = true;
      const point = screenToGraph(event.clientX, event.clientY);
      node.fx = point.x;
      node.fy = point.y;
      node.vx = 0;
      node.vy = 0;
      svg.setPointerCapture(event.pointerId);
    }}

    function endPointerAction(event) {{
      if (dragNode) {{
        const moved = dragStart
          ? Math.hypot(event.clientX - dragStart.clientX, event.clientY - dragStart.clientY)
          : Infinity;
        const clickedNode = moved <= CLICK_MOVE_TOLERANCE ? dragNode : null;
        dragNode.dragging = false;
        dragNode.vx = 0;
        dragNode.vy = 0;
        delete dragNode.fx;
        delete dragNode.fy;
        if (clickedNode) inspect(clickedNode);
        requestSimulation(clickedNode ? 0 : 24);
      }}
      dragNode = null;
      dragStart = null;
      panning = null;
      svg.classList.remove('panning');
      try {{ svg.releasePointerCapture(event.pointerId); }} catch (_error) {{}}
    }}

    function applyViewportTransform() {{
      if (!viewport) return;
      viewport.setAttribute('transform', `translate(${{pan.x}}, ${{pan.y}}) scale(${{pan.scale}})`);
    }}

    function screenToGraph(clientX, clientY) {{
      return svgToGraph(clientToSvg(clientX, clientY));
    }}

    function clientToSvg(clientX, clientY) {{
      const point = svg.createSVGPoint();
      point.x = clientX;
      point.y = clientY;
      return point.matrixTransform(svg.getScreenCTM().inverse());
    }}

    function svgToGraph(point) {{
      return {{
        x: (point.x - pan.x) / pan.scale,
        y: (point.y - pan.y) / pan.scale
      }};
    }}

    function clippedLinkEndpoints(source, target) {{
      let dx = target.x - source.x;
      let dy = target.y - source.y;
      const distance = Math.sqrt(dx * dx + dy * dy) || 1;
      dx /= distance;
      dy /= distance;
      const sourceRadius = nodeBoundaryDistance(source, dx, dy) + 1;
      const targetRadius = nodeBoundaryDistance(target, dx, dy) + 1;
      return {{
        x1: source.x + dx * sourceRadius,
        y1: source.y + dy * sourceRadius,
        x2: target.x - dx * targetRadius,
        y2: target.y - dy * targetRadius,
        dx,
        dy
      }};
    }}

    function nodeBoundaryDistance(node, dx, dy) {{
      if (node.kind !== 'event_slot') return nodeRadius(node);
      const halfWidth = 62;
      const halfHeight = 20;
      const denominator = Math.sqrt((dx * dx) / (halfWidth * halfWidth) + (dy * dy) / (halfHeight * halfHeight)) || 1;
      return 1 / denominator;
    }}

    function directionArrowPath(line) {{
      const midX = (line.x1 + line.x2) / 2;
      const midY = (line.y1 + line.y2) / 2;
      const size = 6;
      const wing = 4.5;
      const normalX = -line.dy;
      const normalY = line.dx;
      const tipX = midX + line.dx * size;
      const tipY = midY + line.dy * size;
      const tailX = midX - line.dx * size;
      const tailY = midY - line.dy * size;
      return `M ${{tipX}} ${{tipY}} L ${{tailX + normalX * wing}} ${{tailY + normalY * wing}} L ${{tailX - normalX * wing}} ${{tailY - normalY * wing}} Z`;
    }}

    function inspect(node) {{
      selectedNode = node;
      for (const group of nodeElements.values()) group.classList.remove('selected');
      const selected = nodeElements.get(node.id);
      if (selected) selected.classList.add('selected');
      const incoming = links.filter(link => link.target === node.id);
      const outgoing = links.filter(link => link.source === node.id);
      const payload = payloadInspector(node.payload);
      const body = node.kind === 'event_slot' ? eventInspector(node, incoming, outgoing) : objectInspector(node, incoming, outgoing, payload);
      inspector.innerHTML = body;
    }}

    function objectInspector(node, incoming, outgoing, payload) {{
      return `
        <h2>${{escapeHtml(node.title || node.id)}}</h2>
        ${{kv('Type', node.object_type)}}${{kv('Strength', node.strength)}}
        ${{kv('Summary', node.summary || '')}}${{kv('Created', node.created_at)}}${{kv('Updated', node.updated_at)}}
        ${{payload}}
        <details class="debug-section"><summary>Metadata</summary>${{kv('Object ID', node.object_id)}}</details>
        <details class="debug-section"><summary>Derived Counters</summary><pre>${{escapeHtml(JSON.stringify(node.derived || {{}}, null, 2))}}</pre></details>
        <details class="debug-section"><summary>Source Slots</summary><pre>${{escapeHtml(JSON.stringify(node.source_slots || [], null, 2))}}</pre></details>
        <details class="debug-section"><summary>Incoming Links</summary><pre>${{escapeHtml(JSON.stringify(incoming, null, 2))}}</pre></details>
        <details class="debug-section"><summary>Outgoing Links</summary><pre>${{escapeHtml(JSON.stringify(outgoing, null, 2))}}</pre></details>
      `;
    }}

    function eventInspector(node, incoming, outgoing) {{
      return `
        <h2>${{escapeHtml(node.label)}}</h2>
        ${{kv('Kind', 'Event Stream provenance')}}${{kv('Range', node.slot_seq_start === node.slot_seq_end ? `slot ${{node.slot_seq_start}}` : `slots ${{node.slot_seq_start}}-${{node.slot_seq_end}}`)}}
        ${{kv('Preview', node.preview || '')}}
        <details class="debug-section"><summary>Slots</summary><pre>${{escapeHtml(JSON.stringify(node.slots || [], null, 2))}}</pre></details>
        <details class="debug-section"><summary>Incoming Links</summary><pre>${{escapeHtml(JSON.stringify(incoming, null, 2))}}</pre></details>
        <details class="debug-section"><summary>Outgoing Links</summary><pre>${{escapeHtml(JSON.stringify(outgoing, null, 2))}}</pre></details>
      `;
    }}

    function payloadInspector(payload) {{
      if (!payload) return '<div class="section"><h3>Payload</h3><p class="muted">No payload.</p></div>';
      const body = payload.body ?? payload.content ?? payload.text;
      const bodyHtml = body === undefined || body === null || String(body) === ''
        ? '<p class="muted">No primary payload body.</p>'
        : `<div class="payload-body">${{escapeHtml(String(body))}}</div>`;
      return `
        <div class="section">
          <h3>Payload</h3>
          ${{bodyHtml}}
          <details class="raw-payload"><summary>Raw payload</summary><pre>${{escapeHtml(JSON.stringify(payload, null, 2))}}</pre></details>
        </div>
      `;
    }}

    function kv(key, value) {{
      return `<div class="kv"><strong>${{escapeHtml(key)}}</strong>${{escapeHtml(String(value ?? ''))}}</div>`;
    }}

    function el(name, attrs) {{
      const node = document.createElementNS('http://www.w3.org/2000/svg', name);
      for (const [key, value] of Object.entries(attrs || {{}})) node.setAttribute(key, value);
      return node;
    }}

    function title(text) {{
      const node = document.createElementNS('http://www.w3.org/2000/svg', 'title');
      node.textContent = text;
      return node;
    }}

    function escapeHtml(value) {{
      return String(value).replace(/[&<>"']/g, char => ({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[char]));
    }}

    function clamp(value, min, max) {{
      return Math.max(min, Math.min(max, value));
    }}

    draw();
  </script>
</body>
</html>
"""


def _connect_ltm_read_only(root=None):
    path = ltm_store.ltm_db_path(root)
    if not path.exists():
        raise FileNotFoundError(path)
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute(f"PRAGMA busy_timeout = {ltm_store.BUSY_TIMEOUT_MS}")
    return connection


def _load_object_nodes(connection):
    system_owned = _system_owned_map(connection)
    rows = connection.execute(
        """
        SELECT o.object_id, o.object_type, o.title, o.summary, o.created_at, o.updated_at,
               p.schema_version, p.body, p.structured_data,
               d.successful_use_count, d.gain_link_count, d.loss_link_count,
               d.neutral_link_count, d.strength, d.strength_formula_version,
               d.updated_at AS derived_updated_at
        FROM live_memory_objects AS o
        LEFT JOIN memory_payloads AS p ON p.object_id = o.object_id
        LEFT JOIN live_memory_object_derived AS d ON d.object_id = o.object_id
        ORDER BY o.object_type, o.created_at, o.object_id
        """
    ).fetchall()
    source_slots = _source_slots_by_object(connection)
    nodes = []
    for row in rows:
        derived = {
            "successful_use_count": int(row["successful_use_count"] or 0),
            "gain_link_count": int(row["gain_link_count"] or 0),
            "loss_link_count": int(row["loss_link_count"] or 0),
            "neutral_link_count": int(row["neutral_link_count"] or 0),
            "strength": float(row["strength"] if row["strength"] is not None else 0.0),
            "strength_formula_version": row["strength_formula_version"],
            "updated_at": row["derived_updated_at"],
        }
        object_type = row["object_type"]
        strength = derived["strength"]
        nodes.append({
            "id": row["object_id"],
            "kind": "memory_object",
            "object_id": row["object_id"],
            "object_type": object_type,
            "title": row["title"],
            "label": row["title"],
            "summary": row["summary"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "payload": _payload_from_row(row),
            "has_payload": row["schema_version"] is not None,
            "derived": derived,
            "strength": strength,
            "source_slots": source_slots.get(row["object_id"], []),
            "system_owned": system_owned.get(row["object_id"]),
            "display": {
                "shape": "circle",
                "color": OBJECT_TYPE_COLORS.get(object_type, "#64748b"),
                "strength": strength,
            },
        })
    return nodes


def _payload_from_row(row):
    if row["schema_version"] is None:
        return None
    return {
        "schema_version": int(row["schema_version"]),
        "body": row["body"],
        "structured_data": json.loads(row["structured_data"]) if row["structured_data"] else None,
    }


def _system_owned_map(connection):
    if not ltm_store._table_exists(connection, "system_owned_objects"):
        return {}
    rows = connection.execute(
        "SELECT tag_key, object_id, owner, protected_kind FROM system_owned_objects"
    ).fetchall()
    return {
        row["object_id"]: {
            "tag_key": row["tag_key"],
            "owner": row["owner"],
            "protected_kind": row["protected_kind"],
        }
        for row in rows
    }


def _source_slots_by_object(connection):
    rows = connection.execute(
        """
        SELECT source_object_id, slot_seq_start, slot_seq_end, link_id, link_state
        FROM live_memory_links
        WHERE relation_type = 'sourced_from' AND target_kind = 'event_slot'
        ORDER BY slot_seq_start, slot_seq_end, link_id
        """
    ).fetchall()
    grouped = {}
    for row in rows:
        grouped.setdefault(row["source_object_id"], []).append({
            "link_id": row["link_id"],
            "slot_seq_start": row["slot_seq_start"],
            "slot_seq_end": row["slot_seq_end"],
            "link_state": row["link_state"],
        })
    return grouped


def _load_links(connection):
    rows = connection.execute(
        """
        SELECT link_id, source_object_id, target_kind, target_object_id,
               slot_seq_start, slot_seq_end, relation_type, link_state,
               created_at, updated_at
        FROM live_memory_links
        ORDER BY created_at, link_id
        """
    ).fetchall()
    return [dict(row) for row in rows]


def _display_links(links):
    display = []
    for link in links:
        target = link["target_object_id"]
        if link["target_kind"] == "event_slot":
            target = _event_node_id(link["slot_seq_start"], link["slot_seq_end"])
        display.append({
            **link,
            "id": link["link_id"],
            "source": link["source_object_id"],
            "target": target,
            "physical_direction": "source_to_target",
            "strength_effect": STRENGTH_EFFECTS.get(link["relation_type"], "unknown"),
            "display": {
                "color": "#9ca3af" if link["link_state"] == "inactive" else LINK_TYPE_COLORS.get(link["relation_type"], "#64748b"),
                "dashed": link["link_state"] == "inactive",
            },
        })
    return display


def _event_nodes_for_links(links, root=None):
    ranges = sorted({
        (int(link["slot_seq_start"]), int(link["slot_seq_end"]))
        for link in links
        if link["target_kind"] == "event_slot"
        and link["slot_seq_start"] is not None
        and link["slot_seq_end"] is not None
    })
    slot_records = _read_event_slots_for_ranges(ranges, root)
    nodes = []
    for start, end in ranges:
        slots = [slot_records[seq] for seq in range(start, end + 1) if seq in slot_records]
        nodes.append({
            "id": _event_node_id(start, end),
            "kind": "event_slot",
            "label": f"slot {start}" if start == end else f"slots {start}-{end}",
            "slot_seq_start": start,
            "slot_seq_end": end,
            "preview": _preview_for_slots(slots),
            "slots": slots,
            "display": {
                "shape": "capsule",
                "color": OBJECT_TYPE_COLORS["event_slot"],
            },
        })
    return nodes


def _read_event_slots_for_ranges(ranges, root=None):
    path = event_stream.event_stream_db_path(root)
    if not ranges or not path.exists():
        return {}
    wanted = sorted({seq for start, end in ranges for seq in range(start, end + 1)})
    placeholders = ",".join("?" for _ in wanted)
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            f"""
            SELECT slots.slot_seq, slots.source, slots.active_event_id, slots.original_event_id,
                   events.kind AS active_kind, events.content_ref, events.inline_content,
                   events.token_count AS active_token_count
            FROM slots
            JOIN events ON events.event_id = slots.active_event_id
            WHERE slots.slot_seq IN ({placeholders})
            ORDER BY slots.slot_seq
            """,
            wanted,
        ).fetchall()
        return {int(row["slot_seq"]): _event_slot_record(connection, row) for row in rows}
    finally:
        connection.close()


def _event_slot_record(connection, row):
    content = _event_content(connection, row["content_ref"], row["inline_content"])
    return {
        "slot_seq": int(row["slot_seq"]),
        "source": row["source"],
        "active_event_id": row["active_event_id"],
        "original_event_id": row["original_event_id"],
        "active_kind": row["active_kind"],
        "active_token_count": int(row["active_token_count"] or 0),
        "preview": _preview_text(content),
    }


def _event_content(connection, content_ref, inline_content):
    if content_ref == "inline":
        try:
            return json.loads(inline_content or "{}")
        except json.JSONDecodeError:
            return inline_content or ""
    row = connection.execute(
        "SELECT storage_path FROM artifacts WHERE artifact_id = ?",
        (content_ref,),
    ).fetchone()
    if row is None:
        return {"missing_artifact": content_ref}
    try:
        return json.loads(Path(row["storage_path"]).read_text(encoding="utf-8"))
    except Exception as error:
        return {"artifact_read_error": str(error)}


def _preview_for_slots(slots):
    return " | ".join(slot.get("preview", "") for slot in slots if slot.get("preview"))[:EVENT_PREVIEW_CHARS]


def _preview_text(content):
    if isinstance(content, dict):
        text = str(content.get("content") or json.dumps(content, ensure_ascii=False, sort_keys=True))
    else:
        text = str(content)
    return " ".join(text.split())[:EVENT_PREVIEW_CHARS]


def _load_maintenance(connection, root=None):
    state = ltm_store.get_maintenance_runtime_state(root)
    reports = []
    if ltm_store._table_exists(connection, "maintenance_reports"):
        reports = [
            dict(row)
            for row in connection.execute(
                "SELECT * FROM maintenance_reports ORDER BY finished_at DESC LIMIT 5"
            ).fetchall()
        ]
    return {"runtime_state": state, "recent_reports": reports}


def _load_health(connection):
    health = {
        "derived": {
            "missing": _scalar(connection, "SELECT COUNT(*) AS count FROM live_memory_objects o LEFT JOIN live_memory_object_derived d ON d.object_id=o.object_id WHERE d.object_id IS NULL"),
            "stale_formula": _scalar(connection, "SELECT COUNT(*) AS count FROM live_memory_object_derived WHERE strength_formula_version != ?", (ltm_store.STRENGTH_FORMULA_VERSION,)),
        },
        "links": {
            "dangling_active": _scalar(
                connection,
                """
                SELECT COUNT(*) AS count
                FROM memory_links l
                LEFT JOIN memory_objects s ON s.object_id=l.source_object_id
                LEFT JOIN memory_objects t ON t.object_id=l.target_object_id
                WHERE l.link_state='active'
                  AND (s.object_id IS NULL OR (l.target_kind='memory_object' AND t.object_id IS NULL))
                """,
            )
        },
        "fts": {"initialized": ltm_store._table_exists(connection, ltm_store.LTM_FTS_TABLE)},
    }
    if health["fts"]["initialized"]:
        health["fts"].update({
            "object_count": _scalar(connection, f"SELECT COUNT(*) AS count FROM {ltm_store.LTM_FTS_TABLE}"),
            "store_count": _scalar(connection, "SELECT COUNT(*) AS count FROM live_memory_objects"),
        })
    return health


def _scalar(connection, query, params=()):
    row = connection.execute(query, params).fetchone()
    return int(row["count"] or 0) if row else 0


def _event_node_id(start, end):
    return f"event_slot:{int(start)}:{int(end)}"


def _timestamp_for_path():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%S-%fZ")


def _now():
    return datetime.now(timezone.utc).isoformat()
