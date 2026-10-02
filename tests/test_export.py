import io
import pandas as pd
import pytest

from export import parse_excel_workbook


def test_parse_wide_channels():
    df = pd.DataFrame([
        {
            "Channel": "news1",
            "Main Link": "https://main1.example.com/live.m3u8",
            "Backup Link": "https://backup1.example.com/live.m3u8",
            "Transcoding Link": "https://trans1.example.com/live.m3u8",
            "Final Link": "https://edge1.example.com/news1/index.m3u8",
        },
        {
            "Channel": "sports",
            "Main Link": "https://main2.example.com/live.m3u8",
            "Backup Link": "-",
            "Transcoding Link": "https://trans2.example.com/live.m3u8",
            "Final Link": "https://edge2.example.com/sports/index.m3u8",
        },
    ])
    buf = io.BytesIO()
    df.to_excel(buf, sheet_name="Channels", index=False)
    buf.seek(0)

    res = parse_excel_workbook(buf.getvalue())
    assert res["sheets_found"] == ["Channels"]
    assert sorted(res["channels"]) == ["news1", "sports"]
    assert len(res["links"]) == 7  # 4 for news1, 3 for sports
    assert len(res["auto_relationships"]) > 0


def test_parse_row_links_and_relationships():
    links_df = pd.DataFrame([
        {"Channel": "ch1", "Role": "MainInput", "URL": "https://m.example.com/ch1.m3u8"},
        {"Channel": "ch1", "Role": "Transcoding", "URL": "https://t.example.com/ch1.m3u8"},
        {"Channel": "ch1", "Role": "FinalLink", "URL": "https://f.example.com/ch1/index.m3u8"},
    ])
    rels_df = pd.DataFrame([
        {"source": "m.example.com", "type": "FEEDS", "target": "t.example.com"},
        {"source": "t.example.com", "type": "PRODUCES", "target": "f.example.com/ch1"},
    ])

    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        links_df.to_excel(writer, sheet_name="Links", index=False)
        rels_df.to_excel(writer, sheet_name="Relationships", index=False)
    buf.seek(0)

    res = parse_excel_workbook(buf.getvalue())
    assert len(res["links"]) == 3
    assert len(res["relationships"]) == 2
    assert res["relationships"][0].source == "m.example.com"
    assert res["relationships"][1].type == "PRODUCES"
