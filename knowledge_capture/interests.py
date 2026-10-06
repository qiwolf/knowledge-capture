"""Infer interest candidates from recent, independently supplied source evidence.

Names are merged by Unicode NFKC, whitespace normalization and casefold only.
This module does not infer semantic synonyms. Feedback is keyed by returned ID;
only an explicit ``followed`` decision enables discovery. Missing/expired topics
are omitted: durable feedback (including names) belongs in the calling store.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import unicodedata


_FEEDBACK_STATES = {"watching", "followed", "paused", "closed"}


def _name(value: object) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(unicodedata.normalize("NFKC", value).split())


def _time(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    # A timestamp without its timezone cannot establish the collection window.
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


def infer_interests(
    records: list[dict], *, now: datetime | None = None, window_days: int = 30,
    threshold: int = 3, feedback: dict[str, str] | None = None,
) -> list[dict]:
    """Return deterministic, evidence-backed candidates without mutating inputs.

    Malformed records are ignored. Invalid configuration raises ValueError.
    At most one (latest) user record per source and one per content hash count.
    Evidence retains the chosen source version and its original line citations.
    """
    for label, value in (("window_days", window_days), ("threshold", threshold)):
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError(f"{label} must be a positive integer")
    if not isinstance(records, list):
        raise ValueError("records must be a list")
    if feedback is None:
        feedback = {}
    if not isinstance(feedback, dict) or any(
        not isinstance(key, str) or not isinstance(state, str) or state not in _FEEDBACK_STATES
        for key, state in feedback.items()
    ):
        raise ValueError("feedback must map interest IDs to watching, followed, paused or closed")
    if now is None:
        now = datetime.now(timezone.utc)
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be a timezone-aware datetime")
    now = now.astimezone(timezone.utc)
    cutoff = now - timedelta(days=window_days)

    valid = []
    for record in records:
        if not isinstance(record, dict) or record.get("origin") != "user":
            continue
        if any(not isinstance(record.get(key), str) or not record[key].strip()
               for key in ("source_id", "version_id", "content_hash", "title", "url")):
            continue
        collected = _time(record.get("collected_at"))
        if collected is None or not cutoff <= collected <= now:
            continue
        if not isinstance(record.get("topics"), list):
            continue
        valid.append((collected, record))
    # Stable tie-breaking prevents input ordering from changing representative data.
    valid.sort(key=lambda item: (
        item[0], item[1]["source_id"], item[1]["version_id"], item[1]["content_hash"]
    ), reverse=True)
    sources, hashes, groups = set(), set(), {}
    for _, record in valid:
        source_id, content_hash = record["source_id"], record["content_hash"]
        if source_id in sources:
            continue
        sources.add(source_id)
        if content_hash in hashes:
            continue
        hashes.add(content_hash)
        seen_topics = set()
        for topic in record["topics"]:
            if not isinstance(topic, dict):
                continue
            name = _name(topic.get("name"))
            normalized = name.casefold()
            if not name or normalized in seen_topics:
                continue
            reason, evidence = topic.get("reason"), topic.get("evidence")
            if not isinstance(reason, str) or not reason.strip() or not isinstance(evidence, list) or not evidence:
                continue
            if any(not isinstance(citation, dict)
                   or type(citation.get("start_line")) is not int
                   or type(citation.get("end_line")) is not int
                   or citation["start_line"] < 1
                   or citation["end_line"] < citation["start_line"]
                   or not isinstance(citation.get("quote"), str)
                   or not citation["quote"].strip()
                   for citation in evidence):
                continue
            seen_topics.add(normalized)
            group = groups.setdefault(normalized, {"name": name, "evidence": []})
            group["evidence"].append({
                **{key: record[key] for key in (
                    "source_id", "version_id", "title", "url", "collected_at")},
                "reason": reason, "evidence": deepcopy(evidence),
            })

    results = []
    for normalized, group in sorted(groups.items()):
        interest_id = "interest_" + hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:24]
        count = len(group["evidence"])
        state = feedback.get(interest_id, "candidate" if count >= threshold else "watching")
        results.append({"id": interest_id, "name": group["name"], "state": state,
                        "user_source_count": count, "evidence": group["evidence"],
                        "eligible_for_discovery": state == "followed"})
    return results
