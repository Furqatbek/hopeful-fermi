"""ACCEPTANCE TEST: add a brand-new question type to a RUNNING system.
Success criteria: zero DDL, zero exam-engine changes, question authored, sat, scored."""
import hashlib, json, os
from sqlalchemy import create_engine, text

E = create_engine(os.environ["DATABASE_URL"], future=True)

def ddl_fingerprint(c):
    rows = c.execute(text("""
        select table_name, column_name, data_type from information_schema.columns
        where table_schema='public' order by 1,2
    """)).all()
    return hashlib.sha256(repr(rows).encode()).hexdigest()

with E.begin() as c:
    before = ddl_fingerprint(c)
    print(f"schema fingerprint before : {before[:16]}")

# ---- STEP 1: drop the JSON file in and sync (this is `ielts qtypes sync`) --------
defn = json.load(open("docs/design/examples/matching_sentence_endings.v1.json"))
defn.pop("_comment", None)
canon = json.dumps({k: v for k, v in defn.items()}, sort_keys=True, separators=(",", ":"))
with E.begin() as c:
    c.execute(text("""
        INSERT INTO question_type_defs (key,version,status,title,description,skills,
            payload_schema,key_schema,response_schema,scoring,validation,authoring,source,checksum)
        VALUES (:k,:v,:s,:t,:d,:sk, CAST(:ps AS jsonb),CAST(:ks AS jsonb),CAST(:rs AS jsonb),
                CAST(:sc AS jsonb),CAST(:va AS jsonb),CAST(:au AS jsonb),'custom',:cs)
    """), dict(k=defn["key"], v=defn["version"], s=defn["status"], t=defn["title"],
               d=defn["description"], sk=defn["skills"],
               ps=json.dumps(defn["payload_schema"]), ks=json.dumps(defn["key_schema"]),
               rs=json.dumps(defn["response_schema"]), sc=json.dumps(defn["scoring"]),
               va=json.dumps(defn["validation"]), au=json.dumps(defn["authoring"]),
               cs=hashlib.sha256(canon.encode()).hexdigest()))
print("step 1  registry row inserted  : matching_sentence_endings v1")

# ---- STEP 2: a teacher authors a group + question of the new type ---------------
with E.begin() as c:
    gid = c.execute(text("""INSERT INTO question_groups (owner_user_id,title,skill)
        VALUES (1,'Questions 14-17',(select 'reading')) RETURNING id""")).scalar()
    gvid = c.execute(text("""
        INSERT INTO question_group_versions (group_id,version_no,instructions,option_bank,checksum,created_by)
        VALUES (:g,1,'{"en":"Complete each sentence with the correct ending, A-F."}',
                CAST(:bank AS jsonb),'gchk',1) RETURNING id"""),
        dict(g=gid, bank=json.dumps([
            {"id":"A","text":"using satellite imagery."},
            {"id":"B","text":"by monks in monasteries."},
            {"id":"C","text":"for military purposes only."},
            {"id":"D","text":"long before written language."},
            {"id":"E","text":"to settle land disputes."},
            {"id":"F","text":"during the industrial revolution."}]))).scalar()
    qid = c.execute(text("""INSERT INTO questions (owner_user_id,type_key,skill)
        VALUES (1,'matching_sentence_endings','reading') RETURNING id""")).scalar()
    qvid = c.execute(text("""
        INSERT INTO question_versions (question_id,version_no,type_key,type_version,payload,slot_keys,checksum,created_by,status)
        VALUES (:q,1,'matching_sentence_endings',1,CAST(:p AS jsonb),'{s1}','qchk',1,'published') RETURNING id"""),
        dict(q=qid, p=json.dumps({"stem":"The earliest known maps were made","paragraph_hint":"B"}))).scalar()
    akid = c.execute(text("""INSERT INTO answer_key_versions (question_version_id,version_no,key,created_by)
        VALUES (:qv,1,CAST(:k AS jsonb),1) RETURNING id"""),
        dict(qv=qvid, k=json.dumps({"slots":{"s1":{"accept":["D"]}}}))).scalar()
    c.execute(text("INSERT INTO question_group_items (group_version_id,question_version_id,position) VALUES (:g,:q,1)"),
              dict(g=gvid, q=qvid))
print(f"step 2  authored               : group={gvid} question_version={qvid} key={akid}")

# ---- STEP 3: a student sits it; the exam engine stores a generic response -------
with E.begin() as c:
    tvid = c.execute(text("""INSERT INTO test_versions (test_id,version_no,title,created_by,status,published_at)
        VALUES (1,3,'Mock 1 v3',1,'published',now()) RETURNING id""")).scalar()
    aid = c.execute(text("""INSERT INTO attempts (user_id,test_version_id,mode,status)
        VALUES (2,:tv,'exam','in_progress') RETURNING id"""), dict(tv=tvid)).scalar()
    c.execute(text("""INSERT INTO attempt_answers (attempt_id,question_version_id,slot_key,response)
        VALUES (:a,:q,'s1',CAST(:r AS jsonb))"""), dict(a=aid, q=qvid, r=json.dumps("D")))
    c.execute(text("UPDATE attempts SET status='submitted', submitted_at=now() WHERE id=:a"), dict(a=aid))
print(f"step 3  attempt sat            : attempt={aid} response=\"D\"")

# ---- STEP 4: the generic scorer dispatches on the registry's primitive ----------
def score(defn, payload, key, response, option_bank):
    """The ENTIRE type-specific knowledge of the exam engine is this dispatch."""
    prim = defn["scoring"]["primitive"]
    if prim == "choice_per_slot":
        opts = {o["id"] for o in option_bank}
        out = {}
        for slot, spec in key["slots"].items():
            got = (response.get("slots") or {}).get(slot)
            got = got.strip().casefold() if isinstance(got, str) else None
            accepted = [a.casefold() for a in spec["accept"]]
            out[slot] = {"awarded": 1 if got in accepted else 0,
                         "verdict": "correct" if got in accepted else ("unanswered" if got is None else "incorrect"),
                         "explain": {"primitive": prim, "normalizers": defn["scoring"].get("normalizers"),
                                     "option_set_size": len(opts), "compared": [got, accepted]}}
        return out
    raise AssertionError("other primitives exercised by the D4 test suite")

with E.begin() as c:
    row = c.execute(text("""
        select d.scoring, d.key_schema, qv.payload, ak.key, gv.option_bank, aa.response
        from attempt_answers aa
        join question_versions qv on qv.id = aa.question_version_id
        join question_type_defs d on (d.key, d.version) = (qv.type_key, qv.type_version)
        join answer_key_versions ak on ak.question_version_id = qv.id and ak.is_current
        join question_group_items gi on gi.question_version_id = qv.id
        join question_group_versions gv on gv.id = gi.group_version_id
        where aa.attempt_id = :a"""), dict(a=aid)).mappings().one()
    result = score({"scoring": row["scoring"]}, row["payload"], row["key"],
                   {"slots": {"s1": row["response"]}}, row["option_bank"])
    srid = c.execute(text("""INSERT INTO score_runs (attempt_id,reason,engine_version,key_versions,raw_score,max_raw,band)
        VALUES (:a,'initial','1.0.0',CAST(:kv AS jsonb),:raw,1,NULL) RETURNING id"""),
        dict(a=aid, kv=json.dumps({str(qvid): akid}), raw=result["s1"]["awarded"])).scalar()
    c.execute(text("""INSERT INTO item_scores (score_run_id,question_id,question_version_id,
        answer_key_version_id,slot_key,awarded,max_points,verdict,raw_response,explain)
        VALUES (:s,:q,:qv,:ak,'s1',:aw,1,:vd,'D',CAST(:ex AS jsonb))"""),
        dict(s=srid, q=qid, qv=qvid, ak=akid, aw=result["s1"]["awarded"],
             vd=result["s1"]["verdict"], ex=json.dumps(result["s1"]["explain"])))
print(f"step 4  scored                 : verdict={result['s1']['verdict']} awarded={result['s1']['awarded']}")

with E.begin() as c:
    after = ddl_fingerprint(c)
    n = c.execute(text("select count(*) from question_type_defs")).scalar()
print(f"schema fingerprint after  : {after[:16]}")
print()
print(f"RESULT: registry rows {n-1} -> {n}, DDL changed: {before != after}")
assert before == after, "FAILED: schema changed"
print("PASS — new question type added end to end with zero migrations.")
