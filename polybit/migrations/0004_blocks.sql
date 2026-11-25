-- Blocks seal hashed journal entries into a hash-linked chain (see polybit/chain.py).
CREATE TABLE blocks (
    height       BIGINT      PRIMARY KEY CHECK (height > 0),
    prev_hash    BYTEA       NOT NULL CHECK (octet_length(prev_hash) = 32),
    merkle_root  BYTEA       NOT NULL CHECK (octet_length(merkle_root) = 32),
    tx_count     INTEGER     NOT NULL CHECK (tx_count > 0),
    hash         BYTEA       NOT NULL UNIQUE CHECK (octet_length(hash) = 32),
    created_at   TIMESTAMPTZ NOT NULL
);

-- Which block each entry is in, and where (its leaf position in the Merkle tree).
CREATE TABLE block_entries (
    journal_id  BIGINT  PRIMARY KEY REFERENCES journal (id),
    height      BIGINT  NOT NULL REFERENCES blocks (height),
    position    INTEGER NOT NULL CHECK (position >= 0),
    UNIQUE (height, position)
);

CREATE TRIGGER blocks_append_only
    BEFORE UPDATE OR DELETE ON blocks
    FOR EACH ROW EXECUTE FUNCTION pb_forbid_ledger_mutation();

CREATE TRIGGER block_entries_append_only
    BEFORE UPDATE OR DELETE ON block_entries
    FOR EACH ROW EXECUTE FUNCTION pb_forbid_ledger_mutation();
