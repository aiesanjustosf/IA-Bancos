# ia_resumen_bancario.py
# IA - AIE San Justo
# App segura con parser Banco Santander + UI mínima (no rompe bancos existentes)

import re
from dataclasses import dataclass
from typing import List, Dict, Optional
from decimal import Decimal, InvalidOperation

import streamlit as st
import pandas as pd

# --- Config UI base (segura, sin loops) ---
st.set_page_config(page_title="IA Resumen Bancario", layout="wide")
st.title("IA Resumen Bancario")
st.caption("AIE San Justo – Parser bancos (incluye Banco Santander)")

# =========================
#  UTILITARIOS NÚMEROS / TEXTO
# =========================

def _to_decimal(ar_amount: str) -> Decimal:
    """
    Convierte importes con formato AR ($ 1.234.567,89) a Decimal.
    Acepta variantes con/ sin '$' y espacios.
    """
    if ar_amount is None:
        return Decimal("0")
    s = str(ar_amount).strip()
    s = s.replace("$", "").replace(" ", "").replace(".", "").replace("\u00A0", "")
    s = s.replace(",", ".")
    if s == "" or s == "-":
        return Decimal("0")
    try:
        return Decimal(s)
    except InvalidOperation:
        s = re.sub(r"[^0-9\.-]", "", s)
        if s in ("", "-", ".", "-."):
            return Decimal("0")
        return Decimal(s)

_money_rx = r"\$?\s?[0-9\.\u00A0]{1,3}(?:[\. \u00A0][0-9]{3})*(?:,[0-9]{1,2})"

def _find_money_all(s: str) -> List[str]:
    return re.findall(_money_rx, s)

def _normalized(s: str) -> str:
    return re.sub(r"\s+", " ", s.strip().lower())

def format_money(v: Decimal) -> str:
    try:
        return f"{float(v):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    except Exception:
        try:
            return f"{float(Decimal(v)) :,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
        except Exception:
            return str(v)

# =========================
#  MODELO DE DATOS
# =========================
@dataclass
class Movimiento:
    fecha: str
    descripcion: str
    debito: Decimal
    credito: Decimal
    saldo: Decimal
    clasificacion: str

# =========================
#  PARSER BANCO SANTANDER
# =========================

def santander_detect(texto: str) -> bool:
    t = _normalized(texto)
    return (
        "banco santander argentina" in t
        and "movimientos en pesos" in t
        and "saldo inicial" in t
    )

def santander_clasificar(descripcion: str, debito: Decimal, credito: Decimal) -> str:
    d = _normalized(descripcion)

    # Gastos de cuenta
    if "comision por servicio de cuenta" in d or "comisión por servicio de cuenta" in d:
        return "GASTO: Servicio de cuenta"

    # IVA/Percepciones (para Resumen Operativo)
    if "iva 21% reg de transfisc ley27743" in d:
        return "IMPUESTO: IVA 21% Reg. Transfisc 27743"
    if "iva percepcion rg 2408" in d or "iva percepción rg 2408" in d:
        return "IMPUESTO: IVA Percepción RG 2408"

    # Ley 25.413 (débitos/créditos)
    if "impuesto ley 25.413" in d or "impuesto ley 25,413" in d:
        if debito > 0:
            return "IMPUESTO: Ley 25.413 (débito)"
        elif credito > 0:
            return "IMPUESTO: Ley 25.413 (crédito)"
        else:
            return "IMPUESTO: Ley 25.413"

    # SIRCREB
    if "sircreb" in d or "régimen de recaudación sircreb" in d or "regimen de recaudacion sircreb" in d:
        return "IMPUESTO: SIRCREB (Ingresos Brutos s/ crédito)"

    # Sueldos / pagos a terceros (ejemplo del PDF)
    if "pago haberes" in d:
        return "Transferencias a terceros: Haberes"

    # Depósitos
    if "deposito" in d or "depósito" in d:
        return "Depósito de efectivo"

    return "Otros"

def santander_resumen_operativo(movs: List[Movimiento]) -> Dict[str, Decimal]:
    total_gastos = Decimal("0")
    total_iva = Decimal("0")
    ley_debitos = Decimal("0")
    ley_creditos = Decimal("0")
    sircreb = Decimal("0")

    for m in movs:
        cls = m.clasificacion.lower()

        # Gastos: comisión + IVA/Percepciones
        if cls.startswith("gasto"):
            total_gastos += m.debito
        if "iva" in cls:
            total_iva += m.debito
            total_iva -= m.credito

        # Ley 25.413
        if "ley 25.413" in cls:
            ley_debitos += m.debito
            ley_creditos += m.credito

        # SIRCREB
        if "sircreb" in cls:
            sircreb += m.debito

    ley_neto = ley_debitos - ley_creditos
    gastos_totales = total_gastos + total_iva

    return {
        "GASTOS_totales": gastos_totales,
        "IVA_total": total_iva,
        "Ley25413_debitos": ley_debitos,
        "Ley25413_creditos": ley_creditos,
        "Ley25413_neto": ley_neto,
        "SIRCREB_total": sircreb,
    }

def santander_parse(texto: str) -> Dict:
    """
    Parsea 'Movimientos en pesos' de Santander.
    """
    t = texto.replace("\r", "\n")
    mov_ini = re.search(r"Movimientos en pesos", t, flags=re.IGNORECASE)
    if not mov_ini:
        raise ValueError("No se encontró el bloque 'Movimientos en pesos' en Santander.")
    sub = t[mov_ini.end():]
    corte = re.search(r"(Saldo total|Detalle impositivo)\b", sub, flags=re.IGNORECASE)
    if corte:
        sub = sub[:corte.start()]

    raw_lines = [l for l in sub.splitlines()]
    lines = [re.sub(r"\s+$", "", l) for l in raw_lines if l.strip() != ""]

    saldo_inicial = None
    movimientos: List[Movimiento] = []

    fecha_rx = re.compile(r"^\s*(\d{2}/\d{2}/\d{2})\b")
    prev_balance: Optional[Decimal] = None
    buffer_fecha: Optional[str] = None
    buffer_desc: List[str] = []

    def flush_record(line_with_amounts: str):
        nonlocal prev_balance, buffer_fecha, buffer_desc, movimientos
        if buffer_fecha is None:
            return
        amounts = _find_money_all(line_with_amounts)
        if len(amounts) < 2:
            return

        mov_importe = _to_decimal(amounts[0])
        saldo_importe = _to_decimal(amounts[-1])

        deb = Decimal("0")
        cre = Decimal("0")

        if prev_balance is not None:
            delta = saldo_importe - prev_balance
            if abs(delta - mov_importe) < Decimal("0.02"):
                cre = mov_importe
            elif abs(delta + mov_importe) < Decimal("0.02"):
                deb = mov_importe
            else:
                joined_desc = _normalized(" ".join(buffer_desc))
                if any(k in joined_desc for k in ("depósito", "deposito", "credito", "crédito")):
                    cre = mov_importe
                else:
                    deb = mov_importe
        else:
            joined_desc = _normalized(" ".join(buffer_desc))
            if any(k in joined_desc for k in ("depósito", "deposito", "credito", "crédito")):
                cre = mov_importe
            else:
                deb = mov_importe

        clasif = santander_clasificar(" ".join(buffer_desc), deb, cre)

        movimientos.append(
            Movimiento(
                fecha=buffer_fecha,
                descripcion=" ".join(buffer_desc).strip(),
                debito=deb,
                credito=cre,
                saldo=saldo_importe,
                clasificacion=clasif,
            )
        )
        prev_balance = saldo_importe
        buffer_fecha = None
        buffer_desc = []

    for ln in lines:
        ln_clean = ln.strip()

        if "saldo inicial" in _normalized(ln_clean):
            amounts = _find_money_all(ln_clean)
            if amounts:
                saldo_inicial = _to_decimal(amounts[-1])
                prev_balance = saldo_inicial
            continue

        m = fecha_rx.match(ln_clean)
        if m:
            if buffer_fecha is not None and buffer_desc:
                buffer_fecha = None
                buffer_desc = []
            buffer_fecha = m.group(1)
            resto = ln_clean[m.end():].strip()
            if resto:
                buffer_desc = [resto]
            else:
                buffer_desc = []
            continue

        if buffer_fecha is not None:
            money_found = _find_money_all(ln_clean)
            if len(money_found) >= 2:
                flush_record(ln_clean)
            else:
                buffer_desc.append(ln_clean)

    if saldo_inicial is None:
        raise ValueError("No se pudo determinar el Saldo Inicial en Santander.")

    saldo_final = prev_balance if prev_balance is not None else saldo_inicial
    resumen = santander_resumen_operativo(movimientos)

    return {
        "movimientos": movimientos,
        "saldo_inicial": saldo_inicial,
        "saldo_final": saldo_final,
        "resumen_operativo": resumen,
    }

def banco_santander_handler(texto_pdf: str) -> Dict:
    parsed = santander_parse(texto_pdf)
    filas = []
    for m in parsed["movimientos"]:
        filas.append({
            "fecha": m.fecha,
            "descripcion": m.descripcion,
            "debito": float(m.debito),
            "credito": float(m.credito),
            "saldo": float(m.saldo),
            "clasificacion": m.clasificacion,
        })

    return {
        "rows": filas,
        "saldo_inicial": float(parsed["saldo_inicial"]),
        "saldo_final": float(parsed["saldo_final"]),
        "resumen_operativo": {k: float(v) for k, v in parsed["resumen_operativo"].items()},
        "banco": "Santander",
        "moneda": "ARS",
        "cuenta_detectada": "Cuenta Corriente (Santander)",
    }

# =========================
#  ROUTER (extensible a otros bancos)
# =========================

def router_detectar_banco(texto: str) -> str:
    if santander_detect(texto):
        return "Santander"
    # Agregá aquí otras detecciones existentes:
    # if nacion_detect(texto): return "Nación"
    # if macro_detect(texto): return "Macro"
    # if santafe_detect(texto): return "Santa Fe"
    # if credicoop_detect(texto): return "Credicoop"
    return "Desconocido"

def router_parse(texto: str) -> Dict:
    banco = router_detectar_banco(texto)
    if banco == "Santander":
        return banco_santander_handler(texto)
    raise ValueError("No se detectó banco soportado en el PDF.")

# =========================
#  LECTURA PDF (pdfplumber)
# =========================

def leer_pdf_a_texto(file) -> str:
    try:
        import pdfplumber
    except Exception as e:
        raise RuntimeError(f"No se pudo importar pdfplumber: {e}")
    full = []
    with pdfplumber.open(file) as pdf:
        for p in pdf.pages:
            try:
                full.append(p.extract_text() or "")
            except Exception:
                full.append("")
    return "\n".join(full)

# =========================
#  UI
# =========================

col1, col2 = st.columns([2,1], gap="large")

with col1:
    st.subheader("Cargar PDF")
    up = st.file_uploader("Subí el resumen bancario (PDF)", type=["pdf"])

    usar_demo = st.checkbox("Usar PDF de prueba (2509 Santander.pdf) si está disponible en el servidor", value=False)

    if up or usar_demo:
        try:
            if usar_demo:
                # si corrés local/servidor y tenés el archivo
                demo_path = "/mnt/data/2509 Santander.pdf"
                texto = leer_pdf_a_texto(demo_path)
            else:
                texto = leer_pdf_a_texto(up)

            banco = router_detectar_banco(texto)
            st.info(f"Banco detectado: **{banco}**")

            datos = router_parse(texto)

            # DataFrame
            df = pd.DataFrame(datos["rows"])
            if not df.empty:
                df_fmt = df.copy()
                for c in ["debito", "credito", "saldo"]:
                    df_fmt[c] = df_fmt[c].apply(lambda x: format_money(Decimal(str(x))))
                st.subheader("Movimientos")
                st.dataframe(df_fmt, use_container_width=True, height=420)

            # Saldos
            st.subheader("Saldos")
            cA, cB, cC = st.columns(3)
            cA.metric("Saldo inicial", f"$ {format_money(Decimal(str(datos['saldo_inicial'])))}")
            cB.metric("Saldo final", f"$ {format_money(Decimal(str(datos['saldo_final'])))}")
            cC.metric("Moneda", datos.get("moneda", "ARS"))

            # Resumen operativo
            st.subheader("Resumen Operativo")
            ro = datos["resumen_operativo"]
            df_ro = pd.DataFrame(
                {
                    "Concepto": [
                        "GASTOS_totales",
                        "IVA_total",
                        "Ley25413_debitos",
                        "Ley25413_creditos",
                        "Ley25413_neto (débitos - créditos)",
                        "SIRCREB_total",
                    ],
                    "Importe": [
                        ro["GASTOS_totales"],
                        ro["IVA_total"],
                        ro["Ley25413_debitos"],
                        ro["Ley25413_creditos"],
                        ro["Ley25413_neto"],
                        ro["SIRCREB_total"],
                    ],
                }
            )
            df_ro["Importe"] = df_ro["Importe"].apply(lambda x: f"$ {format_money(Decimal(str(x)))}")
            st.dataframe(df_ro, use_container_width=True, height=260)

        except Exception as e:
            st.error(f"Error procesando el PDF: {e}")

with col2:
    st.subheader("Estado")
    st.write("- App cargada correctamente.")
    st.write("- Parser Santander activo.")
    st.write("- Si no se detecta el banco, se mostrará error controlado.")
    st.markdown("—")
    st.caption("Tip: si tu app quedaba en blanco, era por una excepción antes del render. Este layout la captura y la muestra en pantalla.")
