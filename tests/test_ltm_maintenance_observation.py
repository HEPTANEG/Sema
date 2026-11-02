from memory_engine import event_stream
from memory_engine.ltm import ltm_store, maintenance_observation, meta_memory
import pytest


def _plan(slot):
    return {"objects": [{"object_id": "mem_" + "a" * 32, "object_type": "assertion", "title": "Preference", "summary": None, "payload": {"schema_version": 1, "body": "prefers tea"}}], "links": [{"source": {"object_id": "mem_" + "a" * 32}, "relation_type": "sourced_from", "target": {"event_slots": {"start": slot, "end": slot}}}]}


def test_observation_tools_are_read_only_and_obey_slot_and_bundle_defaults(isolated_workspace):
    event_stream.append_message({"role": "user", "content": "tea"}, root=isolated_workspace)
    write = ltm_store.commit_write_plan(_plan(0), "maintenance-observation", isolated_workspace)
    assert write["status"] == "committed", write
    objects = maintenance_observation.run_maintenance_observation_tool("get_object", {"title": "preference", "projection": "bundle"}, isolated_workspace)
    assert objects["status"] == "ok" and objects["items"][0]["derived"]["strength"] > 0.5
    assert objects["items"][0]["source_slots"] == [{"slot_seq_start": 0, "slot_seq_end": 0}]
    slots = maintenance_observation.run_maintenance_observation_tool("read_event_slots", {}, isolated_workspace)
    assert [item["slot_seq"] for item in slots["target_slots"]] == [0]
    assert slots["coverage_eligible_slot_seqs"] == [0]
    assert slots["existing_sourced_objects"][0]["object_id"] == "mem_" + "a" * 32
    assert {schema["function"]["name"] for schema in maintenance_observation.maintainer_observation_tool_schemas()} == set(maintenance_observation.OBSERVATION_TOOLS)
    assert maintenance_observation.run_maintenance_observation_tool("get_links", {}, isolated_workspace)["items"]
    assert maintenance_observation.run_maintenance_observation_tool("traverse_objects", {"start_object_ids": ["mem_" + "a" * 32]}, isolated_workspace)["status"] == "ok"
    assert maintenance_observation.run_maintenance_observation_tool("get_overall_health", {}, isolated_workspace)["status"] == "ok"


def test_overall_health_does_not_require_system_owned_tags_in_vector_index(isolated_workspace):
    meta_memory.bootstrap_system_tags(isolated_workspace)

    health = maintenance_observation.get_overall_health({}, isolated_workspace)

    assert health["status"] == "ok"
    assert health["vector"]["object_count"] == 0
    assert health["vector"]["expected_object_count"] == 0
    assert health["vector"]["missing_objects"] == 0


def _search_objects(root):
    objects = [
        {"object_id": "mem_" + char * 32, "object_type": "assertion", "title": title,
         "summary": None, "payload": {"schema_version": 1, "body": body}}
        for char, title, body in [
            ("a", "ThinkPad 内存", "ThinkPad 15GB 内存"),
            ("b", "ThinkPad 8GB", "ThinkPad ThinkPad ThinkPad"),
            ("c", "ThinkPad", "Laptop unrelated text"),
            ("d", "Sema files", '.sema foo(bar) quote"word OR NOT NEAR check'),
        ]
    ]
    result = ltm_store.commit_write_plan({"objects": objects, "links": []}, "search-fixture", root)
    assert result["status"] == "committed", result
    with ltm_store._connect(root) as connection:
        connection.execute("UPDATE memory_object_derived SET strength=0.99 WHERE object_id=?", ("mem_" + "c" * 32,))
        connection.execute("UPDATE memory_object_derived SET strength=0.55 WHERE object_id=?", ("mem_" + "a" * 32,))


def test_keyword_or_coverage_order_dedup_and_filters(isolated_workspace):
    _search_objects(isolated_workspace)
    read = lambda args: maintenance_observation.get_object(args, isolated_workspace)
    result = read({"query": "ThinkPad THINKPAD 内存 15GB 8GB"})
    assert [x["matched_query_term_count"] for x in result["items"]] == [3, 2, 1]
    assert [x["object_id"] for x in result["items"]] == ["mem_" + c * 32 for c in "abc"]
    assert read({"query":"ThinkPad 内存 15GB", "limit":1})["items"][0]["object_id"] == "mem_" + "a"*32
    assert read({"query":"ThinkPad 内存 15GB", "min_strength":0.9})["items"][0]["object_id"] == "mem_" + "c"*32
    assert read({"query":"ThinkPad", "object_ids":["mem_"+"b"*32], "projection":"bundle"})["items"][0]["matched_query_term_count"] == 1
    assert "matched_query_term_count" not in read({"title":"ThinkPad"})["items"][0]


@pytest.mark.parametrize("query", ['.sema', 'foo(bar)', 'quote"word', 'OR', 'NOT', 'NEAR', 'check'])
def test_query_special_characters_are_literal(isolated_workspace, query):
    _search_objects(isolated_workspace)
    result = maintenance_observation.run_maintenance_observation_tool("get_object", {"query":query}, isolated_workspace)
    assert result["status"] == "ok", result
    assert {r["object_id"] for r in result["items"]} == {"mem_"+"d"*32}


@pytest.mark.parametrize("query", ["", "   ", "\n\t", '"()***'])
def test_empty_or_unsearchable_query_does_not_return_all(isolated_workspace, query):
    _search_objects(isolated_workspace)
    assert maintenance_observation.get_object({"query":query}, isolated_workspace)["status"] == "rejected"


def test_short_unicode_query_and_stable_ties(isolated_workspace):
    _search_objects(isolated_workspace)
    assert maintenance_observation.get_object({"query":"内存"}, isolated_workspace)["items"][0]["object_id"] == "mem_"+"a"*32
    with ltm_store._connect(isolated_workspace) as connection:
        connection.execute("UPDATE memory_object_derived SET strength=0.5")
        connection.execute("UPDATE memory_objects SET created_at='2020-01-01T00:00:00Z'")
    result = maintenance_observation.get_object({"query":"ThinkPad"}, isolated_workspace)
    assert [r["object_id"] for r in result["items"]] == ["mem_"+c*32 for c in "abc"]


@pytest.mark.parametrize("tool,args", [
    ("get_links",{"source_object_id":"mem_x"}), ("get_object",{"object_type":"assertion"}),
    ("traverse_objects",{"object_ids":["mem_x"]}), ("read_event_slots",{"end_slot_seq":3}),
    ("get_object",{"limit":"20"}), ("get_links",{"include_inactive":1}),
    ("get_object",{"min_strength":.9,"max_strength":.1}),
    ("get_object",{"object_id":"a","object_ids":["b"]}),
    ("get_object",{"source_slot_after":5,"source_slot_before":2}),
    ("read_event_slots",{"limit":-1}), ("get_overall_health",{"repair":True}),
])
def test_observation_contract_rejects_before_read(tool,args,monkeypatch):
    monkeypatch.setattr(maintenance_observation,"_connection",lambda *a:pytest.fail("must reject before opening store"))
    result=maintenance_observation.run_maintenance_observation_tool(tool,args)
    assert result["status"]=="rejected"
    assert result["error"]["path"]
    assert "allowed_fields" in result["error"]


def test_sparse_schema_and_null_defaults(isolated_workspace):
    _search_objects(isolated_workspace)
    for schema in maintenance_observation.maintainer_observation_tool_schemas():
        function=schema["function"]
        assert function["parameters"]["additionalProperties"] is False
        assert set(function["parameters"]["properties"])==set(maintenance_observation.OBSERVATION_PARAMETERS[function["name"]])
    result=maintenance_observation.get_object({"object_ids":["mem_"+"a"*32],"projection":"payload","limit":None},isolated_workspace)
    assert result["items"][0]["payload"]["body"]=="ThinkPad 15GB 内存"


def test_declared_link_traversal_and_slot_parameters_execute(isolated_workspace):
    for content in ("first", "second", "third"):
        event_stream.append_message({"role":"user","content":content},root=isolated_workspace)
    assert ltm_store.commit_write_plan(_plan(0),"contract-seed",isolated_workspace)["status"]=="committed"
    observe=lambda name,args:maintenance_observation.run_maintenance_observation_tool(name,args,isolated_workspace)
    assert observe("get_links",{"source_object_ids":["mem_"+"a"*32]})["items"]
    assert observe("get_links",{"source_object_ids":["mem_"+"b"*32]})["items"]==[]
    assert observe("traverse_objects",{"start_object_ids":["mem_"+"a"*32],"projection":"payload"})["items"][0]["payload"]["body"]=="prefers tea"
    page=observe("read_event_slots",{"start_slot_seq":1,"limit":1,"include_adjacent_context":False})
    assert [s["slot_seq"] for s in page["target_slots"]]==[1]
    assert page["context_slots"]==[]
    next_page=observe("read_event_slots",{"start_slot_seq":2,"limit":1})
    assert [s["slot_seq"] for s in next_page["target_slots"]]==[2]
