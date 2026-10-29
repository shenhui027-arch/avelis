-- DDL 审计（ADR 0008）：事件触发器把每条 DDL 的执行用户、对象与语句写入 avelis_audit.ddl_event。
-- Avelis 的采集账号只读这张表，用来把采集发现的结构变化归因到数据库用户。
-- 需要超级用户执行（事件触发器只能由超级用户创建）；可重复执行。demo.py seed 每次重置门店库后都会执行。
SET client_encoding = 'UTF8';

CREATE SCHEMA IF NOT EXISTS avelis_audit;
REVOKE ALL ON SCHEMA avelis_audit FROM PUBLIC;

CREATE TABLE IF NOT EXISTS avelis_audit.ddl_event (
  id           bigserial PRIMARY KEY,
  at           timestamptz NOT NULL DEFAULT clock_timestamp(),
  db_user      text NOT NULL,          -- session_user：登录用户（SET ROLE 后的角色不在此记录）
  command_tag  text NOT NULL,          -- ALTER TABLE、CREATE VIEW、COMMENT、DROP TABLE …
  object_type  text,                   -- table、view、table column …
  object_names text[],                 -- pg_identify_object_as_address：{模式, 表[, 列]}
  statement    text,                   -- current_query()：整个请求，可能含多条语句（Avelis 不落库多语句与敏感原文）
  xid          xid8                    -- 执行 DDL 的事务：按两次元数据快照的可见性判断它属于哪个采集窗口
);
-- 早先部署的表补列；按时间窗口与事务号读取，各建一个索引
ALTER TABLE avelis_audit.ddl_event ADD COLUMN IF NOT EXISTS xid xid8;
CREATE INDEX IF NOT EXISTS ddl_event_at_id ON avelis_audit.ddl_event (at, id);
CREATE INDEX IF NOT EXISTS ddl_event_xid ON avelis_audit.ddl_event (xid);
REVOKE ALL ON avelis_audit.ddl_event FROM PUBLIC;

-- SECURITY DEFINER：执行 DDL 的用户不需要（也没有）这张表的写权限，不能伪造审计记录
CREATE OR REPLACE FUNCTION avelis_audit.on_ddl_end() RETURNS event_trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE r record;
BEGIN
  FOR r IN SELECT * FROM pg_event_trigger_ddl_commands() LOOP
    IF r.schema_name = 'avelis_audit' THEN CONTINUE; END IF;
    INSERT INTO avelis_audit.ddl_event (db_user, command_tag, object_type, object_names, statement, xid)
    SELECT session_user, r.command_tag, a.type, a.object_names, current_query(), pg_current_xact_id()
    FROM pg_identify_object_as_address(r.classid, r.objid, r.objsubid) a;
  END LOOP;
END $$;

CREATE OR REPLACE FUNCTION avelis_audit.on_sql_drop() RETURNS event_trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE r record;
BEGIN
  -- 直接删除的对象，以及级联删除的表与视图（DROP SCHEMA … CASCADE 删掉的表也要记到执行人名下）
  FOR r IN SELECT * FROM pg_event_trigger_dropped_objects()
           WHERE original OR object_type IN ('table', 'view', 'materialized view', 'foreign table') LOOP
    IF r.schema_name = 'avelis_audit' THEN CONTINUE; END IF;
    INSERT INTO avelis_audit.ddl_event (db_user, command_tag, object_type, object_names, statement, xid)
    VALUES (session_user, tg_tag, r.object_type, r.address_names, current_query(), pg_current_xact_id());
  END LOOP;
END $$;

DROP EVENT TRIGGER IF EXISTS avelis_ddl_end;
CREATE EVENT TRIGGER avelis_ddl_end ON ddl_command_end EXECUTE FUNCTION avelis_audit.on_ddl_end();
DROP EVENT TRIGGER IF EXISTS avelis_sql_drop;
CREATE EVENT TRIGGER avelis_sql_drop ON sql_drop EXECUTE FUNCTION avelis_audit.on_sql_drop();

GRANT USAGE ON SCHEMA avelis_audit TO avelis_collector;
GRANT SELECT ON avelis_audit.ddl_event TO avelis_collector;
