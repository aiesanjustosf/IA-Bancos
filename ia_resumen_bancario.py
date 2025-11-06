# ia_resumen_bancario.py
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
st.set_page_config(page_title="IA Resumen Bancario", page_icon=str(FAVICON) if FAVICON.exists() else None)
if LOGO.exists():
    st.image(str(LOGO), width=200)
st.title("IA Resumen Bancario")

# --- deps diferidas ---
try:
    import pdfplumber
except Exception as e:
    st.error(f"No se pudo importar pdfplumber: {e}\nRevisá requirements.txt")
    st.stop()

# Para PDF del “Resumen Operativo: Registración Módulo IVA”
try:
    from reportlab.lib.pagesizes import A4
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.lib import colors
    REPORTLAB_OK = True
except Exception:
    REPORTLAB_OK = False

# --- regex base ---
DATE_RE  = re.compile(r"\b\d{1,2}/\d{2}/\d{2,4}\b")
MONEY_RE = re.compile(r'(?<!\S)-?(?:\d{1,3}(?:\.\d{3})*|\d+)\s?,\s?\d{2}-?(?!\S)')
LONG_INT_RE = re.compile(r"\b\d{6,}\b")

# ====== PATRONES ESPECÍFICOS ======
# ---- Banco Macro ----
HYPH = r"[-\u2010\u2011\u2012\u2013\u2014\u2212]"
ACCOUNT_TOKEN_RE = re.compile(rf"\b\d\s*{HYPH}\s*\d{{3}}\s*{HYPH}\s*\d{{10}}\s*{HYPH}\s*\d\b")
SALDO_ANT_PREFIX   = re.compile(r"^SALDO\s+U?LTIMO\s+EXTRACTO\s+AL", re.IGNORECASE)
SALDO_FINAL_PREFIX = re.compile(r"^SALDO\s+FINAL\s+AL\s+D[ÍI]A",     re.IGNORECASE)
RE_MACRO_ACC_START = re.compile(r"^CUENTA\s+(.+)$", re.IGNORECASE)
RE_HAS_NRO         = re.compile(r"\bN[ROº°\.]*\s*:?\b", re.IGNORECASE)
RE_MACRO_ACC_NRO   = re.compile(rf"N[ROº°\.]*\s*:?\s*({ACCOUNT_TOKEN_RE.pattern})", re.IGNORECASE)
PER_PAGE_TITLE_PAT = re.compile(rf"^CUENTA\s+.+N[ROº°\.]*\s*:?\s*({ACCOUNT_TOKEN_RE.pattern})", re.IGNORECASE)
HEADER_ROW_PAT = re.compile(r"^(FECHA\s+DESCRIPC(?:I[ÓO]N|ION)|FECHA\s+CONCEPTO|FECHA\s+DETALLE).*(SALDO|D[ÉE]BITO|CR[ÉE]DITO)", re.IGNORECASE)
NON_MOV_PAT    = re.compile(r"(INFORMACI[ÓO]N\s+DE\s+SU/S\s+CUENTA/S|TOTAL\s+RESUMEN\s+OPERATIVO|RESUMEN\s+DEL\s+PER[IÍ]ODO)", re.IGNORECASE)
INFO_HEADER    = re.compile(r"INFORMACI[ÓO]N\s+DE\s+SU/S\s+CUENTA/S", re.IGNORECASE)

# ---- Banco de Santa Fe ----
SF_ACC_LINE_RE = re.compile(
    r"\b(Cuenta\s+Corriente\s+Pesos|Cuenta\s+Corriente\s+En\s+D[óo]lares|Caja\s+de\s+Ahorro\s+Pesos|Caja\s+de\s+Ahorro\s+En\s+D[óo]lares)\s+Nro\.?\s*([0-9][0-9./-]*)",
    re.IGNORECASE
)

# ---- Banco Nación (BNA) ----
BNA_NAME_HINT = "BANCO DE LA NACION ARGENTINA"
BNA_PERIODO_RE = re.compile(r"PERIODO:\s*(\d{2}/\d{2}/\d{4})\s*AL\s*(\d{2}/\d{2}/\d{4})", re.IGNORECASE)
BNA_CUENTA_CBU_RE = re.compile(
    r"NRO\.\s*CUENTA\s+SUCURSAL\s+CLAVE\s+BANCARIA\s+UNIFORME\s+\(CBU\)\s*[\r\n]+(\d+)\s+\d+\s+(\d{22})",
    re.IGNORECASE
)
BNA_ACC_ONLY_RE = re.compile(r"NRO\.\s*CUENTA\s+SUCURSAL\s*[:\-]?\s*[\r\n ]+(\d{6,})", re.IGNORECASE)
BNA_GASTOS_RE = re.compile(r"-\s*(INTERESES|COMISION|SELLADOS|I\.V\.A\.?\s*BASE|SEGURO\s+DE\s+VIDA)\s*\$\s*([0-9\.\s]+,\d{2})", re.IGNORECASE)

# ---- Banco Santander (hints) ----
BANK_SANTANDER_HINTS = ("BANCO SANTANDER","SANTANDER RIO","DETALLE DE MOVIMIENTO","SALDO INICIAL","SALDO FINAL","SALDO TOTAL")

# ---- Banco Galicia ----
BANK_GALICIA_HINTS = ("BANCO GALICIA","RESUMEN DE CUENTA","SIRCREB","IMP. DEB./CRE. LEY 25413","TRANSFERENCIA DE TERCEROS")
GALICIA_HEADER_RE  = re.compile(r"\bFECHA\s+DESCRIPCI[ÓO]N\s+ORIGEN\s+CR[ÉE]DITO\s+D[ÉE]BITO\s+SALDO\b", re.I)
GAL_SALDO_INICIAL_RE = re.compile(r"SALDO\s+INICIAL.*?(-?(?:\d{1,3}(?:\.\d{3})*|\d+)\s?,\s?\d{2}-?)", re.I)
GAL_SALDO_FINAL_RE   = re.compile(r"SALDO\s+FINAL.*?(-?(?:\d{1,3}(?:\.\d{3})*|\d+)\s?,\s?\d{2}-?)", re.I)

# ---- Santa Fe - "SALDO ULTIMO RESUMEN" ----
SF_SALDO_ULT_RE = re.compile(r"SALDO\s+U?LTIMO\s+RESUMEN", re.IGNORECASE)

# --- utils ---
def normalize_money(tok: str) -> float:
    if not tok: return np.nan
    tok = tok.strip().replace("−","-").replace("\u2212","-")
    neg = tok.endswith("-") or tok.startswith("-")
    tok = tok.lstrip("-").rstrip("-")
    if "," not in tok: return np.nan
    main, frac = tok.rsplit(",", 1)
    main = main.replace(".","").replace(" ","")
    try:
        val = float(f"{main}.{frac}")
        return -val if neg else val
    except Exception:
        return np.nan

def fmt_ar(n) -> str:
    if n is None or (isinstance(n, float) and np.isnan(n)): return "—"
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

def normalize_desc(desc: str) -> str:
    if not desc: return ""
    u = desc.upper()
    for pref in ("SAN JUS ","CASA RO ","CENTRAL ","GOBERNA ","GOBERNADOR ","SANTA FE ","ROSARIO "):
        if u.startswith(pref): u = u[len(pref):]; break
    u = LONG_INT_RE.sub("", u)
    return " ".join(u.split())

# ---------- Detección de banco (Galicia PRIORIDAD) ----------
BANK_MACRO_HINTS   = ("BANCO MACRO","CUENTA CORRIENTE BANCARIA","SALDO ULTIMO EXTRACTO AL","DEBITO FISCAL IVA BASICO","N/D DBCR 25413")
BANK_SANTAFE_HINTS = ("BANCO DE SANTA FE","NUEVO BANCO DE SANTA FE","SALDO ANTERIOR","IMPTRANS","IVA GRAL")
BANK_NACION_HINTS  = (BNA_NAME_HINT, "SALDO ANTERIOR", "SALDO FINAL", "I.V.A. BASE", "COMIS.")

def _text_from_pdf(file_like) -> str:
    try:
        with pdfplumber.open(file_like) as pdf:
            return "\n".join((p.extract_text() or "") for p in pdf.pages)
    except Exception:
        return ""

def detect_bank_from_text(txt: str) -> str:
    U = (txt or "").upper()
    if ("BANCO GALICIA" in U) or ("RESUMEN DE CUENTA" in U) or GALICIA_HEADER_RE.search(U):
        return "Banco Galicia"
    scores = [
        ("Banco Macro",                  sum(1 for k in BANK_MACRO_HINTS      if k in U)),
        ("Banco de Santa Fe",            sum(1 for k in BANK_SANTAFE_HINTS    if k in U)),
        ("Banco de la Nación Argentina", sum(1 for k in BANK_NACION_HINTS     if k in U)),
        ("Banco Santander",              sum(1 for k in BANK_SANTANDER_HINTS  if k in U)),
    ]
    scores.sort(key=lambda x: x[1], reverse=True)
    return scores[0][0] if scores[0][1] > 0 else "Banco no identificado"

# ---------- extracción de líneas ----------
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

# ---------- Saldos ----------
def _only_one_amount(line: str) -> bool:
    return len(list(MONEY_RE.finditer(line))) == 1
def _first_amount_value(line: str) -> float:
    m = MONEY_RE.search(line)
    return normalize_money(m.group(0)) if m else np.nan

def find_saldo_final_from_lines(lines):
    for ln in reversed(lines):
        if SALDO_FINAL_PREFIX.match(ln):
            d = DATE_RE.search(ln)
            if d and _only_one_amount(ln):
                fecha = pd.to_datetime(d.group(0), dayfirst=True, errors="coerce")
                saldo = _first_amount_value(ln)
                if pd.notna(fecha) and not np.isnan(saldo): return fecha, saldo
    for ln in reversed(lines):
        if "SALDO FINAL" in ln.upper() and _only_one_amount(ln):
            saldo = _first_amount_value(ln)
            if not np.isnan(saldo): return pd.NaT, saldo
    return pd.NaT, np.nan

def find_saldo_anterior_from_lines(lines):
    for ln in lines:
        if SALDO_ANT_PREFIX.match(ln):
            d = DATE_RE.search(ln)
            if d and _only_one_amount(ln):
                saldo = _first_amount_value(ln)
                if not np.isnan(saldo): return saldo
    for ln in lines:
        U = ln.upper()
        if "SALDO ANTERIOR" in U and _only_one_amount(ln):
            saldo = _first_amount_value(ln)
            if not np.isnan(saldo): return saldo
    for ln in lines:
        U = ln.upper()
        if "SALDO ULTIMO EXTRACTO" in U or "SALDO ÚLTIMO EXTRACTO" in U:
            d = DATE_RE.search(ln)
            if d and _only_one_amount(ln):
                saldo = _first_amount_value(ln)
                if not np.isnan(saldo): return saldo
    for i, ln in enumerate(lines):
        if SF_SALDO_ULT_RE.search(ln):
            if _only_one_amount(ln):
                v = _first_amount_value(ln)
                if not np.isnan(v): return v
            for j in (i+1, i+2):
                if 0 <= j < len(lines):
                    ln2 = lines[j]
                    if _only_one_amount(ln2):
                        v2 = _first_amount_value(ln2)
                        if not np.isnan(v2): return v2
            break
    return np.nan

# ---------- Render helpers ----------
def metric_text(label: str, value: float):
    st.markdown(
        f"""
        <div style="line-height:1.1">
          <div style="font-size:12px;color:#666">{label}</div>
          <div style="font-size:22px;font-weight:600;font-variant-numeric: tabular-nums">$ {fmt_ar(value)}</div>
        </div>
        """,
        unsafe_allow_html=True
    )

# ---------- Clasificación (común) ----------
RE_SIRCREB = re.compile(r"\bSIRCREB\b", re.IGNORECASE)
RE_PERCEP_RG2408 = re.compile(r"\bPERCEP\.?\s*IVA|\bRG\.?\s*2408\b", re.IGNORECASE)
RE_LEY25413 = re.compile(r"\bLEY\s*25\.?413|IMPDBCR\s*25413|N/?D\s*DBCR\s*25413", re.IGNORECASE)

def clasificar(desc: str, desc_norm: str, deb: float, cre: float) -> str:
    u = (desc or "").upper()
    n = (desc_norm or "").upper()
    if "SALDO ANTERIOR" in u or "SALDO ANTERIOR" in n: return "SALDO ANTERIOR"
    if RE_LEY25413.search(u) or RE_LEY25413.search(n): return "LEY 25.413"
    if RE_SIRCREB.search(u) or RE_SIRCREB.search(n):   return "SIRCREB"
    if RE_PERCEP_RG2408.search(u) or RE_PERCEP_RG2408.search(n): return "Percepciones de IVA"
    if ("I.V.A. BASE" in u) or ("IVA GRAL" in u) or ("DEBITO FISCAL" in u) or ("IVA" in u and "PERCEP" not in u):
        return "IVA 21% (sobre comisiones)"
    if ("COMIS" in n) or ("MANTENIMIENTO" in n) or ("GASTOS" in n): return "Gastos por comisiones"
    if "DB-AUT" in n or "DEB.AUT" in n: return "Débito automático"
    if ("CR-TRSFE" in n or "TRANSF RECIB" in n): return "Transferencia de terceros recibida" if cre else "Crédito"
    if ("DB-TRSFE" in n or "TRSFE-ET" in n):    return "Transferencia a terceros realizada" if deb else "Débito"
    if cre: return "Crédito"
    if deb: return "Débito"
    return "Otros"

# ---------- Parsing líneas (genérico) ----------
def parse_lines(lines) -> pd.DataFrame:
    rows, seq = [], 0
    for ln in lines:
        if not ln.strip(): continue
        if PER_PAGE_TITLE_PAT.search(ln) or HEADER_ROW_PAT.search(ln) or NON_MOV_PAT.search(ln): continue
        am = list(MONEY_RE.finditer(ln))
        if len(am) < 2: continue
        d = DATE_RE.search(ln)
        if not d or d.end() >= am[0].start(): continue
        saldo = normalize_money(am[-1].group(0))
        monto = normalize_money(am[-2].group(0))
        desc  = ln[d.end(): am[0].start()].strip()
        seq += 1
        rows.append({
            "fecha": pd.to_datetime(d.group(0), dayfirst=True, errors="coerce"),
            "descripcion": desc,
            "desc_norm": normalize_desc(desc),
            "debito": 0.0, "credito": 0.0,
            "importe": monto, "monto_pdf": monto,
            "saldo": saldo, "pagina": 0, "orden": seq
        })
    return pd.DataFrame(rows)

# ---------- RENDER: cuenta genérica (no Galicia) ----------
def render_account_report_generic(
    banco_slug: str, account_title: str, account_number: str, acc_id: str,
    lines: list[str], bna_extras: dict | None = None
):
    st.markdown("---")
    st.subheader(f"{account_title} · Nro {account_number}")

    df = parse_lines(lines)
    fecha_cierre, saldo_final_pdf = find_saldo_final_from_lines(lines)
    saldo_anterior = find_saldo_anterior_from_lines(lines)

    if df.empty:
        saldo_inicial = float(saldo_anterior) if not np.isnan(saldo_anterior) else 0.0
        total_debitos = total_creditos = 0.0
        saldo_final_visto = float(saldo_final_pdf) if not np.isnan(saldo_final_pdf) else saldo_inicial
        saldo_final_calculado = saldo_inicial
        diferencia = saldo_final_calculado - saldo_final_visto
        cuadra = abs(diferencia) < 0.01
        st.caption("Resumen del período")
        c1,c2,c3 = st.columns(3)
        with c1: metric_text("Saldo inicial", saldo_inicial)
        with c2: metric_text("Total créditos (+)", total_creditos)
        with c3: metric_text("Total débitos (–)", total_debitos)
        c4,c5,c6 = st.columns(3)
        with c4: metric_text("Saldo final (PDF)",  saldo_final_visto)
        with c5: metric_text("Saldo final calculado", saldo_final_calculado)
        with c6: metric_text("Diferencia", diferencia)
        st.success("Conciliado.") if cuadra else st.error("No cuadra la conciliación.")
        if pd.notna(fecha_cierre): st.caption(f"Cierre según PDF: {fecha_cierre.strftime('%d/%m/%Y')}")
        st.info("Sin Movimientos")
        return

    df = df.sort_values(["fecha", "orden"]).reset_index(drop=True)
    df["delta_saldo"] = df["saldo"].diff()
    df["debito"]  = np.where(df["delta_saldo"] < 0, -df["delta_saldo"], 0.0)
    df["credito"] = np.where(df["delta_saldo"] > 0,  df["delta_saldo"], 0.0)
    df["importe"] = df["debito"] - df["credito"]

    saldo_inicial = np.nan
    if not np.isnan(saldo_anterior):
        saldo_inicial = float(saldo_anterior)
    elif pd.notna(df.loc[0,"saldo"]) and pd.notna(df.loc[0,"delta_saldo"]):
        saldo_inicial = float(df.loc[0,"saldo"] - df.loc[0,"delta_saldo"])

    if not np.isnan(saldo_inicial):
        first_date = df["fecha"].dropna().min()
        fecha_apertura = (
            (first_date - pd.Timedelta(days=1)).normalize() +
            pd.Timedelta(hours=23, minutes=59, seconds=59)
        ) if pd.notna(first_date) else pd.NaT
        apertura = pd.DataFrame([{
            "fecha": fecha_apertura, "descripcion":"SALDO ANTERIOR","desc_norm":"SALDO ANTERIOR",
            "debito":0.0,"credito":0.0,"importe":0.0,"saldo":float(saldo_inicial),"pagina":0,"orden":-1
        }])
        df = pd.concat([apertura, df], ignore_index=True).sort_values(["fecha","orden"]).reset_index(drop=True)

    df["Clasificación"] = df.apply(lambda r: clasificar(str(r.get("descripcion","")), str(r.get("desc_norm","")), r.get("debito",0.0), r.get("credito",0.0)), axis=1)

    df_sorted = df.drop(columns=["orden"]).reset_index(drop=True)
    saldo_inicial_show = float(df_sorted.loc[0,"saldo"])
    total_debitos  = float(df_sorted["debito"].sum())
    total_creditos = float(df_sorted["credito"].sum())
    saldo_final_visto = float(df_sorted["saldo"].iloc[-1]) if np.isnan(saldo_final_pdf) else float(saldo_final_pdf)
    saldo_final_calculado = saldo_inicial_show + total_creditos - total_debitos
    diferencia = saldo_final_calculado - saldo_final_visto
    cuadra = abs(diferencia) < 0.01

    st.caption("Resumen del período")
    c1,c2,c3 = st.columns(3)
    with c1: metric_text("Saldo inicial", saldo_inicial_show)
    with c2: metric_text("Total créditos (+)", total_creditos)
    with c3: metric_text("Total débitos (–)", total_debitos)
    c4,c5,c6 = st.columns(3)
    with c4: metric_text("Saldo final (PDF)",  saldo_final_visto)
    with c5: metric_text("Saldo final calculado", saldo_final_calculado)
    with c6: metric_text("Diferencia", diferencia)
    st.success("Conciliado.") if cuadra else st.error("No cuadra la conciliación.")

    # Resumen Operativo
    iva21_mask  = df_sorted["Clasificación"].eq("IVA 21% (sobre comisiones)")
    iva105_mask = df_sorted["Clasificación"].eq("IVA 10,5% (sobre comisiones)")
    iva21  = float(df_sorted.loc[iva21_mask,  "debito"].sum())
    iva105 = float(df_sorted.loc[iva105_mask, "debito"].sum())
    net21  = round(iva21  / 0.21,  2) if iva21  else 0.0
    net105 = round(iva105 / 0.105, 2) if iva105 else 0.0
    percep_iva = float(df_sorted.loc[df_sorted["Clasificación"].eq("Percepciones de IVA"), "debito"].sum())
    ley_deb = float(df_sorted.loc[df_sorted["Clasificación"].eq("LEY 25.413"), "debito"].sum())
    ley_cre = float(df_sorted.loc[df_sorted["Clasificación"].eq("LEY 25.413"), "credito"].sum())
    ley_25413 = ley_deb - ley_cre
    sircreb = float(df_sorted.loc[df_sorted["Clasificación"].eq("SIRCREB"), "debito"].sum())

    m1,m2,m3 = st.columns(3)
    with m1: metric_text("Neto Comisiones 21%", net21)
    with m2: metric_text("IVA 21%", iva21)
    with m3: metric_text("Bruto 21%", net21 + iva21)

    n1,n2,n3 = st.columns(3)
    with n1: metric_text("Neto Comisiones 10,5%", net105)
    with n2: metric_text("IVA 10,5%", iva105)
    with n3: metric_text("Bruto 10,5%", net105 + iva105)

    o1,o2,o3 = st.columns(3)
    with o1: metric_text("Percepciones de IVA (RG 3337 / RG 2408)", percep_iva)
    with o2: metric_text("Ley 25.413 (neto)", ley_25413)
    with o3: metric_text("SIRCREB", sircreb)

    # Tabla SIN Styler
    st.caption("Detalle de movimientos")
    view = df_sorted.copy()
    for col in ["debito","credito","importe","saldo"]:
        view[col] = view[col].map(fmt_ar)
    st.dataframe(view, use_container_width=True)

# ---------- RENDER: Galicia (aislado) ----------
def render_account_report_galicia(account_title: str, account_number: str, acc_id: str, lines: list[str], header_saldos: dict | None = None):
    st.markdown("---")
    st.subheader(f"{account_title} · Nro {account_number}")

    df = parse_lines(lines)
    fecha_cierre, saldo_final_pdf = find_saldo_final_from_lines(lines)

    df = df.sort_values(["fecha","orden"]).reset_index(drop=True)
    df["debito"]  = np.where(df["monto_pdf"] < 0, -df["monto_pdf"], 0.0)
    df["credito"] = np.where(df["monto_pdf"] > 0,  df["monto_pdf"], 0.0)
    df["delta_saldo"] = df["saldo"].diff()
    mask_neutral = (df["debito"] == 0) & (df["credito"] == 0) & df["delta_saldo"].notna()
    df.loc[mask_neutral & (df["delta_saldo"] < 0), "debito"]  = -df.loc[mask_neutral & (df["delta_saldo"] < 0), "delta_saldo"]
    df.loc[mask_neutral & (df["delta_saldo"] > 0), "credito"] =  df.loc[mask_neutral & (df["delta_saldo"] > 0), "delta_saldo"]

    saldo_inicial = np.nan
    if header_saldos:
        if not np.isnan(header_saldos.get("saldo_inicial", np.nan)): saldo_inicial = float(header_saldos["saldo_inicial"])
        if not np.isnan(header_saldos.get("saldo_final", np.nan)):   saldo_final_pdf = float(header_saldos["saldo_final"])
    if np.isnan(saldo_inicial) and not df.empty:
        s0 = float(df.loc[0,"saldo"]); m0 = float(df.loc[0,"monto_pdf"])
        saldo_inicial = s0 - m0 if m0 > 0 else s0 + (-m0)

    if not np.isnan(saldo_inicial):
        first_date = df["fecha"].dropna().min()
        fecha_ap = (
            (first_date - pd.Timedelta(days=1)).normalize() +
            pd.Timedelta(hours=23, minutes=59, seconds=59)
        ) if pd.notna(first_date) else pd.NaT
        apertura = pd.DataFrame([{
            "fecha": fecha_ap, "descripcion":"SALDO ANTERIOR", "desc_norm":"SALDO ANTERIOR",
            "debito":0.0,"credito":0.0,"importe":0.0,"monto_pdf":0.0,"saldo":float(saldo_inicial),
            "pagina":0,"orden":-1
        }])
        df = pd.concat([apertura, df], ignore_index=True).sort_values(["fecha","orden"]).reset_index(drop=True)

    df["Clasificación"] = df.apply(lambda r: clasificar(str(r.get("descripcion","")), str(r.get("desc_norm","")), r.get("debito",0.0), r.get("credito",0.0)), axis=1)
    df_sorted = df.drop(columns=["orden"]).reset_index(drop=True)

    saldo_inicial_show = float(df_sorted.loc[0,"saldo"])
    total_debitos  = float(df_sorted["debito"].sum())
    total_creditos = float(df_sorted["credito"].sum())
    saldo_final_visto = float(df_sorted["saldo"].iloc[-1]) if np.isnan(saldo_final_pdf) else float(saldo_final_pdf)
    saldo_final_calculado = saldo_inicial_show + total_creditos - total_debitos
    diferencia = saldo_final_calculado - saldo_final_visto
    cuadra = abs(diferencia) < 0.01

    st.caption("Resumen del período")
    c1,c2,c3 = st.columns(3)
    with c1: metric_text("Saldo inicial", saldo_inicial_show)
    with c2: metric_text("Total créditos (+)", total_creditos)
    with c3: metric_text("Total débitos (–)", total_debitos)
    c4,c5,c6 = st.columns(3)
    with c4: metric_text("Saldo final (PDF)",  saldo_final_visto)
    with c5: metric_text("Saldo final calculado", saldo_final_calculado)
    with c6: metric_text("Diferencia", diferencia)
    st.success("Conciliado.") if cuadra else st.error("No cuadra la conciliación.")
    if pd.notna(fecha_cierre): st.caption(f"Cierre según PDF: {fecha_cierre.strftime('%d/%m/%Y')}")

    # Resumen Operativo (Galicia)
    udesc = df_sorted["desc_norm"].fillna("").str.upper()
    mask_percep_iva = udesc.str.contains(r"PERCEP\.?\s*IVA")
    percep_iva = float(df_sorted.loc[mask_percep_iva, "debito"].sum())
    mask_iva = udesc.str.contains(r"\bIVA\b") & (~mask_percep_iva)
    iva21 = float(df_sorted.loc[mask_iva, "debito"].sum())
    net21 = round(iva21/0.21,2) if iva21 else 0.0
    iva105 = 0.0; net105 = 0.0
    ley_deb = float(df_sorted.loc[df_sorted["Clasificación"].eq("LEY 25.413"), "debito"].sum())
    ley_cre = float(df_sorted.loc[df_sorted["Clasificación"].eq("LEY 25.413"), "credito"].sum())
    ley_25413 = ley_deb - ley_cre
    sircreb = float(df_sorted.loc[df_sorted["Clasificación"].eq("SIRCREB"), "debito"].sum())

    m1,m2,m3 = st.columns(3)
    with m1: metric_text("Neto Comisiones 21%", net21)
    with m2: metric_text("IVA 21%", iva21)
    with m3: metric_text("Bruto 21%", net21 + iva21)
    n1,n2,n3 = st.columns(3)
    with n1: metric_text("Neto Comisiones 10,5%", net105)
    with n2: metric_text("IVA 10,5%", iva105)
    with n3: metric_text("Bruto 10,5%", net105 + iva105)
    o1,o2,o3 = st.columns(3)
    with o1: metric_text("Percepciones de IVA (RG 3337 / RG 2408)", percep_iva)
    with o2: metric_text("Ley 25.413 (neto)", ley_25413)
    with o3: metric_text("SIRCREB", sircreb)

    # Tabla SIN Styler
    st.caption("Detalle de movimientos")
    view = df_sorted.copy()
    for col in ["debito","credito","importe","saldo"]:
        view[col] = view[col].map(fmt_ar)
    st.dataframe(view, use_container_width=True)

# ---------- Banco Santa Fe: extraer Nro de cuenta ----------
def santafe_extract_accounts(file_like):
    items = []
    for _, ln in extract_all_lines(file_like):
        m = SF_ACC_LINE_RE.search(ln)
        if m:
            title = " ".join(m.group(1).split())
            nro   = m.group(2).strip()
            items.append({"title": title.title(), "nro": nro})
    seen, uniq = set(), []
    for it in items:
        key = (it["title"], it["nro"])
        if key not in seen:
            seen.add(key); uniq.append(it)
    return uniq

# ---------- Banco Nación: meta + gastos finales ----------
def bna_extract_gastos_finales(txt: str) -> dict:
    out = {}
    for m in BNA_GASTOS_RE.finditer(txt or ""):
        etiqueta = m.group(1).upper()
        importe = normalize_money(m.group(2))
        if "I.V.A" in etiqueta or "IVA" in etiqueta: etiqueta = "I.V.A. BASE"
        out[etiqueta] = float(importe) if importe is not None else np.nan
    return out

def bna_extract_meta(file_like):
    txt = _text_from_pdf(file_like)
    acc = cbu = pstart = pend = None
    mper = BNA_PERIODO_RE.search(txt)
    if mper: pstart, pend = mper.group(1), mper.group(2)
    macc = BNA_CUENTA_CBU_RE.search(txt)
    if macc:
        acc, cbu = macc.group(1), macc.group(2)
    else:
        monly = BNA_ACC_ONLY_RE.search(txt)
        if monly: acc = monly.group(1)
    return {"account_number": acc, "cbu": cbu, "period_start": pstart, "period_end": pend}

# ---------- UI principal ----------
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
        options=("Auto (detectar)", "Banco de Santa Fe", "Banco Macro", "Banco de la Nación Argentina", "Banco Santander", "Banco Galicia"),
        index=0,
        help="Solo cambia la etiqueta informativa y el nombre de archivo."
    )

_bank_name = forced if forced != "Auto (detectar)" else _auto_bank_name

if _bank_name == "Banco Macro":
    st.info(f"Detectado: {_bank_name}")
elif _bank_name == "Banco de Santa Fe":
    st.success(f"Detectado: {_bank_name}")
elif _bank_name == "Banco de la Nación Argentina":
    st.success(f"Detectado: {_bank_name}")
elif _bank_name == "Banco Santander":
    st.success(f"Detectado: {_bank_name}")
elif _bank_name == "Banco Galicia":
    st.success(f"Detectado: {_bank_name}")
else:
    st.warning("No se pudo identificar el banco automáticamente. Se intentará procesar.")

_bank_slug = ("macro" if _bank_name == "Banco Macro"
              else "santafe" if _bank_name == "Banco de Santa Fe"
              else "nacion" if _bank_name == "Banco de la Nación Argentina"
              else "santander" if _bank_name == "Banco Santander"
              else "galicia" if _bank_name == "Banco Galicia"
              else "generico")

# --- Flujo por banco ---
if _bank_name == "Banco Macro":
    blocks = macro_split_account_blocks(io.BytesIO(data))
    if not blocks:
        st.warning("No se detectaron encabezados de cuenta en Macro. Se intentará procesar todo el PDF (podría mezclar cuentas).")
        _lines = [l for _, l in extract_all_lines(io.BytesIO(data))]
        render_account_report_generic(_bank_slug, "CUENTA (PDF completo)", "s/n", "macro-pdf-completo", _lines)
    else:
        st.caption(f"Información de su/s Cuenta/s: {len(blocks)} cuenta(s) detectada(s).")
        for b in blocks:
            render_account_report_generic(_bank_slug, b["titulo"], b["nro"], b["acc_id"], b["lines"])

elif _bank_name == "Banco de Santa Fe":
    sf_accounts = santafe_extract_accounts(io.BytesIO(data))
    all_lines = [l for _, l in extract_all_lines(io.BytesIO(data))]
    if sf_accounts:
        st.caption(f"Consolidado de cuentas: {len(sf_accounts)} detectada(s).")
        for i, acc in enumerate(sf_accounts, start=1):
            title = acc["title"]; nro = acc["nro"]
            acc_id = f"santafe-{re.sub(r'[^0-9A-Za-z]+', '_', nro)}"
            render_account_report_generic(_bank_slug, title, nro, acc_id, all_lines)
            if i < len(sf_accounts): st.markdown("")
    else:
        render_account_report_generic(_bank_slug, "CUENTA", "s/n", "generica-unica", all_lines)

elif _bank_name == "Banco de la Nación Argentina":
    meta = bna_extract_meta(io.BytesIO(data))
    all_lines = [l for _, l in extract_all_lines(io.BytesIO(data))]
    titulo = "CUENTA (BNA)"
    nro = meta.get("account_number") or "s/n"
    acc_id = f"bna-{re.sub(r'[^0-9A-Za-z]+', '_', nro)}"
    col1, col2, col3 = st.columns(3)
    if meta.get("period_start") and meta.get("period_end"):
        with col1: st.caption(f"Período: {meta['period_start']} al {meta['period_end']}")
    if meta.get("account_number"):
        with col2: st.caption(f"Nro. de cuenta: {meta['account_number']}")
    if meta.get("cbu"):
        with col3: st.caption(f"CBU: {meta['cbu']}")
    txt_full = _text_from_pdf(io.BytesIO(data))
    bna_extras = bna_extract_gastos_finales(txt_full)
    render_account_report_generic(_bank_slug, titulo, nro, acc_id, all_lines, bna_extras=bna_extras)

elif _bank_name == "Banco Santander":
    all_lines = [l for _, l in extract_all_lines(io.BytesIO(data))]
    render_account_report_generic(_bank_slug, "Cuenta Corriente (Santander)", "s/n", "santander-unica", all_lines)

elif _bank_name == "Banco Galicia":
    all_lines = [l for _, l in extract_all_lines(io.BytesIO(data))]
    if not any(GALICIA_HEADER_RE.search(l) for l in all_lines):
        st.info("No se encontró explícitamente el encabezado de la tabla de Galicia; se procesa igual por montos.")
    header_saldos = galicia_header_saldos_from_text(_bank_txt)
    render_account_report_galicia("Cuenta Corriente (Galicia)", "s/n", "galicia-unica", all_lines, header_saldos=header_saldos)

else:
    all_lines = [l for _, l in extract_all_lines(io.BytesIO(data))]
    render_account_report_generic("generico", "CUENTA", "s/n", "generica-unica", all_lines)
