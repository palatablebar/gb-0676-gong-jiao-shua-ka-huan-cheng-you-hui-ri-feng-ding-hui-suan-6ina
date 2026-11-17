import sqlite3,json,hashlib
from datetime import datetime,timezone
from app.config import DATA_DIR
from app.errors import DomainError
from app import corrections


def connect():
    DATA_DIR.mkdir(parents=True,exist_ok=True)
    db=sqlite3.connect(DATA_DIR/"records.db",timeout=20)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY,payload TEXT NOT NULL,version INTEGER NOT NULL DEFAULT 1,decision TEXT NOT NULL DEFAULT '待复核')")
    db.execute("CREATE TABLE IF NOT EXISTS corrections (correction_id TEXT PRIMARY KEY,run_id TEXT NOT NULL,input_digest TEXT NOT NULL,payload TEXT NOT NULL,version INTEGER NOT NULL DEFAULT 1,decision TEXT NOT NULL DEFAULT '待复核',created_at TEXT NOT NULL)")
    # 退回的更正不参与幂等：允许同效果重新登记；去重在写事务内判定。
    db.execute("DROP INDEX IF EXISTS corrections_dedupe")
    return db


def save(run_id,payload):
    with connect() as db:
        db.execute("INSERT OR IGNORE INTO runs(id,payload) VALUES (?,?)",(run_id,json.dumps(payload,ensure_ascii=False)))
    return get(run_id)


def _raw(db,run_id):
    row=db.execute("SELECT payload,version,decision FROM runs WHERE id=?",(run_id,)).fetchone()
    if not row: raise DomainError("NOT_FOUND","批次不存在",404)
    return {**json.loads(row[0]),"version":row[1],"decision":row[2]}


def _correction_rows(db,run_id):
    rows=db.execute("SELECT payload,version,decision FROM corrections WHERE run_id=? ORDER BY rowid",(run_id,)).fetchall()
    return [{**json.loads(payload),"correction_version":version,"decision":decision} for payload,version,decision in rows]


def _correction(db,run_id,correction_id):
    row=db.execute("SELECT payload,version,decision FROM corrections WHERE correction_id=? AND run_id=?",(correction_id,run_id)).fetchone()
    if not row: raise DomainError("NOT_FOUND","更正不存在",404)
    return {**json.loads(row[0]),"correction_version":row[1],"decision":row[2]}


def _public(run):
    """批次视图不携带整册重算行，逐条依据与调整走更正详情接口。"""
    return {key:value for key,value in run.items() if key!="_corrections"}


def get(run_id,include_rows=False):
    with connect() as db:
        run=corrections.decorate(_raw(db,run_id),_correction_rows(db,run_id))
    return run if include_rows else _public(run)


def latest():
    with connect() as db:
        row=db.execute("SELECT id FROM runs ORDER BY rowid DESC LIMIT 1").fetchone()
    return get(row[0]) if row else None


def history():
    with connect() as db:
        rows=db.execute("SELECT id,version,decision FROM runs ORDER BY rowid DESC").fetchall()
    output=[]
    for run_id,version,decision in rows:
        try: ready=get(run_id)["settlement"]["ready"]
        except DomainError: ready=False
        output.append({"run_id":run_id,"version":version,"decision":decision,"settlement_ready":ready})
    return output


def review(run_id,version,decision):
    if decision not in {"已复核","退回复核"}: raise DomainError("BAD_DECISION","复核状态无效")
    with connect() as db:
        cursor=db.execute("UPDATE runs SET version=version+1,decision=? WHERE id=? AND version=?",(decision,run_id,version))
        if cursor.rowcount!=1:
            if not db.execute("SELECT 1 FROM runs WHERE id=?",(run_id,)).fetchone(): raise DomainError("NOT_FOUND","批次不存在",404)
            raise DomainError("STALE_VERSION","版本已更新，请刷新",409)
    return get(run_id)


def correct(run_id,base_version,void,added,reason,params):
    void=sorted({str(item).strip() for item in void if str(item).strip()})
    if not void and not added: raise DomainError("EMPTY_CORRECTION","更正需要撤销或补录内容")
    if not reason or not str(reason).strip(): raise DomainError("MISSING_REASON","更正需要留下依据")
    reason=str(reason).strip()
    normalized=corrections.normalize_added(added,params or {})
    # 幂等键只取业务效果：同一更正重试（即使换了措辞）也不会二次冲减。
    digest=corrections.effect_digest(void,added)
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        run=_raw(db,run_id);existing=_correction_rows(db,run_id)
        row=db.execute("SELECT correction_id FROM corrections WHERE run_id=? AND input_digest=? AND decision!='退回复核'",(run_id,digest)).fetchone()
        if row:
            detail=_correction(db,run_id,row[0]);db.rollback()
            return {**detail,"replayed":True}
        if base_version is not None and base_version!=len(existing):
            db.rollback();raise DomainError("STALE_VERSION","批次账册已更新，请刷新",409)
        if existing and existing[-1]["decision"]=="待复核":
            db.rollback();raise DomainError("CORRECTION_PENDING","上一笔更正尚未复核，不能叠加新更正",409)
        base=corrections.effective_rows(run,existing)
        result=corrections.compute(base,void,normalized,params or {},original_basis=run["stages"][5]["rows"])
        correction_id=hashlib.sha256(f"{run_id}:{digest}:{len(existing)+1}".encode()).hexdigest()[:20]
        payload={"correction_id":correction_id,"run_id":run_id,"input_digest":digest,
                 "base_version":len(existing),"void":void,"add":added,"reason":reason,
                 "created_at":datetime.now(timezone.utc).isoformat(),**result}
        db.execute("INSERT INTO corrections(correction_id,run_id,input_digest,payload,created_at) VALUES (?,?,?,?,?)",
                   (correction_id,run_id,digest,json.dumps(payload,ensure_ascii=False),payload["created_at"]))
        detail=_correction(db,run_id,correction_id)
    return {**detail,"replayed":False}


def review_correction(run_id,correction_id,version,decision):
    if decision not in {"已复核","退回复核"}: raise DomainError("BAD_DECISION","复核状态无效")
    with connect() as db:
        row=db.execute("SELECT version,decision FROM corrections WHERE correction_id=? AND run_id=?",(correction_id,run_id)).fetchone()
        if not row: raise DomainError("NOT_FOUND","更正不存在",404)
        # 更正内容不可变，已裁决的单据保持封闭；退回后需重新登记并复核。
        if row[1]!="待复核": raise DomainError("CORRECTION_CLOSED","该更正已裁决，不能再次复核",409)
        cursor=db.execute("UPDATE corrections SET version=version+1,decision=? WHERE correction_id=? AND run_id=? AND version=?",(decision,correction_id,run_id,version))
        if cursor.rowcount!=1: raise DomainError("STALE_VERSION","版本已更新，请刷新",409)
        detail=_correction(db,run_id,correction_id)
    return detail


def correction_detail(run_id,correction_id):
    with connect() as db: return _correction(db,run_id,correction_id)
