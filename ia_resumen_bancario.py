# ia_resumen_bancario.py - versión con parser Galicia corregido
# Herramienta para uso interno - AIE San Justo

import io, re
from pathlib import Path
import numpy as np
import pandas as pd
import streamlit as st

# --- UI / assets ---
HERE = Path(__file__).parent
LOGO = HERE / "logo_aie.png"
FAVICON = HERE / "favicon-aie.ico"
st.set_page_config(
    page_title="IA Resumen Bancario",
    page_icon=str(FAVICON) if FAVICON.exists() else None,
)
if LOGO.exists():
    st.image(str(LOGO), width=200)
st.title("IA Resumen Bancario")

# --- deps diferidas ---
try:
    import pdfplumber
except Exception as e:
    st.error(f"No se pudo importar pdfplumber: {e}")
    st.stop()

# ========== GALICIA PARSER Y DETECCIÓN ==========

DATE_RE = re.compile(r"\b\d{1,2}/\d{2}/\d{2,4}\b")
MONEY_RE = re.compile(
    r"(?<!\S)(?:\d{1,3}(?:\.\d{3})*|\d+)\s?,\s?\d{2}-?(?!\S)"
)
HEADER_ROW_PAT = re.compile(r"FECHA\s+DESCRIPC", re.IGNORECASE)
NON_MOV_PAT = re.compile(r"RESUMEN|TOTALES", re.IGNORECASE)


def normalize_money(tok: str) -> float:
    if not tok:
        return np.nan
    tok = tok.strip().replace("−", "-")
    neg = tok.endswith("-")
    tok = tok.rstrip("-")
    if "," not in tok:
        return np.nan
    main, frac = tok.rsplit(",", 1)
    main = main.replace(".", "").replace(" ", "")
    try:
        val = float(f"{main}.{frac}")
        return -val if neg else val
    except Exception:
        return np.nan


def fmt_ar(n):
    if n is None or (isinstance(n, float) and np.isnan(n)):
        return "—"
    return (
        f"{n:,.2f}".replace(",", "§").replace(".", ",").replace("§", ".")
    )


def detect_bank_from_text(txt: str) -> str:
    U = (txt or "").upper()
    # Prioridad alta a Galicia
    if "BANCO GALICIA" in U or "RESUMEN DE CUENTA CORRIENTE EN PESOS" in U:
        return "Banco Galicia"
    if "BANCO MACRO" in U:
        return "Banco Macro"
    if "BANCO DE SANTA FE" in U:
        return "Banco de Santa Fe"
    if "BANCO DE LA NACION" in U:
        return "Banco de la Nación Argentina"
    if "BANCO CREDICOOP" in U:
        return "Banco Credicoop"
    if "SANTANDER" in U:
        return "Banco Santander"
    return "Banco no identificado"


def parse_galicia_lines(lines):
    """
    Parser por bloques de Galicia.
    """
    rows = []
    seq = 0
    for ln in lines:
        s = ln.strip()
        if not s or HEADER_ROW_PAT.search(s) or NON_MOV_PAT.search(s):
            continue
        d = DATE_RE.search(s)
        am = list(MONEY_RE.finditer(s))
        if not d or len(am) < 2:
            continue

        saldo = normalize_money(am[-1].group(0))
        mids = am[:-1]
        desc = (
            s[d.end() : mids[0].start()].strip()
            if mids
            else s[d.end() : am[-1].start()].strip()
        )

        mid_vals = [normalize_money(m.group(0)) for m in mids]
        credito = debito = 0.0
        for v in mid_vals:
            if v < 0:
                debito += -v
            else:
                credito += v

        if debito == 0.0 and len(mid_vals) == 2 and all(v >= 0 for v in mid_vals):
            mn = min(mid_vals)
            mx = max(mid_vals)
            debito = mn
            credito = mx

        seq += 1
        rows.append(
            {
                "fecha": pd.to_datetime(d.group(0), dayfirst=True, errors="coerce"),
                "descripcion": desc,
                "debito": debito,
                "credito": credito,
                "saldo": saldo,
                "orden": seq,
            }
        )

    df = pd.DataFrame(rows)
    if df.empty:
        return df

    df = df.sort_values(["fecha", "orden"]).reset_index(drop=True)

    # Calcular saldo inicial (regla exclusiva Galicia)
    first = df.iloc[0]
    saldo_inicial = float(first["saldo"]) - (
        float(first["credito"]) - float(first["debito"])
    )

    apertura = pd.DataFrame(
        [
            {
                "fecha": (df["fecha"].min() - pd.Timedelta(days=1)),
                "descripcion": "SALDO INICIAL",
                "debito": 0.0,
                "credito": 0.0,
                "saldo": saldo_inicial,
                "orden": 0,
            }
        ]
    )

    return pd.concat([apertura, df], ignore_index=True)


# ========== APP STREAMLIT ==========

uploaded = st.file_uploader("Subí un PDF del resumen bancario", type=["pdf"])
if not uploaded:
    st.stop()
data = uploaded.read()

import io


def _text_from_pdf(f):
    with pdfplumber.open(f) as pdf:
        return "\n".join((p.extract_text() or "") for p in pdf.pages)


_bank_txt = _text_from_pdf(io.BytesIO(data))
bank = detect_bank_from_text(_bank_txt)
st.write("Detectado:", bank)

if bank == "Banco Galicia":
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        lines = []
        for p in pdf.pages:
            for l in (p.extract_text() or "").splitlines():
                if l.strip():
                    lines.append(l.strip())

    df = parse_galicia_lines(lines)

    if df.empty:
        st.warning("No se detectaron movimientos.")
    else:
        saldo_inicial = df.loc[0, "saldo"]
        tot_deb = float(df["debito"].sum())
        tot_cre = float(df["credito"].sum())
        saldo_final = float(df["saldo"].iloc[-1])

        c1, c2, c3, c4 = st.columns(4)
        with c1:
            st.metric("Saldo inicial", f"$ {fmt_ar(saldo_inicial)}")
        with c2:
            st.metric("Créditos", f"$ {fmt_ar(tot_cre)}")
        with c3:
            st.metric("Débitos", f"$ {fmt_ar(tot_deb)}")
        with c4:
            st.metric("Saldo final", f"$ {fmt_ar(saldo_final)}")

        st.dataframe(df, use_container_width=True, height=600)

else:
    st.info("Procesamiento solo implementado para Banco Galicia en esta versión.")
