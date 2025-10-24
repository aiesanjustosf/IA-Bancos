# Extractor Credicoop — robusto (lee todo dentro de SALDO ANTERIOR → SALDO AL)
# - Fecha se arma con tokens numéricos aun separados; normaliza dd/mm/aa → dd/mm/AAAA (año del período)
# - Movimiento = fila con fecha; el monto es SIEMPRE el primer NO "Saldo" (Débito→Crédito)
# - Líneas continuadas: agregan descripción y, si faltaba, completan monto
# - Columnas por encabezado en MISMA FILA o fallback por montos agregados de todas las páginas
# - Conciliación: saldo_inicial − ΣDébitos + ΣCréditos == saldo_final (±$0,01)

import io, re, unicodedata
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Dict, List

import streamlit as st
import pandas as pd
import pdfplumber

st.set_page_config(page_title="Extractor Credicoop", page_icon="📄")
st.title("📄 Extractor Credicoop")

# ---------- patrones / utilidades ----------
SEP_CHARS  = r"\.\u00A0\u202F\u2007 "  # punto, NBSP, NARROW_NBSP, FIGURE_SPACE, espacio
MONEY_RE   = re.compile(rf"^\(?\$?\s*\d{{1,3}}(?:[{SEP_CHARS}]\d{{3}})*,\d{{2}}\)?$")
AMT_TXT    = r"\d{1,3}(?:[.\u00A0\u202F\u2007]\d{3})*,\d{2}"
DATE_STRICT= re.compile(r"^(0[1-9]|[12][0-9]|3[01])/(0[1-9]|1[0-2])/(\d{2}|\d{4})$")
DATE_TOKEN = re.compile(r"^[0-9/]+$")
PERIOD_RE  = re.compile(r"del:\s*(\d{2}/\d{2}/\d{4})\s*al:\s*(\d{2}/\d{2}/\d{4})", re.I)

BAD_HEADERS = (
    "CABAL DEBITO", "TRANSFERENCIAS PESOS", "DEBITOS AUTOMATICOS",
    "TOTAL IMPUESTO", "DETALLE DE TRANSFERENCIAS", "TOTALES",
    "VIENE DE PAGINA ANTERIOR", "CONTINUA EN PAGINA SIGUIENTE"
)

def q2(x: Decimal) -> Decimal:
    return x.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

def wtext(w):
    t = w.get("text", "")
    return t if isinstance(t, str) else str(t)

def parse_money_es(s: str) -> Decimal:
    s = (s or "").strip()
    neg = s.startswith("(") and s.endswith(")")
    if neg: s = s[1:-1]
    s = s.replace("$", "").strip()
    for ch in ["\u00A0", "\u202F", "\u2007", " "]:
        s = s.replace(ch, "")
    s = s.replace(".", "").replace(",", ".")
    try:
        d = Decimal(s)
    except InvalidOperation:
        return Decimal("0.00")
    return q2(-d if neg else d)

def group_rows_by_top(words, tol: float = 2.0):
    rows = {}
    for w in words:
        rows.setdefault(round(w["top"]/tol)*tol, []).append(w)
    return [rows[k] for k in sorted(rows.keys())]

def detect_amount_runs(row_sorted, max_gap: float = 12.0):
    ALLOWED = re.compile(rf"^[\d,{SEP_CHARS}\(\)\-\$]+$")
    runs, cur, last = [], [], None
    for w in row_sorted:
        t = wtext(w)
        if ALLOWED.match(t):
            if last is None or (w["x0"] - last) <= max_gap:
                cur.append(w); last = w["x1"]
            else:
                runs.append(cur); cur = [w]; last = w["x1"]
        else:
            if cur: runs.append(cur); cur = []; last = None
    if cur: runs.append(cur)
    out = []
    for r in runs:
        txt = "".join(wtext(w) for w in r).strip()
        x0 = min(w["x0"] for w in r); x1 = max(w["x1"] for w in r)
        out.append({"text": txt, "x0": x0, "x1": x1,
                    "top": min(w["top"] for w in r), "bottom": max(w["bottom"] for w in r)})
    return out

def normalize_token(t: str) -> str:
    t = unicodedata.normalize("NFD", t or "").upper()
    t = "".join(ch for ch in t if unicodedata.category(ch) != "Mn")
    return re.sub(r"[^A-Z]", "", t)

# ---------- columnas ----------
def _find_label_center_in_row(row_sorted, label: str):
    toks_norm = [normalize_token(wtext(w)) for w in row_sorted]
    n, L = len(toks_norm), len(label)
    for i in range(n):
        acc = ""
        for j in range(i, min(n, i+8)):
            acc += toks_norm[j]
            if len(acc) < L: continue
            if acc.startswith(label):
                span = row_sorted[i:j+1]
                return (min(w["x0"] for w in span) + max(w["x1"] for w in span)) / 2.0
            if len(acc) > L: break
    return None

def centers_from_headers(words) -> Dict[str, float]:
    for row in group_rows_by_top(words, tol=1.2):
        row_sorted = sorted(row, key=lambda w: w["x0"])
        cD = _find_label_center_in_row(row_sorted, "DEBITO")
        cC = _find_label_center_in_row(row_sorted, "CREDITO")
        cS = _find_label_center_in_row(row_sorted, "SALDO")
        if cD is not None and cC is not None and cS is not None:
            return {"debito": cD, "credito": cC, "saldo": cS}
    return {}

def centers_from_amounts(pages_words) -> Dict[str, float]:
    xs = []
    for words in pages_words:
        for row in group_rows_by_top(words):
            for a in detect_amount_runs(sorted(row, key=lambda w: w["x0"])):
                if MONEY_RE.match(a["text"]):
                    xs.append((a["x0"]+a["x1"]) / 2.0)
    if len(xs) < 3:
        return {}
    xs = sorted(xs); n = len(xs)
    d, c, s = xs[n//6], xs[n//2], xs[5*n//6]
    d, c, s = sorted([d, c, s])
    return {"debito": d, "credito": c, "saldo": s}

def compute_bands(c: Dict[str, float]):
    return {
        "borde_D": (c["debito"] + c["credito"]) / 2.0,
        "borde_C": (c["credito"] + c["saldo"]) / 2.0,
        "xD": c["debito"], "xC": c["credito"], "xS": c["saldo"]
    }

def classify_by_band(x: float, b) -> str:
    if b["borde_D"] >= b["borde_C"]:
        if x <= (b["xD"] + b["xC"]) / 2.0: return "debito"
        if x <= (b["xC"] + b["xS"]) / 2.0: return "credito"
        return "saldo"
    return "debito" if x <= b["borde_D"] else ("credito" if x <= b["borde_C"] else "saldo")

# ---------- período / fechas ----------
def extract_period(pdf_bytes):
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        txt = (pdf.pages[0].extract_text(x_tolerance=1, y_tolerance=1) or "")
    m = PERIOD_RE.search(txt)
    if not m:
        return None, None
    start = datetime.strptime(m.group(1), "%d/%m/%Y").date()
    end   = datetime.strptime(m.group(2), "%d/%m/%Y").date()
    return start, end

def normalize_date(txt, period_year):
    d, m, y = txt.split("/")
    if len(y) == 2 and period_year:
        y = str(period_year)
    return f"{d}/{m}/{y}"

def in_period(date_txt, start, end):
    if (not start) or (not end):
        return True
    d = datetime.strptime(date_txt, "%d/%m/%Y").date()
    return start <= d <= end

def find_date_run(row_sorted, max_gap: float = 25.0):
    """Arma una fecha a partir de tokens 0-9/ permitiendo gaps grandes."""
    tokens = sorted(row_sorted, key=lambda w: w["x0"])
    runs, cur, last = [], [], None
    for w in tokens:
        t = wtext(w)
        if DATE_TOKEN.match(t):
            if last is None or (w["x0"] - last) <= max_gap:
                cur.append(w); last = w["x1"]
            else:
                runs.append(cur); cur = [w]; last = w["x1"]
        else:
            if cur: runs.append(cur); cur = []; last = None
    if cur: runs.append(cur)

    for run in runs:
        raw = "".join(wtext(w) for w in run).replace(" ", "")
        if DATE_STRICT.fullmatch(raw):
            return raw, run  # string fecha, tokens
    return None, []

# ---------- saldos por texto ----------
def _find_last_amount_in_text(s: str) -> str|None:
    m = list(re.finditer(AMT_TXT, s))
    return m[-1].group(0) if m else None

def read_summary_balances_from_text(pdf_bytes: bytes):
    saldo_ant = None; saldo_fin = None; fecha_fin = None
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for p in pdf.pages:
            txt = (p.extract_text(x_tolerance=1, y_tolerance=1) or "")
            lines = [ln.strip().upper() for ln in txt.splitlines() if ln.strip()]
            for i, ln in enumerate(lines):
                if "SALDO ANTERIOR" in ln and saldo_ant is None:
                    amt = _find_last_amount_in_text(ln) or (_find_last_amount_in_text(lines[i+1]) if i+1 < len(lines) else None)
                    if amt: saldo_ant = parse_money_es(amt)
                if "SALDO AL" in ln and saldo_fin is None:
                    m = re.search(r"\b(\d{2}/\d{2}/\d{4})\b", ln)
                    if m: fecha_fin = m.group(1)
                    amt = _find_last_amount_in_text(ln) or (_find_last_amount_in_text(lines[i+1]) if i+1 < len(lines) else None)
                    if amt: saldo_fin = parse_money_es(amt)
    return saldo_ant, saldo_fin, fecha_fin

# ---------- elegir monto no-saldo ----------
def pick_non_saldo_amount(row_sorted, bands):
    amts = [a for a in detect_amount_runs(row_sorted) if MONEY_RE.match(a["text"])]
    if not amts:
        return None, Decimal("0.00")
    by = {"debito": [], "credito": [], "saldo": []}
    for a in amts:
        cx = (a["x0"] + a["x1"]) / 2.0
        by[classify_by_band(cx, bands)].append(a)
    if by["debito"]:
        pick = sorted(by["debito"], key=lambda a: a["x0"])[0]
        return "debito", parse_money_es(pick["text"])
    if by["credito"]:
        pick = sorted(by["credito"], key=lambda a: a["x0"])[0]
        return "credito", parse_money_es(pick["text"])
    return None, Decimal("0.00")

# ---------- parser principal ----------
def parse_pdf(pdf_bytes: bytes) -> pd.DataFrame:
    # palabras por página
    pages_words = []
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for p in pdf.pages:
            ws = p.extract_words(use_text_flow=True, keep_blank_chars=False,
                                 extra_attrs=["x0","x1","top","bottom"]) or []
            pages_words.append(ws)

    # columnas
    centers = {}
    for ws in pages_words:
        centers = centers_from_headers(ws)
        if centers: break
    if not centers:
        centers = centers_from_amounts(pages_words)
    if not centers:
        raise RuntimeError("No pude detectar columnas (encabezado ni montos).")
    bands = compute_bands(centers)

    # período y saldos
    start_period, end_period = extract_period(pdf_bytes)
    period_year = (start_period.year if start_period else None)
    saldo_ant, saldo_fin, fecha_fin = read_summary_balances_from_text(pdf_bytes)

    movs = []
    in_table = False
    stop_all = False

    for words in pages_words:
        if stop_all or not words:
            break

        current = None
        for row in group_rows_by_top(words, tol=1.0):
            row_sorted = sorted(row, key=lambda w: w["x0"])
            up = " ".join(wtext(w) for w in row_sorted).upper()

            if not in_table:
                if "SALDO ANTERIOR" in up:
                    in_table = True
                continue

            if "SALDO AL" in up:
                stop_all = True
                break

            if any(bad in up for bad in BAD_HEADERS):
                continue
            if (_find_label_center_in_row(row_sorted, "DEBITO") is not None and
                _find_label_center_in_row(row_sorted, "CREDITO") is not None and
                _find_label_center_in_row(row_sorted, "SALDO")  is not None):
                continue

            # ---- FECHA (armada por tokens, tolerante)
            date_raw, date_run = find_date_run(row_sorted, max_gap=25.0)
            if date_raw:
                date_txt = normalize_date(date_raw, period_year)
                if period_year and not in_period(date_txt, start_period, end_period):
                    date_raw, date_run = None, []
            # ----

            if not date_raw:
                # línea de continuación
                if current:
                    # pego descripción y, si faltaba, intento completar monto
                    left_of_debit = [w for w in row_sorted if w["x0"] >= (max([x["x1"] for x in date_run], default=0)) and w["x0"] < bands["borde_D"] - 2]
                    extra = " ".join(wtext(w) for w in left_of_debit).strip()
                    if extra:
                        current["descripcion"] = (current["descripcion"] + " | " + extra).strip()
                    if current["debito"] == Decimal("0.00") and current["credito"] == Decimal("0.00"):
                        side, val = pick_non_saldo_amount(row_sorted, bands)
                        if side == "debito":  current["debito"]  = val
                        elif side == "credito": current["credito"] = val
                continue

            # cierro y agrego el anterior
            if current:
                movs.append(current)

            # descripción: tokens a la derecha de la fecha y antes de Débito
            last_date_x1 = max(w["x1"] for w in date_run) if date_run else 0
            desc_tokens = [w for w in row_sorted if (w["x0"] >= last_date_x1 and w["x0"] < bands["borde_D"] - 2)]
            # monto del renglón con fecha: NO "saldo"
            side, val = pick_non_saldo_amount(row_sorted, bands)
            deb = cre = Decimal("0.00")
            if side == "debito":  deb = val
            elif side == "credito": cre = val

            current = {
                "fecha": date_txt,
                "descripcion": " ".join(wtext(w) for w in desc_tokens).strip(),
                "debito": deb,
                "credito": cre,
            }

        if current:
            movs.append(current)

    # salida
    rows = []
    if saldo_ant is not None:
        first_date = movs[0]["fecha"] if movs else None
        rows.append({"tipo":"saldo_inicial","fecha":first_date,"descripcion":"SALDO ANTERIOR",
                     "debito":Decimal("0.00"),"credito":Decimal("0.00"),"saldo":saldo_ant})
    rows.extend({"tipo":"movimiento", **m, "saldo":None} for m in movs)
    if saldo_fin is not None:
        last_date = fecha_fin or (movs[-1]["fecha"] if movs else None)
        rows.append({"tipo":"saldo_final","fecha":last_date,"descripcion":"SALDO AL",
                     "debito":Decimal("0.00"),"credito":Decimal("0.00"),"saldo":saldo_fin})

    return pd.DataFrame(rows, columns=["tipo","fecha","descripcion","debito","credito","saldo"])

# ---------- conciliación ----------
def reconcile(df: pd.DataFrame):
    deb = df.loc[df["tipo"]=="movimiento","debito"].sum() if not df.empty else Decimal("0.00")
    cre = df.loc[df["tipo"]=="movimiento","credito"].sum() if not df.empty else Decimal("0.00")
    si_s = df.loc[df["tipo"]=="saldo_inicial","saldo"]; si = si_s.iloc[0] if len(si_s)>0 else None
    sf_s = df.loc[df["tipo"]=="saldo_final","saldo"]; sf = sf_s.iloc[0] if len(sf_s)>0 else None
    calc = q2(si - deb + cre) if si is not None else None
    diff = q2(calc - sf) if (calc is not None and sf is not None) else None
    ok = (diff is not None) and (abs(diff) <= Decimal("0.01"))
    resumen = {
        "saldo_anterior": str(si) if si is not None else None,
        "debito_total": str(deb),
        "credito_total": str(cre),
        "saldo_final_informe": str(sf) if sf is not None else None,
        "saldo_final_calculado": str(calc) if calc is not None else None,
        "diferencia": str(diff) if diff is not None else None,
        "n_movimientos": int((df["tipo"]=="movimiento").sum()) if not df.empty else 0,
    }
    return ok, resumen

# ---------- UI ----------
pdf = st.file_uploader("Subí tu PDF del Banco Credicoop", type=["pdf"])
if not pdf:
    st.info("Esperando un PDF…")
    st.stop()

try:
    pdf_bytes = pdf.read()
    df = parse_pdf(pdf_bytes)
except Exception as e:
    st.error(f"Error al parsear: {e}")
    st.stop()

ok, resumen = reconcile(df)

st.subheader("Conciliación")
c = st.columns(3)
c[0].metric("Saldo anterior", resumen["saldo_anterior"] or "—")
c[1].metric("Débitos", resumen["debito_total"])
c[2].metric("Créditos", resumen["credito_total"])
c2 = st.columns(3)
c2[0].metric("Saldo final (informado)", resumen["saldo_final_informe"] or "—")
c2[1].metric("Saldo final (calculado)", resumen["saldo_final_calculado"] or "—")
c2[2].metric("Diferencia", resumen["diferencia"] or "—")

st.subheader("Movimientos")
st.dataframe(df, use_container_width=True)

if ok:
    out = io.BytesIO()
    with pd.ExcelWriter(out, engine="openpyxl") as w:
        dfx = df.copy()
        for col in ["debito","credito","saldo"]:
            dfx[col + "_num"] = dfx[col].apply(lambda x: float(x) if x is not None else 0.0)
            dfx[col + "_centavos"] = dfx[col].apply(
                lambda x: int((x*100).to_integral_value(rounding=ROUND_HALF_UP)) if x is not None else 0
            )
            dfx[col] = dfx[col].astype(str)
        dfx.to_excel(w, index=False, sheet_name="Tabla")
    st.download_button("⬇️ Descargar Excel",
                       data=out.getvalue(),
                       file_name="credicoop_movimientos.xlsx",
                       mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
else:
    st.error("❌ La conciliación NO cierra (±$0,01). Revisá que los montos caigan en la banda correcta y que el PDF no tenga separadores rotos.")
