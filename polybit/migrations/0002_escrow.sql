-- Escrow holds PB between payment and settlement (for extensions).
INSERT INTO accounts (kind, name, allow_negative) VALUES ('system', 'escrow', FALSE);
