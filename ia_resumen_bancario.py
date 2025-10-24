# Extractor Credicoop — v3 (robusto)
# Procesa solo la tabla SALDO ANTERIOR → SALDO AL
# - Fecha obligatoria (se arma con tokens, gap grande; debe quedar a la IZQ del borde Crédito→Saldo)
# - Monto del movimiento = SIEMPRE el más izquierdo que NO sea "Saldo" (prioriza Débito)
# - Columnas por encabezado en MISMA FILA o fallback por montos
# - Líneas continuadas: pegan descripción y, si faltaba monto, lo buscan en la continuación
# - "SALDO ANTERIOR" y "SALDO AL dd/mm/aaaa" por texto
# - Período: normaliza dd/mm/aa → dd/mm/AAAA y filtra por rango
# - Conciliación: saldo_inicial − ΣDébitos + ΣCréditos == saldo_final (±$0,01)

import io, re, unicodedata
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Dict, List, Tuple

import streamlit as st
import pandas as pd
import pdfplumber

st.set_page_config(page_title="Extractor Credicoop", page_icon="📄")
st.title("📄 Extractor Credicoop")

# ---------------- Utilidades / patrones ----------------

SEP_CHARS  = r"\.\u00A0\u202F\u2007 "  # punto, NBSP, NARROW_NBSP, FIGURE_SPACE, espacio
MONEY_RE   = re.compile(rf"^\(?\$?\s*\d{{1,3}}(?:[{SEP_CHARS}]\d{{3}})*,\d{{2}}\)?$")
AMT_TXT    = r"\d{1,3}(?:[.\u00A0\u202F\u2007]\d{3})*,\d{2}"
DATE_STRICT= re.compile(r"^(0[1-9]|[12][0-9]|3[01])/(0[1-9]|1[0-2])/(\d{2}|\d{4})$")
DATE_CHARS = re.compile(r"^[0-9/]+$")
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
    neg = False
    if s.startswith("(") and s.endswith(")"):
        neg = True; s = s[1:-1]
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

# ---------------- Columnas (encabezado MISMA FILA + fallback por montos) ----------------

def _find_label_center_in_row(row_sorted, label: str):
    toks_norm = [normalize_token(wtext(w)) for w in row_sorted]
    n, L = len(toks_norm), len(label)
    for i in range(n):
        acc = ""
        for j in range(i, min(n, i+8)):  # tolera "D E B I T O"
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
    per_page_centers = []
    for words in pages_words:
        xs = []
        for row in group_rows_by_top(words):
            for a in detect_amount_runs(sorted(row, key=lambda w: w["x0"])):
                if MONEY_RE.match(a["text"]):
                    xs.append((a["x0"]+a["x1"]) / 2.0)
        if len(xs) >= 3:
            xs = sorted(xs); n = len(xs)
            c = [xs[n//6], xs[n//2], xs[5*n//6]]
            per_page_centers.append(c)
    if not per_page_centers:
        return {}
    d = sum(c[0] for c in per_page_centers) / len(per_page_centers)
    c = sum(c[1] for c in per_page_centers) / len(per_page_centers)
    s = sum(c[2] for c in per_page_centers) / len(per_page_centers)
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

# ---------------- Período y fechas ----------------

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

def detect_date_relaxed(row_sorted, bands, period_year=None, start_period=None, end_period=None, max_gap: float = 25.0):
    """
    Fecha RELAJADA:
    - Arma runs con tokens dígitos o '/' permitiendo gaps grandes (25 px).
    - Acepta si el centro de la fecha está a la IZQ del borde Crédito→Saldo (mucho más laxo).
    - Normaliza año por período y filtra por rango.
    - Devuelve (fecha, tokens_desc) con descripción a la derecha de la fecha y antes del borde Débito.
    """
    # 1) runs de posibles fechas en toda la fila
    tokens = sorted(row_sorted, key=lambda w: w["x0"])
    runs, cur, last = [], [], None
    for w in tokens:
        t = wtext(w)
        if DATE_CHARS.match(t):
            if last is None or (w["x0"] - last) <= max_gap:
                cur.append(w); last = w["x1"]
            else:
                runs.append(cur); cur = [w]; last = w["x1"]
        else:
            if cur: runs.append(cur); cur = []; last = None
    if cur: runs.append(cur)

    candidatos = []
    for run in runs:
        raw = "".join(wtext(w) for w in run)
        raw_squeezed = raw.replace(" ", "")
        if not DATE_STRICT.fullmatch(raw_squeezed):
            continue
        x0 = min(w["x0"] for w in run); x1 = max(w["x1"] for w in run)
        cx = (x0 + x1) / 2.0
        # clave: aceptar si está a la IZQ del borde Crédito→Saldo
        if cx <= (bands["borde_C"] - 5):
            txt = raw_squeezed
            if period_year and len(txt.split("/")[-1]) == 2:
                d, m, y = txt.split("/")
                txt = f"{d}/{m}/{period_year}"
            if period_year and start_period and end_period:
                try:
                    dte = datetime.strptime(txt, "%d/%m/%Y").date()
                    if not (start_period <= dte <= end_period):
                        continue
                except Exception:
                    continue
            candidatos.append((x0, x1, txt, run))

    if not candidatos:
        return None, []

    candidatos.sort(key=lambda t: t[0])
    x0, x1, txt, run = candidatos[0]

    # descripción = tokens a la derecha de la fecha y antes del borde Débito
    desc_tokens = [w for w in tokens if (w["x0"] >= x1 and w["x0"] < (bands["borde_D"] - 2))]
    return txt, desc_tokens

# ---------------- Saldos por TEXTO ----------------

def _find_last_amount_in_text(s: str) -> str|None:
    cands = list(re.finditer(AMT_TXT, s))
    return cands[-1].group(0) if cands else None

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

# ---------------- Monto no-saldo ----------------

def pick_non_saldo_amount(row_sorted, bands):
    """Devuelve ('debito'|'credito', Decimal) tomando SIEMPRE un monto NO 'saldo'."""
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

# ---------------- Parser principal (recorte tabla) ----------------

def parse_pdf(pdf_bytes: bytes) -> pd.DataFrame:
    # Palabras por página
    pages_words = []
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for p in pdf.pages:
            ws = p.extract_words(use_text_flow=True, keep_blank_chars=False,
                                 extra_attrs=["x0","x1","top","bottom"]) or []
            pages_words.append(ws)

    # Columnas
    centers = {}
    for ws in pages_words:
        centers = centers_from_headers(ws)
        if centers: break
    if not centers:
        centers = centers_from_amounts(pages_words)
    if not centers:
        raise RuntimeError("No pude detectar columnas (encabezado ni montos).")
    bands = compute_bands(centers)

    # Período y saldos
    start_period, end_period = extract_period(pdf_bytes)
    period_year = (start_period.year if start_period else None)
    saldo_ant, saldo_fin, fecha_fin = read_summary_balances_from_text(pdf_bytes)

    # Recorrer SOLO la tabla: desde SALDO ANTERIOR hasta SALDO AL...
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

            # entrar a tabla cuando aparece SALDO ANTERIOR
            if not in_table:
                if "SALDO ANTERIOR" in up:
                    in_table = True
                continue  # todo lo previo se ignora

            # cortar DEFINITIVAMENTE cuando aparece SALDO AL
            if "SALDO AL" in up:
                stop_all = True
                break

            # saltar encabezados/leyendas auxiliares y fila de rótulos
            if any(bad in up for bad in BAD_HEADERS):
                continue
            if (_find_label_center_in_row(row_sorted, "DEBITO") is not None and
                _find_label_center_in_row(row_sorted, "CREDITO") is not None and
                _find_label_center_in_row(row_sorted, "SALDO")  is not None):
                continue

            # fecha obligatoria (relajada) + normalización al año de período
            date_txt, desc_tokens = detect_date_relaxed(
                row_sorted, bands,
                period_year=period_year,
                start_period=start_period,
                end_period=end_period
            )

            if not date_txt:
                # continuación: pegar texto y, si aún no hay monto, tomar NO-saldo de la continuación
                if current and row_sorted:
                    extra = " ".join(wtext(w) for w in row_sorted).strip()
                    if extra:
                        current["descripcion"] = (current["descripcion"] + " | " + extra).strip()
                if current and current["debito"] == Decimal("0.00") and current["credito"] == Decimal("0.00"):
                    side, val = pick_non_saldo_amount(row_sorted, bands)
                    if side == "debito":  current["debito"]  = val
                    elif side == "credito": current["credito"] = val
                continue

            # flush inmediato para no comer el primero
            if current:
                movs.append(current)

            # monto del renglón con fecha: NO-saldo (prioriza débito)
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

    # Salida con filas especiales
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

# ---------------- Conciliación ----------------

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

# ---------------- UI ----------------

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
