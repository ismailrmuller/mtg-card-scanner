#!/usr/bin/env python3
"""
Parse and import an AI-generated deck .md file into cards.db.

Usage:
    python import_deck.py <deck.md>
    python import_deck.py <deck.md> --db path/to/cards.db
"""
import json
import re
import sqlite3
import sys
from pathlib import Path

DB_PATH = Path("output/cards.db")

SECTION_MAP = {
    "criaturas":     "criaturas",
    "feiticos":      "feiticos",
    "feitiços":      "feiticos",
    "encantamentos": "feiticos",
    "terrenos":      "terrenos",
}

_MANA_RE = re.compile(r'(\{[^}]+\}(?:\{[^}]+\})*|—)')


def ensure_deck_schema(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS decks (
            id            TEXT PRIMARY KEY,
            nome          TEXT NOT NULL,
            dono          TEXT,
            formato       TEXT,
            cores         TEXT,
            arquetipo     TEXT,
            total_cartas  INTEGER,
            status        TEXT DEFAULT 'proposed',
            criado_em     TEXT,
            notas         TEXT,
            sinergias     TEXT DEFAULT '',
            linha_de_jogo TEXT DEFAULT '',
            substituicoes TEXT DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS deck_cards (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            deck_id     TEXT NOT NULL,
            card_id     INTEGER,
            nome        TEXT NOT NULL,
            quantidade  INTEGER DEFAULT 1,
            secao       TEXT,
            FOREIGN KEY (deck_id) REFERENCES decks(id)
        );
    """)
    # Migrate existing tables that predate extended columns
    for col in ("sinergias", "linha_de_jogo", "substituicoes"):
        try:
            conn.execute(f"ALTER TABLE decks ADD COLUMN {col} TEXT DEFAULT ''")
        except Exception:
            pass
    conn.commit()


def _parse_card_line(line):
    m = re.match(r'^(\d+)\s+', line)
    if not m:
        return None
    qty  = int(m.group(1))
    rest = line[m.end():]

    mana_m = _MANA_RE.search(rest)
    if not mana_m:
        return None

    nome       = rest[:mana_m.start()].strip()
    mana       = mana_m.group(0)
    after_mana = rest[mana_m.end():].strip()

    if not nome or not after_mana:
        return None

    # Split from right: color last, P/T second-to-last if X/Y format
    tokens = after_mana.rsplit(None, 2)
    if len(tokens) == 3 and re.match(r'^\d+/\d+$', tokens[1]):
        tipo, pt, cor = tokens
    else:
        tipo = " ".join(tokens[:-1]).strip() if len(tokens) > 1 else after_mana
        pt   = ""
        cor  = tokens[-1] if tokens else ""

    return {
        "nome":         nome,
        "quantidade":   qty,
        "custo_mana":   "" if mana == "—" else mana,
        "tipo":         tipo,
        "poder_resist": pt,
        "cor":          cor,
    }


def parse_deck_md(path):
    text = path.read_text(encoding="utf-8")

    meta = {}
    jm = re.search(r'```json\s*(\{.*?\})\s*```', text, re.DOTALL)
    if jm:
        try:
            meta = json.loads(jm.group(1))
        except json.JSONDecodeError:
            pass

    dn = re.search(r'^# DECK:\s*(.+)$', text, re.MULTILINE)
    om = re.search(r'\*\*Dono:\*\*\s*(.+)', text)
    fm = re.search(r'\*\*Formato:\*\*\s*(.+)', text)
    dm = re.search(r'## DESCRI\S+O\s*\n\n(.*?)\n\n---', text, re.DOTALL)

    def _sec(keyword):
        m = re.search(r'## ' + keyword + r'[^\n]*\n\n(.*?)\n\n---', text, re.DOTALL)
        return m.group(1).strip() if m else ""

    deck = {
        "id":           meta.get("deck_id", path.stem),
        "nome":         dn.group(1).strip() if dn else path.stem,
        "dono":         om.group(1).strip().strip("*").strip() if om else meta.get("owner", ""),
        "formato":      fm.group(1).strip().strip("*").strip() if fm else meta.get("format", ""),
        "cores":        ",".join(meta.get("colors", [])),
        "arquetipo":    meta.get("archetype", ""),
        "total_cartas": meta.get("total_cards", 0),
        "status":       meta.get("status", "proposed"),
        "criado_em":    meta.get("created_date", ""),
        "notas":        dm.group(1).strip() if dm else "",
        "sinergias":    _sec("SINERGIAS"),
        "linha_de_jogo": _sec("LINHA DE JOGO"),
        "substituicoes": _sec("SUBSTITU"),
        "cards":        [],
    }

    current_section = None
    in_code_block   = False

    for line in text.splitlines():
        stripped = line.strip()

        if stripped.startswith("### "):
            header = stripped[4:].lower()
            current_section = None
            for key, val in SECTION_MAP.items():
                if key in header:
                    current_section = val
                    break
            in_code_block = False
            continue

        if stripped.startswith("```json"):
            in_code_block = False
            continue
        if stripped == "```":
            in_code_block = not in_code_block
            continue

        if not in_code_block or not current_section or not stripped:
            continue

        card = _parse_card_line(stripped)
        if card:
            card["secao"] = current_section
            deck["cards"].append(card)

    return deck


def import_deck(deck, db_path):
    """Insert/replace deck in DB. Returns (total_lines, matched_in_collection)."""
    conn = sqlite3.connect(db_path)
    ensure_deck_schema(conn)

    conn.execute("""
        INSERT OR REPLACE INTO decks
            (id, nome, dono, formato, cores, arquetipo, total_cartas, status, criado_em,
             notas, sinergias, linha_de_jogo, substituicoes)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        deck["id"], deck["nome"], deck["dono"], deck["formato"], deck["cores"],
        deck["arquetipo"], deck["total_cartas"], deck["status"], deck["criado_em"],
        deck["notas"], deck.get("sinergias", ""), deck.get("linha_de_jogo", ""),
        deck.get("substituicoes", ""),
    ))

    conn.execute("DELETE FROM deck_cards WHERE deck_id = ?", (deck["id"],))

    matched = 0
    for card in deck["cards"]:
        row = conn.execute(
            "SELECT id FROM cards WHERE LOWER(nome) = LOWER(?) LIMIT 1",
            (card["nome"],)
        ).fetchone()
        card_id = row[0] if row else None
        if card_id:
            matched += 1
        conn.execute("""
            INSERT INTO deck_cards (deck_id, card_id, nome, quantidade, secao)
            VALUES (?, ?, ?, ?, ?)
        """, (deck["id"], card_id, card["nome"], card["quantidade"], card["secao"]))

    conn.commit()
    conn.close()
    return len(deck["cards"]), matched


if __name__ == "__main__":
    args = sys.argv[1:]
    if not args or args[0].startswith("-"):
        print("Uso: python import_deck.py <deck.md> [--db path/to/cards.db]")
        sys.exit(1)

    md_path = Path(args[0])
    if not md_path.is_file():
        print(f"Ficheiro nao encontrado: {md_path}")
        sys.exit(1)

    db = Path(args[args.index("--db") + 1]) if "--db" in args else DB_PATH

    print(f"A analisar {md_path.name}...")
    d = parse_deck_md(md_path)
    print(f"  Deck: {d['nome']}  |  Dono: {d['dono']}  |  Cartas: {len(d['cards'])}")

    print(f"A importar para {db}...")
    total, matched = import_deck(d, db)
    print(f"  {total} linhas  |  {matched} encontradas na colecao")
    print("Concluido!")
