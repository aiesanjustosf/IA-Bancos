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
DATE_RE  = re.compile(r"\b\d{1,2}/\d{2}/\d{2,4}\b")  # dd/mm/aa o dd/mm/aaaa
MONEY_RE = re.compile(r'(?<!\S)(?:\d{1,3}(?:\.\d{3})*|\d+)\s?,\s?\d{2}-?(?!\S)')
LONG_INT_RE = re.compile(r"\b\d{6,}\b")

# ====== PATRONES ESPECÍFICOS ======
# ---- Banco Macro ----
HYPH = r"[-\u2010\u2011\u2012\u2013\u2014\u2212]"  # guiones variantes
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

# ---- Banco de Santa Fe (Consolidado de cuentas) ----
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
# Captura número de cuenta luego de "NRO. CUENTA SUCURSAL" (variante sin CBU en la misma caja)
BNA_ACC_ONLY_RE = re.compile(
    r"NRO\.\s*CUENTA\s+SUCURSAL\s*[:\-]?\s*[\r\n ]+(\d{6,})",
    re.IGNORECASE
)
# Bloque de gastos finales post “SALDO FINAL”
BNA_GASTOS_RE = re.compile(
    r"-\s*(INTERESES|COMISION|SELLADOS|I\.V\.A\.?\s*BASE|SEGURO\s+DE\s+VIDA)\s*\$\s*([0-9\.\s]+,\d{2})",
    re.IGNORECASE
)

# ---- NUEVO: Santa Fe - "SALDO ULTIMO RESUMEN" sin fecha ----
SF_SALDO_ULT_RE = re.compile(r"SALDO\s+U?LTIMO\s+RESUMEN", re.IGNORECASE)

# ---- NUEVO: Banco Credicoop ----
CREDICOOP_HINTS = (
    "BANCO CREDICOOP",
    "BANCO CREDICOOP COOPERATIVO LIMITADO",
    "IMPUESTO LEY 25.413",
    "I.V.A.",
    "TRANSFERENCIAS",
    "CTA.",
)
SPACED_CAPS_RE = re.compile(r'((?:[A-ZÁÉÍÓÚÜÑ]\s)+[A-ZÁÉÍÓÚÜÑ])')
def _unspread_caps(s: str) -> str:
    return SPACED_CAPS_RE.sub(lambda m: m.group(0).replace(" ", ""), s)

def _money_to_float(s: str) -> float:
    if not s: return np.nan
    s = s.strip().replace("−","-")
    neg = s.endswith("-")
    s = s.rstrip("-")
    if "," not in s: return np.nan
    main, frac = s.rsplit(",",1)
    try:
        v = float(main.replace(".","").replace(" ","") + "." + frac)
        return -v if neg else v
    except Exception:
        return np.nan

HEAD_PAT = re.compile(r'(?:F\s*E\s*C\s*H\s*A|FECHA)', re.IGNORECASE)  # soporta letras espaciadas
HEAD_PAT_WIDE = re.compile(r'FECHA.*COMB?TE.*DESCRIP.*D[ÉE]BITO.*CR[ÉE]DITO.*SALDO', re.IGNORECASE)
DEB_PAT  = re.compile(r'D[ÉE]BITO', re.IGNORECASE)
CRE_PAT  = re.compile(r'CR[ÉE]DITO', re.IGNORECASE)
SAL_PAT  = re.compile(r'SALDO', re.IGNORECASE)
COMB_PAT = re.compile(r'COMB?TE', re.IGNORECASE)
DESC_PAT = re.compile(r'DESCRIP', re.IGNORECASE)

def credicoop_lines_words_xy(page, ytol=2.0):
    wrds = page.extract_words(extra_attrs=["x0","x1","top"])
    if not wrds: return []
    wrds.sort(key=lambda w: (round(w["top"]/ytol), w["x0"]))
    rows, cur, band = [], [], None
    for w in wrds:
        b = round(w["top"]/ytol)
        if band is None or b == band:
            cur.append(w)
        else:
            rows.append(cur); cur = [w]
        band = b
    if cur: rows.append(cur)
    return rows

def _credicoop_detect_columns(page) -> dict:
    """
    Devuelve bordes de columnas por x: {'fecha':(x0,x1), 'combte':..., 'desc':..., 'deb':..., 'cre':..., 'sal':...}
    Busca la fila cabecera con FECHA/COMBTE/DESCRIPCIÓN/DÉBITO/CRÉDITO/SALDO.
    """
    wrds = page.extract_words(extra_attrs=["x0","x1","top"])
    if not wrds:
        return {}
    bands = {}
    for w in wrds:
        b = round(w["top"]/2.0)
        bands.setdefault(b, []).append(w)
    header_band = None
    for b, arr in bands.items():
        txt = " ".join(_unspread_caps(w["text"]) for w in arr)
        if HEAD_PAT.search(txt) and DESC_PAT.search(txt) and (DEB_PAT.search(txt) or CRE_PAT.search(txt)):
            header_band = b; break
    if header_band is None:
        return {}
    arr = sorted(bands[header_band], key=lambda w: w["x0"])

    def find_x(regex):
        cands = [w for w in arr if regex.search(_unspread_caps(w["text"]))]
        return min((w["x0"] for w in cands), default=None)

    x_fecha = find_x(HEAD_PAT)
    x_comb  = find_x(COMB_PAT)
    x_desc  = find_x(DESC_PAT)
    x_deb   = find_x(DEB_PAT)
    x_cre   = find_x(CRE_PAT)
    x_sal   = find_x(SAL_PAT)

    xs = [x for x in (x_fecha,x_comb,x_desc,x_deb,x_cre,x_sal) if x is not None]
    if len(xs) < 4:
        return {}
    named = [(x_fecha,'fecha'),(x_comb,'combte'),(x_desc,'desc'),(x_deb,'deb'),(x_cre,'cre'),(x_sal,'sal')]
    named = sorted([t for t in named if t[0] is not None], key=lambda t: t[0])
    xs_sorted = [x for x,_ in named]
    mids = [(xs_sorted[i] + xs_sorted[i+1])/2 for i in range(len(xs_sorted)-1)]
    bounds = {}
    def span(ix_left, ix_right):
        left = -1e9 if ix_left < 0 else mids[ix_left]
        right = 1e9 if ix_right >= len(mids) else mids[ix_right]
        return (left, right)
    for i, (_, name) in enumerate(named):
        bounds[name] = span(i-1, i)
    return bounds

def _slice_row_by_bounds(words_row, bounds: dict) -> dict:
    cols = {k: [] for k in ("fecha","combte","desc","deb","cre","sal")}
    for w in words_row:
        xmid = (w["x0"] + w["x1"]) / 2.0
        for name, (x0,x1) in bounds.items():
            if x0 < xmid <= x1:
                cols.setdefault(name, []).append(_unspread_caps(w["text"]))
                break
    def j(name):
        return " ".join(" ".join(cols.get(name, [])).split())
    return {k: j(k) for k in cols.keys()}

def credicoop_extract_meta(file_like):
    txt = _text_from_pdf(file_like)
    title = None; cbu = None; acc = None
    m_cbu = re.search(r"CBU\s*(?:de\s*su\s*cuenta)?\s*:\s*([0-9 ]{10,})", txt or "", re.IGNORECASE)
    if m_cbu: cbu = m_cbu.group(1).replace(" ","")
    m_acc = re.search(r"\bCta\.\s*([0-9]{1,4}(?:\.[0-9]{1,4}){2,}[0-9])\b", txt or "", re.IGNORECASE)
    if m_acc: acc = m_acc.group(1)
    for line in (txt or "").splitlines():
        if "Cuenta" in line and "Cta." in line:
            title = " ".join(line.split()); break
    return {"title": title or "CUENTA (Credicoop)", "cbu": cbu, "account_number": acc}

def credicoop_parse_records_xy(file_like):
    """
    Devuelve (df, fecha_cierre, saldo_final_pdf, saldo_anterior_pdf)
    df: fecha, combte, descripcion, debito, credito, saldo, desc_norm
    """
    rows_out = []
    all_text = []
    saldo_anterior_pdf = np.nan
    fecha_cierre, saldo_final_pdf = pd.NaT, np.nan

    with pdfplumber.open(file_like) as pdf:
        # ---------- MODO 1: por palabras (xy) ----------
        for page in pdf.pages:
            bounds = _credicoop_detect_columns(page)
            band_rows = credicoop_lines_words_xy(page, ytol=2.0)
            buffer_idx = None

            for wr in band_rows:
                txtline = " ".join(_unspread_caps(w["text"]) for w in wr)
                all_text.append(txtline)

                if "SALDO ANTERIOR" in txtline.upper():
                    monies = MONEY_RE.findall(txtline)
                    if len(monies) == 1:
                        saldo_anterior_pdf = normalize_money(monies[0])
                    continue

                if HEAD_PAT_WIDE.search(txtline) or ("RESUMEN" in txtline.upper() and "CTA" in txtline.upper()):
                    continue

                if not bounds:
                    continue  # pasamos a fallback por tablas

                cols = _slice_row_by_bounds(wr, bounds)
                fecha_str = cols.get("fecha","").strip()
                desc_str  = cols.get("desc","").strip()
                combte    = cols.get("combte","").strip()
                deb_str   = cols.get("deb","").strip()
                cre_str   = cols.get("cre","").strip()
                sal_str   = cols.get("sal","").strip()

                if not DATE_RE.fullmatch(fecha_str or "") and desc_str:
                    if buffer_idx is not None:
                        rows_out[buffer_idx]["descripcion"] = (rows_out[buffer_idx]["descripcion"] + " " + desc_str).strip()
                    continue

                if not DATE_RE.fullmatch(fecha_str or ""):
                    continue

                deb = _money_to_float(deb_str)
                cre = _money_to_float(cre_str)
                sal = _money_to_float(sal_str)

                row = {
                    "fecha": pd.to_datetime(fecha_str, dayfirst=True, errors="coerce"),
                    "combte": combte or None,
                    "descripcion": desc_str,
                    "debito": float(deb) if pd.notna(deb) else 0.0,
                    "credito": float(cre) if pd.notna(cre) else 0.0,
                    "saldo": sal if pd.notna(sal) else np.nan,
                }
                rows_out.append(row)
                buffer_idx = len(rows_out) - 1

            # “SALDO FINAL …” explícito (por si existe en el pie)
            tail = "\n".join(all_text[-120:])
            for ln in reversed(tail.splitlines()):
                if SALDO_FINAL_PREFIX.match(ln) and _only_one_amount(ln):
                    d = DATE_RE.search(ln)
                    if d:
                        fecha_cierre = pd.to_datetime(d.group(0), dayfirst=True, errors="coerce")
                        saldo_final_pdf = _first_amount_value(ln)
                        break

        # ---------- MODO 2 (fallback): por tablas dibujadas ----------
        if not rows_out:
            for page in pdf.pages:
                tables = page.extract_tables({
                    "vertical_strategy": "lines",
                    "horizontal_strategy": "lines",
                    "intersection_tolerance": 5,
                    "explicit_vertical_lines": [],
                    "explicit_horizontal_lines": [],
                    "snap_tolerance": 3,
                }) or []
                for tb in tables:
                    header = " ".join((_unspread_caps(" ".join(r or [])) for r in tb[:2]))
                    if not (HEAD_PAT.search(header) and "DÉBITO" in header.upper() and "CRÉDITO" in header.upper()):
                        continue
                    for r in tb[1:]:
                        if not r or len(r) < 6:
                            continue
                        fecha_str, combte, desc_str, deb_str, cre_str, sal_str = (r + [""]*6)[:6]
                        fecha_str = (fecha_str or "").strip()
                        if not DATE_RE.fullmatch(fecha_str or ""):
                            if rows_out:
                                rows_out[-1]["descripcion"] = (rows_out[-1]["descripcion"] + " " + (desc_str or "")).strip()
                            continue
                        deb = _money_to_float(deb_str or "")
                        cre = _money_to_float(cre_str or "")
                        sal = _money_to_float(sal_str or "")
                        rows_out.append({
                            "fecha": pd.to_datetime(fecha_str, dayfirst=True, errors="coerce"),
                            "combte": (combte or "").strip() or None,
                            "descripcion": (desc_str or "").strip(),
                            "debito": float(deb) if pd.notna(deb) else 0.0,
                            "credito": float(cre) if pd.notna(cre) else 0.0,
                            "saldo": sal if pd.notna(sal) else np.nan,
                        })

    df = pd.DataFrame(rows_out)
    if df.empty:
        for ln in reversed(all_text):
            if "SALDO FINAL" in ln.upper() and _only_one_amount(ln):
                saldo_final_pdf = _first_amount_value(ln)
                break
        return df, fecha_cierre, saldo_final_pdf, saldo_anterior_pdf

    df["desc_norm"] = df["descripcion"].map(normalize_desc)
    df = df.sort_values(["fecha"]).reset_index(drop=True)

    # ------ Generación/ajuste de saldo ------
    if not np.isnan(saldo_anterior_pdf):
        saldo_inicial = float(saldo_anterior_pdf)
    else:
        first = df.iloc[0]
        if pd.notna(first.get("saldo", np.nan)):
            saldo_inicial = float(first["saldo"]) - float(first.get("credito",0.0)) + float(first.get("debito",0.0))
        else:
            saldo_inicial = 0.0

    running = saldo_inicial
    sal_out = []
    for _, r in df.iterrows():
        if pd.isna(r["saldo"]):
            running = running + float(r["credito"]) - float(r["debito"])  # CRÉDITO suma, DÉBITO resta
            sal_out.append(running)
        else:
            running = float(r["saldo"])
            sal_out.append(running)
    df["saldo"] = sal_out

    if np.isnan(saldo_final_pdf):
        saldo_final_pdf = float(df["saldo"].iloc[-1])

    return df, fecha_cierre, saldo_final_pdf, saldo_anterior_pdf

# --- utils (compartidas) ---
def normalize_money(tok: str) -> float:
    if not tok:
        return np.nan
    tok = tok.strip()
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
        lines.append(" ".join(x["text"] for x in cur))
    return [" ".join(l.split()) for l in lines]

def normalize_desc(desc: str) -> str:
    if not desc:
        return ""
    u = desc.upper()
    for pref in ("SAN JUS ", "CASA RO ", "CENTRAL ", "GOBERNA ", "GOBERNADOR ", "SANTA FE ", "ROSARIO "):
        if u.startswith(pref):
            u = u[len(pref):]
            break
    u = LONG_INT_RE.sub("", u)
    u = " ".join(u.split())
    return u

# ---------- Detección de banco (solo banner) ----------
BANK_MACRO_HINTS = ("BANCO MACRO","CUENTA CORRIENTE BANCARIA","SALDO ULTIMO EXTRACTO AL","DEBITO FISCAL IVA BASICO","N/D DBCR 25413")
BANK_SANTAFE_HINTS = ("BANCO DE SANTA FE","NUEVO BANCO DE SANTA FE","SALDO ANTERIOR","IMPTRANS","IVA GRAL")
BANK_NACION_HINTS = (BNA_NAME_HINT, "SALDO ANTERIOR", "SALDO FINAL", "I.V.A. BASE", "COMIS.")
BANK_CREDICOOP_HINTS = CREDICOOP_HINTS

def _text_from_pdf(file_like) -> str:
    try:
        with pdfplumber.open(file_like) as pdf:
            return "\n".join((p.extract_text() or "") for p in pdf.pages)
    except Exception:
        return ""

def detect_bank_from_text(txt: str) -> str:
    U = (txt or "").upper()
    score_macro = sum(1 for k in BANK_MACRO_HINTS if k in U)
    score_sf    = sum(1 for k in BANK_SANTAFE_HINTS if k in U)
    score_bna   = sum(1 for k in BANK_NACION_HINTS if k in U)
    score_cred  = sum(1 for k in BANK_CREDICOOP_HINTS if k in U)
    scores = [("Banco Macro", score_macro), ("Banco de Santa Fe", score_sf),
              ("Banco de la Nación Argentina", score_bna), ("Banco Credicoop", score_cred)]
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

# ---------- “Información de su/s Cuenta/s” (whitelist Macro) ----------
def _normalize_account_token(tok: str) -> str:
    return re.sub(rf"\s*{HYPH}\s*", "-", tok)

def macro_extract_account_whitelist(file_like) -> dict:
    info = {}
    all_lines = extract_all_lines(file_like)
    in_table = False
    last_tipo = None
    for _, ln in all_lines:
        if INFO_HEADER.search(ln):
            in_table = True
            continue
        if in_table:
            m_token = ACCOUNT_TOKEN_RE.search(ln)
            if m_token:
                nro = _normalize_account_token(m_token.group(0))
                u = ln.upper()
                if "CORRIENTE" in u and "ESPECIAL" in u and ("DOLAR" in u or "DÓLAR" in u or "DOLARES" in u or "DÓLARES" in u):
                    tipo = "CUENTA CORRIENTE ESPECIAL EN DOLARES"
                elif "CORRIENTE" in u and "ESPECIAL" in u:
                    tipo = "CUENTA CORRIENTE ESPECIAL EN PESOS"
                elif "CUENTA CORRIENTE BANCARIA" in u:
                    tipo = "CUENTA CORRIENTE BANCARIA"
                else:
                    tipo = last_tipo or "CUENTA"
                info[nro] = {"titulo": tipo}
                last_tipo = tipo
            else:
                if ln.strip().startswith("CUENTA ") and "NRO" in ln.upper():
                    break
    return info

def _normalize_title_from_pending(pending_title: str) -> str:
    t = pending_title.upper()
    if "CORRIENTE" in t and "ESPECIAL" in t and ("DOLAR" in t or "DÓLAR" in t): return "CUENTA CORRIENTE ESPECIAL EN DOLARES"
    if "CORRIENTE" in t and "ESPECIAL" in t:                                   return "CUENTA CORRIENTE ESPECIAL EN PESOS"
    if "CORRIENTE" in t:                                                       return "CUENTA CORRIENTE BANCARIA"
    if "CAJA DE AHORRO" in t:                                                  return "CAJA DE AHORRO"
    return "CUENTA"

# ---------- Macro: segmentación por cuentas (ID = número completo) ----------
def macro_split_account_blocks(file_like):
    whitelist = macro_extract_account_whitelist(file_like)
    white_set = set(whitelist.keys())

    all_lines = extract_all_lines(file_like)
    accounts, order = {}, []
    current_nro = None
    pending_title = None
    expect_token_in = 0

    def open_block(nro: str, pi: int, titulo_hint: str | None):
        nonlocal accounts, order, current_nro
        titulo = (whitelist.get(nro, {}) or {}).get("titulo") or (titulo_hint and _normalize_title_from_pending(titulo_hint)) or "CUENTA"
        if nro not in accounts:
            accounts[nro] = {"titulo": titulo, "nro": nro, "lines": [], "pages": [pi, pi], "acc_id": nro}
            order.append(nro)
        else:
            accounts[nro]["pages"][1] = max(accounts[nro]["pages"][1], pi)
            if accounts[nro]["titulo"] == "CUENTA" and titulo != "CUENTA":
                accounts[nro]["titulo"] = titulo
        current_nro = nro

    for (pi, ln) in all_lines:
        m_title = RE_MACRO_ACC_START.match(ln)
        if m_title:
            pending_title = "CUENTA " + m_title.group(1).strip()
            expect_token_in = 12
            m_same_line = RE_MACRO_ACC_NRO.search(ln) or ACCOUNT_TOKEN_RE.search(ln)
            if m_same_line:
                nro = _normalize_account_token(m_same_line.group(1) if m_same_line.re is RE_MACRO_ACC_NRO else m_same_line.group(0))
                if (not white_set) or (nro in white_set):
                    open_block(nro, pi, pending_title)
                    pending_title = None
                    expect_token_in = 0
            continue

        if pending_title and expect_token_in > 0:
            expect_token_in -= 1
            m_nro = RE_MACRO_ACC_NRO.search(ln)
            if m_nro:
                nro = _normalize_account_token(m_nro.group(1))
                if (not white_set) or (nro in white_set):
                    open_block(nro, pi, pending_title)
                pending_title = None
                expect_token_in = 0
                continue
            m_tok = ACCOUNT_TOKEN_RE.search(ln)
            if m_tok:
                nro = _normalize_account_token(m_tok.group(0))
                if (not white_set) or (nro in white_set):
                    open_block(nro, pi, pending_title)
                pending_title = None
                expect_token_in = 0
                continue
            if RE_HAS_NRO.search(ln):
                expect_token_in = max(expect_token_in, 12)
                continue

        if (not pending_title) and white_set:
            m_fallback = ACCOUNT_TOKEN_RE.search(ln)
            if m_fallback:
                nro = _normalize_account_token(m_fallback.group(0))
                if nro in white_set and current_nro != nro:
                    open_block(nro, pi, None)

        if current_nro is not None:
            acc = accounts[current_nro]
            acc["lines"].append(ln)
            acc["pages"][1] = max(acc["pages"][1], pi)

    blocks = []
    for nro in order:
        acc = accounts[nro]
        acc["pages"] = tuple(acc["pages"])
        blocks.append(acc)
    return blocks

# ---------- Parsing movimientos (genérico Macro/SF/BNA) ----------
def parse_lines(lines) -> pd.DataFrame:
    rows = []
    seq = 0  # preserva orden exacto de aparición
    for ln in lines:
        if not ln.strip():
            continue
        if PER_PAGE_TITLE_PAT.search(ln) or HEADER_ROW_PAT.search(ln) or NON_MOV_PAT.search(ln):
            continue
        am = list(MONEY_RE.finditer(ln))
        if len(am) < 2:
            continue
        d = DATE_RE.search(ln)
        if not d or d.end() >= am[0].start():
            continue
        saldo   = normalize_money(am[-1].group(0))
        importe = normalize_money(am[-2].group(0))
        first_money = am[0]
        desc = ln[d.end(): first_money.start()].strip()
        seq += 1
        rows.append({
            "fecha": pd.to_datetime(d.group(0), dayfirst=True, errors="coerce"),
            "descripcion": desc,
            "desc_norm": normalize_desc(desc),
            "debito": 0.0,
            "credito": 0.0,
            "importe": importe,      # informativo; conciliamos por Δ saldo
            "saldo": saldo,
            "pagina": 0,
            "orden": seq
        })
    return pd.DataFrame(rows)

# ---------- Saldos ----------
def _only_one_amount(line: str) -> bool:
    return len(list(MONEY_RE.finditer(line))) == 1

def _first_amount_value(line: str) -> float:
    m = MONEY_RE.search(line)
    return normalize_money(m.group(0)) if m else np.nan

def find_saldo_final_from_lines(lines):
    # 1) Macro/otros con formato expreso
    for ln in reversed(lines):
        if SALDO_FINAL_PREFIX.match(ln):
            d = DATE_RE.search(ln)
            if d and _only_one_amount(ln):
                fecha = pd.to_datetime(d.group(0), dayfirst=True, errors="coerce")
                saldo = _first_amount_value(ln)
                if pd.notna(fecha) and not np.isnan(saldo):
                    return fecha, saldo
    # 2) BNA: "SALDO FINAL" sin fecha
    for ln in reversed(lines):
        if "SALDO FINAL" in ln.upper() and _only_one_amount(ln):
            saldo = _first_amount_value(ln)
            if not np.isnan(saldo):
                return pd.NaT, saldo
    return pd.NaT, np.nan

def find_saldo_anterior_from_lines(lines):
    # 1) Macro (expreso con fecha)
    for ln in lines:
        if SALDO_ANT_PREFIX.match(ln):
            d = DATE_RE.search(ln)
            if d and _only_one_amount(ln):
                saldo = _first_amount_value(ln)
                if not np.isnan(saldo):
                    return saldo
    # 2) Genérico: "SALDO ANTERIOR"
    for ln in lines:
        U = ln.upper()
        if "SALDO ANTERIOR" in U and _only_one_amount(ln):
            saldo = _first_amount_value(ln)
            if not np.isnan(saldo):
                return saldo
    # 3) Macro variantes
    for ln in lines:
        U = ln.upper()
        if "SALDO ULTIMO EXTRACTO" in U or "SALDO ÚLTIMO EXTRACTO" in U:
            d = DATE_RE.search(ln)
            if d and _only_one_amount(ln):
                saldo = _first_amount_value(ln)
                if not np.isnan(saldo):
                    return saldo
    # 4) Santa Fe — "SALDO ULTIMO RESUMEN"
    for i, ln in enumerate(lines):
        if SF_SALDO_ULT_RE.search(ln):
            if _only_one_amount(ln):
                v = _first_amount_value(ln)
                if not np.isnan(v):
                    return v
            for j in (i+1, i+2):
                if 0 <= j < len(lines):
                    ln2 = lines[j]
                    if _only_one_amount(ln2):
                        v2 = _first_amount_value(ln2)
                        if not np.isnan(v2):
                            return v2
            break
    return np.nan

# ---------- Clasificación ----------
RE_PERCEP_RG2408 = re.compile(
    r"(PERCEPCI[ÓO]N\s+IVA\s+RG\.?\s*2408|PERCEPCION\s+IVA\s+RG\s*2408\s*/?\s*COMIS\-?GASTOS)",
    re.IGNORECASE
)

def clasificar(desc: str, desc_norm: str, deb: float, cre: float) -> str:
    u = (desc or "").upper()
    n = (desc_norm or "").upper()

    # Saldos
    if "SALDO ANTERIOR" in u or "SALDO ANTERIOR" in n:
        return "SALDO ANTERIOR"

    # Impuesto a los débitos y créditos bancarios
    if ("LEY 25413" in u) or ("IMPTRANS" in u) or ("IMP.S/CREDS" in u) or ("IMPDBCR 25413" in u) or ("N/D DBCR 25413" in u) or \
       ("LEY 25413" in n) or ("IMPTRANS" in n) or ("IMP.S/CREDS" in n) or ("IMPDBCR 25413" in n) or ("N/D DBCR 25413" in n):
        return "LEY 25.413"

    # SIRCREB
    if ("SIRCREB" in u) or ("SIRCREB" in n):
        return "SIRCREB"

    # Percepciones IVA (atajo explícito RG 2408)
    if RE_PERCEP_RG2408.search(u) or RE_PERCEP_RG2408.search(n):
        return "Percepciones de IVA"

    # Percepciones / Retenciones IVA (RG 3337 / RG 2408)
    if (
        ("IVA PERC" in u) or ("IVA PERCEP" in u) or ("RG3337" in u) or
        ("IVA PERC" in n) or ("IVA PERCEP" in n) or ("RG3337" in n) or
        (("RETEN" in u or "RETENC" in u) and (("I.V.A" in u) or ("IVA" in u)) and (("RG.2408" in u) or ("RG 2408" in u) or ("RG2408" in u))) or
        (("RETEN" in n or "RETENC" in n) and (("I.V.A" in n) or ("IVA" in n)) and (("RG.2408" in n) or ("RG 2408" in n) or ("RG2408" in n)))
    ):
        return "Percepciones de IVA"

    # IVA sobre comisiones (BNA usa "I.V.A. BASE", Credicoop usa “I.V.A. - Débito Fiscal 21%”)
    if ("I.V.A. BASE" in u) or ("I.V.A. BASE" in n) or ("IVA GRAL" in u) or ("IVA GRAL" in n) or ("DEBITO FISCAL IVA BASICO" in u) or ("DEBITO FISCAL IVA BASICO" in n) \
       or ("I.V.A" in u and "DÉBITO FISCAL" in u) or ("I.V.A" in n and "DEBITO FISCAL" in n):
        if "10,5" in u or "10,5" in n or "10.5" in u or "10.5" in n:
            return "IVA 10,5% (sobre comisiones)"
        return "IVA 21% (sobre comisiones)"

    # Plazo Fijo (según signo)
    if ("PLAZO FIJO" in u) or ("PLAZO FIJO" in n) or ("P.FIJO" in u) or ("P.FIJO" in n) or ("P FIJO" in u) or ("P FIJO" in n) or ("PFIJO" in u) or ("PFIJO" in n):
        if cre and cre != 0:
            return "Acreditación Plazo Fijo"
        if deb and deb != 0:
            return "Débito Plazo Fijo"
        return "Plazo Fijo"

    # Comisiones solicitadas explícitamente
    if ("COMIS.TRANSF" in u) or ("COMIS.TRANSF" in n) or ("COMIS TRANSF" in u) or ("COMIS TRANSF" in n) or \
       ("COMIS.COMPENSACION" in u) or ("COMIS.COMPENSACION" in n) or ("COMIS COMPENSACION" in u) or ("COMIS COMPENSACION" in n):
        return "Gastos por comisiones"

    # Otras comisiones / gastos (incluye Credicoop)
    if ("MANTENIMIENTO MENSUAL PAQUETE" in u) or ("MANTENIMIENTO MENSUAL PAQUETE" in n) or \
       ("COMOPREM" in n) or ("COMVCAUT" in n) or ("COMTRSIT" in n) or ("COM.NEGO" in n) or ("CO.EXCESO" in n) or ("COM." in n) or \
       ("GASTOS" in u) or ("GASTOS" in n) or ("SERVICIO" in u) or ("SERVICIO" in n) or ("COMISION" in u) or ("COMISION" in n):
        return "Gastos por comisiones"

    # Débitos automáticos / Seguros
    if ("DB-SNP" in n) or ("DEB.AUT" in n) or ("DEB.AUTOM" in n) or ("SEGUROS" in n) or ("GTOS SEG" in n) or ("DEBITO INMEDIATO" in u) or ("DEBIN" in u):
        return "Débito automático"

    # Varias
    if "DYC" in n: return "DyC"
    if ("AFIP" in n or "ARCA" in n) and deb and deb != 0: return "Débitos ARCA"
    if "API" in n: return "API"

    if "DEB.CUOTA PRESTAMO" in n or ("PRESTAMO" in n and "DEB." in n): return "Cuota de préstamo"
    if ("CR.PREST" in n) or ("CREDITO PRESTAMOS" in n) or ("CRÉDITO PRÉSTAMOS" in n): return "Acreditación Préstamos"

    if "CH 48 HS" in n or "CH.48 HS" in n: return "Cheques 48 hs"

    if ("PAGO COMERC" in n) or ("CR-CABAL" in n) or ("CR CABAL" in n) or ("CR TARJ" in n): return "Acreditaciones Tarjetas de Crédito/Débito"

    if ("CR-DEPEF" in n) or ("CR DEPEF" in n) or ("DEPOSITO EFECTIVO" in n) or ("DEP.EFECTIVO" in n) or ("DEP EFECTIVO" in n):
        return "Depósito en Efectivo"

    if (("CR-TRSFE" in n) or ("TRANSF RECIB" in n) or ("TRANLINK" in n) or ("TRANSFERENCIAS RECIBIDAS" in u)) and cre and cre != 0:
        return "Transferencia de terceros recibida"
    if (("DB-TRSFE" in n) or ("TRSFE-ET" in n) or ("TRSFE-IT" in n)) and deb and deb != 0:
        return "Transferencia a terceros realizada"
    if ("DTNCTAPR" in n) or ("ENTRE CTA" in n) or ("CTA PROPIA" in n):
        return "Transferencia entre cuentas propias"

    if ("NEG.CONT" in n) or ("NEGOCIADOS" in n):
        return "Acreditación de valores"

    if cre and cre != 0: return "Crédito"
    if deb and deb != 0: return "Débito"
    return "Otros"

# ---------- Helper de UI por cuenta (genérico) ----------
def render_account_report(
    banco_slug: str,
    account_title: str,
    account_number: str,
    acc_id: str,
    lines: list[str],
    bna_extras: dict | None = None   # extras BNA integrados al resumen operativo
):
    st.markdown("---")
    st.subheader(f"{account_title} · Nro {account_number}")

    df = parse_lines(lines)
    fecha_cierre, saldo_final_pdf = find_saldo_final_from_lines(lines)
    saldo_anterior = find_saldo_anterior_from_lines(lines)

    # Sin movimientos: mostrar saldos y conciliación
    if df.empty:
        total_debitos = 0.0
        total_creditos = 0.0
        saldo_inicial = float(saldo_anterior) if not np.isnan(saldo_anterior) else 0.0
        saldo_final_visto = float(saldo_final_pdf) if not np.isnan(saldo_final_pdf) else saldo_inicial
        saldo_final_calculado = saldo_inicial + total_creditos - total_debitos
        diferencia = saldo_final_calculado - saldo_final_visto
        cuadra = abs(diferencia) < 0.01

        st.caption("Resumen del período")
        c1, c2, c3 = st.columns(3)
        with c1: st.metric("Saldo inicial", f"$ {fmt_ar(saldo_inicial)}")
        with c2: st.metric("Total créditos (+)", f"$ {fmt_ar(total_creditos)}")
        with c3: st.metric("Total débitos (–)", f"$ {fmt_ar(total_debitos)}")
        c4, c5, c6 = st.columns(3)
        with c4: st.metric("Saldo final (PDF)", f"$ {fmt_ar(saldo_final_visto)}")
        with c5: st.metric("Saldo final calculado", f"$ {fmt_ar(saldo_final_calculado)}")
        with c6: st.metric("Diferencia", f"$ {fmt_ar(diferencia)}")
        try:
            st.success("Conciliado.") if cuadra else st.error("No cuadra la conciliación.")
        except Exception:
            st.write("Conciliación:", "OK" if cuadra else "No cuadra")
        if pd.notna(fecha_cierre):
            st.caption(f"Cierre según PDF: {fecha_cierre.strftime('%d/%m/%Y')}")
        st.info("Sin Movimientos")
        return

    # Con movimientos: insertar SALDO ANTERIOR si existe
    if not np.isnan(saldo_anterior):
        first_date = df["fecha"].dropna().min()
        fecha_apertura = (first_date - pd.Timedelta(days=1)).normalize() + pd.Timedelta(hours=23, minutes=59, seconds=59) if pd.notna(first_date) else pd.NaT
        apertura = pd.DataFrame([{
            "fecha": fecha_apertura,
            "descripcion": "SALDO ANTERIOR",
            "desc_norm": "SALDO ANTERIOR",
            "debito": 0.0,
            "credito": 0.0,
            "importe": 0.0,
            "saldo": float(saldo_anterior),
            "pagina": 0,
            "orden": 0
        }])
        df = pd.concat([apertura, df], ignore_index=True)

    # Débito/Crédito por delta de saldo (robusto para BNA)
    df = df.sort_values(["fecha", "orden"]).reset_index(drop=True)
    df["delta_saldo"] = df["saldo"].diff()
    df["debito"]  = np.where(df["delta_saldo"] < 0, -df["delta_saldo"], 0.0)
    df["credito"] = np.where(df["delta_saldo"] > 0,  df["delta_saldo"], 0.0)
    df["importe"] = df["debito"] - df["credito"]  # signo contable

    # Clasificación
    df["Clasificación"] = df.apply(
        lambda r: clasificar(str(r.get("descripcion","")), str(r.get("desc_norm","")), r.get("debito",0.0), r.get("credito",0.0)),
        axis=1
    )

    # Totales / conciliación
    df_sorted = df.drop(columns=["orden"]).reset_index(drop=True)
    saldo_inicial = float(df_sorted.loc[0, "saldo"])
    total_debitos = float(df_sorted["debito"].sum())
    total_creditos = float(df_sorted["credito"].sum())
    saldo_final_visto = float(df_sorted["saldo"].iloc[-1]) if np.isnan(saldo_final_pdf) else float(saldo_final_pdf)
    saldo_final_calculado = saldo_inicial + total_creditos - total_debitos
    diferencia = saldo_final_calculado - saldo_final_visto
    cuadra = abs(diferencia) < 0.01

    date_suffix = f"_{fecha_cierre.strftime('%Y%m%d')}" if pd.notna(fecha_cierre) else ""
    acc_suffix  = f"_{account_number}"

    st.caption("Resumen del período")
    c1, c2, c3 = st.columns(3)
    with c1: st.metric("Saldo inicial", f"$ {fmt_ar(saldo_inicial)}")
    with c2: st.metric("Total créditos (+)", f"$ {fmt_ar(total_creditos)}")
    with c3: st.metric("Total débitos (–)", f"$ {fmt_ar(total_debitos)}")
    c4, c5, c6 = st.columns(3)
    with c4: st.metric("Saldo final (PDF)", f"$ {fmt_ar(saldo_final_visto)}")
    with c5: st.metric("Saldo final calculado", f"$ {fmt_ar(saldo_final_calculado)}")
    with c6: st.metric("Diferencia", f"$ {fmt_ar(diferencia)}")
    try:
        st.success("Conciliado.") if cuadra else st.error("No cuadra la conciliación.")
    except Exception:
        st.write("Conciliación:", "OK" if cuadra else "No cuadra")
    if pd.notna(fecha_cierre):
        st.caption(f"Cierre según PDF: {fecha_cierre.strftime('%d/%m/%Y')}")

    # ===== Resumen Operativo (IVA + Otros) =====
    st.caption("Resumen Operativo: Registración Módulo IVA")
    iva21_mask  = df_sorted["Clasificación"].eq("IVA 21% (sobre comisiones)")
    iva105_mask = df_sorted["Clasificación"].eq("IVA 10,5% (sobre comisiones)")
    iva21  = float(df_sorted.loc[iva21_mask,  "debito"].sum())
    iva105 = float(df_sorted.loc[iva105_mask, "debito"].sum())
    net21  = round(iva21  / 0.21,  2) if iva21  else 0.0
    net105 = round(iva105 / 0.105, 2) if iva105 else 0.0
    percep_iva = float(df_sorted.loc[df_sorted["Clasificación"].eq("Percepciones de IVA"), "debito"].sum())
    ley_25413  = float(df_sorted.loc[df_sorted["Clasificación"].eq("LEY 25.413"),          "debito"].sum())
    sircreb    = float(df_sorted.loc[df_sorted["Clasificación"].eq("SIRCREB"),            "debito"].sum())

    m1, m2, m3 = st.columns(3)
    with m1: st.metric("Neto Comisiones 21%", f"$ {fmt_ar(net21)}")
    with m2: st.metric("IVA 21%", f"$ {fmt_ar(iva21)}")
    with m3: st.metric("Bruto 21%", f"$ {fmt_ar(net21 + iva21)}")

    n1, n2, n3 = st.columns(3)
    with n1: st.metric("Neto Comisiones 10,5%", f"$ {fmt_ar(net105)}")
    with n2: st.metric("IVA 10,5%", f"$ {fmt_ar(iva105)}")
    with n3: st.metric("Bruto 10,5%", f"$ {fmt_ar(net105 + iva105)}")

    o1, o2, o3 = st.columns(3)
    with o1: st.metric("Percepciones de IVA (RG 3337 / RG 2408)", f"$ {fmt_ar(percep_iva)}")
    with o2: st.metric("Ley 25.413", f"$ {fmt_ar(ley_25413)}")
    with o3: st.metric("SIRCREB", f"$ {fmt_ar(sircreb)}")

    # Tabla
    st.caption("Detalle de movimientos")
    styled = df_sorted.style.format({c: fmt_ar for c in ["debito","credito","importe","saldo"]}, na_rep="—")
    st.dataframe(styled, use_container_width=True)

    # Descargas
    st.caption("Descargar")
    try:
        import xlsxwriter
        output = io.BytesIO()
        with pd.ExcelWriter(output, engine="xlsxwriter") as writer:
            df_sorted.to_excel(writer, index=False, sheet_name="Movimientos")
            wb  = writer.book
            ws  = writer.sheets["Movimientos"]
            money_fmt = wb.add_format({"num_format": "#,##0.00"})
            date_fmt  = wb.add_format({"num_format": "dd/mm/yyyy"})
            for idx, col in enumerate(df_sorted.columns, start=0):
                col_values = df_sorted[col].astype(str)
                max_len = max(len(col), *(len(v) for v in col_values))
                ws.set_column(idx, idx, min(max_len + 2, 40))
            for c in ["debito","credito","importe","saldo"]:
                if c in df_sorted.columns:
                    j = df_sorted.columns.get_loc(c)
                    ws.set_column(j, j, 16, money_fmt)
            if "fecha" in df_sorted.columns:
                j = df_sorted.columns.get_loc("fecha")
                ws.set_column(j, j, 14, date_fmt)

        st.download_button(
            "📥 Descargar Excel",
            data=output.getvalue(),
            file_name=f"resumen_bancario_{banco_slug}{acc_suffix}{date_suffix}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            use_container_width=True,
            key=f"dl_xlsx_{acc_id}",
        )
    except Exception:
        csv_bytes = df_sorted.to_csv(index=False).encode("utf-8-sig")
        st.download_button(
            "📥 Descargar CSV (fallback)",
            data=csv_bytes,
            file_name=f"resumen_bancario_{banco_slug}{acc_suffix}{date_suffix}.csv",
            mime="text/csv",
            use_container_width=True,
            key=f"dl_csv_{acc_id}",
        )

    if REPORTLAB_OK:
        try:
            pdf_buf = io.BytesIO()
            doc = SimpleDocTemplate(pdf_buf, pagesize=A4, title="Resumen Operativo - Registración Módulo IVA")
            styles = getSampleStyleSheet()
            elems = []
            elems.append(Paragraph("Resumen Operativo: Registración Módulo IVA", styles["Title"]))
            elems.append(Spacer(1, 8))
            datos = [
                ["Concepto", "Importe"],
                ["Neto Comisiones 21%",  fmt_ar(net21)],
                ["IVA 21%",               fmt_ar(iva21)],
                ["Bruto 21%",             fmt_ar(net21 + iva21)],
                ["Neto Comisiones 10,5%", fmt_ar(net105)],
                ["IVA 10,5%",             fmt_ar(iva105)],
                ["Bruto 10,5%",           fmt_ar(net105 + iva105)],
                ["Percepciones de IVA (RG 3337 / RG 2408)", fmt_ar(percep_iva)],
                ["Ley 25.413",            fmt_ar(ley_25413)],
                ["SIRCREB",               fmt_ar(sircreb)],
            ]
            datos.append(["TOTAL", fmt_ar(net21 + iva21 + net105 + iva105 + percep_iva + ley_25413 + sircreb)])

            tbl = Table(datos, colWidths=[300, 120])
            tbl.setStyle(TableStyle([
                ("BACKGROUND", (0,0), (-1,0), colors.lightgrey),
                ("TEXTCOLOR",  (0,0), (-1,0), colors.black),
                ("GRID",       (0,0), (-1,-1), 0.3, colors.grey),
                ("ALIGN",      (1,1), (1,-1), "RIGHT"),
                ("FONTNAME",   (0,0), (-1,0), "Helvetica-Bold"),
                ("FONTNAME",   (0,-1), (-1,-1), "Helvetica-Bold"),
            ]))
            elems.append(tbl)
            elems.append(Spacer(1, 12))
            elems.append(Paragraph("Herramienta para uso interno - AIE San Justo", styles["Normal"]))
            doc.build(elems)
            st.download_button(
                "📄 Descargar PDF – Resumen Operativo (IVA)",
                data=pdf_buf.getvalue(),
                file_name=f"Resumen_Operativo_IVA_{banco_slug}{acc_suffix}{date_suffix}.pdf",
                mime="application/pdf",
                use_container_width=True,
                key=f"dl_pdf_{acc_id}",
            )
        except Exception as e:
            st.info(f"No se pudo generar el PDF del Resumen Operativo: {e}")

# ---------- Banco Santa Fe: extraer Nro de cuenta desde “Consolidado de cuentas” ----------
def santafe_extract_accounts(file_like):
    """
    Busca líneas tipo: 'Cuenta Corriente Pesos Nro. 1646/00'
    Devuelve lista de dicts [{'title': 'Cuenta Corriente Pesos', 'nro': '1646/00'}]
    """
    items = []
    for _, ln in extract_all_lines(file_like):
        m = SF_ACC_LINE_RE.search(ln)
        if m:
            title = " ".join(m.group(1).split())
            nro   = m.group(2).strip()
            items.append({"title": title.title(), "nro": nro})
    # quitar duplicados preservando orden
    seen = set()
    uniq = []
    for it in items:
        key = (it["title"], it["nro"])
        if key not in seen:
            seen.add(key)
            uniq.append(it)
    return uniq

# ---------- Banco Nación: meta (Cuenta/CBU/Período) + gastos finales ----------
def bna_extract_gastos_finales(txt: str) -> dict:
    out = {}
    for m in BNA_GASTOS_RE.finditer(txt or ""):
        etiqueta = m.group(1).upper()
        importe = normalize_money(m.group(2))
        if "I.V.A" in etiqueta or "IVA" in etiqueta:
            etiqueta = "I.V.A. BASE"
        out[etiqueta] = float(importe) if importe is not None else np.nan
    return out

def bna_extract_meta(file_like):
    """
    Lee el texto completo y devuelve dict con:
    {'account_number': str|None, 'cbu': str|None, 'period_start': str|None, 'period_end': str|None}
    - Soporta caja larga (Cuenta+CBU) y variante corta de "NRO. CUENTA SUCURSAL"
    """
    txt = _text_from_pdf(file_like)
    acc = cbu = pstart = pend = None

    mper = BNA_PERIODO_RE.search(txt)
    if mper:
        pstart, pend = mper.group(1), mper.group(2)

    macc = BNA_CUENTA_CBU_RE.search(txt)
    if macc:
        acc, cbu = macc.group(1), macc.group(2)
    else:
        monly = BNA_ACC_ONLY_RE.search(txt)
        if monly:
            acc = monly.group(1)

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
        options=("Auto (detectar)", "Banco de Santa Fe", "Banco Macro", "Banco de la Nación Argentina", "Banco Credicoop"),
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
elif _bank_name == "Banco Credicoop":
    st.success(f"Detectado: {_bank_name}")
else:
    st.warning("No se pudo identificar el banco automáticamente. Se intentará procesar.")

_bank_slug = ("macro" if _bank_name == "Banco Macro"
              else "santafe" if _bank_name == "Banco de Santa Fe"
              else "nacion" if _bank_name == "Banco de la Nación Argentina"
              else "credicoop" if _bank_name == "Banco Credicoop"
              else "generico")

# --- Flujo por banco ---
if _bank_name == "Banco Macro":
    blocks = macro_split_account_blocks(io.BytesIO(data))
    if not blocks:
        st.warning("No se detectaron encabezados de cuenta en Macro. Se intentará procesar todo el PDF (podría mezclar cuentas).")
        _lines = [l for _, l in extract_all_lines(io.BytesIO(data))]
        render_account_report(_bank_slug, "CUENTA (PDF completo)", "s/n", "macro-pdf-completo", _lines)
    else:
        st.caption(f"Información de su/s Cuenta/s: {len(blocks)} cuenta(s) detectada(s).")
        for b in blocks:
            render_account_report(_bank_slug, b["titulo"], b["nro"], b["acc_id"], b["lines"])

elif _bank_name == "Banco de Santa Fe":
    sf_accounts = santafe_extract_accounts(io.BytesIO(data))
    all_lines = [l for _, l in extract_all_lines(io.BytesIO(data))]

    if sf_accounts:
        st.caption(f"Consolidado de cuentas: {len(sf_accounts)} detectada(s).")
        for i, acc in enumerate(sf_accounts, start=1):
            title = acc["title"]
            nro   = acc["nro"]
            acc_id = f"santafe-{re.sub(r'[^0-9A-Za-z]+', '_', nro)}"
            render_account_report(_bank_slug, title, nro, acc_id, all_lines)
            if i < len(sf_accounts):
                st.markdown("")
    else:
        render_account_report(_bank_slug, "CUENTA", "s/n", "generica-unica", all_lines)

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

    render_account_report(_bank_slug, titulo, nro, acc_id, all_lines, bna_extras=bna_extras)

elif _bank_name == "Banco Credicoop":
    meta = credicoop_extract_meta(io.BytesIO(data))
    dfc, fecha_cierre, saldo_final_pdf, saldo_anterior_pdf = credicoop_parse_records_xy(io.BytesIO(data))

    titulo = meta.get("title") or "CUENTA (Credicoop)"
    nro = meta.get("account_number") or "s/n"
    acc_id = f"credicoop-{re.sub(r'[^0-9A-Za-z]+', '_', nro)}"
    st.markdown("---")
    st.subheader(f"{titulo} · Nro {nro}")
    col1, col2 = st.columns(2)
    with col1: st.caption(f"Nro. de cuenta: {nro}")
    if meta.get("cbu"):
        with col2: st.caption(f"CBU: {meta['cbu']}")

    if dfc.empty:
        st.info("Sin movimientos.")
    else:
        dfc["desc_norm"] = dfc["descripcion"].map(normalize_desc)
        dfc["Clasificación"] = dfc.apply(
            lambda r: clasificar(str(r.get("descripcion","")), str(r.get("desc_norm","")), r.get("debito",0.0), r.get("credito",0.0)),
            axis=1
        )
        total_debitos  = float(dfc["debito"].sum())
        total_creditos = float(dfc["credito"].sum())
        saldo_inicial  = float(saldo_anterior_pdf) if not np.isnan(saldo_anterior_pdf) else (
            float(dfc["saldo"].iloc[0]) - float(dfc["credito"].iloc[0]) + float(dfc["debito"].iloc[0])
            if pd.notna(dfc["saldo"].iloc[0]) else 0.0
        )
        saldo_final_calc = saldo_inicial + total_creditos - total_debitos
        if not np.isnan(saldo_final_pdf):
            dif = saldo_final_calc - float(saldo_final_pdf)
            cuadra = abs(dif) < 0.01
            saldo_final_visto = float(saldo_final_pdf)
        else:
            dif = 0.0
            cuadra = True
            saldo_final_visto = dfc["saldo"].iloc[-1]

        st.caption("Resumen del período")
        c1, c2, c3 = st.columns(3)
        with c1: st.metric("Saldo inicial", f"$ {fmt_ar(saldo_inicial)}")
        with c2: st.metric("Total créditos (+)", f"$ {fmt_ar(total_creditos)}")
        with c3: st.metric("Total débitos (–)", f"$ {fmt_ar(total_debitos)}")
        c4, c5, c6 = st.columns(3)
        with c4: st.metric("Saldo final (PDF)", f"$ {fmt_ar(saldo_final_visto)}")
        with c5: st.metric("Saldo final calculado", f"$ {fmt_ar(saldo_final_calc)}")
        with c6: st.metric("Diferencia", f"$ {fmt_ar(dif)}")
        try:
            st.success("Conciliado.") if cuadra else st.error("No cuadra la conciliación.")
        except Exception:
            st.write("Conciliación:", "OK" if cuadra else "No cuadra")
        if pd.notna(fecha_cierre):
            st.caption(f"Cierre según PDF: {fecha_cierre.strftime('%d/%m/%Y')}")

        # ===== Resumen Operativo =====
        st.caption("Resumen Operativo: Registración Módulo IVA")
        iva21_mask  = dfc["Clasificación"].eq("IVA 21% (sobre comisiones)")
        iva105_mask = dfc["Clasificación"].eq("IVA 10,5% (sobre comisiones)")
        iva21  = float(dfc.loc[iva21_mask,  "debito"].sum())
        iva105 = float(dfc.loc[iva105_mask, "debito"].sum())
        net21  = round(iva21  / 0.21,  2) if iva21  else 0.0
        net105 = round(iva105 / 0.105, 2) if iva105 else 0.0
        percep_iva = float(dfc.loc[dfc["Clasificación"].eq("Percepciones de IVA"), "debito"].sum())
        ley_25413  = float(dfc.loc[dfc["Clasificación"].eq("LEY 25.413"),          "debito"].sum())
        sircreb    = float(dfc.loc[dfc["Clasificación"].eq("SIRCREB"),            "debito"].sum())

        m1, m2, m3 = st.columns(3)
        with m1: st.metric("Neto Comisiones 21%", f"$ {fmt_ar(net21)}")
        with m2: st.metric("IVA 21%", f"$ {fmt_ar(iva21)}")
        with m3: st.metric("Bruto 21%", f"$ {fmt_ar(net21 + iva21)}")

        n1, n2, n3 = st.columns(3)
        with n1: st.metric("Neto Comisiones 10,5%", f"$ {fmt_ar(net105)}")
        with n2: st.metric("IVA 10,5%", f"$ {fmt_ar(iva105)}")
        with n3: st.metric("Bruto 10,5%", f"$ {fmt_ar(net105 + iva105)}")

        o1, o2, o3 = st.columns(3)
        with o1: st.metric("Percepciones de IVA (RG 3337 / RG 2408)", f"$ {fmt_ar(percep_iva)}")
        with o2: st.metric("Ley 25.413", f"$ {fmt_ar(ley_25413)}")
        with o3: st.metric("SIRCREB", f"$ {fmt_ar(sircreb)}")

        st.caption("Detalle de movimientos")
        show_cols = ["fecha","combte","descripcion","debito","credito","saldo","Clasificación"]
        for c in show_cols:
            if c not in dfc.columns:
                dfc[c] = np.nan
        styled = dfc[show_cols].style.format({c: fmt_ar for c in ["debito","credito","saldo"]}, na_rep="—")
        st.dataframe(styled, use_container_width=True)

        st.caption("Descargar")
        try:
            import xlsxwriter
            output = io.BytesIO()
            with pd.ExcelWriter(output, engine="xlsxwriter") as writer:
                dfc.to_excel(writer, index=False, sheet_name="Movimientos")
                wb  = writer.book
                ws  = writer.sheets["Movimientos"]
                money_fmt = wb.add_format({"num_format": "#,##0.00"})
                date_fmt  = wb.add_format({"num_format": "dd/mm/yyyy"})
                for idx, col in enumerate(dfc.columns, start=0):
                    col_values = dfc[col].astype(str)
                    max_len = max(len(col), *(len(v) for v in col_values))
                    ws.set_column(idx, idx, min(max_len + 2, 50))
                for c in ["debito","credito","saldo"]:
                    if c in dfc.columns:
                        j = dfc.columns.get_loc(c)
                        ws.set_column(j, j, 16, money_fmt)
                if "fecha" in dfc.columns:
                    j = dfc.columns.get_loc("fecha")
                    ws.set_column(j, j, 14, date_fmt)

            st.download_button(
                "📥 Descargar Excel",
                data=output.getvalue(),
                file_name=f"resumen_bancario_{_bank_slug}_{nro}.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                use_container_width=True,
                key=f"dl_xlsx_{acc_id}",
            )
        except Exception:
            csv_bytes = dfc.to_csv(index=False).encode("utf-8-sig")
            st.download_button(
                "📥 Descargar CSV (fallback)",
                data=csv_bytes,
                file_name=f"resumen_bancario_{_bank_slug}_{nro}.csv",
                mime="text/csv",
                use_container_width=True,
                key=f"dl_csv_{acc_id}",
            )

else:
    # Desconocido: procesar genérico
    all_lines = [l for _, l in extract_all_lines(io.BytesIO(data))]
    render_account_report(_bank_slug, "CUENTA", "s/n", "generica-unica", all_lines)
