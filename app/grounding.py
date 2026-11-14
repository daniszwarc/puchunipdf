"""Names in an answer that the retrieved excerpts do not contain. A hint, not a verdict (see README).

It only looks at capitalized names (people, authors, organizations, works) and acronyms of 3+ letters. It cannot see invented
ideas, lower-case names, a lone capitalized word that starts a sentence, or a name translated between languages ("Francia"
vs "France" is flagged)."""
import re

from highlight import STOP, fold

CITE = re.compile(r"\[p\.\s*\d+\]")
WORD = re.compile(r"[^\W\d_]+(?:['’-][^\W\d_]+)*")
LETTERS = re.compile(r"[^\W\d_]+")
CONNECT = set("de del la las los le da do dos das du di van von der den of".split())  # allowed inside a name: "Ministerio de Educación"
# Sentence openers and capitalized common words that are not names.
SKIP = STOP | set("""segun ademas tambien asi finalmente selon aussi donc cependant according however also finally
january february march april may june july august september october november december
monday tuesday wednesday thursday friday saturday sunday english french spanish""".split())


def is_name(w: str) -> bool:
    return (w[0].isupper() and not w.isupper() or w.isupper() and len(w) >= 3) and fold(w) not in SKIP


def names(text: str) -> list[str]:
    """Runs of consecutive capitalized words (with connectors inside), as written. At the start of a sentence, leading
    openers are dropped and a lone word is ignored: it may just be a capitalized common word."""
    text = CITE.sub("", text)
    runs, run, held, prev = [], None, [], 0  # run = [words, starts_sentence]
    for m in WORD.finditer(text):
        w, gap = m.group(), text[prev:m.start()]
        joined = not gap.strip(" \t\xa0")  # only spaces between this word and the last: a comma, quote or new line ends the run
        if run and joined and is_name(w):
            run[0] += held + [w]
            held = []
        elif run and joined and w in CONNECT:
            held = held + [w]
        else:
            if run:
                runs.append(run)
            run, held = ([[w], prev == 0 or re.search(r"[.!?…:\n¿¡]", gap) is not None] if is_name(w) else None), []
        prev = m.end()
    if run:
        runs.append(run)
    out = []
    for words, initial in runs:
        if initial:
            while words and fold(words[0]) in SKIP:
                words = words[1:]
            if len(words) < 2:
                continue
        out.append(" ".join(words))
    return out


def ungrounded(answer: str, excerpts: list[str], question: str = "") -> list[str]:
    """Names of the answer with a word that appears in neither the excerpts nor the question (case and accents ignored).
    "Eva Illouz" against an excerpt that only says "ILLOUZ E." is flagged: "eva" is not there."""
    have = set(LETTERS.findall(fold(" ".join([*excerpts, question]))))
    out = []
    for name in names(answer):
        parts = [p for p in LETTERS.findall(fold(name)) if p not in CONNECT and len(p) > 1]
        if not all(p in have for p in parts) and name not in out:
            out.append(name)
    return out
