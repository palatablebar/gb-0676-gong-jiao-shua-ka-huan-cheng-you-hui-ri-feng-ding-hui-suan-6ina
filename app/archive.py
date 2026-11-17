import csv,io,json
from app import corrections as correction_rules
def build_archive(run):
    stream=io.StringIO(newline="");writer=csv.writer(stream)
    writer.writerow(["run_id","input_sha256","rule_version","kind","source_lines","reason","evidence"])
    meta=run["manifest"]
    settlement=run.get("settlement") or {}
    book=correction_rules.effective_rows(run,run.get("_corrections",[]))
    for row in book:
        writer.writerow([run["run_id"],meta["input_sha256"],meta["rule_version"],"accepted",json.dumps(row.get("_sources",[])),row.get("status",""),json.dumps(row,ensure_ascii=False)])
    for row in run["stages"][-1].get("cumulative_quarantine",[]):
        writer.writerow([run["run_id"],meta["input_sha256"],meta["rule_version"],"quarantine",json.dumps(row.get("_sources",[])),row.get("reason",""),json.dumps(row,ensure_ascii=False)])
    for correction in run.get("corrections",[]):
        writer.writerow([run["run_id"],meta["input_sha256"],meta["rule_version"],"correction",
                         json.dumps(correction.get("void",[]),ensure_ascii=False),correction.get("reason",""),
                         json.dumps({k:v for k,v in correction.items() if k!="final_rows"},ensure_ascii=False)])
        for line in correction.get("adjustments",[]):
            writer.writerow([run["run_id"],meta["input_sha256"],meta["rule_version"],"adjustment:"+line["kind"],
                             json.dumps([line["event_id"]],ensure_ascii=False),",".join(line.get("reasons",[])),
                             json.dumps(line,ensure_ascii=False)])
    writer.writerow([run["run_id"],meta["input_sha256"],meta["rule_version"],"settlement",
                     json.dumps(sorted(settlement.get("affected",[])),ensure_ascii=False),
                     "READY" if settlement.get("ready") else "BLOCKED",
                     json.dumps({k:v for k,v in settlement.items() if k!="blocking"},ensure_ascii=False)])
    return "﻿"+stream.getvalue()
