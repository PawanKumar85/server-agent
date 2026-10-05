"""Token-efficient RAG: overlap removal and context compression before the LLM."""

from context_compress import compress

OVERVIEW = "\n".join(["Overview of all channels. Channels DOWN right now: tnpnews."] +
                     [f"- ch{i}: OK, fed from Main." for i in range(10)] +
                     ["- tnpnews: DOWN, NO INPUT.",
                      "All servers (status, HTTP latency): a.x UP 100.0 ms; b.x UP 120.0 ms; xcode4.x UP 300.0 ms; cloud.x DOWN 90.0 ms"])
XCODE = "\n".join(["Server xcode4.x is a Transcoding server, status UP."] +
                  [f"Server xcode4.x OUTAGE at 2026-10-0{d} 10:00:00 IST: STALE_MEDIA." for d in range(1, 8)] +
                  ["- tnpnews: DOWN, NO INPUT."])


def docs(*pairs):
    return [{"document": {"id": i, "text": t}, "via": "x"} for i, t in pairs]


def test_normal_items_fold_and_problems_and_named_things_stay():
    out, stats = compress("why is xcode4 slow", docs(("overview", OVERVIEW)), names=["xcode4"])
    text = out[0]["document"]["text"]
    assert "tnpnews: DOWN" in text and "cloud.x DOWN" in text and "xcode4.x UP 300.0 ms" in text
    assert "ch3: OK" not in text and "10 more, all normal" in text and "2 more, all normal" in text
    assert stats["after"] < stats["before"]


def test_repeated_facts_are_given_once_and_old_history_is_trimmed():
    out, _ = compress("is xcode4 ok", docs(("overview", OVERVIEW), ("server:xcode4", XCODE)), names=["xcode4"])
    x = out[1]["document"]["text"]
    assert "tnpnews: DOWN" not in x  # already in the overview
    assert x.count("OUTAGE at") == 3 and "2026-10-07" in x and "2026-10-01" not in x  # the newest three
    hist, _ = compress("when did xcode4 go down in the past", docs(("server:xcode4", XCODE)), names=["xcode4"])
    assert hist[0]["document"]["text"].count("OUTAGE at") >= 7  # a history question keeps the history


def test_asking_for_everything_is_not_cut_and_the_budget_holds():
    out, _ = compress("list all channels", docs(("overview", OVERVIEW)))
    assert "ch3: OK" in out[0]["document"]["text"]
    many = docs(*[(f"d{i}", "\n".join(f"line {i}-{j} xcode4 detail" for j in range(40))) for i in range(30)])
    out, stats = compress("xcode4", many, names=["xcode4"], budget_tokens=500)
    assert stats["after"] <= 500 and sum(len(r["document"]["text"]) for r in out) <= 2000
