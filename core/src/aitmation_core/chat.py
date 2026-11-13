"""Chat as an input path, not a chatbot (ZIELBILD §9, ADR-0021).

The person types a sentence, the instance recognizes *which of its own
views* is meant and answers with structured data - open decisions, cases,
one case's trail. Deciding still happens at the gate, with the same
controls as everywhere else; the chat only brings the gate to the person.

Intent recognition is deterministic keyword matching in the four
languages of ADR-0022, deliberately not a model call: it costs nothing,
works offline, is reproducible in the audit, and cannot be talked into
something by the text of a document. A model may sharpen this later - the
interface (an :class:`Intent`) stays the same.

Answers travel as a key plus parameters (ADR-0022: no German strings out
of the core); the shell translates them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

KIND_PENDING = "pending"
KIND_CASES = "cases"
KIND_CASE = "case"
KIND_HELP = "help"
KIND_START = "start"
KIND_UNKNOWN = "unknown"

# Word stems per intent, all four languages in one list. Matching is on
# whole words so "offene" matches but "offenbar" does not.
_KEYWORDS: dict[str, str] = {
    KIND_PENDING: (
        "offen offene offenes pendenz pendenzen entscheiden entscheidung freigabe "
        "freigeben wartet warten anstehend attente attentes décider decider décision "
        "decision ouvert ouverts ouverte en_attente approbation sospeso sospesi "
        "decidere decisione aperto aperti approvazione pending open decide decision "
        "decisions approve approval waiting todo "
    ),
    KIND_CASES: (
        "vorgang vorgänge vorgaenge fall fälle faelle dossier dossiers affaire affaires "
        "pratica pratiche case cases status stand übersicht uebersicht aperçu apercu "
        "panoramica overview letzte letzten recent recenti récents recents "
    ),
    KIND_HELP: (
        "hilfe hilf help aide aiuto was kannst kannst_du fähigkeiten faehigkeiten "
        "commandes comandi commands "
    ),
}
# Verbs that ask for a process to be started; which one is decided by the
# words of the process ids and titles the instance offers (``starters``).
_START_TEXT = (
    "starte starten start erstelle erstellen erstell mach mache anlegen ausführen ausfuehren "
    "neuer neue neues lance lancer lancez crée créer creer démarre demarre démarrer nouveau "
    "nouvelle avvia avviare crea creare nuovo nuova run create make launch new begin "
    "recherchiere recherchier recherchieren research rechercher cerca ricerca"
)
_START_WORDS = frozenset(_START_TEXT.split())
# Words that say nothing about which process is meant.
_NOISE_TEXT = (
    "ein eine einen der die das den dem des und mit für fur zu zum zur bitte jetzt heute "
    "un une le la les des du de et pour avec il lo gli le per con a an the for with to of "
    "about on please now today über ueber sur su vorgang prozess process processus processo"
)
_NOISE = frozenset(_NOISE_TEXT.split())
KEYWORDS: dict[str, tuple[str, ...]] = {k: tuple(v.split()) for k, v in _KEYWORDS.items()}

# A case id as the API hands it out: 32 hex characters, prefix is enough.
_CASE_ID = re.compile(r"\b([0-9a-f]{8,32})\b")
_WORD = re.compile(r"[\w]+", re.UNICODE)
_STATUS_HINTS: dict[str, str] = {
    "running": "laufend laufende offen en_cours corso running ",
    "completed": "abgeschlossen erledigt fertig terminé termine concluso completed done ",
    "failed": "fehler gescheitert échoué echoue fallito failed error ",
}
_STATUS_WORDS = {k: frozenset(v.split()) for k, v in _STATUS_HINTS.items()}


@dataclass(frozen=True)
class Intent:
    kind: str
    case_id: str | None = None
    status: str | None = None
    words: tuple[str, ...] = field(default_factory=tuple)
    # start: the process to start (None = several fit, see candidates) and
    # the free text left after the verb and the process words
    process_id: str | None = None
    candidates: tuple[str, ...] = field(default_factory=tuple)
    remainder: str = ""


Starter = tuple[frozenset[str], frozenset[str]]  # (id words, title words)


def starter_words(process_id: str, titles: dict[str, str] | str | None) -> Starter:
    """The words a person may use to name a process: its id parts (they
    weigh double - "post" names make-post before "Post messen") and the
    words of its titles in every language, minus noise."""
    ids = set(_WORD.findall(process_id.lower().replace("-", " ").replace("_", " ")))
    names: set[str] = set()
    if isinstance(titles, dict):
        for text in titles.values():
            names.update(_WORD.findall(str(text).lower()))
    elif isinstance(titles, str):
        names.update(_WORD.findall(titles.lower()))
    keep = lambda ws: frozenset(w for w in ws if len(w) > 2 and w not in _NOISE)  # noqa: E731
    return keep(ids), keep(names - ids)


def parse(message: str, starters: dict[str, Starter] | None = None) -> Intent:
    """Map a sentence to one of the instance's views (or, with
    ``starters`` = process id -> naming words, to a process to start)."""
    text = (message or "").strip().lower()
    if not text:
        return Intent(KIND_UNKNOWN)
    words = tuple(_WORD.findall(text))
    wordset = set(words)

    other = {kind: len(wordset & set(keys)) for kind, keys in KEYWORDS.items()}
    if starters and wordset & _START_WORDS:
        # "Erstelle Aufgabe: Offerte Müller nachfassen" - what stands before
        # the colon names the process, what follows is the topic.
        naming = set(_WORD.findall(text.split(":", 1)[0])) if ":" in text else wordset
        if ":" in text and not any(_hits(naming, i | n) for i, n in starters.values()):
            naming = wordset
        scored = sorted(
            (
                (2 * _hits(naming, ids) + _hits(naming, names), pid)
                for pid, (ids, names) in starters.items()
                if _hits(naming, ids | names)
            ),
            reverse=True,
        )
        if scored:
            best = scored[0][0]
            fitting = tuple(pid for score, pid in scored if score == best)
        elif max(other.values()):
            fitting = ()  # "show new open decisions" names no process: a view
        else:
            fitting = tuple(sorted(starters))  # "start something" - ask which
        chosen = fitting[0] if len(fitting) == 1 else None
        if fitting:
            names = (starters[chosen][0] | starters[chosen][1]) if chosen else frozenset()
            used = _START_WORDS | names | {
                w for w in wordset if _hits({w}, names)
            }
            # The remainder keeps the person's own words (and their case)
            # for a topic field: the sentence without verb and process words.
            return Intent(
                KIND_START, words=words, process_id=chosen, candidates=fitting,
                remainder=_remainder(message, used),
            )

    found = _CASE_ID.search(text)
    case_words = ("vorgang", "dossier", "pratica", "case", "spur", "trail", "audit", "verlauf")
    if found and any(w in wordset for w in case_words):
        return Intent(KIND_CASE, case_id=found.group(1), words=words)
    if found and len(found.group(1)) >= 16:
        return Intent(KIND_CASE, case_id=found.group(1), words=words)

    scores = other
    best = max(scores, key=lambda k: scores[k])
    if scores[best] == 0:
        return Intent(KIND_UNKNOWN, words=words)
    # "offene Vorgänge" mentions both - the noun decides, pending wins only
    # when nothing points at the case list.
    if best == KIND_PENDING and scores[KIND_CASES] >= scores[KIND_PENDING]:
        best = KIND_CASES
    status = None
    if best == KIND_CASES:
        for value, hints in _STATUS_WORDS.items():
            # Prefix match: "abgeschlossene" and "abgeschlossen" mean the same.
            if any(w.startswith(h) or h.startswith(w) for w in words for h in hints):
                status = value
                break
    return Intent(best, status=status, words=words)


def _hits(words: set[str], names: frozenset[str]) -> int:
    """How many of the sentence's words name the process - exactly, or by
    a shared stem of six letters or more ("recherchiere" ~ "recherche")."""
    count = 0
    for w in words:
        if w in names or any(len(w) >= 6 and len(n) >= 6 and w[:6] == n[:6] for n in names):
            count += 1
    return count


def _remainder(message: str, used: frozenset[str] | set[str]) -> str:
    """The sentence without the verb and the process words, trimmed of
    leading connectors - "starte einen Post über Roboter" -> "Roboter"."""
    kept: list[str] = []
    for token in message.strip().split():
        bare = token.strip(".,;:!?\"'()").lower()
        if bare in used or (not kept and bare in _NOISE):
            continue
        kept.append(token)
    return " ".join(kept).strip(" .,;:!?")
