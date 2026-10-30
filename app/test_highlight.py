"""Unit tests for highlight.py (pure stdlib, no database).

Run:  python app/test_highlight.py        or        docker compose run --rm --no-deps app python test_highlight.py
"""
import highlight as h


def page(*lines, x=72, top=100, lh=14, cw=6):
    """Fake word boxes for text lines: words in reading order, one box per word, 6 pt per character."""
    words = []
    for n, line in enumerate(lines):
        cx = x
        for w in line.split():
            words.append([cx, top + n * lh, cx + cw * len(w), top + n * lh + 12, w])
            cx += cw * len(w) + cw
    return words


def texts(words, span):
    return " ".join(w[4] for w in words[span[0]:span[1] + 1])


# ---- normalization
assert h.tokens("Économie « d’énergie » — ÉTÉ") == ["economie", "d", "energie", "ete"]  # accents, curly quotes, dash
assert h.tokens("ﬁnancial ﬂow oﬀer ﬃne") == ["financial", "flow", "offer", "ffine"], h.tokens("ﬁnancial ﬂow oﬀer ﬃne")
assert h.tokens("infor\xadmation\xa0systems") == ["information", "systems"]  # soft hyphen, non-breaking space
assert h.tokens("Don't STOP, it's 3.5%") == ["don", "t", "stop", "it", "s", "3", "5"]
assert h.content("The cat and LE chat de la maison") == {"cat", "chat", "maison"}  # stopwords es/fr/en dropped
assert "es" not in h.content("¿Cuál es el motor?") and "motor" in h.content("¿Cuál es el motor?")
# line-end hyphenation joins; ordinary hyphens do not
assert h.word_tokens(["infor-", "mation", "flows"]) == [("information", 0, 1), ("flows", 2, 2)]
assert h.word_tokens(["well-known", "fact"]) == [("well", 0, 0), ("known", 0, 0), ("fact", 1, 1)]
assert h.word_tokens(["end-", "Of", "list"])[0] == ("end", 0, 0)  # next word capitalized: not a break
print("ok normalization: accents, ligatures, soft hyphen, nbsp, curly quotes, stopwords, line-end hyphenation")

# ---- sentence splitting
assert h.sentences("It rose 3% (see p. 3 for details). Costs fell. See e.g. the annex!") == \
    ["It rose 3% (see p. 3 for details).", "Costs fell.", "See e.g. the annex!"]
assert h.sentences("Costs fell ⟦2⟧. ⟦3⟧ Then rose.") == ["Costs fell ⟦2⟧. ⟦3⟧ Then rose."]  # a chip after a stop stays with its sentence
long = " ".join(["word"] * 200)
assert all(len(s) <= h.MAX_CHARS for s in h.sentences(long)) and len(h.sentences(long)) >= 2
print("ok sentences: abbreviations kept together, long runs cut at 400 characters")

# ---- sentence selection
chunk = ("The Falcon rocket has two stages. The first stage burns kerosene and liquid oxygen. "
         "The second stage uses a vacuum engine to reach orbit. The company was founded in 2002.")
pick = h.best_sentence(["The second stage runs a vacuum engine"], [chunk])
assert pick == "The second stage uses a vacuum engine to reach orbit.", pick
# two consecutive sentences when the claim spans both
pick = h.best_sentence(["It has two stages and the first stage burns kerosene"], [chunk])
assert pick == "The Falcon rocket has two stages. The first stage burns kerosene and liquid oxygen.", pick
# Spanish and French sources
assert h.best_sentence(["Los ingresos crecieron con fuerza en 2023"], ["Resumen. Los ingresos crecieron con fuerza durante 2023. Los costes bajaron."]) \
    == "Los ingresos crecieron con fuerza durante 2023."
assert h.best_sentence(["Les coûts ont baissé"], ["Les ventes ont augmenté. Les coûts ont fortement baissé cette année."]) \
    == "Les coûts ont fortement baissé cette année."
# a short claim still finds a long sentence; a window padded with unrelated sentences loses
assert h.best_sentence(["costs fell sharply"], ["Intro text here. Overall operating costs fell sharply across every region during the whole of last year. Other topic."]) \
    == "Overall operating costs fell sharply across every region during the whole of last year."
# paraphrase with almost no shared words: below the threshold, no highlight
para = h.best_sentence(["The upper stage relies on a motor optimized for airless conditions"], [chunk])
assert para is None, para
# never longer than 400 characters
capped = h.best_sentence(["alpha beta gamma"], ["alpha beta gamma delta. " * 40])
assert capped and len(capped) <= h.MAX_CHARS
# score() sanity: the tuned constant separates the cases above
claim = h.content("The second stage runs a vacuum engine")
assert h.score(claim, h.content("The second stage uses a vacuum engine to reach orbit.")) > h.MIN_SCORE
assert h.score(h.content("The upper stage relies on a motor optimized for airless conditions"), h.content(chunk)) < h.MIN_SCORE
print(f"ok selection: right sentence, 2-sentence window, es/fr, long sentence, paraphrase -> none (MIN_SCORE={h.MIN_SCORE})")

# ---- alignment on word boxes
w = page("Revenue grew strongly in the third quarter because the", "new subscription plan attracted many customers across", "Europe. Costs remained flat.")
sent = "Revenue grew strongly in the third quarter because the new subscription plan attracted many customers across Europe."
span = h.align(sent, w)
assert texts(w, span) == "Revenue grew strongly in the third quarter because the new subscription plan attracted many customers across Europe.", texts(w, span)
# hyphenated line break on the page, joined word in the sentence
w = page("The comprehensive infor-", "mation system stores records.")
assert texts(w, h.align("The comprehensive information system stores records.", w)) == "The comprehensive infor- mation system stores records."
# the other way round: hyphen in the (chunk) sentence, plain word on the page
w = page("The comprehensive information system stores records.")
assert h.align("The comprehensive infor- mation system stores records.", w) == (0, 5)
# ligatures on the page, plain letters in the sentence
w = page("Our ﬁnancial ﬂow improved after the oﬀer was accepted.")
assert h.align("Our financial flow improved after the offer was accepted.", w) == (0, 8)
# accents, curly quotes and nbsp
w = page("L’économie\xa0française", "a crû de 2 % cette année.")
assert h.align("L'economie francaise a cru de 2 % cette annee.", w) == (0, 8)
w = page("«L’économie", "française», a-t-il dit.")
assert h.align("L'economie francaise a-t-il dit.", w) == (0, 3)
# exact match wins over an earlier partial one
w = page("costs fell in March", "revenue rose and costs fell sharply in April")
assert texts(w, h.align("costs fell sharply in April", w)) == "costs fell sharply in April"
# OCR noise: one word misread, the window is still found and trimmed to matching words
w = page("Intro words here.", "The new subscrlption plan attracted rnany customers across Europe", "and more filler follows")
span = h.align("The new subscription plan attracted many customers across Europe.", w)
assert span and texts(w, span).startswith("new") and texts(w, span).endswith("Europe"), span
# too little of the sentence on the page: no highlight
w = page("Something entirely different is written on this page.")
assert h.align("Revenue grew strongly in the third quarter.", w) is None
print("ok alignment: exact, hyphenation both ways, ligatures, accents/quotes/nbsp, OCR noise window, low coverage -> none")

# ---- line rectangles
w = page("alpha beta gamma", "delta epsilon", "zeta eta")
r = h.line_rects(w, 1, 6)  # beta .. eta
assert len(r) == 3, r
assert r[0][0] == w[1][0] and r[0][2] == w[2][2] and r[1] == [w[3][0], w[3][1], w[4][2], w[4][3]] and r[2][2] == w[6][2]
assert len(h.line_rects(w, 0, 0)) == 1
# two-column order (left column top to bottom, then right column): a rectangle never spans both columns
left = page("one two three", "four five six", x=50)
right = page("seven eight nine", "ten eleven twelve", x=320)
cols = left + right
r = h.line_rects(cols, 4, 9)  # "five six" | "seven eight nine" | "ten"
assert len(r) == 3 and all(rc[2] < 300 or rc[0] > 300 for rc in r), r
# same-height words from different columns are not glued together
r = h.line_rects(page("a b", x=50) + page("c d", x=320), 0, 3)
assert len(r) == 2 and r[0][2] < 100 and r[1][0] == 320, r
# text turned by /Rotate 90: lines run top to bottom, so a "line" is a column of the displayed page
side, ends = [], []
for col, line in enumerate(["alpha beta gamma", "delta epsilon"]):
    y = 50
    for wd in line.split():
        side.append([300 - 20 * col, y, 312 - 20 * col, y + 6 * len(wd), wd])
        y += 6 * len(wd) + 6
    ends.append(y - 6)
assert h.line_rects(side, 0, 4) == [[300, 50, 312, ends[0]], [280, 50, 292, ends[1]]], h.line_rects(side, 0, 4)
# text turned by 180 degrees: words run right to left
rtl = [[430, 100, 490, 112, "a"], [360, 100, 424, 112, "b"], [290, 100, 354, 112, "c"], [430, 120, 490, 132, "d"], [360, 120, 424, 132, "e"]]
assert h.line_rects(rtl, 0, 4) == [[290, 100, 490, 112], [360, 120, 490, 132]], h.line_rects(rtl, 0, 4)
print("ok line rectangles: one per line, column switch starts a new rectangle, sideways (90/270) and right-to-left (180) text")

# ---- the whole path
words = page("Quarterly report.", "Revenue grew strongly in the third quarter because the new", "subscription plan attracted many customers across Europe.", "Costs remained flat.")
chunks = ["Quarterly report. Revenue grew strongly in the third quarter because the new subscription plan attracted many customers across Europe. Costs remained flat."]
hl = h.for_citation("Revenue grew strongly in Q3 thanks to the new subscription plan [p. 4]. Costs were flat [p. 5].", 4, chunks, 612, 792, words)
assert hl["page"] == 4 and (hl["width"], hl["height"]) == (612, 792) and len(hl["rects"]) == 2, hl
box = lambda a, b: [words[a][0], words[a][1], words[b][2], words[b][3]]
assert hl["rects"] == [box(2, 11), box(12, 18)], hl["rects"]  # the Revenue sentence, one rectangle per line
hl5 = h.for_citation("Revenue grew strongly in Q3 thanks to the new subscription plan [p. 4]. Costs were flat [p. 5].", 5, chunks, 612, 792, words)
assert hl5["rects"] == [box(19, 21)], hl5  # the other chip finds "Costs remained flat."
# the chip after the full stop still belongs to the sentence before it
assert h.for_citation("Revenue grew strongly in the third quarter. [p. 4]", 4, chunks, 612, 792, words)
# the page of a chip that is not cited, or an unrelated claim
assert h.for_citation("Revenue grew [p. 4]", 9, chunks, 612, 792, words) is None
assert h.for_citation("Employees enjoy generous parental leave policies [p. 4]", 4, chunks, 612, 792, words) is None
print("ok whole path: right sentence per chip, rectangles per line, chip after the stop, unrelated claim -> null")
print("ALL PASSED")
