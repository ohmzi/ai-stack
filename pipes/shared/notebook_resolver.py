"""
Notebook mode's deterministic notebook resolver, plus the Open Notebook client.

Split in two, deliberately:

  * The RESOLVER is pure. It takes the user's text and a list of notebook rows and returns a
    decision. No network, no clock, no globals — so it is testable offline and so the same input
    always produces the same answer. "Which notebook did they mean" is the whole feature, and
    guessing at it with a language model is precisely what this replaces.

  * The CLIENT is thin, synchronous urllib. The pipe runs it inside asyncio.to_thread, matching
    how this stack already talks to the hermes gateway. Every function takes `base` and the bearer
    token so tests can point them at a stub.

The scoring is measured, not intuited: tests/test_notebook_resolver.py pins the exact table this
produces for real phrasings, including the false positives it has to reject.
"""

from __future__ import annotations

import json
import re
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, Iterable, List, NamedTuple, Optional, Sequence, Tuple

__version__ = "1.0.0"

# ---------------------------------------------------------------------------------------------
# Fuzzy backend. rapidfuzz ships transitively with Open WebUI today, but "today" is doing a lot of
# work in that sentence — an upstream bump could drop it, and a resolver that disappears is worse
# than one that is slightly less sharp. difflib is always there.
# ---------------------------------------------------------------------------------------------

try:  # pragma: no cover - exercised by whichever branch the environment provides
    from rapidfuzz import fuzz as _fuzz

    _BACKEND = "rapidfuzz"

    def _ratio(a: str, b: str) -> float:
        return _fuzz.ratio(a, b) / 100.0

    def _token_set(a: str, b: str) -> float:
        return _fuzz.token_set_ratio(a, b) / 100.0

except Exception:  # pragma: no cover
    from difflib import SequenceMatcher

    _BACKEND = "difflib"

    def _ratio(a: str, b: str) -> float:
        return SequenceMatcher(None, a, b).ratio()

    def _token_set(a: str, b: str) -> float:
        # rapidfuzz's token_set_ratio compares the sorted intersection against each side and
        # returns the best of the three ratios. Close enough for the fallback, and the demotion
        # factors below are applied the same way either way.
        ta, tb = set(a.split()), set(b.split())
        inter = " ".join(sorted(ta & tb))
        if not inter:
            return 0.0
        return max(_ratio(inter, " ".join(sorted(ta))), _ratio(inter, " ".join(sorted(tb))))


# ---------------------------------------------------------------------------------------------
# Tunables. All measured against the real notebooks on this box — see the table in the test.
# ---------------------------------------------------------------------------------------------

HIGH = 0.85          # at or above, with a clear margin and a named match: answer directly
LOW = 0.55           # at or above, with a named match: offer to confirm
MARGIN = 0.15        # how far the winner must beat the runner-up to be unambiguous
SUGGEST_FLOOR = 0.50  # below this, the message names no notebook we can see
CUE_DISTANCE = 3     # how near a naming cue must sit to the matched window
CUE_SHARE = 0.34     # ...or how much of the message the name must occupy

# Verbs and nouns that mean "I am pointing at a notebook". Deliberately EXCLUDED: in, from, about,
# to, the — with `in` included, "the technical debt in this codebase is high" clears the gate
# against the notebook `techincal`, and positional prepositions are exactly the noise the gate
# exists to reject.
NAMING_CUES = frozenset({
    "check", "checking", "look", "consult", "search", "read", "ask", "use", "using",
    "review", "notebook", "book", "notes", "note", "docs", "doc", "document", "source", "open",
})

# Boilerplate stripped from the ends of a message once the notebook name has been removed, so the
# text handed to the answer engine is the question and not the instruction that found it.
_LEADING = frozenset({
    "please", "can", "could", "would", "you", "check", "checking", "look", "looking", "search",
    "consult", "use", "using", "read", "reading", "ask", "asking", "answer", "answering",
    "question", "query", "refer", "see", "in", "into", "on", "from", "the", "a", "an", "my",
    "our", "this", "that", "for", "and", "then", "to", "notebook", "book", "notes", "note",
    "docs", "doc", "document", "source",
})
_TRAILING = (
    "to answer this question", "answer this question", "for this question", "to answer",
    "and answer", "then answer", "please", "thanks", "thank you", "book", "notebook", "notes",
    "docs", "document", "the", "a", "an",
)


def norm(s: Any) -> str:
    """Fold a string to lowercase ASCII words separated by single spaces.

    Accents and smart quotes are decomposed away, and every non-alphanumeric becomes a SPACE
    rather than being deleted — so "self-help", "self help" and "Self  Help" all land on the same
    key, and "A.I." does not collapse into a single nonsense token.
    """
    s = unicodedata.normalize("NFKD", str(s or ""))
    s = "".join(c for c in s if not unicodedata.combining(c))
    return " ".join(re.sub(r"[^a-z0-9]+", " ", s.lower()).split())


class Score(NamedTuple):
    score: float
    named: bool
    span: Optional[Tuple[int, int]]  # half-open token indices into the ORIGINAL tokens


class Row(NamedTuple):
    id: str
    name: str
    description: str
    source_count: int
    score: float
    named: bool
    span: Optional[Tuple[int, int]]


class Resolution(NamedTuple):
    kind: str                    # "answer" | "confirm" | "suggest"
    rows: List[Row]              # candidates, best first
    question: str                # the message with the name and boilerplate stripped
    text: str = ""               # the original message


def _span_list(span) -> List[Tuple[int, int]]:
    """Accept one span or several, so callers can strip every name a message might have meant."""
    if not span:
        return []
    if isinstance(span[0], int):
        return [tuple(span)]  # type: ignore[return-value]
    return [tuple(s) for s in span]


def _tokens(text: str) -> List[Tuple[str, int, int, str]]:
    """Whitespace tokens of the ORIGINAL text, each with offsets and its normalized form.

    Alignment matters: the span the scorer returns indexes this list, and strip_question uses
    those indices to cut the name out of the text the user actually typed — keeping its
    punctuation and casing, because that text is destined for a language model.
    """
    return [(m.group(0), m.start(), m.end(), norm(m.group(0))) for m in re.finditer(r"\S+", text)]


def _window_score(win: str, name: str, size: int, k: int) -> float:
    """One window against one name, from both arms, best wins.

    The demotion factors are the point. token_set_ratio returns 1.0 whenever one side's tokens are
    a subset of the other's, so a single word out of a multi-word name scores a perfect 1.0. Exact
    arity keeps more of its score than the shrunk/grown windows do; everything else is pushed
    below a real phrase match.
    """
    return max(_ratio(win, name), _token_set(win, name) * (0.97 if size == k else 0.92))


def score_name(name: str, toks: Sequence[Tuple[str, int, int, str]]) -> Score:
    """Score one notebook name against a tokenized message. Pure."""
    n = norm(name)
    ntoks = n.split()
    if not n or not toks:
        return Score(0.0, False, None)

    k = len(ntoks)
    ntext = " ".join(t[3] for t in toks if t[3])
    best_score = 0.0
    best_span: Optional[Tuple[int, int]] = None

    for size in (k - 1, k, k + 1):
        if size < 1 or size > len(toks):
            continue
        for i in range(0, len(toks) - size + 1):
            win = " ".join(t[3] for t in toks[i:i + size] if t[3])
            if not win:
                continue
            s = _window_score(win, n, size, k)
            if s > best_score:
                best_score, best_span = s, (i, i + size)

    # Containment is checked against the whole message, so "islamic guidance" matches inside
    # "check islamic guidance to answer this question" regardless of window alignment.
    contained = n in ntext
    if contained:
        for i in range(0, max(1, len(toks) - k + 1)):
            if " ".join(t[3] for t in toks[i:i + k] if t[3]) == n:
                best_span = (i, i + k)
                break

    return Score(1.0 if contained else best_score, _is_named(n, contained, toks, best_span),
                 best_span)


def _is_named(n: str, contained: bool, toks: Sequence[Tuple[str, int, int, str]],
              span: Optional[Tuple[int, int]]) -> bool:
    """Does the message actually NAME this notebook, or does it merely look similar?

    This gate is the difference between a useful control and an embarrassing one. Measured:

        "the technical debt in this codebase is high"   scores 0.889 against `techincal`

    ...which is a higher score than "check the technical book" needs to win. Without the gate, a
    single-word notebook name turns every topical sentence into a notebook request. Four ways to
    earn the gate, because different phrasings need different evidence.
    """
    if contained:
        return True                                    # the name appears verbatim
    if len(n.split()) >= 2:
        return True                                    # multi-word names are self-evidencing
    if span is not None:
        lo = max(0, span[0] - CUE_DISTANCE)
        hi = min(len(toks), span[1] + CUE_DISTANCE)
        if any(t[3] in NAMING_CUES for t in toks[lo:hi]):
            return True
    ntok = len(n.split())
    msg = sum(1 for t in toks if t[3])
    return bool(msg) and (ntok / msg) >= CUE_SHARE       # "fictional please"


def resolve(text: str, notebooks: Iterable[Dict[str, Any]]) -> Resolution:
    """Decide which notebook a message is asking about. Pure and deterministic.

    Ordering is a TOTAL order — score, then source_count, then name, then id — so identical input
    always yields an identical list, ties included. A resolver that reorders ties between runs
    would make "reply with the second one" mean different things on different days.
    """
    toks = _tokens(text or "")
    rows: List[Row] = []
    for nb in notebooks or []:
        nb_id = str(nb.get("id") or "")
        name = str(nb.get("name") or "")
        s = score_name(name, toks)
        rows.append(Row(
            id=nb_id,
            name=name,
            description=str(nb.get("description") or ""),
            source_count=int(nb.get("source_count") or 0),
            score=s.score,
            named=s.named,
            span=s.span,
        ))

    rows.sort(key=lambda r: (-r.score, -r.source_count, r.name.lower(), r.id))

    top = rows[0] if rows else None
    second = rows[1].score if len(rows) > 1 else 0.0
    # Only cut the matched name out when the message really named it. On a spurious match the span
    # points at ordinary words — "tell me about the history of rome" matched a window around
    # "the history" and lost both — which would hand the notebook a mangled question. The suggest
    # path does not use the question anyway; this keeps it honest if that ever changes.
    question = strip_question(text or "", top.span if (top and top.named) else None)
    # When several notebooks were named, strip all of them: the confirmation is about which one
    # the user meant, and "fiction" left in the question reads as part of it. The score floor is
    # load-bearing — a multi-word name counts as `named` at ANY score (that is the gate's
    # self-evidencing arm), so without it every unrelated multi-word notebook contributes a span
    # and the question loses ordinary words ("how do i configure nginx" -> "how do nginx").
    if top and top.named:
        named_spans = [r.span for r in rows if r.named and r.span and r.score >= LOW]
        if len(named_spans) > 1:
            question = strip_question(text or "", named_spans)

    if top and top.named and top.score >= HIGH and (top.score - second) >= MARGIN:
        return Resolution("answer", rows, question, text or "")
    if top and top.named and top.score >= LOW:
        # Include everything close to the winner so a tie on the same name is visible rather than
        # silently resolved by sort order.
        cands = [r for r in rows if r.named and r.score >= LOW and (top.score - r.score) < MARGIN]
        return Resolution("confirm", cands or [top], question, text or "")
    near = [r for r in rows if r.score >= SUGGEST_FLOOR]
    return Resolution("suggest", near or rows, question, text or "")


_ORDINALS = {
    "1": 0, "1st": 0, "first": 0, "#1": 0, "one": 0,
    "2": 1, "2nd": 1, "second": 1, "#2": 1, "two": 1,
    "3": 2, "3rd": 2, "third": 2, "#3": 2, "three": 2,
    "4": 3, "4th": 3, "fourth": 3, "#4": 3, "four": 3,
    "5": 4, "5th": 4, "fifth": 4, "#5": 4, "five": 4,
}


def pick_candidate(text: str, cands: Sequence[Dict[str, Any]]) -> Optional[int]:
    """Which parked candidate is the user replying about? Index, or None.

    A name beats an ordinal, because a name is evidence and a bare digit is a guess about which
    list they are looking at. Everything here is matched against the parked candidates ONLY —
    never the whole notebook set — so an unrelated message cannot silently select a notebook that
    was never offered.
    """
    if not cands:
        return None
    toks = _tokens(text or "")

    best: Optional[Tuple[int, float]] = None
    for i, c in enumerate(cands):
        s = score_name(str(c.get("name") or ""), toks)
        # HIGH, not the `named` gate. A reply to a clarification is SHORT by construction, and the
        # gate's "the name is >=34% of the message" arm then fires for every single-word notebook:
        # "2", "maybe" and "never mind" all came back as a confident notebook choice. Requiring a
        # real score means only an actual (or near-miss) name counts here.
        if s.score >= HIGH and (best is None or s.score > best[1]):
            best = (i, s.score)
    if best is not None:
        return best[0]

    for t in toks:
        idx = _ORDINALS.get(t[3])
        if idx is not None and idx < len(cands):
            return idx
    return None


def strip_question(text: str, span) -> str:
    """Remove the matched notebook name(s) and the boilerplate around them.

    `span` is one (lo, hi) pair or a list of them — the confirm path passes every candidate the
    user might have named, so "check the historical fiction notebook on rome" does not leave
    "fiction notebook on rome" behind as the question.

    Cuts on the ORIGINAL text, not the normalized form: this string is what gets asked of the
    notebook, so it keeps its casing and punctuation.
    """
    toks = _tokens(text)
    spans = _span_list(span)
    if spans:
        drop = set()
        for lo, hi in spans:
            drop.update(range(max(0, lo), min(len(toks), hi)))
        kept = [t for i, t in enumerate(toks) if i not in drop]
    else:
        kept = list(toks)
    words = [t[0] for t in kept]

    changed = True
    while changed and words:
        changed = False
        # Trailing phrases are tested BEFORE the leading words, because the two overlap:
        # "check islamic guidance to answer this question" has "to" and "answer" in the leading
        # set, so stripping from the front first consumes the phrase word by word and leaves the
        # orphan "question" behind.
        low = " ".join(w.lower() for w in words)
        for phrase in _TRAILING:
            if low.endswith(" " + phrase) or low == phrase:
                words = words[:len(words) - len(phrase.split())]
                changed = True
                break
        if changed:
            continue
        while words and norm(words[0]) in _LEADING:
            words.pop(0)
            changed = True
    out = " ".join(words)
    return out.strip().lstrip(":-,–— ").strip()


# ---------------------------------------------------------------------------------------------
# Presentation
# ---------------------------------------------------------------------------------------------

# ---------------------------------------------------------------------------------------------
# Catalogue intent: "what is in here, and how do I ask about it?"
#
# Deterministic, like the resolver, and for the same reason: this decides whether a turn gets a
# description of the collection or an answer from it.
#
# HIGH PRECISION ON PURPOSE. The word "book" appears in ordinary questions constantly — the real
# session this was designed against opens with "What does this book talk about in regards to
# living within your means…", and "According to islamic books what should I do…". Every pattern
# below therefore requires a *collection* noun AND an inventory-shaped question around it; none of
# them fires on "what does the book say about X".
#
# Recall is deliberately cheap to get wrong: a missed catalogue question falls through to the
# resolve() path, which for an unnamed collection already lists the notebooks and says how to name
# one. A false positive, by contrast, refuses to answer a real question.
# ---------------------------------------------------------------------------------------------

_COLL = (r"(?:notebooks?|books?|sources?|categor(?:y|ies)|topics?|subjects?|collections?|library)")

CATALOG_PATTERNS = tuple(re.compile(p) for p in (
    # "what kind/type/sort of books there are"
    r"\bwhat (?:kind|kinds|type|types|sort|sorts|category|categories) of\b[^?]*\b" + _COLL + r"\b",
    # "list the notebooks", "list all books"
    r"\blist\b[^?]*\b" + _COLL + r"\b",
    # "what notebooks are there", "which books do you have"
    r"\b(?:what|which)\b[^?]*\b" + _COLL + r"\b[^?]*\b(?:are there|do you have|does it have|available|exist|there)\b",
    # "what can i ask", "which questions can i ask"
    r"\b(?:what|which)\b[^?]*\b(?:can|could|should) i ask\b",
    # "how do i ask a question", "how can i address them to ask"
    r"\bhow (?:do|can|should) i (?:ask|address|target|pick|choose|use)\b",
    r"\bhow do i ask\b",
    # "what can you answer"
    r"\bwhat can (?:you|i) (?:answer|ask|do)\b",
    # "what's in the notebooks" — norm() turns "what's" into "what s", hence the loose optional
    r"\bwhat(?: s| is)?\s*(?:in|available in)\b[^?]*\b(?:notebooks?|books|open notebook|library|sources?)\b",
    # "which notebook should i use" — the modal is what keeps "which notebook talks about X" out
    r"\b(?:what|which) notebooks?\b[^?]*\b(?:should|do|can|could|would)\b[^?]*\bi\b",
    # a bare "help"
    r"^\s*help\s*$",
))


def is_catalog_request(text: str) -> bool:
    """Is the user asking what is available, rather than asking something of it?"""
    n = norm(text)
    if not n:
        return False
    return any(p.search(n) for p in CATALOG_PATTERNS)


def example_ask(name: str) -> str:
    """The phrasing this mode actually matches, quoted back so the user can copy it."""
    return f"check {name.lower()} to answer this question: …"


def render_catalog(notebooks: Sequence[Dict[str, Any]],
                   sources: Dict[str, List[str]],
                   scoped: Optional[Dict[str, Any]] = None) -> str:
    """Describe the collection deterministically — no model, no summarising, no guessing.

    `sources` maps notebook id -> source titles. `scoped` limits the description to one notebook.
    """
    rows = [scoped] if scoped else list(notebooks)
    if not rows:
        return ("There are no notebooks in Open Notebook yet. Create one, add a source, then ask "
                "me about it.")

    if scoped:
        # The loop below already renders the notebook's own heading, so no intro here.
        head: List[str] = []
    else:
        usable = [r for r in rows if _rcount(r)]
        head = [
            f"Open Notebook has {len(rows)} notebook{'' if len(rows) == 1 else 's'}"
            + (f", {len(usable)} of them with sources." if len(usable) != len(rows) else "."),
            "",
            "Turn **Notebook** on and name what you want in your message — the name is matched "
            "against these, so you do not have to be exact:",
            "",
        ]

    lines = list(head)
    for r in rows:
        titles = sources.get(_rid(r)) or []
        lines.append(f"**{_rname(r)}** — {_n_sources(_rcount(r))}")
        if titles:
            for t in titles[:8]:
                lines.append(f"  • {t}")
            if len(titles) > 8:
                lines.append(f"  • …and {len(titles) - 8} more")
            lines.append(f"  → say: “{example_ask(_rname(r))}”")
        else:
            # A notebook whose count we know but whose titles we could not fetch must NOT be
            # described as empty — "no sources yet" is a claim about the user's data, and being
            # wrong about it sends them to add a source they already have.
            lines.append("  • *source list unavailable right now*" if _rcount(r)
                         else "  • *no sources yet — add one in Open Notebook before asking*")
        lines.append("")

    if not scoped:
        lines.append("You can also just ask “what’s in <notebook>” to see one of "
                     "them on its own.")
    return "\n".join(lines).rstrip()


def _n_sources(n: int) -> str:
    return "no sources yet" if not n else f"{n} source{'' if n == 1 else 's'}"


def catalog_suggestions(notebooks: Sequence[Dict[str, Any]],
                        sources: Dict[str, List[str]], limit: int = 4) -> List[str]:
    """Follow-up questions the catalogue can offer, derived from the collection itself.

    Open WebUI renders these as the clickable chips under the reply. It normally gets them from a
    language model; here they are built from the notebook names, so they are deterministic AND —
    the part that matters — every one of them is a phrasing this mode actually resolves. A
    suggestion naming a *book* would look helpful and then fail to find anything, because the
    resolver matches notebook names.

    Only notebooks with sources are offered. Suggesting one that cannot answer sends the user to a
    dead end that the catalogue just told them was empty.
    """
    out: List[str] = []
    for r in notebooks:
        if not _rcount(r):
            continue
        name = _rname(r).lower()
        out.append(f"check {name} to answer this question: what does it cover?")
        if len(out) >= limit:
            break
        out.append(f"what's in {name}")
        if len(out) >= limit:
            break
    return out[:limit]


def _rname(r: Any) -> str:
    """Read a field off either a Row or the plain dict the pipe parks in pending state."""
    return str((r.get("name") if isinstance(r, dict) else getattr(r, "name", "")) or "")


def _rid(r: Any) -> str:
    return str((r.get("id") if isinstance(r, dict) else getattr(r, "id", "")) or "")


def _rcount(r: Any) -> int:
    v = r.get("source_count") if isinstance(r, dict) else getattr(r, "source_count", 0)
    return int(v or 0)


def _rdesc(r: Any) -> str:
    v = r.get("description") if isinstance(r, dict) else getattr(r, "description", "")
    return str(v or "")


def render_confirm(res: Resolution) -> str:
    names = [f"**{_rname(r)}**" for r in res.rows]
    if len(names) == 1:
        head = f"That looks like {names[0]}"
    else:
        head = "Which did you mean — " + " or ".join(names) + "?"
    lines = [head + "." if len(names) == 1 else head]
    for i, r in enumerate(res.rows, 1):
        n = _rcount(r)
        lines.append(f"{i}. **{_rname(r)}** — {n} source{'' if n == 1 else 's'}"
                     + (f" — {_rdesc(r)}" if _rdesc(r) else ""))
    if res.question:
        lines.append("")
        lines.append(f"Reply with one, and I'll ask it: “{res.question}”")
    else:
        lines.append("")
        lines.append("Reply with one and tell me what to ask it.")
    return "\n".join(lines)


def render_suggest(res: Resolution, all_rows: Sequence[Row] = ()) -> str:
    rows = list(res.rows) or list(all_rows)
    if not rows:
        return ("I don't see any notebooks in Open Notebook yet. Create one and add a source, "
                "then ask me again.")
    lines = ["I couldn't tell which notebook you meant. The closest I can see:"]
    for i, r in enumerate(rows[:5], 1):
        n = _rcount(r)
        lines.append(f"{i}. **{_rname(r)}** — {n} source{'' if n == 1 else 's'}")
    lines.append("")
    lines.append("Name the notebook in your message — for example, "
                 f"“check {_rname(rows[0]).lower()} to answer this question”.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------------------------
# Citations. The markers are written by the model, not validated by the server, so expect junk.
# ---------------------------------------------------------------------------------------------

# Observed in real answers: [source:abc], [source_insight:abc], [note:abc].
MARKER_RE = re.compile(r"\[(source_insight|source|note):([A-Za-z0-9_]+)\]")

# Which endpoint resolves which marker kind, and the record prefix the API needs.
_TABLE = {
    "source": ("/api/sources/", "source:"),
    "source_insight": ("/api/insights/", "source_insight:"),
    "note": ("/api/notes/", "note:"),
}


def renumber(answer: str) -> Tuple[str, List[Tuple[str, str]]]:
    """Replace inline markers with footnote numbers, returning the new text and the target list.

    The markers exist for the model's benefit; a reader gets "[1]" and a Sources footer. Order is
    first-appearance so the numbering reads down the page.
    """
    order: List[Tuple[str, str]] = []
    index: Dict[Tuple[str, str], int] = {}

    def sub(m: "re.Match[str]") -> str:
        key = (m.group(1), m.group(2))
        if key not in index:
            order.append(key)
            index[key] = len(order)
        return f"[{index[key]}]"

    return MARKER_RE.sub(sub, answer or ""), order


def resolve_citations(base: str, auth: Optional[str], targets: Sequence[Tuple[str, str]],
                      timeout: int = 15) -> List[Dict[str, str]]:
    """Turn (kind, bare-id) pairs into titled sources, dropping the ones that will not resolve.

    THE TRAP: the id in a marker has had its record prefix stripped, and putting it back wrong
    fails QUIETLY. GET /api/sources/<bare-id> answers 200 with title: null rather than erroring,
    so a naive implementation ships a confidently blank source list. Always re-attach the prefix.

    Insights are worse: they carry no title at all, only a source_id, so an insight citation is
    resolved by following that to the source it summarises.
    """
    out: List[Dict[str, str]] = []
    cache: Dict[Tuple[str, str], Optional[Dict[str, str]]] = {}
    for kind, bare in targets:
        key = (kind, bare)
        if key not in cache:
            cache[key] = _resolve_one(base, auth, kind, bare, timeout)
        row = cache[key]
        if row:
            out.append(row)
    return out


def _resolve_one(base: str, auth: Optional[str], kind: str, bare: str,
                 timeout: int) -> Optional[Dict[str, str]]:
    path, prefix = _TABLE.get(kind, (None, None))
    if not path:
        return None
    status, data = request(base, f"{path}{urllib.parse.quote(prefix + bare, safe=':')}",
                           auth, timeout=timeout)
    if status != 200 or not isinstance(data, dict):
        return None
    if kind == "source_insight":
        # No title of its own — go to the source it came from.
        parent = data.get("source_id")
        if not parent:
            return None
        status, data = request(base, f"/api/sources/{urllib.parse.quote(str(parent), safe=':')}",
                               auth, timeout=timeout)
        if status != 200 or not isinstance(data, dict):
            return None
    title = data.get("title") or data.get("name")
    if not title:
        return None
    return {"kind": kind, "id": f"{prefix}{bare}", "title": str(title)}


def format_sources(resolved: Sequence[Dict[str, str]]) -> str:
    if not resolved:
        return ""
    lines = ["", "---", "**Sources**"]
    for i, r in enumerate(resolved, 1):
        lines.append(f"{i}. {r['title']}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------------------------
# Open Notebook client. Synchronous on purpose: the pipe wraps each call in asyncio.to_thread,
# which is how this stack already reaches the hermes gateway.
# ---------------------------------------------------------------------------------------------

class NotebookError(Exception):
    """Carries a message already fit to show the user."""

    def __init__(self, message: str, status: Optional[int] = None):
        super().__init__(message)
        self.message = message
        self.status = status


def _headers(auth: Optional[str], json_body: bool) -> Dict[str, str]:
    h = {"Accept": "application/json"}
    if json_body:
        h["Content-Type"] = "application/json"
    if auth:
        h["Authorization"] = f"Bearer {auth}"
    return h


def request(base: str, path: str, auth: Optional[str], *, method: str = "GET",
            body: Optional[Dict[str, Any]] = None, timeout: int = 20) -> Tuple[int, Any]:
    """One call. Returns (status, parsed). Raises NotebookError only for transport failure.

    HTTP error statuses are returned, not raised: several are meaningful and each has its own
    message (404 unknown notebook, 400 no embedding model, 401 password).
    """
    url = base.rstrip("/") + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers=_headers(auth, body is not None),
                                 method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", "replace")
            return r.status, (json.loads(raw) if raw.strip() else {})
    except urllib.error.HTTPError as e:
        raw = ""
        try:
            raw = e.read().decode("utf-8", "replace")
        except Exception:
            pass
        try:
            payload = json.loads(raw) if raw.strip() else {}
        except Exception:
            payload = {"detail": raw[:300]}
        return e.code, payload
    except Exception as e:
        raise NotebookError(
            f"Open Notebook isn't reachable at {base} — nothing was asked. "
            f"(Notebook mode answers only from Open Notebook.) [{type(e).__name__}]"
        )


def _detail(payload: Any, fallback: str) -> str:
    if isinstance(payload, dict):
        d = payload.get("detail") or payload.get("message")
        if isinstance(d, str) and d.strip():
            return d.strip()
    return fallback


def list_notebooks(base: str, auth: Optional[str], timeout: int = 15) -> List[Dict[str, Any]]:
    status, data = request(base, "/api/notebooks", auth, timeout=timeout)
    if status == 401:
        raise NotebookError(
            "Open Notebook rejected the request (401). Set OPEN_NOTEBOOK_PASSWORD for the "
            "assistant to the same value Open Notebook expects.", 401)
    if status != 200 or not isinstance(data, list):
        raise NotebookError(_detail(data, f"Open Notebook returned HTTP {status} listing "
                                         f"notebooks."), status)
    return [d for d in data if isinstance(d, dict)]


def default_models(base: str, auth: Optional[str], timeout: int = 15) -> Optional[List[str]]:
    """(strategy, answer, final) model ids, or None when none is configured.

    Deliberately reads ONLY the configured default. Picking a model off /api/models by position or
    by name is guesswork by another route, and avoiding guesswork is the point of this feature.
    """
    status, data = request(base, "/api/models/defaults", auth, timeout=timeout)
    if status != 200 or not isinstance(data, dict):
        return None
    mid = data.get("default_chat_model")
    if not mid:
        return None
    return [str(mid)] * 3


def source_ids_for(base: str, auth: Optional[str], notebook_id: str,
                   timeout: int = 20, max_pages: int = 10) -> List[str]:
    """Every source id linked to a notebook. `limit` caps at 100, so page until a short page."""
    ids: List[str] = []
    offset = 0
    for _ in range(max_pages):
        q = urllib.parse.urlencode({
            "notebook_id": notebook_id, "limit": 100, "offset": offset,
        })
        status, data = request(base, f"/api/sources?{q}", auth, timeout=timeout)
        if status != 200 or not isinstance(data, list):
            raise NotebookError(_detail(data, f"Open Notebook returned HTTP {status} listing "
                                             f"sources."), status)
        ids.extend(str(s.get("id")) for s in data if isinstance(s, dict) and s.get("id"))
        if len(data) < 100:
            break
        offset += 100
    return ids


def probe_scope_support(base: str, auth: Optional[str], timeout: int = 20) -> Optional[bool]:
    """Does this server actually honour a notebook scope? Measured, never assumed.

    A notebook id that cannot exist is sent in the scope field and the status tells the story:

        200 -> the field was dropped and the search ran globally. Scoping is NOT supported.
        404 -> the server validated the id and found no such notebook. Scoping IS supported.
        400 -> it parsed the id and rejected it. Also proves the field is read.

    Returns None when the answer is unclear (transport failure, unexpected status). Callers must
    treat None as "not supported" — never assert a scope that cannot be enforced, because the
    failure mode is an answer from the entire knowledge base presented as an answer from one
    notebook.
    """
    body = {"query": "scope probe", "type": "text", "limit": 1,
            "notebook_id": "notebook:scope_probe_probe"}
    try:
        status, _ = request(base, "/api/search", auth, method="POST", body=body, timeout=timeout)
    except NotebookError:
        return None
    if status == 200:
        return False
    if status in (400, 404):
        return True
    return None


def list_source_titles(base: str, auth: Optional[str], notebook_id: str,
                       timeout: int = 20, max_pages: int = 10) -> List[str]:
    """The titles of a notebook's sources — what the catalogue shows the user.

    Same endpoint and paging as source_ids_for, which discards the titles; both exist because the
    scoped-retrieval path wants ids and the catalogue wants names.
    """
    titles: List[str] = []
    offset = 0
    for _ in range(max_pages):
        q = urllib.parse.urlencode({"notebook_id": notebook_id, "limit": 100, "offset": offset})
        status, data = request(base, f"/api/sources?{q}", auth, timeout=timeout)
        if status != 200 or not isinstance(data, list):
            raise NotebookError(_detail(data, f"Open Notebook returned HTTP {status} listing "
                                             f"sources."), status)
        titles.extend(str(s.get("title") or "untitled")
                      for s in data if isinstance(s, dict))
        if len(data) < 100:
            break
        offset += 100
    return titles


def ask_stream_url(base: str) -> str:
    return base.rstrip("/") + "/api/search/ask"


def ask_headers(auth: Optional[str]) -> Dict[str, str]:
    h = {"Accept": "text/event-stream", "Content-Type": "application/json"}
    if auth:
        h["Authorization"] = f"Bearer {auth}"
    return h


def build_ask_payload(question: str, notebook_id: str, models: Sequence[str]) -> Dict[str, Any]:
    return {
        "question": question,
        "notebook_id": notebook_id,
        "strategy_model": models[0],
        "answer_model": models[1],
        "final_answer_model": models[2],
    }


def parse_sse_line(line: bytes) -> Optional[Dict[str, Any]]:
    """One `data: {json}` frame, or None for anything else (comments, blanks, keepalives)."""
    try:
        text = line.decode("utf-8", "replace").strip()
    except Exception:
        return None
    if not text.startswith("data:"):
        return None
    payload = text[5:].strip()
    if not payload or payload == "[DONE]":
        return None
    try:
        obj = json.loads(payload)
    except Exception:
        return None
    return obj if isinstance(obj, dict) else None
