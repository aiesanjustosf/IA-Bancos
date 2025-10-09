
# ia_resumen_bancario.py (LITE - TEXT ONLY)
# Herramienta para uso interno - AIE San Justo | Developer: Alfonso Alderete

import io, re
import pdfplumber
import pandas as pd
import numpy as np
import streamlit as st

st.set_page_config(page_title="IA Resumen Bancario", page_icon="favicon-aie.ico")
st.image("logo_aie.png", width=200)
st.title("IA Resumen Bancario")

# ----- Regex sólidos y rápidos -----
DATE_RE  = re.compile(r"\b\d{1,2}/\d{2}/\d{4}\b")
MONEY_RE = re.compile(r"(?:\d{1,3}(?:[.\s]\d{3})*|\d+),\d{2}-?")  # dos decimales SIEMPRE

def normalize_money(tok: str) -> float:
    tok = tok.strip()
    neg = tok.endswith("-")
    tok = tok.rstrip("-")
    # separador decimal fijo: coma
    if "," in tok:
        main, frac = tok.rsplit(",", 1)
    else:
        return np.nan
    main = main.replace(".", "").replace(" ", "")
    s = f"{main}.{frac}"
    try:
        val = float(s)
        return -val if neg else val
    except Exception:
        return np.nan

def parse_pdf(file_like) -> pd.DataFrame:
    rows = []
    with pdfplumber.open(file_like) as pdf:
        for pageno, page in enumerate(pdf.pages, start=1):
            txt = page.extract_text() or ""
            for raw in txt.splitlines():
                line = " ".join(raw.split())
                d = DATE_RE.search(line)
                if not d:
                    continue
                monies = MONEY_RE.findall(line)
                if len(monies) < 2:
                    continue
                saldo = normalize_money(monies[-1])
                importe = normalize_money(monies[-2])
                first_m = MONEY_RE.search(line)
                desc = line[d.end(): first_m.start()].strip() if first_m else line[d.end():].strip()
                up = desc.upper()
                is_credit = ("CR-" in up) or up.startswith("CR") or ("CR-TRSFE" in up) or ("NEG.CONT" in up) or ("TRANSF RECIB" in up)
                debito = 0.0 if is_credit else importe
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
    return pd.DataFrame(rows)

def find_saldo_final(file_like):
    with pdfplumber.open(file_like) as pdf:
        for page in reversed(pdf.pages):
            txt = page.extract_text() or ""
            for line in txt.splitlines():
                if "Saldo al" in line:
                    d = DATE_RE.search(line)
                    m_all = MONEY_RE.findall(line)
                    if d and m_all:
                        return (pd.to_datetime(d.group(0), dayfirst=True, errors="coerce"),
                                normalize_money(m_all[-1]))
    return pd.NaT, np.nan

uploaded = st.file_uploader("Subí un PDF del resumen bancario", type=["pdf"])
if uploaded is None:
    st.stop()

data = uploaded.read()
file_like = io.BytesIO(data)

with st.spinner("Procesando PDF..."):
    df = parse_pdf(file_like)

if df.empty:
    st.error("No se detectaron movimientos en el PDF.")
    st.stop()

file_like.seek(0)
fecha_cierre, saldo_final = find_saldo_final(file_like)
saldo_inicial = saldo_final - df["importe"].sum() if not np.isnan(saldo_final) else np.nan

st.subheader("Resumen del período")
col1, col2 = st.columns(2)
col1.metric("Saldo inicial (calculado)", f"$ {saldo_inicial:,.2f}" if not np.isnan(saldo_inicial) else "—")
col2.metric("Saldo final (PDF)", f"$ {saldo_final:,.2f}" if not np.isnan(saldo_final) else "—")
if pd.notna(fecha_cierre):
    st.caption(f"Cierre: {fecha_cierre.strftime('%d/%m/%Y')}")

st.divider()
st.subheader("Detalle de movimientos")
st.dataframe(df.sort_values(["fecha","pagina"]).reset_index(drop=True))

st.markdown(
    """
    <div style="position:fixed; left:0; right:0; bottom:0; padding:8px 12px; background:#f6f8fa; color:#444; font-size:12px; text-align:center; border-top:1px solid #e5e7eb;">
    Herramienta para uso interno - AIE San Justo · Developer: Alfonso Alderete
    </div>
    """, unsafe_allow_html=True
)
