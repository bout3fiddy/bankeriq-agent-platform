"""Source-checked retirement of explicit corrections and resolved questions."""

import json
import re
from dataclasses import dataclass

from agent_harness.memory.semantic import MemoryBlocked, MemoryFact, select_facts


@dataclass(frozen=True)
class MemorySelection:
    facts: tuple[MemoryFact, ...]
    retirements: tuple[dict[str, object], ...] = ()


def select_memory(
    answer: str, candidates: tuple[MemoryFact, ...], sources: tuple[MemoryFact, ...]
) -> MemorySelection:
    """Resolve candidate IDs and validate replacements before releasing protected facts.

    For example, an earlier decision 'Budget: EUR 1200.' can be retired by a later
    user excerpt 'Budget correction: CHF 1500 replaces EUR 1200.' Both source cursors
    and excerpts enter the durable retirement record. A missing candidate, reversed
    chronology, unrelated subject, or a completion claim cannot retire the decision.
    Ordinary omission never retires anything. This deliberately conservative lexical
    gate leaves ambiguous natural-language updates available together for retrieval.
    """

    if len(answer.encode()) > 32768:
        raise MemoryBlocked("summary_output_limit")

    # Preserve exact explicitly headed user goals/constraints/decisions even
    # when omitted by the selector. Candidate splitting never changes their
    # source or authority; a valid retirement can release that precise excerpt.
    protected = sources + tuple(
        fact for fact in candidates if fact.label != "fact" and fact not in sources
    )

    try:
        value = json.loads(answer)

        if not isinstance(value, dict) or set(value) not in (
            {"entries"},
            {"entries", "retirements"},
        ):
            raise ValueError

        entries = value["entries"]
        changes = value.get("retirements", [])

        if not isinstance(entries, list) or not isinstance(changes, list):
            raise ValueError

        if len(entries) > 64 or len(changes) > 32:
            raise ValueError

        def candidate(index: object) -> MemoryFact:
            if type(index) is not int or not 0 <= index < len(candidates):
                raise ValueError

            return candidates[index]

        selected = []
        selected_ids = set()

        for entry in entries:
            if set(entry) != {"candidate", "label"}:
                raise ValueError

            fact = candidate(entry["candidate"])
            selected_ids.add(entry["candidate"])
            label = entry["label"]

            if label == "completed_action" and fact.kind != "tool_completed":
                label = "fact"

            selected.append({"source": fact.source, "quote": fact.text, "label": label})

        retired: set[MemoryFact] = set()
        records = []

        for change in changes:
            if set(change) != {"previous", "replacement", "relation"}:
                raise ValueError

            old = candidate(change["previous"])
            new = candidate(change["replacement"])
            relation = change["relation"]

            if (
                old not in protected
                or change["replacement"] not in selected_ids
                or (old.turn, old.source) >= (new.turn, new.source)
                or old.kind not in ("user", "assistant")
                or new.kind not in ("user", "assistant")
                or not _explicit(old, new, relation)
            ):

                # Unsupported semantics leave the protected fact active. A bad
                # suggestion cannot poison an otherwise source-valid summary or
                # turn a repeated question into an answer. Bad IDs/shapes fail.
                continue

            # Candidate splitting must never retire unselected text from the
            # same source. Only a whole earlier protected excerpt is released.
            retired.update(fact for fact in protected if fact == old)

            # Validated replacement takes precedence even if the selector also
            # listed the old fact. Preserve the protection of a current decision
            # rather than letting a generic replacement label lose it next time.
            selected = [
                entry
                for entry in selected
                if (entry["source"], entry["quote"]) != (old.source, old.text)
            ]

            for entry in selected:
                if (entry["source"], entry["quote"]) == (new.source, new.text):
                    entry["label"] = old.label if relation == "supersedes" else "decision"

            records.append(
                {
                    "previous": {"source": old.source, "turn": old.turn, "text": old.text},
                    "replacement": {"source": new.source, "turn": new.turn, "text": new.text},
                    "relation": relation,
                }
            )

        facts = select_facts(
            json.dumps({"entries": selected}), protected, retired=frozenset(retired)
        )
        return MemorySelection(facts, tuple(records))
    except ValueError, TypeError, KeyError:
        raise MemoryBlocked("summary_invalid") from None


def _explicit(old: MemoryFact, new: MemoryFact, relation: object) -> bool:

    # The selector supplies a relation, never authority. Require a replacement
    # cue in the exact later excerpt and an overlapping subject/value. 'The job
    # completed' has neither and cannot resolve 'When will payment complete?'.
    if relation == "supersedes":
        allowed = old.label in ("goal", "constraint", "decision", "next_step", "identity")
        allowed = allowed and new.kind == "user"
        cue = (
            r"\b(replace\w*|instead|supersed\w*|correction|revis\w*|no longer|"
            r"vervang\w*|in plaats|correctie|herzien\w*|niet langer)\b"
        )
    elif relation == "resolves":
        allowed = old.label == "open_question"
        cue = r"\b(answer\w*|resolv\w*|confirmed|decided|antwoord\w*|opgelost|bevestigd|besloten)\b"
    else:
        return False

    noise = {
        "question",
        "decision",
        "which",
        "what",
        "when",
        "will",
        "that",
        "this",
        "have",
        "vraag",
        "besluit",
        "welke",
        "wanneer",
        "deze",
        "wordt",
        "zijn",
        "voor",
        "open",
    }

    # Only reviewed subject aliases bridge these two languages. They establish
    # no equivalence between people/projects and never change the source quote.
    # For example, 'Welke talen?' and 'Languages confirmed: English' share a
    # subject, while 'Payment confirmed' still cannot resolve the language question.
    aliases = {
        "begroting": "budget",
        "talen": "languages",
        "taal": "language",
        "beoordelaar": "reviewer",
        "valuta": "currency",
        "datum": "date",
        "locatie": "venue",
        "bijnaam": "nickname",
        "eigenaar": "owner",
    }

    def words(text):
        return {aliases.get(word, word) for word in re.findall(r"\w{4,}", text.lower())} - noise

    return bool(allowed and re.search(cue, new.text, re.I) and words(old.text) & words(new.text))
