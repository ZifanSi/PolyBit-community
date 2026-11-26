-- Block hashes written to a public EVM chain (see polybit/anchor.py).
CREATE TABLE anchors (
    id          BIGSERIAL   PRIMARY KEY,
    height      BIGINT      NOT NULL REFERENCES blocks (height),
    block_hash  BYTEA       NOT NULL CHECK (octet_length(block_hash) = 32),
    chain_id    BIGINT      NOT NULL,
    address     TEXT        NOT NULL CHECK (address ~ '^0x[0-9a-fA-F]{40}$'),
    tx_hash     BYTEA       NOT NULL CHECK (octet_length(tx_hash) = 32),
    status      TEXT        NOT NULL DEFAULT 'pending'
                            CHECK (status IN ('pending', 'confirmed', 'failed', 'dropped')),
    l1_block    BIGINT,
    sent_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    settled_at  TIMESTAMPTZ
);

-- One transaction in flight at a time. A retry after a failed broadcast or a drop can be
-- the very same signed transaction (same nonce and fees), hence the same hash.
CREATE UNIQUE INDEX anchors_one_pending ON anchors ((TRUE)) WHERE status = 'pending';
CREATE UNIQUE INDEX anchors_live_tx ON anchors (tx_hash) WHERE status IN ('pending', 'confirmed');
CREATE INDEX anchors_confirmed_height ON anchors (height) WHERE status = 'confirmed';

-- A record is never deleted, and only ever settles once: pending -> confirmed/failed/dropped.
CREATE FUNCTION pb_anchor_settles_once() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'PolyBit ledger: anchors is append-only';
    END IF;
    IF OLD.status <> 'pending'
       OR (NEW.id, NEW.height, NEW.block_hash, NEW.chain_id, NEW.address, NEW.tx_hash, NEW.sent_at)
          IS DISTINCT FROM (OLD.id, OLD.height, OLD.block_hash, OLD.chain_id, OLD.address, OLD.tx_hash, OLD.sent_at) THEN
        RAISE EXCEPTION 'PolyBit ledger: an anchor only settles once';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER anchors_settle_once
    BEFORE UPDATE OR DELETE ON anchors
    FOR EACH ROW EXECUTE FUNCTION pb_anchor_settles_once();
