import json

from app.worker import merge_cost


def test_merge_cost_accumulates_stages():
    a = {"runs": 1, "input": 100, "cost_usd": 0.01, "spent_cny": 0.11, "web_search": 4, "web_extract": 6}
    b = {"runs": 1, "input": 50, "cost_usd": 0.02, "spent_cny": None, "web_search": 1, "web_extract": 6}
    m = merge_cost(json.dumps(a), b)
    assert m["runs"] == 2 and m["input"] == 150 and m["web_search"] == 5 and m["web_extract"] == 12
    assert abs(m["cost_usd"] - 0.03) < 1e-9 and m["stages"] == 2
    assert merge_cost(None, b) == b and merge_cost("坏数据", b) == b
