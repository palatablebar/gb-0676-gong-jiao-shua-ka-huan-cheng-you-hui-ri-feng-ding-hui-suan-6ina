import csv,io
from decimal import Decimal

def book(run): return run["stages"][5]["rows"]

def test_original_clearing_is_immutable(client):
    p=client.post("/api/runs",json={"sample":"clean.csv"}).get_json();u="/api/runs/"+p["run_id"]
    before=client.get(u).get_json()["stages"][5]["rows"]
    client.post(u+"/corrections",json={"version":0,"void":["0011"],"reason":"晚到撤销"})
    after=client.get(u).get_json()["stages"][5]["rows"]
    assert after==before

def test_rejected_correction_blocks_until_replacement(client):
    p=client.post("/api/runs",json={"sample":"clean.csv"}).get_json();u="/api/runs/"+p["run_id"]
    detail=client.post(u+"/corrections",json={"version":0,"void":["0011"],"reason":"晚到撤销"}).get_json()
    cid=detail["correction_id"]
    assert client.post(u+f"/corrections/{cid}/review",json={"version":1,"decision":"退回复核"}).status_code==200
    run=client.get(u).get_json()
    assert run["settlement"]["ready"] is False
    assert run["settlement"]["records"]==32  # 退回的更正不落账
    assert client.get(u+"/archive").status_code==409
    assert client.post(u+f"/corrections/{cid}/review",json={"version":2,"decision":"已复核"}).status_code==409
    replacement=client.post(u+"/corrections",json={"version":1,"void":["0011"],"reason":"重报撤销"}).get_json()
    assert replacement["metrics"]["final"]["records"]==31
    client.post(u+f"/corrections/{replacement['correction_id']}/review",json={"version":1,"decision":"已复核"})
    run=client.get(u).get_json()
    assert run["settlement"]["ready"] is True and run["settlement"]["records"]==31
    assert client.get(u+"/archive").status_code==200

def test_correction_review_unknown_404(client):
    p=client.post("/api/runs",json={"sample":"clean.csv"}).get_json()
    assert client.post("/api/runs/"+p["run_id"]+"/corrections/nope/review",json={"version":1,"decision":"已复核"}).status_code==404
    assert client.get("/api/runs/"+p["run_id"]+"/corrections/nope").status_code==404

def test_correction_recomputes_whole_card_day(client):
    p=client.post("/api/runs",json={"sample":"clean.csv"}).get_json();u="/api/runs/"+p["run_id"]
    detail=client.post(u+"/corrections",json={"version":0,"void":["0011"],"reason":"晚到撤销"}).get_json()
    final={row["event_id"]:row for row in detail["final_rows"] if row["card"]=="0001"}
    assert "0011" not in final
    assert final["0012"]["transfer_anchor"]=="0010"
    assert final["0013"]["cap_discount"]=="0.50" and final["0013"]["expected"]=="2.00"
    assert detail["affected_keys"]==["0001|2026-11-16"]
    assert all(row["card"]!="0002" or row["expected"]==next(x["expected"] for x in book(client.get(u).get_json()) if x["event_id"]==row["event_id"]) for row in detail["final_rows"] if row["card"]=="0002")

def test_adjustments_bridge_original_and_final(client):
    p=client.post("/api/runs",json={"sample":"clean.csv"}).get_json();u="/api/runs/"+p["run_id"]
    detail=client.post(u+"/corrections",json={"version":0,"void":["0011"],"reason":"晚到撤销"}).get_json()
    assert detail["metrics"]["balanced"] is True
    assert all(detail["metrics"]["checks"].values())
    kinds={line["event_id"]:line for line in detail["adjustments"]}
    assert kinds["0011"]["kind"]=="VOIDED" and kinds["0011"]["delta"]["expected"]=="-1.50"
    assert kinds["0012"]["kind"]=="ADJUST" and "TRANSFER_ANCHOR_CHANGED" in kinds["0012"]["reasons"]
    assert kinds["0013"]["delta"]["expected"]=="1.50"
    expected=Decimal(detail["metrics"]["base"]["expected_total"])+sum(Decimal(x["delta"]["expected"]) for x in detail["adjustments"])
    assert expected==Decimal(detail["metrics"]["final"]["expected_total"])

def test_final_book_is_single_ledger(client):
    p=client.post("/api/runs",json={"sample":"clean.csv"}).get_json();u="/api/runs/"+p["run_id"]
    detail=client.post(u+"/corrections",json={"version":0,"void":["0011"],"reason":"晚到撤销"}).get_json()
    pending=client.get(u).get_json()["settlement"]
    assert pending["ready"] is False and pending["records"]==32
    client.post(u+f"/corrections/{detail['correction_id']}/review",json={"version":1,"decision":"已复核"})
    run=client.get(u).get_json();final=run["settlement"]
    assert final["records"]==len(detail["final_rows"])
    assert final["expected_total"]==str(sum(Decimal(r["expected"]) for r in detail["final_rows"]))
    assert final["transfer_hits"]==sum(1 for r in detail["final_rows"] if r["transfer_anchor"] not in ("—","",None))
    assert final["cap_discount_total"]==str(sum(Decimal(r["cap_discount"]) for r in detail["final_rows"]))

def test_repeat_correction_does_not_double_post(client):
    p=client.post("/api/runs",json={"sample":"clean.csv"}).get_json();u="/api/runs/"+p["run_id"]
    body={"version":0,"void":["0011"],"reason":"晚到撤销"}
    first=client.post(u+"/corrections",json=body).get_json()
    second=client.post(u+"/corrections",json=body).get_json()
    assert second["correction_id"]==first["correction_id"] and second["replayed"] is True
    run=client.get(u).get_json()
    assert len(run["corrections"])==1
    assert run["settlement"]["expected_total"]==first["metrics"]["final"]["expected_total"]

def test_stale_correction_rejected(client):
    p=client.post("/api/runs",json={"sample":"clean.csv"}).get_json();u="/api/runs/"+p["run_id"]
    first=client.post(u+"/corrections",json={"version":0,"void":["0011"],"reason":"晚到撤销"}).get_json()
    assert client.post(u+"/corrections",json={"version":0,"void":["0012"],"reason":"第二笔"}).status_code==409
    client.post(u+f"/corrections/{first['correction_id']}/review",json={"version":1,"decision":"已复核"})
    assert client.post(u+"/corrections",json={"version":1,"void":["0012"],"reason":"第二笔"}).status_code==200

def test_unreviewed_correction_blocks_archive(client):
    p=client.post("/api/runs",json={"sample":"clean.csv"}).get_json();u="/api/runs/"+p["run_id"]
    detail=client.post(u+"/corrections",json={"version":0,"void":["0011"],"reason":"晚到撤销"}).get_json()
    assert client.get(u+"/archive").status_code==409
    run=client.get(u).get_json();assert run["settlement"]["ready"] is False
    client.post(u+f"/corrections/{detail['correction_id']}/review",json={"version":1,"decision":"已复核"})
    assert client.get(u+"/archive").status_code==200

def test_archive_contains_correction_evidence(client):
    p=client.post("/api/runs",json={"sample":"clean.csv"}).get_json();u="/api/runs/"+p["run_id"]
    detail=client.post(u+"/corrections",json={"version":0,"void":["0011"],"reason":"晚到撤销"}).get_json()
    client.post(u+f"/corrections/{detail['correction_id']}/review",json={"version":1,"decision":"已复核"})
    rows=list(csv.reader(io.StringIO(client.get(u+"/archive").data.decode("utf-8-sig"))))
    kinds={row[3] for row in rows[1:]}
    assert {"correction","adjustment:VOIDED","adjustment:ADJUST","settlement"}<=kinds
    correction=[row for row in rows if row[3]=="correction"][0]
    assert "晚到撤销" in correction[5]

def test_added_row_goes_through_same_rules(client):
    p=client.post("/api/runs",json={"sample":"clean.csv"}).get_json();u="/api/runs/"+p["run_id"]
    added=[{"event_id":"0099","card":"0009","route":"1路","event_time":"2026-11-16T08:05:00+08:00","result":"success","reported":"2.50"}]
    detail=client.post(u+"/corrections",json={"version":0,"void":[],"add":added,"reason":"补录漏刷"}).get_json()
    row=[x for x in detail["final_rows"] if x["event_id"]=="0099"][0]
    assert row["route"]=="01" and row["fare_version"]=="F2" and row["expected"]=="2.50"

def test_bad_added_row_rejected(client):
    p=client.post("/api/runs",json={"sample":"clean.csv"}).get_json()
    bad=[{"event_id":"0099","card":"0009","route":"99","event_time":"2026-11-16T08:05:00+08:00","result":"success","reported":"2.50"}]
    assert client.post("/api/runs/"+p["run_id"]+"/corrections",json={"version":0,"void":[],"add":bad,"reason":"补录"}).status_code==422

def test_unknown_void_and_missing_reason(client):
    p=client.post("/api/runs",json={"sample":"clean.csv"}).get_json();u="/api/runs/"+p["run_id"]
    assert client.post(u+"/corrections",json={"version":0,"void":["zzz"],"reason":"x"}).status_code==422
    assert client.post(u+"/corrections",json={"version":0,"void":["0011"],"reason":""}).status_code==422
    assert client.post(u+"/corrections",json={"version":0,"void":[]}).status_code==422

def test_transfer_cap_conservation_after_correction(client):
    p=client.post("/api/runs",json={"sample":"clean.csv"}).get_json();u="/api/runs/"+p["run_id"]
    detail=client.post(u+"/corrections",json={"version":0,"void":["0010"],"reason":"首乘撤销"}).get_json()
    for row in detail["final_rows"]:
        assert Decimal(row["base_fare"])==Decimal(row["expected"])+Decimal(row["transfer_discount"])+Decimal(row["cap_discount"])
    assert detail["metrics"]["balanced"] is True

def test_settlement_page_explains_changes(client):
    p=client.post("/api/runs",json={"sample":"clean.csv"}).get_json();u="/api/runs/"+p["run_id"]
    client.post(u+"/corrections",json={"version":0,"void":["0011"],"reason":"晚到撤销"})
    page=client.get("/settlement").data.decode()
    for text in ["未放行","受影响行程为何变化","换乘锚点变化","封顶减免变化","卡日前后对照","原清分依据"]:
        assert text in page

def test_correction_chains_on_latest_book(client):
    p=client.post("/api/runs",json={"sample":"clean.csv"}).get_json();u="/api/runs/"+p["run_id"]
    first=client.post(u+"/corrections",json={"version":0,"void":["0011"],"reason":"第一笔"}).get_json()
    assert client.post(u+"/corrections",json={"version":1,"void":["0012"],"reason":"第二笔"}).status_code==409
    client.post(u+f"/corrections/{first['correction_id']}/review",json={"version":1,"decision":"已复核"})
    second=client.post(u+"/corrections",json={"version":1,"void":["0012"],"reason":"第二笔"}).get_json()
    ids=[r["event_id"] for r in second["final_rows"] if r["card"]=="0001"]
    assert ids==["0010","0013"]
    assert second["metrics"]["base"]["records"]==31
