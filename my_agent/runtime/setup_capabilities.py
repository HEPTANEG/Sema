"""Backend-local readiness and project-local explicit LTM skip accounting."""

import json
import threading

_enabled = True
_accepted = False
_managed = False
_active_fingerprints = {}
_active_values = {}
LTM_TOOLS = {"get_object", "retrieve_memory", "memorize"}
_threads = []
_thread_lock = threading.Lock()


def register_thread(thread):
    with _thread_lock:
        _threads[:] = [item for item in _threads if item.is_alive() or item.ident is None]
        _threads.append(thread)
    return thread


def background_busy():
    with _thread_lock:
        return any(item.is_alive() or item.ident is None for item in _threads)


def ltm_enabled():
    return _enabled


def initialize():
    global _enabled, _accepted, _managed, _active_fingerprints, _active_values
    from runtime import setup_config
    _managed = True
    _active_fingerprints = {name: setup_config.fingerprint(name) for name in ("embedding", "reranker")}
    _active_values = {name: setup_config.effective(name)[0] for name in _active_fingerprints}
    snapshot = setup_config.status()
    _enabled = all(snapshot["components"][name]["status"] == "Ready" for name in _active_fingerprints)
    _enabled = _enabled and all(snapshot["infrastructure"][name] == "Ready" for name in ("tiktoken", "usearch", "SQLite / FTS5", "sentence_transformers", "torch"))
    _accepted = False
    return snapshot


def service_value(component, key):
    from runtime.setup_config import value
    return _active_values[component][key] if _managed and component in _active_values else value(component, key)


def snapshot():
    from runtime import setup_config
    result = setup_config.status(_active_fingerprints if _managed else None)
    result["ltm_enabled"] = _enabled
    result["limited_mode_accepted"] = _accepted
    return result


def accept_limited():
    global _accepted
    from runtime import setup_config
    if setup_config.status()["components"]["llm"]["status"] != "Ready":
        raise RuntimeError("Configure and test the LLM before continuing.")
    _accepted = True
    if not _enabled:
        from memory_engine import event_stream
        with event_stream.ensure_event_stream() as connection:
            _ensure_pause_table(connection)
            newest = connection.execute("SELECT MAX(slot_seq) FROM slots").fetchone()[0]
            connection.execute("INSERT OR IGNORE INTO setup_ltm_pause VALUES (1, ?)", (int(newest) + 1 if newest is not None else 0,))
            connection.commit()
    return snapshot()


def require_chat():
    if not _managed:
        return
    from runtime import setup_config
    if setup_config.status()["components"]["llm"]["status"] != "Ready":
        raise RuntimeError("Configure and test the LLM in Settings before chatting.")
    if not _enabled and not _accepted:
        raise RuntimeError("Complete setup or explicitly choose Continue without LTM.")


def require_ltm():
    if not _enabled:
        raise RuntimeError("LTM is unavailable. Configure Embedding and Reranker in Settings, test them, then restart Backend.")


def skipped_slots(connection):
    exists = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='setup_ltm_skips'").fetchone()
    if not exists:
        return set()
    return {int(row[0]) for row in connection.execute("SELECT slot_seq FROM setup_ltm_skips")}


def observe_window(messages, runtime_state=None):
    if not _managed or (_enabled and not _has_pending_window()):
        return
    if not _enabled and not _accepted:
        return
    from memory_engine import event_stream
    from memory_engine.ltm import formation
    try:
        slots, _ = formation._buffer_slots(messages, runtime_state)
    except (RuntimeError, ValueError):

        return
    if not slots:
        return
    with event_stream.ensure_event_stream() as connection:
        connection.execute("CREATE TABLE IF NOT EXISTS setup_ltm_skips (slot_seq INTEGER PRIMARY KEY, reason TEXT NOT NULL)")
        connection.execute("CREATE TABLE IF NOT EXISTS setup_ltm_window (id INTEGER PRIMARY KEY, slots TEXT NOT NULL)")
        row = connection.execute("SELECT slots FROM setup_ltm_window WHERE id=1").fetchone()
        previous = set(json.loads(row[0])) if row else set()
        _ensure_pause_table(connection)
        pause = connection.execute("SELECT first_slot FROM setup_ltm_pause WHERE id=1").fetchone()
        if pause:
            previous.update(int(row[0]) for row in connection.execute(
                "SELECT slot_seq FROM slots WHERE slot_seq >= ? AND source != 'system'", (pause[0],)
            ))


        expired = {slot for slot in previous - slots if slot < min(slots)}
        completed = set()
        if connection.execute("SELECT 1 FROM sqlite_master WHERE name='formation_work_items'").fetchone():
            for item in connection.execute("SELECT pending_slot_ranges FROM formation_work_items WHERE status='completed'"):
                completed.update(formation._slots_from_ranges(json.loads(item[0])))
        expired -= completed
        connection.executemany("INSERT OR IGNORE INTO setup_ltm_skips VALUES (?, 'unconfigured_ltm')", [(slot,) for slot in sorted(expired)])
        retained = ((previous - expired) | (set() if _enabled else slots)) - completed
        if _enabled:
            connection.execute("DELETE FROM setup_ltm_window")
            connection.execute("DELETE FROM setup_ltm_pause")
        else:
            connection.execute("INSERT OR REPLACE INTO setup_ltm_window VALUES (1, ?)", (json.dumps(sorted(retained)),))
        connection.commit()


def _has_pending_window():
    from memory_engine import event_stream
    import sqlite3
    path = event_stream.event_stream_db_path()
    if not path.exists():
        return False
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as connection:
        for name in ('setup_ltm_window', 'setup_ltm_pause'):
            exists = connection.execute("SELECT 1 FROM sqlite_master WHERE name=?", (name,)).fetchone()
            if exists and connection.execute(f"SELECT 1 FROM {name} WHERE id=1").fetchone():
                return True
    return False


def _ensure_pause_table(connection):
    connection.execute("CREATE TABLE IF NOT EXISTS setup_ltm_pause (id INTEGER PRIMARY KEY, first_slot INTEGER NOT NULL)")


def restoration_pending():
    return _managed and _enabled and _has_pending_window()


def excluded_slots(root=None):
    from memory_engine import event_stream
    import sqlite3
    path = event_stream.event_stream_db_path(root)
    if not path.exists():
        return set()
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as connection:
        return skipped_slots(connection)
