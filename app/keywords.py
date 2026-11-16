"""Keyword channel for retrieval, next to the vector search (see README, "How retrieval works").

Literal words of the question are matched against chunks.content, ignoring case and accents (Postgres `unaccent`), and the
result is fused with the vector results by reciprocal rank fusion. No index and no new column: it scans one document's chunks.
"""
import re

from highlight import STOP, fold

KEYWORD_K = 3        # keyword chunks taken
CUTOFF = 0.5         # a keyword chunk must score at least this fraction of the best one
RRF_K = 60           # reciprocal rank fusion constant (the usual value)
FINAL_K = 10         # cap on the fused list when the keyword channel found something (vector TOP_K 8 + 2)
MAX_TERMS = 12
PREFIX_MIN = 4       # terms of 4+ chars match at the start of a word ("indice" -> "indices"); shorter ones as whole words

# one statement: df and score are computed in the database; only page and content come back (never the embedding)
SQL = """
WITH c AS (SELECT id, page, content, unaccent(lower(content)) AS f FROM chunks WHERE document_id = %(doc)s),
     p AS (SELECT unnest(%(pats)s::text[]) AS pat),
     m AS (SELECT c.id, c.page, c.content, p.pat FROM c JOIN p ON c.f ~ p.pat),
     df AS (SELECT pat, count(*) AS n FROM m GROUP BY pat),
     tot AS (SELECT count(*) AS n FROM c)
SELECT m.page, m.content, sum(ln(tot.n::float8 / df.n)) AS score
FROM m JOIN df USING (pat) CROSS JOIN tot
GROUP BY m.id, m.page, m.content
HAVING sum(ln(tot.n::float8 / df.n)) > 0
ORDER BY score DESC, m.page ASC, m.id ASC
LIMIT %(k)s
"""


def terms(question: str) -> list[str]:
    """Search terms: folded (lowercase, no accents), letters and digits only, generic stopwords dropped, no duplicates.
    A question made only of stopwords gives [] and the keyword channel is skipped."""
    out = []
    for w in re.findall(r"[^\W_]+", fold(question)):
        if w in STOP or (len(w) < 2 and not w.isdigit()) or not w.isascii() or w in out:
            continue  # non-ASCII leftovers (other scripts) are not searched: they would not agree with unaccent
        out.append(w)
    return out[:MAX_TERMS]


def pattern(term: str) -> str:
    """Postgres regex for a term; \\y is a word boundary. Terms only hold [a-z0-9], so nothing needs escaping."""
    return rf"\y{term}" if len(term) >= PREFIX_MIN else rf"\y{term}\y"


def pick(rows: list[tuple]) -> list[tuple[int, str]]:
    """rows: (page, content, score), best first. Keep the top KEYWORD_K whose score is at least CUTOFF of the best."""
    if not rows:
        return []
    best = rows[0][2]
    return [(p, t) for p, t, s in rows[:KEYWORD_K] if s >= CUTOFF * best]


async def search(conn, doc_id: str, question: str) -> list[tuple[int, str]]:
    ts = terms(question)
    if not ts:
        return []
    cur = await conn.execute(SQL, {"doc": doc_id, "pats": [pattern(t) for t in ts], "k": KEYWORD_K})
    return pick(await cur.fetchall())


def fuse(vector: list[tuple], keyword: list[tuple]) -> list[tuple]:
    """Reciprocal rank fusion of two ranked lists of (page, content). Without keyword hits the vector list is returned as is.
    Otherwise: score = sum of 1/(RRF_K + rank) over the lists a chunk is in; ties keep vector order first; capped at FINAL_K."""
    if not keyword:
        return list(vector)
    score, order = {}, []
    for lst in (vector, keyword):
        for rank, key in enumerate(lst, 1):
            key = tuple(key)
            if key not in score:
                score[key] = 0.0
                order.append(key)
            score[key] += 1 / (RRF_K + rank)
    return sorted(order, key=lambda k: -score[k])[:FINAL_K]  # sorted() is stable: equal scores keep first-seen order
