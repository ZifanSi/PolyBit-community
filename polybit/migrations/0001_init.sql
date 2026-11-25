-- PolyBit initial schema: users, accounts and an append-only double-entry ledger.

CREATE TABLE users (
    discord_id  BIGINT PRIMARY KEY,
    frozen      BOOLEAN     NOT NULL DEFAULT FALSE,
    welcomed    BOOLEAN     NOT NULL DEFAULT FALSE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Every balance lives in an account. User accounts belong to one Discord user;
-- system accounts (mint, treasury, burn) are named. Only the mint may go negative:
-- its negated balance is the total amount of PB ever issued.
CREATE TABLE accounts (
    id              BIGSERIAL PRIMARY KEY,
    kind            TEXT        NOT NULL CHECK (kind IN ('user', 'system')),
    owner_id        BIGINT      UNIQUE REFERENCES users (discord_id),
    name            TEXT        UNIQUE,
    allow_negative  BOOLEAN     NOT NULL DEFAULT FALSE,
    balance         BIGINT      NOT NULL DEFAULT 0,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT accounts_kind_shape CHECK (
        (kind = 'user'   AND owner_id IS NOT NULL AND name IS NULL AND NOT allow_negative)
     OR (kind = 'system' AND owner_id IS NULL     AND name IS NOT NULL)
    ),
    CONSTRAINT accounts_non_negative CHECK (allow_negative OR balance >= 0)
);

INSERT INTO accounts (kind, name, allow_negative) VALUES
    ('system', 'mint',     TRUE),
    ('system', 'treasury', FALSE),
    ('system', 'burn',     FALSE);

-- One row per economic event. idempotency_key makes retries safe.
CREATE TABLE journal (
    id               BIGSERIAL PRIMARY KEY,
    kind             TEXT        NOT NULL,
    idempotency_key  TEXT        UNIQUE,
    memo             TEXT        CHECK (char_length(memo) <= 200),
    actor_id         BIGINT,
    guild_id         BIGINT,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- The legs of each journal entry. The amounts of one entry always sum to zero.
CREATE TABLE postings (
    id          BIGSERIAL PRIMARY KEY,
    journal_id  BIGINT NOT NULL REFERENCES journal (id),
    account_id  BIGINT NOT NULL REFERENCES accounts (id),
    amount      BIGINT NOT NULL CHECK (amount <> 0)
);

CREATE INDEX postings_account_idx ON postings (account_id, journal_id DESC);
CREATE INDEX postings_journal_idx ON postings (journal_id);

-- Enforce zero-sum entries at commit time.
CREATE FUNCTION pb_check_journal_balanced() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    total NUMERIC;
BEGIN
    SELECT COALESCE(SUM(amount), 0) INTO total FROM postings WHERE journal_id = NEW.journal_id;
    IF total <> 0 THEN
        RAISE EXCEPTION 'PolyBit ledger: journal % is unbalanced (sum = %)', NEW.journal_id, total;
    END IF;
    RETURN NULL;
END;
$$;

CREATE CONSTRAINT TRIGGER postings_balanced
    AFTER INSERT ON postings
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION pb_check_journal_balanced();

-- The ledger is append-only: corrections are new entries, never edits.
CREATE FUNCTION pb_forbid_ledger_mutation() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'PolyBit ledger: % is append-only', TG_TABLE_NAME;
END;
$$;

CREATE TRIGGER journal_append_only
    BEFORE UPDATE OR DELETE ON journal
    FOR EACH ROW EXECUTE FUNCTION pb_forbid_ledger_mutation();

CREATE TRIGGER postings_append_only
    BEFORE UPDATE OR DELETE ON postings
    FOR EACH ROW EXECUTE FUNCTION pb_forbid_ledger_mutation();

CREATE TABLE daily_claims (
    user_id        BIGINT PRIMARY KEY REFERENCES users (discord_id),
    last_claim_at  TIMESTAMPTZ,
    streak         INTEGER NOT NULL DEFAULT 0
);

-- Which servers a user has been seen in (for per-server leaderboards).
CREATE TABLE user_guilds (
    user_id       BIGINT NOT NULL REFERENCES users (discord_id),
    guild_id      BIGINT NOT NULL,
    last_seen_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, guild_id)
);

CREATE INDEX user_guilds_guild_idx ON user_guilds (guild_id);

-- Administrative actions that are not themselves ledger entries (e.g. freezing).
CREATE TABLE audit_log (
    id          BIGSERIAL PRIMARY KEY,
    actor_id    BIGINT      NOT NULL,
    action      TEXT        NOT NULL,
    target_id   BIGINT,
    details     JSONB       NOT NULL DEFAULT '{}'::jsonb,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
