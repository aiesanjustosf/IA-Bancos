# ia_resumen_bancario.py
# AIE San Justo – IA Resumen Bancario (unificado Galicia + resto)
import io, re
from pathlib import Path
import numpy as np
import pandas as pd
import streamlit as st

# -------- UI / assets --------
HERE = Path(__file__).parent
LOGO = HERE / "logo_aie.png"
FAVICON = HERE / "favicon-aie.ico"

st.set_page_config(page_title="IA Resumen Bancario", page_icon=str(FAVICON) if FAVICON.exists() else None)
if LOGO.exists():
    st.image(str(LOGO), width=200)
st.title("IA Resumen Bancario")

# -------- deps diferidas --------
try:
    import pdfplumber
except Exception as e:
    st.error(f"No se pudo importar pdfplumber: {e}\nRevisá requirements.txt")
    st.stop()

# -------- Regex base --------
DATE_RE  = re.compile(r"\b\d{1,2}/\d{2}/\d{2,4}\b")  # dd/mm/aa o dd/mm/aaaa
MONEY_RE = re.compile(r'(?<!\S)-?(?:\d{1,3}(?:\.\d{3})*|\d+)\s?,\s?\d{2}-?(?!\S)')
LONG_INT_RE = re.compile(r"\b\d{6,}\b")

def normalize_money(tok: str) -> float:
    if not tok:
        return np.nan
    tok = tok.strip()
    neg = tok.endswith("-") or tok.startswith("-")
    tok = tok.lstrip("-").rstrip("-")
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

# -------- util extracción PDF --------
def lines_from_text(page):
    txt = page.extract_text() or ""
    return [" ".join(l.split()) for l in txt.splitlines()]

def lines_from_words(page, ytol=2.0):
    words = page.extract_words(extra_attrs=["x0", "top"])
    if not words:
        return []
    words.sort(key=lambda w: (round(w["top"]/ytol), w["x0"]))
    lines, cur, band = [], [], None
    for w in words:
        b = round(w["top"]/ytol)
        if band is None or b == band:
            cur.append(w)
        else:
            lines.append(" ".join(x["text"] for x in cur))
            cur = [w]
        band = b
    if cur:
        lines.append(" ".join(l.split()) for l in cur)
        return [" ".join(l.split()) for l in lines]
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

def text_from_pdf(file_like) -> str:
    try:
        with pdfplumber.open(file_like) as pdf:
            return "\n".join((p.extract_text() or "") for p in pdf.pages)
    except Exception:
        return ""

# -------- Detección de banco --------
BANK_MACRO_HINTS   = ("BANCO MACRO","CUENTA CORRIENTE BANCARIA","SALDO ULTIMO EXTRACTO AL","DEBITO FISCAL IVA BASICO","N/D DBCR 25413")
BANK_SANTAFE_HINTS = ("BANCO DE SANTA FE","NUEVO BANCO DE SANTA FE","SALDO ANTERIOR","IMPTRANS","IVA GRAL")
BNA_NAME_HINT      = "BANCO DE LA NACION ARGENTINA"
BANK_NACION_HINTS  = (BNA_NAME_HINT, "SALDO ANTERIOR", "SALDO FINAL", "I.V.A. BASE", "COMIS.")
BANK_GALICIA_HINTS = ("BANCO GALICIA","RESUMEN DE CUENTA","SIRCREB","IMP. DEB./CRE. LEY 25413","TRANSFERENCIA DE TERCEROS")

def detect_bank_from_text(txt: str) -> str:
    U = (txt or "").upper()
    scores = {
        "Banco Macro": sum(1 for k in BANK_MACRO_HINTS if k in U),
        "Banco de Santa Fe": sum(1 for k in BANK_SANTAFE_HINTS if k in U),
        "Banco de la Nación Argentina": sum(1 for k in BANK_NACION_HINTS if k in U),
        "Banco Galicia": sum(1 for k in BANK_GALICIA_HINTS if k in U),
        "Banco Santander": 1 if "BANCO SANTANDER" in U or "SANTANDER RÍO" in U or "SANTANDER RIO" in U else 0,
    }
    best = max(scores.items(), key=lambda x: x[1])
    return best[0] if best[1] > 0 else "Banco no identificado"

# -------- Galicia: encabezado saldos --------
GAL_SALDO_INICIAL_RE = re.compile(r"SALDO\s+INICIAL.*?(-?(?:\d{1,3}(?:\.\d{3})*|\d+)\s?,\s?\d{2}-?)", re.I)
GAL_SALDO_FINAL_RE   = re.compile(r"SALDO\s+FINAL.*?(-?(?:\d{1,3}(?:\.\d{3})*|\d+)\s?,\s?\d{2}-?)", re.I)
GALICIA_HEADER_RE    = re.compile(r"\bFECHA\s+DESCRIPCI[ÓO]N\s+ORIGEN\s+CR[ÉE]DITO\s+D[ÉE]BITO\s+SALDO\b", re.I)

def galicia_header_saldos_from_text(txt: str) -> dict:
    ini = fin = np.nan
    m1 = GAL_SALDO_INICIAL_RE.search(txt or "")
    if m1:
        ini = normalize_money(m1.group(1))
    m2 = GAL_SALDO_FINAL_RE.search(txt or "")
    if m2:
        fin = normalize_money(m2.group(1))
    return {"saldo_inicial": ini, "saldo_final": fin}

# -------- Helpers de saldos (genérico) --------
SALDO_ANT_PREFIX   = re.compile(r"^SALDO\s+U?LTIMO\s+EXTRACTO\s+AL", re.IGNORECASE)
SALDO_FINAL_PREFIX = re.compile(r"^SALDO\s+FINAL\s+AL\s+D[ÍI]A",     re.IGNORECASE)
SF_SALDO_ULT_RE    = re.compile(r"SALDO\s+U?LTIMO\s+RESUMEN", re.IGNORECASE)

def only_one_amount(line: str) -> bool:
    return len(list(MONEY_RE.finditer(line))) == 1

def first_amount_value(line: str) -> float:
    m = MONEY_RE.search(line)
    return normalize_money(m.group(0)) if m else np.nan

def find_saldo_final_from_lines(lines):
    for ln in reversed(lines):
        if SALDO_FINAL_PREFIX.match(ln):
            d = DATE_RE.search(ln)
            if d and only_one_amount(ln):
                fecha = pd.to_datetime(d.group(0), dayfirst=True, errors="coerce")
                saldo = first_amount_value(ln)
                if pd.notna(fecha) and not np.isnan(saldo):
                    return fecha, saldo
    for ln in reversed(lines):
        if "SALDO FINAL" in ln.upper() and only_one_amount(ln):
            saldo = first_amount_value(ln)
            if not np.isnan(saldo):
                return pd.NaT, saldo
    return pd.NaT, np.nan

def find_saldo_anterior_from_lines(lines):
    for ln in lines:
        if SALDO_ANT_PREFIX.match(ln):
            d = DATE_RE.search(ln)
            if d and only_one_amount(ln):
                saldo = first_amount_value(ln)
                if not np.isnan(saldo):
                    return saldo
    for ln in lines:
        U = ln.upper()
        if "SALDO ANTERIOR" in U and only_one_amount(ln):
            saldo = first_amount_value(ln)
            if not np.isnan(saldo):
                return saldo
    for ln in lines:
        U = ln.upper()
        if "SALDO ULTIMO EXTRACTO" in U or "SALDO ÚLTIMO EXTRACTO" in U:
            d = DATE_RE.search(ln)
            if d and only_one_amount(ln):
                saldo = first_amount_value(ln)
                if not np.isnan(saldo):
                    return saldo
    for i, ln in enumerate(lines):
        if SF_SALDO_ULT_RE.search(ln):
            if only_one_amount(ln):
                v = first_amount_value(ln)
                if not np.isnan(v):
                    return v
            for j in (i+1, i+2):
                if 0 <= j < len(lines):
                    ln2 = lines[j]
                    if only_one_amount(ln2):
                        v2 = first_amount_value(ln2)
                        if not np.isnan(v2):
                            return v2
            break
    return np.nan

# -------- Normalizador de descripciones --------
def normalize_desc(desc: str) -> str:
    if not desc:
        return ""
    u = desc.upper()
    u = LONG_INT_RE.sub("", u)
    u = " ".join(u.split())
    return u

# -------- Parser base de filas (mismo para ambos) --------
HEADER_ROW_PAT = re.compile(r"^(FECHA\s+DESCRIPC(?:I[ÓO]N|ION)|FECHA\s+CONCEPTO|FECHA\s+DETALLE).*(SALDO|D[ÉE]BITO|CR[ÉE]DITO)", re.IGNORECASE)
NON_MOV_PAT    = re.compile(r"(INFORMACI[ÓO]N\s+DE\s+SU/S\s+CUENTA/S|TOTAL\s+RESUMEN\s+OPERATIVO|RESUMEN\s+DEL\s+PER[IÍ]ODO)", re.IGNORECASE)

def parse_lines(lines) -> pd.DataFrame:
    rows, seq = [], 0
    for ln in lines:
        if not ln.strip():
            continue
        if HEADER_ROW_PAT.search(ln) or NON_MOV_PAT.search(ln):
            continue
        am = list(MONEY_RE.finditer(ln))
        if len(am) < 2:
            continue
        d = DATE_RE.search(ln)
        if not d or d.end() >= am[0].start():
            continue
        saldo   = normalize_money(am[-1].group(0))   # última = saldo
        monto   = normalize_money(am[-2].group(0))   # penúltima = movimiento
        desc = ln[d.end(): am[0].start()].strip()
        seq += 1
        rows.append({
            "fecha": pd.to_datetime(d.group(0), dayfirst=True, errors="coerce"),
            "descripcion": desc,
            "desc_norm": normalize_desc(desc),
            "monto_pdf": monto,
            "saldo": saldo,
            "orden": seq
        })
    df = pd.DataFrame(rows)
    if df.empty:
        # asegurar columnas mínimas para evitar KeyError río abajo
        df = pd.DataFrame(columns=["fecha","descripcion","desc_norm","monto_pdf","saldo","orden"])
    return df

# -------- Clasificación (resumen operativo) --------
def clasificar(desc_norm: str, deb: float, cre: float) -> str:
    n = (desc_norm or "").upper()

    if "LEY 25413" in n or "IMPTRANS" in n or "IMP.S/CREDS" in n or "IMPDBCR 25413" in n or "N/D DBCR 25413" in n:
        return "LEY 25413"
    if "SIRCREB" in n or re.search(r"ING\.?\s*BRUTOS.*S/?\s*CRED", n):
        return "SIRCREB"

    if "I.V.A. BASE" in n or "IVA GRAL" in n or "DEBITO FISCAL IVA BASICO" in n:
        return "IVA 21% (sobre comisiones)"
    if "IVA 10,5" in n or "IVA REDUC" in n or "IVA RINS" in n:
        return "IVA 10,5% (sobre comisiones)"

    if ("PERCEP" in n or "RG3337" in n) or ("RETEN" in n and "IVA" in n and ("2408" in n or "RG 2408" in n)):
        return "Percepciones de IVA"

    if "COMIS" in n:
        return "Gastos por comisiones"

    if "DEB.AUT" in n or "DEB.AUTOM" in n or "SEGURO" in n or "DB-SNP" in n:
        return "Débito automático"

    if "TRANSFERENCIA DE TERCEROS" in n or "TRANSF RECIB" in n or "CR-TRSFE" in n:
        return "Transferencia de terceros recibida" if cre and cre > 0 else "Transferencia a terceros realizada" if deb and deb > 0 else "Transferencia"
    if "CTA PROPIA" in n or "ENTRE CTA" in n:
        return "Transferencia entre cuentas propias"

    if cre and cre > 0: return "Crédito"
    if deb and deb > 0: return "Débito"
    return "Otros"

# -------- Render único (decide rama Galicia vs genérico) --------
def render_account_report(
    banco_slug: str,
    account_title: str,
    account_number: str,
    acc_id: str,
    lines: list[str],
    header_saldos: dict | None = None
):
    st.markdown("---")
    st.subheader(f"{account_title} · Nro {account_number}")

    df = parse_lines(lines).copy()
    # asegurar columnas básicas
    for col in ["fecha","orden","monto_pdf","saldo","desc_norm","descripcion"]:
        if col not in df.columns:
            df[col] = np.nan
    # orden y fecha seguras
    if "orden" not in df.columns or df["orden"].isna().any():
        df["orden"] = np.arange(1, len(df)+1)
    df["fecha"] = pd.to_datetime(df["fecha"], errors="coerce")

    fecha_cierre, saldo_final_pdf = find_saldo_final_from_lines(lines)

    # --- Lógica separada de débitos/créditos
    df = df.sort_values(["fecha","orden"]).reset_index(drop=True)
    if banco_slug == "galicia":
        # Galicia: usa el signo del movimiento de la columna penúltima (monto_pdf)
        df["debito"]  = np.where(df["monto_pdf"] < 0, -df["monto_pdf"], 0.0)
        df["credito"] = np.where(df["monto_pdf"] > 0,  df["monto_pdf"], 0.0)
        df["delta_saldo"] = df["saldo"].diff()

        # Saldos desde encabezado si están; si no, reconstrucción con primera fila
        saldo_inicial = np.nan
        if header_saldos:
            ini = header_saldos.get("saldo_inicial", np.nan)
            fin = header_saldos.get("saldo_final", np.nan)
            if not np.isnan(ini): saldo_inicial = float(ini)
            if not np.isnan(fin): saldo_final_pdf = float(fin)

        if np.isnan(saldo_inicial) and not df.empty and pd.notna(df.loc[0,"saldo"]) and pd.notna(df.loc[0,"monto_pdf"]):
            s0 = float(df.loc[0,"saldo"]); m0 = float(df.loc[0,"monto_pdf"])
            saldo_inicial = s0 - m0 if m0 > 0 else s0 + (-m0)

    else:
        # Resto: deriva deb/cred por diferencia de saldo
        df["delta_saldo"] = df["saldo"].diff()
        df["debito"]  = np.where(df["delta_saldo"] < 0, -df["delta_saldo"], 0.0)
        df["credito"] = np.where(df["delta_saldo"] > 0,  df["delta_saldo"], 0.0)

        saldo_inicial = find_saldo_anterior_from_lines(lines)
        if np.isnan(saldo_inicial):
            if not df.empty and pd.notna(df.loc[0, "saldo"]) and pd.notna(df.loc[0, "delta_saldo"]):
                saldo_inicial = float(df.loc[0, "saldo"] - df.loc[0, "delta_saldo"])

    # Insertar SALDO ANTERIOR sintético para conciliación estable
    if np.isnan(saldo_inicial):
        saldo_inicial = 0.0 if df.empty else float(df.loc[0,"saldo"])

    first_date = df["fecha"].dropna().min() if not df.empty else pd.NaT
    if pd.notna(first_date):
        fecha_apertura = (first_date - pd.Timedelta(days=1)).normalize() + pd.Timedelta(hours=23, minutes=59, seconds=59)
    else:
        fecha_apertura = pd.NaT

    apertura = pd.DataFrame([{
        "fecha": fecha_apertura,
        "descripcion": "SALDO ANTERIOR",
        "desc_norm": "SALDO ANTERIOR",
        "debito": 0.0, "credito": 0.0,
        "monto_pdf": 0.0,
        "saldo": float(saldo_inicial),
        "orden": -1
    }])
    df = pd.concat([apertura, df], ignore_index=True).sort_values(["fecha","orden"]).reset_index(drop=True)

    # Clasificación para Resumen Operativo
    df["Clasificación"] = df.apply(lambda r: clasificar(str(r.get("desc_norm","")), r.get("debito",0.0), r.get("credito",0.0)), axis=1)

    # Totales / conciliación
    saldo_inicial_show   = float(df.loc[0,"saldo"]) if not df.empty else 0.0
    total_debitos        = float(df["debito"].sum()) if "debito" in df else 0.0
    total_creditos       = float(df["credito"].sum()) if "credito" in df else 0.0
    if np.isnan(saldo_final_pdf):
        saldo_final_visto = float(df["saldo"].iloc[-1]) if not df.empty else saldo_inicial_show
    else:
        saldo_final_visto = float(saldo_final_pdf)
    saldo_final_calculado= saldo_inicial_show + total_creditos - total_debitos
    diferencia           = saldo_final_calculado - saldo_final_visto
    cuadra               = abs(diferencia) < 0.01

    # Métricas
    st.caption("Resumen del período")
    c1, c2, c3 = st.columns(3)
    with c1: st.markdown(f"**Saldo inicial**<br>$ {fmt_ar(saldo_inicial_show)}", unsafe_allow_html=True)
    with c2: st.markdown(f"**Total créditos (+)**<br>$ {fmt_ar(total_creditos)}", unsafe_allow_html=True)
    with c3: st.markdown(f"**Total débitos (–)**<br>$ {fmt_ar(total_debitos)}", unsafe_allow_html=True)
    c4, c5, c6 = st.columns(3)
    with c4: st.markdown(f"**Saldo final (PDF)**<br>$ {fmt_ar(saldo_final_visto)}", unsafe_allow_html=True)
    with c5: st.markdown(f"**Saldo final calculado**<br>$ {fmt_ar(saldo_final_calculado)}", unsafe_allow_html=True)
    with c6: st.markdown(f"**Diferencia**<br>$ {fmt_ar(diferencia)}", unsafe_allow_html=True)

    st.success("Conciliado.") if cuadra else st.error("No cuadra la conciliación.")
    if pd.notna(fecha_cierre):
        st.caption(f"Cierre según PDF: {fecha_cierre.strftime('%d/%m/%Y')}")

    # Resumen Operativo (IVA)
    st.caption("Resumen Operativo: Registración Módulo IVA")
    iva21_mask  = df["Clasificación"].eq("IVA 21% (sobre comisiones)")
    iva105_mask = df["Clasificación"].eq("IVA 10,5% (sobre comisiones)")
    iva21  = float(df.loc[iva21_mask,  "debito"].sum())
    iva105 = float(df.loc[iva105_mask, "debito"].sum())
    net21  = round(iva21/0.21, 2) if iva21 else 0.0
    net105 = round(iva105/0.105,2) if iva105 else 0.0
    percep_iva = float(df.loc[df["Clasificación"].eq("Percepciones de IVA"), "debito"].sum())
    ley_deb = float(df.loc[df["Clasificación"].eq("LEY 25413"), "debito"].sum())
    ley_cre = float(df.loc[df["Clasificación"].eq("LEY 25413"), "credito"].sum())
    ley_25413 = ley_deb - ley_cre
    sircreb = float(df.loc[df["Clasificación"].eq("SIRCREB"), "debito"].sum())

    m1, m2, m3 = st.columns(3)
    with m1: st.markdown(f"**Neto Comisiones 21%**<br>$ {fmt_ar(net21)}", unsafe_allow_html=True)
    with m2: st.markdown(f"**IVA 21%**<br>$ {fmt_ar(iva21)}", unsafe_allow_html=True)
    with m3: st.markdown(f"**Bruto 21%**<br>$ {fmt_ar(net21+iva21)}", unsafe_allow_html=True)

    n1, n2, n3 = st.columns(3)
    with n1: st.markdown(f"**Neto Comisiones 10,5%**<br>$ {fmt_ar(net105)}", unsafe_allow_html=True)
    with n2: st.markdown(f"**IVA 10,5%**<br>$ {fmt_ar(iva105)}", unsafe_allow_html=True)
    with n3: st.markdown(f"**Bruto 10,5%**<br>$ {fmt_ar(net105+iva105)}", unsafe_allow_html=True)

    o1, o2, o3 = st.columns(3)
    with o1: st.markdown(f"**Percepciones de IVA (RG 3337 / RG 2408)**<br>$ {fmt_ar(percep_iva)}", unsafe_allow_html=True)
    with o2: st.markdown(f"**Ley 25.413 (neto)**<br>$ {fmt_ar(ley_25413)}", unsafe_allow_html=True)
    with o3: st.markdown(f"**SIRCREB**<br>$ {fmt_ar(sircreb)}", unsafe_allow_html=True)

    total_operativo = net21 + iva21 + net105 + iva105 + percep_iva + ley_25413 + sircreb
    st.markdown(f"**Total Resumen Operativo**<br>$ {fmt_ar(total_operativo)}", unsafe_allow_html=True)

    # Tabla (con fallback)
    st.caption("Detalle de movimientos")
    show_df = df[["fecha","descripcion","desc_norm","debito","credito","saldo","Clasificación"]].copy()
    try:
        st.dataframe(show_df, use_container_width=True)
    except Exception:
        st.write(show_df)

# ======================== UI principal ========================
uploaded = st.file_uploader("Subí un PDF del resumen bancario", type=["pdf"])
if uploaded is None:
    st.info("La app no almacena datos. Subí un PDF para procesar.")
    st.stop()

data = uploaded.read()
txt  = text_from_pdf(io.BytesIO(data))
auto_name = detect_bank_from_text(txt)

with st.expander("Opciones avanzadas (detección de banco)", expanded=False):
    forced = st.selectbox(
        "Forzar identificación del banco",
        options=("Auto (detectar)", "Banco de Santa Fe", "Banco Macro", "Banco de la Nación Argentina", "Banco Santander", "Banco Galicia"),
        index=0,
        help="Solo cambia la etiqueta informativa y el nombre de archivo."
    )

bank_name = forced if forced != "Auto (detectar)" else auto_name
slug = ("santafe" if bank_name == "Banco de Santa Fe"
        else "macro" if bank_name == "Banco Macro"
        else "nacion" if bank_name == "Banco de la Nación Argentina"
        else "santander" if bank_name == "Banco Santander"
        else "galicia" if bank_name == "Banco Galicia"
        else "generico")

# títulos seguros (sin split raro)
TITLE_MAP = {
    "Banco de Santa Fe": "Cuenta (Santa Fe)",
    "Banco Macro": "Cuenta (Macro)",
    "Banco de la Nación Argentina": "Cuenta (BNA)",
    "Banco Santander": "Cuenta (Santander)",
    "Banco Galicia": "Cuenta Corriente (Galicia)",
    "Banco no identificado": "Cuenta",
}

if bank_name == "Banco Galicia":
    st.success("Detectado: Banco Galicia")
    all_lines = [l for _, l in extract_all_lines(io.BytesIO(data))]
    if not any(GALICIA_HEADER_RE.search(l) for l in all_lines):
        st.info("No se encontró explícitamente el encabezado Galicia; se procesa igual por montos.")
    header_saldos = galicia_header_saldos_from_text(txt)
    render_account_report("galicia", TITLE_MAP.get(bank_name, "Cuenta"), "s/n", "galicia-unica", all_lines, header_saldos=header_saldos)
else:
    st.success(f"Detectado: {bank_name}")
    all_lines = [l for _, l in extract_all_lines(io.BytesIO(data))]
    render_account_report(slug, TITLE_MAP.get(bank_name, "Cuenta"), "s/n", f"{slug}-unica", all_lines)
