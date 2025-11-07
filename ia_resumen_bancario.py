# ia_resumen_bancario.py
# Herramienta para uso interno - AIE San Justo (unificado)

import io, re
from pathlib import Path
import numpy as np
import pandas as pd
import streamlit as st

# ---------------- UI / assets ----------------
HERE = Path(__file__).parent
LOGO = HERE / "logo_aie.png"
FAVICON = HERE / "favicon-aie.ico"
st.set_page_config(page_title="IA Resumen Bancario", page_icon=str(FAVICON) if FAVICON.exists() else None)
if LOGO.exists():
    st.image(str(LOGO), width=200)
st.title("IA Resumen Bancario")

# ---------------- deps diferidas ----------------
try:
    import pdfplumber
except Exception as e:
    st.error(f"No se pudo importar pdfplumber: {e}\nRevisá requirements.txt")
    st.stop()

# Reportlab opcional (para futuro PDF de Resumen Operativo)
try:
    from reportlab.lib.pagesizes import A4
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.lib import colors
    REPORTLAB_OK = True
except Exception:
    REPORTLAB_OK = False

# ---------------- regex base / utils ----------------
DATE_RE  = re.compile(r"\b\d{1,2}/\d{2}/\d{2,4}\b")
MONEY_RE = re.compile(r'(?<!\S)-?(?:\d{1,3}(?:\.\d{3})*|\d+)\s?,\s?\d{2}-?(?!\S)')
LONG_INT_RE = re.compile(r"\b\d{6,}\b")

def normalize_money(tok: str) -> float:
    if not tok: return np.nan
    tok = tok.strip()
    neg = tok.endswith("-") or tok.startswith("-")
    tok = tok.strip("-")
    if "," not in tok: return np.nan
    main, frac = tok.rsplit(",", 1)
    try:
        val = float(main.replace(".", "").replace(" ", "") + "." + frac)
        return -val if neg else val
    except Exception:
        return np.nan

def fmt_ar(n) -> str:
    if n is None or (isinstance(n, float) and (np.isnan(n) or np.isinf(n))):
        return "—"
    try:
        return f"{float(n):,.2f}".replace(",", "§").replace(".", ",").replace("§", ".")
    except Exception:
        return "—"

def lines_from_text(page):
    txt = page.extract_text() or ""
    return [" ".join(l.split()) for l in txt.splitlines()]

def lines_from_words(page, ytol=2.0):
    try:
        words = page.extract_words(extra_attrs=["x0","top"]) or []
    except Exception:
        return []
    if not words: return []
    words.sort(key=lambda w: (round(w["top"]/ytol), w["x0"]))
    lines, cur, band = [], [], None
    for w in words:
        b = round(w["top"]/ytol)
        if band is None or b==band: cur.append(w)
        else:
            lines.append(" ".join(x["text"] for x in cur))
            cur = [w]
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

# ---------------- Detección de banco ----------------
BANK_MACRO_HINTS   = ("BANCO MACRO","CUENTA CORRIENTE","INFORMACION DE SU/S CUENTA/S","CUENTA CORRIENTE ESPECIAL")
BANK_SANTAFE_HINTS = ("BANCO DE SANTA FE","NUEVO BANCO DE SANTA FE","SALDO ANTERIOR")
BANK_NACION_HINTS  = ("BANCO DE LA NACION ARGENTINA","SALDO ANTERIOR","SALDO FINAL","I.V.A. BASE","COMIS.")
BANK_GALICIA_HINTS = ("BANCO GALICIA","RESUMEN DE CUENTA","SIRCREB","IMP. DEB./CRE. LEY 25413","TRANSFERENCIA DE TERCEROS")
BANK_SANTANDER_HINTS = ("BANCO SANTANDER","SANTANDER ARGENTINA","IMPUESTO LEY 25413","RG 3337","RG 2408","IVA")

def _text_from_pdf(file_like) -> str:
    try:
        with pdfplumber.open(file_like) as pdf:
            return "\n".join((p.extract_text() or "") for p in pdf.pages)
    except Exception:
        return ""

def detect_bank_from_text(txt: str) -> str:
    U = (txt or "").upper()
    scores = {
        "Banco Macro": sum(1 for k in BANK_MACRO_HINTS if k in U),
        "Banco de Santa Fe": sum(1 for k in BANK_SANTAFE_HINTS if k in U),
        "Banco de la Nación Argentina": sum(1 for k in BANK_NACION_HINTS if k in U),
        "Banco Galicia": sum(1 for k in BANK_GALICIA_HINTS if k in U),
        "Banco Santander": sum(1 for k in BANK_SANTANDER_HINTS if k in U),
    }
    best = max(scores.items(), key=lambda x: x[1])
    return best[0] if best[1] > 0 else "Banco no identificado"

# ---------------- Parsers / Clasificación ----------------
GAL_SALDO_INICIAL_RE = re.compile(r"SALDO\s+INICIAL.*?(-?(?:\d{1,3}(?:\.\d{3})*|\d+)\s?,\s?\d{2}-?)", re.I)
GAL_SALDO_FINAL_RE   = re.compile(r"SALDO\s+FINAL.*?(-?(?:\d{1,3}(?:\.\d{3})*|\d+)\s?,\s?\d{2}-?)", re.I)

def galicia_header_saldos_from_text(txt: str) -> dict:
    ini = fin = np.nan
    m1 = GAL_SALDO_INICIAL_RE.search(txt or "")
    if m1: ini = normalize_money(m1.group(1))
    m2 = GAL_SALDO_FINAL_RE.search(txt or "")
    if m2: fin = normalize_money(m2.group(1))
    return {"saldo_inicial": ini, "saldo_final": fin}

HEADER_ROW_PAT = re.compile(r"^(FECHA\s+DESCRIPC(?:I[ÓO]N|ION)|FECHA\s+CONCEPTO|FECHA\s+DETALLE).*(SALDO|D[ÉE]BITO|CR[ÉE]DITO)", re.I)
NON_MOV_PAT    = re.compile(r"(INFORMACI[ÓO]N\s+DE\s+SU/S\s+CUENTA/S|TOTAL\s+RESUMEN\s+OPERATIVO|RESUMEN\s+DEL\s+PER[IÍ]ODO)", re.I)

def parse_lines(lines) -> pd.DataFrame:
    rows, seq = [], 0
    for ln in lines:
        if not ln.strip(): continue
        if HEADER_ROW_PAT.search(ln) or NON_MOV_PAT.search(ln): continue
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
            "desc_norm": " ".join(LONG_INT_RE.sub("", (desc or "").upper()).split()),
            "monto_pdf": monto,
            "saldo": saldo,
            "pagina": 0,
            "orden": seq
        })
    df = pd.DataFrame(rows)
    if df.empty:
        return pd.DataFrame(columns=["fecha","descripcion","desc_norm","monto_pdf","saldo","pagina","orden"])
    return df

def clasificar(desc: str, n: str, deb: float, cre: float) -> str:
    u = (desc or "").upper(); n = (n or "")
    if "SALDO ANTERIOR" in u or "SALDO ANTERIOR" in n: return "SALDO ANTERIOR"

    # Ley 25.413
    if any(k in u for k in ["LEY 25413","IMPTRANS","IMP.S/CREDS","IMPDBCR 25413","N/D DBCR 25413"]) or \
       any(k in n for k in ["LEY 25413","IMPTRANS","IMP.S/CREDS","IMPDBCR 25413","N/D DBCR 25413"]):
        return "LEY 25413"

    # SIRCREB
    if "SIRCREB" in u or "SIRCREB" in n or re.search(r"ING\.?\s*BRUTOS.*S/?\s*CRED", u) or re.search(r"ING\.?\s*BRUTOS.*S/?\s*CRED", n):
        return "SIRCREB"

    # Percepciones / Retenciones IVA
    if ("PERCEP" in u and "IVA" in u) or ("PERCEP" in n and "IVA" in n) or ("RG3337" in (u+n)) or ("RG 2408" in (u+n)) or ("RG2408" in (u+n)):
        return "Percepciones de IVA"

    # IVA sobre comisiones
    if any(k in u for k in ["I.V.A. BASE","IVA GRAL","IVA 21%","IVA S/COMISION"]) or any(k in n for k in ["I.V.A. BASE","IVA GRAL","IVA 21%","IVA S/COMISION"]):
        return "IVA 21% (sobre comisiones)"
    if any(k in u for k in ["IVA 10,5","IVA REDUC","IVA RINS"]) or any(k in n for k in ["IVA 10,5","IVA REDUC","IVA RINS"]):
        return "IVA 10,5% (sobre comisiones)"

    # Transferencias
    if "TRANSFERENCIA DE TERCEROS" in u or "TRANSFERENCIA DE TERCEROS" in n:
        return "Transferencia de terceros recibida" if cre>0 else "Transferencia a terceros realizada" if deb>0 else "Transferencia"
    if any(k in n for k in ["CR-TRSFE","TRANSF RECIB","TRANLINK"]) and cre>0:
        return "Transferencia de terceros recibida"
    if any(k in n for k in ["DB-TRSFE","TRSFE-ET","TRSFE-IT"]) and deb>0:
        return "Transferencia a terceros realizada"
    if any(k in n for k in ["DTNCTAPR","ENTRE CTA","CTA PROPIA"]): return "Transferencia entre cuentas propias"

    # Comisiones
    if any(k in u for k in ["COMIS.TRANSF","COMIS TRANSF","COMIS.COMPENSACION","COMIS COMPENSACION"]) or \
       any(k in n for k in ["COMOPREM","COMVCAUT","COMTRSIT","COM.NEGO","CO.EXCESO","COM."]):
        return "Gastos por comisiones"

    # Débitos automáticos / Seguros
    if any(k in n for k in ["DB-SNP","DEB.AUT","DEB.AUTOM","SEGUROS","GTOS SEG"]): return "Débito automático"

    # Plazo Fijo
    if any(k in u for k in ["PLAZO FIJO","P.FIJO","P FIJO"]) or any(k in n for k in ["PLAZO FIJO","PFIJO"]):
        if cre>0: return "Acreditación Plazo Fijo"
        if deb>0: return "Débito Plazo Fijo"
        return "Plazo Fijo"

    if cre>0: return "Crédito"
    if deb>0: return "Débito"
    return "Otros"

# ---------------- Núcleo de render por cuenta ----------------
def render_account_report(banco_slug, account_title, account_number, acc_id, lines, header_saldos=None):
    st.markdown("---")
    st.subheader(f"{account_title} · Nro {account_number}")

    df = parse_lines(lines).copy()
    if df.empty:
        st.warning("No se detectaron movimientos.")
        return

    # Ordenar sin romper si faltan columnas
    for col in ["fecha","orden"]:
        if col not in df.columns:
            df[col] = pd.NaT if col=="fecha" else 0
    df = df.sort_values(["fecha","orden"]).reset_index(drop=True)

    # Galicia: débito/crédito por signo del PDF
    if banco_slug == "galicia":
        df["debito"]  = np.where(df["monto_pdf"]<0, -df["monto_pdf"], 0.0)
        df["credito"] = np.where(df["monto_pdf"]>0,  df["monto_pdf"], 0.0)
        saldo_final_pdf = np.nan
        saldo_inicial = np.nan
        if header_saldos:
            si = header_saldos.get("saldo_inicial", np.nan)
            sf = header_saldos.get("saldo_final", np.nan)
            if not np.isnan(si): saldo_inicial = float(si)
            if not np.isnan(sf): saldo_final_pdf = float(sf)
        if np.isnan(saldo_inicial):
            # reconstruyo con la primera línea
            s0 = float(df.loc[0,"saldo"]); m0 = float(df.loc[0,"monto_pdf"])
            saldo_inicial = s0 - m0 if m0>0 else s0 + (-m0)
    else:
        # Resto: delta de saldo (primera línea usa monto_pdf)
        df["delta_saldo"] = df["saldo"].diff()
        df.loc[df.index[0], "delta_saldo"] = df.loc[df.index[0], "monto_pdf"]
        df["debito"]  = np.where(df["delta_saldo"]<0, -df["delta_saldo"], 0.0)
        df["credito"] = np.where(df["delta_saldo"]>0,  df["delta_saldo"], 0.0)
        saldo_inicial = float(df.loc[0,"saldo"] - df.loc[0,"delta_saldo"])
        saldo_final_pdf = float(df["saldo"].iloc[-1])

    # Inserto SALDO ANTERIOR explícito al inicio
    apertura = pd.DataFrame([{
        "fecha": (df["fecha"].dropna().min() - pd.Timedelta(days=1))
                 if pd.notna(df["fecha"].dropna().min()) else pd.NaT,
        "descripcion":"SALDO ANTERIOR",
        "desc_norm":"SALDO ANTERIOR",
        "debito":0.0,"credito":0.0,
        "monto_pdf":0.0,
        "saldo": float(saldo_inicial),
        "pagina":0,"orden":-1
    }])
    df = pd.concat([apertura, df], ignore_index=True)
    for c in ["debito","credito","monto_pdf","saldo"]:
        if c not in df.columns: df[c]=0.0

    # Clasificación
    df["Clasificación"] = df.apply(
        lambda r: clasificar(str(r.get("descripcion","")), str(r.get("desc_norm","")),
                             float(r.get("debito",0)), float(r.get("credito",0))),
        axis=1
    )

    # Totales & conciliación
    total_debitos  = float(df["debito"].sum())
    total_creditos = float(df["credito"].sum())
    saldo_final_calculado = float(saldo_inicial + total_creditos - total_debitos)
    saldo_final_visto = float(saldo_final_pdf) if not np.isnan(saldo_final_pdf) else float(df["saldo"].iloc[-1])
    diferencia = saldo_final_calculado - saldo_final_visto
    cuadra = abs(diferencia) < 0.01

    # Métricas
    c1,c2,c3 = st.columns(3)
    with c1: metric_text("Saldo inicial", saldo_inicial)
    with c2: metric_text("Total créditos (+)", total_creditos)
    with c3: metric_text("Total débitos (–)", total_debitos)

    c4,c5,c6 = st.columns(3)
    with c4: metric_text("Saldo final (PDF/tabla)", saldo_final_visto)
    with c5: metric_text("Saldo final calculado", saldo_final_calculado)
    with c6: metric_text("Diferencia", diferencia)
    st.success("Conciliado.") if cuadra else st.error("No cuadra la conciliación.")

    # ===== Resumen Operativo =====
    st.caption("Resumen Operativo: Registración Módulo IVA")

    iva21_mask  = df["Clasificación"].eq("IVA 21% (sobre comisiones)")
    iva105_mask = df["Clasificación"].eq("IVA 10,5% (sobre comisiones)")
    iva21  = float(df.loc[iva21_mask, "debito"].sum())
    iva105 = float(df.loc[iva105_mask, "debito"].sum())
    net21  = round(iva21/0.21, 2) if iva21 else 0.0
    net105 = round(iva105/0.105,2) if iva105 else 0.0

    percep_iva = float(df.loc[df["Clasificación"].eq("Percepciones de IVA"), "debito"].sum())

    ley_mask = df["Clasificación"].eq("LEY 25413")
    ley_25413 = float(df.loc[ley_mask, "debito"].sum() - df.loc[ley_mask, "credito"].sum())

    sircreb = float(df.loc[df["Clasificación"].eq("SIRCREB"), "debito"].sum())

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

    # Tabla
    show = df.copy()
    for col in ["debito","credito","monto_pdf","saldo"]:
        show[col] = show[col].astype(float)
    st.dataframe(
        show.drop(columns=[c for c in ["delta_saldo"] if c in show.columns]).style.format(
            {"debito":fmt_ar,"credito":fmt_ar,"monto_pdf":fmt_ar,"saldo":fmt_ar}, na_rep="—"
        ),
        use_container_width=True
    )

# ---------------- Dispatcher por banco ----------------
def run_report_for_pdf(_bank_name, data, _bank_txt):
    _slug = ("macro" if _bank_name=="Banco Macro"
             else "santafe" if _bank_name=="Banco de Santa Fe"
             else "nacion" if _bank_name=="Banco de la Nación Argentina"
             else "galicia" if _bank_name=="Banco Galicia"
             else "santander" if _bank_name=="Banco Santander"
             else "generico")

    all_lines = [l for _, l in extract_all_lines(io.BytesIO(data))]

    if _slug == "galicia":
        header_saldos = galicia_header_saldos_from_text(_bank_txt)
        render_account_report("galicia", "Cuenta Corriente (Galicia)", "s/n", "galicia-unica", all_lines, header_saldos=header_saldos)
    else:
        # Para Macro/Santa Fe/Nación/Santander procesamos el PDF completo
        titulo = {"macro":"CUENTA (Macro)", "santafe":"CUENTA (Santa Fe)", "nacion":"CUENTA (BNA)",
                  "santander":"CUENTA (Santander)"} .get(_slug, "CUENTA")
        render_account_report(_slug, titulo, "s/n", f"{_slug}-unica", all_lines)

# ---------------- UI principal ----------------
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
        options=("Auto (detectar)", "Banco de Santa Fe", "Banco Macro", "Banco de la Nación Argentina", "Banco Galicia", "Banco Santander"),
        index=0,
        help="Solo cambia la etiqueta informativa y el flujo interno."
    )

_bank_name = forced if forced != "Auto (detectar)" else _auto_bank_name

if _bank_name == "Banco Macro":
    st.info(f"Detectado: {_bank_name}")
elif _bank_name in ("Banco de Santa Fe","Banco de la Nación Argentina","Banco Galicia","Banco Santander"):
    st.success(f"Detectado: {_bank_name}")
else:
    st.warning("No se pudo identificar el banco automáticamente. Se intentará procesar igualmente.")

run_report_for_pdf(_bank_name, data, _bank_txt)
