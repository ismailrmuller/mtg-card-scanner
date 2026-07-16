#!/usr/bin/env python3
"""
MTG Collection — AI Export
Generates two files optimised for pasting into an AI assistant:
  - colecao_ai[_owner].csv  : structured CSV, all fields
  - colecao_ai[_owner].txt  : compact grouped list, easier to read / fewer tokens

Usage:
    python export_ai.py                  # export all owners
    python export_ai.py --dono Vicente   # export one owner only
"""

import csv
import sqlite3
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path

from rich.console import Console
from rich.table import Table

# ── Paths ─────────────────────────────────────────────────────────────────────

DB_PATH    = Path("output/cards.db")
OUTPUT_DIR = Path("output")

console = Console()

# ── Helpers ───────────────────────────────────────────────────────────────────

def parse_dono():
    for i, arg in enumerate(sys.argv[1:], 1):
        if arg == "--dono" and i < len(sys.argv):
            return sys.argv[i + 1]
    return None

def file_suffix(dono):
    return f"_{dono.replace(' ', '_')}" if dono else "_todos"

# ── Queries ───────────────────────────────────────────────────────────────────

COLS = "dono, nome, tipo, subtipo, cor, custo_mana, poder_resist, raridade, edicao, texto"

def fetch(conn, dono):
    if dono:
        return conn.execute(
            f"SELECT {COLS} FROM cards WHERE dono=? ORDER BY tipo, nome", (dono,)
        ).fetchall()
    return conn.execute(
        f"SELECT {COLS} FROM cards ORDER BY dono, tipo, nome"
    ).fetchall()

def owner_list(conn):
    return [r[0] for r in conn.execute(
        "SELECT dono, COUNT(*) n FROM cards GROUP BY dono ORDER BY n DESC"
    ).fetchall()]

# ── CSV export ────────────────────────────────────────────────────────────────

HEADERS = ["Dono", "Nome", "Tipo", "Subtipo", "Cor", "Custo Mana",
           "P/R", "Raridade", "Edição", "Habilidades"]

def export_csv(rows, path):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(HEADERS)
        w.writerows(rows)

# ── Text export ───────────────────────────────────────────────────────────────

# Order types into logical groups for the text file
TYPE_ORDER = [
    "Criatura", "Planeswalker",
    "Feitiço", "Mágica Instantânea",
    "Encantamento", "Artefato",
    "Terreno",
]

def export_txt(rows, path, dono, today):
    owners   = sorted({r[0] for r in rows if r[0]})
    total    = len(rows)
    header   = dono if dono else "Todos os donos (" + ", ".join(owners) + ")"

    by_type  = defaultdict(list)
    for r in rows:
        dono_r, nome, tipo, subtipo, cor, custo, pr, raridade, edicao, texto = r
        tipo_key = next((t for t in TYPE_ORDER if tipo and tipo.startswith(t)), tipo or "Outro")
        pr_str   = f" {pr}" if pr else ""
        sub_str  = f" — {subtipo}" if subtipo else ""
        edt_str  = f" [{edicao}]" if edicao else ""
        txt_str  = f"\n     {texto}" if texto else ""
        line     = f"  {dono_r or '?':10}  {nome}{sub_str} | {cor} {custo}{pr_str} | {raridade}{edt_str}{txt_str}"
        by_type[tipo_key].append(line)

    # Remaining types not in TYPE_ORDER
    ordered_keys = [k for k in TYPE_ORDER if k in by_type]
    ordered_keys += [k for k in sorted(by_type) if k not in ordered_keys]

    with open(path, "w", encoding="utf-8") as f:
        f.write(f"=== COLEÇÃO MTG — {header.upper()} ({total} cartas) ===\n")
        f.write(f"Exportado em {today}\n\n")
        f.write("Colunas: Dono | Nome — Subtipo | Cor CustoMana P/R | Raridade [Edição]\n")
        f.write("         Habilidades (linha abaixo, indentada)\n\n")

        for tipo_key in ordered_keys:
            lines = by_type[tipo_key]
            f.write(f"── {tipo_key.upper()} ({len(lines)}) {'─' * (50 - len(tipo_key))}\n")
            f.write("\n".join(lines))
            f.write("\n\n")

# ── Deck export ───────────────────────────────────────────────────────────────

_SEC_LABELS = {
    "criaturas": "CRIATURAS",
    "feiticos":  "FEITIÇOS/ENCANTAMENTOS",
    "terrenos":  "TERRENOS",
}

def fetch_decks(conn, dono):
    """Return deck data for all decks matching dono (or all if dono is None)."""
    try:
        decks = conn.execute("""
            SELECT id, nome, dono, formato, cores, status,
                   notas, sinergias, linha_de_jogo, substituicoes
            FROM decks
            WHERE (? IS NULL OR dono = ?)
            ORDER BY nome
        """, (dono, dono)).fetchall()
    except sqlite3.OperationalError:
        return []   # table doesn't exist yet — no decks created

    result = []
    for deck in decks:
        deck_id = deck[0]
        try:
            rows = conn.execute("""
                SELECT dc.nome, dc.quantidade, dc.secao,
                       CASE WHEN dc.card_id IS NOT NULL THEN 1 ELSE 0 END
                FROM deck_cards dc
                WHERE dc.deck_id = ?
                ORDER BY dc.secao, dc.nome
            """, (deck_id,)).fetchall()
        except sqlite3.OperationalError:
            rows = []

        sections: dict = {}
        in_col = 0
        for nome, qty, secao, matched in rows:
            key = secao if secao in ("criaturas", "feiticos", "terrenos") else "feiticos"
            sections.setdefault(key, []).append((nome, qty))
            if matched:
                in_col += 1
        result.append((deck, sections, in_col, len(rows)))
    return result


def append_txt_decks(deck_data, path):
    """Append deck listings to an existing txt export file."""
    if not deck_data:
        return
    with open(path, "a", encoding="utf-8") as f:
        f.write(f"\n\n{'═' * 62}\n")
        f.write(f"DECKS ({len(deck_data)})\n")
        f.write(f"{'═' * 62}\n\n")
        for deck, sections, in_col, total in deck_data:
            _, nome, dono_d, formato, cores, status, notas, sinergias, linha_jogo, subs = deck
            meta = "  ·  ".join(filter(None, [dono_d, formato, cores, status]))
            f.write(f"▶ {nome}  [{meta}]  {in_col}/{total} na coleção\n")
            for sec in ("criaturas", "feiticos", "terrenos"):
                cards = sections.get(sec, [])
                if not cards:
                    continue
                total_qty = sum(q for _, q in cards)
                card_list = ",  ".join(
                    f"{q}× {n}" if q > 1 else n for n, q in cards
                )
                f.write(f"  {_SEC_LABELS[sec]} ({total_qty}): {card_list}\n")
            for label, text in [("Descrição", notas), ("Sinergias", sinergias),
                                 ("Linha de jogo", linha_jogo), ("Substituições", subs)]:
                if text and text.strip():
                    truncated = text.strip()[:300]
                    if len(text.strip()) > 300:
                        truncated += "…"
                    f.write(f"  {label}: {truncated}\n")
            f.write("\n")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    if not DB_PATH.is_file():
        console.print(f"[red]Banco não encontrado:[/red] {DB_PATH.resolve()}")
        sys.exit(1)

    dono  = parse_dono()
    today = date.today().isoformat()

    conn  = sqlite3.connect(DB_PATH)
    rows  = fetch(conn, dono)

    if not rows:
        label = f"dono='{dono}'" if dono else "qualquer dono"
        console.print(f"[yellow]Nenhuma carta encontrada para {label}[/yellow]")
        conn.close()
        sys.exit(0)

    suffix   = file_suffix(dono)
    csv_path = OUTPUT_DIR / f"colecao_ai{suffix}.csv"
    txt_path = OUTPUT_DIR / f"colecao_ai{suffix}.txt"

    export_csv(rows, csv_path)
    export_txt(rows, txt_path, dono, today)

    deck_data = fetch_decks(conn, dono)
    append_txt_decks(deck_data, txt_path)
    conn.close()

    # ── Summary table ─────────────────────────────────────────────────────────
    label = dono or "todos os donos"
    console.print(f"\n[bold green]Exportado[/bold green] — {len(rows)} cartas"
                  f" + {len(deck_data)} deck(s)  ({label})\n")

    t = Table(show_header=False, box=None, padding=(0, 2))
    t.add_column(style="dim")
    t.add_column()
    t.add_row("CSV  (todos os campos):", str(csv_path))
    t.add_row("TXT  (colecção + decks):", str(txt_path))
    console.print(t)

    console.print(
        "\n[dim]Sugestão de prompt:[/dim]\n"
        f"  Cole o conteúdo de [bold]{txt_path.name}[/bold] e escreva:\n"
        '  "[italic]Monte um deck de 60 cartas usando apenas estas cartas. '
        'Prefiro estratégia agressiva vermelho/verde.[/italic]"\n'
    )

if __name__ == "__main__":
    main()
