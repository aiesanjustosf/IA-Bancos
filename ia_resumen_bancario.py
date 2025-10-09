
# ia_resumen_bancario.py (PyPDF ultra-lean)
# Herramienta para uso interno - AIE San Justo · Developer: Alfonso Alderete

import io, re
from pypdf import PdfReader
import pandas as pd
import numpy as np
import streamlit as st

st.set_page_config(page_title="IA Resumen Bancario", page_icon="favicon-aie.ico")
st.image("logo_aie.png", width=200)
st.title("IA Resumen Bancario")

DATE_RE  = re.compile(r"\b\d{1,2}/\d{2}/\d{4}\b")
MONEY_RE = re.compile(r"(?:\d{1,3}(?:[.\s]\d{3})*|\d+),\d{2}-?")  # SIEMPRE coma + 2 decimales

def normalize_money(tok: str) -> float:
    tok = tok.strip()
    neg = tok.endswith("-")
    tok = tok.rstrip("-")
    if "," not in tok:
        return np.nan
    main, frac = tok.rsplit(",", 1)
    main = main.replace(".", "").replace(" ", "")
    s = f"{main}.{frac}"
    try:
        v = float(s)
        return -v if neg else v
    except Exception:
        return np.nan

def pdf_text_lines(file_like):
    reader = PdfReader(file_like)
    for p in reader.pages:
        text = p.extract_text() or ""
        for raw in text.splitlines():
            yield " ".join(raw.split())  # collapse spaces

def parse_pdf(file_like):
    rows = []
    for pageno, page_lines in enumerate([list(pdf_text_lines(file_like))], start=1):
        # pdf_text_lines already iterates all pages; we just build rows.
        pass
    # Re-iterate to build rows with page numbers
    file_like.seek(0)
    reader = PdfReader(file_like)
    page_no = 0
    for page in reader.pages:
        page_no += 1
        text = page.extract_text() or ""
        for raw in text.splitlines():
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
                "pagina": page_no
            })
    return pd.DataFrame(rows)

def find_saldo_final(file_like):
    reader = PdfReader(file_like)
    for page in reversed(reader.pages):
        text = page.extract_text() or ""
        for line in text.splitlines():
            if "Saldo al" in line:
                d = DATE_RE.search(line)
                amounts = MONEY_RE.findall(line)
                if d and amounts:
                    return (pd.to_datetime(d.group(0), dayfirst=True, errors="coerce"),
                            normalize_money(amounts[-1]))
    return pd.NaT, np.nan

uploaded = st.file_uploader("Subí un PDF del resumen bancario", type=["pdf"])
if uploaded is None:
    st.stop()

data = uploaded.read()
file_like = io.BytesIO(data)

with st.spinner("Procesando PDF..."):
    df = parse_pdf(io.BytesIO(data))

if df.empty:
    st.error("No se detectaron movimientos en el PDF.")
    st.stop()

fecha_cierre, saldo_final = find_saldo_final(io.BytesIO(data))
saldo_inicial = saldo_final - df["importe"].sum() if not np.isnan(saldo_final) else np.nan

st.subheader("Resumen del período")
c1, c2 = st.columns(2)
c1.metric("Saldo inicial (calculado)", f"$ {saldo_inicial:,.2f}" if not np.isnan(saldo_inicial) else "—")
c2.metric("Saldo final (PDF)", f"$ {saldo_final:,.2f}" if not np.isnan(saldo_final) else "—")
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
