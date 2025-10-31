# =========================
#  PARSER BANCO SANTANDER
#  Integración no destructiva
#  AIE San Justo - IA Resumen Bancario
# =========================

import re
from dataclasses import dataclass
from typing import List, Dict, Optional, Tuple
from decimal import Decimal, InvalidOperation

# ============== Utilitarios numéricos ==============

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
        # último intento: limpiar cualquier basura residual
        s = re.sub(r"[^0-9\.-]", "", s)
        if s in ("", "-", ".", "-."):
            return Decimal("0")
        return Decimal(s)

_money_rx = r"\$?\s?[0-9\.\u00A0]{1,3}(?:[\. \u00A0][0-9]{3})*(?:,[0-9]{1,2})"

def _find_money_all(s: str) -> List[str]:
    return re.findall(_money_rx, s)

def _normalized(s: str) -> str:
    return re.sub(r"\s+", " ", s.strip().lower())

# ============== Modelo de datos ==============

@dataclass
class Movimiento:
    fecha: str
    descripcion: str
    debito: Decimal
    credito: Decimal
    saldo: Decimal
    clasificacion: str

# ============== Detección ==============

def santander_detect(texto: str) -> bool:
    t = _normalized(texto)
    # Señales bien propias del PDF Santander mostrado
    return (
        "banco santander argentina" in t
        and "movimientos en pesos" in t
        and "saldo inicial" in t
    )

# ============== Parser principal ==============

def santander_parse(texto: str) -> Dict:
    """
    Parsea el bloque 'Movimientos en pesos' de Santander.
    - Usa 'Saldo Inicial' como saldo inicial.
    - Cada renglón con fecha tiene un solo importe de movimiento y un saldo.
    - Decide débito/crédito por delta de saldo.
    - Clasifica según reglas pedidas.
    Retorna dict con:
      - movimientos: List[Movimiento]
      - saldo_inicial, saldo_final : Decimal
      - resumen_operativo: dict con totales
    """
    # 1) Aislar sección de 'Movimientos en pesos' hasta 'Saldo total' o 'Detalle impositivo'
    t = texto.replace("\r", "\n")
    mov_ini = re.search(r"Movimientos en pesos", t, flags=re.IGNORECASE)
    if not mov_ini:
        raise ValueError("No se encontró el bloque 'Movimientos en pesos' en Santander.")
    sub = t[mov_ini.end():]

    corte = re.search(r"(Saldo total|Detalle impositivo)\b", sub, flags=re.IGNORECASE)
    if corte:
        sub = sub[:corte.start()]

    # 2) Limpiar y separar líneas
    raw_lines = [l for l in sub.splitlines()]
    lines = [re.sub(r"\s+$", "", l) for l in raw_lines if l.strip() != ""]

    # 3) Buscar 'Saldo Inicial' y tomar saldo
    saldo_inicial = None
    movimientos: List[Movimiento] = []

    fecha_rx = re.compile(r"^\s*(\d{2}/\d{2}/\d{2})\b")
    # Algunos renglones pueden partir la descripción (ej: "Pago haberes" / "2509...").
    # Haremos un pequeño state machine para “acumular” descripción hasta ver importes.
    prev_balance: Optional[Decimal] = None
    buffer_fecha: Optional[str] = None
    buffer_desc: List[str] = []

    def flush_record(line_with_amounts: str):
        nonlocal prev_balance, buffer_fecha, buffer_desc, movimientos
        if buffer_fecha is None:
            return
        # extraer importes: se esperan 2 en cada movimiento (monto y saldo)
        amounts = _find_money_all(line_with_amounts)
        if len(amounts) < 2:
            # Si no se obtienen 2, intentar sumar la línea anterior (algunas extracciones pegan)
            # NOTA: en la muestra real siempre hay 2 (monto, saldo)
            return

        mov_importe = _to_decimal(amounts[0])
        saldo_importe = _to_decimal(amounts[-1])

        # Determinar débito/crédito por delta contra saldo previo
        deb = Decimal("0")
        cre = Decimal("0")

        if prev_balance is not None:
            delta = saldo_importe - prev_balance
            # tolerancia centavos
            if abs(delta - mov_importe) < Decimal("0.02"):
                # Aumentó el saldo -> Crédito
                cre = mov_importe
            elif abs(delta + mov_importe) < Decimal("0.02"):
                # Disminuyó el saldo -> Débito
                deb = mov_importe
            else:
                # Fallback por palabras clave si delta no calza (muy raro)
                joined_desc = _normalized(" ".join(buffer_desc))
                if any(k in joined_desc for k in ("depósito", "deposito", "credito", "crédito")):
                    cre = mov_importe
                else:
                    deb = mov_importe
        else:
            # Primer movimiento sin previo (no debería ocurrir porque tenemos 'Saldo inicial')
            # Asumir crédito por defecto si dice depósito, si no, débito.
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

    for i, ln in enumerate(lines):
        ln_clean = ln.strip()

        # Capturar saldo inicial explícito
        if "saldo inicial" in _normalized(ln_clean):
            # Buscar el último importe de esa línea como saldo inicial
            amounts = _find_money_all(ln_clean)
            if amounts:
                saldo_inicial = _to_decimal(amounts[-1])
                prev_balance = saldo_inicial
            continue

        # Si arranca con fecha, es un nuevo movimiento
        m = fecha_rx.match(ln_clean)
        if m:
            # Si quedó uno abierto, hay que cerrarlo (caso raro sin importes)
            # pero normalmente el cierre se hace cuando aparecen importes.
            if buffer_fecha is not None and buffer_desc:
                # No hay importes, lo descartamos (no debería ocurrir)
                buffer_fecha = None
                buffer_desc = []
            buffer_fecha = m.group(1)
            # Quitar la fecha de la línea para rastrear descripción/ importes
            resto = ln_clean[m.end():].strip()
            if resto:
                buffer_desc = [resto]
            else:
                buffer_desc = []
            continue

        # Si estamos dentro de un movimiento (hay fecha en buffer)
        if buffer_fecha is not None:
            # ¿Vienen importes en esta línea?
            money_found = _find_money_all(ln_clean)
            if len(money_found) >= 2:
                # Esta línea contiene el (monto movimiento) y el (saldo)
                flush_record(ln_clean)
            else:
                # línea de continuación de descripción (ej: “Pago haberes” / “2509...”)
                buffer_desc.append(ln_clean)
        else:
            # Estamos fuera de un registro; no hacemos nada
            pass

    # Cerrar si quedó algo pendiente (muy raro)
    # (Sin importes no se puede decidir débito/crédito, así que lo ignoramos)
    # -- nada --

    if saldo_inicial is None:
        raise ValueError("No se pudo determinar el Saldo Inicial en Santander.")

    saldo_final = prev_balance if prev_balance is not None else saldo_inicial

    # ============== Resumen operativo ==============
    resumen = santander_resumen_operativo(movimientos)

    return {
        "movimientos": movimientos,
        "saldo_inicial": saldo_inicial,
        "saldo_final": saldo_final,
        "resumen_operativo": resumen,
    }

# ============== Clasificación ==============

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

    # Genérica
    return "Otros"

# ============== Totales Resumen Operativo ==============

def santander_resumen_operativo(movs: List[Movimiento]) -> Dict[str, Decimal]:
    total_gastos = Decimal("0")
    total_iva = Decimal("0")
    ley_debitos = Decimal("0")
    ley_creditos = Decimal("0")
    sircreb = Decimal("0")

    for m in movs:
        cls = m.clasificacion.lower()

        # Gastos: comisión + IVA/Percepciones forman parte de gastos operativos
        if cls.startswith("gasto"):
            total_gastos += m.debito  # comisión suele ser débito
        if "iva" in cls:
            # pueden aparecer como débitos (percepciones/rg); si hubiera créditos, se restan
            total_iva += m.debito
            total_iva -= m.credito

        # Ley 25.413: sumar débitos y restar créditos al neto
        if "ley 25.413" in cls:
            ley_debitos += m.debito
            ley_creditos += m.credito

        # SIRCREB: normalmente es débito (percepción sobre créditos)
        if "sircreb" in cls:
            sircreb += m.debito

    # Neto ley 25.413 (como pediste: los créditos RESTAN)
    ley_neto = ley_debitos - ley_creditos

    # Gastos totales incluyen comisión + IVA/percepciones
    gastos_totales = total_gastos + total_iva

    return {
        "GASTOS_totales": gastos_totales,         # Comisión + IVA/percepciones
        "IVA_total": total_iva,                   # Desglosado por claridad
        "Ley25413_debitos": ley_debitos,
        "Ley25413_creditos": ley_creditos,
        "Ley25413_neto": ley_neto,               # débitos - créditos
        "SIRCREB_total": sircreb,                 # Ingresos Brutos s/ crédito
    }

# ============== Integración con tu router de bancos ==============

def banco_santander_handler(texto_pdf: str) -> Dict:
    """
    Empaqueta parseo y devuelve dict listo para integrarse a tu flujo actual.
    """
    parsed = santander_parse(texto_pdf)
    # Convertir a estructura homogénea con el resto (lista de dicts para DataFrame)
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

# Ejemplo de cómo enchufarlo a tu router existente:
# (1) En tu detección global, agregá santander_detect(texto_pdf)
# (2) En tu switch/if de bancos, llamá a banco_santander_handler(texto_pdf)
#
# if santander_detect(texto_pdf):
#     return banco_santander_handler(texto_pdf)
#
# Listo. Mantiene TODO lo previo y suma Santander.
