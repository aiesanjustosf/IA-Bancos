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
# Montos válidos: coma y 2 decimales; miles con punto (sin espacios como miles); guion final opcional.
# Bordes: no debe estar pegado a otros caracteres (inicio/espacio a la izquierda y espacio/fin a la derecha).
MONEY_RE = re.compile(r'(?<!\S)(?:\d{1,3}(?:\.\d{3})*|\d+)\s?,\s?\d{2}-?(?!\S)')

# --- Utilidades de parseo/estilo ---
def normalize_money(tok: str) -> float:
    """'1.234.567,89-' -> -1234567.89 (coma decimal; dos decimales)."""
    if not tok:
        return np.nan
    tok = tok.strip()
    neg = tok.endswith("-")
    tok = tok.rstrip("-")
    if "," not in tok:
        return np.nan
    main, frac = tok.rsplit(",", 1)  # separador decimal fijo: coma
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
    """Reconstruye líneas agrupando por altura; se usa SIEMPRE junto a lines_from_text."""
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

# --- Parser principal (según regla acordada) ---
def parse_pdf(file_like) -> pd.DataFrame:
    """
    Para cada línea:
      - SALDO = último número con coma+2 decimales (puede terminar en '-')
      - IMPORTE = el número con coma+2 decimales inmediatamente a la izquierda del saldo (el más cercano)
      - Si la línea no tiene ≥2 montos con coma, se descarta (enteros sin decimales se ignoran)
    Se procesan SIEMPRE TEXTO y PALABRAS por cada página y se UNEN sin duplicados.
    """
    rows = []
    descartadas = []

    with pdfplumber.open(file_like) as pdf:
        for pageno, page in enumerate(pdf.pages, start=1):
            # Obtener líneas de TEXTO y de PALABRAS
            lt = lines_from_text(page)
            lw = lines_from_words(page, ytol=2.0)

            # Unir sin duplicar (preferimos el texto; sumamos solo las no vistas)
            seen = set(lt)
            combined = lt + [l for l in lw if l not in seen]

            for line in combined:
                if not line.strip():
                    continue

                # Montos válidos: SOLO coma+2 decimales (enteros NO son importes)
                am = list(MONEY_RE.finditer(line))
                if len(am) < 2:
                    descartadas.append((pageno, line))
                    continue

                # Regla: saldo = último; importe = inmediato a la izquierda
                saldo_tok   = am[-1].group(0)
                importe_tok = am[-2].group(0)
                saldo       = normalize_money(saldo_tok)
                importe     = normalize_money(importe_tok)

                # Fecha (en cualquier parte de la línea)
                d = DATE_RE.search(line)
                if not d:
                    descartadas.append((pageno, line))
                    continue

                # Descripción = desde fin de fecha hasta inicio del PRIMER monto con coma
                first_money = am[0]
                desc = line[d.end(): first_money.start()].strip()

                rows.append({
                    "fecha": pd.to_datetime(d.group(0), dayfirst=True, errors="coerce"),
                    "descripcion": desc,
                    "debito": 0.0,   # se recalculan luego por delta de saldo
                    "credito": 0.0,  # se recalculan luego por delta de saldo
                    "importe": importe,  # magnitud del movimiento; signo se decide después
                    "saldo": saldo,
                    "pagina": pageno
                })

    df = pd.DataFrame(rows)

    # Diagnóstico (opcional): ver qué no cumplió la regla de 2 montos con coma
    if 'st' in globals() and descartadas:
        st.warning(f"Líneas descartadas por no contener 2 montos con coma (importe + saldo): {len(descartadas)}")
        sample = descartadas[:10]
        st.caption("Ejemplos (página, línea):")
        for p, l in sample:
            st.text(f"[p.{p}] {l}")

    return df

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
    Detecta 'SALDO ANTERIOR' y devuelve el primer monto (con coma) que aparece
    en esa línea o inmediatamente después. Devuelve (fecha_detectada_o_NaT, saldo_inicial).
    """
    with pdfplumber.open(file_like) as pdf:
        for pageno, page in enumerate(pdf.pages, start=1):
            lines = (page.extract_text() or "").splitlines()
            for i, line in enumerate(lines):
                if "SALDO ANTERIOR" in line.upper():
                    # monto en la misma línea
                    am = list(MONEY_RE.finditer(line))
                    if am:
                        return pd.NaT, normalize_money(am[-1].group(0))
                    # o en la(s) siguiente(s) líneas hasta encontrar un monto
                    j = i + 1
                    while j < len(lines):
                        am2 = list(MONEY_RE.finditer(lines[j]))
                        if am2:
                            return pd.NaT, normalize_money(am2[-1].group(0))
                        # si aparece una fecha antes del monto, frenamos (ya arrancaron los movimientos)
                        if DATE_RE.search(lines[j]):
                            break
                        j += 1
                    return pd.NaT, np.nan
    return pd.NaT, np.nan

# --- UI principal ---
uploaded = st.file_uploader("Subí un PDF del resumen bancario", type=["pdf"])
if uploaded is None:
    st.info("Cargá un PDF. La app no inventa montos: exige importe+saldo (ambos con coma y 2 decimales).")
    st.stop()

data = uploaded.read()
file_like = io.BytesIO(data)

with st.spinner("Procesando PDF..."):
    df = parse_pdf(io.BytesIO(data))

if df.empty:
    st.error("No se detectaron movimientos. Si el PDF tiene un formato distinto, pasame una línea ejemplo (fecha + descripción + importe + saldo).")
    st.stop()

# --- INSERTAR SALDO ANTERIOR COMO PRIMER REGISTRO (sin importe) ---
_, saldo_anterior = find_saldo_anterior(io.BytesIO(data))
if not np.isnan(saldo_anterior):
    # usar como fecha la del primer movimiento (para orden), sin inventar otra
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

# --- CLASIFICACIÓN POR VARIACIÓN DE SALDO (TU REGLA) ---
# Orden lógico
df = df.sort_values(["fecha", "pagina"]).reset_index(drop=True)

# Variación entre saldos consecutivos
df["delta_saldo"] = df["saldo"].diff()

# Magnitud del movimiento (positiva)
monto = df["importe"].abs()

# Inicializo
df["debito"] = 0.0
df["credito"] = 0.0

mask = df["delta_saldo"].notna()

# Tu regla:
#   - Si el saldo SUBE  -> CRÉDITO
#   - Si el saldo BAJA  -> DÉBITO
creditos = mask & (df["delta_saldo"] > 0)
debitos  = mask & (df["delta_saldo"] < 0)

df.loc[creditos, "credito"] = monto[creditos]
df.loc[debitos,  "debito"]  = monto[debitos]

# Primera fila (apertura) queda con 0/0
df["importe"] = df["debito"] - df["credito"]
# --- FIN CLASIFICACIÓN ---

# Saldos de cierre (solo para mostrar en la cabecera)
fecha_cierre, saldo_final = find_saldo_final(io.BytesIO(data))
saldo_inicial_calc = saldo_final - df["importe"].sum() if not np.isnan(saldo_final) else np.nan

# Resumen
st.subheader("Resumen del período")
c1, c2 = st.columns(2)
c1.metric("Saldo inicial (PDF)", f"$ {fmt_ar(df.iloc[0]['saldo'])}" if not df.empty else "—")
c2.metric("Saldo final (PDF)", f"$ {fmt_ar(saldo_final)}")
if pd.notna(fecha_cierre):
    st.caption(f"Cierre: {fecha_cierre.strftime('%d/%m/%Y')}")

st.divider()
st.subheader("Detalle de movimientos")

df_sorted = df.sort_values(["fecha","pagina"]).reset_index(drop=True)
cols_money = ["debito", "credito", "importe", "saldo"]

# Estilo: miles con punto y decimales con coma; mantiene dtype numérico
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

