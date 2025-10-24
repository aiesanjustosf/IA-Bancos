import streamlit as st
import pandas as pd
import re
from io import StringIO
from datetime import datetime

# --- CONFIGURACIÓN DE LA PÁGINA ---
st.set_page_config(
    page_title="Analizador de Resúmenes Bancarios (Credicoop)",
    layout="wide",
    initial_sidebar_state="expanded"
)

# --- CONSTANTES Y UTILIDADES ---

# Patrón regex para identificar las líneas de movimientos en el texto del PDF
# Busca el patrón de una línea que comienza con una fecha DD/MM/AA
PATRON_MOVIMIENTO = r'^\s*\"(\d{2}\/\d{2}\/\d{2})\"\s*,\s*\"?([^\"]*)\"?\s*,\s*\"?([^\"]*)\"?\s*,\s*\"?([^\"]*)\"?\s*,\s*\"?([^\"]*)\"?'

# Palabras clave para categorización (se puede expandir)
CATEGORIAS = {
    "Transferencias Recibidas": ["Transf", "Transfer"],
    "Pagos a Proveedores": ["C MS SA", "VAMAGRO SRL", "CH SISTEMAS SRL", "FILIPPA SILVIA GLADIS", "LOS NARANJOSSA"],
    "Tarjetas y Préstamos": ["TARJETA NARANJA SA", "PILAY SA"],
    "Comisiones Bancarias": ["Comision", "I.V.A. Debito Fiscal", "Percepcion IVA RG 2408", "Servicio Modulo NyP", "Impuesto Ley 25.413"],
    "Cheques y Débitos": ["Cheque de Camara", "Debito Inmediato (DEBIN)", "Debito/Credito Aut Segurcoop"],
    "Ventas Cabal (Ingreso)": ["Pago a Comercios Cabal"],
    "Propinas (Egreso)": ["Contrasiento Liquidacion PROPINAS CABAL"],
}

def clean_and_convert_value(value_str):
    """Limpia y convierte una cadena de valor monetario a float."""
    if isinstance(value_str, str):
        # El resumen usa comas y puntos en el formato de tabla,
        # pero para seguridad, quitamos separadores de miles (punto)
        # y reemplazamos la coma por punto si existe.
        value = value_str.replace('.', '').replace(',', '.')
        # Quitamos espacios y comillas
        value = value.strip().replace('"', '')
        try:
            return float(value)
        except ValueError:
            return 0.0
    return 0.0

def categorize_movement(description):
    """Asigna una categoría basada en la descripción."""
    description_upper = description.upper()
    for cat, keywords in CATEGORIAS.items():
        if any(kw.upper() in description_upper for kw in keywords):
            return cat
    
    # Categorización por defecto basada en el tipo de movimiento
    if "Impuesto Ley 25.413" in description:
        return "Impuestos sobre Movimientos"
    if "IVA" in description or "Percepcion" in description:
        return "Impuestos/Percepciones"
    if "Comision" in description or "Servicio" in description:
        return "Comisiones Bancarias"
    if "CABAL" in description:
        return "Movimientos Cabal"
    if "Transf" in description:
        return "Transferencias"
    if "Pago" in description:
        return "Pagos Varios"

    return "Otros Movimientos"

def extract_movements_from_pdf_text(text):
    """
    Procesa el texto completo del PDF y extrae los movimientos financieros
    siguiendo un patrón regex específico.
    """
    
    # 1. Limpieza preliminar del texto
    # El contenido de las páginas se une. Buscamos la tabla de movimientos.
    start_match = re.search(r'\"FECHA\"\s*,\s*\"COMBTE\"\s*,\s*\"DESCRIPCION\"\s*,\s*\"DEBITO\"\s*,\s*\"CREDITO\"\s*,\s*\"SALDO\"', text)
    if not start_match:
        return pd.DataFrame() # No se encontró la tabla de movimientos

    # Empezar a buscar desde después del encabezado de la tabla
    text_segment = text[start_match.end():]
    
    # Extraer el SALDO ANTERIOR para referencia
    saldo_anterior_match = re.search(r'\"SALDO\"\s*,\s*\"ANTERIOR\"\s*,,,\s*\"?([\d\.,\- ]+)\"?', text)
    saldo_anterior = clean_and_convert_value(saldo_anterior_match.group(1)) if saldo_anterior_match else 0.0

    # Extraer el SALDO FINAL para referencia
    saldo_final_match = re.search(r'\"SALDO\"\s*,\s*\"AL\s+\d{2}/\d{2}/\d{2}\"\s*,,,\s*\"?([\d\.,\- ]+)\"?', text)
    saldo_final = clean_and_convert_value(saldo_final_match.group(1)) if saldo_final_match else 0.0

    
    # 2. Extracción de líneas de movimiento
    
    data = []
    # Usamos re.findall para encontrar todas las coincidencias del patrón
    # El patrón se ajusta para capturar las 5 primeras columnas de la tabla de movimientos
    # [FECHA, COMBTE, DESCRIPCION, DEBITO, CREDITO]
    # Usaremos el SALDO para calcular la conciliación (columna 6)

    # El formato del PDF es difícil de parsear por las comillas y saltos de línea.
    # Intentaremos una aproximación más robusta.

    # Buscamos el bloque de movimientos
    movements_block_match = re.search(
        r'\"FECHA\"\s*,\s*\"COMBTE\"\s*,\s*\"DESCRIPCION\"\s*,\s*\"DEBITO\"\s*,\s*\"CREDITO\"\s*,\s*\"SALDO\"(.*?)\"SALDO\"\s*,\s*\"AL\s+\d{2}/\d{2}/\d{2}\"', 
        text, 
        re.DOTALL
    )
    
    if not movements_block_match:
        return pd.DataFrame()

    movements_text = movements_block_match.group(1)
    
    # Intentamos parsear cada línea manualmente, buscando la fecha al inicio
    lines = movements_text.split('\n')
    
    # Inicializar la lista de movimientos y el saldo
    movements_list = []
    
    # La información de los movimientos se presenta en varias líneas dentro del texto,
    # por lo que el regex simple de línea no funciona bien.
    
    # Reconstruiremos los movimientos buscando las líneas que empiezan con una fecha
    current_movement = None
    
    for line in lines:
        line = line.strip().replace('\r', '')
        if not line:
            continue
            
        # Intentar matchear el inicio de un nuevo movimiento: "DD/MM/AA" , "COMBTE" , "DESCRIPCION"
        # Ajustamos el regex para capturar hasta la descripción larga, y luego los valores.
        match = re.match(r'^\s*\"(\d{2}\/\d{2}\/\d{2})\"\s*,\s*\"?([\d ]*)\"?\s*,\s*\"?([^\"]*)\"?\s*,\s*\"?([\d\., ]*)\"?\s*,\s*\"?([\d\., ]*)\"?\s*,\s*\"?([\d\., ]*)\"?' , line)

        if match:
            # Nuevo movimiento encontrado
            if current_movement:
                # Procesar el movimiento anterior antes de empezar uno nuevo (solo si hay saldo)
                if len(current_movement) == 6:
                    movements_list.append(current_movement)
            
            # Inicializar nuevo movimiento
            fecha, combte, descripcion, debito, credito, saldo = match.groups()
            current_movement = [fecha, combte.strip(), descripcion.strip(), debito, credito, saldo]
        else:
            # Línea de continuación (parte de la DESCRIPCION o valores de impuesto que no tienen fecha)
            # Para este formato específico de Credicoop, las líneas de continuación
            # contienen la continuación de la DESCRIPCION y, a veces, los valores de DEBITO/CREDITO/SALDO
            
            # Si hay un movimiento activo, intentamos agregar el texto a la descripción
            if current_movement and len(current_movement) >= 3:
                # Buscamos patrones de continuación de DESCRIPCION
                continuation_match = re.match(r'^\s*,\"\"?([^\"]*)\"?(?:,\s*\"?([\d\., ]*)\"?\s*,\s*\"?([\d\., ]*)\"?\s*,\s*\"?([\d\., ]*)\"?)?$', line)
                if continuation_match:
                    # Si es una línea de continuación de descripción o detalle, la añadimos
                    continuation_text = continuation_match.group(1).strip()
                    if continuation_text:
                        current_movement[2] += " | " + continuation_text
                    
                    # Además, si hay valores de débito/crédito/saldo en esta línea, los usamos para completar
                    new_debito = continuation_match.group(2)
                    new_credito = continuation_match.group(3)
                    new_saldo = continuation_match.group(4)
                    
                    # Sobrescribir si el valor no estaba presente o es cero
                    if new_debito and clean_and_convert_value(new_debito) > 0 and clean_and_convert_value(current_movement[3]) == 0:
                        current_movement[3] = new_debito
                    if new_credito and clean_and_convert_value(new_credito) > 0 and clean_and_convert_value(current_movement[4]) == 0:
                        current_movement[4] = new_credito
                    if new_saldo and clean_and_convert_value(new_saldo) > 0 and clean_and_convert_value(current_movement[5]) == 0:
                        current_movement[5] = new_saldo

    # Añadir el último movimiento si existe
    if current_movement and len(current_movement) == 6:
        movements_list.append(current_movement)

    if not movements_list:
        return pd.DataFrame()
        
    df = pd.DataFrame(movements_list, columns=["FECHA", "COMPROBANTE", "DESCRIPCION", "DEBITO", "CREDITO", "SALDO_PDF"])
    
    # 3. Limpieza y Conversión de Datos
    
    df['DEBITO'] = df['DEBITO'].apply(clean_and_convert_value)
    df['CREDITO'] = df['CREDITO'].apply(clean_and_convert_value)
    df['SALDO_PDF'] = df['SALDO_PDF'].apply(clean_and_convert_value)
    
    # 4. Cálculo del Saldo Operacional (para Conciliación)
    
    df['IMPORTE'] = df['CREDITO'] - df['DEBITO']
    df['FECHA'] = pd.to_datetime(df['FECHA'], format='%d/%m/%y', errors='coerce')
    
    # Calculamos el saldo acumulado (saldo contable)
    df['SALDO_CALCULADO'] = saldo_anterior + df['IMPORTE'].cumsum()
    
    # 5. Categorización
    df['CATEGORIA'] = df['DESCRIPCION'].apply(categorize_movement)
    
    # 6. Conciliación (diferencia entre el saldo del PDF y el saldo calculado)
    df['DIFERENCIA_CONCILIACION'] = df['SALDO_PDF'] - df['SALDO_CALCULADO']
    
    # 7. Reordenar y limpiar columnas
    df = df[['FECHA', 'COMPROBANTE', 'DESCRIPCION', 'DEBITO', 'CREDITO', 'IMPORTE', 'CATEGORIA', 'SALDO_CALCULADO', 'SALDO_PDF', 'DIFERENCIA_CONCILIACION']]
    
    # Añadir Saldo Anterior y Final como atributos del DF para el resumen
    df.saldo_anterior = saldo_anterior
    df.saldo_final = saldo_final
    
    return df

@st.cache_data
def load_and_process_file(uploaded_file):
    """Carga y procesa un archivo subido."""
    try:
        # Asumiendo que el archivo es un PDF y ya se extrajo el texto
        # El contenido ya viene como texto plano desde el content_fetcher
        if uploaded_file.name == 'CREDICOOP.pdf':
            # Si es el archivo subido en el prompt, usamos el contenido cacheado
            raw_text = st.session_state.credicoop_content
        else:
            # Para archivos nuevos (el usuario sube más en el futuro)
            # Necesitaríamos una librería de extracción de texto como pdfplumber/PyPDF2,
            # pero dado el entorno, solo podemos trabajar con el texto extraído
            # por el content_fetcher para el archivo inicial.
            # Aquí simulamos la lectura de texto si fuera posible:
            st.error("La extracción automática de texto de un nuevo PDF no es posible en este entorno. Por favor, trabaje con el archivo inicial subido (CREDICOOP.pdf).")
            return None

        df_movements = extract_movements_from_pdf_text(raw_text)
        
        # Filtrar movimientos con importe 0.0 que suelen ser líneas de texto sin valor.
        df_movements = df_movements[df_movements['IMPORTE'] != 0.0].copy()

        return df_movements
    
    except Exception as e:
        st.error(f"Error al procesar el archivo {uploaded_file.name}: {e}")
        return None

def display_summary(df):
    """Muestra el resumen general de la cuenta."""
    if df.empty:
        st.info("No hay movimientos para mostrar.")
        return

    st.subheader("Resumen General de la Cuenta")
    
    saldo_inicial = getattr(df, 'saldo_anterior', df['SALDO_CALCULADO'].iloc[0] - df['IMPORTE'].iloc[0] if not df.empty else 0.0)
    saldo_final_calc = df['SALDO_CALCULADO'].iloc[-1] if not df.empty else saldo_inicial
    saldo_final_pdf = getattr(df, 'saldo_final', df['SALDO_PDF'].iloc[-1] if not df.empty else 0.0)

    total_ingresos = df[df['IMPORTE'] > 0]['IMPORTE'].sum()
    total_egresos = df[df['IMPORTE'] < 0]['IMPORTE'].sum() * -1 # Mostrar como valor positivo

    col1, col2, col3, col4, col5 = st.columns(5)
    
    col1.metric("Saldo Inicial", f"${saldo_inicial:,.2f}")
    col2.metric("Total Ingresos (CRÉDITO)", f"${total_ingresos:,.2f}", delta=f"{total_ingresos/saldo_inicial * 100 if saldo_inicial else 0.0:.2f}%" if saldo_inicial > 0 else None)
    col3.metric("Total Egresos (DÉBITO)", f"-${total_egresos:,.2f}", delta=f"{-total_egresos/saldo_inicial * 100 if saldo_inicial else 0.0:.2f}%" if saldo_inicial > 0 else None)
    col4.metric("Saldo Calculado (Final)", f"${saldo_final_calc:,.2f}")
    
    # Usamos el saldo final del PDF para la métrica principal
    delta_conciliacion = saldo_final_pdf - saldo_final_calc
    col5.metric(
        "Saldo Resumen (PDF)", 
        f"${saldo_final_pdf:,.2f}",
        delta=f"Diferencia: ${delta_conciliacion:,.2f}" if abs(delta_conciliacion) > 0.01 else "Conciliado OK"
    )

def display_category_summary(df):
    """Muestra el resumen de ingresos y egresos por categoría."""
    st.subheader("Resumen de Movimientos por Categoría")

    # Separar Ingresos y Egresos
    ingresos_df = df[df['IMPORTE'] > 0].copy()
    egresos_df = df[df['IMPORTE'] < 0].copy()
    
    # Agrupar Ingresos
    ingresos_summary = ingresos_df.groupby('CATEGORIA')['IMPORTE'].sum().sort_values(ascending=False).reset_index()
    ingresos_summary.columns = ['Categoría', 'Total Ingreso']
    ingresos_summary['Porcentaje'] = (ingresos_summary['Total Ingreso'] / ingresos_summary['Total Ingreso'].sum() * 100).round(2)
    ingresos_summary['Total Ingreso'] = ingresos_summary['Total Ingreso'].apply(lambda x: f"${x:,.2f}")
    
    # Agrupar Egresos
    egresos_summary = egresos_df.groupby('CATEGORIA')['IMPORTE'].sum().sort_values(ascending=True).reset_index()
    egresos_summary.columns = ['Categoría', 'Total Egreso']
    egresos_summary['Total Egreso'] = egresos_summary['Total Egreso'] * -1 # Mostrar como valor positivo
    egresos_summary['Porcentaje'] = (egresos_summary['Total Egreso'] / egresos_summary['Total Egreso'].sum() * 100).round(2)
    egresos_summary['Total Egreso'] = egresos_summary['Total Egreso'].apply(lambda x: f"${x:,.2f}")


    col_ingresos, col_egresos = st.columns(2)
    
    with col_ingresos:
        st.markdown("##### Ingresos")
        st.dataframe(ingresos_summary, use_container_width=True, hide_index=True)
    
    with col_egresos:
        st.markdown("##### Egresos")
        st.dataframe(egresos_summary, use_container_width=True, hide_index=True)


def display_reconciliation(df):
    """Muestra la tabla de conciliación."""
    st.subheader("Detalle de Conciliación (PDF vs. Cálculo)")
    st.info("La conciliación verifica que el saldo final del resumen bancario coincida con el saldo calculado (Saldo Inicial + Movimientos). Una 'Diferencia de Conciliación' distinta a cero indica un posible error en el saldo inicial o en el parseo del movimiento.")
    
    reconciliation_df = df[abs(df['DIFERENCIA_CONCILIACION']) > 0.01].copy()

    if reconciliation_df.empty:
        st.success("🎉 ¡La Conciliación se ve correcta! El saldo calculado coincide con el saldo del PDF en cada paso.")
    else:
        st.warning(f"⚠️ Se encontraron {len(reconciliation_df)} movimientos con diferencia de conciliación.")
        
        # Mostrar solo las columnas relevantes para la conciliación
        st.dataframe(
            reconciliation_df[[
                'FECHA', 
                'DESCRIPCION', 
                'IMPORTE', 
                'SALDO_CALCULADO', 
                'SALDO_PDF', 
                'DIFERENCIA_CONCILIACION'
            ]].style.format(
                {'SALDO_CALCULADO': 'R$ {:,.2f}', 'SALDO_PDF': 'R$ {:,.2f}', 'DIFERENCIA_CONCILIACION': 'R$ {:,.2f}'}
            ),
            use_container_width=True
        )


# --- LÓGICA PRINCIPAL DE LA APP ---

# Contenido del archivo subido inicialmente
CREDICOOP_CONTENT = """--- PAGE 1 ---

BERGA FABRICIO ROLANDO

AV IRIONDO 2228

3040 SAN JUSTO

334 Mariano Cabal San Justo

Cuenta Corriente Mod. NyP 3

0582944345

R.N.P.S.P.
 Nro. 766

16/07/2025
 GRUPO:
 2
 SUCUR: SFE
 Distribuido por
 Urbano Express

PAGINA 001/004

0582944345-00000001 **

Mariano Cabal San Justo

Sucursal 334
 CUIT 30-57142135-2
 9 de Julio 2402
 San Justo

Resumen: 25006

del: 01/06/2025 al: 30/06/2025 Cta. 191.334.008471.6

Debito directo

CBU de su cuenta: 19103345 55033400847164


"FECHA
","COMBTE
","DESCRIPCION
","DEBITO
","CREDITO
","SALDO
"
"SALDO
","ANTERIOR
",,,
"4.216.032,04
"
"02/06/25


02/06/25


02/06/25
","262461
","Pago de Cheque de Camara
 Comision Cheque Pagado por Clearing
 I.V.A. Debito Fiscal 21%
","1.000.000,00


500,00


105,00
",,"4.216.032,04
"
"02/06/25


02/06/25
","011816


000001
","Debito Inmediato (DEBIN)
 30703088534-VAR-MERCADOLIBRE S.R.L.
 Debito/Credito Aut Segurcoop Comercio
 SEGUR.SOCIO INT.COM.-2529077220000001
","150.000,00


12.864,32
",,
"02/06/25
",,"Impuesto Ley 25.413 Alic Gral s/Debitos
","6.980,82
",,"3.045.581,90
"
"03/06/25
","902009
","Pago a Comercios Cabal
 CABAL-008703902009
",,"248.217,64
",
"03/06/25


03/06/25
","797986
","Transf.Inmediata e/Ctas.Igual Tit.O/Bco
 20228760057-VAR-BERGA FABRICIO ROLANDO
 Impuesto Ley 25.413 Ali Gral s/Creditos
","1.000.000,00


1.489,31
",,"2.292.310,23
"
"04/06/25
","470688
","Comision por Transferencia
 B. INTERNET COM. USO-000470688
","300,00
",,
"04/06/25
",,"I.V.A. Debito Fiscal 21%
","63,00
",,
"04/06/25


04/06/25


04/06/25
","902009


826131


439837
","Pago a Comercios Cabal
 CABAL-008703902009
 Transf. Inmediata e/Ctas.Dist Tit.0/Bco
 30708225300-FAC-C MS
 SA
 Transfer. e/Cuentas de Distinto Titular
 Cuit/1:30718296214-VAMAGRO SRL
","1.536.065,83
","85.325,78


100.950,06
",
"04/06/25
","006951
","ECHO Acreditac de Valores Camara
 Dep: 3349904265-SUPERVIELLE-Ch:00006951
",,"1.314.763,28
",
"04/06/25
",,"ECHO- Comis acred Camara con Filial Bco
","8.545,96
",,
"04/06/25
",,"Percepcion IVA RG 2408 s/Comis-Gastos
","256,38
",,
"04/06/25
",,"I.V.A. Debito Fiscal 21%
","1.794,65
",,
"04/06/25
",,"Impuesto Ley 25.413 Ali Gral s/Creditos
","9.006,23
",,
"04/06/25
",,"Impuesto Ley 25.413 Alic Gral s/Debitos
","9.282,16
",,"2.228.035,14
"
"05/06/25


05/06/25


05/06/25
","470688


657066
","Comision por Transferencia
 B. INTERNET COM. USO-000470688
 I.V.A. Debito Fiscal 21%
 Transf. Inmediata e/Ctas. Dist. Titular
 20166824193-VAR-SONZOGNI EDGARDO DANIE


","300,00


63,00
","106.000,00
",
"05/06/25


05/06/25
",,"Impuesto Ley 25.413 Ali Gral s/Creditos
 Impuesto Ley 25.413 Alic Gral s/Debitos
","636,00


2,18
",,"2.333.033,96
"
"06/06/25
","686908
","Transf. Inmediata e/Ctas.Dist Tit.O/Bco
 30710847742-ALQ-LOS NARANJOSSA
","890.293,44
",,
"06/06/25


06/06/25


09/06/25


09/06/25
","24253


470688,
","Transf.Inmediata e/Ctas. Dist Tit.0/Bco
 30711483523-FAC-CH SISTEMAS SRL
 Impuesto Ley 25.413 Alic Gral s/
 Comision por Transferencia
 B. INTERNET COM. USO-000470688
 I.V.A. Debito Fiscal 21%


Debitos
","22.000,00


5.473,76
 300,00


63,00
",,"1.415.266,76
"
"09/06/25
","902009
","Contrasiento Liquidacion PROPINAS CABAL
 CABAL-008703902009
","27.414,62
",,
"09/06/25
","902009
","Contrasiento Liquidacion PROPINAS CABAL
 CABAL-008703902009
","13.392,69
",,
"09/06/25
","902009
","Contrasiento Liquidacion PROPINAS CABAL
 CABAL-008703902009
","11.330,69
",,
"09/06/25
","902009
","Contrasiento Liquidacion PROPINAS CABAL
 CABAL-008703902009
","3.503,34
",,
"09/06/25
","902009
","Contrasiento Liquidacion PROPINAS CABAL
","22.859,97
",,

CONTINUA EN PAGINA SIGUIENTE >>>>>>

Banco Credicoop Cooperativo Limitado - Reconquista 484, C1003ABJ Buenos Aires, República Argentina
 Ctro. de Contacto Telefonico: cct@bancocredicoop.coop - Credicoop Responde: 0810-888-4500
 Calidad de Servicios: calidad@bancocredicoop.coop Sitio de Internet: www.bancocredicoop.coop


--- PAGE 2 ---

>>>>>> VIENE DE PAGINA ANTERIOR

PAGINA

002/004

Cuenta Corriente Mod. NyP 3

Resumen: 25006

del: 01/06/2025 al: 30/06/2025 Cta. 191.334.008471.6

Debito directo

CBU de su cuenta: 19103345 55033400847164


"FECHA
","COMBTE
","DESCRIPCION
","DEBITO
","CREDITO
","SALDO
"
,,"CABAL-008703902009
",,,
"09/06/25
","902009
","Contrasiento Liquidacion PROPINAS CABAL
 CABAL-008703902009
","9.268,69
",,
"09/06/25


10/06/25


10/06/25


11/06/25
","970571




470688
","Impuesto Ley 25.413 Alic Gral s/Debitos
 Transf. Inmediata e/Ctas. Dist Tit.O/Bco
 30573819256-CUO-PILAY SA
 Impuesto Ley 25.413 Alic Gral s/Debitos
 Comision por Transferencia
 B. INTERNET COM. USO-000470688




","2,18
 1.248.465,68


7.490,79


300,00
","

","1.327.131,58


71.175,11
"

"11/06/25
",,"I.V.A. Debito Fiscal 21%
","63,00
",,
"11/06/25
",,"Impuesto Ley 25.413 Alic Gral s/Debitos
","2,18
",,"70.809,93
"
"13/06/25
","902009
","Pago a Comercios Cabal
 CABAL-008703902009
",,"205.477,94
",
"13/06/25


17/06/25


17/06/25


17/06/25
","902009


231040


050723
","Impuesto Ley 25.413 Ali Gral s/Creditos


Pago a Comercios Cabal
 CABAL-008703902009
 Transf.Inmediata e/Ctas. Dist Tit.0/Bco
 23184950464-FAC-FILIPPA SILVIA GLADIS


Transf. Interbanking
 Distinto Titular
 Ord.:30685376349-TARJETA NARANJA SA
","1.232,87


14.500,00
","292.271,08


861.164,25
","275.055,00
"
"17/06/25
","115918
","Transf. Interbanking Distinto Titular
 Ord.:30707736107-VIVI TRANQUILO SA
",,"211.399,27
",
"17/06/25
",,"Impuesto Ley 25.413 Ali Gral s/Creditos
","8.189,02
",,
"17/06/25
",,"Impuesto Ley 25.413 Alic Gral s/Debitos
","87,00
",,"1.617.113,58
"
"18/06/25


18/06/25


18/06/25
","918075


223635


","Transf. Inmediata e/Ctas. Igual Tit.O/Bco
 20228760057-VAR-BERGA FABRICIO ROLANDO
 Transf. Inmediata e/Ctas. Dist Tit.0/Bco
 30708225300-FAC-C MS SA
 Impuesto Ley 25.413 Alic Gral s/Debitos
","900.000,00


253.976,32


1.523,86
","

","461.613,40
"

"19/06/25
","470688
","Comision por Transferencia
 B. INTERNET COM. USO-000470688
","350,00
",,
"19/06/25


19/06/25
",,"I.V.A. Debito Fiscal 21%
 Impuesto Ley 25.413 Alic Gral s/Debitos
","73,50


2,54
",,"461.187,36
"
"24/06/25


24/06/25


25/06/25
","902009


902009
","Pago a Comercios Cabal
 CABAL-008703902009
 Impuesto Ley 25.413 Ali Gral s/Creditos


Pago a Comercios Cabal
 CABAL-008703902009
","719,51
","119.917,81


430.776,79
","580.385,66
"
"25/06/25
",,"Impuesto Ley 25.413 Ali Gral s/Creditos
","2.584,66
",,"1.008.577,79
"
"26/06/25


26/06/25
","262458
","Pago de Cheque de Camara
 Comision Cheque Pagado por Clearing
","823.700,00


500,00
",,
"26/06/25
",,"I.V.A. Debito Fiscal 21%
","105,00
",,
"26/06/25
","902009
","Pago a Comercios Cabal
 CABAL-008703902009
",,"91.671,04
",
"26/06/25
",,,"550,03
",,
"26/06/25


27/06/25
","902009
","Impuesto Ley 25.413 Ali Gral s/Creditos
 Impuesto Ley 25.413 Alic Gral s/Debitos


Pago a Comercios Cabal
 CABAL-008703902009
","4.945,83
","61.062,79
","270.447,97
"
"27/06/25
","251788
","Servicio Modulo NyP
","37.500,00
",,
"27/06/25
",,"Percepcion IVA RG 2408 s/Comis-Gastos
","1.125,00
",,
"27/06/25
",,"I.V.A. Debito Fiscal 21%
","7.875,00
",,
"27/06/25
",,"Impuesto Ley 25.413 Ali Gral s/Creditos
","366,38
",,
"27/06/25
",,"Impuesto Ley 25.413 Alic Gral s/Debitos
","279,00
",,"284.365,38
"
,"SALDO
","AL 30/06/25
",,,"284.365,38
"
"""

if 'credicoop_content' not in st.session_state:
    st.session_state.credicoop_content = CREDICOOP_CONTENT


st.title("💰 Analizador de Resúmenes Bancarios")
st.markdown("Herramienta para procesar y analizar movimientos de resúmenes de Cuenta Corriente (Banco Credicoop) y realizar una conciliación básica.")

# Mostrar el archivo que se está usando
st.sidebar.markdown("### Archivos a Analizar")
st.sidebar.info(f"Usando el archivo cargado: **CREDICOOP.pdf** (Resumen 06/2025)")

# El usuario podría subir más archivos si la funcionalidad estuviera disponible,
# pero en este entorno solo podemos usar el texto que ya fue extraído.
# uploaded_files = st.sidebar.file_uploader(
#     "Subir más resúmenes bancarios (PDF)",
#     type=["pdf"],
#     accept_multiple_files=True,
#     disabled=True # Deshabilitado por la restricción de entorno
# )

# Procesar el archivo inicial
df_movements = load_and_process_file(pd.Series(data={'name': 'CREDICOOP.pdf'}))


if not df_movements.empty:
    
    # 1. Resumen por Categoría
    st.header("1. Resumen de Cuentas y Categorías")
    display_summary(df_movements)
    display_category_summary(df_movements)
    
    st.markdown("---")
    
    # 2. Detalle de Movimientos
    st.header("2. Detalle Completo de Movimientos")
    st.dataframe(
        df_movements.style.format({
            'IMPORTE': 'R$ {:,.2f}', 
            'DEBITO': 'R$ {:,.2f}', 
            'CREDITO': 'R$ {:,.2f}',
            'SALDO_CALCULADO': 'R$ {:,.2f}',
            'SALDO_PDF': 'R$ {:,.2f}',
            'DIFERENCIA_CONCILIACION': 'R$ {:,.2f}'
        }),
        use_container_width=True,
        hide_index=True
    )

    st.markdown("---")

    # 3. Conciliación
    st.header("3. Control de Conciliación")
    display_reconciliation(df_movements)

else:
    st.error("No se pudieron extraer los movimientos de la tabla del resumen bancario. Por favor, asegúrese de que el archivo sea un resumen de Banco Credicoop con el formato de movimientos estándar.")
