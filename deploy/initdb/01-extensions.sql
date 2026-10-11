-- 交付形态数据库初始化（PostgreSQL 16 + pgvector）
--
-- 说明：
--   1) 本地开发默认走 SQLite（单机一键拉起，无需服务）；本文件服务于交付形态（docker-compose）。
--   2) 表结构由各模块的 ensure_schema() 幂等创建（queue_store / runtime_store / rules_store 等），
--      此处只做"扩展与基础设施"部分，避免与代码里的 DDL 产生第二份真相源。
--   3) 向量检索（法规库）二期接入时用 vector 列；一期检索走关键词 + 2-gram（见 regulations_store.py）。

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- 法规库向量表（预留：二期把 regulations.csv 灌入并建索引）
CREATE TABLE IF NOT EXISTS regulation_embedding (
    regulation_ref TEXT PRIMARY KEY,
    source         TEXT,
    domain         TEXT,
    text           TEXT NOT NULL,
    embedding      vector(1024),          -- bge-large-zh 维度；接入时按实际模型调整
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 待办队列的并发消费索引（FOR UPDATE SKIP LOCKED 依赖）
CREATE INDEX IF NOT EXISTS idx_audit_run_created
    ON compliance_audit (run_id, created_at);
