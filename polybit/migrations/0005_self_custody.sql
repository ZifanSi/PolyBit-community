-- Self-custody wallets (see polybit/signing.py). A wallet with a key can only be
-- debited by an entry signed with that key.
ALTER TABLE users
    ADD COLUMN pubkey BYTEA UNIQUE CHECK (octet_length(pubkey) = 32),
    ADD COLUMN nonce  BIGINT NOT NULL DEFAULT 0 CHECK (nonce >= 0);

-- Signed entries keep the exact bytes that were signed, so anyone can re-check them.
ALTER TABLE journal
    ADD COLUMN signer    BYTEA CHECK (octet_length(signer) = 32),
    ADD COLUMN signature BYTEA CHECK (octet_length(signature) = 64),
    ADD COLUMN payload   TEXT  CHECK (char_length(payload) <= 4000),
    ADD CONSTRAINT journal_signed_shape CHECK (
        (signer IS NULL) = (signature IS NULL) AND (signer IS NULL) = (payload IS NULL)
    );

-- A key, once bound, is for good.
CREATE FUNCTION pb_key_is_permanent() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.pubkey IS NOT NULL AND NEW.pubkey IS DISTINCT FROM OLD.pubkey THEN
        RAISE EXCEPTION 'PolyBit ledger: wallet key of % is permanent', OLD.discord_id;
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER users_key_permanent
    BEFORE UPDATE OF pubkey ON users
    FOR EACH ROW EXECUTE FUNCTION pb_key_is_permanent();

-- No unsigned debit from a keyed wallet, whatever code path wrote it. FOR SHARE waits
-- for a key being bound right now, so a debit can't slip in under a half-done bind.
CREATE FUNCTION pb_require_owner_signature() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    owner_key BYTEA;
    entry_signer BYTEA;
BEGIN
    IF NEW.amount >= 0 THEN
        RETURN NEW;
    END IF;
    SELECT u.pubkey INTO owner_key
    FROM accounts a JOIN users u ON u.discord_id = a.owner_id
    WHERE a.id = NEW.account_id
    FOR SHARE OF u;
    IF owner_key IS NULL THEN
        RETURN NEW;
    END IF;
    SELECT signer INTO entry_signer FROM journal WHERE id = NEW.journal_id;
    IF entry_signer IS DISTINCT FROM owner_key THEN
        RAISE EXCEPTION 'PolyBit ledger: account % is self-custody, debits need its signature', NEW.account_id;
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER postings_owner_signature
    BEFORE INSERT ON postings
    FOR EACH ROW EXECUTE FUNCTION pb_require_owner_signature();
