# ia_resumen_bancario.py
# Herramienta para uso interno - AIE San Justo · Developer: Alfonso Alderete

import io, re
from pathlib import Path
import numpy as np
import pandas as pd
import streamlit as st

# --- Config UI (no rompe si faltan assets) ---
HERE = Path(__file__).parent
LOGO = HERE / "logo_aie.png"
FAVICON = HERE / "favicon-aie.ico"
st.set_page_config(page_title="IA Resumen Bancario", page_icon=str(FAVICON) if FAVICON.exists() else None)
if LOGO.exists():
    st.image(str(LOGO), width=200)
st.title("IA Resumen Bancario")

# --- Import diferido (si hay error de lib, la página igual carga y ves el error) ---
try:
    import pdfplumber
except Exception as e:
    st.error(f"No se pudo importar pdfplumber: {e}\nRevisá requirements.txt")
    st.stop()

# --- Regex EXACTOS (dos decimales con coma; guion final opcional; con bordes para no pegarse a texto) ---
DATE_RE  = re.compile(r"\b\d{1,2}/\d{2}/\d{4}\b")

# Antes:
# MONEY_RE = re.compile(r"(?:\d{1,3}(?:[.\s]\d{3})*|\d+)\s?,\s?\d{2}-?")
# Después (robusto, sin espacios como miles y con bordes):
MONEY_RE = re.compile(r'(?<!\S)(?:\d{1,3}(?:\.\d{3})*|\d+)\s?,\s?\d{2}-?(?!\S)')

def normalize_money(tok: str) -> float:
    """'1 . 234 . 567 , 89 -' -> -1234567.89 (coma = decimal; dos decimales)"""
    if not tok:
        return np.nan
    tok = tok.strip()
    neg = tok.endswith("-")
    tok = tok.rstrip("-")
    if "," not in tok:
        return np.nan
    main, frac = tok.rsplit(",", 1)  # decimal fijo: coma
    main = main.replace(".", "").replace(" ", "")
    try:
        val = float(f"{main}.{frac}")
        return -val if neg else val
    except Exception:
        return np.nan

def fmt_ar(n) -> str:
    """Devuelve 1.234.567,89 para números; '—' si es NaN."""
    if n is None or (isinstance(n, float) and np.isnan(n)):
        return "—"
    return f"{n:,.2f}".replace(",", "§").replace(".", ",").replace("§", ".")

def lines_from_text(page):
    txt = page.extract_text() or ""
    return [" ".join(l.split()) for l in txt.splitlines()]

def lines_from_words(page, ytol=2.0):
    """Reconstruye líneas agrupando por altura; solo para fallback."""
    words = page.extract_words(extra_attrs=["x0", "top"])
    if not words:
        return []
    words.sort(key=lambda w: (round(w["top"]/ytol), w["x0"]))
    lines, cur, band = [], [], None
    for w in words:
        b = round(w["top"]/ytol)
        if band is None or b == band:
            cur.append(w)
        else:
            lines.append(" ".join(x["text"] for x in cur))
            cur = [w]
        band = b
    if cur:
        lines.append(" ".join(x["text"] for x in cur))
    return [" ".join(l.split()) for l in lines]

def parse_pdf(file_like) -> pd.DataFrame:
    """
    Lee TODAS las páginas. Por línea exige >=2 montos con coma:
    - anteúltimo = importe (movimiento, único por fila)
    - último = saldo (a la derecha, puede terminar en '-')
    Enteros sin decimales SIEMPRE se ignoran (son parte de la descripción).
    """
    rows = []
    descartadas = []  # diagnóstico de líneas sin 2 montos con coma

    with pdfplumber.open(file_like) as pdf:
        for pageno, page in enumerate(pdf.pages, start=1):
            hits_en_pagina = 0

            # 1) TEXTO
            for line in lines_from_text(page):
                am = list(MONEY_RE.finditer(line))  # SOLO montos con coma (no enteros)
                if len(am) < 2:
                    if line.strip():
                        descartadas.append((pageno, line))
                    continue

                saldo_tok   = am[-1].group(0)
                saldo       = normalize_money(saldo_tok)
                importe_tok = am[-2].group(0)
                importe     = normalize_money(importe_tok)

                # descripción: desde el fin de la fecha hasta el primer monto con coma
                d = DATE_RE.search(line)
                if not d:
                    if line.strip():
                        descartadas.append((pageno, line))
                    continue
                first_money = am[0]
                desc = line[d.end(): first_money.start()].strip()

                up = desc.upper()
                is_credit = ("CR-" in up) or up.startswith("CR") or ("CR-TRSFE" in up) or ("NEG.CONT" in up) or ("TRANSF RECIB" in up)
                debito  = 0.0 if is_credit else importe
                credito = importe if is_credit else 0.0

                rows.append({
                    "fecha": pd.to_datetime(d.group(0), dayfirst=True, errors="coerce"),
                    "descripcion": desc,
                    "debito": debito,
                    "credito": credito,
                    "importe": debito - credito,  # Débito suma / Crédito resta
                    "saldo": saldo,
                    "pagina": pageno
                })
                hits_en_pagina += 1

            # 2) FALLBACK por PALABRAS solo si por texto no hubo ningún hit
            if hits_en_pagina == 0:
                for line in lines_from_words(page, ytol=2.0):
                    am = list(MONEY_RE.finditer(line))
                    if len(am) < 2:
                        if line.strip():
                            descartadas.append((pageno, line))
                        continue

                    saldo_tok   = am[-1].group(0)
                    saldo       = normalize_money(saldo_tok)
                    importe_tok = am[-2].group(0)
                    importe     = normalize_money(importe_tok)

                    d = DATE_RE.search(line)
                    if not d:
                        if line.strip():
                            descartadas.append((pageno, line))
                        continue
                    first_money = am[0]
                    desc = line[d.end(): first_money.start()].strip()

                    up = desc.upper()
                    is_credit = ("CR-" in up) or up.startswith("CR") or ("CR-TRSFE" in up) or ("NEG.CONT" in up) or ("TRANSF RECIB" in up)
                    debito  = 0.0 if is_credit else importe
                    credito = importe if is_credit else 0.0

                    rows.append({
                        "fecha": pd.to_datetime(d.group(0), dayfirst=True, errors="coerce"),
                        "descripcion": desc,
                        "debito": debito,
                        "credito": credito,
                        "importe": debito - credito,
                        "saldo": saldo,
                        "pagina": pageno
                    })

    df = pd.DataFrame(rows)

    # Diagnóstico visible (opcional): cuántas líneas del PDF no cumplen la regla
    if 'st' in globals():
        if descartadas:
            st.warning(f"Líneas descartadas por no contener 2 montos con coma (importe + saldo): {len(descartadas)}")
            # Mostrar un muestreo de hasta 10 para controlar
            sample = descartadas[:10]
            st.caption("Ejemplos (página, línea):")
            for p, l in sample:
                st.text(f"[p.{p}] {l}")

    return df

def find_saldo_final(file_like):
    """Busca la línea 'Saldo al dd/mm/aaaa ... <saldo>' y devuelve (fecha_cierre, saldo_final)."""
    with pdfplumber.open(file_like) as pdf:
        for page in reversed(pdf.pages):
            txt = page.extract_text() or ""
            for line in txt.splitlines():
                if "Saldo al" in line:
                    d = DATE_RE.search(line)
                    am = list(MONEY_RE.finditer(line))
                    if d and am:
                        fecha = pd.to_datetime(d.group(0), dayfirst=True, errors="coerce")
                        saldo = normalize_money(am[-1].group(0))
                        return fecha, saldo
    return pd.NaT, np.nan

# --- UI principal ---
uploaded = st.file_uploader("Subí un PDF del resumen bancario", type=["pdf"])
if uploaded is None:
    st.info("Cargá un PDF. La app usa extracción por TEXTO (con fallback por palabras) y no inventa montos.")
    st.stop()

data = uploaded.read()
file_like = io.BytesIO(data)

with st.spinner("Procesando PDF..."):
    df = parse_pdf(io.BytesIO(data))

if df.empty:
    st.error("No se detectaron movimientos. Si el PDF tiene un formato distinto, pasame una línea de ejemplo (fecha + concepto + 2 montos).")
    st.stop()

# Saldos
fecha_cierre, saldo_final = find_saldo_final(io.BytesIO(data))
saldo_inicial = saldo_final - df["importe"].sum() if not np.isnan(saldo_final) else np.nan

# Resumen
st.subheader("Resumen del período")
c1, c2 = st.columns(2)
c1.metric("Saldo inicial (calculado)", f"$ {fmt_ar(saldo_inicial)}")
c2.metric("Saldo final (PDF)", f"$ {fmt_ar(saldo_final)}")
if pd.notna(fecha_cierre):
    st.caption(f"Cierre: {fecha_cierre.strftime('%d/%m/%Y')}")

st.divider()
st.subheader("Detalle de movimientos")

df_sorted = df.sort_values(["fecha","pagina"]).reset_index(drop=True)
cols_money = ["debito", "credito", "importe", "saldo"]

# Estilo: miles con punto y decimales con coma; mantiene dtype numérico
styled = df_sorted.style.format({c: fmt_ar for c in cols_money}, na_rep="—")
st.dataframe(styled, use_container_width=True)

st.markdown(
    """
    <div style="position:fixed; left:0; right:0; bottom:0; padding:8px 12px; background:#f6f8fa; color:#444; font-size:12px; text-align:center; border-top:1px solid #e5e7eb;">
      Herramienta para uso interno - AIE San Justo · Developer: Alfonso Alderete
    </div>
    """,
    unsafe_allow_html=True,
)


