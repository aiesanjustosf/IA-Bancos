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
MONEY_RE = re.compile(r'(?<!\S)-?(?:\d{1,3}(?:\.\d{3})*|\d+)\s?,\s?\d{2}-?(?!\S)')
MONEY_WITH_SIGN_RE = re.compile(r'\$\s*-?(?:\d{1,3}(?:\.\d{3})*|\d+)\s?,\s?\d{2}-?')
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

# ---- Banco Nación ----
BNA_NAME_HINT = "BANCO DE LA NACION ARGENTINA"
BNA_PERIODO_RE = re.compile(r"PERIODO:\s*(\d{2}/\d{2}/\d{4})\s*AL\s*(\d{2}/\d{2}/\d{4})", re.IGNORECASE)
BNA_CUENTA_CBU_RE = re.compile(
    r"NRO\.\s*CUENTA\s+SUCURSAL\s+CLAVE\s+BANCARIA\s+UNIFORME\s+\(CBU\)\s*[\r\n]+(\d+)\s+\d+\s+(\d{22})",
    re.IGNORECASE
)
BNA_ACC_ONLY_RE = re.compile(r"NRO\.\s*CUENTA\s+SUCURSAL\s*[:\-]?\s*[\r\n ]+(\d{6,})", re.IGNORECASE)
BNA_GASTOS_RE = re.compile(r"-\s*(INTERESES|COMISION|SELLADOS|I\.V\.A\.?\s*BASE|SEGURO\s+DE\s+VIDA)\s*\$\s*([0-9\.\s]+,\d{2})", re.IGNORECASE)

# ---- Banco Galicia ----
BANK_GALICIA_HINTS = ("BANCO GALICIA","RESUMEN DE CUENTA","SIRCREB","IMP. DEB./CRE. LEY 25413","TRANSFERENCIA DE TERCEROS")
GALICIA_HEADER_RE  = re.compile(r"\bFECHA\s+DESCRIPCI[ÓO]N\s+ORIGEN\s+CR[ÉE]DITO\s+D[ÉE]BITO\s+SALDO\b", re.I)
GAL_SALDO_INICIAL_RE = re.compile(r"SALDO\s+INICIAL.*?(-?(?:\d{1,3}(?:\.\d{3})*|\d+)\s?,\s?\d{2}-?)", re.I)
GAL_SALDO_FINAL_RE   = re.compile(r"SALDO\s+FINAL.*?(-?(?:\d{1,3}(?:\.\d{3})*|\d+)\s?,\s?\d{2}-?)", re.I)

# ---- Santa Fe - "SALDO ULTIMO RESUMEN" ----
SF_SALDO_ULT_RE = re.compile(r"SALDO\s+U?LTIMO\s+RESUMEN", re.IGNORECASE)

# ---- Banco Santander ----
BANK_SANTANDER_HINTS = ("BANCO SANTANDER", "MOVIMIENTOS EN PESOS", "SALDO INICIAL", "SIRCREB", "LEY 25.413")

# --- utils ---
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

# ---------- Detección de banco ----------
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
    scores = {
        "Banco Macro": sum(1 for k in BANK_MACRO_HINTS if k in U),
        "Banco de Santa Fe": sum(1 for k in BANK_SANTAFE_HINTS if k in U),
        "Banco de la Nación Argentina": sum(1 for k in BANK_NACION_HINTS if k in U),
        "Banco Galicia": sum(1 for k in BANK_GALICIA_HINTS if k in U),
        "Banco Santander": sum(1 for k in BANK_SANTANDER_HINTS if k in U),
    }
    best = max(scores.items(), key=lambda x: x[1])
    return best[0] if best[1] > 0 else "Banco no identificado"

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

# ---------- Galicia específico ----------
def galicia_header_saldos_from_text(txt: str) -> dict:
    ini = fin = np.nan
    m1 = GAL_SALDO_INICIAL_RE.search(txt or "")
    if m1:
        ini = normalize_money(m1.group(1))
    m2 = GAL_SALDO_FINAL_RE.search(txt or "")
    if m2:
        fin = normalize_money(m2.group(1))
    return {"saldo_inicial": ini, "saldo_final": fin}

def parse_galicia_lines(lines: list[str]) -> pd.DataFrame:
    rows, seq = [], 0
    for ln in lines:
        if not ln.strip() or NON_MOV_PAT.search(ln):
            continue
        d = DATE_RE.search(ln)
        if not d:
            continue
        amounts = list(MONEY_RE.finditer(ln))
        if len(amounts) < 3:
            amounts = list(MONEY_WITH_SIGN_RE.finditer(ln))
            if len(amounts) < 3:
                continue
        # esperado: ... crédito, débito, saldo
        saldo = normalize_money(amounts[-1].group(0))
        debito = normalize_money(amounts[-2].group(0))
        credito = normalize_money(amounts[-3].group(0))
        desc = ln[d.end(): amounts[0].start()].strip()
        seq += 1
        rows.append({
            "fecha": pd.to_datetime(d.group(0), dayfirst=True, errors="coerce"),
            "descripcion": desc,
            "desc_norm": normalize_desc(desc),
            "debito": debito,
            "credito": credito,
            "importe": credito - debito,
            "monto_pdf": credito - debito,
            "saldo": saldo,
            "pagina": 0,
            "orden": seq
        })
    return pd.DataFrame(rows)

# ---------- Macro: “Información de su/s Cuenta/s” ----------
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

# ---------- Parsing líneas (genérico + Galicia) ----------
def parse_lines(lines) -> pd.DataFrame:
    """
    Parser genérico por líneas:
    - Acepta montos con o sin '$'
    - No exige que la fecha esté antes del primer monto
    - Usa el último monto como 'saldo' y el penúltimo como 'movimiento'
    """
    rows, seq = [], 0
    for ln in lines:
        s = ln.strip()
        if not s:
            continue
        if PER_PAGE_TITLE_PAT.search(s) or HEADER_ROW_PAT.search(s) or NON_MOV_PAT.search(s):
            continue

        # Montos: combinar con y sin '$'
        ms = list(MONEY_RE.finditer(s))
        ms_dollar = list(MONEY_WITH_SIGN_RE.finditer(s))
        if len(ms_dollar) > len(ms):
            ms = ms_dollar
        # Necesitamos al menos 2 (movimiento y saldo)
        if len(ms) < 2:
            continue

        # Fecha en cualquier parte de la línea
        d = DATE_RE.search(s)
        fecha = pd.to_datetime(d.group(0), dayfirst=True, errors="coerce") if d else pd.NaT

        saldo = normalize_money(ms[-1].group(0))
        mov   = normalize_money(ms[-2].group(0))

        # Descripción: desde el fin de la fecha hasta el inicio del primer monto detectable
        # Si la fecha está después, igual guardamos lo que haya antes del primer monto.
        first_amt_start = ms[0].start()
        desc_start = d.end() if d else 0
        if desc_start > first_amt_start:
            # fecha después del primer monto -> tomar el tramo hasta el monto
            desc = s[:first_amt_start].strip()
        else:
            desc = s[desc_start:first_amt_start].strip()

        seq += 1
        rows.append({
            "fecha": fecha,
            "descripcion": desc,
            "desc_norm": normalize_desc(desc),
            "debito": 0.0,
            "credito": 0.0,
            "importe": mov,          # signo se resuelve luego por delta de saldo
            "monto_pdf": mov,
            "saldo": saldo,
            "pagina": 0,
            "orden": seq
        })
    return pd.DataFrame(rows)


# ---------- Santander: parser dedicado ----------
def parse_santander_lines(lines: list[str]) -> pd.DataFrame:
    rows, seq = [], 0
    current_date = None
    prev_balance = None
    seen_opening = False

    for ln in lines:
        if not ln.strip():
            continue

        mdate = DATE_RE.search(ln)
        if mdate:
            current_date = pd.to_datetime(mdate.group(0), dayfirst=True, errors="coerce")

        U = ln.upper()

        if "SALDO INICIAL" in U:
            am = MONEY_WITH_SIGN_RE.findall(ln) or MONEY_RE.findall(ln)
            if am:
                prev_balance = normalize_money(am[-1])
                seq += 1
                rows.append({
                    "fecha": current_date if current_date is not None else pd.NaT,
                    "descripcion": "SALDO ANTERIOR",
                    "desc_norm": "SALDO ANTERIOR",
                    "debito": 0.0, "credito": 0.0,
                    "importe": 0.0, "monto_pdf": 0.0,
                    "saldo": prev_balance,
                    "pagina": 0, "orden": seq
                })
                seen_opening = True
            continue

        am_dollar = list(MONEY_WITH_SIGN_RE.finditer(ln))
        if len(am_dollar) >= 2:
            mov = normalize_money(am_dollar[0].group(0))
            saldo = normalize_money(am_dollar[-1].group(0))

            deb = 0.0
            cre = 0.0
            if prev_balance is not None:
                delta = saldo - prev_balance
                if abs(delta - mov) < 0.02:
                    cre = mov
                elif abs(delta + mov) < 0.02:
                    deb = mov
                else:
                    if re.search(r"dep[óo]sito|cr[eé]dito", ln, re.IGNORECASE):
                        cre = mov
                    else:
                        deb = mov
            else:
                # primera línea antes de ver saldo inicial (fallback)
                if re.search(r"dep[óo]sito|cr[eé]dito", ln, re.IGNORECASE):
                    cre = mov
                else:
                    deb = mov

            desc_end = am_dollar[0].start()
            if mdate and mdate.end() < desc_end:
                desc = ln[mdate.end():desc_end].strip()
            else:
                desc = ln[:desc_end].strip()

            seq += 1
            rows.append({
                "fecha": current_date if current_date is not None else pd.NaT,
                "descripcion": desc,
                "desc_norm": normalize_desc(desc),
                "debito": deb, "credito": cre,
                "importe": cre - deb, "monto_pdf": mov,
                "saldo": saldo,
                "pagina": 0, "orden": seq
            })
            prev_balance = saldo

    return pd.DataFrame(rows)

# ---------- Saldos ----------
def _only_one_amount(line: str) -> bool:
    return len(list(MONEY_RE.finditer(line))) == 1 or len(list(MONEY_WITH_SIGN_RE.finditer(line))) == 1

def _first_amount_value(line: str) -> float:
    m = MONEY_RE.search(line) or MONEY_WITH_SIGN_RE.search(line)
    return normalize_money(m.group(0)) if m else np.nan

def find_saldo_final_from_lines(lines):
    for ln in reversed(lines):
        if SALDO_FINAL_PREFIX.match(ln):
            d = DATE_RE.search(ln)
            if d and _only_one_amount(ln):
                fecha = pd.to_datetime(d.group(0), dayfirst=True, errors="coerce")
                saldo = _first_amount_value(ln)
                if pd.notna(fecha) and not np.isnan(saldo):
                    return fecha, saldo
    for ln in reversed(lines):
        if "SALDO FINAL" in ln.upper() and _only_one_amount(ln):
            saldo = _first_amount_value(ln)
            if not np.isnan(saldo):
                return pd.NaT, saldo
    return pd.NaT, np.nan

def find_saldo_anterior_from_lines(lines):
    for ln in lines:
        U = ln.upper()
        if "SALDO INICIAL" in U and _only_one_amount(ln):
            v = _first_amount_value(ln)
            if not np.isnan(v):
                return v
    for ln in lines:
        if SALDO_ANT_PREFIX.match(ln):
            d = DATE_RE.search(ln)
            if d and _only_one_amount(ln):
                saldo = _first_amount_value(ln)
                if not np.isnan(saldo):
                    return saldo
    for ln in lines:
        U = ln.upper()
        if "SALDO ANTERIOR" in U and _only_one_amount(ln):
            saldo = _first_amount_value(ln)
            if not np.isnan(saldo):
                return saldo
    for ln in lines:
        U = ln.upper()
        if "SALDO ULTIMO EXTRACTO" in U or "SALDO ÚLTIMO EXTRACTO" in U:
            d = DATE_RE.search(ln)
            if d and _only_one_amount(ln):
                saldo = _first_amount_value(ln)
                if not np.isnan(saldo):
                    return saldo
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

# ---------- Clasificación ----------
def clasificar(desc: str, desc_norm: str, deb: float, cre: float) -> str:
    u = (desc or "").upper()
    n = (desc_norm or "").upper()

    if "SALDO ANTERIOR" in u or "SALDO ANTERIOR" in n:
        return "SALDO ANTERIOR"

    # Ley 25.413
    if ("LEY 25413" in u) or ("IMPTRANS" in u) or ("IMP.S/CREDS" in u) or ("IMPDBCR 25413" in u) or ("N/D DBCR 25413" in u) or \
       ("LEY 25413" in n) or ("IMPTRANS" in n) or ("IMP.S/CREDS" in n) or ("IMPDBCR 25413" in n) or ("N/D DBCR 25413" in n) or \
       ("LEY 25.413" in u) or ("LEY 25.413" in n):
        return "LEY 25413"

    # SIRCREB / Ingresos Brutos s/cred
    if ("SIRCREB" in u) or ("SIRCREB" in n) or re.search(r"ING\.?\s*BRUTOS.*S/?\s*CRED", u) or re.search(r"ING\.?\s*BRUTOS.*S/?\s*CRED", n):
        return "SIRCREB"

    # Percepciones IVA
    if ("IVA PERC" in u) or ("IVA PERCEP" in u) or ("RG3337" in u) or \
       ("IVA PERC" in n) or ("IVA PERCEP" in n) or ("RG3337" in n) or \
       ("RG 2408" in u) or ("RG 2408" in n):
        return "Percepciones de IVA"

    # IVA comisiones
    if ("I.V.A. BASE" in u) or ("I.V.A. BASE" in n) or ("IVA GRAL" in u) or ("IVA GRAL" in n) or \
       ("DEBITO FISCAL IVA BASICO" in u) or ("DEBITO FISCAL IVA BASICO" in n) or \
       ("IVA 21% REG DE TRANSFISC" in u) or ("IVA 21% REG DE TRANSFISC" in n):
        return "IVA 21% (sobre comisiones)"
    if ("IVA RINS" in u or "IVA REDUC" in u) or ("IVA RINS" in n or "IVA REDUC" in n) or ("IVA 10,5" in u) or ("IVA 10,5" in n):
        return "IVA 10,5% (sobre comisiones)"

    # Comisiones (incluye "Comisión por servicio de cuenta")
    if ("COMISION POR SERVICIO DE CUENTA" in u) or ("COMISIÓN POR SERVICIO DE CUENTA" in u):
        return "Gastos por comisiones"
    if ("COMIS.TRANSF" in u) or ("COMIS.TRANSF" in n) or ("COMIS TRANSF" in u) or ("COMIS TRANSF" in n) or \
       ("COMIS.COMPENSACION" in u) or ("COMIS.COMPENSACION" in n) or ("COMIS COMPENSACION" in u) or ("COMIS COMPENSACION" in n) or \
       ("MANTENIMIENTO MENSUAL PAQUETE" in u) or ("MANTENIMIENTO MENSUAL PAQUETE" in n) or \
       ("COMOPREM" in n) or ("COMVCAUT" in n) or ("COMTRSIT" in n) or ("COM.NEGO" in n) or ("CO.EXCESO" in n) or ("COM." in n):
        return "Gastos por comisiones"

    # Débitos automáticos / Seguros
    if ("DB-SNP" in n) or ("DEB.AUT" in n) or ("DEB.AUTOM" in n) or ("SEGUROS" in n) or ("GTOS SEG" in n):
        return "Débito automático"

    if "DYC" in n: return "DyC"
    if ("AFIP" in n or "ARCA" in n) and deb and deb != 0: return "Débitos ARCA"
    if "API" in n: return "API"

    # Préstamos
    if "CUOTA PRÉSTAMO" in u or "CUOTA PRÉSTAMO" in n or "CUOTA PRESTAMO" in u or "CUOTA PRESTAMO" in n or "DEB.CUOTA PRESTAMO" in n:
        return "Cuota de préstamo"
    if ("CR.PREST" in n) or ("CREDITO PRESTAMOS" in n) or ("CRÉDITO PRÉSTAMOS" in n):
        return "Acreditación Préstamos"

    # Cheques
    if "CH 48 HS" in n or "CH.48 HS" in n: return "Cheques 48 hs"

    # Transferencias
    if ("TRANSFERENCIA DE TERCEROS" in u) or ("TRANSFERENCIA DE TERCEROS" in n):
        if cre and cre != 0: return "Transferencia de terceros recibida"
        if deb and deb != 0: return "Transferencia a terceros realizada"
    if (("CR-TRSFE" in n) or ("TRANSF RECIB" in n) or ("TRANLINK" in n)) and cre and cre != 0:
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

# ---------- Helper de UI por cuenta ----------
def render_account_report(
    banco_slug: str,
    account_title: str,
    account_number: str,
    acc_id: str,
    lines: list[str],
    pre_df: pd.DataFrame | None = None,
    bna_extras: dict | None = None,
    header_saldos: dict | None = None
):
    st.markdown("---")
    st.subheader(f"{account_title} · Nro {account_number}")

    # 1) DF base
    df = pre_df.copy() if pre_df is not None else parse_lines(lines)

    # Fallback duro: si no hay filas, usamos parser genérico
    if df is None or df.empty:
        df = parse_lines(lines)

    # Si aun así no hay datos, abortamos limpio
    if df.empty:
        st.error("No se detectaron movimientos en el PDF con los parsers disponibles.")
        return

    # 2) Saldos (si el DF ya trae apertura la respetamos)
    fecha_cierre, saldo_final_pdf = find_saldo_final_from_lines(lines)
    saldo_anterior = find_saldo_anterior_from_lines(lines)

    # 3) Débito/Crédito
    df = df.sort_values(["fecha", "orden"]).reset_index(drop=True)
    if banco_slug in ("galicia", "santander"):
        df["delta_saldo"] = df["saldo"].diff()
        # si vino sin deb/cre (improbable), derivamos del delta
        if ("debito" not in df.columns) or ("credito" not in df.columns):
            df["debito"]  = np.where(df["delta_saldo"] < 0, -df["delta_saldo"], 0.0)
            df["credito"] = np.where(df["delta_saldo"] > 0,  df["delta_saldo"], 0.0)
        if "importe" not in df.columns:
            df["importe"] = df.get("credito", 0.0) - df.get("debito", 0.0)
    else:
        df["delta_saldo"] = df["saldo"].diff()
        df["debito"]  = np.where(df["delta_saldo"] < 0, -df["delta_saldo"], 0.0)
        df["credito"] = np.where(df["delta_saldo"] > 0,  df["delta_saldo"], 0.0)
        df["importe"] = df["debito"] - df["credito"]

    # 4) Saldo inicial / final
    saldo_inicial = np.nan
    if banco_slug == "galicia":
        if header_saldos:
            if not np.isnan(header_saldos.get("saldo_inicial", np.nan)):
                saldo_inicial = float(header_saldos["saldo_inicial"])
            if not np.isnan(header_saldos.get("saldo_final", np.nan)):
                saldo_final_pdf = float(header_saldos["saldo_final"])
        if np.isnan(saldo_inicial) and not df.empty:
            s0 = float(df.loc[0, "saldo"])
            cr0 = float(df.loc[0, "credito"] if "credito" in df.columns else 0.0)
            db0 = float(df.loc[0, "debito"] if "debito" in df.columns else 0.0)
            saldo_inicial = s0 - (cr0 - db0)
    else:
        if not np.isnan(saldo_anterior):
            saldo_inicial = float(saldo_anterior)
        elif not df.empty and pd.notna(df.loc[0, "saldo"]) and pd.notna(df.loc[0, "delta_saldo"]):
            saldo_inicial = float(df.loc[0, "saldo"] - df.loc[0, "delta_saldo"])

    # 5) Insertar SALDO ANTERIOR si no vino en pre_df
    if not np.isnan(saldo_inicial) and not (not df.empty and str(df.loc[0, "descripcion"]).upper() == "SALDO ANTERIOR"):
        first_date = df["fecha"].dropna().min()
        fecha_apertura = (first_date - pd.Timedelta(days=1)).normalize() + pd.Timedelta(hours=23, minutes=59, seconds=59) if pd.notna(first_date) else pd.NaT
        apertura = pd.DataFrame([{
            "fecha": fecha_apertura,
            "descripcion": "SALDO ANTERIOR",
            "desc_norm": "SALDO ANTERIOR",
            "debito": 0.0, "credito": 0.0,
            "importe": 0.0, "monto_pdf": 0.0,
            "saldo": float(saldo_inicial),
            "pagina": 0, "orden": -1
        }])
        df = pd.concat([apertura, df], ignore_index=True).sort_values(["fecha","orden"]).reset_index(drop=True)

    # 6) Clasificación
    df["Clasificación"] = df.apply(
        lambda r: clasificar(str(r.get("descripcion","")), str(r.get("desc_norm","")), r.get("debito",0.0), r.get("credito",0.0)),
        axis=1
    )

    # 7) Totales / conciliación
    df_sorted = df.drop(columns=["orden"]).reset_index(drop=True)
    # Guardas anti-IndexError/KeyError
    if df_sorted.empty:
        st.error("No hay filas para resumir.")
        return

    saldo_inicial_show = float(df_sorted["saldo"].iloc[0]) if "saldo" in df_sorted.columns else 0.0
    total_debitos = float(df_sorted["debito"].sum()) if "debito" in df_sorted.columns else 0.0
    total_creditos = float(df_sorted["credito"].sum()) if "credito" in df_sorted.columns else 0.0
    saldo_final_visto = float(df_sorted["saldo"].iloc[-1]) if np.isnan(saldo_final_pdf) else float(saldo_final_pdf)
    saldo_final_calculado = saldo_inicial_show + total_creditos - total_debitos
    diferencia = saldo_final_calculado - saldo_final_visto
    cuadra = abs(diferencia) < 0.01

    # 8) UI métricas
    date_suffix = f"_{fecha_cierre.strftime('%Y%m%d')}" if pd.notna(fecha_cierre) else ""
    acc_suffix  = f"_{account_number}"

    st.caption("Resumen del período")
    c1, c2, c3 = st.columns(3)
    with c1: metric_text("Saldo inicial", saldo_inicial_show)
    with c2: metric_text("Total créditos (+)", total_creditos)
    with c3: metric_text("Total débitos (–)", total_debitos)
    c4, c5, c6 = st.columns(3)
    with c4: metric_text("Saldo final (PDF)",  saldo_final_visto)
    with c5: metric_text("Saldo final calculado", saldo_final_calculado)
    with c6: metric_text("Diferencia", diferencia)

    if cuadra: st.success("Conciliado.")
    else:      st.error("No cuadra la conciliación.")
    if pd.notna(fecha_cierre):
        st.caption(f"Cierre según PDF: {fecha_cierre.strftime('%d/%m/%Y')}")

    # ===== Resumen Operativo =====
    st.caption("Resumen Operativo: Registración Módulo IVA")

    iva21_mask  = df_sorted["Clasificación"].eq("IVA 21% (sobre comisiones)")
    iva105_mask = df_sorted["Clasificación"].eq("IVA 10,5% (sobre comisiones)")
    iva21  = float(df_sorted.loc[iva21_mask,  "debito"].sum()) if "debito" in df_sorted.columns else 0.0
    iva105 = float(df_sorted.loc[iva105_mask, "debito"].sum()) if "debito" in df_sorted.columns else 0.0
    net21  = round(iva21  / 0.21,  2) if iva21  else 0.0
    net105 = round(iva105 / 0.105, 2) if iva105 else 0.0

    percep_iva = float(df_sorted.loc[df_sorted["Clasificación"].eq("Percepciones de IVA"), "debito"].sum()) if "debito" in df_sorted.columns else 0.0

    # Ley 25.413 neto
    ley_mask = df_sorted["Clasificación"].eq("LEY 25413")
    ley_deb = float(df_sorted.loc[ley_mask, "debito"].sum()) if "debito" in df_sorted.columns else 0.0
    ley_cre = float(df_sorted.loc[ley_mask, "credito"].sum()) if "credito" in df_sorted.columns else 0.0
    ley_25413 = ley_deb - ley_cre

    sircreb = float(df_sorted.loc[df_sorted["Clasificación"].eq("SIRCREB"), "debito"].sum()) if "debito" in df_sorted.columns else 0.0

    m1, m2, m3 = st.columns(3)
    with m1: metric_text("Neto Comisiones 21%", net21)
    with m2: metric_text("IVA 21%", iva21)
    with m3: metric_text("Bruto 21%", net21 + iva21)

    n1, n2, n3 = st.columns(3)
    with n1: metric_text("Neto Comisiones 10,5%", net105)
    with n2: metric_text("IVA 10,5%", iva105)
    with n3: metric_text("Bruto 10,5%", net105 + iva105)

    o1, o2, o3 = st.columns(3)
    with o1: metric_text("Percepciones de IVA (RG 3337 / RG 2408)", percep_iva)
    with o2: metric_text("Ley 25.413 (neto)", ley_25413)
    with o3: metric_text("SIRCREB", sircreb)

    total_operativo = net21 + iva21 + net105 + iva105 + percep_iva + ley_25413 + sircreb
    metric_text("Total Resumen Operativo", total_operativo)

    # 9) Tabla
    st.caption("Detalle de movimientos")
    num_cols = [c for c in ["debito","credito","importe","saldo"] if c in df_sorted.columns]
    styled = df_sorted.style.format({c: fmt_ar for c in num_cols}, na_rep="—")
    st.dataframe(styled, use_container_width=True)

    # 10) Descargas
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
            for c in num_cols:
                j = df_sorted.columns.get_loc(c)
                ws.set_column(j, j, 16, money_fmt)
            if "fecha" in df_sorted.columns:
                j = df_sorted.columns.get_loc("fecha")
                ws.set_column(j, j, 14, date_fmt)

        st.download_button(
            "📥 Descargar Excel",
            data=output.getvalue(),
            file_name=f"resumen_bancario_{banco_slug}_{acc_suffix}{date_suffix}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            use_container_width=True,
            key=f"dl_xlsx_{acc_id}",
        )
    except Exception:
        csv_bytes = df_sorted.to_csv(index=False).encode("utf-8-sig")
        st.download_button(
            "📥 Descargar CSV (fallback)",
            data=csv_bytes,
            file_name=f"resumen_bancario_{banco_slug}_{acc_suffix}{date_suffix}.csv",
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
                ["Ley 25.413 (neto)",     fmt_ar(ley_25413)],
                ["SIRCREB",               fmt_ar(sircreb)],
                ["TOTAL",                 fmt_ar(total_operativo)],
            ]
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
                file_name=f"Resumen_Operativo_IVA_{banco_slug}_{acc_suffix}{date_suffix}.pdf",
                mime="application/pdf",
                use_container_width=True,
                key=f"dl_pdf_{acc_id}",
            )
        except Exception as e:
            st.info(f"No se pudo generar el PDF del Resumen Operativo: {e}")

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
        if "I.V.A" in etiqueta or "IVA" in etiqueta:
            etiqueta = "I.V.A. BASE"
        out[etiqueta] = float(importe) if importe is not None else np.nan
    return out

def bna_extract_meta(file_like):
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
        options=("Auto (detectar)", "Banco de Santa Fe", "Banco Macro", "Banco de la Nación Argentina", "Banco Galicia", "Banco Santander"),
        index=0,
        help="Solo cambia la etiqueta informativa y el nombre de archivo."
    )

_bank_name = forced if forced != "Auto (detectar)" else _auto_bank_name

if _bank_name in ("Banco Macro", "Banco de Santa Fe", "Banco de la Nación Argentina", "Banco Galicia", "Banco Santander"):
    st.success(f"Detectado: {_bank_name}")
else:
    st.warning("No se pudo identificar el banco automáticamente. Se intentará procesar.")

_bank_slug = ("macro" if _bank_name == "Banco Macro"
              else "santafe" if _bank_name == "Banco de Santa Fe"
              else "nacion" if _bank_name == "Banco de la Nación Argentina"
              else "galicia" if _bank_name == "Banco Galicia"
              else "santander" if _bank_name == "Banco Santander"
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
            title = acc["title"]; nro = acc["nro"]
            acc_id = f"santafe-{re.sub(r'[^0-9A-Za-z]+', '_', nro)}"
            render_account_report(_bank_slug, title, nro, acc_id, all_lines)
            if i < len(sf_accounts): st.markdown("")
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

elif _bank_name == "Banco Galicia":
    all_lines = [l for _, l in extract_all_lines(io.BytesIO(data))]
    header_saldos = galicia_header_saldos_from_text(_bank_txt)
    df_gal = parse_galicia_lines(all_lines)
    # Si Galicia no devuelve filas (layout distinto), caemos a genérico
    if df_gal.empty:
        st.info("No se reconoció la tabla estándar de Galicia; usando parser genérico por montos.")
        render_account_report("generico", "Cuenta Corriente (Galicia)", "s/n", "galicia-unica", all_lines)
    else:
        render_account_report(_bank_slug, "Cuenta Corriente (Galicia)", "s/n", "galicia-unica", all_lines, pre_df=df_gal, header_saldos=header_saldos)

elif _bank_name == "Banco Santander":
    all_lines = [l for _, l in extract_all_lines(io.BytesIO(data))]
    df_san = parse_santander_lines(all_lines)
    if df_san.empty:
        st.info("No se reconoció el formato de Santander; usando parser genérico por montos.")
        render_account_report("generico", "Cuenta Corriente (Santander)", "s/n", "santander-unica", all_lines)
    else:
        render_account_report(_bank_slug, "Cuenta Corriente (Santander)", "s/n", "santander-unica", all_lines, pre_df=df_san)

else:
    all_lines = [l for _, l in extract_all_lines(io.BytesIO(data))]
    render_account_report(_bank_slug, "CUENTA", "s/n", "generica-unica", all_lines)
