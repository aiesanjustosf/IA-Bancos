# ia_resumen_bancario.py
# Herramienta para uso interno - AIE San Justo

import io, re
from pathlib import Path
import numpy as np
import pandas as pd
import streamlit as st

# ============== UI / assets ==============
HERE = Path(__file__).parent
LOGO = HERE / "logo_aie.png"
FAVICON = HERE / "favicon-aie.ico"
st.set_page_config(page_title="IA Resumen Bancario",
                   page_icon=str(FAVICON) if FAVICON.exists() else None,
                   layout="wide")
if LOGO.exists():
    st.image(str(LOGO), width=200)
st.title("IA Resumen Bancario")

# ============== deps diferidas ==============
try:
    import pdfplumber
except Exception as e:
    st.error(f"No se pudo importar pdfplumber: {e}\nRevisá requirements.txt")
    st.stop()

# PDF (Resumen Operativo IVA)
try:
    from reportlab.lib.pagesizes import A4
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.lib import colors
    REPORTLAB_OK = True
except Exception:
    REPORTLAB_OK = False

# ============== regex base ==============
DATE_RE  = re.compile(r"\b\d{1,2}/\d{2}/\d{2,4}\b")  # dd/mm/aa(aa)
MONEY_RE = re.compile(r'(?<!\S)(?:\d{1,3}(?:\.\d{3})*|\d+)\s?,\s?\d{2}-?(?!\S)')
LONG_INT_RE = re.compile(r"\b\d{6,}\b")

# ==================== utils ====================
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

def fmt_ar(n) -> str:
    if n is None or (isinstance(n, float) and np.isnan(n)):
        return "—"
    return f"{n:,.2f}".replace(",", "§").replace(".", ",").replace("§", ".")

def lines_from_text(page):
    txt = page.extract_text() or ""
    return [" ".join(l.split()) for l in txt.splitlines()]

def lines_from_words(page, ytol=2.0):
    words = page.extract_words(extra_attrs=["x0", "top"])
    if not words: return []
    words.sort(key=lambda w: (round(w["top"]/ytol), w["x0"]))
    lines, cur, band = [], [], None
    for w in words:
        b = round(w["top"]/ytol)
        if band is None or b == band: cur.append(w)
        else:
            lines.append(" ".join(x["text"] for x in cur)); cur = [w]
        band = b
    if cur: lines.append(" ".join(x["text"] for x in cur))
    return [" ".join(l.split()) for l in lines]

def extract_all_lines(file_like):
    out = []
    with pdfplumber.open(file_like) as pdf:
        for pi, p in enumerate(pdf.pages, start=1):
            lt = lines_from_text(p)
            lw = lines_from_words(p, ytol=2.0)
            seen = set(lt)
            combined = lt + [l for l in lw if l not in seen]
            out.extend([(pi, l) for l in combined if l.strip()])
    return out

def _text_from_pdf(file_like) -> str:
    try:
        with pdfplumber.open(file_like) as pdf:
            return "\n".join((p.extract_text() or "") for p in pdf.pages)
    except Exception:
        return ""

# ==================== detección de banco ====================
BANK_MACRO_HINTS   = ("BANCO MACRO","CUENTA CORRIENTE BANCARIA","SALDO ULTIMO EXTRACTO AL","DEBITO FISCAL IVA BASICO","N/D DBCR 25413")
BANK_SANTAFE_HINTS = ("BANCO DE SANTA FE","NUEVO BANCO DE SANTA FE","SALDO ANTERIOR","IMPTRANS","IVA GRAL")
BNA_NAME_HINT      = "BANCO DE LA NACION ARGENTINA"
BANK_NACION_HINTS  = (BNA_NAME_HINT,"SALDO ANTERIOR","SALDO FINAL","I.V.A. BASE","COMIS.")
BANK_CREDICOOP_HINTS = ("BANCO CREDICOOP","BANCO CREDICOOP COOPERATIVO LIMITADO","IMPUESTO LEY 25.413","I.V.A.","TRANSFERENCIAS PESOS","CTA.")
BANK_SANTANDER_HINTS = ("BANCO SANTANDER","SANTANDER RIO","DETALLE DE MOVIMIENTO","SALDO INICIAL","SALDO FINAL","SALDO TOTAL")
BANK_GALICIA_HINTS   = ("BANCO GALICIA","RESUMEN DE CUENTA","DESCRIPCIÓN ORIGEN CRÉDITO DÉBITO SALDO","SIRCREB","ING. BRUTOS S/ CRED","IMP. DEB. LEY 25413","IMP. CRE. LEY 25413")

def detect_bank_from_text(txt: str) -> str:
    U = (txt or "").upper()

    # Prioridad absoluta: si aparece "BANCO GALICIA" → Galicia
    if "BANCO GALICIA" in U:
        return "Banco Galicia"

    scores = [
        ("Banco Macro", sum(1 for k in BANK_MACRO_HINTS if k in U)),
        ("Banco de Santa Fe", sum(1 for k in BANK_SANTAFE_HINTS if k in U)),
        ("Banco de la Nación Argentina", sum(1 for k in BANK_NACION_HINTS if k in U)),
        ("Banco Santander", sum(1 for k in BANK_SANTANDER_HINTS if k in U)),
        ("Banco Credicoop", sum(1 for k in BANK_CREDICOOP_HINTS if k in U)),
        ("Banco Galicia", sum(1 for k in BANK_GALICIA_HINTS if k in U)),  # refuerzo
    ]
    scores.sort(key=lambda x: x[1], reverse=True)
    return scores[0][0] if scores[0][1] > 0 else "Banco no identificado"

# ==================== Clasificación (genérica + Galicia) ====================
RE_SIRCREB = re.compile(r"(SIRCREB|ING\.?\s*BRUTOS\s*/?\s*CRED)", re.IGNORECASE)
RE_LEY25413_CRE = re.compile(r"IMP\.?\s*CRE\.?\s*LEY\s*25\.?413", re.IGNORECASE)
RE_LEY25413_DEB = re.compile(r"IMP\.?\s*DEB\.?\s*LEY\s*25\.?413", re.IGNORECASE)
RE_COM_CTA  = re.compile(r"COMISI[ÓO]N\s+SERVICIO\s+DE\s+CUENTA", re.IGNORECASE)
RE_COM_CHQ  = re.compile(r"COM\.\s*DEPOSITO\s*DE\s*CHEQUE", re.IGNORECASE)
RE_IVA      = re.compile(r"\bIVA\b", re.IGNORECASE)

def clasificar_galicia(desc: str, deb: float, cre: float) -> str:
    u = (desc or "").upper()

    if RE_SIRCREB.search(u):
        return "SIRCREB"
    if RE_LEY25413_CRE.search(u) or RE_LEY25413_DEB.search(u):
        return "LEY 25.413"
    if RE_COM_CTA.search(u):
        return "COMISION SERVICIO DE CUENTA (neto)"
    if RE_COM_CHQ.search(u):
        return "COM. DEPOSITO DE CHEQUE (neto)"
    if RE_IVA.search(u):
        # si en algún extracto existiera 10,5 distinguimos
        if "10,5" in u or "10.5" in u:
            return "IVA 10,5%"
        return "IVA 21%"
    return "Otros"

# ==================== GALICIA: parser ====================
def galicia_parse_lines(all_lines: list[str]) -> pd.DataFrame:
    """
    Espera filas con: dd/mm/aa ... [CRÉDITO] [DÉBITO] [SALDO]
    En Galicia suele venir en ese orden: CREDITO · DEBITO · SALDO.
    También acepta importes negativos (trailing '-') para detectar débitos.
    """
    rows = []
    prev_saldo = None
    seq = 0
    for s in all_lines:
        s = s.strip()
        if not s:
            continue

        # encabezados y totales fuera
        U = s.upper()
        if ("DESCRIPC" in U and "CR" in U and "DEB" in U and "SALDO" in U):
            continue
        if "RESUMEN DEL PERÍODO" in U or "TOTAL" in U:
            continue

        mdate = DATE_RE.search(s)
        am = list(MONEY_RE.finditer(s))
        if not mdate or len(am) < 2:
            continue

        # Tomamos siempre último como saldo
        saldo = normalize_money(am[-1].group(0))

        # Intentamos tomar crédito y débito (si hay 3+ importes)
        deb = 0.0
        cre = 0.0
        if len(am) >= 3:
            cre = normalize_money(am[-3].group(0))
            deb = normalize_money(am[-2].group(0))
        else:
            # Solo un importe + saldo → inferimos por delta o signo
            imp = normalize_money(am[0].group(0))
            if imp < 0:
                deb = -imp
            elif prev_saldo is not None:
                delta = saldo - prev_saldo
                if abs(delta - imp) < 0.02:
                    cre = imp
                elif abs(delta + imp) < 0.02:
                    deb = imp
                else:
                    # fallback: si dice ACREDIT., TRANSF. → crédito
                    if "ACREDIT" in U or "TRANSF" in U:
                        cre = imp
                    else:
                        deb = imp
            else:
                # primera fila: sin referencia, asumimos por palabra
                if "ACREDIT" in U or "TRANSF" in U:
                    cre = imp
                else:
                    deb = imp

        first_amt_start = am[0].start()
        desc = s[mdate.end():first_amt_start].strip()

        seq += 1
        rows.append({
            "fecha": pd.to_datetime(mdate.group(0), dayfirst=True, errors="coerce"),
            "descripcion": desc,
            "debito": float(max(deb, 0.0)),
            "credito": float(max(cre, 0.0)),
            "saldo": float(saldo) if saldo is not None else np.nan,
            "orden": seq
        })
        prev_saldo = saldo

    df = pd.DataFrame(rows)
    if df.empty:
        return df

    # Normalización de descripción
    df["desc_norm"] = df["descripcion"].astype(str).str.upper().str.replace(r"\s+", " ", regex=True)
    return df.sort_values(["fecha","orden"]).reset_index(drop=True)

# ==================== GALICIA: saldo inicial + resumen ====================
def galicia_compute_initial_balance(df: pd.DataFrame) -> float:
    """
    SOLO GALICIA:
    saldo_inicial = primer_saldo + (primer_débito - primer_crédito)
    """
    first = df.iloc[0]
    return float(first["saldo"]) + float(first["debito"]) - float(first["credito"])

def galicia_operativo(df: pd.DataFrame):
    """
    Devuelve dict con:
      neto_comisiones_21, iva_21, bruto_21,
      neto_comisiones_105, iva_105, bruto_105,
      percep_iva, ley_25413_neto, sircreb
    """
    # Clasificación específica
    df = df.copy()
    df["Clasificación"] = df.apply(lambda r: clasificar_galicia(r["descripcion"], r["debito"], r["credito"]), axis=1)

    # IVA
    iva21  = float(df.loc[df["Clasificación"].eq("IVA 21%"),  "debito"].sum())
    iva105 = float(df.loc[df["Clasificación"].eq("IVA 10,5%"), "debito"].sum())
    net21  = round(iva21 / 0.21, 2) if iva21 else 0.0
    net105 = round(iva105 / 0.105, 2) if iva105 else 0.0

    # Comisiones (neto) – filas con “COMISION SERVICIO DE CUENTA” y “COM. DEPOSITO DE CHEQUE”
    com_cta = float(df.loc[df["Clasificación"].eq("COMISION SERVICIO DE CUENTA (neto)"), "debito"].sum())
    com_chq = float(df.loc[df["Clasificación"].eq("COM. DEPOSITO DE CHEQUE (neto)"), "debito"].sum())
    # Si los IVAs vienen en filas separadas ya están arriba; aquí solo netos.

    percep_iva = 0.0  # Galicia suele traer “PERCEP. IVA” como texto plano → contarlo como percepción:
    percep_iva = float(df.loc[df["desc_norm"].str.contains("PERCEP. IVA"), "debito"].sum())

    # Ley 25.413 → débitos menos créditos (créditos son devoluciones)
    ley_deb = float(df.loc[df["Clasificación"].eq("LEY 25.413"), "debito"].sum())
    ley_cre = float(df.loc[df["Clasificación"].eq("LEY 25.413"), "credito"].sum())
    ley_25413_neto = ley_deb - ley_cre

    sircreb = float(df.loc[df["Clasificación"].eq("SIRCREB"), "debito"].sum())

    # Sumatoria de netos de comisiones (para mostrar “Neto Comisiones 21% (2 rubros)” como pediste)
    neto_comisiones_21 = round(net21, 2)  # neto contable por IVA21
    # agregamos netos explícitos si el banco separa (servicio cuenta / depósito cheque)
    neto_comisiones_21 += com_cta + com_chq

    return {
        "neto_comisiones_21": neto_comisiones_21,
        "iva_21": iva21,
        "bruto_21": neto_comisiones_21 + iva21,
        "neto_comisiones_105": net105,
        "iva_105": iva105,
        "bruto_105": net105 + iva105,
        "percep_iva": percep_iva,
        "ley_25413_neto": ley_25413_neto,
        "sircreb": sircreb,
    }, df

# ==================== Render (genérico reducido) ====================
def render_resumen_periodo(saldo_inicial, total_creditos, total_debitos, saldo_final_pdf, saldo_final_calc):
    diferencia = saldo_final_calc - saldo_final_pdf
    cuadra = abs(diferencia) < 0.01
    st.caption("Resumen del período")
    c1, c2, c3 = st.columns(3)
    with c1: st.metric("Saldo inicial", f"$ {fmt_ar(saldo_inicial)}")
    with c2: st.metric("Total créditos (+)", f"$ {fmt_ar(total_creditos)}")
    with c3: st.metric("Total débitos (–)", f"$ {fmt_ar(total_debitos)}")
    c4, c5, c6 = st.columns(3)
    with c4: st.metric("Saldo final (PDF)", f"$ {fmt_ar(saldo_final_pdf)}")
    with c5: st.metric("Saldo final calculado", f"$ {fmt_ar(saldo_final_calc)}")
    with c6: st.metric("Diferencia", f"$ {fmt_ar(diferencia)}")
    try:
        st.success("Conciliado.") if cuadra else st.error("No cuadra la conciliación.")
    except Exception:
        st.write("Conciliación:", "OK" if cuadra else "No cuadra")

# ==================== MAIN ====================
uploaded = st.file_uploader("Subí un PDF del resumen bancario", type=["pdf"])
if uploaded is None:
    st.info("La app no almacena datos, toda la información está protegida.")
    st.stop()

data = uploaded.read()
_bank_txt = _text_from_pdf(io.BytesIO(data))
_auto_bank_name = detect_bank_from_text(_bank_txt)

with st.expander("Opciones avanzadas (detección de banco)", expanded=False):
    forced = st.selectbox(
        "Forzar identificación del banco",
        options=(
            "Auto (detectar)",
            "Banco de Santa Fe",
            "Banco Macro",
            "Banco de la Nación Argentina",
            "Banco Credicoop",
            "Banco Santander",
            "Banco Galicia",
        ),
        index=0,
        help="Solo cambia la etiqueta informativa y el nombre de archivo."
    )

_bank_name = forced if forced != "Auto (detectar)" else _auto_bank_name

if _bank_name == "Banco Galicia":
    st.success("Detectado: Banco Galicia")
    all_lines = [l for _, l in extract_all_lines(io.BytesIO(data))]

    # Parse específico Galicia
    df = galicia_parse_lines(all_lines)

    if df.empty:
        st.warning("No se pudieron leer movimientos de Galicia.")
        st.stop()

    # Saldo inicial (solo Galicia, como pediste)
    saldo_inicial = galicia_compute_initial_balance(df)
    total_debitos  = float(df["debito"].sum())
    total_creditos = float(df["credito"].sum())
    saldo_final_pdf = float(df["saldo"].iloc[-1])
    saldo_final_calc = saldo_inicial + total_creditos - total_debitos

    # Resumen Operativo
    op, dfc = galicia_operativo(df)

    # ------ UI ------
    st.markdown("---")
    st.subheader("Cuenta Corriente (Galicia) · Nro s/n")

    render_resumen_periodo(saldo_inicial, total_creditos, total_debitos, saldo_final_pdf, saldo_final_calc)

    st.caption("Resumen Operativo: Registración Módulo IVA (Galicia)")
    m1, m2, m3 = st.columns(3)
    with m1: st.metric("Neto Comisiones (2 rubros)", f"$ {fmt_ar(op['neto_comisiones_21'])}")
    with m2: st.metric("IVA 21% (sobre comisiones)", f"$ {fmt_ar(op['iva_21'])}")
    with m3: st.metric("Bruto 21%", f"$ {fmt_ar(op['bruto_21'])}")

    n1, n2, n3 = st.columns(3)
    with n1: st.metric("Neto Comisiones 10,5%", f"$ {fmt_ar(op['neto_comisiones_105'])}")
    with n2: st.metric("IVA 10,5%", f"$ {fmt_ar(op['iva_105'])}")
    with n3: st.metric("Bruto 10,5%", f"$ {fmt_ar(op['bruto_105'])}")

    o1, o2, o3 = st.columns(3)
    with o1: st.metric("Percepciones de IVA", f"$ {fmt_ar(op['percep_iva'])}")
    with o2: st.metric("Ley 25.413 (neto)", f"$ {fmt_ar(op['ley_25413_neto'])}")
    with o3: st.metric("ING. BRUTOS S/ CRED (SIRCREB)", f"$ {fmt_ar(op['sircreb'])}")

    st.caption("Detalle de movimientos")
    # columnas visibles
    show = dfc[["fecha","descripcion","debito","credito","saldo"]].copy()
    # grilla ancha y números grandes
    try:
        from streamlit import column_config as cc  # st.column_config
        st.dataframe(
            show,
            use_container_width=True,
            column_config={
                "fecha": cc.DatetimeColumn("fecha", format="DD/MM/YYYY"),
                "descripcion": cc.TextColumn("descripcion", width="large"),
                "debito": cc.NumberColumn("debito", format="%.2f"),
                "credito": cc.NumberColumn("credito", format="%.2f"),
                "saldo": cc.NumberColumn("saldo", format="%.2f", width="medium"),
            },
        )
    except Exception:
        styled = show.style.format({c: fmt_ar for c in ["debito","credito","saldo"]}, na_rep="—")
        st.dataframe(styled, use_container_width=True)

    # Descarga
    st.caption("Descargar")
    try:
        import xlsxwriter
        output = io.BytesIO()
        with pd.ExcelWriter(output, engine="xlsxwriter") as writer:
            show.to_excel(writer, index=False, sheet_name="Movimientos")
            wb = writer.book; ws = writer.sheets["Movimientos"]
            money_fmt = wb.add_format({"num_format": "#,##0.00"})
            date_fmt  = wb.add_format({"num_format": "dd/mm/yyyy"})
            for idx, col in enumerate(show.columns, start=0):
                col_values = show[col].astype(str)
                max_len = max(len(col), *(len(v) for v in col_values))
                ws.set_column(idx, idx, min(max_len + 2, 50))
            for c in ["debito","credito","saldo"]:
                j = show.columns.get_loc(c); ws.set_column(j, j, 16, money_fmt)
            j = show.columns.get_loc("fecha"); ws.set_column(j, j, 14, date_fmt)

        st.download_button(
            "📥 Descargar Excel",
            data=output.getvalue(),
            file_name=f"resumen_bancario_galicia.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            use_container_width=True,
            key="dl_xlsx_galicia",
        )
    except Exception:
        csv_bytes = show.to_csv(index=False).encode("utf-8-sig")
        st.download_button(
            "📥 Descargar CSV (fallback)",
            data=csv_bytes,
            file_name=f"resumen_bancario_galicia.csv",
            mime="text/csv",
            use_container_width=True,
            key="dl_csv_galicia",
        )

else:
    # NO TOCADO: solo un banner y finalizamos para no alterar otros bancos.
    if _bank_name == "Banco Macro":
        st.info("Detectado: Banco Macro")
    elif _bank_name == "Banco de Santa Fe":
        st.success("Detectado: Banco de Santa Fe")
    elif _bank_name == "Banco de la Nación Argentina":
        st.success("Detectado: Banco de la Nación Argentina")
    elif _bank_name == "Banco Santander":
        st.success("Detectado: Banco Santander")
    elif _bank_name == "Banco Credicoop":
        st.success("Detectado: Banco Credicoop")
    else:
        st.warning("No se pudo identificar el banco automáticamente.")
    st.stop()
