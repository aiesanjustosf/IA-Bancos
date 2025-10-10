# ia_resumen_bancario.py
# Herramienta para uso interno - AIE San Justo · Developer: Alfonso Alderete

import io, re
from pathlib import Path
import numpy as np
import pandas as pd
import streamlit as st

# --- Config UI (no rompe si faltan assets) ---
HERE = Path(__file__).parent
LOGO = HERE / "logo_aie.png"
FAVICON = HERE / "favicon-aie.ico"
st.set_page_config(page_title="IA Resumen Bancario", page_icon=str(FAVICON) if FAVICON.exists() else None)
if LOGO.exists():
    st.image(str(LOGO), width=200)
st.title("IA Resumen Bancario")

# --- Import diferido (si hay error de lib, la página igual carga y ves el error) ---
try:
    import pdfplumber
except Exception as e:
    st.error(f"No se pudo importar pdfplumber: {e}\nRevisá requirements.txt")
    st.stop()

# --- Regex EXACTOS ---
DATE_RE  = re.compile(r"\b\d{1,2}/\d{2}/\d{4}\b")
# Montos válidos: coma y 2 decimales; miles con punto; guion final opcional.
# Bordes: no pegado a otros caracteres (inicio/espacio a la izq. y espacio/fin a la der.).
MONEY_RE = re.compile(r'(?<!\S)(?:\d{1,3}(?:\.\d{3})*|\d+)\s?,\s?\d{2}-?(?!\S)')

# --- Utilidades ---
def normalize_money(tok: str) -> float:
    """'1.234.567,89-' -> -1234567.89 (coma decimal; dos decimales)."""
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
    """Devuelve 1.234.567,89 para números; '—' si es NaN."""
    if n is None or (isinstance(n, float) and np.isnan(n)):
        return "—"
    return f"{n:,.2f}".replace(",", "§").replace(".", ",").replace("§", ".")

def lines_from_text(page):
    txt = page.extract_text() or ""
    return [" ".join(l.split()) for l in txt.splitlines()]

def lines_from_words(page, ytol=2.0):
    """Reconstruye líneas agrupando por altura; se usa junto a lines_from_text."""
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

# --- Parser principal ---
def parse_pdf(file_like) -> pd.DataFrame:
    """
    Para cada línea:
      - SALDO = último número con coma+2 decimales (puede terminar en '-')
      - IMPORTE = el número con coma+2 decimales inmediatamente a la izquierda del saldo
      - Si la línea no tiene ≥2 montos con coma, se descarta (enteros sin decimales se ignoran)
    Se procesan TEXTO y PALABRAS por página y se UNEN sin duplicados.
    """
    rows = []
    with pdfplumber.open(file_like) as pdf:
        for pageno, page in enumerate(pdf.pages, start=1):
            lt = lines_from_text(page)
            lw = lines_from_words(page, ytol=2.0)
            seen = set(lt)
            combined = lt + [l for l in lw if l not in seen]

            for line in combined:
                if not line.strip():
                    continue
                am = list(MONEY_RE.finditer(line))
                if len(am) < 2:
                    continue  # no inventamos montos

                saldo_tok   = am[-1].group(0)
                importe_tok = am[-2].group(0)
                saldo       = normalize_money(saldo_tok)
                importe     = normalize_money(importe_tok)

                d = DATE_RE.search(line)
                if not d:
                    continue

                first_money = am[0]
                desc = line[d.end(): first_money.start()].strip()

                rows.append({
                    "fecha": pd.to_datetime(d.group(0), dayfirst=True, errors="coerce"),
                    "descripcion": desc,
                    "debito": 0.0,            # se recalculan luego por delta de saldo
                    "credito": 0.0,
                    "importe": importe,        # magnitud; el signo se decide con delta
                    "saldo": saldo,
                    "pagina": pageno
                })

    return pd.DataFrame(rows)

def find_saldo_final(file_like):
    """Busca 'Saldo al dd/mm/aaaa ... <saldo>' y devuelve (fecha_cierre, saldo_final)."""
    with pdfplumber.open(file_like) as pdf:
        for page in reversed(pdf.pages):
            txt = page.extract_text() or ""
            for line in txt.splitlines():
                if "Saldo al" in line:
                    d = DATE_RE.search(line)
                    am = list(MONEY_RE.finditer(line))
                    if d and am:
                        fecha = pd.to_datetime(d.group(0), dayfirst=True, errors="coerce")
                        saldo = normalize_money(am[-1].group(0))
                        return fecha, saldo
    return pd.NaT, np.nan

def find_saldo_anterior(file_like):
    """
    Devuelve el saldo de apertura de la línea que contiene 'SALDO ANTERIOR'.
    Reconstruye la línea por 'altura' y toma EXCLUSIVAMENTE el último monto con coma de ESA línea.
    """
    with pdfplumber.open(file_like) as pdf:
        for page in pdf.pages:
            words = page.extract_words(extra_attrs=["top", "x0"])
            if not words:
                continue
            ytol = 2.0
            lines = {}
            for w in words:
                band = round(w["top"]/ytol)
                lines.setdefault(band, []).append(w)
            for band in sorted(lines.keys()):
                ws = sorted(lines[band], key=lambda w: w["x0"])
                line_text = " ".join(w["text"] for w in ws)
                if "SALDO ANTERIOR" in line_text.upper():
                    am_tokens = list(MONEY_RE.finditer(line_text))
                    if am_tokens:
                        return pd.NaT, normalize_money(am_tokens[-1].group(0))
                    return pd.NaT, np.nan
    return pd.NaT, np.nan

# --- UI principal ---
uploaded = st.file_uploader("Subí un PDF del resumen bancario", type=["pdf"])
if uploaded is None:
    st.info("Cargá un PDF. La app no inventa montos: exige importe+saldo (ambos con coma y 2 decimales).")
    st.stop()

data = uploaded.read()

with st.spinner("Procesando PDF..."):
    df = parse_pdf(io.BytesIO(data))

if df.empty:
    st.error("No se detectaron movimientos. Si el PDF tiene un formato distinto, pasame una línea ejemplo (fecha + descripción + importe + saldo).")
    st.stop()

# --- Insertar SALDO ANTERIOR como primera fila (sin importe) ---
_, saldo_anterior = find_saldo_anterior(io.BytesIO(data))
if not np.isnan(saldo_anterior):
    first_date = df["fecha"].min()
    apertura = pd.DataFrame([{
        "fecha": first_date,
        "descripcion": "SALDO ANTERIOR",
        "debito": 0.0,
        "credito": 0.0,
        "importe": 0.0,
        "saldo": float(saldo_anterior),
        "pagina": 1
    }])
    df = pd.concat([apertura, df], ignore_index=True)

# --- Clasificación por variación de saldo (tu regla) ---
df = df.sort_values(["fecha", "pagina"]).reset_index(drop=True)
df["delta_saldo"] = df["saldo"].diff()

monto = df["importe"].abs()
df["debito"] = 0.0
df["credito"] = 0.0

mask = df["delta_saldo"].notna()
creditos = mask & (df["delta_saldo"] > 0)   # sube el saldo -> crédito
debitos  = mask & (df["delta_saldo"] < 0)   # baja el saldo -> débito

df.loc[creditos, "credito"] = monto[creditos]
df.loc[debitos,  "debito"]  = monto[debitos]

# Recalcular importe con la convención Débito - Crédito (primera fila queda 0/0)
df["importe"] = df["debito"] - df["credito"]

# --- Saldos cabecera ---
fecha_cierre, saldo_final = find_saldo_final(io.BytesIO(data))

# Resumen
st.subheader("Resumen del período")
c1, c2 = st.columns(2)
c1.metric("Saldo inicial (PDF)", f"$ {fmt_ar(df.iloc[0]['saldo'])}" if not df.empty else "—")
c2.metric("Saldo final (PDF)", f"$ {fmt_ar(saldo_final)}" if not np.isnan(saldo_final) else "—")
if pd.notna(fecha_cierre):
    st.caption(f"Cierre: {fecha_cierre.strftime('%d/%m/%Y')}")

st.divider()
st.subheader("Detalle de movimientos")

df_sorted = df.sort_values(["fecha","pagina"]).reset_index(drop=True)
cols_money = ["debito", "credito", "importe", "saldo"]
styled = df_sorted.style.format({c: fmt_ar for c in cols_money}, na_rep="—")
st.dataframe(styled, use_container_width=True)

st.markdown(
    """
    <div style="position:fixed; left:0; right:0; bottom:0; padding:8px 12px; background:#f6f8fa; color:#444; font-size:12px; text-align:center; border-top:1px solid #e5e7eb;">
      Herramienta para uso interno - AIE San Justo · Developer: Alfonso Alderete
    </div>
    """,
    unsafe_allow_html=True,
)


