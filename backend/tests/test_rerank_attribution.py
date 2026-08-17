from eval.rerank_attribution import classify_attribution


def test_rerank_attribution_statuses_are_mutually_exclusive() -> None:
    assert (
        classify_attribution(
            dense_rank=35,
            lexical_rank=None,
            rrf_rank=None,
            rerank_rank=None,
            final_top_k=10,
        )
        == "rrf_truncated"
    )
    assert (
        classify_attribution(
            dense_rank=None,
            lexical_rank=8,
            rrf_rank=42,
            rerank_rank=31,
            final_top_k=10,
        )
        == "rerank_demoted"
    )
    assert (
        classify_attribution(
            dense_rank=None,
            lexical_rank=None,
            rrf_rank=None,
            rerank_rank=None,
            final_top_k=10,
        )
        == "pool_outside"
    )
    assert (
        classify_attribution(
            dense_rank=2,
            lexical_rank=4,
            rrf_rank=1,
            rerank_rank=3,
            final_top_k=10,
        )
        == "survives_top_k"
    )
