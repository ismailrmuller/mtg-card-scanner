# Future Feature: External Collection & Deck Sync

> **Status:** Unimplemented — design is ready to pick up.  
> This document was written from a design session so that a future contributor
> (or the original author in a few months) can implement it without starting from scratch.

---

## What this is

The app currently manages a card collection **entirely locally** — SQLite database, local thumbnails, local exports. This feature would add a one-way **push to external MTG platforms** (Moxfield, Archidekt, Deckbox, or any other service that accepts a standard import format), without rebuilding any of the UI those platforms already provide.

---

## Design principles

- **No lock-in to a single service.** An abstraction layer lets formats be added or swapped without touching core logic.
- **Reliability over automation.** If a target has no stable public API, a clean file export the user can manually import is the right default — not fragile undocumented API calls.
- **Scryfall is the canonical card reference.** Cards in the DB may already have a Scryfall ID. Use it as the primary key when mapping to external formats. If a card is missing one, resolve it via `api.scryfall.com/cards/named?fuzzy=<name>` before formatting.
- **Additive only.** No changes to the existing DB schema or core logic — this is a new module that reads from the DB and writes to files or HTTP.

---

## Proposed architecture

```
server/
└── exporter.py          ← new module

         Local DB
            │
            ▼
    CollectionExporter
            │
     ┌──────┴──────┐
     │             │
 Formatter      Delivery
     │             │
  format_plain_text_deck()    export_to_file()     → .txt / .csv saved locally
  format_deckbox_csv()        copy_to_clipboard()  → pastes into browser import field
  format_moxfield_csv()       api_push()           → POST to service (optional)
  format_archidekt_json()
```

The GUI gets one new button in the existing **Exportar** tab — a dropdown to pick the target format, a choice of what to export (whole collection / one owner / one deck / all decks), and an Export button.

---

## File formats to implement (in priority order)

### 1. Plain-text deck list — universally accepted

Accepted by Moxfield, Archidekt, Deckbox, MTGO, CubeCobra, and most others for deck import.

```
// Deck Name: Legiões de Naya
// Owner: Rodrigo
// Format: Casual Constructed

2 Gallant Cavalry
3 Estrategista Ampryn
2 Arenovelocista Viashino
1 Doomed Traveler

// Lands
8 Plains
7 Forest
5 Mountain
1 Evolving Wilds
```

### 2. Deckbox CSV — standard collection format

```
Count,Name,Edition,Condition,Language,Foil,Signed,Artist Proof,Altered Art
2,Gallant Cavalry,Core Set 2019,Near Mint,English,,,,
3,Estrategista Ampryn,Magic Origins,Near Mint,Portuguese,,,,
```

The `Edition` field maps from the `edicao` column already in the DB.  
`Condition` is not currently tracked — default to `Near Mint` and document this assumption.

### 3. Moxfield CSV / Archidekt JSON (optional)

These platforms have community-documented import formats. Evaluate at implementation
time — they change occasionally. Wrap any format-specific logic in its own function so
it can be updated independently.

---

## Scryfall ID resolution

Some cards in the DB may have a `NULL` Scryfall ID (especially early scans before the
enrichment step was added). Before formatting, resolve missing IDs:

```python
import requests

def resolve_scryfall_id(card_name: str) -> str | None:
    r = requests.get(
        "https://api.scryfall.com/cards/named",
        params={"fuzzy": card_name},
        timeout=5,
    )
    if r.status_code == 200:
        return r.json().get("id")
    return None
```

Cache results in memory for the duration of the export run — don't hit the API twice
for the same card name. Scryfall's rate limit is 10 req/s with a `100ms` sleep between
calls recommended in their docs.

---

## DB queries needed

Both already work with the existing schema — no schema changes required.

```python
# Collection export (per owner or all)
conn.execute("""
    SELECT nome, quantidade, edicao, cor, raridade, scryfall_id, dono
    FROM cards
    WHERE (? = 'Todos' OR dono = ?)
    ORDER BY dono, nome
""", (owner, owner))

# Deck export
conn.execute("""
    SELECT dc.nome, dc.quantidade, dc.secao, c.edicao, c.scryfall_id
    FROM deck_cards dc
    LEFT JOIN cards c ON dc.card_id = c.id
    WHERE dc.deck_id = ?
    ORDER BY dc.secao, dc.nome
""", (deck_id,))
```

Note: `quantidade` is stored per card row in `cards`, not as a separate owned-quantity
field. If a user owns 3 copies, there are 3 rows. The export should `SUM(quantidade)`
or `COUNT(*)` grouped by `(nome, edicao, dono)` depending on how duplicates are handled.
Check the current DB to confirm before writing the query.

---

## GUI integration

The existing **Exportar** tab already has owner filtering and export buttons. Add a second
row below the existing buttons:

```
[ Format ▾ ]  [ What ▾ ]          [ Export ]
  Plain text    Whole collection
  Deckbox CSV   Current owner
  Moxfield CSV  Deck: <dropdown>
```

Use `tkinter.filedialog.asksaveasfilename()` for the save dialog (same pattern as the
existing file picker — `ft.FilePicker` is broken in Flet 0.86).

---

## Optional: API push

Only implement this if the target service has a **documented, stable, authenticated API**.
At the time of writing:

| Service | API status |
|---|---|
| Scryfall | Public, stable, read-only (use for ID resolution) |
| Deckbox | No public write API |
| Moxfield | Unofficial, community-reverse-engineered, may break |
| Archidekt | Partial official API — check current docs |

Wrap any `api_push()` implementation in a `try/except` that falls back to file export
on any error, and display the error to the user. Gate it behind a config option
(`ENABLE_API_PUSH=true` in `.env`) so it can be disabled without touching code.

---

## Files to create

| File | Purpose |
|---|---|
| `server/exporter.py` | Core module — formatters + delivery functions |
| `server/scryfall_cache.py` | (optional) Simple in-memory name→ID cache |

Update `server/app.py` — Exportar tab only — to add the new UI row and wire it to
`exporter.py`. No other files should need to change.

---

## Known gaps to resolve at implementation time

- Confirm whether `cards.quantidade` is always `1` per row or can be >1 for duplicates.
- Confirm whether `cards.edicao` stores a Scryfall-compatible set name or a localized string (Portuguese names may need mapping).
- Decide whether to export the sideboard separately (currently no sideboard concept in the DB schema — would need `deck_cards.secao = 'sideboard'` which is unused).
