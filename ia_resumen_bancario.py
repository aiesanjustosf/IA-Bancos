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

# --- Regex EXACTOS (dos decimales con coma; miles con punto o espacio; guion final opcional) ---
DATE_RE  = re.compile(r"\b\d{1,2}/\d{2}/\d{4}\b")
MONEY_RE = re.compile(r"(?:\d{1,3}(?:[.\s]\d{3})*|\d+)\s?,\s?\d{2}-?")

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

def parse_pdf(file_like) -> pd.DataFrame:
    """Extracción SOLO por TEXTO (estable). Penúltimo = Importe, Último = Saldo. Crédito RESTA / Débito SUMA."""
    rows = []
    with pdfplumber.open(file_like) as pdf:
        for pageno, page in enumerate(pdf.pages, start=1):
            text = page.extract_text() or ""
            for raw in text.splitlines():
                line = " ".join(raw.split())              # colapsa espacios
                d = DATE_RE.search(line)                  # busca fecha en cualquier lugar
                if not d:
                    continue
                amounts = MONEY_RE.findall(line)
                if len(amounts) < 2:
                    continue
                saldo = normalize_money(amounts[-1])      # último = saldo
                importe = normalize_money(amounts[-2])    # penúltimo = importe
                first_m = MONEY_RE.search(line)
                desc = line[d.end(): first_m.start()].strip() if first_m else line[d.end():].strip()

                up = desc.upper()
                # Heurística mínima: SIN tocar el importe
                is_credit = ("CR-" in up) or up.startswith("CR") or ("CR-TRSFE" in up) or ("NEG.CONT" in up) or ("TRANSF RECIB" in up)
                debito  = 0.0 if is_credit else importe
                credito = importe if is_credit else 0.0

                rows.append({
                    "fecha": pd.to_datetime(d.group(0), dayfirst=True, errors="coerce"),
                    "descripcion": desc,
                    "debito": debito,
                    "credito": credito,
                    "importe": debito - credito,  # Débito SUMA / Crédito RESTA
                    "saldo": saldo,
                    "pagina": pageno
                })
    return pd.DataFrame(rows)

def find_saldo_final(file_like):
    with pdfplumber.open(file_like) as pdf:
        for page in reversed(pdf.pages):
            text = page.extract_text() or ""
            for line in text.splitlines():
                if "Saldo al" in line:
                    d = DATE_RE.search(line)
                    amts = MONEY_RE.findall(line)
                    if d and amts:
                        return pd.to_datetime(d.group(0), dayfirst=True, errors="coerce"), normalize_money(amts[-1])
    return pd.NaT, np.nan

# --- UI principal ---
uploaded = st.file_uploader("Subí un PDF del resumen bancario", type=["pdf"])
if uploaded is None:
    st.info("Cargá un PDF. La app usa extracción por TEXTO; no inventa números.")
    st.stop()

data = uploaded.read()
file_like = io.BytesIO(data)

with st.spinner("Procesando PDF..."):
    df = parse_pdf(io.BytesIO(data))

if df.empty:
    st.error("No se detectaron movimientos. Si el PDF tiene un formato distinto, pasame una línea de ejemplo (fecha + concepto + 2 montos).")
    st.stop()

# Saldos
file_like.seek(0)
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

