"""Content language of a document (ADR-0022, second of the three levels).

A deliberately small heuristic: score the four languages of the first
stage (de, fr, it, en) by their most frequent function words. Documents in
a Swiss household are letters, invoices, statements - a few hundred words
of ordinary prose - and there stop words separate the languages reliably.
No model, no dependency, deterministic and auditable; a model may refine
the guess later inside the classification step, where the text is read
anyway.

``detect`` returns ``None`` when the text is too short or the scores do not
separate - "unknown" is a better answer than a wrong one, because the
result feeds Vermittler rules and the reply-language policy.
"""

from __future__ import annotations

import re
from collections import Counter

_WORDS = {
    "de": (
        "der die das und ist nicht mit für von auf den dem des ein eine einer sie wir ihnen "
        "ihre ihr bitte sehr geehrte betrag rechnung zahlung datum bei nach zum zur werden "
        "wurde haben sind oder auch als bis über unter freundliche grüsse grüssen betreff "
    ),
    "fr": (
        "le la les et est pas avec pour de des du un une vous nous votre vos veuillez "
        "madame monsieur montant facture paiement date chez dans sur sont ont ou aussi que "
        "qui ce cette salutations cordialement objet "
    ),
    "it": (
        "il lo la gli le e è non con per di del della dei delle un una uno voi noi vostro "
        "vostra gentile signora signore importo fattura pagamento data presso nel nella "
        "sono hanno o anche che chi questo questa cordiali saluti oggetto "
    ),
    "en": (
        "the and is not with for of on to in a an you we your our please dear amount "
        "invoice payment date at by from are have or also as that this which regards "
        "sincerely subject "
    ),
}
STOPWORDS: dict[str, frozenset[str]] = {k: frozenset(v.split()) for k, v in _WORDS.items()}

_WORD = re.compile(r"[a-zà-ÿ]+", re.IGNORECASE)
MIN_WORDS = 8
MIN_HITS = 3


def detect(text: str, *, min_words: int = MIN_WORDS) -> str | None:
    """Best guess among de/fr/it/en, or ``None`` when unsure."""
    words = [w.lower() for w in _WORD.findall(text or "")]
    if len(words) < min_words:
        return None
    counts = Counter(words)
    scores = {
        lang: sum(n for w, n in counts.items() if w in stop) for lang, stop in STOPWORDS.items()
    }
    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    best, runner = ranked[0], ranked[1]
    if best[1] < MIN_HITS or best[1] < runner[1] * 1.5:
        return None
    return best[0]
