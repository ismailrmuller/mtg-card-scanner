#!/usr/bin/env python3
"""
MTG Card Scanner -- Desktop App
Run from the server/ directory:  python app.py
"""
import asyncio
import json
import os
import re
import sqlite3
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

import flet as ft
import import_deck as _deck_mod

# Paths (relative to server/ where app.py lives)
DB_PATH       = Path("output/cards.db")
OUTPUT_DIR    = Path("output")
SCANS_DIR     = Path("scans")
HTML_PATH     = OUTPUT_DIR / "colecao.html"
XLSX_PATH     = OUTPUT_DIR / "cards.xlsx"
ENV_PATH      = Path(".env")
RECEIVER_PORT = 8765


def read_env() -> dict:
    if not ENV_PATH.is_file():
        return {}
    result = {}
    for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        result[k.strip()] = v.strip().strip('"').strip("'")
    return result


def write_env(data: dict):
    ENV_PATH.write_text(
        "\n".join(f"{k}={v}" for k, v in data.items()) + "\n",
        encoding="utf-8"
    )


def get_stats() -> dict:
    if not DB_PATH.is_file():
        return {"total": 0, "by_owner": [], "pending": 0}
    conn = sqlite3.connect(DB_PATH)
    total     = conn.execute("SELECT COUNT(*) FROM cards").fetchone()[0]
    by_owner  = conn.execute(
        "SELECT dono, COUNT(*) FROM cards GROUP BY dono ORDER BY 2 DESC"
    ).fetchall()
    processed = conn.execute("SELECT COUNT(*) FROM imagens_processadas").fetchone()[0]
    conn.close()
    n_jpgs = len(list(SCANS_DIR.glob("*.jpg"))) + len(list(SCANS_DIR.glob("*.jpeg")))
    return {"total": total, "by_owner": by_owner, "pending": max(0, n_jpgs - processed)}


def get_owners() -> list:
    if not DB_PATH.is_file():
        return []
    conn = sqlite3.connect(DB_PATH)
    owners = [r[0] for r in conn.execute(
        "SELECT DISTINCT dono FROM cards WHERE dono IS NOT NULL ORDER BY dono"
    ).fetchall()]
    conn.close()
    return owners


def strip_ansi(s: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*m", "", s)


def get_all_cards(search: str = "", dono: str = "") -> list:
    """Return cards matching search/owner filters. All fields for detail view."""
    if not DB_PATH.is_file():
        return []
    conn = sqlite3.connect(DB_PATH)
    q = """SELECT id, nome, tipo, subtipo, cor, custo_mana, poder_resist,
                  raridade, edicao, texto, thumb, dono
           FROM cards"""
    params: list = []
    clauses: list = []
    if dono:
        clauses.append("dono = ?")
        params.append(dono)
    if search:
        clauses.append("(nome LIKE ? OR tipo LIKE ? OR cor LIKE ? OR edicao LIKE ?)")
        like = f"%{search}%"
        params += [like, like, like, like]
    if clauses:
        q += " WHERE " + " AND ".join(clauses)
    q += " ORDER BY nome"
    rows = conn.execute(q, params).fetchall()
    conn.close()
    return rows


def get_all_decks() -> list:
    if not DB_PATH.is_file():
        return []
    conn = sqlite3.connect(DB_PATH)
    _deck_mod.ensure_deck_schema(conn)
    rows = conn.execute("""
        SELECT d.id, d.nome, d.dono, d.formato, d.cores,
               COUNT(dc.id)                                             AS n_linhas,
               SUM(CASE WHEN dc.card_id IS NOT NULL THEN 1 ELSE 0 END) AS n_matched
        FROM decks d
        LEFT JOIN deck_cards dc ON d.id = dc.deck_id
        GROUP BY d.id
        ORDER BY d.nome
    """).fetchall()
    conn.close()
    return rows


def get_deck_cards(deck_id: str):
    if not DB_PATH.is_file():
        return None, {}
    conn = sqlite3.connect(DB_PATH)
    _deck_mod.ensure_deck_schema(conn)
    deck = conn.execute(
        "SELECT id, nome, dono, formato, cores, total_cartas, status, notas FROM decks WHERE id = ?",
        (deck_id,)
    ).fetchone()
    rows = conn.execute("""
        SELECT dc.nome, dc.quantidade, dc.secao,
               CASE WHEN dc.card_id IS NOT NULL THEN 1 ELSE 0 END AS in_col,
               c.thumb
        FROM deck_cards dc
        LEFT JOIN cards c ON dc.card_id = c.id
        WHERE dc.deck_id = ?
        ORDER BY dc.secao, dc.nome
    """, (deck_id,)).fetchall()
    conn.close()
    sections = {}
    for nome, qty, secao, in_col, thumb in rows:
        key = secao if secao in ("criaturas", "feiticos", "terrenos") else "feiticos"
        sections.setdefault(key, []).append((nome, qty, bool(in_col), thumb))
    return deck, sections


def get_deck_full(deck_id: str):
    """Returns deck metadata (with extended info) and per-section card dicts."""
    if not DB_PATH.is_file():
        return None, {}
    conn = sqlite3.connect(DB_PATH)
    _deck_mod.ensure_deck_schema(conn)
    deck = conn.execute("""
        SELECT id, nome, dono, formato, cores, total_cartas, status,
               notas, sinergias, linha_de_jogo, substituicoes
        FROM decks WHERE id = ?
    """, (deck_id,)).fetchone()
    rows = conn.execute("""
        SELECT dc.nome, dc.quantidade, dc.secao,
               CASE WHEN dc.card_id IS NOT NULL THEN 1 ELSE 0 END AS in_col,
               c.thumb, c.tipo, c.subtipo, c.cor, c.custo_mana,
               c.poder_resist, c.raridade, c.edicao, c.texto
        FROM deck_cards dc
        LEFT JOIN cards c ON dc.card_id = c.id
        WHERE dc.deck_id = ?
        ORDER BY dc.secao, dc.nome
    """, (deck_id,)).fetchall()
    conn.close()
    sections: dict = {}
    for row in rows:
        nome, qty, secao, in_col, thumb, tipo, subtipo, cor, custo, pr, raridade, edicao, texto = row
        key = secao if secao in ("criaturas", "feiticos", "terrenos") else "feiticos"
        sections.setdefault(key, []).append({
            "nome": nome, "qty": qty, "in_col": bool(in_col), "thumb": thumb,
            "tipo": tipo, "subtipo": subtipo, "cor": cor, "custo": custo,
            "pr": pr, "raridade": raridade, "edicao": edicao, "texto": texto,
        })
    return deck, sections


def open_file(path: Path):
    """Open a local file with the default Windows application."""
    if path.is_file():
        os.startfile(str(path.resolve()))
    else:
        import subprocess
        subprocess.Popen(["explorer", str(path.resolve())])


receiver_proc: subprocess.Popen | None = None


def main(page: ft.Page):
    global receiver_proc

    page.title             = "MTG Card Scanner"
    page.theme_mode        = ft.ThemeMode.DARK
    page.bgcolor           = "#1a1a2e"
    page.window.width      = 980
    page.window.height     = 740
    page.window.min_width  = 820
    page.window.min_height = 560

    ACCENT = "#e94560"
    PANEL  = "#16213e"

    def panel(content, expand=False, width=None) -> ft.Container:
        return ft.Container(
            content=content, bgcolor=PANEL, border_radius=12, padding=24,
            expand=expand, width=width
        )

    snack = ft.SnackBar(content=ft.Text(""))
    page.overlay.append(snack)

    def show_snack(msg: str):
        snack.content = ft.Text(msg)
        snack.open    = True
        page.update()

    # -------------------------------------------------------------------------
    # TAB 1 -- GALERIA
    # -------------------------------------------------------------------------
    g_search      = ft.TextField(hint_text="Pesquisar nome, tipo, cor, edicao...",
                                  border_color="#0f3460", expand=True, height=44,
                                  text_size=13, on_change=lambda e: apply_gallery_filter())
    g_count       = ft.Text("", size=12, color="#888888")
    g_filter_row  = ft.Row(wrap=True, spacing=6, run_spacing=6)
    g_grid        = ft.GridView(max_extent=130, spacing=6, run_spacing=6,
                                expand=True, child_aspect_ratio=0.72)
    g_busy        = ft.ProgressRing(visible=False, width=20, height=20, stroke_width=3)

    _gallery_dono = [""]   # mutable cell: current owner filter

    # Lightbox (full-size image overlay)
    lb_img = ft.Image(src="", fit="contain", width=680, height=680)
    lb_nome = ft.Text("", size=13, color="#aaaaaa")
    lightbox_dlg = ft.AlertDialog(
        modal=True,
        content=ft.Container(
            content=ft.Column([
                lb_img,
                lb_nome,
            ], spacing=8, horizontal_alignment=ft.CrossAxisAlignment.CENTER),
            bgcolor="#000000", padding=ft.Padding(left=8, top=8, right=8, bottom=4),
        ),
        bgcolor="#000000",
        actions=[
            ft.TextButton("Fechar", on_click=lambda _: (
                setattr(lightbox_dlg, "open", False), page.update()
            )),
        ],
        actions_alignment=ft.MainAxisAlignment.END,
    )
    page.overlay.append(lightbox_dlg)

    def open_lightbox(src: str, nome: str):
        lb_img.src  = src
        lb_nome.value = nome
        lightbox_dlg.open = True
        page.update()

    # Add-to-deck (from gallery)
    _dlg_card   = [None]
    add_deck_dd = ft.Dropdown(label="Deck", expand=True, options=[],
                              border_color="#0f3460")

    def open_add_to_deck_dlg(e):
        if not _dlg_card[0]:
            return
        decks = get_all_decks()
        add_deck_dd.options = [ft.dropdown.Option(key=r[0], text=r[1]) for r in decks]
        add_deck_dd.value   = decks[0][0] if decks else None
        add_deck_dlg.open   = True
        page.update()

    def do_add_to_deck(e=None):
        row = _dlg_card[0]
        if not row or not add_deck_dd.value:
            show_snack("Seleciona um deck.")
            return
        cid, cname, tipo = row[0], row[1], (row[2] or "")
        tipo_l = tipo.lower()
        secao  = ("terrenos"  if any(t in tipo_l for t in ["terreno", "terra", "land"])
                  else "criaturas" if any(t in tipo_l for t in ["criatura", "creature"])
                  else "feiticos")
        deck_id = add_deck_dd.value
        conn = sqlite3.connect(DB_PATH)
        ex = conn.execute(
            "SELECT id FROM deck_cards WHERE deck_id=? AND LOWER(nome)=LOWER(?)",
            (deck_id, cname)
        ).fetchone()
        if ex:
            conn.execute("UPDATE deck_cards SET quantidade=quantidade+1 WHERE id=?", (ex[0],))
        else:
            conn.execute(
                "INSERT INTO deck_cards (deck_id, card_id, nome, quantidade, secao) VALUES (?,?,?,1,?)",
                (deck_id, cid, cname, secao)
            )
        conn.commit()
        conn.close()
        add_deck_dlg.open = False
        show_snack(f"'{cname}' adicionada ao deck!")
        page.update()

    add_deck_dlg = ft.AlertDialog(
        modal=True,
        title=ft.Text("Adicionar ao deck", size=14, color="#eaeaea"),
        content=ft.Container(
            content=add_deck_dd,
            width=320, bgcolor="#16213e", padding=ft.Padding(left=0,top=8,right=0,bottom=0)
        ),
        bgcolor="#16213e",
        actions=[
            ft.TextButton("Cancelar", on_click=lambda _: (
                setattr(add_deck_dlg, "open", False), page.update()
            )),
            ft.ElevatedButton("Adicionar", icon=ft.Icons.ADD,
                style=ft.ButtonStyle(bgcolor=ACCENT, color="#fff"),
                on_click=do_add_to_deck),
        ],
        actions_alignment=ft.MainAxisAlignment.END,
    )
    page.overlay.append(add_deck_dlg)

    # Detail dialog
    dlg_nome   = ft.Text("", size=16, weight=ft.FontWeight.BOLD, color=ACCENT)
    dlg_img    = ft.Image(src="", width=130, height=182, fit="contain", border_radius=6)
    dlg_img_wrap = ft.Container(
        content=dlg_img,
        tooltip="Clica para ampliar",
        ink=True,
        border_radius=6,
    )
    dlg_table  = ft.Column(spacing=4, scroll=ft.ScrollMode.AUTO)
    detail_dlg = ft.AlertDialog(
        modal=False,
        content=ft.Container(
            content=ft.Column([
                dlg_nome,
                ft.Row([
                    dlg_img_wrap,
                    ft.Container(content=dlg_table, expand=True, padding=ft.Padding(left=12, top=0, right=0, bottom=0)),
                ], spacing=0, vertical_alignment=ft.CrossAxisAlignment.START),
            ], spacing=10, scroll=ft.ScrollMode.AUTO),
            width=520, bgcolor="#16213e", border_radius=12, padding=20,
        ),
        bgcolor="#16213e",
        actions=[
            ft.TextButton("+ Deck", icon=ft.Icons.PLAYLIST_ADD,
                on_click=open_add_to_deck_dlg),
            ft.TextButton("Fechar", on_click=lambda _: (
                setattr(detail_dlg, "open", False), page.update()
            )),
        ],
        actions_alignment=ft.MainAxisAlignment.END,
    )
    page.overlay.append(detail_dlg)

    def show_detail(row):
        _dlg_card[0] = row
        _, nome, tipo, subtipo, cor, custo, pr, raridade, edicao, texto, thumb, dono = row
        dlg_nome.value = nome or "?"
        src = thumb.replace("\\", "/") if thumb else ""
        img_src = f"/{src}" if src else ""
        dlg_img.src = img_src
        dlg_img_wrap.on_click = lambda _: open_lightbox(img_src, nome or "?")
        fields = [
            ("Dono",     dono or "—"),
            ("Tipo",     f"{tipo or '—'}{(' — ' + subtipo) if subtipo else ''}"),
            ("Cor",      cor or "—"),
            ("Mana",     custo or "—"),
            ("P/R",      pr or "—"),
            ("Raridade", raridade or "—"),
            ("Edicao",   edicao or "—"),
            ("Texto",    texto or "—"),
        ]
        dlg_table.controls.clear()
        for label, val in fields:
            dlg_table.controls.append(
                ft.Row([
                    ft.Text(label, size=11, color="#888888", width=65),
                    ft.Text(val, size=11, color="#eaeaea", expand=True,
                            max_lines=6 if label == "Texto" else 2,
                            overflow=ft.TextOverflow.ELLIPSIS),
                ], spacing=4, vertical_alignment=ft.CrossAxisAlignment.START)
            )
        detail_dlg.open = True
        page.update()

    def _card_tile(row) -> ft.Container:
        _, nome, tipo, subtipo, cor, custo, pr, raridade, edicao, texto, thumb, dono = row
        src = thumb.replace("\\", "/") if thumb else ""
        img = ft.Image(src=f"/{src}", fit="cover", width=112, height=156,
                       border_radius=ft.BorderRadius(top_left=6, top_right=6,
                                                     bottom_left=0, bottom_right=0),
                       error_content=ft.Container(
                           bgcolor="#0f3460", width=112, height=156,
                           content=ft.Text("?", color="#555", size=18,
                                           text_align=ft.TextAlign.CENTER),
                           alignment=ft.Alignment(0, 0)
                       ))
        tile = ft.Container(
            content=ft.Column([
                img,
                ft.Container(
                    content=ft.Column([
                        ft.Text(nome or "?", size=9, color="#eaeaea",
                                max_lines=2, overflow=ft.TextOverflow.ELLIPSIS),
                        ft.Text(tipo or "", size=8, color="#888888",
                                max_lines=1, overflow=ft.TextOverflow.ELLIPSIS),
                    ], spacing=1),
                    padding=ft.Padding(left=4, top=3, right=4, bottom=3)
                ),
            ], spacing=0),
            bgcolor="#16213e", border_radius=6,
            border=ft.Border(
                left=ft.BorderSide(1, "#0f3460"), right=ft.BorderSide(1, "#0f3460"),
                top=ft.BorderSide(1, "#0f3460"),  bottom=ft.BorderSide(1, "#0f3460")
            ),
            on_click=lambda _, r=row: show_detail(r),
            ink=True,
        )
        return tile

    _gallery_gen = [0]  # cancel stale gallery loads

    async def _apply_gallery_async():
        gen = _gallery_gen[0] = _gallery_gen[0] + 1
        search = g_search.value.strip() if g_search.value else ""
        dono   = _gallery_dono[0]
        rows   = await asyncio.to_thread(get_all_cards, search, dono)
        if _gallery_gen[0] != gen:
            return
        tiles = await asyncio.to_thread(lambda: [_card_tile(r) for r in rows])
        if _gallery_gen[0] != gen:
            return
        g_count.value = f"{len(rows)} carta(s)"
        g_grid.controls.clear()
        g_grid.controls.extend(tiles)
        page.update()

    def apply_gallery_filter():
        page.run_task(_apply_gallery_async)

    async def _refresh_galeria_async(e=None):
        s = await asyncio.to_thread(get_stats)
        owners = [d for d, _ in s["by_owner"]]
        g_filter_row.controls.clear()
        for label, val in [("Todos", "")] + [(o, o) for o in owners]:
            count = s["total"] if val == "" else next(
                (c for d, c in s["by_owner"] if d == val), 0)
            active = (_gallery_dono[0] == val)
            btn = ft.ElevatedButton(
                f"{label}  {count}",
                style=ft.ButtonStyle(
                    bgcolor=ACCENT if active else "#0f3460",
                    color="#fff"
                ),
                height=32,
            )
            btn.on_click = lambda _, v=val: set_dono_filter(v)
            g_filter_row.controls.append(btn)
        await _apply_gallery_async()

    def refresh_galeria(e=None):
        page.run_task(_refresh_galeria_async)

    def set_dono_filter(val: str):
        _gallery_dono[0] = val
        refresh_galeria()

    def do_rethumb(e):
        g_busy.visible = True
        page.update()
        def _go():
            subprocess.run(
                [sys.executable, "identify_cards.py", "--rethumb"],
                cwd=str(Path(__file__).parent), capture_output=True
            )
            g_busy.visible = False
            page.run_task(_refresh_galeria_async)
        threading.Thread(target=_go, daemon=True).start()

    galeria_tab = ft.Column([
        ft.Row([
            g_search,
            ft.Row([
                ft.OutlinedButton("Exportar HTML", icon=ft.Icons.OPEN_IN_BROWSER,
                    on_click=lambda _: open_file(HTML_PATH), width=150),
                ft.OutlinedButton("Excel", icon=ft.Icons.TABLE_CHART,
                    on_click=lambda _: open_file(XLSX_PATH), width=110),
                ft.Row([
                    ft.OutlinedButton("Actualizar", icon=ft.Icons.REFRESH,
                        on_click=do_rethumb, width=120),
                    g_busy,
                ], spacing=6, vertical_alignment=ft.CrossAxisAlignment.CENTER),
            ], spacing=8),
        ], spacing=12, vertical_alignment=ft.CrossAxisAlignment.CENTER),
        ft.Row([g_filter_row, ft.Container(expand=True), g_count], spacing=8,
               vertical_alignment=ft.CrossAxisAlignment.CENTER),
        ft.Container(content=g_grid, expand=True),
    ], spacing=10, expand=True)

    # -------------------------------------------------------------------------
    # TAB 2 -- ESCANEAR
    # -------------------------------------------------------------------------
    scan_dot    = ft.Container(
        width=12, height=12, bgcolor="#888888",
        border_radius=6,
        margin=ft.Margin(left=0, top=0, right=6, bottom=0)
    )
    scan_status = ft.Text("Servidor parado", color="#888888", size=14)
    scan_count  = ft.Text("0 foto(s)", size=36, weight=ft.FontWeight.BOLD, color=ACCENT)
    scan_start  = ft.ElevatedButton(
        "Iniciar", icon=ft.Icons.PLAY_CIRCLE_FILL,
        style=ft.ButtonStyle(bgcolor="#27ae60", color="#fff"), width=160
    )
    scan_stop   = ft.ElevatedButton(
        "Parar", icon=ft.Icons.STOP_CIRCLE,
        style=ft.ButtonStyle(bgcolor="#c0392b", color="#fff"), width=160, visible=False
    )
    # Wrap in container — replace content to force image redraw
    scan_thumb_wrap = ft.Container(
        width=210, height=150, border_radius=6, bgcolor="#0a0a1a",
        content=ft.Text("Aguardando\nfoto...", color="#555555", size=11,
                        text_align=ft.TextAlign.CENTER),
        alignment=ft.Alignment(0, 0)
    )
    scan_fname  = ft.Text("", size=10, color="#555555",
                          max_lines=1, overflow=ft.TextOverflow.ELLIPSIS, width=210)
    scan_list   = ft.ListView(spacing=2, auto_scroll=True, expand=True)
    scan_session_files: list = []

    async def _poll_receiver():
        last_seen  = ""
        last_count = -1
        while True:
            await asyncio.sleep(1)
            if receiver_proc is None or receiver_proc.poll() is not None:
                continue
            try:
                data = await asyncio.to_thread(
                    lambda: json.loads(
                        urlopen(f"http://127.0.0.1:{RECEIVER_PORT}/status", timeout=1).read()
                    )
                )
                changed = False
                new_count = data["received"]
                if new_count != last_count:
                    scan_count.value = f"{new_count} foto(s)"
                    last_count = new_count
                    changed = True
                lf = data.get("last_file", "")
                if lf and lf != last_seen:
                    last_seen = lf
                    scan_thumb_wrap.content = ft.Image(
                        src=f"http://127.0.0.1:{RECEIVER_PORT}/scans/thumbs/{lf}",
                        width=210, height=150, fit="contain", border_radius=6
                    )
                    scan_fname.value = lf
                    scan_session_files.append(lf)
                    scan_list.controls.append(
                        ft.Text(f"  {len(scan_session_files):>3}.  {lf}",
                                size=11, font_family="Courier New", color="#aaaaaa")
                    )
                    changed = True
                if changed:
                    page.update()
            except Exception:
                pass

    page.run_task(_poll_receiver)

    def start_receiver(e):
        global receiver_proc
        scan_session_files.clear()
        scan_list.controls.clear()
        receiver_proc = subprocess.Popen(
            [sys.executable, "receiver.py"],
            cwd=str(Path(__file__).parent)
        )
        scan_dot.bgcolor   = "#27ae60"
        scan_status.value  = f"A correr -- porta {RECEIVER_PORT}"
        scan_status.color  = "#27ae60"
        scan_start.visible = False
        scan_stop.visible  = True
        scan_count.value   = "0 foto(s)"
        scan_thumb_wrap.content = ft.Text("Aguardando\nfoto...", color="#555555", size=11,
                                          text_align=ft.TextAlign.CENTER)
        page.update()

    def stop_receiver(e):
        global receiver_proc
        if receiver_proc:
            receiver_proc.terminate()
            receiver_proc = None
        scan_dot.bgcolor   = "#888888"
        scan_status.value  = "Servidor parado"
        scan_status.color  = "#888888"
        scan_start.visible = True
        scan_stop.visible  = False
        page.update()

    scan_start.on_click = start_receiver
    scan_stop.on_click  = stop_receiver

    escanear_tab = ft.Column([
        ft.Row([
            panel(
                ft.Column([
                    ft.Row([scan_dot, scan_status],
                           vertical_alignment=ft.CrossAxisAlignment.CENTER),
                    ft.Container(height=6),
                    scan_count,
                    ft.Container(height=16),
                    ft.Row([scan_start, scan_stop], spacing=12),
                    ft.Container(height=16),
                    ft.Text("Fotos desta sessao:", size=12, color="#888888"),
                    scan_list,
                ], spacing=4, expand=True),
                expand=True
            ),
            panel(
                ft.Column([
                    scan_thumb_wrap,
                    scan_fname,
                ], spacing=6, horizontal_alignment=ft.CrossAxisAlignment.CENTER),
                width=260
            ),
        ], spacing=12, expand=True),
        ft.Text(
            "Tip: depois de escanear vai ao separador Processar para identificar as cartas com IA.",
            size=12, color="#555555", italic=True
        ),
    ], spacing=12, expand=True)

    # -------------------------------------------------------------------------
    # TAB 3 -- PROCESSAR
    # -------------------------------------------------------------------------
    proc_pending   = ft.Text("-- por processar", size=22, weight=ft.FontWeight.BOLD, color=ACCENT)
    proc_tf        = ft.TextField(
        label="Dono das cartas", hint_text="ex: Vicente",
        border_color="#0f3460", expand=True
    )
    proc_chips     = ft.Row(wrap=True, spacing=6, run_spacing=6)
    proc_btn       = ft.ElevatedButton(
        "Processar agora", icon=ft.Icons.AUTO_FIX_HIGH,
        style=ft.ButtonStyle(bgcolor=ACCENT, color="#fff"), width=220
    )
    proc_bar       = ft.ProgressBar(visible=False, color=ACCENT, value=0)
    proc_counter   = ft.Text("", size=13, color="#888888", visible=False)
    proc_card_name = ft.Text("", size=13, color="#eaeaea", visible=False,
                             weight=ft.FontWeight.W_500)
    proc_log       = ft.ListView(height=160, spacing=1, auto_scroll=True)
    proc_running   = False

    def refresh_processar(e=None):
        s = get_stats()
        proc_pending.value = f"{s['pending']} foto(s) por processar"
        owners = get_owners()
        proc_chips.controls.clear()
        for o in owners:
            btn = ft.ElevatedButton(
                o, height=32,
                style=ft.ButtonStyle(bgcolor="#0f3460", color="#eaeaea")
            )
            btn.on_click = lambda _, name=o: (
                setattr(proc_tf, "value", name), page.update()
            )
            proc_chips.controls.append(btn)
        page.update()

    def run_processar(e):
        nonlocal proc_running
        if proc_running:
            return
        dono = proc_tf.value.strip()
        if not dono:
            show_snack("Escreve o nome do dono das cartas.")
            return
        proc_running         = True
        proc_btn.disabled    = True
        proc_bar.visible     = True
        proc_bar.value       = 0
        proc_counter.visible = True
        proc_counter.value   = "A iniciar..."
        proc_card_name.visible = True
        proc_card_name.value   = ""
        proc_log.controls.clear()
        page.update()

        def _do():
            nonlocal proc_running
            env = {**os.environ}
            env_data = read_env()
            if "ANTHROPIC_API_KEY" in env_data:
                env["ANTHROPIC_API_KEY"] = env_data["ANTHROPIC_API_KEY"]
            p = subprocess.Popen(
                [sys.executable, "-u", "identify_cards.py", "--dono", dono, "--gui"],
                cwd=str(Path(__file__).parent),
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace", env=env
            )
            _last_ui = [0.0]
            for raw in p.stdout:
                clean = strip_ansi(raw.rstrip())
                if not clean:
                    continue
                if clean.startswith("PROG:"):
                    _, counts, names = clean.split(":", 2)
                    done, total = counts.split("/")
                    left, _, right = names.partition("|")
                    proc_bar.value       = int(done) / max(int(total), 1)
                    proc_counter.value   = f"{done} / {total} imagens"
                    proc_card_name.value = f"{left.strip()}  |  {right.strip()}"
                else:
                    proc_log.controls.append(
                        ft.Text(clean, size=11, font_family="Courier New", color="#888888")
                    )
                now = time.time()
                if now - _last_ui[0] >= 0.15:
                    page.update()
                    _last_ui[0] = now
            p.wait()
            proc_running           = False
            proc_btn.disabled      = False
            proc_bar.visible       = False
            proc_counter.visible   = False
            proc_card_name.visible = False
            refresh_processar()
            refresh_galeria()

        threading.Thread(target=_do, daemon=True).start()

    proc_btn.on_click = run_processar

    processar_tab = ft.Column([
        ft.Row([
            panel(
                ft.Column([
                    proc_pending,
                    ft.Container(height=12),
                    ft.Text("Dono das cartas neste lote:", size=13, color="#888888"),
                    ft.Row([proc_tf]),
                    ft.Container(height=4),
                    ft.Text("Clica num nome:", size=11, color="#555555"),
                    proc_chips,
                    ft.Container(height=16),
                    proc_btn,
                    ft.Container(height=8),
                    proc_bar,
                    ft.Container(height=4),
                    proc_counter,
                    proc_card_name,
                ], spacing=4),
                expand=True
            ),
        ]),
        panel(
            ft.Column([
                ft.Text("Info:", size=12, color="#888888"),
                proc_log,
            ], spacing=6, expand=True),
            expand=True
        ),
    ], spacing=12, expand=True)

    # -------------------------------------------------------------------------
    # TAB 4 -- EXPORTAR
    # -------------------------------------------------------------------------
    exp_dd     = ft.Dropdown(
        label="Dono", width=260,
        options=[ft.dropdown.Option("Todos")], value="Todos"
    )
    exp_status = ft.Text("", size=13)
    exp_busy   = ft.ProgressRing(visible=False, width=22, height=22, stroke_width=3)

    def refresh_exportar(e=None):
        owners = get_owners()
        exp_dd.options = [ft.dropdown.Option("Todos")] + [
            ft.dropdown.Option(o) for o in owners
        ]
        page.update()

    def do_export(mode: str):
        exp_status.value = "A exportar..."
        exp_status.color = "#888888"
        exp_busy.visible = True
        page.update()

        def _do():
            dono_val = exp_dd.value
            if mode == "html_xlsx":
                args = [sys.executable, "identify_cards.py", "--rethumb"]
            else:
                args = [sys.executable, "export_ai.py"]
                if dono_val and dono_val != "Todos":
                    args += ["--dono", dono_val]
            r = subprocess.run(
                args, cwd=str(Path(__file__).parent),
                capture_output=True, text=True, encoding="utf-8", errors="replace"
            )
            exp_busy.visible = False
            if r.returncode == 0:
                exp_status.value = "Exportacao concluida!"
                exp_status.color = "#27ae60"
            else:
                exp_status.value = f"Erro (codigo {r.returncode})"
                exp_status.color = "#e94560"
            page.update()

        threading.Thread(target=_do, daemon=True).start()

    exportar_tab = ft.Column([
        panel(
            ft.Column([
                ft.Text("Exportar coleccao", size=18, weight=ft.FontWeight.BOLD),
                ft.Container(height=16),
                exp_dd,
                ft.Container(height=20),
                ft.Row([
                    ft.ElevatedButton(
                        "HTML + Excel", icon=ft.Icons.TABLE_CHART,
                        on_click=lambda _: do_export("html_xlsx"),
                        style=ft.ButtonStyle(bgcolor="#0f3460", color="#eaeaea"), width=200
                    ),
                    ft.ElevatedButton(
                        "AI -- CSV + TXT", icon=ft.Icons.SMART_TOY,
                        on_click=lambda _: do_export("ai"),
                        style=ft.ButtonStyle(bgcolor=ACCENT, color="#fff"), width=200
                    ),
                ], spacing=12),
                ft.Container(height=16),
                ft.Row([
                    ft.OutlinedButton(
                        "Abrir Galeria", icon=ft.Icons.OPEN_IN_BROWSER,
                        on_click=lambda _: webbrowser.open(HTML_PATH.resolve().as_uri()),
                        width=200
                    ),
                    ft.OutlinedButton(
                        "Abrir Excel", icon=ft.Icons.FILE_OPEN,
                        on_click=lambda _: os.startfile(str(XLSX_PATH.resolve())),
                        width=200
                    ),
                ], spacing=12),
                ft.Container(height=16),
                ft.Row(
                    [exp_busy, exp_status], spacing=10,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER
                ),
            ], spacing=6),
            expand=True
        ),
    ], scroll=ft.ScrollMode.AUTO, spacing=12, expand=True)

    # -------------------------------------------------------------------------
    # TAB 5 -- CONFIG
    # -------------------------------------------------------------------------
    env_now = read_env()
    cfg_api = ft.TextField(
        label="Chave API Anthropic",
        value=env_now.get("ANTHROPIC_API_KEY", ""),
        password=True, can_reveal_password=True,
        hint_text="sk-ant-api03-...",
        border_color="#0f3460", expand=True
    )
    cfg_status = ft.Text("", size=13)

    def save_config(e):
        data = read_env()
        key  = cfg_api.value.strip()
        if key:
            data["ANTHROPIC_API_KEY"] = key
        write_env(data)
        cfg_status.value = "Guardado!"
        cfg_status.color = "#27ae60"
        page.update()

    config_tab = ft.Column([
        panel(
            ft.Column([
                ft.Text("Configuracao", size=18, weight=ft.FontWeight.BOLD),
                ft.Container(height=16),
                ft.Text("Chave API Anthropic", size=13, weight=ft.FontWeight.W_600),
                ft.Text(
                    "Obtem em console.anthropic.com -- API Keys",
                    size=12, color="#888888"
                ),
                ft.Container(height=8),
                ft.Row([cfg_api]),
                ft.Container(height=16),
                ft.ElevatedButton(
                    "Guardar", icon=ft.Icons.SAVE,
                    on_click=save_config,
                    style=ft.ButtonStyle(bgcolor=ACCENT, color="#fff"), width=160
                ),
                ft.Container(height=8),
                cfg_status,
            ], spacing=6)
        ),
    ], scroll=ft.ScrollMode.AUTO, spacing=12, expand=True)

    # -------------------------------------------------------------------------
    # TAB 6 -- DECKS
    # -------------------------------------------------------------------------
    _SEC_LABELS = {
        "criaturas": "CRIATURAS",
        "feiticos":  "FEITIÇOS E ENCANTAMENTOS",
        "terrenos":  "TERRENOS",
    }
    _COL_BG = {"W": "#7a6040", "U": "#1e3a8a", "B": "#333333",
               "R": "#7a1a1a", "G": "#1a5c1a"}

    _current_deck_id = [""]

    # ── new deck dialog ────────────────────────────────────────────────────────
    nd_name   = ft.TextField(label="Nome do deck", border_color="#0f3460", expand=True)
    nd_dono   = ft.TextField(label="Dono",  border_color="#0f3460", width=150)
    nd_fmt    = ft.TextField(label="Formato", border_color="#0f3460", width=150)
    nd_status = ft.Dropdown(
        label="Status", width=130, border_color="#0f3460",
        options=[
            ft.dropdown.Option("proposed"),
            ft.dropdown.Option("active"),
            ft.dropdown.Option("archived"),
        ],
        value="proposed",
    )
    _nd_sel_colors: list = []
    nd_colors_row = ft.Row(spacing=6)

    def _nd_toggle_color(c, btn):
        if c in _nd_sel_colors:
            _nd_sel_colors.remove(c)
            btn.bgcolor = "#1a1a2e"
        else:
            _nd_sel_colors.append(c)
            btn.bgcolor = _COL_BG.get(c, "#555555")
        btn.update()

    for _nc in ("W", "U", "B", "R", "G"):
        _nbtn = ft.ElevatedButton(
            _nc, data=_nc, bgcolor="#1a1a2e",
            style=ft.ButtonStyle(color="#ffffff"),
        )
        _nbtn.on_click = (lambda b, c=_nc: lambda e: _nd_toggle_color(c, b))(_nbtn)
        nd_colors_row.controls.append(_nbtn)

    new_deck_dlg = ft.AlertDialog(
        modal=True,
        title=ft.Text("Novo Deck"),
        content=ft.Column([
            ft.Row([nd_name], spacing=8),
            ft.Row([nd_dono, nd_fmt, nd_status], spacing=8),
            ft.Row([ft.Text("Cores:", size=12, color="#aaa"), nd_colors_row], spacing=8),
        ], spacing=12, tight=True, width=480),
        bgcolor="#16213e",
    )

    def open_new_deck_dlg(e=None):
        nd_name.value   = ""
        nd_dono.value   = ""
        nd_fmt.value    = ""
        nd_status.value = "proposed"
        _nd_sel_colors.clear()
        for btn in nd_colors_row.controls:
            btn.bgcolor = "#1a1a2e"
            btn.update()
        new_deck_dlg.open = True
        page.update()

    def create_new_deck(e=None):
        nm = (nd_name.value or "").strip()
        if not nm:
            show_snack("O deck precisa de um nome.")
            return
        deck_id = f"manual_{int(time.time())}"
        conn = sqlite3.connect(DB_PATH)
        _deck_mod.ensure_deck_schema(conn)
        conn.execute("""
            INSERT OR REPLACE INTO decks
                (id, nome, dono, formato, cores, arquetipo, total_cartas,
                 status, criado_em, notas, sinergias, linha_de_jogo, substituicoes)
            VALUES (?, ?, ?, ?, ?, '', 0, ?, ?, '', '', '', '')
        """, (deck_id, nm, nd_dono.value.strip(), nd_fmt.value.strip(),
              ",".join(_nd_sel_colors), nd_status.value or "proposed",
              time.strftime("%Y-%m-%d")))
        conn.commit()
        conn.close()
        new_deck_dlg.open = False
        page.update()
        show_deck_detail(deck_id)

    new_deck_dlg.actions = [
        ft.ElevatedButton("Criar", icon=ft.Icons.ADD,
            style=ft.ButtonStyle(bgcolor=ACCENT, color="#fff"),
            on_click=create_new_deck),
        ft.TextButton("Cancelar", on_click=lambda _: [
            setattr(new_deck_dlg, "open", False), page.update()]),
    ]
    page.overlay.append(new_deck_dlg)

    # ── add-card-to-deck dialog ────────────────────────────────────────────────
    _acd_search = ft.TextField(label="Pesquisar carta", border_color="#0f3460", expand=True)
    _acd_lv     = ft.ListView(spacing=4, height=260)

    def _refresh_acd(e=None):
        q = (_acd_search.value or "").strip()
        rows = get_all_cards(search=q) if q else []
        _acd_lv.controls.clear()
        for row in rows[:40]:
            c_id, nm = row[0], row[1]
            _acd_lv.controls.append(ft.Container(
                content=ft.Text(nm, size=12, color="#eaeaea"),
                bgcolor="#16213e", border_radius=4,
                padding=ft.Padding(left=8, top=4, right=8, bottom=4),
                ink=True,
                on_click=(lambda r: lambda e: _acd_add(r))(row),
            ))
        page.update()

    _acd_search.on_change = _refresh_acd

    def _acd_add(card_row):
        c_id, nm, tipo = card_row[0], card_row[1], card_row[2]
        did = _current_deck_id[0]
        if not did:
            return
        conn = sqlite3.connect(DB_PATH)
        _deck_mod.ensure_deck_schema(conn)
        existing = conn.execute(
            "SELECT id FROM deck_cards WHERE deck_id=? AND LOWER(nome)=LOWER(?)",
            (did, nm)
        ).fetchone()
        if existing:
            conn.execute("UPDATE deck_cards SET quantidade=quantidade+1 WHERE id=?",
                         (existing[0],))
        else:
            sec = ("criaturas" if tipo and "criatura" in tipo.lower() else
                   "terrenos"  if tipo and "terreno"  in tipo.lower() else "feiticos")
            conn.execute(
                "INSERT INTO deck_cards (deck_id, card_id, nome, quantidade, secao) VALUES (?,?,?,1,?)",
                (did, c_id, nm, sec)
            )
        conn.commit()
        conn.close()
        add_card_dlg.open = False
        page.update()
        show_deck_detail(did)

    def open_add_card_dlg(e=None):
        _acd_search.value = ""
        _acd_lv.controls.clear()
        add_card_dlg.open = True
        page.update()

    add_card_dlg = ft.AlertDialog(
        modal=True,
        title=ft.Text("Adicionar carta ao deck"),
        content=ft.Column([_acd_search, _acd_lv], spacing=8, tight=True, width=420),
        actions=[
            ft.TextButton("Fechar", on_click=lambda _: [
                setattr(add_card_dlg, "open", False), page.update()]),
        ],
        bgcolor="#16213e",
    )
    page.overlay.append(add_card_dlg)

    decks_content = ft.Container(expand=True)

    def _color_chip(c):
        return ft.Container(
            content=ft.Text(c, size=10, color="#ffffff"),
            bgcolor=_COL_BG.get(c, "#555555"), border_radius=4,
            padding=ft.Padding(left=6, top=2, right=6, bottom=2)
        )

    def show_deck_list():
        rows = get_all_decks()
        top = ft.Row([
            ft.ElevatedButton(
                "Novo Deck", icon=ft.Icons.ADD,
                style=ft.ButtonStyle(bgcolor="#1a5c1a", color="#fff"),
                on_click=open_new_deck_dlg
            ),
            ft.ElevatedButton(
                "Importar Deck (.md)", icon=ft.Icons.UPLOAD_FILE,
                style=ft.ButtonStyle(bgcolor=ACCENT, color="#fff"),
                on_click=import_deck_from_file
            ),
            ft.OutlinedButton("Actualizar", icon=ft.Icons.REFRESH,
                on_click=lambda _: show_deck_list()),
        ], spacing=12)

        if not rows:
            body = ft.Container(
                content=ft.Text(
                    "Nenhum deck importado ainda.\nClica em 'Importar Deck (.md)' para começar.",
                    color="#888888", text_align=ft.TextAlign.CENTER, size=14
                ),
                alignment=ft.Alignment(0, 0), expand=True
            )
        else:
            lv = ft.ListView(spacing=8, expand=True)
            for id_, nome, dono, formato, cores, n_lin, n_mat in rows:
                pct = n_mat / max(n_lin, 1)
                chips = ft.Row(
                    [_color_chip(c.strip()) for c in (cores or "").split(",") if c.strip()],
                    spacing=4
                )
                tile = ft.Container(
                    content=ft.Column([
                        ft.Row([
                            ft.Text(nome, size=14, weight=ft.FontWeight.BOLD,
                                    color="#eaeaea", expand=True),
                            ft.Text(f"{n_mat}/{n_lin} na coleção",
                                    size=12, color=ACCENT),
                        ], spacing=8),
                        ft.Text(f"Dono: {dono or '—'}  |  {formato or '—'}",
                                size=12, color="#888888"),
                        chips,
                        ft.ProgressBar(value=pct, color=ACCENT, bgcolor="#0a0a1a"),
                    ], spacing=4),
                    bgcolor="#16213e", border_radius=8,
                    border=ft.Border(
                        left=ft.BorderSide(1, "#0f3460"), right=ft.BorderSide(1, "#0f3460"),
                        top=ft.BorderSide(1, "#0f3460"),  bottom=ft.BorderSide(1, "#0f3460")
                    ),
                    padding=16, ink=True,
                    on_click=lambda _, did=id_: show_deck_detail(did),
                )
                lv.controls.append(tile)
            body = lv

        decks_content.content = ft.Column([top, body], spacing=12, expand=True)
        page.update()

    def show_deck_detail(deck_id):
        _current_deck_id[0] = deck_id
        deck_row, sections = get_deck_full(deck_id)
        if not deck_row:
            show_snack("Deck não encontrado.")
            return

        _, nome, dono, formato, cores, total_c, status, notas, sinergias, linha_jogo, subs = deck_row

        _all:  list = []
        _items: list = []
        _sel   = [0]

        for sec in ["criaturas", "feiticos", "terrenos"]:
            for card in sections.get(sec, []):
                _all.append({**card, "secao": sec})

        n_match = sum(1 for c in _all if c["in_col"])
        n_total = len(_all)

        # ── image viewer (right panel) ──────────────────────────────────────
        img_wrap    = ft.Container(width=300, height=420, bgcolor="#0a0a1a",
                                   border_radius=6, alignment=ft.Alignment(0, 0),
                                   content=ft.Text("Seleciona uma carta", color="#555",
                                                   size=12, text_align=ft.TextAlign.CENTER))
        img_label   = ft.Text("", size=12, color="#aaaaaa",
                              text_align=ft.TextAlign.CENTER, max_lines=2)
        nav_counter = ft.Text(f"0/{len(_all)}", size=11, color="#555555")

        # card details readout
        d_tipo     = ft.Text("—", size=10, color="#aaaaaa", expand=True)
        d_subtipo  = ft.Text("—", size=10, color="#aaaaaa", expand=True)
        d_cor      = ft.Text("—", size=10, color="#aaaaaa", expand=True)
        d_custo    = ft.Text("—", size=10, color="#aaaaaa", expand=True)
        d_pr       = ft.Text("—", size=10, color="#aaaaaa", expand=True)
        d_raridade = ft.Text("—", size=10, color="#aaaaaa", expand=True)
        d_edicao   = ft.Text("—", size=10, color="#aaaaaa", expand=True)
        d_texto    = ft.Text("—", size=10, color="#aaaaaa", expand=True, max_lines=5)

        def _lbl(label): return ft.Text(label, size=10, color="#555555",
                                        width=62, weight=ft.FontWeight.W_600)

        card_details = ft.Container(
            content=ft.Column([
                ft.Row([_lbl("Tipo:"),     d_tipo],     spacing=4),
                ft.Row([_lbl("Subtipo:"),  d_subtipo],  spacing=4),
                ft.Row([_lbl("Cor:"),      d_cor],      spacing=4),
                ft.Row([_lbl("Mana:"),     d_custo],    spacing=4),
                ft.Row([_lbl("P/R:"),      d_pr],       spacing=4),
                ft.Row([_lbl("Raridade:"), d_raridade], spacing=4),
                ft.Row([_lbl("Edição:"),   d_edicao],   spacing=4),
                ft.Row([_lbl("Texto:"),    d_texto],    spacing=4,
                       vertical_alignment=ft.CrossAxisAlignment.START),
            ], spacing=3),
            bgcolor="#0a0a1a", border_radius=6,
            padding=ft.Padding(left=8, top=6, right=8, bottom=6),
            width=300,
        )

        def _fill_details(card):
            d_tipo.value     = card.get("tipo")    or "—"
            d_subtipo.value  = card.get("subtipo") or "—"
            d_cor.value      = card.get("cor")     or "—"
            d_custo.value    = card.get("custo")   or "—"
            d_pr.value       = card.get("pr")      or "—"
            d_raridade.value = card.get("raridade") or "—"
            d_edicao.value   = card.get("edicao")  or "—"
            d_texto.value    = card.get("texto")   or "—"

        def select_card(idx):
            if not _all:
                return
            old     = _sel[0]
            _sel[0] = idx % len(_all)
            if 0 <= old < len(_items):
                _items[old].bgcolor = "transparent"
            if 0 <= _sel[0] < len(_items):
                _items[_sel[0]].bgcolor = "#1f3060"
            card = _all[_sel[0]]
            nm, qty, th = card["nome"], card["qty"], card.get("thumb")
            img_wrap.content = ft.Image(
                src="/" + th.replace("\\", "/") if th else "",
                fit="contain", width=300, height=420, border_radius=6,
                error_content=ft.Container(
                    bgcolor="#0a0a1a", width=300, height=420,
                    content=ft.Text("Sem foto", color="#555", size=12,
                                    text_align=ft.TextAlign.CENTER),
                    alignment=ft.Alignment(0, 0)
                )
            )
            img_label.value   = f"{qty}×  {nm}"
            nav_counter.value = f"{_sel[0]+1}/{len(_all)}"
            _fill_details(card)
            page.update()

        # ── card list (left panel) ──────────────────────────────────────────
        list_lv = ft.ListView(expand=True, spacing=1)

        def remove_card(nm):
            conn = sqlite3.connect(DB_PATH)
            conn.execute("""
                DELETE FROM deck_cards WHERE id = (
                    SELECT id FROM deck_cards WHERE deck_id=? AND nome=? LIMIT 1
                )
            """, (deck_id, nm))
            conn.commit()
            conn.close()
            show_deck_detail(deck_id)

        def build_list():
            _items.clear()
            list_lv.controls.clear()
            cur_sec = None
            for i, card in enumerate(_all):
                nm, qty, ic, sec = card["nome"], card["qty"], card["in_col"], card["secao"]
                if sec != cur_sec:
                    cur_sec = sec
                    ic_c = sum(1 for c in _all if c["secao"] == sec and c["in_col"])
                    tot  = sum(1 for c in _all if c["secao"] == sec)
                    list_lv.controls.append(ft.Container(
                        content=ft.Text(
                            f"  {_SEC_LABELS[sec]}  ({ic_c}/{tot})",
                            size=11, weight=ft.FontWeight.BOLD, color="#888888"
                        ),
                        padding=ft.Padding(left=0, top=8, right=0, bottom=2)
                    ))
                item = ft.Container(
                    content=ft.Row([
                        ft.Text("✓" if ic else "○", size=12,
                                color="#27ae60" if ic else "#555555", width=18),
                        ft.Text(f"{qty}×", size=11, color="#888888", width=24),
                        ft.Text(nm, size=11,
                                color="#eaeaea" if ic else "#666666", expand=True),
                        ft.IconButton(ft.Icons.CLOSE, icon_size=14, icon_color="#555555",
                            tooltip="Remover do deck",
                            on_click=lambda _, n=nm: remove_card(n)),
                    ], spacing=2, vertical_alignment=ft.CrossAxisAlignment.CENTER),
                    bgcolor="transparent", border_radius=4,
                    padding=ft.Padding(left=6, top=2, right=2, bottom=2),
                    on_click=lambda _, idx=i: select_card(idx),
                    ink=True,
                )
                _items.append(item)
                list_lv.controls.append(item)

        build_list()

        # ── info panel (expandable + editable) ─────────────────────────────
        _info_open = [False]

        t_desc = ft.Text(notas      or "—", size=12, color="#aaaaaa")
        t_sin  = ft.Text(sinergias  or "—", size=12, color="#aaaaaa")
        t_lj   = ft.Text(linha_jogo or "—", size=12, color="#aaaaaa")
        t_sub  = ft.Text(subs       or "—", size=12, color="#aaaaaa")

        f_desc = ft.TextField(value=notas      or "", multiline=True, min_lines=2,
                              max_lines=5, border_color="#0f3460", text_size=12,
                              expand=True, visible=False)
        f_sin  = ft.TextField(value=sinergias  or "", multiline=True, min_lines=2,
                              max_lines=5, border_color="#0f3460", text_size=12,
                              expand=True, visible=False)
        f_lj   = ft.TextField(value=linha_jogo or "", multiline=True, min_lines=2,
                              max_lines=5, border_color="#0f3460", text_size=12,
                              expand=True, visible=False)
        f_sub  = ft.TextField(value=subs       or "", multiline=True, min_lines=2,
                              max_lines=5, border_color="#0f3460", text_size=12,
                              expand=True, visible=False)

        save_btn   = ft.ElevatedButton("Guardar", icon=ft.Icons.SAVE,
                         style=ft.ButtonStyle(bgcolor=ACCENT, color="#fff"), visible=False)
        edit_btn   = ft.TextButton("Editar", icon=ft.Icons.EDIT, visible=False)
        toggle_btn = ft.TextButton("Ver informação", icon=ft.Icons.EXPAND_MORE)

        info_body  = ft.Column([
            ft.Text("Descrição:",    size=11, color="#555555", weight=ft.FontWeight.W_600),
            t_desc, f_desc,
            ft.Text("Sinergias:",    size=11, color="#555555", weight=ft.FontWeight.W_600),
            t_sin, f_sin,
            ft.Text("Linha de Jogo:", size=11, color="#555555", weight=ft.FontWeight.W_600),
            t_lj, f_lj,
            ft.Text("Substituições:", size=11, color="#555555", weight=ft.FontWeight.W_600),
            t_sub, f_sub,
        ], spacing=4, visible=False)

        def toggle_info(e=None):
            _info_open[0] = not _info_open[0]
            info_body.visible  = _info_open[0]
            toggle_btn.text    = "Ocultar informação" if _info_open[0] else "Ver informação"
            toggle_btn.icon    = ft.Icons.EXPAND_LESS if _info_open[0] else ft.Icons.EXPAND_MORE
            edit_btn.visible   = _info_open[0]
            page.update()

        def toggle_edit(e=None):
            em = not f_desc.visible
            f_desc.visible = em;  t_desc.visible = not em
            f_sin.visible  = em;  t_sin.visible  = not em
            f_lj.visible   = em;  t_lj.visible   = not em
            f_sub.visible  = em;  t_sub.visible  = not em
            save_btn.visible = em
            edit_btn.text    = "Cancelar" if em else "Editar"
            page.update()

        def save_info(e=None):
            conn = sqlite3.connect(DB_PATH)
            conn.execute(
                "UPDATE decks SET notas=?,sinergias=?,linha_de_jogo=?,substituicoes=? WHERE id=?",
                (f_desc.value, f_sin.value, f_lj.value, f_sub.value, deck_id)
            )
            conn.commit()
            conn.close()
            t_desc.value = f_desc.value or "—"
            t_sin.value  = f_sin.value  or "—"
            t_lj.value   = f_lj.value   or "—"
            t_sub.value  = f_sub.value  or "—"
            toggle_edit()

        toggle_btn.on_click = toggle_info
        edit_btn.on_click   = toggle_edit
        save_btn.on_click   = save_info

        info_panel = ft.Container(
            content=ft.Column([
                ft.Row([
                    ft.Text("INFORMAÇÃO DO DECK", size=11,
                            weight=ft.FontWeight.BOLD, color="#888888"),
                    ft.Container(expand=True),
                    edit_btn, save_btn, toggle_btn,
                ], spacing=4, vertical_alignment=ft.CrossAxisAlignment.CENTER),
                info_body,
            ], spacing=6),
            bgcolor="#0f1a30", border_radius=8,
            padding=ft.Padding(left=12, top=8, right=12, bottom=8)
        )

        # ── assemble ───────────────────────────────────────────────────────
        image_col = ft.Column([
            img_wrap,
            img_label,
            ft.Row([
                ft.IconButton(ft.Icons.CHEVRON_LEFT, icon_color="#888888",
                    on_click=lambda _: select_card(_sel[0] - 1)),
                ft.Container(expand=True),
                nav_counter,
                ft.Container(expand=True),
                ft.IconButton(ft.Icons.CHEVRON_RIGHT, icon_color="#888888",
                    on_click=lambda _: select_card(_sel[0] + 1)),
            ], vertical_alignment=ft.CrossAxisAlignment.CENTER),
            card_details,
        ], horizontal_alignment=ft.CrossAxisAlignment.CENTER, spacing=6)

        left_col = ft.Column([
            ft.Container(content=list_lv, expand=True),
            ft.ElevatedButton(
                "Adicionar carta", icon=ft.Icons.ADD,
                style=ft.ButtonStyle(bgcolor="#1a3a1a", color="#aaffaa"),
                on_click=open_add_card_dlg,
            ),
        ], expand=True, spacing=6)

        middle = ft.Row([
            left_col,
            ft.Container(width=1, bgcolor="#0f3460"),
            ft.Container(content=image_col,
                         padding=ft.Padding(left=12, top=0, right=0, bottom=0),
                         width=334),
        ], expand=True, spacing=8)

        decks_content.content = ft.Column([
            ft.Row([
                ft.TextButton("< Voltar", icon=ft.Icons.ARROW_BACK,
                    on_click=lambda _: show_deck_list()),
                ft.Text(nome, size=17, weight=ft.FontWeight.BOLD,
                        color="#eaeaea", expand=True),
                ft.Text(f"{n_match}/{n_total} na coleção", size=13, color=ACCENT),
            ], spacing=8, vertical_alignment=ft.CrossAxisAlignment.CENTER),
            ft.Text(f"Dono: {dono or '—'}  |  {formato or '—'}  |  Status: {status or '—'}",
                    size=12, color="#888888"),
            middle,
            info_panel,
        ], spacing=8, expand=True)

        # initial selection
        if _all:
            _sel[0] = 0
            if _items:
                _items[0].bgcolor = "#1f3060"
            card = _all[0]
            nm, qty, th = card["nome"], card["qty"], card.get("thumb")
            img_wrap.content  = ft.Image(
                src="/" + th.replace("\\", "/") if th else "",
                fit="contain", width=300, height=420, border_radius=6,
                error_content=ft.Container(
                    bgcolor="#0a0a1a", width=300, height=420,
                    content=ft.Text("Sem foto", color="#555", size=12,
                                    text_align=ft.TextAlign.CENTER),
                    alignment=ft.Alignment(0, 0)
                )
            )
            img_label.value   = f"{qty}×  {nm}"
            nav_counter.value = f"1/{len(_all)}"
            _fill_details(card)

        page.update()

    def import_deck_from_file(e=None):
        def _do():
            import tkinter as tk
            from tkinter import filedialog
            root = tk.Tk()
            root.withdraw()
            root.wm_attributes("-topmost", 1)
            path_str = filedialog.askopenfilename(
                title="Selecionar deck .md",
                filetypes=[("Markdown", "*.md"), ("Todos", "*.*")]
            )
            root.destroy()
            if not path_str:
                return
            md_path = Path(path_str)
            show_snack(f"A importar {md_path.name}...")
            try:
                d = _deck_mod.parse_deck_md(md_path)
                total, matched = _deck_mod.import_deck(d, DB_PATH)
                show_snack(f"'{d['nome']}' importado — {matched}/{total} na coleção")
            except Exception as ex:
                show_snack(f"Erro ao importar: {ex}")
            show_deck_list()

        threading.Thread(target=_do, daemon=True).start()

    def refresh_decks(e=None):
        show_deck_list()

    decks_tab = ft.Column([decks_content], expand=True)

    # -------------------------------------------------------------------------
    # Navigation -- NavigationBar at bottom, content area above
    # -------------------------------------------------------------------------
    pages = [galeria_tab, escanear_tab, processar_tab, exportar_tab, config_tab, decks_tab]
    _tab_refresh = {0: refresh_galeria, 2: refresh_processar, 3: refresh_exportar, 5: refresh_decks}

    content_area = ft.Container(content=galeria_tab, expand=True, padding=16)

    def switch_tab(idx: int):
        content_area.content = pages[idx]
        _tab_refresh.get(idx, lambda e=None: None)()
        page.update()

    nav_bar = ft.NavigationBar(
        selected_index=0,
        bgcolor="#0f3460",
        on_change=lambda e: switch_tab(e.control.selected_index),
        destinations=[
            ft.NavigationBarDestination(icon=ft.Icons.COLLECTIONS,    label="Galeria"),
            ft.NavigationBarDestination(icon=ft.Icons.CAMERA_ALT,     label="Escanear"),
            ft.NavigationBarDestination(icon=ft.Icons.MEMORY,         label="Processar"),
            ft.NavigationBarDestination(icon=ft.Icons.DOWNLOAD,       label="Exportar"),
            ft.NavigationBarDestination(icon=ft.Icons.SETTINGS,       label="Config"),
            ft.NavigationBarDestination(icon=ft.Icons.STYLE,          label="Decks"),
        ],
    )

    page.add(
        ft.Column([content_area, nav_bar], spacing=0, expand=True)
    )

    refresh_galeria()

    def on_close(e):
        global receiver_proc
        if receiver_proc and receiver_proc.poll() is None:
            receiver_proc.terminate()

    page.on_close = on_close


ft.run(main, assets_dir=str(Path(__file__).parent.resolve()))
