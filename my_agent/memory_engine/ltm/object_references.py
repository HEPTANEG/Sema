"""Runtime-only short references for model-facing MemoryObject data."""


class ObjectReferenceError(ValueError):
    """Raised when a temporary MemoryObject reference is invalid."""


class ObjectReferenceMap:
    """Map short integer references to real MemoryObject IDs in one scope."""

    def __init__(self):
        self._ref_to_object = {}
        self._object_to_ref = {}
        self._next_ref = 1

    def register(self, object_id):
        if not isinstance(object_id, str) or not object_id.strip():
            raise ObjectReferenceError("object_id must be a non-empty string")
        if object_id in self._object_to_ref:
            return self._object_to_ref[object_id]
        reference = self._next_ref
        self._next_ref += 1
        self._ref_to_object[reference] = object_id
        self._object_to_ref[object_id] = reference
        return reference

    def register_many(self, object_ids):
        if not isinstance(object_ids, (list, tuple)):
            raise ObjectReferenceError("object_ids must be a sequence")
        return {self.register(object_id): object_id for object_id in object_ids}

    def ref_for(self, object_id):
        try:
            return self._object_to_ref[object_id]
        except (KeyError, TypeError) as error:
            raise ObjectReferenceError("object_id is not registered") from error

    def resolve(self, reference):
        if isinstance(reference, bool) or not isinstance(reference, int) or reference <= 0:
            raise ObjectReferenceError("reference must be a positive integer")
        try:
            return self._ref_to_object[reference]
        except KeyError as error:
            raise ObjectReferenceError("reference is unknown") from error

    def resolve_many(self, references):
        if not isinstance(references, list):
            raise ObjectReferenceError("references must be an array")
        seen = []
        for reference in references:
            if any(reference == previous for previous in seen):
                raise ObjectReferenceError("references must not contain duplicates")
            seen.append(reference)
        return [self.resolve(reference) for reference in references]

    def __len__(self):
        return len(self._ref_to_object)
