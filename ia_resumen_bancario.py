# ia_resumen_bancario.py
# Herramienta para uso interno - AIE San Justo · Developer: Alfonso Alderete

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

# --- regex ---
DATE_RE  = re.compile(r"\b\d{1,2}/\d{2}/\d{4}\b")
# número con coma y 2 decimales; miles con punto; posible guion final
MONEY_RE = re.compile(r'(?<!\S)(?:\d{1,3}(?:\.\d{3})*|\d+)\s?,\s?\d{2}-?(?!\S)')

# --- utils ---
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

# --- parser movimientos ---
def parse_pdf(file_like) -> pd.DataFrame:
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
                    continue  # se requieren importe+saldo (ambos con coma)
                saldo   = normalize_money(am[-1].group(0))
                importe = normalize_money(am[-2].group(0))
                d = DATE_RE.search(line)
                if not d:
                    continue
                first_money = am[0]
                desc = line[d.end(): first_money.start()].strip()
                rows.append({
                    "fecha": pd.to_datetime(d.group(0), dayfirst=True, errors="coerce"),
                    "descripcion": desc,
                    "debito": 0.0,
                    "credito": 0.0,
                    "importe": importe,   # magnitud; el signo lo da el delta
                    "saldo": saldo,
                    "pagina": pageno,
                    "orden": 1
                })
    return pd.DataFrame(rows)

# --- saldo final ---
def find_saldo_final(file_like):
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

# --- saldo anterior (misma línea) ---
def find_saldo_anterior(file_like):
    """
    Devuelve el saldo anterior tomando EXCLUSIVAMENTE el último monto con coma
    de la MISMA línea que contiene 'SALDO ANTERIOR'. Intenta por palabras y,
    si no lo encuentra, cae a texto crudo.
    """
    with pdfplumber.open(file_like) as pdf:
        # 1) Intento por PALABRAS (más robusto a alineación)
        for page in pdf.pages:
            words = page.extract_words(extra_attrs=["top", "x0"])
            if words:
                ytol = 2.0
                lines = {}
                for w in words:
                    band = round(w["top"] / ytol)
                    lines.setdefault(band, []).append(w)
                for band in sorted(lines):
                    ws = sorted(lines[band], key=lambda w: w["x0"])
                    line_text = " ".join(w["text"] for w in ws)
                    if "SALDO ANTERIOR" in line_text.upper():
                        am = list(MONEY_RE.finditer(line_text))
                        if am:
                            return normalize_money(am[-1].group(0))
        # 2) Fallback por TEXTO
        for page in pdf.pages:
            txt = page.extract_text() or ""
            for raw in txt.splitlines():
                line = " ".join(raw.split())
                if "SALDO ANTERIOR" in line.upper():
                    am = list(MONEY_RE.finditer(line))
                    if am:
                        return normalize_money(am[-1].group(0))
    return np.nan

# --- UI principal ---
uploaded = st.file_uploader("Subí un PDF del resumen bancario", type=["pdf"])
if uploaded is None:
    st.info("La app no almacena datos, toda la información está protegida.")
    st.stop()

data = uploaded.read()

with st.spinner("Procesando PDF..."):
    df = parse_pdf(io.BytesIO(data))

if df.empty:
    st.error("No se detectaron movimientos. Si el PDF tiene un formato distinto, pasame una línea ejemplo (fecha + descripción + importe + saldo).")
    st.stop()

# --- insertar SALDO ANTERIOR como PRIMERA fila sí o sí ---
saldo_anterior = find_saldo_anterior(io.BytesIO(data))
if not np.isnan(saldo_anterior):
    first_date = df["fecha"].min()
    fecha_apertura = (first_date - pd.Timedelta(days=1)).normalize() + pd.Timedelta(hours=23, minutes=59, seconds=59)
    apertura = pd.DataFrame([{
        "fecha": fecha_apertura,
        "descripcion": "SALDO ANTERIOR",
        "debito": 0.0,
        "credito": 0.0,
        "importe": 0.0,
        "saldo": float(saldo_anterior),
        "pagina": 0,
        "orden": 0
    }])
    df = pd.concat([apertura, df], ignore_index=True)

# --- clasificar por variación de saldo ---
df = df.sort_values(["fecha", "orden", "pagina"]).reset_index(drop=True)
df["delta_saldo"] = df["saldo"].diff()

df["debito"]  = 0.0
df["credito"] = 0.0
monto = df["importe"].abs()

mask = df["delta_saldo"].notna()
df.loc[mask & (df["delta_saldo"] > 0), "credito"] = monto[mask & (df["delta_saldo"] > 0)]
df.loc[mask & (df["delta_saldo"] < 0), "debito"]  = monto[mask & (df["delta_saldo"] < 0)]

# importe con convención Débito - Crédito
df["importe"] = df["debito"] - df["credito"]

# ---------- CLASIFICACIÓN (NUEVO) ----------
def clasificar(desc: str, deb: float, cre: float) -> str:
    u = (desc or "").upper()

    if "SALDO ANTERIOR" in u:
        return "SALDO ANTERIOR"

    # Impuesto ley 25413 / IMPTRANS
    if "LEY 25413" in u or "IMPTRANS" in u:
        return "LEY 25413"

    # SIRCREB
    if "SIRCREB" in u:
        return "SIRCREB"

    # Percepciones de IVA
    if "IVA PERC" in u or "IVA PERCEP" in u or "RG3337" in u:
        return "Percepciones de IVA"

    # IVA general / reducido (sobre comisiones)
    if "IVA GRAL" in u or "IVA RINS" in u or "IVA REDUC" in u:
        return "IVA (sobre comisiones)"

    # Comisiones varias
    if "COM." in u or "COMVCAUT" in u or "COMTRSIT" in u or "COM.NEGO" in u:
        return "Gastos por comisiones"

    # Débitos automáticos (seguros/servicios)
    if "DB-SNP" in u or "DEB.AUT" in u or "DEB.AUTOM" in u or "SEGU" in u:
        return "Débito automático"

    # DyC / ARCA / API
    if "DYC" in u:
        return "DyC"
    if "ARCA" in u:
        return "ARCA"
    if "API" in u:
        return "API"

    # Préstamos
    if "DEB.CUOTA PRESTAMO" in u or ("PRESTAMO" in u and "DEB." in u):
        return "Cuota de préstamo"
    if "CR.PREST" in u or "CREDITO PRESTAMOS" in u:
        return "Crédito de préstamo"

    # Cheques 48hs
    if "CH 48 HS" in u or "CH.48 HS" in u:
        return "Cheques 48 hs"

    # Transferencias
    if ("CR-TRSFE" in u or "TRANSF RECIB" in u) and cre and cre != 0:
        return "Transferencia de terceros recibida"
    if (("DB-TRSFE" in u) or ("TRSFE-ET" in u) or ("TRSFE-IT" in u)) and deb and deb != 0:
        return "Transferencia a terceros realizada"
    if "DTNCTAPR" in u or "ENTRE CTA" in u or "CTA PROPIA" in u:
        return "Transferencia entre cuentas propias"

    # Negociados / acreditaciones
    if "NEG.CONT" in u or "NEGOCIADOS" in u:
        return "Acreditación de valores"

    # Fallback por signo
    if cre and cre != 0:
        return "Crédito"
    if deb and deb != 0:
        return "Débito"
    return "Otros"

df["Clasificación"] = df.apply(
    lambda r: clasificar(str(r.get("descripcion","")), r.get("debito",0.0), r.get("credito",0.0)),
    axis=1
)
# -------------------------------------------

# --- cabecera / totales / conciliación ---
fecha_cierre, saldo_final_pdf = find_saldo_final(io.BytesIO(data))

# Orden final (ya viene con SALDO ANTERIOR al inicio)
df = df.sort_values(["fecha", "orden", "pagina"]).reset_index(drop=True)
df_sorted = df.drop(columns=["orden"]).reset_index(drop=True)

# Totales
saldo_inicial = float(df_sorted.loc[0, "saldo"])
total_debitos = float(df_sorted["debito"].sum())
total_creditos = float(df_sorted["credito"].sum())
saldo_final_visto = (
    float(df_sorted["saldo"].iloc[-1])
    if np.isnan(saldo_final_pdf) else float(saldo_final_pdf)
)
saldo_final_calculado = saldo_inicial + total_creditos - total_debitos
diferencia = saldo_final_calculado - saldo_final_visto
cuadra = abs(diferencia) < 0.01  # tolerancia de 1 centavo

# Encabezado
st.subheader("Resumen del período")

c1, c2, c3 = st.columns(3)
with c1:
    st.metric("Saldo inicial", f"$ {fmt_ar(saldo_inicial)}")
with c2:
    st.metric("Total créditos (+)", f"$ {fmt_ar(total_creditos)}")
with c3:
    st.metric("Total débitos (–)", f"$ {fmt_ar(total_debitos)}")

c4, c5, c6 = st.columns(3)
with c4:
    st.metric("Saldo final (PDF)", f"$ {fmt_ar(saldo_final_visto)}")
with c5:
    st.metric("Saldo final calculado", f"$ {fmt_ar(saldo_final_calculado)}")
with c6:
    st.metric("Diferencia", f"$ {fmt_ar(diferencia)}")

if cuadra:
    st.success("✅ Conciliado: Saldo inicial + Créditos – Débitos = Saldo final.")
else:
    st.error("❌ No cuadra la conciliación. Revisá diferencias o líneas descartadas.")

if pd.notna(fecha_cierre):
    st.caption(f"Cierre según PDF: {fecha_cierre.strftime('%d/%m/%Y')}")

st.divider()
st.subheader("Detalle de movimientos")
styled = df_sorted.style.format({c: fmt_ar for c in ["debito","credito","importe","saldo"]}, na_rep="—")
st.dataframe(styled, use_container_width=True)

st.markdown(
    """
    <div style="position:fixed; left:0; right:0; bottom:0; padding:8px 12px; background:#f6f8fa; color:#444; font-size:12px; text-align:center; border-top:1px solid #e5e7eb;">
      Herramienta para uso interno - AIE San Justo · Developer: Alfonso Alderete
    </div>
    """,
    unsafe_allow_html=True,
)

# --- Descargar grilla en Excel (con fallback a CSV) ---
st.divider()
st.subheader("Descargar")

try:
    import xlsxwriter  # preferido por pandas para escribir .xlsx
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine="xlsxwriter") as writer:
        # hoja principal
        df_sorted.to_excel(writer, index=False, sheet_name="Movimientos")
        wb  = writer.book
        ws  = writer.sheets["Movimientos"]

        # formato miles/decimales (Excel usará tu configuración regional al mostrar)
        money_fmt = wb.add_format({"num_format": "#,##0.00"})
        date_fmt  = wb.add_format({"num_format": "dd/mm/yyyy"})

        # autosize columnas y aplicar formato a columnas de dinero/fecha
        for idx, col in enumerate(df_sorted.columns, start=0):
            col_values = df_sorted[col].astype(str)
            max_len = max(len(col), *(len(v) for v in col_values))
            ws.set_column(idx, idx, min(max_len + 2, 40))  # ancho razonable

        cols_money = ["debito", "credito", "importe", "saldo"]
        for c in cols_money:
            if c in df_sorted.columns:
                j = df_sorted.columns.get_loc(c)
                ws.set_column(j, j, 16, money_fmt)

        if "fecha" in df_sorted.columns:
            j = df_sorted.columns.get_loc("fecha")
            ws.set_column(j, j, 14, date_fmt)

    st.download_button(
        "📥 Descargar Excel",
        data=output.getvalue(),
        file_name="resumen_bancario.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        use_container_width=True,
    )

except Exception:
    # Fallback seguro a CSV
    csv_bytes = df_sorted.to_csv(index=False).encode("utf-8-sig")
    st.download_button(
        "📥 Descargar CSV (fallback)",
        data=csv_bytes,
        file_name="resumen_bancario.csv",
        mime="text/csv",
        use_container_width=True,
    )





