"""Splitting documents into passages: size, overlap, page and heading tracking."""
from app.rag.chunker import chunk_pages
from app.rag.extract import Page


def pages(*texts):
    return [Page(i, t) for i, t in enumerate(texts, start=1)]


def paragraph(word, n=40):
    return " ".join([word] * n) + "."


def test_empty_input_gives_no_chunks():
    assert chunk_pages([]) == [] and chunk_pages([Page(1, "   ")]) == []


def test_a_short_document_is_one_chunk_with_its_page():
    (chunk,) = chunk_pages(pages("A short paragraph about refunds."))
    assert (chunk.ordinal, chunk.page, chunk.content) == (0, 1, "A short paragraph about refunds.")
    assert chunk.tokens > 0


def test_chunks_respect_the_target_size():
    text = "\n\n".join(paragraph(f"w{i}") for i in range(30))  # ~250 chars each
    chunks = chunk_pages(pages(text), target_chars=1000, overlap_chars=100)
    assert len(chunks) > 3
    assert all(len(c.content) <= 1000 + 100 + 2 for c in chunks)  # target plus the carried-over overlap


def test_paragraphs_are_not_cut_in_the_middle():
    text = "\n\n".join(paragraph(f"alpha{i}", 20) for i in range(12))
    for c in chunk_pages(pages(text), target_chars=700, overlap_chars=0):
        for part in c.content.split("\n\n"):
            assert part.endswith(".")  # every piece is a whole paragraph


def test_neighbouring_chunks_overlap():
    text = "\n\n".join(f"Paragraph number {i} " + "filler " * 30 + f"ends here {i}." for i in range(12))
    chunks = chunk_pages(pages(text), target_chars=800, overlap_chars=150)
    for a, b in zip(chunks, chunks[1:]):
        tail = a.content[-60:]
        assert tail.split()[-1] in b.content  # the end of one chunk reappears at the start of the next


def test_no_overlap_means_no_repeated_text():
    text = "\n\n".join(paragraph(f"unique{i}", 30) for i in range(10))
    chunks = chunk_pages(pages(text), target_chars=700, overlap_chars=0)
    seen = [w for c in chunks for w in c.content.split() if w.startswith("unique")]
    assert len(seen) == len(set(f"{w}-{i}" for i, w in enumerate(seen)))  # each paragraph appears exactly once
    for i in range(10):
        assert sum(c.content.count(f"unique{i} ") for c in chunks) >= 1


def test_each_chunk_remembers_the_page_where_it_starts():
    chunks = chunk_pages(pages(paragraph("one", 60), paragraph("two", 60), paragraph("three", 60)), target_chars=500, overlap_chars=0)
    assert [c.page for c in chunks][0] == 1 and [c.page for c in chunks][-1] == 3
    assert sorted(c.page for c in chunks) == [c.page for c in chunks]  # pages never go backwards


def test_the_nearest_heading_travels_with_the_text_below_it():
    text = "# Refunds\n\n" + paragraph("refund", 20) + "\n\n## Shipping\n\n" + paragraph("ship", 20)
    chunks = chunk_pages([Page(None, text)], target_chars=200, overlap_chars=0)
    assert len(chunks) == 2
    assert chunks[0].heading == "Refunds" and "refund refund" in chunks[0].content
    assert chunks[1].heading == "Shipping" and "ship ship" in chunks[1].content  # a chunk takes the heading in force where it starts


def test_a_very_long_paragraph_is_split_at_sentences():
    sentences = " ".join(f"Sentence number {i} says something useful." for i in range(200))
    chunks = chunk_pages([Page(None, sentences)], target_chars=600, overlap_chars=0)
    assert len(chunks) > 5 and all(len(c.content) <= 600 for c in chunks)
    assert all(c.content.rstrip().endswith(".") for c in chunks)  # cut at sentence ends, not mid-sentence


def test_text_with_no_spaces_is_still_split():
    chunks = chunk_pages([Page(None, "x" * 5000)], target_chars=1000, overlap_chars=0)
    assert len(chunks) == 5 and all(len(c.content) <= 1000 for c in chunks)


def test_ordinals_are_contiguous_from_zero():
    text = "\n\n".join(paragraph(f"p{i}", 40) for i in range(25))
    assert [c.ordinal for c in chunk_pages(pages(text), target_chars=900)] == list(range(len(chunk_pages(pages(text), target_chars=900))))


def test_nothing_is_lost_when_overlap_is_off():
    text = "\n\n".join(paragraph(f"token{i}", 25) for i in range(15))
    joined = " ".join(c.content for c in chunk_pages(pages(text), target_chars=800, overlap_chars=0))
    for i in range(15):
        assert f"token{i}" in joined
