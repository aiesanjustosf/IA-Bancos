
# ia_resumen_bancario.py
# Autor: Herramienta para uso interno - AIE San Justo | Developer: Alfonso Alderete
# Requisitos: streamlit, pdfplumber, pandas, numpy, python-dateutil

import io
import re
import pdfplumber
import pandas as pd
import numpy as np
from dateutil import parser as dateparser
import streamlit as st
from pathlib import Path

APP_TITLE = "IA Resumen Bancario"

# -----------------------------
# Utilidades
# -----------------------------
CUIT_RE = re.compile(r"\b\d{2}-?\d{8}-?\d\b")  # SIN grupos
MONEY_RE = re.compile(r"[-+]?\d{1,3}(?:\.\d{3})*,\d{2}|\d+(?:[.,]\d+)?")

def parse_money(text):
    if text is None:
        return np.nan
    t = str(text).strip()
    if t == "":
        return np.nan
    t = t.replace(" ", "").replace("$", "")
    if re.search(r",\d{1,2}$", t):
        t = t.replace(".", "").replace(",", ".")
    try:
        return float(t)
    except Exception:
        m = MONEY_RE.search(str(text))
        if m:
            return parse_money(m.group(0))
        return np.nan

def normalize_space(s):
    return re.sub(r"\s+", " ", str(s or "").strip())

def detect_date(s):
    s = normalize_space(s)
    for fmt in ("%d/%m/%Y", "%d-%m-%Y", "%d/%m/%y", "%d-%m-%y"):
        try:
            return pd.to_datetime(s, format=fmt, dayfirst=True, errors="raise")
        except Exception:
            pass
    try:
        return pd.to_datetime(dateparser.parse(s, dayfirst=True, fuzzy=True))
    except Exception:
        return pd.NaT

def extract_first_cuit(text):
    """Devuelve el primer CUIT encontrado en el texto o NaN (evita error de grupos)."""
    m = CUIT_RE.search(str(text))
    return m.group(0) if m else np.nan

# -----------------------------
# Extracción del PDF
# -----------------------------
def extract_tables_from_pdf(file_like) -> pd.DataFrame:
    """
    Devuelve DataFrame con: fecha, descripcion, debito, credito, saldo, pagina, fila
    SÓLO lo que lee del PDF (no inventa valores).
    """
    records = []
    with pdfplumber.open(file_like) as pdf:
        for pageno, page in enumerate(pdf.pages, start=1):
            # Intento 1: tablas por líneas
            tables = page.extract_tables(
                {
                    "vertical_strategy": "lines",
                    "horizontal_strategy": "lines",
                    "intersection_tolerance": 5,
                }
            ) or []
            # Intento 2: tablas por flujo (stream) si no detectó nada
            if not tables:
                tables = page.extract_tables({"vertical_strategy":"text","horizontal_strategy":"text"}) or []
            for table in tables:
                if not table:
                    continue
                header = [normalize_space(c) for c in (table[0] or [])]
                cols = [c.lower() for c in header]
                idx_fecha = idx_desc = idx_debito = idx_credito = idx_saldo = None
                for i, c in enumerate(cols):
                    if idx_fecha is None and "fecha" in c:
                        idx_fecha = i
                    if idx_desc is None and re.search(r"concepto|detalle|descrip|movim", c):
                        idx_desc = i
                    if idx_debito is None and re.search(r"d[eé]bito|debitos?", c):
                        idx_debito = i
                    if idx_credito is None and re.search(r"cr[eé]dito|creditos?", c):
                        idx_credito = i
                    if idx_saldo is None and "saldo" in c:
                        idx_saldo = i
                start_row = 1 if any([idx_fecha is not None, idx_desc is not None, idx_debito is not None, idx_credito is not None, idx_saldo is not None]) else 0
                for ridx, row in enumerate(table[start_row:], start=start_row):
                    cells = [normalize_space(c) for c in (row or [])]
                    if not any(cells):
                        continue
                    fecha = cells[idx_fecha] if idx_fecha is not None and idx_fecha < len(cells) else ""
                    desc  = cells[idx_desc]  if idx_desc  is not None and idx_desc  < len(cells) else " "
                    deb   = cells[idx_debito] if idx_debito is not None and idx_debito < len(cells) else ""
                    cred  = cells[idx_credito] if idx_credito is not None and idx_credito < len(cells) else ""
                    sal   = cells[idx_saldo] if idx_saldo is not None and idx_saldo < len(cells) else ""
                    records.append(
                        {
                            "fecha_raw": fecha,
                            "descripcion": desc,
                            "debito_raw": deb,
                            "credito_raw": cred,
                            "saldo_raw": sal,
                            "pagina": pageno,
                            "fila": ridx + 1,
                        }
                    )
    df = pd.DataFrame.from_records(records)
    if df.empty:
        return df
    df["fecha"] = df["fecha_raw"].apply(detect_date)
    df["debito"] = df["debito_raw"].apply(parse_money)
    df["credito"] = df["credito_raw"].apply(parse_money)
    df["saldo"] = df["saldo_raw"].apply(parse_money)
    df["descripcion"] = df["descripcion"].astype(str).apply(normalize_space)
    # FIX: extraer CUIT sin grupos
    df["cuit"] = df["descripcion"].apply(extract_first_cuit)
    mask_empty_amounts = df["debito"].isna() & df["credito"].isna()
    df = df[~(mask_empty_amounts & df["descripcion"].str.len().lt(2))].copy()
    return df.reset_index(drop=True)

def find_period_final_balance(pdf_file_like):
    """Busca “Saldo al dd/mm/yyyy” y su importe. Devuelve dict con 'fecha' y 'monto'."""
    with pdfplumber.open(pdf_file_like) as pdf:
        candidates = []
        for pageno, page in enumerate(pdf.pages, start=1):
            text = page.extract_text() or ""
            for line in text.splitlines():
                if re.search(r"Saldo al \d{1,2}[/\-]\d{1,2}[/\-]\d{2,4}", line, re.IGNORECASE):
                    m = MONEY_RE.search(line)
                    amount = parse_money(m.group(0)) if m else np.nan
                    dmatch = re.search(r"(\d{1,2}[/\-]\d{1,2}[/\-]\d{2,4})", line)
                    f = detect_date(dmatch.group(1)) if dmatch else pd.NaT
                    candidates.append({"pageno": pageno, "fecha": f, "monto": amount, "line": line})
        if candidates:
            return candidates[-1]
    return None

# -----------------------------
# Clasificación de movimientos
# -----------------------------
def clasificar(df: pd.DataFrame) -> pd.DataFrame:
    desc = df["descripcion"].str.upper()
    df["tipo"] = "OTROS"
    df.loc[desc.str.contains(r"\bTRANSFEREN(CIA|CIAS)?\b") & desc.str.contains(r"\bRECIBID", regex=True), "tipo"] = "TRF_RECIBIDA_TERCEROS"
    df.loc[desc.str.contains(r"\bTRANSFEREN(CIA|CIAS)?\b") & desc.str.contains(r"\bREALIZAD", regex=True), "tipo"] = "TRF_REALIZADA_TERCEROS"
    df.loc[desc.str.contains(r"PROPIA|MISMA TITULARIDAD|ENTRE CUENTAS", regex=True), "tipo"] = "TRF_PROPIAS"
    df.loc[desc.str.contains(r"\bAPI\b"), "tipo"] = "DEBITO_API"
    df.loc[desc.str.contains(r"\bARCA\b"), "tipo"] = "DEBITO_ARCA"
    df.loc[desc.str.contains(r"\bSIRCREB\b"), "tipo"] = "SIRCREB"
    df.loc[desc.str.contains(r"\bDY[ /]?C\b|DEUDA Y CREDITO|DGC", regex=True), "tipo"] = "DyC"
    df.loc[desc.str.contains(r"SEGURO|PRIMA|DEBITO AUTOMATICO|D[ée]BITO AUTOM", regex=True), "tipo"] = "DEBITO_AUTOMATICO"
    df.loc[desc.str.contains(r"COMISION|COMISI[ÓO]N|GASTO(S)? BANCARIO(S)?"), "tipo"] = "COMISION"
    df.loc[desc.str.contains(r"\bIVA\b"), "tipo"] = "IVA"
    df.loc[desc.str.contains(r"PERCEPCION\s+IVA|PERCEP\.?\s*IVA", regex=True), "tipo"] = "PERCEPCION_IVA"
    df["alicuota"] = np.where(df["tipo"].eq("IVA") & desc.str.contains(r"10[,\.]5"), 10.5,
                        np.where(df["tipo"].eq("IVA") & desc.str.contains(r"21"), 21.0, np.nan))
    return df

# -----------------------------
# Cálculos y Resúmenes
# -----------------------------
def construir_signo(df: pd.DataFrame):
    """Regla fija: Crédito RESTA (negativo), Débito SUMA (positivo)."""
    debit = df["debito"].fillna(0.0)
    credit = df["credito"].fillna(0.0)
    df["importe"] = debit - credit
    return df

def calcular_saldos(df: pd.DataFrame, saldo_final_ref: float | None):
    total_mov = df["importe"].sum()
    if saldo_final_ref is not None and not np.isnan(saldo_final_ref):
        saldo_inicial = saldo_final_ref - total_mov
        return saldo_inicial, saldo_final_ref
    return np.nan, np.nan

def resumen_por_categorias(df: pd.DataFrame) -> dict:
    out = {}
    out["trf_recibidas_terceros"] = df.loc[df["tipo"].eq("TRF_RECIBIDA_TERCEROS") & (df["importe"]>0), "importe"].sum()
    out["trf_realizadas_terceros"] = -df.loc[df["tipo"].eq("TRF_REALIZADA_TERCEROS") & (df["importe"]<0), "importe"].sum()
    out["trf_propias_recibidas"]  = df.loc[df["tipo"].eq("TRF_PROPIAS") & (df["importe"]>0), "importe"].sum()
    out["trf_propias_realizadas"] = -df.loc[df["tipo"].eq("TRF_PROPIAS") & (df["importe"]<0), "importe"].sum()
    out["debitos_api"]  = -df.loc[df["tipo"].eq("DEBITO_API") & (df["importe"]<0), "importe"].sum()
    out["debitos_arca"] = -df.loc[df["tipo"].eq("DEBITO_ARCA") & (df["importe"]<0), "importe"].sum()
    out["otros_debitos_automaticos"] = -df.loc[df["tipo"].eq("DEBITO_AUTOMATICO") & (df["importe"]<0), "importe"].sum()
    out["sircreb"] = -df.loc[df["tipo"].eq("SIRCREB") & (df["importe"]<0), "importe"].sum()
    out["dyc"]     = -df.loc[df["tipo"].eq("DyC") & (df["importe"]<0), "importe"].sum()
    out["comisiones_neto"] = -df.loc[df["tipo"].eq("COMISION") & (df["importe"]<0), "importe"].sum()
    iva_21 = -df.loc[df["tipo"].eq("IVA") & (df["alicuota"]==21.0) & (df["importe"]<0), "importe"].sum()
    iva_105 = -df.loc[df["tipo"].eq("IVA") & (df["alicuota"]==10.5) & (df["importe"]<0), "importe"].sum()
    out["iva_21"] = iva_21
    out["iva_105"] = iva_105
    out["percepciones_iva"] = -df.loc[df["tipo"].eq("PERCEPCION_IVA") & (df["importe"]<0), "importe"].sum()
    return out

def detalle_debitos_creditos(df: pd.DataFrame):
    debitos = df.loc[df["importe"] < 0].copy()
    debitos["monto"] = -debitos["importe"]
    creditos = df.loc[df["importe"] > 0].copy()
    creditos["monto"] = creditos["importe"]
    return debitos, creditos

# -----------------------------
# Streamlit UI
# -----------------------------
def main():
    st.set_page_config(page_title=APP_TITLE, page_icon="favicon-aie.ico")
    st.image("logo_aie.png", width=220)
    st.title(APP_TITLE)
    st.caption("Herramienta para uso interno - AIE San Justo · Developer: Alfonso Alderete")

    uploaded = st.file_uploader("Subí un PDF del resumen bancario", type=["pdf"])

    if uploaded is None:
        st.info("Cargá un PDF para procesar. La app no usa archivos por defecto en el servidor.")
        st.stop()

    file_like = io.BytesIO(uploaded.read())

    # Extraer movimientos
    df = extract_tables_from_pdf(file_like)
    if len(df) == 0:
        st.error("No se detectaron tablas con movimientos en el PDF.")
        st.stop()

    # Clasificar y aplicar signos (Crédito resta, Débito suma)
    df = clasificar(df)
    df = construir_signo(df)

    # Buscar saldo final en el texto del PDF
    file_like.seek(0)
    final_info = find_period_final_balance(file_like)
    saldo_final_ref = final_info["monto"] if final_info else np.nan
    fecha_final_ref = final_info["fecha"] if final_info else pd.NaT

    saldo_inicial, saldo_final = calcular_saldos(df, saldo_final_ref)

    # Orden
    df = df.sort_values(by=["fecha", "pagina", "fila"], kind="stable").reset_index(drop=True)

    # Resúmenes
    resumen = resumen_por_categorias(df)
    debitos, creditos = detalle_debitos_creditos(df)

    # Panel de Resumen
    st.subheader("Resumen del período")
    col1, col2 = st.columns(2)
    with col1:
        st.metric("Saldo inicial (calculado)", f"$ {saldo_inicial:,.2f}" if not np.isnan(saldo_inicial) else "—")
        st.metric("Saldo final (PDF)", f"$ {saldo_final:,.2f}" if not np.isnan(saldo_final) else "—")
        if pd.notna(fecha_final_ref):
            st.caption(f"Saldo al {fecha_final_ref.strftime('%d/%m/%Y')} (extraído del PDF).")
    with col2:
        st.metric("Transferencias recibidas (terceros)", f"$ {resumen['trf_recibidas_terceros']:,.2f}")
        st.metric("Transferencias realizadas (terceros)", f"$ {resumen['trf_realizadas_terceros']:,.2f}")
        st.metric("Entre cuentas propias - recibidas", f"$ {resumen['trf_propias_recibidas']:,.2f}")
        st.metric("Entre cuentas propias - realizadas", f"$ {resumen['trf_propias_realizadas']:,.2f}")

    st.divider()
    st.subheader("Débitos automáticos y retenciones")
    c1, c2, c3 = st.columns(3)
    with c1:
        st.metric("Débitos API", f"$ {resumen['debitos_api']:,.2f}")
        st.metric("Débitos ARCA", f"$ {resumen['debitos_arca']:,.2f}")
    with c2:
        st.metric("Otros débitos automáticos (seguros, etc.)", f"$ {resumen['otros_debitos_automaticos']:,.2f}")
        st.metric("Sircreb", f"$ {resumen['sircreb']:,.2f}")
    with c3:
        st.metric("DyC", f"$ {resumen['dyc']:,.2f}")
        st.metric("Percepciones IVA", f"$ {resumen['percepciones_iva']:,.2f}")

    st.divider()
    st.subheader("Gastos por comisiones e IVA (según líneas del PDF)")
    col_iva1, col_iva2, col_iva3 = st.columns(3)
    with col_iva1:
        st.metric("Comisiones (neto, líneas 'COMISION')", f"$ {resumen['comisiones_neto']:,.2f}")
    with col_iva2:
        st.metric("IVA 21% (líneas 'IVA 21')", f"$ {resumen['iva_21']:,.2f}")
    with col_iva3:
        st.metric("IVA 10.5% (líneas 'IVA 10,5')", f"$ {resumen['iva_105']:,.2f}")

    st.divider()
    st.subheader("Detalle completo de movimientos")
    show_cols = ["fecha", "descripcion", "debito", "credito", "importe", "saldo", "tipo", "cuit", "pagina", "fila"]
    st.dataframe(df[show_cols])

    st.subheader("Listado de TODOS los Débitos")
    st.dataframe(debitos[["fecha", "descripcion", "monto", "tipo", "cuit", "pagina", "fila"]])

    st.subheader("Listado de TODOS los Créditos")
    st.dataframe(creditos[["fecha", "descripcion", "monto", "tipo", "cuit", "pagina", "fila"]])

    st.divider()
    st.subheader("Transferencias de y a terceros por CUIT")
    trf = df[df["tipo"].isin(["TRF_RECIBIDA_TERCEROS", "TRF_REALIZADA_TERCEROS"])].copy()
    trf["tercero_cuit"] = trf["cuit"]
    agrup = trf.groupby(["tipo", "tercero_cuit"], dropna=False)["importe"].sum().reset_index()
    agrup["importe_abs"] = agrup["importe"].abs()
    st.dataframe(agrup.sort_values("importe_abs", ascending=False)[["tipo", "tercero_cuit", "importe"]])

if __name__ == "__main__":
    main()
