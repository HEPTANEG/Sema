"""Project-local Vector Index backed by usearch HNSW."""

import json
import math
import os
import threading
from collections.abc import Mapping
from uuid import uuid4

from .. import event_stream
from .embedding_adapter import EmbeddingServiceError, embed_documents
from .embedding_projection import EMBEDDING_PROJECTION_VERSION, build_embedding_projection
from .ltm_store import ltm_dir, list_objects, get_object
from .meta_memory import system_owned_tags


MODEL_NAME = "BAAI/bge-m3"
DIMENSION = 1024
METRIC = "cos"
INDEX_FORMAT_VERSION = 1
VECTOR_DIR_NAME = "vector"
INDEX_FILE_NAME = "index.usearch"
METADATA_FILE_NAME = "metadata.json"


class VectorIndexError(RuntimeError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


class VectorIndexUnavailable(VectorIndexError):
    def __init__(self, message="Vector Index is unavailable."):
        super().__init__("vector_index_unavailable", message)


def _vector_dir(root=None):
    return ltm_dir(root) / VECTOR_DIR_NAME


def _index_path(root=None):
    return _vector_dir(root) / INDEX_FILE_NAME


def _metadata_path(root=None):
    return _vector_dir(root) / METADATA_FILE_NAME


def _expected_metadata():
    return {
        "embedding_model": MODEL_NAME,
        "projection_version": EMBEDDING_PROJECTION_VERSION,
        "dimension": DIMENSION,
        "metric": METRIC,
        "index_format_version": INDEX_FORMAT_VERSION,
    }


def _normalize_vector(vector):
    try:
        import numpy as np
    except ImportError as error:
        raise VectorIndexUnavailable("numpy is required by the Vector Index.") from error
    try:
        values = np.asarray(vector, dtype=np.float32)
    except (TypeError, ValueError) as error:
        raise VectorIndexError("invalid_vector", "vector must contain numeric values.") from error
    if values.ndim != 1 or len(values) != DIMENSION:
        raise VectorIndexError("invalid_dimension", f"vector dimension must be {DIMENSION}.")
    if not bool(np.isfinite(values).all()):
        raise VectorIndexError("invalid_vector", "vector must contain finite values.")
    norm = float(np.linalg.norm(values))
    if not math.isclose(norm, 1.0, rel_tol=1e-3, abs_tol=1e-3):
        raise VectorIndexError("invalid_vector", "vector must be L2-normalized.")
    return values


def _load_usearch():
    try:
        from usearch.index import Index
    except ImportError as error:
        raise VectorIndexUnavailable("usearch is not installed.") from error
    return Index


def _metadata_with_mapping(object_to_label, next_label):
    return {
        **_expected_metadata(),
        "next_label": int(next_label),
        "object_to_label": {key: int(value) for key, value in object_to_label.items()},
        "label_to_object": {str(value): key for key, value in object_to_label.items()},
    }


def _validate_metadata(metadata, index):
    if not isinstance(metadata, dict):
        raise VectorIndexError("invalid_metadata", "Vector Index metadata must be an object.")
    expected = _expected_metadata()
    for key, value in expected.items():
        if metadata.get(key) != value:
            raise VectorIndexError("index_incompatible", f"Vector Index metadata mismatch: {key}.")
    object_to_label = metadata.get("object_to_label")
    label_to_object = metadata.get("label_to_object")
    next_label = metadata.get("next_label")
    if not isinstance(object_to_label, dict) or not isinstance(label_to_object, dict):
        raise VectorIndexError("invalid_metadata", "Vector Index label mappings are required.")
    if not isinstance(next_label, int) or next_label < 0:
        raise VectorIndexError("invalid_metadata", "Vector Index next_label is invalid.")
    normalized_labels = {str(int(value)): key for key, value in object_to_label.items()}
    if normalized_labels != label_to_object:
        raise VectorIndexError("invalid_metadata", "Vector Index label mappings disagree.")
    if len(index) != len(object_to_label):
        raise VectorIndexError("invalid_metadata", "Vector Index object count disagrees with metadata.")
    try:
        index_labels = {str(int(value)) for value in index.keys}
    except Exception as error:
        raise VectorIndexError("invalid_metadata", "Vector Index labels could not be read.") from error
    if index_labels != set(label_to_object):
        raise VectorIndexError("invalid_metadata", "Vector Index labels disagree with metadata.")


class VectorIndexAdapter:
    def __init__(self, root=None):
        self.root = root
        self._lock = threading.RLock()
        self._index = None
        self._metadata = None
        self._load_attempted = False
        self._status = "not_initialized"
        self._error = ""

    @property
    def index_path(self):
        return _index_path(self.root)

    @property
    def metadata_path(self):
        return _metadata_path(self.root)

    def _mark_dirty(self, error):
        self._status = "dirty"
        self._error = str(error)

    def _load_locked(self):
        if self._load_attempted:
            return
        self._load_attempted = True
        index_exists = self.index_path.exists()
        metadata_exists = self.metadata_path.exists()
        if not index_exists and not metadata_exists:
            return
        if index_exists != metadata_exists:
            self._status = "unavailable"
            self._error = "Vector Index files are incomplete."
            return
        try:
            Index = _load_usearch()
            metadata = json.loads(self.metadata_path.read_text(encoding="utf-8"))
            index = Index.restore(str(self.index_path))
            if index is None:
                raise VectorIndexError("invalid_index", "Vector Index could not be restored.")
            _validate_metadata(metadata, index)
            self._index = index
            self._metadata = metadata
            self._status = "ready"
            self._error = ""
        except VectorIndexError as error:
            self._status = "unavailable"
            self._error = str(error)
        except Exception as error:
            self._status = "unavailable"
            self._error = str(error)

    def _ensure_ready_locked(self):
        self._load_locked()
        if self._status == "dirty":
            raise VectorIndexUnavailable(self._error or "Vector Index is dirty.")
        if self._status == "unavailable":
            raise VectorIndexUnavailable(self._error or "Vector Index is unavailable.")

    def _new_index(self):
        Index = _load_usearch()
        return Index(ndim=DIMENSION, metric=METRIC, dtype="f32")

    def _persist_locked(self, index, metadata):
        directory = self.index_path.parent
        directory.mkdir(parents=True, exist_ok=True)
        suffix = f".{uuid4().hex}.tmp"
        temporary_index = directory / f"{INDEX_FILE_NAME}{suffix}"
        temporary_metadata = directory / f"{METADATA_FILE_NAME}{suffix}"
        try:
            index.save(str(temporary_index))
            temporary_metadata.write_text(
                json.dumps(metadata, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
                encoding="utf-8",
            )
            os.replace(temporary_index, self.index_path)
            os.replace(temporary_metadata, self.metadata_path)
        except Exception as error:
            for path in (temporary_index, temporary_metadata):
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass
            self._mark_dirty(error)
            raise VectorIndexUnavailable(str(error)) from error

    def _check_index_metadata(self, index_metadata):
        if not isinstance(index_metadata, Mapping):
            raise VectorIndexError("invalid_metadata", "index_metadata must be an object.")
        for key, expected in _expected_metadata().items():
            if index_metadata.get(key) != expected:
                raise VectorIndexError("index_incompatible", f"index_metadata mismatch: {key}.")

    def upsert(self, object_id, vector, index_metadata):
        if not isinstance(object_id, str) or not object_id.startswith("mem_"):
            raise VectorIndexError("invalid_object_id", "object_id must start with mem_.")
        values = _normalize_vector(vector)
        self._check_index_metadata(index_metadata)
        with self._lock:
            self._ensure_ready_locked()
            if self._index is None:
                self._index = self._new_index()
                self._metadata = _metadata_with_mapping({}, 0)
                self._status = "ready"
            object_to_label = dict(self._metadata["object_to_label"])
            label = object_to_label.get(object_id)
            if label is None:
                label = int(self._metadata["next_label"])
                self._metadata["next_label"] = label + 1
                object_to_label[object_id] = label
            self._index.add(label, values)
            self._metadata = _metadata_with_mapping(object_to_label, self._metadata["next_label"])
            self._persist_locked(self._index, self._metadata)
            return {"status": "ready", "object_id": object_id, "label": label}

    def search(self, query_vector, limit):
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
            raise VectorIndexError("invalid_limit", "limit must be a positive integer.")
        values = _normalize_vector(query_vector)
        with self._lock:
            self._load_locked()
            if self._status == "not_initialized":
                return []
            self._ensure_ready_locked()
            if self._index is None or len(self._index) == 0:
                return []
            matches = self._index.search(values, limit)
            object_to_label = self._metadata["object_to_label"]
            label_to_object = {int(label): object_id for object_id, label in object_to_label.items()}
            results = []
            for label, distance in zip(list(matches.keys), list(matches.distances)):
                object_id = label_to_object.get(int(label))
                if object_id is None:
                    self._mark_dirty("Vector Index returned an unknown label.")
                    raise VectorIndexUnavailable(self._error)
                results.append({"object_id": object_id, "similarity": 1.0 - float(distance)})
            return sorted(results, key=lambda item: (-item["similarity"], item["object_id"]))

    def remove(self, object_id):
        with self._lock:
            self._load_locked()
            if self._status == "not_initialized":
                return {"status": "not_initialized", "removed": False}
            self._ensure_ready_locked()
            label = self._metadata["object_to_label"].get(object_id)
            if label is None:
                return {"status": "ready", "removed": False}
            self._index.remove(int(label))
            object_to_label = dict(self._metadata["object_to_label"])
            object_to_label.pop(object_id, None)
            self._metadata = _metadata_with_mapping(object_to_label, self._metadata["next_label"])
            self._persist_locked(self._index, self._metadata)
            return {"status": "ready", "removed": True, "object_id": object_id}

    def remove_many(self, object_ids):
        """Idempotent index-only cleanup with a durable staged removal journal."""
        import shutil

        with self._lock:
            pending = self.index_path.parent / "pending-removal.json"

            def install(staged):


                copies = {}
                for key, path in staged.items():
                    copy_path = str(path) + ".install"
                    shutil.copyfile(path, copy_path)
                    copies[key] = copy_path
                self.install_staged(copies)
                pending.unlink(missing_ok=True)
                self.discard_staged(staged)

            if pending.exists():
                install(json.loads(pending.read_text(encoding="utf-8")))
            self._load_locked()
            if self._status == "not_initialized":
                return
            self._ensure_ready_locked()
            ids = set(object_ids) & set(self._metadata["object_to_label"])
            if not ids:
                return
            try:
                staged = self.stage_upserts([], remove_ids=ids)
            except Exception:
                self.close()
                raise
            temporary = pending.with_suffix(".tmp")
            temporary.write_text(json.dumps(staged), encoding="utf-8")
            os.replace(temporary, pending)
            install(staged)

    def rebuild(self, items):
        normalized = []
        seen = set()
        for item in items:
            if not isinstance(item, Mapping):
                raise VectorIndexError("invalid_rebuild_item", "rebuild items must be objects.")
            object_id = item.get("object_id")
            if not isinstance(object_id, str) or not object_id.startswith("mem_") or object_id in seen:
                raise VectorIndexError("invalid_rebuild_item", "rebuild object IDs must be unique mem_ IDs.")
            seen.add(object_id)
            normalized.append((object_id, _normalize_vector(item.get("vector"))))
        normalized.sort(key=lambda item: item[0])
        with self._lock:
            try:
                index = self._new_index()
                object_to_label = {}
                for label, (object_id, values) in enumerate(normalized):
                    index.add(label, values)
                    object_to_label[object_id] = label
                metadata = _metadata_with_mapping(object_to_label, len(normalized))
                self._persist_locked(index, metadata)
                self._index = index
                self._metadata = metadata
                self._load_attempted = True
                self._status = "ready"
                self._error = ""
                return {"status": "ready", "object_count": len(normalized)}
            except VectorIndexError:
                raise
            except Exception as error:
                self._mark_dirty(error)
                raise VectorIndexUnavailable(str(error)) from error

    def stage_upserts(self, items, *, remove_ids=()):
        """Write a complete replacement index to temporary files without installing it."""
        normalized = []
        for item in items:
            normalized.append((item["object_id"], _normalize_vector(item["vector"])))
        with self._lock:
            self._ensure_ready_locked()
            try:
                Index = _load_usearch()
                if self._index is None:
                    index = self._new_index()
                    metadata = _metadata_with_mapping({}, 0)
                else:
                    seed = self.index_path.parent / f"{INDEX_FILE_NAME}.{uuid4().hex}.seed"
                    self._index.save(str(seed))
                    index = Index.restore(str(seed))
                    seed.unlink(missing_ok=True)
                    metadata = dict(self._metadata)
                    metadata["object_to_label"] = dict(metadata["object_to_label"])
                object_to_label = dict(metadata["object_to_label"])
                next_label = int(metadata["next_label"])
                for object_id in remove_ids:
                    label = object_to_label.pop(object_id, None)
                    if label is not None:
                        index.remove(int(label))
                for object_id, vector in normalized:
                    label = object_to_label.get(object_id)
                    if label is None:
                        label = next_label
                        next_label += 1
                        object_to_label[object_id] = label
                    else:
                        index.remove(int(label))
                    index.add(int(label), vector)
                metadata = _metadata_with_mapping(object_to_label, next_label)
                suffix = f".stage.{uuid4().hex}"
                staged_index = self.index_path.parent / f"{INDEX_FILE_NAME}{suffix}"
                staged_metadata = self.index_path.parent / f"{METADATA_FILE_NAME}{suffix}"
                staged_index.parent.mkdir(parents=True, exist_ok=True)
                index.save(str(staged_index))
                staged_metadata.write_text(json.dumps(metadata, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
                return {"index_path": str(staged_index), "metadata_path": str(staged_metadata)}
            except Exception as error:
                self._mark_dirty(error)
                raise VectorIndexUnavailable(str(error)) from error

    def install_staged(self, staged):
        index_path = os.fspath(staged["index_path"])
        metadata_path = os.fspath(staged["metadata_path"])
        with self._lock:
            try:
                os.replace(index_path, self.index_path)
                os.replace(metadata_path, self.metadata_path)
                self._index = None
                self._metadata = None
                self._load_attempted = False
                self._status = "not_initialized"
                self._error = ""
                self._load_locked()
                self._ensure_ready_locked()
            except Exception as error:
                self._mark_dirty(error)
                raise VectorIndexUnavailable(str(error)) from error

    def discard_staged(self, staged):
        for path in (staged.get("index_path"), staged.get("metadata_path")):
            if path:
                try:
                    os.unlink(path)
                except FileNotFoundError:
                    pass

    def status(self):
        with self._lock:
            self._load_locked()
            metadata = self._metadata or {}
            return {
                "initialized": self._status == "ready",
                "status": self._status,
                "dirty": self._status == "dirty",
                "object_count": len(metadata.get("object_to_label") or {}),
                "embedding_model": metadata.get("embedding_model", MODEL_NAME),
                "projection_version": metadata.get("projection_version", EMBEDDING_PROJECTION_VERSION),
                "dimension": metadata.get("dimension", DIMENSION),
                "metric": metadata.get("metric", METRIC),
                "error": self._error,
            }

    def object_ids(self):
        """Return the canonical object ids represented by the persisted index."""
        with self._lock:
            self._load_locked()
            return sorted((self._metadata or {}).get("object_to_label", {}).keys())

    def close(self):
        with self._lock:
            self._index = None
            self._metadata = None
            self._load_attempted = False
            self._status = "not_initialized"
            self._error = ""


_adapters = {}
_adapters_lock = threading.Lock()


def _adapter_key(root=None):
    return str(event_stream.memory_root(root).resolve())


def get_vector_index(root=None):
    key = _adapter_key(root)
    with _adapters_lock:
        adapter = _adapters.get(key)
        if adapter is None:
            adapter = VectorIndexAdapter(root)
            _adapters[key] = adapter
        return adapter


def close_vector_indexes():
    with _adapters_lock:
        adapters = list(_adapters.values())
        _adapters.clear()
    for adapter in adapters:
        adapter.close()


def vector_index_snapshot(root=None):
    return get_vector_index(root).status()


def _active_memory_objects(root=None):
    system_ids = {tag["object_id"] for tag in system_owned_tags(root)}
    objects = []
    for object_type in ("assertion", "episode", "tag"):
        objects.extend(list_objects(object_type, include_payload=True, root=root))
    return [obj for obj in objects if obj["object_id"] not in system_ids]


def rebuild_from_store(root=None):
    objects = _active_memory_objects(root)
    projections = [build_embedding_projection(obj) for obj in objects]
    batches = embed_documents(projections) if projections else None
    items = [
        {"object_id": obj["object_id"], "vector": vector}
        for obj, vector in zip(objects, batches.vectors if batches else ())
    ]
    return get_vector_index(root).rebuild(items)


def sync_committed_objects(object_ids, root=None, runtime_state=None):
    object_ids = list(dict.fromkeys(object_ids or []))
    if not object_ids:
        return {"status": "not_applicable", "object_count": 0, "error": ""}
    try:
        system_ids = {tag["object_id"] for tag in system_owned_tags(root)}
        objects = [
            obj
            for object_id in object_ids
            if object_id not in system_ids
            for obj in [get_object(object_id, include_payload=True, root=root)]
            if obj is not None
        ]
        if not objects:
            return {"status": "not_applicable", "object_count": 0, "error": ""}
        projections = [build_embedding_projection(obj) for obj in objects]
        batch = embed_documents(projections)
        adapter = get_vector_index(root)
        index_metadata = _expected_metadata()
        for object_id, vector in zip((obj["object_id"] for obj in objects), batch.vectors):
            adapter.upsert(object_id, vector, index_metadata)
        result = {"status": "synced", "object_count": len(objects), "error": ""}
    except (EmbeddingServiceError, VectorIndexError, OSError, ValueError, RuntimeError) as error:
        try:
            get_vector_index(root)._mark_dirty(error)
        except Exception:
            pass
        result = {"status": "unavailable", "object_count": 0, "error": str(error)}
    if isinstance(runtime_state, dict):
        runtime_state["memory_engine_vector_index_last_sync"] = dict(result)
    return result


def stage_object_upserts(objects, root=None):
    """Embed canonical object projections and stage, but do not install, vector changes."""
    objects = list(objects or [])
    if not objects:
        return None
    projections = [build_embedding_projection(obj) for obj in objects]
    batch = embed_documents(projections)
    items = [
        {"object_id": obj["object_id"], "vector": vector}
        for obj, vector in zip(objects, batch.vectors)
    ]
    return get_vector_index(root).stage_upserts(items)


def install_staged_vector(staged, root=None):
    if staged:
        get_vector_index(root).install_staged(staged)


def discard_staged_vector(staged, root=None):
    if staged:
        get_vector_index(root).discard_staged(staged)
