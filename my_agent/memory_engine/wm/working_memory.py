import json
from datetime import datetime, timezone

import memory_engine.event_stream as event_stream
from . import ingress as wm_ingress
from model.provider import call_chat_completion
from shared.prompt_loader import load_prompt
from .. import evidence
from ..stm_projection import stm_projection_status
from ..token_counter import count_text_tokens










WORKING_MEMORY_TIMEOUT_SECONDS = 45
WORKING_MEMORY_AGENT = "working-memory-maintainer-v1"
SUM_TRIGGER_TOKENS = 400
SUM_MAX_RETRIES = 1



def _now():
    return datetime.now(timezone.utc).isoformat()



def _ensure_tables(connection):
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS working_memory_state (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS working_memory_slot_state (
            slot_seq INTEGER PRIMARY KEY,
            status TEXT NOT NULL,
            action TEXT NOT NULL,
            reason TEXT NOT NULL,
            original_event_id TEXT,
            sum_event_id TEXT,
            original_token_count INTEGER NOT NULL DEFAULT 0,
            summary_token_count INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL
        )
        """
    )
    connection.commit()


def _state_get(connection, key, default=None):
    row = connection.execute(
        "SELECT value FROM working_memory_state WHERE key = ?",
        (key,),
    ).fetchone()
    if row is None:
        return default
    return row["value"]


def _state_set(connection, key, value):
    connection.execute(
        """
        INSERT INTO working_memory_state (key, value)
        VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """,
        (key, str(value)),
    )


def _has_table(connection, table):
    row = connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()
    return row is not None


def _record_slot_state(connection, slot_seq, status, action, reason, slot=None, sum_event_id="", summary_token_count=0):
    slot = slot or {}
    connection.execute(
        """
        INSERT INTO working_memory_slot_state
            (slot_seq, status, action, reason, original_event_id, sum_event_id, original_token_count, summary_token_count, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(slot_seq) DO UPDATE SET
            status = excluded.status,
            action = excluded.action,
            reason = excluded.reason,
            original_event_id = excluded.original_event_id,
            sum_event_id = excluded.sum_event_id,
            original_token_count = excluded.original_token_count,
            summary_token_count = excluded.summary_token_count,
            updated_at = excluded.updated_at
        """,
        (
            int(slot_seq),
            status,
            action,
            str(reason or ""),
            slot.get("original_event_id") or "",
            sum_event_id or "",
            int(slot.get("active_token_count") or slot.get("original_token_count") or 0),
            int(summary_token_count or 0),
            _now(),
        ),
    )


def _slot_records(connection):
    has_slot_state = _has_table(connection, "working_memory_slot_state")
    state_columns = (
        """
            state.status AS wm_status,
            state.action AS wm_action,
            state.reason AS wm_reason,
            state.sum_event_id AS wm_sum_event_id,
            state.summary_token_count AS wm_summary_token_count,
            state.updated_at AS wm_updated_at
        """
        if has_slot_state
        else """
            NULL AS wm_status,
            NULL AS wm_action,
            NULL AS wm_reason,
            NULL AS wm_sum_event_id,
            NULL AS wm_summary_token_count,
            NULL AS wm_updated_at
        """
    )
    state_join = "LEFT JOIN working_memory_slot_state AS state ON state.slot_seq = slots.slot_seq" if has_slot_state else ""
    rows = connection.execute(
        f"""
        SELECT
            slots.slot_seq,
            slots.source,
            slots.active_event_id,
            slots.original_event_id,
            active_events.kind AS active_kind,
            active_events.token_count AS active_token_count,
            original_events.kind AS original_kind,
            original_events.token_count AS original_token_count,
            {state_columns}
        FROM slots
        JOIN events AS active_events ON active_events.event_id = slots.active_event_id
        JOIN events AS original_events ON original_events.event_id = slots.original_event_id
        {state_join}
        ORDER BY slots.slot_seq ASC
        """
    ).fetchall()
    return [{key: row[key] for key in row.keys()} for row in rows]


def _omitted_slot_sequences(messages, runtime_state=None):
    budget = wm_ingress.current_stm_budget(messages, runtime_state)
    status = stm_projection_status(
        messages or event_stream.load_messages(),
        budget=budget,
        wm_ingress_watermark=wm_ingress.effective_wm_ingress_watermark(runtime_state),
    )
    sequences = []
    for unit in status.get("omitted_units", []):
        start = int(unit.get("start_index", 0))
        end = int(unit.get("end_index", start))
        sequences.extend(range(start, end + 1))
    return status, set(sequences)


def _candidate_slots(messages, runtime_state=None, ensure_tables=True):
    status, omitted_sequences = _omitted_slot_sequences(messages, runtime_state)
    if not omitted_sequences:
        return status, []

    return status, _candidate_slots_for_sequences(omitted_sequences, ensure_tables=ensure_tables)


def _candidate_slots_for_sequences(sequences, ensure_tables=True):
    with event_stream.ensure_event_stream() as connection:
        if ensure_tables:
            _ensure_tables(connection)
        records = _slot_records(connection)

    sequence_set = {int(sequence) for sequence in sequences}
    candidates = []
    for record in records:
        if int(record["slot_seq"]) not in sequence_set:
            continue
        if record.get("wm_status"):
            continue
        if record.get("active_kind") not in {"raw", "ref"}:
            continue
        if int(record.get("active_token_count") or 0) <= SUM_TRIGGER_TOKENS:
            continue
        candidates.append(record)
    return candidates


def _preflight_candidate_slots_for_sequences(sequences, min_tokens=SUM_TRIGGER_TOKENS):
    with event_stream.ensure_event_stream() as connection:
        _ensure_tables(connection)
        records = _slot_records(connection)

    sequence_set = {int(sequence) for sequence in sequences}
    candidates = []
    for record in records:
        if int(record["slot_seq"]) not in sequence_set:
            continue
        if record.get("active_kind") not in {"raw", "ref"}:
            continue
        if record.get("wm_status") and record.get("wm_status") != "kept_raw":
            continue
        if int(record.get("active_token_count") or 0) <= int(min_tokens):
            continue
        candidates.append(record)
    return candidates


def _message_content_text(message):
    content = (message or {}).get("content", "")
    if isinstance(content, str):
        return content
    return json.dumps(content, ensure_ascii=False)


def _candidate_payload(slot, message, retry_reason="", additional_evidence=None):
    payload = {
        "task": (
            "Decide whether this single candidate message should be summarized for working memory, "
            "kept raw, or marked as needing more surrounding context."
        ),
        "actions": ["summarize", "keep_raw", "need_context"],
        "compression_rules": {
            "trigger_tokens": SUM_TRIGGER_TOKENS,
            "summary_must_be_below_trigger_tokens": True,
            "summary_must_be_below_half_original_tokens": True,
            "raw_message_traceable": True,
        },
        "retry_reason": retry_reason,
        "candidate": {
            "slot_seq": slot.get("slot_seq"),
            "source": slot.get("source", ""),
            "source_role": message.get("role", ""),
            "original_token_count": int(slot.get("active_token_count") or 0),
            "content": _message_content_text(message),
        },
        "output_schema": {
            "action": "summarize, keep_raw, or need_context",
            "summary": "required only when action is summarize",
            "reason": "brief decision reason",
        },
    }
    if additional_evidence is not None:
        payload["additional_evidence"] = additional_evidence
        payload["instruction"] = (
            "You previously needed more context. Use additional_evidence to make a final decision. "
            "Return summarize or keep_raw when possible. Return need_context only if the evidence is still insufficient."
        )
    return payload


def _build_maintainer_prompt(slot, message, retry_reason="", additional_evidence=None):
    return [
        {
            "role": "system",
            "content": load_prompt("working_memory"),
        },
        {
            "role": "user",
            "content": json.dumps(_candidate_payload(slot, message, retry_reason, additional_evidence), ensure_ascii=False),
        },
    ]


def _call_model_json(messages):
    data = call_chat_completion(
        messages,
        temperature=0,
        timeout_seconds=WORKING_MEMORY_TIMEOUT_SECONDS,
        purpose="working memory",
    )
    return data["choices"][0]["message"].get("content", "")


def _extract_json_object(text):
    text = str(text or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lstrip().startswith("json"):
            text = text.lstrip()[4:].strip()

    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise RuntimeError("Working Memory Maintainer did not return a JSON object.")
    return json.loads(text[start : end + 1])


def _normalized_decision(raw):
    action = str((raw or {}).get("action", "")).strip()
    if action not in {"summarize", "keep_raw", "need_context"}:
        action = "keep_raw"
    return {
        "action": action,
        "summary": str((raw or {}).get("summary", "")).strip(),
        "reason": str((raw or {}).get("reason", "")).strip(),
    }


def _summary_is_valid(summary, original_token_count):
    summary_token_count = count_text_tokens(summary)
    return (
        bool(summary.strip())
        and summary_token_count < SUM_TRIGGER_TOKENS
        and summary_token_count < (int(original_token_count or 0) * 0.5)
    )


def _decide_candidate_from_prompt(slot, message, additional_evidence=None, runtime_state=None):
    original_token_count = int(slot.get("active_token_count") or 0)
    retry_reason = ""
    last_decision = None
    for attempt in range(SUM_MAX_RETRIES + 1):
        prompt = _build_maintainer_prompt(slot, message, retry_reason=retry_reason, additional_evidence=additional_evidence)
        raw_text = _eval_decision_json(runtime_state)
        if raw_text is None:
            raw_text = _call_model_json(prompt)
        decision = _normalized_decision(_extract_json_object(raw_text))
        last_decision = decision
        if decision["action"] != "summarize":
            return decision

        summary = decision["summary"]
        if _summary_is_valid(summary, original_token_count):
            decision["summary_token_count"] = count_text_tokens(summary)
            return decision

        retry_reason = (
            "The previous summary was not sufficiently compressed. "
            f"summary_tokens={count_text_tokens(summary)}, original_tokens={original_token_count}. "
            "Return a shorter useful summary, or choose keep_raw."
        )

    return {
        "action": "keep_raw",
        "summary": "",
        "reason": f"compression_failed: {last_decision.get('reason', '') if last_decision else ''}".strip(),
    }


def _decide_candidate(slot, message, runtime_state=None):
    decision = _decide_candidate_from_prompt(slot, message, runtime_state=runtime_state)
    if decision.get("action") != "need_context":
        return decision

    try:
        evidence_result = evidence.expand_around(
            slot["slot_seq"],
            before_blocks=1,
            after_blocks=1,
            projection="active",
            runtime_state=runtime_state,
        )
        second_decision = _decide_candidate_from_prompt(
            slot,
            message,
            additional_evidence=evidence_result,
            runtime_state=runtime_state,
        )
    except Exception as error:
        return {
            "action": "keep_raw",
            "summary": "",
            "reason": f"need_context evidence retrieval failed; kept raw: {error}",
        }

    if second_decision.get("action") == "need_context":
        return {
            "action": "keep_raw",
            "summary": "",
            "reason": f"need_context unresolved after evidence retrieval: {second_decision.get('reason', '')}".strip(),
            "evidence_retrieved": True,
        }
    second_decision["evidence_retrieved"] = True
    return second_decision


def _eval_decision_json(runtime_state=None):
    if not isinstance(runtime_state, dict):
        return None
    decisions = runtime_state.get("eval_working_memory_decisions")
    if not isinstance(decisions, list):
        return None
    index = int(runtime_state.get("eval_working_memory_decision_index") or 0)
    if index >= len(decisions):
        raise RuntimeError("eval working memory decision hook exhausted")
    runtime_state["eval_working_memory_decision_index"] = index + 1
    return json.dumps(decisions[index], ensure_ascii=False)


def _process_candidate(slot, runtime_state=None):
    message = event_stream.load_message(slot["slot_seq"])
    decision = _decide_candidate(slot, message, runtime_state=runtime_state)
    action = decision["action"]
    if action == "need_context":
        action = "keep_raw"
        decision["reason"] = f"need_context treated as keep_raw in V1: {decision.get('reason', '')}".strip()

    sum_event = None
    if action == "summarize":
        summary_token_count = int(decision.get("summary_token_count") or count_text_tokens(decision["summary"]))
        metadata = {
            "source_role": message.get("role", ""),
            "summary": decision["summary"],
            "reason": decision.get("reason", ""),
            "original_token_count": int(slot.get("active_token_count") or 0),
            "summary_token_count": summary_token_count,
            "created_by": WORKING_MEMORY_AGENT,
            "slot_seq": int(slot["slot_seq"]),
            "original_event_id": slot.get("original_event_id", ""),
        }
        sum_event = event_stream.create_sum_event(slot["slot_seq"], metadata)
        status = "summarized"
        updated = True
        sum_event_id = sum_event["event_id"]
    else:
        summary_token_count = 0
        status = "kept_raw"
        updated = False
        sum_event_id = ""

    with event_stream.ensure_event_stream() as connection:
        _ensure_tables(connection)
        _record_slot_state(
            connection,
            slot["slot_seq"],
            status,
            action,
            decision.get("reason", ""),
            slot=slot,
            sum_event_id=sum_event_id,
            summary_token_count=summary_token_count,
        )
        connection.commit()

    return {
        "updated": updated,
        "reason": status,
        "decision_reason": decision.get("reason", ""),
        "action": action,
        "slot_seq": int(slot["slot_seq"]),
        "sum_event_id": sum_event_id,
    }


def _processed_counts(processed):
    return {
        "updated_count": sum(1 for item in processed if item.get("updated")),
        "summarized_count": sum(1 for item in processed if item.get("reason") == "summarized"),
        "kept_raw_count": sum(1 for item in processed if item.get("reason") == "kept_raw"),
    }


def _write_last_result(connection, result):
    _state_set(connection, "last_result", json.dumps(result, ensure_ascii=False, sort_keys=True))
    _state_set(connection, "last_run_at", _now())
    if "omitted_unit_count" in result:
        _state_set(connection, "last_omitted_unit_count", result["omitted_unit_count"])
    connection.commit()


def _last_result(connection):
    value = _state_get(connection, "last_result")
    if not value:
        return {}
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return {"error": "invalid last_result state"}


def _candidate_preview(candidates):
    return [
        {
            "slot_seq": int(record.get("slot_seq") or 0),
            "source": record.get("source") or "",
            "active_kind": record.get("active_kind") or "",
            "active_token_count": int(record.get("active_token_count") or 0),
            "original_token_count": int(record.get("original_token_count") or 0),
        }
        for record in candidates
    ]


def _truncate_text(value, limit=180):
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return f"{text[:limit]}..."


def _compression_ratio(row):
    original = int(row["original_token_count"] or 0)
    summary = int(row["summary_token_count"] or 0)
    if original <= 0 or summary <= 0:
        return ""
    return round(summary / original, 4)


def _recent_processed_rows(connection, limit=8):
    rows = connection.execute(
        """
        SELECT
            slot_seq,
            status,
            action,
            reason,
            original_token_count,
            summary_token_count,
            updated_at
        FROM working_memory_slot_state
        ORDER BY updated_at DESC, slot_seq DESC
        LIMIT ?
        """,
        (int(limit),),
    ).fetchall()
    return [
        {
            "slot_seq": int(row["slot_seq"]),
            "status": row["status"],
            "action": row["action"],
            "reason": _truncate_text(row["reason"]),
            "original_token_count": int(row["original_token_count"] or 0),
            "summary_token_count": int(row["summary_token_count"] or 0),
            "compression_ratio": _compression_ratio(row),
            "updated_at": row["updated_at"],
        }
        for row in rows
    ]



def working_memory_status(messages=None, runtime_state=None):
    with event_stream.ensure_event_stream() as connection:
        active_rows = connection.execute(
            """
            SELECT events.kind, COUNT(*) AS count
            FROM slots
            JOIN events ON events.event_id = slots.active_event_id
            GROUP BY events.kind
            """
        ).fetchall()
        active_counts = {row["kind"]: int(row["count"]) for row in active_rows}
        active_token_rows = connection.execute(
            """
            SELECT events.kind, COALESCE(SUM(events.token_count), 0) AS token_count
            FROM slots
            JOIN events ON events.event_id = slots.active_event_id
            GROUP BY events.kind
            """
        ).fetchall()
        active_token_totals = {row["kind"]: int(row["token_count"]) for row in active_token_rows}
        if _has_table(connection, "working_memory_slot_state"):
            slot_states = connection.execute(
                "SELECT status, COUNT(*) AS count FROM working_memory_slot_state GROUP BY status"
            ).fetchall()
            status_counts = {row["status"]: int(row["count"]) for row in slot_states}
            processed_checkpoint = connection.execute(
                "SELECT MAX(slot_seq) AS slot_seq FROM working_memory_slot_state"
            ).fetchone()["slot_seq"]
            recent_processed = _recent_processed_rows(connection)
        else:
            status_counts = {}
            processed_checkpoint = None
            recent_processed = []
        last_result = _last_result(connection) if _has_table(connection, "working_memory_state") else {}

    stm = None
    candidates = []
    error = None
    try:
        stm, candidates = _candidate_slots(messages or event_stream.load_messages(), runtime_state, ensure_tables=False)
    except Exception as exc:
        error = str(exc)

    return {
        "active_raw_count": active_counts.get("raw", 0),
        "active_ref_count": active_counts.get("ref", 0),
        "sum_event_count": active_counts.get("sum", 0),
        "active_raw_tokens": active_token_totals.get("raw", 0),
        "active_ref_tokens": active_token_totals.get("ref", 0),
        "active_sum_tokens": active_token_totals.get("sum", 0),
        "candidate_count": len(candidates),
        "candidate_tokens_total": sum(int(record.get("active_token_count") or 0) for record in candidates),
        "current_candidates": _candidate_preview(candidates),
        "processed_slot_checkpoint": processed_checkpoint,
        "status_counts": status_counts,
        "recent_processed": recent_processed,
        "last_result": last_result,
        "stm": stm or {},
        "error": error,
    }



def run_working_memory_maintenance(messages=None, force=False, runtime_state=None):
    messages = messages or event_stream.load_messages()
    try:
        stm, candidates = _candidate_slots(messages, runtime_state)
        omitted_count = int(stm.get("omitted_unit_count", 0))
        with event_stream.ensure_event_stream() as connection:
            _ensure_tables(connection)
            previous_omitted = int(_state_get(connection, "last_omitted_unit_count", "0") or 0)
            if not force and omitted_count <= previous_omitted and not candidates:
                result = {
                    "triggered": False,
                    "updated": False,
                    "reason": "no_new_omitted_unit",
                    "omitted_unit_count": omitted_count,
                    "candidate_count": len(candidates),
                    "error": None,
                }
                _write_last_result(connection, result)
                return result

            if omitted_count <= 0:
                result = {
                    "triggered": False,
                    "updated": False,
                    "reason": "no_omitted_units",
                    "omitted_unit_count": omitted_count,
                    "candidate_count": 0,
                    "error": None,
                }
                _write_last_result(connection, result)
                return result

            if not candidates:
                result = {
                    "triggered": True,
                    "updated": False,
                    "reason": "no_candidates",
                    "omitted_unit_count": omitted_count,
                    "candidate_count": 0,
                    "error": None,
                }
                _write_last_result(connection, result)
                return result

        processed = []
        for slot in sorted(candidates, key=lambda record: int(record["slot_seq"])):
            processed.append(_process_candidate(slot, runtime_state=runtime_state))

        counts = _processed_counts(processed)
        updated_count = counts["updated_count"]
        summarized_count = counts["summarized_count"]
        kept_raw_count = counts["kept_raw_count"]
        last = processed[-1] if processed else {}

        with event_stream.ensure_event_stream() as connection:
            _ensure_tables(connection)
            result = {
                "triggered": True,
                "updated": updated_count > 0,
                "reason": "processed_candidates",
                "decision_reason": last.get("decision_reason", ""),
                "action": last.get("action", ""),
                "slot_seq": last.get("slot_seq", ""),
                "sum_event_id": last.get("sum_event_id", ""),
                "omitted_unit_count": omitted_count,
                "candidate_count": len(candidates),
                "processed_count": len(processed),
                "updated_count": updated_count,
                "summarized_count": summarized_count,
                "kept_raw_count": kept_raw_count,
                "processed_slot_seqs": [item.get("slot_seq") for item in processed],
                "error": None,
            }
            if len(processed) == 1:
                result["reason"] = last.get("reason", "processed_candidates")
            _write_last_result(connection, result)
            return result
    except Exception as error:
        result = {
            "triggered": False,
            "updated": False,
            "reason": "error",
            "error": str(error),
        }
        with event_stream.ensure_event_stream() as connection:
            _ensure_tables(connection)
            _write_last_result(connection, result)
        return result


def compact_exact_slot(slot_seq, runtime_state=None):
    slot_seq = int(slot_seq)
    candidates = _preflight_candidate_slots_for_sequences([slot_seq], min_tokens=0)
    try:
        processed = _process_candidate(candidates[0], runtime_state=runtime_state) if candidates else None
    except Exception:
        processed = None
    if processed and processed.get("updated"):
        return {
            "updated": True,
            "action": "summarized",
            "slot_seq": slot_seq,
            "sum_event_id": processed.get("sum_event_id", ""),
        }
    slot = event_stream.load_slot(slot_seq)
    original = event_stream.load_message(slot_seq)
    content = original.get("content", "") if isinstance(original, dict) else ""
    if not isinstance(content, str):
        content = json.dumps(content, ensure_ascii=False)
    preview = content[:2000]
    metadata = {
        "status": "too_large",
        "source_slot_seq": slot_seq,
        "source_role": original.get("role", slot.get("source", "")) if isinstance(original, dict) else slot.get("source", ""),
        "original_event_id": slot.get("original_event_id", ""),
        "original_token_count": int(event_stream.load_event(slot["original_event_id"]).get("token_count") or 0),
        "preview": preview,
        "truncated": len(content) > len(preview),
        "retrieval_guidance": "Inspect the immutable original event through Event Stream evidence or a narrower source operation.",
        "created_by": WORKING_MEMORY_AGENT,
    }
    ref = event_stream.create_ref_event(slot_seq, metadata)
    return {
        "updated": True,
        "action": "bounded_ref",
        "slot_seq": slot_seq,
        "ref_event_id": ref.get("event_id", ""),
    }



def format_working_memory_status(status):
    status = status or {}
    last = status.get("last_result") or {}
    lines = [
        "Working Memory status:",
        f"active_raw_count: {status.get('active_raw_count', 0)}",
        f"active_ref_count: {status.get('active_ref_count', 0)}",
        f"sum_event_count: {status.get('sum_event_count', 0)}",
        f"active_raw_tokens: {status.get('active_raw_tokens', 0)}",
        f"active_ref_tokens: {status.get('active_ref_tokens', 0)}",
        f"active_sum_tokens: {status.get('active_sum_tokens', 0)}",
        f"candidate_count: {status.get('candidate_count', 0)}",
        f"candidate_tokens_total: {status.get('candidate_tokens_total', 0)}",
        f"processed_slot_checkpoint: {status.get('processed_slot_checkpoint')}",
        f"status_counts: {json.dumps(status.get('status_counts', {}), ensure_ascii=False, sort_keys=True)}",
        f"last_reason: {last.get('reason', '')}",
        f"last_processed_count: {last.get('processed_count', 0)}",
        f"last_summarized_count: {last.get('summarized_count', 0)}",
        f"last_kept_raw_count: {last.get('kept_raw_count', 0)}",
        f"last_decision_reason: {last.get('decision_reason', '')}",
        f"last_action: {last.get('action', '')}",
        f"last_slot_seq: {last.get('slot_seq', '')}",
        "current_candidates:",
    ]
    for candidate in status.get("current_candidates", [])[:12]:
        lines.append(
            "- "
            f"slot={candidate.get('slot_seq')} "
            f"source={candidate.get('source')} "
            f"kind={candidate.get('active_kind')} "
            f"tokens={candidate.get('active_token_count', 0)} "
            f"original_tokens={candidate.get('original_token_count', 0)}"
        )
    if not status.get("current_candidates"):
        lines.append("- none")
    lines.append("recent_processed:")
    for record in status.get("recent_processed", []):
        line = (
            "- "
            f"slot={record.get('slot_seq')} "
            f"status={record.get('status')} "
            f"action={record.get('action')} "
            f"original_tokens={record.get('original_token_count', 0)} "
            f"summary_tokens={record.get('summary_token_count', 0)}"
        )
        if record.get("compression_ratio") != "":
            line += f" compression_ratio={record.get('compression_ratio')}"
        if record.get("reason"):
            line += f" reason={record.get('reason')}"
        lines.append(line)
    if not status.get("recent_processed"):
        lines.append("- none")
    if status.get("error"):
        lines.append(f"error: {status['error']}")
    return "\n".join(lines)



def format_working_memory_run(result):
    result = result or {}
    lines = [
        "Working Memory run:",
        f"triggered: {str(bool(result.get('triggered', False))).lower()}",
        f"updated: {str(bool(result.get('updated', False))).lower()}",
        f"reason: {result.get('reason', '')}",
        f"action: {result.get('action', '')}",
        f"slot_seq: {result.get('slot_seq', '')}",
        f"sum_event_id: {result.get('sum_event_id', '')}",
        f"candidate_count: {result.get('candidate_count', 0)}",
        f"processed_count: {result.get('processed_count', 0)}",
        f"updated_count: {result.get('updated_count', 0)}",
        f"summarized_count: {result.get('summarized_count', 0)}",
        f"kept_raw_count: {result.get('kept_raw_count', 0)}",
        f"omitted_unit_count: {result.get('omitted_unit_count', 0)}",
        f"candidate_policy: {result.get('candidate_policy', '')}",
        f"preflight_target: {result.get('preflight_target', '')}",
        f"reprocessed_slot_seqs: {json.dumps(result.get('reprocessed_slot_seqs', []), ensure_ascii=False)}",
        f"fallback_candidate_count: {result.get('fallback_candidate_count', 0)}",
    ]
    if result.get("error"):
        lines.append(f"error: {result['error']}")
    return "\n".join(lines)
