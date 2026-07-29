\set ON_ERROR_STOP off
\pset format unaligned
\pset tuples_only on

-- Every SAVEPOINT below needs an explicit transaction. Without this BEGIN psql
-- runs in autocommit, `SAVEPOINT` raises "can only be used in transaction
-- blocks", and the script only appeared to work because each failing statement
-- was its own transaction that rolled itself back. It emitted nine spurious
-- errors of its own, which is why the output could never be machine-checked.
BEGIN;

-- ---------- fixtures ----------
INSERT INTO users (phone, given_name, date_of_birth) VALUES ('+998901234567','Aziza','2010-03-01');
INSERT INTO users (phone, given_name, date_of_birth) VALUES ('+998901234568','Bekzod','1999-06-15');
INSERT INTO organizations (name, slug, status) VALUES ('Tashkent Prep','tashkent-prep','active');

SELECT '1. generated adult_at  -> ' || string_agg(given_name || '=' || adult_at::text || '/minor=' ||
       (adult_at > current_date)::text, ' ') FROM users;

-- ---------- audit log is append-only ----------
INSERT INTO audit_log (actor_kind, action, subject_type, subject_id)
VALUES ('user','test.publish','test_version','1');
SELECT '2. audit rows          -> ' || count(*)::text FROM audit_log;
SAVEPOINT s1;
UPDATE audit_log SET action = 'tampered';
ROLLBACK TO s1;
DELETE FROM audit_log;
ROLLBACK TO s1;

-- ---------- one current answer key per question version ----------
INSERT INTO passages (owner_user_id, title) VALUES (1,'Cartography');
INSERT INTO passage_versions (passage_id, version_no, title, blocks, paragraph_labels, checksum, created_by)
VALUES (1,1,'Cartography','[]','{A,B,C,D}','abc',1);
INSERT INTO questions (owner_user_id, type_key, skill) VALUES (1,'mcq_single','reading');
INSERT INTO question_versions (question_id, version_no, type_key, type_version, payload, slot_keys, checksum, created_by)
VALUES (1,1,'mcq_single',1,'{"stem":"x","options":[{"id":"A","text":"a"},{"id":"B","text":"b"}]}','{s1}','h1',1);
INSERT INTO answer_key_versions (question_version_id, version_no, key, created_by)
VALUES (1,1,'{"slots":{"s1":{"accept":["A"]}}}',1);
SAVEPOINT s2;
INSERT INTO answer_key_versions (question_version_id, version_no, key, created_by, is_current)
VALUES (1,2,'{"slots":{"s1":{"accept":["B"]}}}',1,true);
ROLLBACK TO s2;

-- correct key-fix flow: supersede, then insert
UPDATE answer_key_versions SET is_current=false, superseded_at=now() WHERE id=1;
INSERT INTO answer_key_versions (question_version_id, version_no, key, created_by, reason)
VALUES (1,2,'{"slots":{"s1":{"accept":["B"]}}}',1,'key_fix');
SELECT '3. key versions        -> total=' || count(*)::text || ' current=' ||
       count(*) FILTER (WHERE is_current)::text FROM answer_key_versions;

-- ---------- registry FK rejects an unknown type ----------
SAVEPOINT s3;
INSERT INTO question_versions (question_id, version_no, type_key, type_version, payload, slot_keys, checksum, created_by)
VALUES (1,99,'not_a_real_type',1,'{}','{s1}','h9',1);
ROLLBACK TO s3;

-- ---------- published test versions are immutable ----------
INSERT INTO tests (owner_user_id, title, skills) VALUES (1,'Mock 1','{reading}');
INSERT INTO test_versions (test_id, version_no, title, created_by, status, published_at)
VALUES (1,1,'Mock 1 v1',1,'published',now());
SAVEPOINT s4;
UPDATE test_versions SET title='Mock 1 EDITED' WHERE id=1;
ROLLBACK TO s4;
SAVEPOINT s5;
UPDATE test_versions SET status='draft' WHERE id=1;
ROLLBACK TO s5;
UPDATE test_versions SET status='archived', archived_at=now() WHERE id=1;
SELECT '4. published tv status -> ' || status FROM test_versions WHERE id=1;

-- ---------- answers freeze at submit ----------
INSERT INTO test_versions (test_id, version_no, title, created_by, status, published_at)
VALUES (1,2,'Mock 1 v2',1,'published',now());
INSERT INTO attempts (user_id, test_version_id, mode, status) VALUES (2,2,'exam','in_progress');
INSERT INTO attempt_answers (attempt_id, question_version_id, slot_key, response)
VALUES (1,1,'s1','"A"');
UPDATE attempts SET status='submitted', submitted_at=now() WHERE id=1;
SAVEPOINT s6;
UPDATE attempt_answers SET response='"B"' WHERE attempt_id=1;
ROLLBACK TO s6;
SELECT '5. frozen answer       -> ' || response::text FROM attempt_answers WHERE attempt_id=1;

-- ---------- one current score run per attempt ----------
INSERT INTO score_runs (attempt_id, reason, engine_version, key_versions, raw_score, max_raw, band)
VALUES (1,'initial','1.0.0','{"1":1}',30,40,7.0);
SAVEPOINT s7;
INSERT INTO score_runs (attempt_id, reason, engine_version, key_versions, raw_score, max_raw, band)
VALUES (1,'regrade_key','1.0.0','{"1":2}',31,40,7.0);
ROLLBACK TO s7;
UPDATE score_runs SET is_current=false WHERE attempt_id=1;
INSERT INTO score_runs (attempt_id, reason, engine_version, key_versions, raw_score, max_raw, band)
VALUES (1,'regrade_key','1.0.0','{"1":2}',31,40,7.0);
SELECT '6. score runs          -> total=' || count(*)::text || ' current_raw=' ||
       max(raw_score) FILTER (WHERE is_current)::text FROM score_runs WHERE attempt_id=1;

-- ---------- org isolation default ----------
SELECT '7. default visibility  -> passages=' || (SELECT visibility FROM passages LIMIT 1) ||
       ' tests=' || (SELECT visibility FROM tests LIMIT 1);

-- COMMIT, not ROLLBACK: `acceptance_new_question_type.py` builds on these
-- fixtures and assumes their ids, which is why the README says to run it second.
COMMIT;
