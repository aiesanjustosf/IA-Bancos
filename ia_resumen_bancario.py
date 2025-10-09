
# ia_resumen_bancario.py
# Herramienta para uso interno - AIE San Justo | Developer: Alfonso Alderete

import io
import re
import pdfplumber
import pandas as pd
import numpy as np
import streamlit as st

# ---- UI meta ----
st.set_page_config(page_title="IA Resumen Bancario", page_icon="favicon-aie.ico")

APP_TITLE = "IA Resumen Bancario"

# -----------------------------
# Utilidades
# -----------------------------
DATE_RE  = re.compile(r"\b\d{1,2}/\d{2}/\d{4}\b")
MONEY_RE_FUZZY  = re.compile(r"\d{1,3}(?:\s?\.\s?\d{3})*(?:\s?,\s?\d{2})(?:\s?-)?")
CUIT_RE  = re.compile(r"\b\d{2}-?\d{8}-?\d\b|\b\d{11}\b", re.ASCII)

def clean_money_token(tok: str) -> str:
    # normaliza espacios en separadores, conserva '-' final
    tok = tok.strip()
    neg = tok.endswith("-")
    tok = tok.rstrip("-")
    tok = tok.replace(" ", "")
    tok = tok.replace(".", "").replace(",", ".")
    if neg:
        tok += "-"
    return tok

def parse_money(tok: str) -> float:
    if not tok:
        return np.nan
    neg = tok.endswith("-")
    s = tok.rstrip("-")
    try:
        val = float(s)
        return -val if neg else val
    except Exception:
        return np.nan

# -----------------------------
# Extracción basada en TEXTO (principal)
# -----------------------------
def extract_movs_by_text(file_like) -> pd.DataFrame:
    rows = []
    with pdfplumber.open(file_like) as pdf:
        for pageno, page in enumerate(pdf.pages, start=1):
            text = page.extract_text() or ""
            if not text:
                continue
            for raw in text.splitlines():
                line = " ".join(raw.split())  # colapsa espacios
                # fecha en cualquier lugar
                dmatch = DATE_RE.search(line)
                if not dmatch:
                    continue
                fecha = pd.to_datetime(dmatch.group(0), dayfirst=True, errors="coerce")

                # todos los montos de la línea (fuzzy)
                monies = MONEY_RE_FUZZY.findall(line)
                if len(monies) < 2:
                    continue
                # último = saldo, penúltimo = importe
                saldo_tok = clean_money_token(monies[-1])
                imp_tok   = clean_money_token(monies[-2])
                saldo = parse_money(saldo_tok)
                importe = parse_money(imp_tok)

                # descripción: texto entre fecha y primer monto encontrado
                first_money = MONEY_RE_FUZZY.search(line)
                desc = line[dmatch.end(): first_money.start()] if first_money else line[dmatch.end():]
                desc = desc.strip()

                # heurística de DB/CR mínima (no inventa montos, solo separa columnas)
                up = desc.upper()
                # default: débito (sale de cuenta)
                is_credit = ("CR-" in up) or ("CR-TRSFE" in up) or up.startswith("CR") or ("NEG.CONT" in up) or ("TRANSF RECIB" in up)
                debito = 0.0 if is_credit else importe
                credito = importe if is_credit else 0.0

                # cuit si aparece
                cuit_m = CUIT_RE.search(desc.replace(" ", ""))
                cuit = cuit_m.group(0) if cuit_m else ""

                rows.append({
                    "fecha": fecha,
                    "descripcion": desc,
                    "debito": debito,
                    "credito": credito,
                    "importe": debito - credito,  # Débito suma / Crédito resta
                    "saldo": saldo,
                    "cuit": cuit,
                    "pagina": pageno,
                })
    return pd.DataFrame(rows)

# -----------------------------
# Fallback por coordenadas (si hiciera falta)
# -----------------------------
def bucket_lines(words, tol=2.0):
    lines, cur, top = [], [], None
    for w in sorted(words, key=lambda w: (w["top"], w["x0"])):
        if top is None or abs(w["top"] - top) <= tol:
            cur.append(w)
            top = w["top"] if top is None else top
        else:
            lines.append(cur)
            cur, top = [w], w["top"]
    if cur: lines.append(cur)
    return lines

def extract_movs_by_words(file_like) -> pd.DataFrame:
    rows = []
    with pdfplumber.open(file_like) as pdf:
        for pageno, page in enumerate(pdf.pages, start=1):
            words = page.extract_words(extra_attrs=["x0","x1","top","bottom"])
            if not words: continue
            for line in bucket_lines(words, tol=2.0):
                txt = " ".join(w["text"] for w in line)
                dmatch = DATE_RE.search(txt)
                if not dmatch: continue
                monies = MONEY_RE_FUZZY.findall(txt)
                if len(monies) < 2: continue
                saldo = parse_money(clean_money_token(monies[-1]))
                importe = parse_money(clean_money_token(monies[-2]))
                # desc: entre fecha y primer monto
                first_money = MONEY_RE_FUZZY.search(txt)
                desc = txt[dmatch.end(): first_money.start()].strip() if first_money else txt[dmatch.end():].strip()
                up = desc.upper()
                is_credit = ("CR-" in up) or ("CR-TRSFE" in up) or up.startswith("CR") or ("NEG.CONT" in up) or ("TRANSF RECIB" in up)
                debito = 0.0 if is_credit else importe
                credito = importe if is_credit else 0.0
                cuit_m = CUIT_RE.search(desc.replace(" ", ""))
                cuit = cuit_m.group(0) if cuit_m else ""
                rows.append({
                    "fecha": pd.to_datetime(dmatch.group(0), dayfirst=True, errors="coerce"),
                    "descripcion": desc,
                    "debito": debito,
                    "credito": credito,
                    "importe": debito - credito,
                    "saldo": saldo,
                    "cuit": cuit,
                    "pagina": pageno,
                })
    return pd.DataFrame(rows)

def extract_movimientos(file_like) -> pd.DataFrame:
    df = extract_movs_by_text(file_like)
    if not df.empty: return df
    # fallback
    file_like.seek(0)
    return extract_movs_by_words(file_like)

# -----------------------------
# Saldo final
# -----------------------------
def find_saldo_final(file_like):
    with pdfplumber.open(file_like) as pdf:
        for page in pdf.pages[::-1]:
            txt = page.extract_text() or ""
            for line in txt.splitlines():
                if "Saldo al" in line:
                    d = DATE_RE.search(line)
                    m = MONEY_RE_FUZZY.search(line)
                    if d and m:
                        fecha = pd.to_datetime(d.group(0), dayfirst=True, errors="coerce")
                        saldo = parse_money(clean_money_token(m.group(0)))
                        return fecha, saldo
    return pd.NaT, np.nan

# -----------------------------
# Streamlit UI
# -----------------------------
st.image("logo_aie.png", width=200)
st.title(APP_TITLE)

uploaded = st.file_uploader("Subí un PDF del resumen bancario", type=["pdf"])
if uploaded is None:
    st.stop()

file_like = io.BytesIO(uploaded.read())

# 1) Extraer movimientos
df = extract_movimientos(file_like)
if df.empty:
    st.error("No se detectaron movimientos en el PDF.")
    st.stop()

# 2) Saldos
file_like.seek(0)
fecha_cierre, saldo_final_pdf = find_saldo_final(file_like)
saldo_inicial = saldo_final_pdf - df["importe"].sum() if not np.isnan(saldo_final_pdf) else np.nan

# 3) Clasificaciones mínimas para el resumen
U = df["descripcion"].str.upper().fillna("")
tipo = pd.Series("OTROS", index=df.index)
tipo[U.str.contains(r"\bSIRCREB\b")] = "SIRCREB"
tipo[U.str.contains(r"\bIMPTRANS\b|LEY\s*25413", regex=True)] = "IMP_LEY_25413"
tipo[U.str.contains(r"\bIVA\b") & ~U.str.contains("PERCEP")] = "IVA"
tipo[U.str.contains("PERCEP") & U.str.contains("IVA")] = "PERCEPCION_IVA"
tipo[U.str.contains(r"DB\.INMED|DB/PG/VS|DEB\.AUT|PAGO\s*VISA|DB-")] = "DEBITO_AUTOMATICO"
tipo[U.str.contains(r"TRSFE-IT|TRSFE-ET|TRSFE-RT")] = "TRF_TERCEROS_SALIENTE"
tipo[U.str.contains(r"CR-TRSFE|TRSFE\s+RECIB|CR-")] = "TRF_TERCEROS_ENTRANTE"
tipo[U.str.contains(r"ENTRE CUENTAS|PROPIA|MISMA TITULARIDAD")] = "TRF_PROPIAS"
df["tipo"] = tipo

# 4) Resumen
def pos(x): return x[x>0].sum()
def neg_abs(x): return (-x[x<0]).sum()
summary = {
    "saldo_inicial": saldo_inicial,
    "saldo_final": saldo_final_pdf,
    "trf_recibidas_terceros": pos(df.loc[df["tipo"]=="TRF_TERCEROS_ENTRANTE","importe"]),
    "trf_realizadas_terceros": neg_abs(df.loc[df["tipo"]=="TRF_TERCEROS_SALIENTE","importe"]),
    "trf_propias_recibidas": pos(df.loc[df["tipo"]=="TRF_PROPIAS","importe"]),
    "trf_propias_realizadas": neg_abs(df.loc[df["tipo"]=="TRF_PROPIAS","importe"]),
    "sircreb": neg_abs(df.loc[df["tipo"]=="SIRCREB","importe"]),
    "imp_25413": neg_abs(df.loc[df["tipo"]=="IMP_LEY_25413","importe"]),
    "debitos_automaticos": neg_abs(df.loc[df["tipo"]=="DEBITO_AUTOMATICO","importe"]),
    "iva": neg_abs(df.loc[df["tipo"]=="IVA","importe"]),
    "percepciones_iva": neg_abs(df.loc[df["tipo"]=="PERCEPCION_IVA","importe"]),
}

# 5) UI
st.subheader("Resumen del período")
c1, c2, c3 = st.columns(3)
c1.metric("Saldo inicial (calculado)", f"$ {summary['saldo_inicial']:,.2f}" if not np.isnan(summary['saldo_inicial']) else "—")
c2.metric("Saldo final (PDF)", f"$ {summary['saldo_final']:,.2f}" if not np.isnan(summary['saldo_final']) else "—")
if pd.notna(fecha_cierre):
    c3.write(f"**Cierre:** {fecha_cierre.strftime('%d/%m/%Y')}")

st.divider()
st.subheader("Detalle completo de movimientos")
st.dataframe(df.sort_values(["fecha","pagina"]).reset_index(drop=True)[["fecha","descripcion","debito","credito","importe","saldo","tipo","cuit","pagina"]])

st.subheader("TODOS los Débitos")
deb = df[df["importe"]<0].copy()
deb["monto"] = -deb["importe"]
st.dataframe(deb[["fecha","descripcion","monto","tipo","cuit","pagina"]])

st.subheader("TODOS los Créditos")
cre = df[df["importe"]>0].copy()
cre["monto"] = cre["importe"]
st.dataframe(cre[["fecha","descripcion","monto","tipo","cuit","pagina"]])

st.subheader("Transferencias de/para terceros por CUIT")
trf = df[df["tipo"].isin(["TRF_TERCEROS_ENTRANTE","TRF_TERCEROS_SALIENTE"])].copy()
agr = trf.groupby(["tipo","cuit"], dropna=False)["importe"].sum().reset_index()
agr["abs"] = agr["importe"].abs()
st.dataframe(agr.sort_values("abs", ascending=False)[["tipo","cuit","importe"]])

# Footer
st.markdown(
    """
    <div style="position:fixed; left:0; right:0; bottom:0; padding:8px 12px; background:#f6f8fa; color:#444; font-size:12px; text-align:center; border-top:1px solid #e5e7eb;">
    Herramienta para uso interno - AIE San Justo · Developer: Alfonso Alderete
    </div>
    """, unsafe_allow_html=True
)
