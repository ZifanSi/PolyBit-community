-- Every journal entry carries a SHA-256 of its own content (see polybit/ledger.py).
-- Entries written before this migration have no hash; every new one must.
ALTER TABLE journal ADD COLUMN hash BYTEA CHECK (octet_length(hash) = 32);

CREATE FUNCTION pb_require_entry_hash() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.hash IS NULL THEN
        RAISE EXCEPTION 'PolyBit ledger: journal entries need a hash';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER journal_hash_required
    BEFORE INSERT ON journal
    FOR EACH ROW EXECUTE FUNCTION pb_require_entry_hash();
