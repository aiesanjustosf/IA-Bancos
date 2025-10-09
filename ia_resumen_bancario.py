
# ia_resumen_bancario.py
# Herramienta para uso interno - AIE San Justo | Developer: Alfonso Alderete

import io
import re
import pdfplumber
import pandas as pd
import numpy as np
import streamlit as st
from datetime import datetime

# ---- UI meta (debe ser el primer llamado de Streamlit) ----
st.set_page_config(page_title="IA Resumen Bancario", page_icon="favicon-aie.ico")

APP_TITLE = "IA Resumen Bancario"

# -----------------------------
# Utilidades
# -----------------------------
# Estricta (sin espacios): 1.234,56-
MONEY_RE_STRICT = re.compile(r"\d{1,3}(?:\.\d{3})*,\d{2}-?")
# Tolerante (con espacios entre dígitos/sep): 1 . 234 , 56  -
MONEY_RE_FUZZY  = re.compile(r"\d{1,3}(?:\s?\.\s?\d{3})*(?:\s?,\s?\d{2})(?:\s?-)?")
DATE_RE  = re.compile(r"^\d{1,2}/\d{2}/\d{4}")
CUIT_RE  = re.compile(r"\b\d{11}\b")

def parse_money(s: str) -> float:
    """Convierte '1.234.567,89-' o '1 .234 , 567 , 89 -' -> signo correcto."""
    if s is None:
        return np.nan
    s = str(s)
    neg = s.strip().endswith("-")
    # Normalizar: quitar espacios dispersos
    s = s.replace(" ", "")
    s = s.replace(".", "").replace(",", ".").rstrip("-")
    try:
        v = float(s)
        return -v if neg else v
    except Exception:
        return np.nan

def bucket_lines(words, tol=2.0):
    """Agrupa palabras por línea usando la coordenada 'top' con tolerancia."""
    lines = []
    current = []
    current_top = None
    for w in sorted(words, key=lambda w: (w["top"], w["x0"])):
        if current_top is None or abs(w["top"] - current_top) <= tol:
            current.append(w)
            if current_top is None:
                current_top = w["top"]
        else:
            lines.append(current)
            current = [w]
            current_top = w["top"]
    if current:
        lines.append(current)
    return lines

def extract_movimientos(file_like) -> pd.DataFrame:
    """Parser robusto por coordenadas; reensambla números con coma decimal aunque vengan fragmentados."""
    rows = []
    with pdfplumber.open(file_like) as pdf:
        for pageno, page in enumerate(pdf.pages, start=1):
            width = float(page.width)
            words = page.extract_words(extra_attrs=["x0", "x1", "top", "bottom"])
            if not words:
                continue
            for line in bucket_lines(words, tol=2.0):
                full_line_txt = "".join(w["text"] for w in line)
                m = DATE_RE.match(full_line_txt)
                if not m:
                    continue
                fecha = m.group(0)
                # derecha: montos (importe y saldo)
                right_tokens = [w["text"] for w in line if w["x0"] >= width * 0.55]
                right_txt = " ".join(right_tokens)
                monies = MONEY_RE_FUZZY.findall(right_txt)
                if len(monies) < 2:
                    # último intento: usar estricta por si vino sin espacios
                    monies = MONEY_RE_STRICT.findall(right_txt)
                if len(monies) < 2:
                    continue
                importe = parse_money(monies[-2])
                saldo   = parse_money(monies[-1])

                # izquierda: descripción
                left_txt = "".join(w["text"] for w in line if w["x0"] < width * 0.55)
                descripcion = left_txt[len(fecha):]
                up = descripcion.upper()

                # Clasificación (heurstica mínima para DB/CR)
                sign = -1  # débito por defecto
                if "CR-" in up or up.startswith("CR") or "NEG.CONT" in up or "CR-TRSFE" in up:
                    sign = +1
                elif "TRSFE-IT" in up or "TRSFE-ET" in up or "DB" in up or "DEB." in up or "IMPTRANS" in up or "SIRCREB" in up or "IVA" in up:
                    sign = -1
                elif "TRSFE" in up:
                    sign = +1  # recibidas
                debito  = importe if sign == -1 else 0.0
                credito = importe if sign == +1 else 0.0

                # CUIT
                m_cuit = CUIT_RE.search(descripcion.replace("-", ""))
                cuit = m_cuit.group(0) if m_cuit else ""

                rows.append({
                    "fecha": pd.to_datetime(fecha, dayfirst=True, errors="coerce"),
                    "descripcion": descripcion,
                    "debito": debito,
                    "credito": credito,
                    "importe": debito - credito,   # Débito suma / Crédito resta
                    "saldo": saldo,
                    "cuit": cuit,
                    "pagina": pageno
                })
    return pd.DataFrame(rows)

def find_saldo_final(file_like):
    """Busca una línea 'Saldo al dd/mm/aaaa ...' en el texto del PDF (tolerando espacios)."""
    with pdfplumber.open(file_like) as pdf:
        for page in pdf.pages[::-1]:  # desde el final
            text = page.extract_text() or ""
            for line in text.splitlines():
                if "Saldo al" in line:
                    m_date = re.search(r"\d{1,2}/\d{2}/\d{4}", line)
                    m_val  = MONEY_RE_FUZZY.search(line) or MONEY_RE_STRICT.search(line)
                    if m_date and m_val:
                        return pd.to_datetime(m_date.group(0), dayfirst=True), parse_money(m_val.group(0))
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

# 2) Saldo final del PDF y saldo inicial calculado
file_like.seek(0)
fecha_cierre, saldo_final_pdf = find_saldo_final(file_like)
saldo_inicial_calc = saldo_final_pdf - df["importe"].sum() if not np.isnan(saldo_final_pdf) else np.nan

# 3) Clasificaciones para el resumen
U = df["descripcion"].str.upper().fillna("")
tipo = pd.Series("OTROS", index=df.index)
tipo[U.str.contains(r"\bSIRCREB\b")] = "SIRCREB"
tipo[U.str_contains := U.str.contains(r"\bIMPTRANS\b|LEY\s*25413")] = "IMP_LEY_25413"
tipo[U.str.contains(r"\bIVA\b") & ~U.str.contains("PERCEP")] = "IVA"
tipo[U.str.contains("PERCEP") & U.str.contains("IVA")] = "PERCEPCION_IVA"
tipo[U.str.contains(r"DB\.INMED|DB/PG/VS|DEB\.AUT|PAGO\s*VISA|DB-")] = "DEBITO_AUTOMATICO"
tipo[U.str.contains(r"TRSFE-IT|TRSFE-ET|TRSFE-RT")] = "TRF_TERCEROS_SALIENTE"
tipo[U.str.contains(r"CR-TRSFE|TRSFE\s+RECIB|CR-")] = "TRF_TERCEROS_ENTRANTE"
tipo[U.str.contains(r"ENTRE CUENTAS|PROPIA|MISMA TITULARIDAD")] = "TRF_PROPIAS"
df["tipo"] = tipo

# 4) Resúmenes
def pos(x): return x[x>0].sum()
def neg_abs(x): return (-x[x<0]).sum()

summary = {
    "saldo_inicial": saldo_inicial_calc,
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

# 5) UI de resumen
st.subheader("Resumen del período")
c1, c2, c3 = st.columns(3)
c1.metric("Saldo inicial (calculado)", f"$ {summary['saldo_inicial']:,.2f}" if not np.isnan(summary['saldo_inicial']) else "—")
c2.metric("Saldo final (del PDF)", f"$ {summary['saldo_final']:,.2f}" if not np.isnan(summary['saldo_final']) else "—")
if pd.notna(fecha_cierre):
    c3.write(f"**Cierre:** {fecha_cierre.strftime('%d/%m/%Y')}")

c1.metric("Trf. terceros recibidas", f"$ {summary['trf_recibidas_terceros']:,.2f}")
c2.metric("Trf. terceros realizadas", f"$ {summary['trf_realizadas_terceros']:,.2f}")
c3.metric("Trf. propias recibidas", f"$ {summary['trf_propias_recibidas']:,.2f}")
c1.metric("Trf. propias realizadas", f"$ {summary['trf_propias_realizadas']:,.2f}")

st.divider()
st.subheader("Retenciones y débitos automáticos")
c1, c2, c3 = st.columns(3)
c1.metric("SIRCREB", f"$ {summary['sircreb']:,.2f}")
c2.metric("Imp. Ley 25413", f"$ {summary['imp_25413']:,.2f}")
c3.metric("Débitos automáticos", f"$ {summary['debitos_automaticos']:,.2f}")
c1.metric("IVA (líneas 'IVA')", f"$ {summary['iva']:,.2f}")
c2.metric("Percepciones de IVA", f"$ {summary['percepciones_iva']:,.2f}")

# 6) Tablas
df_sorted = df.sort_values(["fecha","pagina"]).reset_index(drop=True)
st.subheader("Detalle completo de movimientos")
st.dataframe(df_sorted[["fecha","descripcion","debito","credito","importe","saldo","tipo","cuit","pagina"]])

st.subheader("TODOS los Débitos")
deb = df_sorted.loc[df_sorted["importe"]<0, ["fecha","descripcion","importe","tipo","cuit","pagina"]].copy()
deb["monto"] = -deb["importe"]
st.dataframe(deb.drop(columns=["importe"]))

st.subheader("TODOS los Créditos")
cre = df_sorted.loc[df_sorted["importe"]>0, ["fecha","descripcion","importe","tipo","cuit","pagina"]].copy()
cre["monto"] = cre["importe"]
st.dataframe(cre.drop(columns=["importe"]))

st.subheader("Transferencias de/para terceros por CUIT")
trf = df_sorted[df_sorted["tipo"].isin(["TRF_TERCEROS_ENTRANTE","TRF_TERCEROS_SALIENTE"])].copy()
agr = trf.groupby(["tipo","cuit"], dropna=False)["importe"].sum().reset_index()
agr["abs"] = agr["importe"].abs()
st.dataframe(agr.sort_values("abs", ascending=False)[["tipo","cuit","importe"]])

# ---- Footer fijo ----
st.markdown(
    """
    <div style="position:fixed; left:0; right:0; bottom:0; padding:8px 12px; background:#f6f8fa; color:#444; font-size:12px; text-align:center; border-top:1px solid #e5e7eb;">
    Herramienta para uso interno - AIE San Justo · Developer: Alfonso Alderete
    </div>
    """, unsafe_allow_html=True
)
