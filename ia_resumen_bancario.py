# Extractor Credicoop — v3.4 (chargrid)
# Procesa únicamente SALDO ANTERIOR → SALDO AL
# Fechas y montos por caracteres; evita perder filas cuando el PDF “rompe” tokens.

import io, re, unicodedata
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Dict, List, Tuple, Optional

import streamlit as st
import pandas as pd
import pdfplumber

st.set_page_config(page_title="Extractor Credicoop", page_icon="📄")
st.title("📄 Extractor Credicoop (chargrid)")

# ---------- utilidades ----------
SEP_CHARS  = r"\.\u00A0\u202F\u2007 "         # . NBSP NNBSP FIGURE_SPACE SP
MONEY_RE   = re.compile(rf"\(?\$?\s*\d{{1,3}}(?:[{SEP_CHARS}]\d{{3}})*,\d{{2}}\)?")
DATE_RE    = re.compile(r"^(0[1-9]|[12][0-9]|3[01])/(0[1-9]|1[0-2])/(\d{2}|\d{4})$")
DATE_ANY   = re.compile(r"[0-9/]+")
PERIOD_RE  = re.compile(r"del:\s*(\d{2}/\d{2}/\d{4})\s*al:\s*(\d{2}/\d{2}/\d{4})", re.I)

BAD_HEADERS = (
    "CABAL DEBITO", "TRANSFERENCIAS PESOS", "DEBITOS AUTOMATICOS",
    "TOTAL IMPUESTO", "DETALLE DE TRANSFERENCIAS", "TOTALES",
    "VIENE DE PAGINA ANTERIOR", "CONTINUA EN PAGINA SIGUIENTE"
)

def q2(x: Decimal) -> Decimal:
    return x.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

def norm_money(s: str) -> Optional[Decimal]:
    if not s: return None
    s = s.strip()
    neg = s.startswith("(") and s.endswith(")")
    if neg: s = s[1:-1]
    s = s.replace("$", "")
    for ch in ["\u00A0", "\u202F", "\u2007", " "]: s = s.replace(ch, "")
    s = s.replace(".", "").replace(",", ".")
    try:
        d = Decimal(s)
        return q2(-d if neg else d)
    except InvalidOperation:
        return None

def normalize_token(t: str) -> str:
    t = unicodedata.normalize("NFD", t or "").upper()
    t = "".join(ch for ch in t if unicodedata.category(ch) != "Mn")
    return re.sub(r"[^A-Z]", "", t)

# ---------- helpers de pdf ----------
def line_groups_by_top(chars, tol=1.1):
    # agrupa caracteres por y (top) parecido a “líneas”
    rows = {}
    for c in chars:
        key = round(c["top"]/tol)*tol
        rows.setdefault(key, []).append(c)
    return [sorted(v, key=lambda x: x["x0"]) for k,v in sorted(rows.items())]

def text_from_chars(chars):
    return "".join(c["text"] for c in chars)

def find_centers_from_header_chars(chars) -> Dict[str, float]:
    # busca DEBITO/CREDITO/SALDO en la MISMA LÍNEA (por caracteres)
    for row in line_groups_by_top(chars, tol=1.1):
        row_txt_norm = normalize_token(text_from_chars(row))
        if all(lbl in row_txt_norm for lbl in ("DEBITO","CREDITO","SALDO")):
            # localizar centro aproximado de cada etiqueta uniendo chars consecutivos
            def center_of(label:str)->Optional[float]:
                lab = normalize_token(label)
                accum = ""
                start_x = None; end_x = None
                for c in row:
                    accum += normalize_token(c["text"])
                    if start_x is None and accum.endswith(lab[:1]):  # primer char
                        start_x = c["x0"]
                    if accum.endswith(lab):
                        end_x = c["x1"]; break
                if start_x is not None and end_x is not None:
                    return (start_x + end_x)/2.0
                return None
            cD = center_of("DEBITO"); cC = center_of("CREDITO"); cS = center_of("SALDO")
            if None not in (cD,cC,cS):
                return {"debito":cD,"credito":cC,"saldo":cS}
    return {}

def centers_from_amounts_all_pages(pages_chars) -> Dict[str,float]:
    xs=[]
    for chars in pages_chars:
        for row in line_groups_by_top(chars):
            # tomar secuencias que parezcan montos
            buf=""; run=[]
            for c in row:
                ch=c["text"]
                if re.match(rf"[\d,{SEP_CHARS}\(\)\-\$]", ch):
                    buf+=ch; run.append(c)
                else:
                    if MONEY_RE.fullmatch(buf.strip()):
                        xs.append((run[0]["x0"]+run[-1]["x1"])/2.0)
                    buf=""; run=[]
            if MONEY_RE.fullmatch((buf or "").strip()):
                xs.append((run[0]["x0"]+run[-1]["x1"])/2.0)
    if len(xs)<3: return {}
    xs=sorted(xs); n=len(xs)
    d,c,s=xs[n//6],xs[n//2],xs[5*n//6]
    d,c,s=sorted([d,c,s])
    return {"debito":d,"credito":c,"saldo":s}

def compute_bands(c):
    return {
        "borde_D": (c["debito"] + c["credito"])/2.0,
        "borde_C": (c["credito"] + c["saldo"])/2.0,
        "xD": c["debito"], "xC": c["credito"], "xS": c["saldo"]
    }

def classify_band(x,b):
    if x <= b["borde_D"]: return "debito"
    if x <= b["borde_C"]: return "credito"
    return "saldo"

# ---------- período y saldos ----------
def extract_period(pdf_bytes):
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        txt = (pdf.pages[0].extract_text(x_tolerance=1, y_tolerance=1) or "")
    m = PERIOD_RE.search(txt)
    if not m: return None, None
    return (datetime.strptime(m.group(1), "%d/%m/%Y").date(),
            datetime.strptime(m.group(2), "%d/%m/%Y").date())

def read_saldos_from_text(pdf_bytes):
    saldo_ant = None; saldo_fin=None; fecha_fin=None
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for p in pdf.pages:
            lines = (p.extract_text(x_tolerance=1, y_tolerance=1) or "").splitlines()
            for ln in lines:
                up = (ln or "").upper()
                if "SALDO ANTERIOR" in up and saldo_ant is None:
                    m = re.search(AMT_TXT, up)
                    if m: saldo_ant = norm_money(m.group(0))
                if "SALDO AL" in up and saldo_fin is None:
                    m2 = re.search(r"\b\d{2}/\d{2}/\d{4}\b", up)
                    if m2: fecha_fin = m2.group(0)
                    m3 = re.search(AMT_TXT, up)
                    if m3: saldo_fin = norm_money(m3.group(0))
    return saldo_ant, saldo_fin, fecha_fin

# ---------- detección fecha/montos por fila ----------
def parse_row_by_chars(row, bands, period_year) -> Tuple[Optional[str], str, Optional[Decimal], Optional[Decimal], Optional[Decimal]]:
    """
    Devuelve (fecha, descripcion, deb, cred, saldo)
    - fecha puede ser None si es continuación
    - deb/cred/saldo como Decimal o None
    """
    # segmentar por bandas usando el centro de cada token
    left=[]; deb=[]; cred=[]; sal=[]
    for c in row:
        cx = (c["x0"]+c["x1"])/2.0
        if cx <= bands["borde_D"]-1: left.append(c)
        elif cx <= bands["borde_C"]-1: deb.append(c)
        else: 
            # separar crédito y saldo con el borde_C
            if cx <= bands["borde_C"] + (bands["xS"]-bands["borde_C"])/2.0:
                cred.append(c)
            else:
                sal.append(c)

    # fecha: armar con tokens que matcheen [0-9/], permitir gaps
    date_chars=[c for c in left if DATE_ANY.fullmatch(c["text"])]
    fecha=None; used_until_x=None
    if date_chars:
        buf=""; run=[]
        last_x=None
        for c in date_chars:
            if last_x is None or (c["x0"]-last_x)<=25.0:
                buf += c["text"]; run.append(c); last_x=c["x1"]
            else:
                break
        buf = buf.replace(" ","")
        if DATE_RE.fullmatch(buf):
            d,m,y = buf.split("/")
            if len(y)==2 and period_year: y=str(period_year)
            fecha=f"{d}/{m}/{y}"
            used_until_x = max(x["x1"] for x in run)

    # descripcion: chars del bloque izquierdo a la derecha de la fecha (si hubo)
    desc_chars=[c for c in left if used_until_x is None or c["x0"]>=used_until_x+1]
    descripcion = text_from_chars(desc_chars).strip()

    # montos: texto consolidado por bloque
    def first_money(chars):
        if not chars: return None
        buf=""; run=[]
        for c in chars:
            ch=c["text"]
            if re.fullmatch(rf"[\d,{SEP_CHARS}\(\)\-\$]", ch):
                buf+=ch; run.append(c)
            else:
                if MONEY_RE.fullmatch(buf.strip()): return norm_money(buf.strip())
                buf=""; run=[]
        if MONEY_RE.fullmatch(buf.strip()): return norm_money(buf.strip())
        return None

    deb_v = first_money(deb)
    cred_v = first_money(cred)
    sal_v  = first_money(sal)

    return fecha, descripcion, deb_v, cred_v, sal_v

# ---------- parser principal ----------
def parse_pdf(pdf_bytes: bytes) -> pd.DataFrame:
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        pages_chars = [p.chars for p in pdf.pages]

    # columnas
    centers={}
    for chars in pages_chars:
        centers = find_centers_from_header_chars(chars)
        if centers: break
    if not centers:
        centers = centers_from_amounts_all_pages(pages_chars)
    if not centers:
        raise RuntimeError("No pude detectar columnas (encabezado ni montos).")
    bands = compute_bands(centers)

    # período y saldos
    start_period, end_period = extract_period(pdf_bytes)
    period_year = start_period.year if start_period else None
    saldo_ant, saldo_fin, fecha_fin = read_saldos_from_text(pdf_bytes)

    rows_out=[]
    in_table=False; stop=False
    current=None

    for page_idx, chars in enumerate(pages_chars):
        if stop: break
        for row in line_groups_by_top(chars, tol=1.1):
            row_txt = (text_from_chars(row) or "").upper().strip()
            if not in_table:
                if "SALDO ANTERIOR" in row_txt:
                    in_table=True
                continue
            if "SALDO AL" in row_txt:
                stop=True
                break
            if any(bad in row_txt for bad in BAD_HEADERS):
                continue
            # descartar línea de rótulos
            if all(lbl in normalize_token(row_txt) for lbl in ("DEBITO","CREDITO","SALDO")):
                continue

            fecha, descripcion, deb, cred, sal = parse_row_by_chars(row, bands, period_year)

            if fecha:
                # flush anterior
                if current: rows_out.append(current)
                # normalizar fecha dentro de período si lo conocemos
                if start_period and end_period:
                    try:
                        fdate = datetime.strptime(fecha,"%d/%m/%Y").date()
                        if not (start_period<=fdate<=end_period):
                            # fuera de rango → ignoro
                            current=None
                            continue
                    except: pass
                if deb is None and cred is None:
                    # intentar rescatar de la misma fila en saldo→nulo
                    pass
                current = {"tipo":"movimiento","fecha":fecha,"descripcion":descripcion,
                           "debito":deb or Decimal("0.00"),
                           "credito":cred or Decimal("0.00"),
                           "saldo":None}
            else:
                # continuación
                if current:
                    extra = descripcion.strip()
                    if extra:
                        current["descripcion"] = (current["descripcion"]+" | "+extra).strip()
                    if current["debito"]==Decimal("0.00") and current["credito"]==Decimal("0.00"):
                        if deb: current["debito"]=deb
                        elif cred: current["credito"]=cred

    if current: rows_out.append(current)

    # filas especiales y dataframe
    df_rows=[]
    if saldo_ant is not None:
        first_date = rows_out[0]["fecha"] if rows_out else None
        df_rows.append({"tipo":"saldo_inicial","fecha":first_date,"descripcion":"SALDO ANTERIOR",
                        "debito":Decimal("0.00"),"credito":Decimal("0.00"),"saldo":saldo_ant})
    df_rows += rows_out
    if saldo_fin is not None:
        last_date = fecha_fin or (rows_out[-1]["fecha"] if rows_out else None)
        df_rows.append({"tipo":"saldo_final","fecha":last_date,"descripcion":"SALDO AL",
                        "debito":Decimal("0.00"),"credito":Decimal("0.00"),"saldo":saldo_fin})

    return pd.DataFrame(df_rows, columns=["tipo","fecha","descripcion","debito","credito","saldo"])

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
    st.error("❌ La conciliación NO cierra (±$0,01). Si no aparecen movimientos, el PDF puede venir con separadores de fecha o montos “rotos”.")
