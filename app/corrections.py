"""晚到撤销/补录的整日重算与调整分录。

已复核的原清分账册不被改写：更正另存一版账，受影响卡日整日重算，
未受影响卡日逐行结转，并通过调整分录把原版与终版之差显式列出。
换乘与封顶只在同一卡日内联动，因此按卡日重算与整日重算等价。
"""
import hashlib, json
from decimal import Decimal
from app.config import COLUMNS
from app.errors import DomainError
from app.reasons import sources
from app.values import instant, money
from app.modules.m2_routes.rules import evaluate as routes
from app.modules.m3_fares.rules import evaluate as fares
from app.modules.m4_journeys.rules import evaluate as journeys
from app.modules.m5_discounts.rules import evaluate as discounts
from app.modules.m7_settlement.rules import evaluate as settlement_rule

SNAP_FIELDS=("route","base_fare","reported","transfer_anchor","transfer_discount","cap_discount","expected","day_paid","status")
DELTA_FIELDS=("expected","transfer_discount","cap_discount","day_paid")


def _dec(value):
    return Decimal(str(value if value not in (None,"") else "0"))


def _sum(rows,field):
    return sum((_dec(r.get(field)) for r in rows),Decimal(0))


def canonical(value):
    return json.dumps(value,sort_keys=True,ensure_ascii=False,separators=(",",":"))


def effect_digest(void_ids,added):
    """幂等键只取业务效果：撤销集合与补录内容，同一更正换措辞重试也不二次冲减。"""
    normalized=[]
    for item in added or []:
        row={key:("" if item.get(key) is None else str(item.get(key)).strip()) for key in COLUMNS}
        normalized.append(row)
    normalized.sort(key=canonical)
    return hashlib.sha256(canonical({"void":sorted(void_ids),"add":normalized}).encode()).hexdigest()


def normalize_added(add,params):
    """补录行必须走与正常导入相同的线路/票价/行程规则，任何隔离都整体拒绝。"""
    if add is None: return []
    if not isinstance(add,list): raise DomainError("BAD_CORRECTION","补录内容需要列表")
    raw=[];seen=set()
    for item in add:
        if not isinstance(item,dict): raise DomainError("BAD_CORRECTION","补录行需要对象")
        event=str(item.get("event_id","")).strip()
        if not event: raise DomainError("BAD_CORRECTION","补录行缺少 event_id")
        if event in seen: raise DomainError("BAD_CORRECTION","补录行 event_id 重复")
        seen.add(event)
        row={"_line":f"correction:{event}","_sources":[f"correction:{event}"],"_raw":[]}
        for column in COLUMNS: row[column]="" if item.get(column) is None else str(item.get(column))
        raw.append(row)
    context={"parameters":params,"quarantine":[]}
    stage2=routes(raw,context);stage3=fares(stage2["rows"],context);stage4=journeys(stage3["rows"],context)
    bad=stage2["quarantine"]+stage3["quarantine"]+stage4["quarantine"]
    if bad:
        first=bad[0]
        raise DomainError("BAD_CORRECTION_ROW",f"补录行无法入账：{first.get('reason')} {first.get('detail','')}".strip())
    return stage4["rows"]


def _affected(current,void_ids,added):
    index={row["event_id"]:row for row in current}
    missing=[event for event in void_ids if event not in index]
    if missing: raise DomainError("UNKNOWN_EVENT","撤销事件不存在或已撤销："+",".join(sorted(missing)[:5]))
    void_set=set(void_ids)
    clash=[row["event_id"] for row in added if row["event_id"] in index and row["event_id"] not in void_set]
    if clash: raise DomainError("DUPLICATE_EVENT","补录事件已存在："+",".join(sorted(clash)[:5]))
    occupied={(row["card"],row["at"]):row["event_id"] for row in current if row["event_id"] not in void_set}
    for row in added:
        other=occupied.get((row["card"],row["at"]))
        if other: raise DomainError("SIMULTANEOUS_TAP",f"补录行与事件 {other} 同卡同刻")
    keys=set()
    for event in void_ids:
        row=index[event];keys.add((row["card"],row["business_day"]))
    for row in added: keys.add((row["card"],row["business_day"]))
    return index,keys


def _recompute(current,keys,void_ids,added,params):
    void_set=set(void_ids)
    kept=[row for row in current if (row["card"],row["business_day"]) not in keys]
    day=[row for row in current if (row["card"],row["business_day"]) in keys and row["event_id"] not in void_set]
    recomputed=discounts(day+added,{"parameters":params})["rows"]
    final=kept+recomputed
    final.sort(key=lambda row:(row["card"],instant(row["at"]),row["event_id"]))
    return final


def _finalize(rows):
    output=[]
    for row in rows:
        expected=_dec(row.get("expected"));reported=_dec(row.get("reported"))
        output.append({**row,"difference":str(expected-reported),"status":"通过" if expected==reported else "差异"})
    return output


def _metrics(rows):
    return {"records":len(rows),
            "expected_total":money(_sum(rows,"expected")),
            "reported_total":money(_sum(rows,"reported")),
            "transfer_discount_total":money(_sum(rows,"transfer_discount")),
            "cap_discount_total":money(_sum(rows,"cap_discount")),
            "transfer_hits":sum(1 for row in rows if row.get("transfer_anchor") not in (None,"","—")),
            "differences":sum(1 for row in rows if row.get("status")=="差异")}


def _snapshot(row):
    return {field:row.get(field) for field in SNAP_FIELDS} if row else None


def _reasons(before,after):
    output=[]
    if before.get("transfer_anchor")!=after.get("transfer_anchor"): output.append("TRANSFER_ANCHOR_CHANGED")
    if _dec(before.get("transfer_discount"))!=_dec(after.get("transfer_discount")): output.append("TRANSFER_DISCOUNT_CHANGED")
    if _dec(before.get("cap_discount"))!=_dec(after.get("cap_discount")): output.append("CAP_DISCOUNT_CHANGED")
    if _dec(before.get("expected"))!=_dec(after.get("expected")): output.append("EXPECTED_CHANGED")
    if before.get("day_paid")!=after.get("day_paid"): output.append("DAY_CUMULATIVE_CHANGED")
    return output


def _delta(before,after):
    """差异只按账上口径给出；day_paid 为运行累计，仅在两侧都存在时有意义。"""
    fields=DELTA_FIELDS if before and after else DELTA_FIELDS[:3]
    output={}
    for field in fields:
        old=_dec(before.get(field)) if before else Decimal(0)
        new=_dec(after.get(field)) if after else Decimal(0)
        output[field]=money(new-old)
    return output


def _adjustments(current, final, keys):
    before_index={row["event_id"]:row for row in current if (row["card"],row["business_day"]) in keys}
    after_index={row["event_id"]:row for row in final if (row["card"],row["business_day"]) in keys}
    lines=[]
    for event in sorted(set(before_index)|set(after_index)):
        before=before_index.get(event);after=after_index.get(event)
        if before and not after: kind,reasons="VOIDED",["VOIDED"]
        elif after and not before: kind,reasons="ADDED",["ADDED"]
        else:
            reasons=_reasons(before,after)
            if not reasons: continue
            kind="ADJUST"
        row=after or before
        lines.append({"kind":kind,"event_id":event,"card":row["card"],"business_day":row["business_day"],
                      "before":_snapshot(before),"after":_snapshot(after),
                      "delta":_delta(before,after),"reasons":reasons})
    return lines


def _per_day(current,final,keys):
    output=[]
    for card,day in sorted(keys):
        before=[row for row in current if row["card"]==card and row["business_day"]==day]
        after=[row for row in final if row["card"]==card and row["business_day"]==day]
        before_total=_sum(before,"expected");after_total=_sum(after,"expected")
        output.append({"card":card,"business_day":day,
                       "before":money(before_total),"after":money(after_total),
                       "delta":money(after_total-before_total),
                       "records_before":len(before),"records_after":len(after)})
    return output


def compute(current, void_ids, added, params, original_basis=None):
    """current 为上一版有效账册；added 需已通过 normalize_added。"""
    _,keys=_affected(current,void_ids,added)
    final=_finalize(_recompute(current,keys,void_ids,added,params))
    adjustments=_adjustments(current,final,keys)
    base_metrics=_metrics(current);final_metrics=_metrics(final)
    per_day=_per_day(current,final,keys)
    adjustment_expected=sum((_dec(line["delta"]["expected"]) for line in adjustments),Decimal(0))
    adjustment_cap=sum((_dec(line["delta"]["cap_discount"]) for line in adjustments),Decimal(0))
    adjustment_transfer=sum((_dec(line["delta"]["transfer_discount"]) for line in adjustments),Decimal(0))
    row_conserved=all(_dec(row.get("base_fare"))==_dec(row.get("expected"))+_dec(row.get("transfer_discount"))+_dec(row.get("cap_discount")) for row in final)
    expected_ok=adjustment_expected==_dec(final_metrics["expected_total"])-_dec(base_metrics["expected_total"])
    cap_ok=adjustment_cap==_dec(final_metrics["cap_discount_total"])-_dec(base_metrics["cap_discount_total"])
    transfer_ok=adjustment_transfer==_dec(final_metrics["transfer_discount_total"])-_dec(base_metrics["transfer_discount_total"])
    day_ok=sum((_dec(item["delta"]) for item in per_day),Decimal(0))==adjustment_expected
    balanced=bool(row_conserved and expected_ok and cap_ok and transfer_ok and day_ok)
    replaced=[row for row in current if (row["card"],row["business_day"]) in keys]
    basis=[row for row in (original_basis if original_basis is not None else current) if (row["card"],row["business_day"]) in keys]
    delta={key:(final_metrics[key]-base_metrics[key]) if isinstance(base_metrics[key],int) else money(_dec(final_metrics[key])-_dec(base_metrics[key])) for key in ("transfer_hits","differences","expected_total","reported_total","cap_discount_total","transfer_discount_total")}
    delta["records"]=final_metrics["records"]-base_metrics["records"]
    return {"affected_keys":[f"{card}|{day}" for card,day in sorted(keys)],
            "replaced_rows":replaced,"original_basis":basis,"final_rows":final,
            "adjustments":adjustments,
            "metrics":{"base":base_metrics,"final":final_metrics,"delta":delta,
                       "adjustment_expected":money(adjustment_expected),
                       "adjustment_cap":money(adjustment_cap),
                       "adjustment_transfer":money(adjustment_transfer),
                       "per_day":per_day,"balanced":balanced,
                       "checks":{"row_conserved":row_conserved,"expected_ok":expected_ok,
                                 "cap_ok":cap_ok,"transfer_ok":transfer_ok,"day_ok":day_ok}}}


def effective_rows(run,corrections):
    """终版账册取最后一笔已复核更正；待复核不改账，退回则回到上一版。"""
    approved=[item for item in corrections if item["decision"]=="已复核"]
    return approved[-1]["final_rows"] if approved else run["stages"][5]["rows"]


def blocking_correction(corrections):
    """只有末笔更正未复核时才阻断日结；更早的已复核或已退回单视为历史。"""
    if not corrections: return None
    latest=corrections[-1]
    return latest if latest["decision"]!="已复核" else None


def settle(run,corrections):
    """唯一的日终结果：换乘命中、封顶减免、应收与差异取自同一版账。"""
    book=effective_rows(run,corrections)
    metrics=_metrics(book)
    approved=[item for item in corrections if item["decision"]=="已复核"]
    blocker=blocking_correction(corrections)
    if not approved:
        ready=bool(run["stages"][6]["metrics"]["ready"]) and blocker is None
        blocking=[{"code":"CORRECTION_PENDING" if blocker["decision"]=="待复核" else "CORRECTION_REJECTED",
                   "message":"存在未复核的更正，不能归档" if blocker["decision"]=="待复核" else "末笔更正已退回，需重新登记并复核"}] if blocker else []
        return {"rows":book,"metrics":{**metrics,"source_lines":run["stages"][6]["metrics"]["source_lines"],
                                       "sha256":run["stages"][6]["metrics"]["sha256"],
                                       "ready":ready},
                "blocking":blocking}
    context={"corrections":[{"correction_id":item["correction_id"],"decision":item["decision"],
                             "balanced":item["metrics"]["balanced"]} for item in approved],
             "blocked":blocker is not None,
             "reconcile":{"metrics":run["stages"][5]["metrics"]}}
    try:
        result=settlement_rule(book,context)
        ready=True;blocking=[];sha=result["metrics"]["sha256"];source_lines=result["metrics"]["source_lines"]
    except DomainError as error:
        ready=False;blocking=[{"code":error.code,"message":error.message}]
        sha=hashlib.sha256(json.dumps(book,ensure_ascii=False,sort_keys=True,default=str).encode()).hexdigest()
        source_lines=len(set(source for row in book for source in sources(row)))
    return {"rows":book,"metrics":{**metrics,"source_lines":source_lines,"sha256":sha,"ready":ready},"blocking":blocking}


def summarize(correction):
    """列表视图：保留裁决与调整分录，原清分依据走详情接口/stages。"""
    return {key:value for key,value in correction.items() if key not in {"final_rows","original_basis","replaced_rows"}}


def decorate(run,corrections):
    """为已存批次附加更正摘要与日终放行状态，原 stages 保持只读。"""
    run["corrections"]=[summarize(item) for item in corrections]
    run["_corrections"]=corrections
    result=settle(run,corrections)
    approved=[item for item in corrections if item["decision"]=="已复核"]
    pending=sum(1 for item in corrections if item["decision"]!="已复核")
    affected=sorted({key for correction in approved for key in correction.get("affected_keys",[])}) if approved else []
    run["settlement"]={**result["metrics"],"ready":result["metrics"]["ready"],"blocking":result["blocking"],
                       "corrections":len(corrections),"approved":len(approved),"pending":pending,"affected":affected,
                       "correction_version":approved[-1]["correction_version"] if approved else 0,
                       "latest_correction":corrections[-1]["correction_id"] if corrections else None}
    return run


def book_for(run):
    """日终唯一结果：有更正取末版重算行，否则取原清分行；两者共用同一输出字段。"""
    return effective_rows(run,run.get("_corrections",[]))
