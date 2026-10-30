"""Which sentence of a cited page supports an answer, and where it sits on the page.

Pure functions, no model call: pick the closest sentence by token overlap, align it to the page's word boxes
(page_words: [x0, y0, x1, y1, text] in reading order, PDF points as displayed), merge the boxes into one rectangle per line.
"""
import re
import unicodedata

MIN_SCORE = 0.35     # overlap score (see score()) below which a citation gets no highlight
MIN_COVERAGE = 0.7   # share of the sentence's content words that must be found on the page
MAX_CHARS = 400      # highlighted text: one sentence, or two consecutive ones, at most this long

MARK = re.compile(r"\[p\.\s*(\d+)\]")

# Unaccented, lowercase: tokens are folded before they are compared with this list.
STOP = set("""
a about after all also am an and any are as at be been but by can could did do does for from had has have he her his how i if in into is it its
more most no not of on one or our she so some such than that the their them then there these they this to up was we were what when where which
who will with would you your
a al algo ante como con contra cual cuando de del desde donde el ella ellos en entre era es esa ese eso esta este esto fue ha han hay la las le les lo los mas
me mi muy ni no nos o para pero por que se si sin sobre son su sus te tiene un una uno y ya
au aux avec ce ces dans de des du elle elles en est et il ils je la le les leur mais me ne nous on ou par pas pour qu que qui sa se ses son sur te tu un une vous
""".split())

_SENTENCE_END = re.compile(r"(?<=[.!?…])\s+")


def fold(s: str) -> str:
    """Lowercase, accent-insensitive; ligatures (fi, fl, ff), soft hyphens and non-breaking spaces become plain text."""
    s = unicodedata.normalize("NFKD", s.replace("\xad", "")).casefold().replace("œ", "oe").replace("æ", "ae")
    return "".join(c for c in s if not unicodedata.combining(c))


def tokens(s: str) -> list[str]:
    return re.findall(r"[^\W_]+", fold(s))  # punctuation and quotes (curly or straight) just separate tokens


def content(s: str) -> set[str]:
    return {t for t in tokens(s) if t not in STOP and (len(t) > 1 or t.isdigit())}


def word_tokens(words: list[str]) -> list[tuple[str, int, int]]:
    """(token, first_word, last_word) per token. A word broken at a line end ("infor-", "mation") gives the joined token."""
    out, i = [], 0
    while i < len(words):
        w, j = words[i], i
        if len(w) > 1 and w.endswith(("-", "\xad")) and i + 1 < len(words) and words[i + 1][:1].islower():
            w, j = w[:-1] + words[i + 1], i + 1
        out += [(t, i, j) for t in tokens(w)]
        i = j + 1
    return out


def sentences(text: str) -> list[str]:
    """Split at . ! ? followed by a capital; a full stop before a lowercase word ("e.g. the", "p. 3") is not a boundary."""
    out = []
    for p in _SENTENCE_END.split(text.strip()):
        if out and not (p[:1].isupper() or p[:1] in "¿¡«“\"(["):
            out[-1] += " " + p
        elif p:
            out.append(p)
    cut = []
    for s in out:
        while len(s) > MAX_CHARS:  # no stop in sight: cut at a word boundary
            k = s.rfind(" ", 0, MAX_CHARS)
            cut.append(s[:k if k > 0 else MAX_CHARS])
            s = s[k if k > 0 else MAX_CHARS:].lstrip()
        cut.append(s)
    return cut


def score(claim: set[str], cand: set[str]) -> float:
    """F2 of the shared content words: recall of the claim counts four times as much as precision, so a short claim
    can still match a long source sentence, but a window padded with unrelated sentences loses."""
    hit = len(claim & cand)
    if not hit:
        return 0.0
    p, r = hit / len(cand), hit / len(claim)
    return 5 * p * r / (4 * p + r)


def best_sentence(claims: list[str], chunks: list[str]) -> str | None:
    """The sentence (or two consecutive sentences, <= MAX_CHARS) of the chunks closest to any claim, or None below MIN_SCORE."""
    best = (0.0, 0, None)
    for chunk in chunks:
        ss = sentences(chunk)
        for i in range(len(ss)):
            for text in (ss[i], f"{ss[i]} {ss[i + 1]}" if i + 1 < len(ss) else None):
                if text is None or len(text) > MAX_CHARS:
                    continue
                cand = content(text)
                for claim in claims:
                    cl = content(claim)
                    if cl and cand:
                        best = max(best, (score(cl, cand), -len(text), text), key=lambda b: b[:2])
    return best[2] if best[0] >= MIN_SCORE else None


def align(sentence: str, words: list) -> tuple[int, int] | None:
    """(first, last) index of the page words holding the sentence: exact contiguous token match first,
    otherwise the window with the best overlap, if it covers at least MIN_COVERAGE of the sentence's content words."""
    want = [t for t, _, _ in word_tokens(sentence.split())]
    have = word_tokens([w[4] for w in words])
    seq, n = [t for t, _, _ in have], len(want)
    if not n:
        return None
    for i in range(len(seq) - n + 1):
        if seq[i:i + n] == want:
            return have[i][1], have[i + n - 1][2]
    key = {t for t in want if t not in STOP} or set(want)
    size = n + n // 4 + 2  # room for OCR splitting or merging words
    best = (0, 0)
    for i in range(len(seq)):
        hit = len(key & set(seq[i:i + size]))
        if hit > best[0]:
            best = (hit, i)
    if best[0] / len(key) < MIN_COVERAGE:
        return None
    idx = [i for i in range(best[1], min(best[1] + size, len(seq))) if seq[i] in key]
    return have[idx[0]][1], have[idx[-1]][2]


def line_rects(words: list, first: int, last: int) -> list[list[float]]:
    """One rectangle per text line over words[first..last]. A word joins the current line while it sits at a similar
    height and next to it (within a few line heights); a new line, a column change or a table gap starts another.
    Text turned by /Rotate 90 or 270 flows along y: the axes are swapped for the merge and back. Direction-agnostic otherwise."""
    span = words[first:last + 1]
    pairs = list(zip(span, span[1:]))
    sideways = sum(abs(a[1] + a[3] - b[1] - b[3]) for a, b in pairs) > sum(abs(a[0] + a[2] - b[0] - b[2]) for a, b in pairs)
    rects = []
    for w in span:
        x0, y0, x1, y1 = (w[1], w[0], w[3], w[2]) if sideways else w[:4]
        r = rects[-1] if rects else None
        if r and abs((y0 + y1) / 2 - (r[1] + r[3]) / 2) <= 0.5 * min(y1 - y0, r[3] - r[1]) and max(x0 - r[2], r[0] - x1) <= 3 * (y1 - y0):
            r[:] = [min(r[0], x0), min(r[1], y0), max(r[2], x1), max(r[3], y1)]
        else:
            rects.append([x0, y0, x1, y1])
    return [[round(v, 1) for v in ((r[1], r[0], r[3], r[2]) if sideways else r)] for r in rects]


def for_citation(answer: str, page: int, chunks: list[str], width: float, height: float, words: list) -> dict | None:
    """{page, width, height, rects} for the [p. page] chip of this answer, or None when no sentence matches well enough."""
    marked = MARK.sub(lambda m: f"⟦{m[1]}⟧", answer)
    claims = [re.sub(r"⟦\d+⟧", " ", s) for s in sentences(marked) if f"⟦{page}⟧" in s]
    sentence = best_sentence(claims, chunks) if claims else None
    span = align(sentence, words) if sentence else None
    if not span:
        return None
    return {"page": page, "width": width, "height": height, "rects": line_rects(words, *span)}
