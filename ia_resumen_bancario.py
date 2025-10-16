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

# --- regex ---
DATE_RE  = re.compile(r"\b\d{1,2}/\d{2}/\d{4}\b")
MONEY_RE = re.compile(r'(?<!\S)(?:\d{1,3}(?:\.\d{3})*|\d+)\s?,\s?\d{2}-?(?!\S)')
LONG_INT_RE = re.compile(r"\b\d{6,}\b")

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

def normalize_desc(desc: str) -> str:
    """Estandariza descripciones: quita prefijos de sucursal, enteros largos y normaliza espacios."""
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
                    continue
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
                    "desc_norm": normalize_desc(desc),
                    "debito": 0.0,
                    "credito": 0.0,
                    "importe": importe,  # magnitud; el signo lo da el delta
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
    with pdfplumber.open(file_like) as pdf:
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
        "desc_norm": "SALDO ANTERIOR",
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
df["importe"] = df["debito"] - df["credito"]

# ---------- CLASIFICACIÓN ----------
def clasificar(desc: str, desc_norm: str, deb: float, cre: float) -> str:
    u = (desc or "").upper()
    n = (desc_norm or "").upper()

    if "SALDO ANTERIOR" in u or "SALDO ANTERIOR" in n:
        return "SALDO ANTERIOR"

    # Impuesto ley 25413 / IMPTRANS
    if ("LEY 25413" in u) or ("IMPTRANS" in u) or ("LEY 25413" in n) or ("IMPTRANS" in n):
        return "LEY 25413"

    # SIRCREB
    if ("SIRCREB" in u) or ("SIRCREB" in n):
        return "SIRCREB"

    # Percepciones de IVA
    if ("IVA PERC" in u) or ("IVA PERCEP" in u) or ("RG3337" in u) or ("IVA PERC" in n) or ("IVA PERCEP" in n) or ("RG3337" in n):
        return "Percepciones de IVA"

    # IVA 21% (sobre comisiones)
    if ("IVA GRAL" in u or "IVA GRAL" in n):
        return "IVA 21% (sobre comisiones)"

    # IVA 10,5% (sobre comisiones)
    if ("IVA RINS" in u or "IVA REDUC" in u or "IVA RINS" in n or "IVA REDUC" in n):
        return "IVA 10,5% (sobre comisiones)"

    # Comisiones varias
    if ("COMOPREM" in n) or ("COMVCAUT" in n) or ("COMTRSIT" in n) or ("COM.NEGO" in n) or ("CO.EXCESO" in n) or ("COM." in n):
        return "Gastos por comisiones"

    # Débitos automáticos (seguros/servicios)
    if ("DB-SNP" in n) or ("DEB.AUT" in n) or ("DEB.AUTOM" in n) or ("SEGUROS" in n) or ("GTOS SEG" in n):
        return "Débito automático"

    # DyC / ARCA / API
    if "DYC" in n:
        return "DyC"
    if "ARCA" in n:
        return "ARCA"
    if "API" in n:
        return "API"

    # Préstamos
    if "DEB.CUOTA PRESTAMO" in n or ("PRESTAMO" in n and "DEB." in n):
        return "Cuota de préstamo"
    if ("CR.PREST" in n) or ("CREDITO PRESTAMOS" in n) or ("CRÉDITO PRÉSTAMOS" in n):
        return "Acreditación Préstamos"

    # Cheques 48hs
    if "CH 48 HS" in n or "CH.48 HS" in n:
        return "Cheques 48 hs"

    # Créditos específicos
    if ("PAGO COMERC" in n) or ("CR-CABAL" in n) or ("CR CABAL" in n) or ("CR TARJ" in n):
        return "Acreditaciones Tarjetas de Crédito/Débito"

    # Depósitos en efectivo
    if ("CR-DEPEF" in n) or ("CR DEPEF" in n) or ("DEPOSITO EFECTIVO" in n) or ("DEP.EFECTIVO" in n) or ("DEP EFECTIVO" in n):
        return "Depósito en Efectivo"

    # Transferencias
    if (("CR-TRSFE" in n) or ("TRANSF RECIB" in n) or ("TRANLINK" in n)) and cre and cre != 0:
        return "Transferencia de terceros recibida"
    if (("DB-TRSFE" in n) or ("TRSFE-ET" in n) or ("TRSFE-IT" in n)) and deb and deb != 0:
        return "Transferencia a terceros realizada"
    if ("DTNCTAPR" in n) or ("ENTRE CTA" in n) or ("CTA PROPIA" in n):
        return "Transferencia entre cuentas propias"

    # Negociados / acreditaciones de valores
    if ("NEG.CONT" in n) or ("NEGOCIADOS" in n):
        return "Acreditación de valores"

    # Fallback por signo
    if cre and cre != 0:
        return "Crédito"
    if deb and deb != 0:
        return "Débito"
    return "Otros"

df["Clasificación"] = df.apply(
    lambda r: clasificar(str(r.get("descripcion","")), str(r.get("desc_norm","")), r.get("debito",0.0), r.get("credito",0.0)),
    axis=1
)
# -------------------------------------------

# --- cabecera / totales / conciliación ---
fecha_cierre, saldo_final_pdf = find_saldo_final(io.BytesIO(data))

df = df.sort_values(["fecha", "orden", "pagina"]).reset_index(drop=True)
df_sorted = df.drop(columns=["orden"]).reset_index(drop=True)

saldo_inicial = float(df_sorted.loc[0, "saldo"])
total_debitos = float(df_sorted["debito"].sum())
total_creditos = float(df_sorted["credito"].sum())
saldo_final_visto = float(df_sorted["saldo"].iloc[-1]) if np.isnan(saldo_final_pdf) else float(saldo_final_pdf)
saldo_final_calculado = saldo_inicial + total_creditos - total_debitos
diferencia = saldo_final_calculado - saldo_final_visto
cuadra = abs(diferencia) < 0.01

# Encabezado
st.subheader("Resumen del período")
c1, c2, c3 = st.columns(3)
with c1: st.metric("Saldo inicial", f"$ {fmt_ar(saldo_inicial)}")
with c2: st.metric("Total créditos (+)", f"$ {fmt_ar(total_creditos)}")
with c3: st.metric("Total débitos (–)", f"$ {fmt_ar(total_debitos)}")
c4, c5, c6 = st.columns(3)
with c4: st.metric("Saldo final (PDF)", f"$ {fmt_ar(saldo_final_visto)}")
with c5: st.metric("Saldo final calculado", f"$ {fmt_ar(saldo_final_calculado)}")
with c6: st.metric("Diferencia", f"$ {fmt_ar(diferencia)}")
st.success("✅ Conciliado.") if cuadra else st.error("❌ No cuadra la conciliación.")

if pd.notna(fecha_cierre):
    st.caption(f"Cierre según PDF: {fecha_cierre.strftime('%d/%m/%Y')}")

st.divider()
st.subheader("Detalle de movimientos")
styled = df_sorted.style.format({c: fmt_ar for c in ["debito","credito","importe","saldo"]}, na_rep="—")
st.dataframe(styled, use_container_width=True)

# ====== Resumen Operativo: Registración Módulo IVA ======
st.divider()
st.subheader("Resumen Operativo: Registración Módulo IVA")

iva21_mask  = df_sorted["Clasificación"].eq("IVA 21% (sobre comisiones)")
iva105_mask = df_sorted["Clasificación"].eq("IVA 10,5% (sobre comisiones)")
iva21  = float(df_sorted.loc[iva21_mask,  "debito"].sum())
iva105 = float(df_sorted.loc[iva105_mask, "debito"].sum())
net21  = round(iva21  / 0.21,  2) if iva21  else 0.0
net105 = round(iva105 / 0.105, 2) if iva105 else 0.0

percep_iva = float(df_sorted.loc[df_sorted["Clasificación"].eq("Percepciones de IVA"), "debito"].sum())
ley_25413  = float(df_sorted.loc[df_sorted["Clasificación"].eq("LEY 25413"),          "debito"].sum())
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
with o1: st.metric("Percepciones de IVA (RG 3337)", f"$ {fmt_ar(percep_iva)}")
with o2: st.metric("Ley 25.413", f"$ {fmt_ar(ley_25413)}")
with o3: st.metric("SIRCREB", f"$ {fmt_ar(sircreb)}")

total_operativo = net21 + iva21 + net105 + iva105 + percep_iva + ley_25413 + sircreb
st.metric("Total Resumen Operativo", f"$ {fmt_ar(total_operativo)}")

# --- Descargar grilla en Excel (con fallback a CSV) ---
st.divider()
st.subheader("Descargar")

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
        for c in ["debito", "credito", "importe", "saldo"]:
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
    csv_bytes = df_sorted.to_csv(index=False).encode("utf-8-sig")
    st.download_button(
        "📥 Descargar CSV (fallback)",
        data=csv_bytes,
        file_name="resumen_bancario.csv",
        mime="text/csv",
        use_container_width=True,
    )

# --- PDF del Resumen Operativo (si reportlab disponible) ---
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
            ["Percepciones de IVA (RG 3337)", fmt_ar(percep_iva)],
            ["Ley 25.413",            fmt_ar(ley_25413)],
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
            file_name="Resumen_Operativo_IVA.pdf",
            mime="application/pdf",
            use_container_width=True,
        )
    except Exception as e:
        st.info(f"No se pudo generar el PDF del Resumen Operativo: {e}")
else:
    st.caption("Para descargar el PDF del Resumen Operativo instalá reportlab en requirements.txt")







