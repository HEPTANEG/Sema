import json
import re
import subprocess
from pathlib import Path

from commands import command_registry, structured_commands
from memory_engine import event_stream
from memory_engine.ltm import ltm_store, ltm_visualization


def _id(char):
    return "mem_" + char * 32


def _object(object_id, title, body):
    return {
        "object_id": object_id,
        "object_type": "assertion",
        "title": title,
        "summary": None,
        "payload": {
            "schema_version": 1,
            "body": body,
            "structured_data": None,
        },
    }


def _derived_link(object_id, start, end):
    return {
        "source": {"object_id": object_id},
        "relation_type": "sourced_from",
        "target": {"event_slots": {"start": start, "end": end}},
    }


def test_ltm_visualization_missing_store_is_read_only(isolated_workspace):
    result = ltm_visualization.generate_ltm_visualization(isolated_workspace)

    assert result["status"] == "not_initialized"
    assert not ltm_store.ltm_db_path(isolated_workspace).exists()
    assert not ltm_visualization.visualization_root(isolated_workspace).exists()


def test_ltm_visualization_exports_snapshot_and_static_html(isolated_workspace):
    for index in range(4):
        event_stream.append_message({"role": "user", "content": f"slot {index}"}, root=isolated_workspace)
    first_id, second_id = _id("a"), _id("b")
    first = ltm_store.commit_write_plan(
        {"objects": [_object(first_id, "Tea preference", "prefers tea")], "links": [_derived_link(first_id, 0, 0)]},
        "visualization-first",
        isolated_workspace,
    )
    second = ltm_store.commit_write_plan(
        {"objects": [_object(second_id, "Coffee note", "secret payload marker")], "links": [_derived_link(second_id, 1, 2)]},
        "visualization-second",
        isolated_workspace,
    )
    support = ltm_store.commit_write_plan(
        {
            "objects": [],
            "links": [
                {
                    "source": {"object_id": first_id},
                    "relation_type": "supports",
                    "target": {"object_id": second_id},
                }
            ],
        },
        "visualization-support",
        isolated_workspace,
    )
    ltm_store.deactivate_memory_link(support["link_ids"][0], "visualization-deactivate", isolated_workspace)

    result = ltm_visualization.generate_ltm_visualization(isolated_workspace)

    assert result["status"] == "ok"
    snapshot_path = isolated_workspace / ".sema" / "memory" / "ltm" / "visualizations"
    assert str(snapshot_path) in result["snapshot_path"]
    snapshot = json.loads(Path(result["snapshot_path"]).read_text(encoding="utf-8"))
    html = Path(result["html_path"]).read_text(encoding="utf-8")
    object_nodes = [node for node in snapshot["nodes"] if node["kind"] == "memory_object"]
    event_nodes = [node for node in snapshot["nodes"] if node["kind"] == "event_slot"]

    assert first["status"] == "committed"
    assert second["status"] == "committed"
    assert snapshot["metadata"]["object_count"] == 2
    assert {node["object_id"] for node in object_nodes} == {first_id, second_id}
    assert {link["link_id"] for link in snapshot["links"]} >= {support["link_ids"][0]}
    assert any(link["link_state"] == "inactive" for link in snapshot["links"])
    assert {node["label"] for node in event_nodes} == {"slot 0", "slots 1-2"}
    assert all("slot 3" not in node["label"] for node in event_nodes)
    assert any(node["payload"]["body"] == "secret payload marker" for node in object_nodes)
    assert "secret payload marker" in html
    assert 'class="payload-body"' in html
    assert '<details class="raw-payload">' in html
    assert '<details class="raw-payload" open' not in html
    assert '<details class="debug-section">' in html
    assert '<details class="debug-section" open' not in html
    object_inspector = html[html.index("function objectInspector") : html.index("function eventInspector")]
    assert object_inspector.index("kv('Type', node.object_type)") < object_inspector.index("${payload}")
    assert object_inspector.index("${payload}") < object_inspector.index("<summary>Metadata</summary>")
    assert object_inspector.index("<summary>Metadata</summary>") < object_inspector.index("kv('Object ID', node.object_id)")
    assert "objectColors" in html
    assert "strengthColor" in html
    assert "linkColors" in html
    assert "stroke-dasharray" in html
    assert "function inspect(node)" in html
    assert "columns = ['tag'" not in html
    assert "column-label" not in html
    assert "function placeNodes" not in html
    assert "const FORCE_TICKS" in html
    assert "function applyLinkForce()" in html
    assert "function applyRepulsionForce()" in html
    assert "function applyCollisionForce()" in html
    assert "function applyCenterAndRadialForces()" in html
    assert "const EVENT_RADIAL_FORCE" in html
    assert "function setupZoomPan()" in html
    assert "function startDrag(event, node)" in html
    assert "function clippedLinkEndpoints(source, target)" in html
    assert "function directionArrowPath(line)" in html
    assert "function clientToSvg(clientX, clientY)" in html
    assert "function svgToGraph(point)" in html
    assert "function nodeBoundaryDistance(node, dx, dy)" in html
    assert "svg.getScreenCTM().inverse()" in html
    assert "const halfWidth = 62" in html
    assert "const halfHeight = 20" in html
    assert "class: `link-direction" in html
    assert "const DAMPING = 0.72" in html
    assert "const MAX_VELOCITY = 8" in html
    assert "const CLICK_MOVE_TOLERANCE = 5" in html
    assert "const LABEL_CHARS" in html
    assert "const LINK_DISTANCE = 210" in html
    assert "const COLLISION_PADDING = 20" in html
    assert "paint-order: stroke" in html
    assert "line-swatch" in html
    assert "Object.entries(linkColors)" in html
    assert "requestAnimationFrame(simulationFrame)" in html
    assert "'marker-end': 'url(#arrow)'" not in html
    assert "remainingTicks = Math.max(remainingTicks, 80)" not in html
    assert "group.addEventListener('click'" not in html
    assert "if (clickedNode) inspect(clickedNode)" in html
    assert "class: 'node-shape object-circle'" in html
    assert "class: 'node-shape event-capsule'" in html
    scripts = re.findall(r"<script(?: [^>]*)?>(.*?)</script>", html, flags=re.S)
    subprocess.run(["node", "--check"], input=scripts[-1], text=True, check=True)


def test_ltm_visualization_force_graph_contains_mixed_links_and_shapes(isolated_workspace):
    for index in range(3):
        event_stream.append_message({"role": "user", "content": f"event {index}"}, root=isolated_workspace)
    first_id, second_id = _id("d"), _id("e")
    ltm_store.commit_write_plan(
        {
            "objects": [
                _object(first_id, "Project tag", "tag payload"),
                _object(second_id, "Project assertion", "assertion payload"),
            ],
            "links": [
                _derived_link(first_id, 0, 1),
                _derived_link(second_id, 1, 2),
                {
                    "source": {"object_id": first_id},
                    "relation_type": "supports",
                    "target": {"object_id": second_id},
                },
            ],
        },
        "visualization-mixed-graph",
        isolated_workspace,
    )

    result = ltm_visualization.generate_ltm_visualization(isolated_workspace)

    snapshot = json.loads(Path(result["snapshot_path"]).read_text(encoding="utf-8"))
    html = Path(result["html_path"]).read_text(encoding="utf-8")
    node_kinds = {node["kind"] for node in snapshot["nodes"]}
    kinds_by_id = {node["id"]: node["kind"] for node in snapshot["nodes"]}
    link_targets = {(kinds_by_id[link["source"]], kinds_by_id[link["target"]]) for link in snapshot["links"]}

    assert node_kinds == {"memory_object", "event_slot"}
    assert ("memory_object", "memory_object") in link_targets
    assert ("memory_object", "event_slot") in link_targets
    assert "class: 'node-shape object-circle'" in html
    assert "class: 'node-shape event-capsule'" in html
    assert "x: 0, y: 1, 'text-anchor': 'middle'" in html


def test_ltm_visualize_command_is_registered_and_does_not_run_maintainer(isolated_workspace, monkeypatch):
    for index in range(1):
        event_stream.append_message({"role": "user", "content": f"slot {index}"}, root=isolated_workspace)
    object_id = _id("c")
    ltm_store.commit_write_plan(
        {"objects": [_object(object_id, "Debug node", "payload")], "links": [_derived_link(object_id, 0, 0)]},
        "visualization-command",
        isolated_workspace,
    )
    monkeypatch.setattr(
        structured_commands,
        "run_ltm_maintenance",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("Maintainer must not run")),
    )
    opened_paths = []
    monkeypatch.setattr(ltm_visualization, "_open_local_file", lambda path: opened_paths.append(path))

    assert "/memory ltm visualize" in command_registry.SLASH_COMMANDS
    assert command_registry.SLASH_COMMAND_HELP["/memory ltm visualize"]

    output = structured_commands.handle_memory_command("/memory ltm visualize", [], {})

    assert "LTM Visualization:" in output
    assert "status: ok" in output
    assert "snapshot_path:" in output
    assert "html_path:" in output
    assert "open_status: opened" in output
    assert len(opened_paths) == 1
    assert opened_paths[0].endswith("graph.html")
    assert not opened_paths[0].endswith("snapshot.json")


def test_ltm_visualize_command_does_not_open_missing_store(isolated_workspace, monkeypatch):
    opened_paths = []
    monkeypatch.setattr(ltm_visualization, "_open_local_file", lambda path: opened_paths.append(path))

    output = structured_commands.handle_memory_command("/memory ltm visualize", [], {})

    assert "status: not_initialized" in output
    assert opened_paths == []
