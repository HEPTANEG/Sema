import argparse
import contextlib
import hashlib
import io
import json
import os
import sys
import threading
import time
from pathlib import Path

from runtime import agent
from bridge.headless_runtime import patch_agent_for_headless
from persistence import session, trajectory
from eval.policy import EvalWorkspacePolicy
from memory_engine import context_builder, event_stream
from memory_engine.ltm import formation, ltm_store
from memory_engine.wm import ingress as wm_ingress
from memory_engine.wm import working_memory
from memory_engine.ltm import memory_push_ranking
from memory_engine.ltm import maintenance_pressure
from memory_engine.ltm.maintenance_runner import HALUMEM_SESSION_STAGES, run_ltm_maintenance
from memory_engine.ltm import maintenance_digest_cache
from memory_engine.observer import memory_engine_snapshot
from memory_engine.observer import context_build_preview
from runtime import maintenance_gate
from bridge.ui_bridge_server import BridgeServer


PROTOCOL = "sema-eval-bridge-v2"
EVAL_SESSION_STATE_FILE = Path("eval") / "halumem_sessions.json"
SYSTEM_ROOT_TAG_TITLES = {"others", "sema self", "user model", "safety"}


def main(argv=None):
    parser = argparse.ArgumentParser(prog="sema eval-bridge")
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--sema-home", required=True)
    parser.add_argument("--policy", choices=["eval-workspace"], default="eval-workspace")
    args = parser.parse_args(argv)

    workspace = Path(args.workspace).resolve()
    sema_home = Path(args.sema_home).resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    sema_home.mkdir(parents=True, exist_ok=True)

    old_cwd = Path.cwd()
    old_home = os.environ.get("SEMA_HOME")
    old_workspace_env = os.environ.get("SEMA_TUI_WORKSPACE_CWD")
    os.environ["SEMA_HOME"] = str(sema_home)
    os.environ["SEMA_TUI_WORKSPACE_CWD"] = str(workspace)

    output_lock = threading.Lock()
    events_lock = threading.Lock()
    events = []

    def emit_event(event):
        with events_lock:
            events.append(event)
        _write({"type": "event", "event": event}, output_lock=output_lock)

    def emit_policy_event(event):
        with events_lock:
            events.append(event)
        _write({"type": "event", "event": event}, output_lock=output_lock)

    def emit_provider_usage(event):
        emit_event({"type": "provider_usage", **event})

    server = BridgeServer(output_stream=io.StringIO(), event_sink=emit_event)
    server.runtime_state["ui_event_consumer"] = "eval"
    server.confirmations = EvalWorkspacePolicy(workspace, event_sink=emit_policy_event)
    trajectory.set_provider_usage_callback(emit_provider_usage)

    try:
        server.initialize({})
        server.permissions.update({"allow_run_commands": True, "allow_edits": True})
        _write({"type": "ready", "protocol": PROTOCOL}, output_lock=output_lock)
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                command = json.loads(line)
            except json.JSONDecodeError as error:
                _write({"type": "error", "error": f"invalid JSON: {error}"}, output_lock=output_lock)
                continue

            command_type = command.get("type")
            if command_type == "close":
                server._eval_worker_stop.set()
                server.wake_scheduler.stop()
                _write({"type": "closed"}, output_lock=output_lock)
                return 0
            if command_type == "seed_messages":
                try:
                    server.messages = _seed_messages(server.messages, command.get("messages") or [])
                    _write({"type": "seed_result", "message_count": len(server.messages)}, output_lock=output_lock)
                except Exception as error:
                    _write({"type": "error", "error": f"seed_messages failed: {error}"}, output_lock=output_lock)
                continue
            if command_type == "history_session":
                try:
                    session_date = _normalize_timestamp(command.get("session_date"))
                    server.messages = _append_history_session(
                        server.messages,
                        command.get("turns") or [],
                        session_date,
                    )
                    _write(
                        {
                            "type": "history_result",
                            "message_count": len(server.messages),
                            "session_id": str(command.get("session_id") or ""),
                            "session_date": session_date,
                            "maintenance": {"status": "compatibility_import"},
                        },
                        output_lock=output_lock,
                    )
                except Exception as error:
                    _write({"type": "error", "error": f"history_session failed: {error}"}, output_lock=output_lock)
                continue
            if command_type == "process_session":
                try:
                    result = _process_historical_session(
                        server,
                        command,
                        heartbeat_callback=lambda event: _write(
                            {"type": "event", "event": event},
                            output_lock=output_lock,
                        ),
                    )
                    _write({"type": "session_result", **result}, output_lock=output_lock)
                except Exception as error:
                    _write({"type": "error", "error": f"process_session failed: {error}"}, output_lock=output_lock)
                continue
            if command_type == "memory_search":
                try:
                    result = _run_eval_memory_search(server, command)
                    _write({"type": "memory_search_result", **result}, output_lock=output_lock)
                except Exception as error:
                    _write({"type": "error", "error": f"memory_search failed: {error}"}, output_lock=output_lock)
                continue
            if command_type == "user":
                try:
                    result = _run_runtime_user(server, command, events, output_lock, events_lock)
                except Exception as error:
                    result = {
                        "type": "turn_result",
                        "request_id": str(command.get("request_id") or ""),
                        "final_answer": "",
                        "assistant_messages": [],
                        "events": [],
                        "error": str(error),
                    }
                _write(result, output_lock=output_lock)
                continue
            if command_type == "memory_snapshot":
                try:
                    snapshot = memory_engine_snapshot(
                        messages=server.messages,
                        runtime_state=server.runtime_state,
                    )
                    _write({"type": "memory_snapshot", "snapshot": snapshot}, output_lock=output_lock)
                except Exception as error:
                    _write({"type": "error", "error": f"memory_snapshot failed: {error}"}, output_lock=output_lock)
                continue
            if command_type == "context_build_preview":
                try:
                    preview = context_build_preview(
                        messages=server.messages,
                        runtime_state=server.runtime_state,
                    )
                    _write({"type": "context_build_preview", "preview": preview}, output_lock=output_lock)
                except Exception as error:
                    _write({"type": "error", "error": f"context_build_preview failed: {error}"}, output_lock=output_lock)
                continue
            if command_type == "maintenance_status":
                _write(
                    {
                        "type": "maintenance_status",
                        "gate": maintenance_gate.user_turn_gate_status(),
                        "runtime_state": _maintenance_runtime_status(),
                        "runtime": server.runtime_status({}),
                    },
                    output_lock=output_lock,
                )
                continue
            if command_type == "runtime_status":
                _write({"type": "runtime_status", "runtime": server.runtime_status({})}, output_lock=output_lock)
                continue
            if command_type == "history_complete":
                _write(
                    {
                        "type": "history_complete",
                        "message_count": len(server.messages or []),
                        "pressure": maintenance_pressure.maintenance_pressure_snapshot(),
                    },
                    output_lock=output_lock,
                )
                continue
            if command_type == "configure_budgets":
                stm = int(command.get("stm") or 0)
                wm = int(command.get("wm") or 0)
                if stm < 2000 or wm < 2000:
                    _write({"type": "error", "error": "memory budgets must be at least 2000"}, output_lock=output_lock)
                    continue
                try:
                    server.execute_slash({"command": f"/memory budget set stm {stm}"})
                    result = server.execute_slash({"command": f"/memory budget set wm {wm}"})
                    _write({"type": "configure_budgets_result", "result": result}, output_lock=output_lock)
                except Exception as error:
                    _write({"type": "error", "error": f"configure_budgets failed: {error}"}, output_lock=output_lock)
                continue
            _write({"type": "error", "error": f"unknown command type: {command_type}"}, output_lock=output_lock)
    finally:
        from model.provider import close_provider_connections

        close_provider_connections()
        trajectory.set_provider_usage_callback(None)
        os.chdir(old_cwd)
        if old_home is None:
            os.environ.pop("SEMA_HOME", None)
        else:
            os.environ["SEMA_HOME"] = old_home
        if old_workspace_env is None:
            os.environ.pop("SEMA_TUI_WORKSPACE_CWD", None)
        else:
            os.environ["SEMA_TUI_WORKSPACE_CWD"] = old_workspace_env

    return 0


def _run_runtime_user(server, command, events, output_lock, events_lock):
    request_id = str(command.get("request_id") or "eval_" + os.urandom(8).hex())
    with events_lock:
        start_event_index = len(events)
    params = {
        "prompt": command.get("content") or "",
        "source": "eval",
        "request_id": request_id,
        "created_at": command.get("created_at"),
        "phase": command.get("phase") or "user",
        "parent_id": command.get("parent_id") or "",
    }
    accepted = server.send_message(params)
    completed = server.wait_eval_request(request_id, timeout=command.get("timeout_seconds"))
    server.forget_eval_request(request_id)
    result = dict(completed.get("result") or {})
    result.setdefault("type", "turn_result")
    result["request_id"] = request_id
    if command.get("wait_stable", False):
        result["stability"] = server.wait_runtime_stable(
            quiet_seconds=float(command.get("quiet_seconds", 1.0)),
            timeout_seconds=float(command.get("stable_timeout_seconds", 30.0)),
        )
        with events_lock:
            result["events"] = list(events)
            del events[:]
    else:
        with events_lock:
            result["events"] = list(events[start_event_index:])
            del events[:]
    result["accepted"] = accepted
    return result


def _run_turn(content, messages, permissions, runtime_state, created_at=None, phase="user"):
    maintenance_status = _apply_pressure_gate(runtime_state)
    before_snapshot = memory_engine_snapshot(messages=messages, runtime_state=runtime_state)
    context_preview = before_snapshot.get("context_preview") or {}
    session.append_message_and_persist(
        messages,
        {"role": "user", "content": content},
        created_at=_normalize_timestamp(created_at) if created_at else None,
    )
    captured = io.StringIO()
    error = None
    try:
        with contextlib.redirect_stdout(captured), contextlib.redirect_stderr(captured):
            agent.answer_with_tools(messages, permissions=permissions, runtime_state=runtime_state)
    except Exception as exc:
        error = str(exc)

    after_snapshot = memory_engine_snapshot(messages=messages, runtime_state=runtime_state)
    return {
        "type": "turn_result",
        "phase": phase,
        "final_answer": _final_answer(messages),
        "messages": messages,
        "tool_trace": _tool_trace(runtime_state.get("last_run") or []),
        "stdout": captured.getvalue(),
        "error": error,
        "maintenance_status": maintenance_status,
        "memory_snapshot_before_question": before_snapshot,
        "memory_snapshot_after_question": after_snapshot,
        "context_preview": context_preview,
    }


def _append_history_session(current_messages, turns, session_date):
    if not isinstance(turns, list):
        raise RuntimeError("turns must be a list")
    history = []
    timestamps = [None] * len(current_messages)
    for turn in turns:
        if not isinstance(turn, dict) or turn.get("role") not in {"user", "assistant"}:
            raise RuntimeError("history turns must contain only user/assistant messages")
        content = turn.get("content")
        if not isinstance(content, str):
            raise RuntimeError("history turn content must be text")

        history.append({"role": turn["role"], "content": content})
        timestamps.append(_normalize_timestamp(turn.get("timestamp") or session_date))
    seeded = [*current_messages, *history]
    session.save_last_session(seeded, created_at_by_index=timestamps)
    return seeded


def _object_snapshot(root=None):
    objects = {}
    for object_type in ltm_store.OBJECT_TYPES:
        for item in ltm_store.list_objects(object_type, include_payload=True, root=root):
            object_id = item.get("object_id")
            if object_id:
                objects[object_id] = item
    return objects


def _source_ranges(object_id, root=None):
    ranges = []
    for link in ltm_store.get_links(object_id, "outgoing", relation_type="sourced_from", root=root):
        if link.get("target_kind") != "event_slot":
            continue
        try:
            ranges.append({"start": int(link["slot_seq_start"]), "end": int(link["slot_seq_end"])})
        except (KeyError, TypeError, ValueError):
            continue
    return ranges


def _range_overlaps(ranges, start, end):
    return any(int(item.get("start", 1)) <= end and int(item.get("end", -1)) >= start for item in ranges)


def _memory_text(memory_object):
    parts = [str(memory_object.get("title") or "").strip(), str(memory_object.get("summary") or "").strip()]
    payload = memory_object.get("payload") or {}
    body = str(payload.get("body") or "").strip()
    if body:
        parts.append(body)
    structured = payload.get("structured_data")
    if structured:
        parts.append(json.dumps(structured, ensure_ascii=False, sort_keys=True))
    return "\n".join(part for part in parts if part)


def _eval_session_state_path():
    return Path(os.environ["SEMA_HOME"]) / EVAL_SESSION_STATE_FILE


def _load_eval_session_state():
    try:
        value = json.loads(_eval_session_state_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _save_eval_session_state(state):
    path = _eval_session_state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f".{os.getpid()}.tmp")
    temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def _session_fingerprint(session_id, session_date, turns):
    payload = json.dumps(
        {"session_id": session_id, "session_date": session_date, "turns": turns},
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _is_system_root_tag(item):
    title = str(item.get("title") or "").strip().casefold()
    return title in SYSTEM_ROOT_TAG_TITLES


def _safe_history_turns(turns):
    return [{"role": turn["role"], "content": turn.get("content") or ""} for turn in turns]


def _matched_history_count(expected_history, existing_history):
    """Return the historical prefix already persisted for a session.

    Runtime-generated messages may be appended after a historical session when
    a later maintenance or wake operation outlives a failed request. They are
    outside the session's expected prefix and must not cause a replay conflict.
    """
    if existing_history and existing_history[:len(expected_history)] != expected_history:
        raise RuntimeError("existing history differs")
    return min(len(existing_history), len(expected_history))


DEFAULT_OPERATION_HEARTBEAT_SECONDS = 15
DEFAULT_OPERATION_HARD_TIMEOUT_SECONDS = 900


def _run_with_timeout(
    function,
    timeout_seconds,
    heartbeat_callback=None,
    heartbeat_seconds=DEFAULT_OPERATION_HEARTBEAT_SECONDS,
    hard_timeout_seconds=None,
):
    holder = {}

    def worker():
        try:
            holder["result"] = function()
        except Exception as error:
            holder["error"] = error

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    if heartbeat_callback is None:
        thread.join(max(1, int(timeout_seconds)))
        if thread.is_alive():
            raise TimeoutError(f"operation timed out after {int(timeout_seconds)}s")
    else:
        hard_timeout = int(hard_timeout_seconds or DEFAULT_OPERATION_HARD_TIMEOUT_SECONDS)
        started = time.monotonic()
        deadline = started + max(1, hard_timeout)
        interval = max(1, int(heartbeat_seconds))
        while thread.is_alive():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"operation hard timeout after {hard_timeout}s")
            thread.join(min(interval, remaining))
            if thread.is_alive():
                try:
                    heartbeat_callback({
                        "elapsed_seconds": int(time.monotonic() - started),
                        "hard_timeout_seconds": hard_timeout,
                    })
                except Exception:
                    pass

    if holder.get("error"):
        raise holder["error"]
    return holder.get("result")


def _process_historical_session(server, command, heartbeat_callback=None):
    turns = command.get("turns") or []
    if not isinstance(turns, list) or not turns:
        raise RuntimeError("process_session requires a non-empty turns list")
    session_id = str(command.get("session_id") or "")
    if not session_id:
        raise RuntimeError("process_session requires session_id")
    session_date = _normalize_timestamp(command.get("session_date"))
    fingerprint = _session_fingerprint(session_id, session_date, turns)
    state = _load_eval_session_state()
    previous = state.get(session_id)
    if previous:
        if previous.get("fingerprint") != fingerprint:
            raise RuntimeError(f"session_id {session_id} was already used with different content")
        if previous.get("status") == "completed" and isinstance(previous.get("result"), dict):
            return {**previous["result"], "idempotent_replay": True}
        if previous.get("status") in {"started", "appended"} and not isinstance(
            previous.get("before_objects"), dict
        ):
            raise RuntimeError(
                "Cannot safely resume this session: the checkpoint was created "
                "before retry baselines were recorded. Start a fresh HaluMem run."
            )



    recorded_before_slot = None
    recorded_before_objects = None
    if isinstance(previous, dict) and previous.get("status") in {"started", "appended"}:
        try:
            recorded_before_slot = int(previous.get("before_slot"))
        except (TypeError, ValueError):
            recorded_before_slot = None
        if isinstance(previous.get("before_objects"), dict):
            recorded_before_objects = previous.get("before_objects")
    before_slot = recorded_before_slot if recorded_before_slot is not None else event_stream.event_stream_message_count()
    before_objects = (
        recorded_before_objects
        if recorded_before_objects is not None
        else _object_snapshot()
    )
    previous_digest = maintenance_digest_cache.load_latest_staged_summary()
    previous_stages = (previous or {}).get("stages") if isinstance(previous, dict) else {}
    state[session_id] = {
        "status": "started",
        "fingerprint": fingerprint,
        "before_slot": before_slot,
        "session_date": session_date,
        "message_count": len(turns),
        "started_at": time.time(),
        "stages": dict(previous_stages or {}),
    }
    if recorded_before_slot is not None:
        state[session_id]["started_at"] = previous.get("started_at") or time.time()
    state[session_id]["before_objects"] = before_objects
    _save_eval_session_state(state)
    expected_history = _safe_history_turns(turns)
    existing_history = [
        {"role": message.get("role"), "content": message.get("content") or ""}
        for message in (server.messages or [])[before_slot:]
        if message.get("role") in {"user", "assistant"}
    ]



    try:
        matched_history_count = _matched_history_count(expected_history, existing_history)
    except RuntimeError as error:
        raise RuntimeError(f"checkpoint conflict for session_id {session_id}: {error}") from error
    if matched_history_count < len(expected_history):
        server.messages = _append_history_session(
            server.messages,
            turns[matched_history_count:],
            session_date,
        )


    after_slot = before_slot + len(expected_history) - 1
    historical_slot_range = {"start": before_slot, "end": after_slot}
    state[session_id].update({
        "status": "appended",
        "session_slot_range": {"start": before_slot, "end": after_slot},
        "historical_slot_range": historical_slot_range,
    })
    _save_eval_session_state(state)
    def emit_heartbeat(detail):
        if heartbeat_callback is None:
            return
        heartbeat_callback({
            "type": "bridge_operation_heartbeat",
            "operation": "process_session",
            "session_id": session_id,
            **detail,
        })

    operation_hard_timeout = int(
        command.get("operation_hard_timeout_seconds")
        or DEFAULT_OPERATION_HARD_TIMEOUT_SECONDS
    )

    if (previous_stages or {}).get("formation", {}).get("status") == "completed":
        formation_result = (previous_stages.get("formation") or {}).get("result") or {}
        formation_result = {**formation_result, "idempotent_replay": True}
    else:
        formation_result = _run_with_timeout(
            lambda: formation.run_formation_maintenance(
                server.messages,
                runtime_state=server.runtime_state,
                force=True,
                max_tool_calls=(
                    int(command["formation_retrieval_tool_limit"])
                    if command.get("formation_retrieval_tool_limit") is not None
                    else None
                ),
                historical_slot_range=historical_slot_range,
            ),
            command.get("formation_timeout_seconds") or command.get("maintenance_timeout_seconds") or 180,
            heartbeat_callback=lambda detail: emit_heartbeat({"phase": "formation", **detail}),
            hard_timeout_seconds=operation_hard_timeout,
        ) or {}
    if formation_result.get("error"):
        raise RuntimeError(f"Formation failed: {formation_result['error']}")
    state[session_id]["stages"]["formation"] = {
        "status": "completed",
        "completed_at": time.time(),
        "result": formation_result,
    }
    _save_eval_session_state(state)
    previous_maintenance = (previous_stages or {}).get("maintenance") or {}
    prior_stage_summaries = dict(previous_maintenance.get("stage_summaries") or {})
    completed_stage_names = list(previous_maintenance.get("completed_stages") or [])
    start_stage_index = 0
    while (
        start_stage_index < len(HALUMEM_SESSION_STAGES)
        and HALUMEM_SESSION_STAGES[start_stage_index] in completed_stage_names
        and HALUMEM_SESSION_STAGES[start_stage_index] in prior_stage_summaries
    ):
        start_stage_index += 1
    state[session_id]["stages"]["maintenance"] = {
        "status": "running",
        "started_at": time.time(),
        "completed_stages": completed_stage_names,
        "stage_summaries": prior_stage_summaries,
        "current_stage_index": start_stage_index,
    }
    _save_eval_session_state(state)

    def maintenance_progress(detail):
        current = state[session_id]["stages"]["maintenance"]
        current["current_stage_index"] = int(detail.get("stage_index", current.get("current_stage_index", 0)))
        current["current_stage"] = detail.get("stage") or ""
        current["current_stage_status"] = detail.get("status") or ""
        if detail.get("status") == "completed":
            stage = detail.get("stage") or ""
            if stage not in current["completed_stages"]:
                current["completed_stages"].append(stage)
            if detail.get("stage_summary"):
                current["stage_summaries"][stage] = detail["stage_summary"]
        _save_eval_session_state(state)

    def maintenance_heartbeat(detail):
        current = state[session_id]["stages"].get("maintenance") or {}
        emit_heartbeat({
            "phase": "ltm_maintainer",
            "stage": current.get("current_stage") or "",
            "stage_index": current.get("current_stage_index", 0),
            **detail,
        })

    maintenance_result = _run_with_timeout(
        lambda: run_ltm_maintenance(
            runtime_state=server.runtime_state,
            force=True,
            stages=HALUMEM_SESSION_STAGES,
            stage_summary_max_tokens=int(command.get("stage_summary_max_tokens") or 1000),
            start_stage_index=start_stage_index,
            initial_stage_summaries=prior_stage_summaries,
            progress_callback=maintenance_progress,
            historical_slot_range=historical_slot_range,
        ),
        command.get("maintenance_timeout_seconds") or 180,
        heartbeat_callback=maintenance_heartbeat,
        hard_timeout_seconds=operation_hard_timeout,
    ) or {}
    if maintenance_result.get("failure_reason") or maintenance_result.get("status") == "failed":
        raise RuntimeError(maintenance_result.get("failure_reason") or "Maintainer failed")
    maintenance_result = {
        **maintenance_result,
        "digest_stage_count": len([stage for stage in HALUMEM_SESSION_STAGES if stage != "completion"]),
        "previous_digest_consumed": bool(previous_digest),
    }
    maintenance_checkpoint = state[session_id]["stages"].get("maintenance") or {}
    state[session_id]["stages"]["maintenance"] = {
        "status": "completed",
        "completed_at": time.time(),
        "completed_stages": maintenance_checkpoint.get("completed_stages") or list(HALUMEM_SESSION_STAGES),
        "stage_summaries": maintenance_checkpoint.get("stage_summaries") or {},
        "result": maintenance_result,
    }
    _save_eval_session_state(state)
    after_objects = _object_snapshot()
    changed = []
    unavailable = []
    for object_id, item in after_objects.items():
        if before_objects.get(object_id) != item:
            if _is_system_root_tag(item):
                continue
            ranges = _source_ranges(object_id)
            if _range_overlaps(ranges, before_slot, after_slot):
                changed.append({
                    "object_id": object_id,
                    "object_type": item.get("object_type"),
                    "memory": _memory_text(item),
                    "created_at": item.get("created_at"),
                    "updated_at": item.get("updated_at"),
                    "source_ranges": ranges,
                    "is_new": object_id not in before_objects,
                })
            else:
                unavailable.append({
                    "object_id": object_id,
                    "title": item.get("title"),
                    "memory": _memory_text(item),
                    "source_ranges": ranges,
                    "reason": "unavailable_provenance",
                })
    result = {
        "session_id": session_id,
        "session_slot_range": {"start": before_slot, "end": after_slot},
        "historical_session_finalization": True,
        "historical_slot_range": historical_slot_range,
        "message_count": len(turns),
        "formation": formation_result,
        "maintenance": maintenance_result,
        "extracted_memories": changed,
        "unavailable_changed_objects": unavailable,
        "unavailable_changed_object_count": len(unavailable),
        "system_root_tags_excluded": sorted(SYSTEM_ROOT_TAG_TITLES),
    }
    state[session_id].update({"status": "completed", "completed_at": time.time(), "result": result})
    _save_eval_session_state(state)
    return result


def _run_eval_memory_search(server, command):
    query = str(command.get("query") or "").strip()
    if not query:
        raise RuntimeError("memory_search requires a non-empty query")
    top_k = int(command.get("top_k") or 20)
    if not 1 <= top_k <= memory_push_ranking.RANK_3_MAX_ITEMS:
        raise RuntimeError(f"top_k must be between 1 and {memory_push_ranking.RANK_3_MAX_ITEMS}")
    result = memory_push_ranking.run_memory_push(
        [{"role": "user", "content": query}],
        [],
        runtime_state=server.runtime_state,
        mode="full",
        max_selected_items=top_k,
    )
    selected = list(result.get("selected_objects") or [])[:top_k]
    memories = []
    for index, item in enumerate(selected, 1):
        memories.append({
            "memory": _memory_text(item),
            "timestamp": item.get("created_at"),
            "updated_at": item.get("updated_at"),
            "score": None,
            "rank": index,
            "object_id": item.get("object_id"),
        })
    rendered = [f"{item.get('timestamp')}: {item.get('memory')}" for item in memories]
    return {
        "query": query,
        "top_k": top_k,
        "purpose": command.get("purpose") or "qa",
        "status": result.get("status") or "ok",
        "memories": memories,
        "context": json.dumps(rendered, ensure_ascii=False, indent=4),
        "metrics": result.get("metrics") or {},
    }


def _normalize_timestamp(value):
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        from datetime import datetime, timezone

        return datetime.fromtimestamp(value, tz=timezone.utc).isoformat()
    text = str(value).strip()
    if not text:
        return None
    from datetime import datetime, timezone

    normalized = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        parsed = None
        for date_format in (
            "%b %d, %Y, %H:%M:%S",
            "%b %d, %Y, %H:%M",
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%d %H:%M",
            "%Y/%m/%d (%a) %H:%M:%S",
            "%Y/%m/%d (%a) %H:%M",
            "%Y/%m/%d %H:%M:%S",
            "%Y/%m/%d %H:%M",
        ):
            try:
                parsed = datetime.strptime(text, date_format)
                break
            except ValueError:
                continue
        if parsed is None:
            raise ValueError(f"unsupported timestamp format: {text}")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


def _run_history_maintenance(messages, runtime_state, event_log, timeout_seconds=180):
    event_log.clear()
    thread = agent.schedule_event_stream_memory_maintenance(messages, runtime_state)
    thread.join(timeout_seconds)
    if thread.is_alive():
        return {"status": "timeout", "error": f"history maintenance timed out after {timeout_seconds}s"}
    errors = [
        event.get("error")
        for event in event_log
        if isinstance(event, dict) and event.get("error")
    ]
    return {"status": "failed" if errors else "completed", "error": errors[-1] if errors else ""}


def _apply_pressure_gate(runtime_state, timeout_seconds=180):
    gate = maintenance_gate.user_turn_gate_status()
    pressure = gate["pressure"]
    if not gate["blocked"]:
        return {"status": "not_blocked", "pressure": pressure}

    result_holder = {}

    def worker():
        try:
            result_holder["result"] = run_ltm_maintenance(runtime_state=runtime_state, force=True)
        except Exception as error:
            result_holder["error"] = str(error)

    import threading

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    thread.join(timeout_seconds)
    if thread.is_alive():
        raise RuntimeError(f"automatic LTM maintenance timed out after {timeout_seconds}s")
    if result_holder.get("error"):
        raise RuntimeError(f"automatic LTM maintenance failed: {result_holder['error']}")
    result = result_holder.get("result") or {}
    if result.get("failure_reason") or result.get("status") == "failed":
        raise RuntimeError(f"automatic LTM maintenance failed: {result.get('failure_reason') or result}")
    return {"status": "completed", "pressure_before": pressure, "result": result}


def _maintenance_runtime_status():
    from memory_engine.ltm import ltm_store

    return ltm_store.get_maintenance_runtime_state()


def _seed_messages(current_messages, tail_messages):
    if not isinstance(tail_messages, list):
        raise RuntimeError("messages must be a list")
    system = current_messages[:1] or agent.create_initial_messages()
    seeded = [*system, *tail_messages]
    session.save_last_session(seeded)
    return seeded


def _stage_wm_ingress(messages, runtime_state, commit=False):
    pressure = (runtime_state or {}).get("context_pressure") or {}
    budgets = pressure.get("budgets") or {}
    stm_budget = int(budgets.get("stm") or 10000)
    watermark = wm_ingress.effective_wm_ingress_watermark(runtime_state)
    projection = context_builder.build_stm_projection(
        messages,
        budget=stm_budget,
        wm_ingress_watermark=watermark,
    )
    staged = wm_ingress.stage_wm_ingress_from_projection(runtime_state, projection)
    commit_result = wm_ingress.commit_staged_wm_ingress_watermark(runtime_state) if commit else {}
    return {
        "staged_watermark": staged,
        "commit_result": commit_result,
        "selected_units": [_block_metadata(unit) for unit in projection.get("selected_units") or []],
        "omitted_units": [_block_metadata(unit) for unit in projection.get("omitted_units") or []],
        "watermark_locked_omitted_unit_count": projection.get("watermark_locked_omitted_unit_count", 0),
    }


def _block_metadata(block):
    return {
        "kind": block.get("kind", "interaction"),
        "start_index": int(block.get("start_index", 0)),
        "end_index": int(block.get("end_index", 0)),
        "message_count": int(block.get("message_count", 0)),
        "token_count": int(block.get("token_count", 0)),
    }


def _final_answer(messages):
    for message in reversed(messages):
        if message.get("role") == "assistant":
            return message.get("content") or ""
    return ""


def _tool_trace(steps):
    trace = []
    for step in steps:
        trace.append(
            {
                "tool": step.get("tool"),
                "args": step.get("args"),
                "result": step.get("result"),
                "error": step.get("error"),
                "duration_ms": step.get("duration_ms"),
            }
        )
    return trace


def _write(payload, output_lock=None):
    lock = output_lock or threading.Lock()
    with lock:
        sys.stdout.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
        sys.stdout.flush()

if __name__ == "__main__":
    raise SystemExit(main())
