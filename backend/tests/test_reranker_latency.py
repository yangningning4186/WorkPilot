from eval.reranker_latency import _synthetic_candidates


def test_synthetic_reranker_candidates_match_candidate_window_without_private_data() -> None:
    candidates = _synthetic_candidates(200)

    assert len(candidates) == 200
    assert len({candidate.chunk_id for candidate in candidates}) == 200
    assert all(len(candidate.content) > 1200 for candidate in candidates)
    assert {candidate.source_uri for candidate in candidates} == {"synthetic://reranker-latency"}
