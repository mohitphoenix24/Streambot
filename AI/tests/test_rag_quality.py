"""Retrieval QUALITY: does the right passage come back? A small gold set, measured, not eyeballed.

Each case is a question and a phrase that the correct passage contains. The documents are indexed
through the real pipeline (extract -> chunk -> embed -> pgvector) and searched with the real hybrid
query. Two numbers summarise a run:

  hit@k   the share of questions whose correct passage is among the top k results
  MRR     mean reciprocal rank: 1 for rank 1, 1/2 for rank 2, ... (0 when it is not found)

The embedder here is the test stand-in (hashed words), so this measures the whole PLUMBING (chunking,
the fusion query, scoping) and catches regressions in it. It does not measure how good OpenAI's
embeddings are on your documents: for that, run the same cases with the real embedder.
"""
import pytest

from app.config import Settings
from app.llm.pricing import PriceTable
from app.rag.ingest import DocumentManager
from tests.helpers import FakeEmbedder, fake_vector

DOCUMENTS = {
    "policies.md": """# Refunds

Refunds are issued within five business days of the return. The money goes back to the card used for the purchase.

# Shipping

Standard shipping is free for orders over fifty dollars. Orders to remote islands can take three extra weeks.

# Warranty

Every device has a two year warranty that covers manufacturing defects but not accidental damage.""",
    "handbook.md": """# Remote work

Employees may work remotely up to three days a week with manager approval.

# Expenses

Submit expense reports within thirty days. Receipts are required for any amount over twenty five dollars.

# Security

Report a lost laptop to the security desk immediately. Part code ZX9-QWERTY identifies the replacement hinge.""",
    "faq.md": """# Accounts

To reset a forgotten password use the reset link on the sign in page. Reset links expire after one hour.

# Billing

Invoices are emailed on the first day of every month. Annual plans are billed once a year.""",
}

# (question, a phrase only the correct passage contains)
CASES = [
    ("How long do refunds take?", "five business days"),
    ("Is shipping free?", "free for orders over fifty dollars"),
    ("How long is the warranty?", "two year warranty"),
    ("Can I work from home?", "work remotely up to three days"),
    ("When are expense reports due?", "within thirty days"),
    ("What do I do if I lose my laptop?", "lost laptop to the security desk"),
    ("What is part ZX9-QWERTY?", "replacement hinge"),
    ("I forgot my password", "reset link on the sign in page"),
    ("When are invoices sent?", "first day of every month"),
    ("Do receipts matter for small purchases?", "Receipts are required for any amount over"),
    # Word forms: "refund" vs "Refunds", "stolen" vs "lost" share no exact word with the right passage.
    # Keyword search stems words (refund = refunds), which is where the keyword leg earns its place.
    ("Can I get a refund?", "five business days"),
    ("Is there a guarantee against defects?", "manufacturing defects"),
]


def hit_at_k(ranks: list[int | None], k: int) -> float:
    """The share of questions whose right passage ranked within the top k (rank 1 = best; None = not found)."""
    return sum(1 for r in ranks if r is not None and r <= k) / len(ranks)


def mean_reciprocal_rank(ranks: list[int | None]) -> float:
    return sum(1 / r for r in ranks if r is not None) / len(ranks)


async def index_all(db, settings, uid, cid):
    # Small chunks, so each paragraph is its own passage and ranking is not trivial.
    small = Settings(**{**settings.__dict__, "rag_chunk_chars": 130, "rag_overlap_chars": 0})
    manager = DocumentManager(db, FakeEmbedder(), PriceTable(), small)
    for name, text in DOCUMENTS.items():
        doc = db.add_document(uid, cid, name, "markdown", len(text))
        await manager.run(doc["id"], uid, cid, "markdown", text.encode())


def ranks_for(db, uid, cid, hybrid: bool, limit: int = 5) -> list[int | None]:
    out = []
    for question, phrase in CASES:
        # Vector-only is the same query with no keyword text, so only the meaning-based leg contributes.
        hits = db.search_chunks(uid, cid, fake_vector(question), question if hybrid else "", limit=limit)
        out.append(next((i for i, h in enumerate(hits, start=1) if phrase in h["content"]), None))
    return out


@pytest.fixture
async def indexed(db, settings, uid):
    cid = db.create_conversation(uid)["id"]
    await index_all(db, settings, uid, cid)
    return db, uid, cid


# ── the metrics themselves ───────────────────────────────────────────────
def test_hit_at_k_and_mrr_on_a_known_ranking():
    ranks = [1, 2, None, 5]
    assert hit_at_k(ranks, 1) == 0.25 and hit_at_k(ranks, 3) == 0.5 and hit_at_k(ranks, 5) == 0.75
    assert mean_reciprocal_rank(ranks) == pytest.approx((1 + 0.5 + 0 + 0.2) / 4)


# ── the pipeline's quality ───────────────────────────────────────────────
async def test_the_right_passage_is_in_the_top_three_for_nearly_every_question(indexed):
    db, uid, cid = indexed
    ranks = ranks_for(db, uid, cid, hybrid=True)
    assert hit_at_k(ranks, 3) >= 0.9, ranks
    assert mean_reciprocal_rank(ranks) >= 0.8, ranks


async def test_hybrid_is_never_worse_than_vector_only_and_beats_it_somewhere(indexed):
    db, uid, cid = indexed
    hybrid = ranks_for(db, uid, cid, hybrid=True)
    vector = ranks_for(db, uid, cid, hybrid=False)
    assert mean_reciprocal_rank(hybrid) >= mean_reciprocal_rank(vector), (hybrid, vector)
    # keyword search stems words ("lose" finds "lost"), which the word-hashing stand-in cannot
    assert mean_reciprocal_rank(hybrid) > mean_reciprocal_rank(vector), f"hybrid={hybrid} vector={vector}"


async def test_nothing_from_another_chat_is_ever_returned(db, settings, uid):
    mine = db.create_conversation(uid)["id"]
    theirs = db.create_conversation(uid)["id"]
    await index_all(db, settings, uid, mine)
    assert db.search_chunks(uid, theirs, fake_vector("How long do refunds take?"), "refunds") == []
