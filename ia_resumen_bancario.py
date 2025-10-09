import io
import re
import pandas as pd
import numpy as np
import streamlit as st
from pathlib import Path

# --- Assets seguros (no crashea si faltan) ---
HERE = Path(__file__).parent
LOGO = HERE / "logo_aie.png"
FAVICON = HERE / "favicon-aie.ico"

st.set_page_config(
    page_title="IA Resumen Bancario",
    page_icon=str(FAVICON) if FAVICON.exists() else None
)

if LOGO.exists():
    st.image(str(LOGO), width=200)

st.title("IA Resumen Bancario")

# ====== SOLO este cambio en montos (decimales) ======
MONEY_RE = re.compile(r"(?:\d{1,3}(?:\s?\.\s?\d{3})*|\d+)\s?,\s?\d{2}-?")  # coma + 2 decimales

def parse_money(s: str) -> float:
    """Convierte '1 . 234 . 567 , 89 -' -> -1234567.89 (coma decimal)"""
    if s is None:
        return np.nan
    s = str(s).strip()
    neg = s.endswith("-")
    s = s.rstrip("-")
    s = s.replace(" ", "")  # elimina espacios dispersos
    s = s.replace(".", "").replace(",", ".")
    try:
        v = float(s)
        return -v if neg else v
    except Exception:
        return np.nan
# ====== fin del cambio de decimales ======


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
    st.error("No se detectaron movimientos en el PDF. Ajusto el parser si me compartís este archivo por mensaje.")
    st.stop()

# 2) Regla de signo pedida (se traduce a una sola columna 'importe')
df["importe"] = df["debito"].fillna(0.0) - df["credito"].fillna(0.0)  # Débito suma / Crédito resta

# 3) Saldo final del PDF y saldo inicial calculado
file_like.seek(0)
fecha_cierre, saldo_final_pdf = find_saldo_final(file_like)
saldo_inicial_calc = saldo_final_pdf - df["importe"].sum() if not np.isnan(saldo_final_pdf) else np.nan

# 4) Clasificaciones puntuales para el resumen
U = df["descripcion"].str.upper().fillna("")
tipo = pd.Series("OTROS", index=df.index)
tipo[U.str.contains(r"\bSIRCREB\b")] = "SIRCREB"
tipo[U.str.contains(r"\bIMPTRANS\b|LEY\s*25413")] = "IMP_LEY_25413"
tipo[U.str.contains(r"\bIVA\b") & ~U.str.contains("PERCEP")] = "IVA"
tipo[U.str.contains("PERCEP") & U.str.contains("IVA")] = "PERCEPCION_IVA"
tipo[U.str.contains(r"DB\.INMED|DB/PG/VS|DEB\.AUT|PAGO\s*VISA|DB-")] = "DEBITO_AUTOMATICO"
tipo[U.str.contains(r"TRSFE-IT|TRSFE-ET|TRSFE-RT")] = "TRF_TERCEROS_SALIENTE"
tipo[U.str.contains(r"CR-TRSFE|TRSFE\s+RECIB|CR-")] = "TRF_TERCEROS_ENTRANTE"
tipo[U.str.contains(r"ENTRE CUENTAS|PROPIA|MISMA TITULARIDAD")] = "TRF_PROPIAS"
df["tipo"] = tipo

# 5) Resúmenes
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

# 6) UI de resumen
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

# 7) Tablas completas (débitos y créditos)
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
