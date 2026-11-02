"""Invocation-local, deterministic Formation retrieval reminders."""

import hashlib
import json


def _fingerprint(value):
    return hashlib.sha256(json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")).digest()


def _leaves(value, path=()):
    if value is None or value == "" or value == {} or value == []:
        return
    if isinstance(value, dict):
        for key, child in value.items():
            yield from _leaves(child, (*path, key))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _leaves(child, (*path, index))
    else:
        yield path, value


class FormationRetrievalProgress:
    def __init__(self, limit):
        self.limit = limit
        self.objects = set()
        self.fields = set()
        self.links = set()
        self.stalled_rounds = 0
        self.stalled_errors = 0
        self.warned = False
        self.budget_tier = -1

    def observe_round(self, results, used, model_round):
        counts = {"new_objects": 0, "new_fields": 0, "new_relations": 0}
        errors = 0
        for result in results:
            if result.get("status") not in {"ok", "partial"}:
                errors += 1
                continue
            for item in result.get("items") or []:
                object_id = item.get("object_id")
                if not object_id:
                    continue
                if object_id not in self.objects:
                    self.objects.add(object_id)
                    counts["new_objects"] += 1
                payload = item.get("payload") or {}
                content = {key: item.get(key) for key in ("object_type", "title", "summary")}
                content["payload"] = {key: payload.get(key) for key in ("body", "structured_data")}
                for path, value in _leaves(content):
                    fingerprint = _fingerprint([object_id, path, value])
                    if fingerprint not in self.fields:
                        self.fields.add(fingerprint)
                        counts["new_fields"] += 1
                for link in item.get("path") or []:
                    fingerprint = _fingerprint([link.get(key) for key in (
                        "link_id", "relation_type", "source_object_id", "target_object_id"
                    )])
                    if fingerprint not in self.links:
                        self.links.add(fingerprint)
                        counts["new_relations"] += 1
        if any(counts.values()):
            self.stalled_rounds = self.stalled_errors = 0
            self.warned = False
        else:
            self.stalled_rounds += 1
            self.stalled_errors += errors
        summary = {
            **counts, "model_round": model_round, "error_call_count": errors,
            "consecutive_no_new_material_rounds": self.stalled_rounds,
            "no_new_material_error_call_count": self.stalled_errors,
            "used": used, "limit": self.limit, "remaining": max(0, self.limit - used),
        }
        notices = []
        if self.stalled_rounds >= 3 and not self.warned:
            self.warned = True
            message = (
                "Runtime detected that the last 3 retrieval rounds provided no new memory "
                "material. Re-examine your retrieval strategy and assess what information "
                "is still needed for the formation decision."
            )
            if self.stalled_errors:
                message += (
                    f" This no-new-material period included {self.stalled_errors} failed tool calls. "
                    "Review the existing error feedback and correct those calls; tool failures "
                    "are not successful empty searches."
                )
            notices.append(({**summary, "kind": "no_new_material", "threshold": 3}, message))

        thresholds = [(self.limit * numerator + denominator - 1) // denominator
                      for numerator, denominator in ((1, 2), (3, 4), (7, 8))]
        reached = [index for index, threshold in enumerate(thresholds) if used >= threshold]
        if reached and max(reached) > self.budget_tier:
            self.budget_tier = max(reached)
            guidance = (
                "At least half of the retrieval budget has been used.",
                "Prioritize the necessary memory material that is still missing.",
                "Prioritize the remaining necessary checks and work toward a final formation decision.",
            )[self.budget_tier]
            message = (
                f"You have used {used} of {self.limit} retrieval tool calls; {summary['remaining']} remain. "
                f"{guidance} Budget pressure does not establish that the evidence is sufficient."
            )
            notices.append(({**summary, "kind": "retrieval_budget", "threshold": thresholds[self.budget_tier],
                             "budget_fraction": (0.5, 0.75, 0.875)[self.budget_tier]}, message))
        return summary, notices
