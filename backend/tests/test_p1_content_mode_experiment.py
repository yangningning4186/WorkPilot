from eval.p1_content_mode_experiment import _metric_slice


def _item(item_id: str, default: float, content: float) -> dict[str, object]:
    def variant(value: float) -> dict[str, object]:
        return {
            "metrics": {
                "span_recall_at_k": value,
                "gold_doc_recall_at_k": value,
                "ndcg_at_k": value,
                "max_doc_share_at_k": 1 - value,
            }
        }

    return {
        "item_id": item_id,
        "variants": {
            "title_heading_content": variant(default),
            "content": variant(content),
        },
    }


def test_metric_slice_uses_paired_items_and_metric_directions() -> None:
    summary = _metric_slice([_item("a", 0, 1), _item("b", 0, 1)])
    comparison = summary["content_vs_default"]

    assert summary["sample_size"] == 2
    assert comparison["span_recall_at_k"]["delta"] == 1
    assert comparison["span_recall_at_k"]["verdict"] == "improved"
    assert comparison["max_doc_share_at_k"]["delta"] == -1
    assert comparison["max_doc_share_at_k"]["verdict"] == "improved"
