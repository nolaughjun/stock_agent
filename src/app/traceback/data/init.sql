-- ============================================================
-- 溯源分析系统 P1 PostgreSQL Schema
-- 对齐 01_phase1_design.md §5.1
-- P1 不做多租户, 所有表暂不加 tenant_id (P4 统一补)
-- ============================================================

-- 溯源任务
CREATE TABLE IF NOT EXISTS traceback_tasks (
    task_id          TEXT PRIMARY KEY,
    tenant_id        TEXT NOT NULL DEFAULT 'default',  -- P4 多租户
    task_name        TEXT NOT NULL,
    description      TEXT,
    status           TEXT NOT NULL,          -- created/analyzing/reviewing/done/failed
    current_stage    TEXT NOT NULL,
    alert_ids        JSONB NOT NULL DEFAULT '[]',
    asset_ips        JSONB NOT NULL DEFAULT '[]',
    asset_hosts      JSONB NOT NULL DEFAULT '[]',
    asset_users      JSONB NOT NULL DEFAULT '[]',
    event_start_time TIMESTAMPTZ,
    event_end_time   TIMESTAMPTZ,
    assignee         TEXT,
    retry_count      INT DEFAULT 0,
    error_message    TEXT,
    create_time      TIMESTAMPTZ NOT NULL DEFAULT now(),
    update_time      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON traceback_tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_update_time ON traceback_tasks(update_time);
CREATE INDEX IF NOT EXISTS idx_tasks_tenant ON traceback_tasks(tenant_id);

-- 证据引用 (PG 只存轻量引用, 原始数据在 MinIO, 元数据在 Mongo)
CREATE TABLE IF NOT EXISTS evidences (
    evidence_id   TEXT PRIMARY KEY,
    task_id       TEXT NOT NULL REFERENCES traceback_tasks(task_id) ON DELETE CASCADE,
    evidence_type TEXT NOT NULL,             -- log/traffic/intel/dns/sample
    source        TEXT NOT NULL,
    importance    TEXT NOT NULL,             -- high/medium/low
    tags          JSONB DEFAULT '[]',
    object_key    TEXT NOT NULL,             -- MinIO 对象 key
    meta_id       TEXT NOT NULL,             -- MongoDB 文档 id
    collect_time  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_evid_task ON evidences(task_id);
CREATE INDEX IF NOT EXISTS idx_evid_type ON evidences(evidence_type);

-- 攻击链 (JSONB 存完整结构)
CREATE TABLE IF NOT EXISTS attack_chains (
    chain_id    TEXT PRIMARY KEY,
    task_id     TEXT UNIQUE NOT NULL REFERENCES traceback_tasks(task_id) ON DELETE CASCADE,
    payload     JSONB NOT NULL,
    create_time TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 归因 (P1 桩)
CREATE TABLE IF NOT EXISTS attributions (
    attribution_id TEXT PRIMARY KEY,
    task_id        TEXT UNIQUE NOT NULL REFERENCES traceback_tasks(task_id) ON DELETE CASCADE,
    attributed     BOOLEAN DEFAULT FALSE,
    confidence     REAL DEFAULT 0,
    matched_iocs   JSONB DEFAULT '[]',
    payload        JSONB,
    create_time    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 人工审核记录
CREATE TABLE IF NOT EXISTS human_reviews (
    review_id     TEXT PRIMARY KEY,
    task_id       TEXT NOT NULL REFERENCES traceback_tasks(task_id) ON DELETE CASCADE,
    stage         TEXT NOT NULL,             -- timeline/attack_chain/report
    action        TEXT NOT NULL,             -- approve/revise
    reviewer      TEXT NOT NULL,
    comments      TEXT,
    modifications JSONB DEFAULT '[]',
    review_time   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_review_task ON human_reviews(task_id);

-- LangGraph checkpoint (用于中断恢复; P1 自管, P4 可换 langgraph-checkpoint-postgres)
CREATE TABLE IF NOT EXISTS langgraph_checkpoints (
    thread_id   TEXT NOT NULL,
    checkpoint  JSONB NOT NULL,
    update_time TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (thread_id)
);

-- updated_at 触发器
CREATE OR REPLACE FUNCTION trg_set_update_time() RETURNS trigger AS $$
BEGIN
    NEW.update_time = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_tasks_update ON traceback_tasks;
CREATE TRIGGER trg_tasks_update BEFORE UPDATE ON traceback_tasks
    FOR EACH ROW EXECUTE FUNCTION trg_set_update_time();

-- ============================================================
-- P3 知识库表
-- ============================================================

-- TTP 知识 (MITRE 关联 + 检测/处置 tips + 案例反向引用)
CREATE TABLE IF NOT EXISTS ttp_knowledge (
    ttp_id           TEXT PRIMARY KEY,
    mitre_tactic     TEXT NOT NULL,
    mitre_technique  TEXT NOT NULL,
    name             TEXT NOT NULL,
    description      TEXT,
    detection_tips   JSONB DEFAULT '[]',
    response_tips    JSONB DEFAULT '[]',
    source_cases     JSONB DEFAULT '[]',
    create_time      TIMESTAMPTZ DEFAULT now()
);

-- 处置方案库 (Playbook)
CREATE TABLE IF NOT EXISTS response_playbooks (
    playbook_id      TEXT PRIMARY KEY,
    source_case_id   TEXT,
    name             TEXT NOT NULL,
    description      TEXT,
    attack_type      TEXT NOT NULL,
    steps            JSONB NOT NULL,
    prerequisites    JSONB DEFAULT '[]',
    applicable_assets JSONB DEFAULT '[]',
    effectiveness    REAL DEFAULT 0,
    side_effects     JSONB DEFAULT '[]',
    author           TEXT DEFAULT 'system',
    version          TEXT DEFAULT '1.0',
    usage_count      INT DEFAULT 0,
    success_rate     REAL DEFAULT 0,
    create_time      TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_playbook_attack_type ON response_playbooks(attack_type);

-- IOC 知识元数据 (图谱关系在内存/Neo4j, 元数据冗余 PG 便于查询)
CREATE TABLE IF NOT EXISTS ioc_knowledge (
    ioc_id        TEXT PRIMARY KEY,
    source_case_id TEXT,
    ioc_type      TEXT NOT NULL,
    ioc_value     TEXT NOT NULL,
    threat_type   TEXT DEFAULT '',
    malware_family TEXT,
    threat_actor  TEXT,
    campaign      TEXT,
    context       TEXT DEFAULT '',
    confidence    REAL DEFAULT 0.5,
    false_positive BOOLEAN DEFAULT FALSE,
    tags          JSONB DEFAULT '[]',
    create_time   TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_ioc_value ON ioc_knowledge(ioc_type, ioc_value);

-- ============================================================
-- P4 安全: 操作审计日志 (SEC-004, 追加写不可篡改)
-- ============================================================
CREATE TABLE IF NOT EXISTS audit_logs (
    log_id        TEXT PRIMARY KEY,
    tenant_id     TEXT NOT NULL,
    user_id       TEXT NOT NULL,
    action        TEXT NOT NULL,         -- create_task/review/export_report...
    resource      TEXT NOT NULL,
    resource_id   TEXT,
    detail        JSONB DEFAULT '{}',
    ip            TEXT,
    user_agent    TEXT,
    log_time      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_audit_tenant_time ON audit_logs(tenant_id, log_time);

