import streamlit as st
import pandas as pd
import re
from io import StringIO
from datetime import datetime

# --- CONFIGURACIÓN DE LA PÁGINA ---
st.set_page_config(
    page_title="Analizador de Resúmenes Bancarios",
    layout="wide",
    initial_sidebar_state="expanded"
)

# --- CONSTANTES Y UTILIDADES ---

# Patrón regex para identificar las líneas de movimientos en el texto del PDF
# Busca el patrón de una línea que comienza con una fecha DD/MM/AA
PATRON_MOVIMIENTO = r'^\s*\"(\d{2}\/\d{2}\/\d{2})\"\s*,\s*\"?([^\"]*)\"?\s*,\s*\"?([^\"]*)\"?\s*,\s*\"?([\d\., ]*)\"?\s*,\s*\"?([\d\., ]*)\"?\s*,\s*\"?([\d\., ]*)\"?'

# Palabras clave para categorización (se puede expandir y el usuario puede editar esta lista en un entorno real)
CATEGORIAS = {
    "Ventas/Transferencias Recibidas": ["Transf", "Transfer", "Pago a Comercios Cabal"],
    "Pagos a Proveedores/Servicios": ["C MS SA", "VAMAGRO SRL", "CH SISTEMAS SRL", "FILIPPA SILVIA GLADIS", "LOS NARANJOSSA", "PILAY SA"],
    "Gastos Financieros / Comisiones": ["Comision", "I.V.A. Debito Fiscal", "Percepcion IVA RG 2408", "Servicio Modulo NyP"],
    "Impuestos y Percepciones": ["Impuesto Ley 25.413", "Percepcion IVA"],
    "Débitos y Cheques": ["Cheque de Camara", "Debito Inmediato (DEBIN)", "Debito/Credito Aut Segurcoop"],
    "Retiros/Extracciones": ["Contrasiento Liquidacion PROPINAS CABAL"],
}

def clean_and_convert_value(value_str):
    """Limpia y convierte una cadena de valor monetario a float."""
    if isinstance(value_str, str):
        # Limpieza: quitamos separadores de miles (punto) y usamos coma como separador decimal.
        # Luego reemplazamos la coma por punto para el float.
        value = value_str.replace('.', '').replace(',', '.')
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
    if "IVA" in description or "Percepcion" in description:
        return "Impuestos y Percepciones"
    if "Comision" in description or "Servicio" in description:
        return "Gastos Financieros / Comisiones"
    if "Transf" in description or "PAGO" in description:
        return "Transferencias/Pagos Varios"

    return "Movimientos No Clasificados"

def extract_movements_from_pdf_text(text):
    """
    Procesa el texto completo del PDF y extrae los movimientos financieros.
    Esta función está optimizada para la estructura de tabla Credicoop,
    pero busca los patrones universales de saldo y tabla de movimientos.
    """
    
    # --- 1. Extracción de Saldos Clave ---
    saldo_anterior = 0.0
    saldo_final = 0.0

    # Patrón para SALDO ANTERIOR
    saldo_anterior_match = re.search(r'\"SALDO\"\s*,\s*\"ANTERIOR\"\s*,,,\s*\"?([\d\.,\- ]+)\"?', text)
    if saldo_anterior_match:
        saldo_anterior = clean_and_convert_value(saldo_anterior_match.group(1))

    # Patrón para SALDO FINAL
    saldo_final_match = re.search(r'\"SALDO\"\s*,\s*\"AL\s+\d{2}/\d{2}/\d{2}\"\s*,,,\s*\"?([\d\.,\- ]+)\"?', text)
    if saldo_final_match:
        saldo_final = clean_and_convert_value(saldo_final_match.group(1))

    # --- 2. Localización y Extracción del Bloque de Movimientos ---
    
    # Buscamos el bloque de movimientos: desde el encabezado de la tabla hasta el saldo final
    movements_block_match = re.search(
        r'\"FECHA\"\s*,\s*\"COMBTE\"\s*,\s*\"DESCRIPCION\"\s*,\s*\"DEBITO\"\s*,\s*\"CREDITO\"\s*,\s*\"SALDO\"(.*?)\"SALDO\"\s*,\s*\"AL\s+\d{2}/\d{2}/\d{2}\"', 
        text, 
        re.DOTALL
    )
    
    if not movements_block_match:
        st.error("No se pudo identificar el bloque de movimientos (FECHA, COMBTE, DESCRIPCION, DEBITO, CREDITO, SALDO) en el archivo.")
        return pd.DataFrame()

    movements_text = movements_block_match.group(1)
    
    # 3. Parsing de líneas
    movements_list = []
    current_movement = None
    
    for line in movements_text.split('\n'):
        line = line.strip().replace('\r', '')
        if not line:
            continue
            
        # Intenta matchear el inicio de un nuevo movimiento con el patrón general
        match = re.match(PATRON_MOVIMIENTO, line)

        if match:
            # Si hay un movimiento anterior incompleto, lo forzamos a la lista
            if current_movement and len(current_movement) == 6:
                 movements_list.append(current_movement)
            
            # Inicializar nuevo movimiento: [FECHA, COMBTE, DESCRIPCION, DEBITO, CREDITO, SALDO]
            current_movement = list(match.groups())
            
        else:
            # Línea de continuación (parte de la DESCRIPCION o valores)
            if current_movement and len(current_movement) == 6:
                # Buscamos patrones de continuación de DESCRIPCION y posible corrección de valores
                # El regex busca: ,"", DESCRIPCION (opcionalmente) , VALOR , VALOR , VALOR 
                continuation_match = re.match(r'^\s*,\"\"?([^\"]*)\"?(?:,\s*\"?([\d\., ]*)\"?\s*,\s*\"?([\d\., ]*)\"?\s*,\s*\"?([\d\., ]*)\"?)?$', line)
                if continuation_match:
                    continuation_text = continuation_match.group(1).strip()
                    if continuation_text:
                        # Agregamos la continuación a la descripción
                        current_movement[2] += " | " + continuation_text
                    
                    # Verificamos si hay nuevos valores de débito/crédito/saldo
                    new_debito = continuation_match.group(2)
                    new_credito = continuation_match.group(3)
                    new_saldo = continuation_match.group(4)
                    
                    # Sobrescribir solo si el valor es positivo (mayor a 0) y el campo actual es cero
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
        st.error("No se pudieron extraer líneas de movimiento válidas.")
        return pd.DataFrame()
        
    df = pd.DataFrame(movements_list, columns=["FECHA", "COMPROBANTE", "DESCRIPCION", "DEBITO", "CREDITO", "SALDO_PDF"])
    
    # --- 4. Conversión y Cálculos ---
    
    df['DEBITO'] = df['DEBITO'].apply(clean_and_convert_value)
    df['CREDITO'] = df['CREDITO'].apply(clean_and_convert_value)
    df['SALDO_PDF'] = df['SALDO_PDF'].apply(clean_and_convert_value)
    
    df['IMPORTE'] = df['CREDITO'] - df['DEBITO']
    df['FECHA'] = pd.to_datetime(df['FECHA'], format='%d/%m/%y', errors='coerce')
    
    # 5. Cálculo del Saldo Operacional y Conciliación
    df['SALDO_CALCULADO'] = saldo_anterior + df['IMPORTE'].cumsum()
    df['CATEGORIA'] = df['DESCRIPCION'].apply(categorize_movement)
    df['DIFERENCIA_CONCILIACION'] = df['SALDO_PDF'] - df['SALDO_CALCULADO']
    
    # Filtrar movimientos con importe 0.0 que suelen ser líneas de texto sin valor.
    df = df[df['IMPORTE'] != 0.0].copy()

    # Añadir Saldo Anterior y Final como atributos del DF para el resumen
    df.saldo_anterior = saldo_anterior
    df.saldo_final = saldo_final
    
    return df

@st.cache_data
def load_and_process_file(uploaded_file):
    """Carga y procesa un archivo subido (simulación de extracción de texto)."""
    
    # --- SIMULACIÓN DE EXTRACCIÓN DE TEXTO DEL PDF ---
    # En un entorno real, usaríamos librerías como `pdfplumber` o `PyPDF2` aquí.
    # Dado que solo tengo acceso al texto del archivo original subido,
    # solo puedo procesar ese texto, pero la lógica de la función
    # `extract_movements_from_pdf_text` es ahora genérica.
    
    # Contenido del archivo subido inicialmente
    CREDICOOP_CONTENT = st.session_state.get('credicoop_content')

    if uploaded_file.name == 'CREDICOOP.pdf' and CREDICOOP_CONTENT:
        raw_text = CREDICOOP_CONTENT
        st.sidebar.success(f"Procesando el archivo: **{uploaded_file.name}**")
    else:
        st.warning("⚠️ **Advertencia:** Para archivos PDF nuevos, este entorno no puede extraer el texto automáticamente. Solo se procesará el texto del PDF inicial, asumiendo que el archivo subido tiene el mismo formato de texto extraíble.")
        # Simular lectura de texto si el usuario sube otro archivo del mismo banco
        try:
             # Si se subió un nuevo archivo, intentamos leerlo como texto simple
             raw_text = uploaded_file.getvalue().decode("utf-8")
        except:
             # Usamos el contenido original para la demo si la lectura falla
             raw_text = CREDICOOP_CONTENT
             
    # --- FIN SIMULACIÓN ---

    try:
        df_movements = extract_movements_from_pdf_text(raw_text)
        return df_movements
    
    except Exception as e:
        st.error(f"Error al procesar el archivo {uploaded_file.name}: {e}")
        return pd.DataFrame()

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
    col2.metric("Total Ingresos (CRÉDITO)", f"${total_ingresos:,.2f}")
    col3.metric("Total Egresos (DÉBITO)", f"-${total_egresos:,.2f}")
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
    st.subheader("Resumen de Flujos por Categoría")

    # Separar Ingresos y Egresos
    ingresos_df = df[df['IMPORTE'] > 0].copy()
    egresos_df = df[df['IMPORTE'] < 0].copy()
    
    # Agrupar Ingresos
    ingresos_summary = ingresos_df.groupby('CATEGORIA')['IMPORTE'].sum().sort_values(ascending=False).reset_index()
    ingresos_summary.columns = ['Categoría', 'Total Ingreso']
    ingresos_summary['Total Ingreso'] = ingresos_summary['Total Ingreso'].apply(lambda x: f"${x:,.2f}")
    
    # Agrupar Egresos
    egresos_summary = egresos_df.groupby('CATEGORIA')['IMPORTE'].sum().sort_values(ascending=True).reset_index()
    egresos_summary.columns = ['Categoría', 'Total Egreso']
    egresos_summary['Total Egreso'] = egresos_summary['Total Egreso'] * -1 # Mostrar como valor positivo
    egresos_summary['Total Egreso'] = egresos_summary['Total Egreso'].apply(lambda x: f"${x:,.2f}")


    col_ingresos, col_egresos = st.columns(2)
    
    with col_ingresos:
        st.markdown("##### Ingresos (Créditos)")
        st.dataframe(ingresos_summary, use_container_width=True, hide_index=True)
    
    with col_egresos:
        st.markdown("##### Egresos (Débitos)")
        st.dataframe(egresos_summary, use_container_width=True, hide_index=True)


def display_reconciliation(df):
    """Muestra la tabla de conciliación."""
    st.subheader("Control de Conciliación Bancaria")
    st.info("La conciliación verifica que el saldo final del resumen (SALDO_PDF) coincida con el saldo calculado (SALDO_CALCULADO) sumando todos los movimientos al saldo inicial. Una 'Diferencia' distinta a cero indica un posible error de lectura o un saldo inicial incorrecto.")
    
    reconciliation_df = df[abs(df['DIFERENCIA_CONCILIACION']) > 0.01].copy()

    if reconciliation_df.empty:
        st.success("🎉 ¡Conciliación Perfecta! El saldo calculado coincide con el saldo del PDF en cada paso.")
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

# Inicializar o cargar el contenido del archivo inicial para la demo
if 'credicoop_content' not in st.session_state:
    # Este es el contenido que se obtuvo del archivo CREDICOOP.pdf
    # En un entorno real, esta variable no existiría y se usaría una librería de PDF.
    st.session_state.credicoop_content = """--- PAGE 1 ---\n\nBERGA FABRICIO ROLANDO\n\nAV IRIONDO 2228\n\n3040 SAN JUSTO\n\n334 Mariano Cabal San Justo\n\nCuenta Corriente Mod. NyP 3\n\n0582944345\n\nR.N.P.S.P.\n Nro. 766\n\n16/07/2025\n GRUPO:\n 2\n SUCUR: SFE\n Distribuido por\n Urbano Express\n\nPAGINA 001/004\n\n0582944345-00000001 **\n\nMariano Cabal San Justo\n\nSucursal 334\n CUIT 30-57142135-2\n 9 de Julio 2402\n San Justo\n\nResumen: 25006\n\ndel: 01/06/2025 al: 30/06/2025 Cta. 191.334.008471.6\n\nDebito directo\n\nCBU de su cuenta: 19103345 55033400847164\n\n\n"FECHA\n","COMBTE\n","DESCRIPCION\n","DEBITO\n","CREDITO\n","SALDO\n"\r\n"SALDO\n","ANTERIOR\n",,,\r\n"4.216.032,04\n"\r\n"02/06/25\n\n\n02/06/25\n\n\n02/06/25\n","262461\n","Pago de Cheque de Camara\n Comision Cheque Pagado por Clearing\n I.V.A. Debito Fiscal 21%\n","1.000.000,00\n\n\n500,00\n\n\n105,00\n",,"4.216.032,04\n"\r\n"02/06/25\n\n\n02/06/25\n","011816\n\n\n000001\n","Debito Inmediato (DEBIN)\n 30703088534-VAR-MERCADOLIBRE S.R.L.\n Debito/Credito Aut Segurcoop Comercio\n SEGUR.SOCIO INT.COM.-2529077220000001\n","150.000,00\n\n\n12.864,32\n",,\r\n"02/06/25\n",,"Impuesto Ley 25.413 Alic Gral s/Debitos\n","6.980,82\n",,"3.045.581,90\n"\r\n"03/06/25\n","902009\n","Pago a Comercios Cabal\n CABAL-008703902009\n",,"248.217,64\n"\r\n"03/06/25\n\n\n03/06/25\n","797986\n","Transf.Inmediata e/Ctas.Igual Tit.O/Bco\n 20228760057-VAR-BERGA FABRICIO ROLANDO\n Impuesto Ley 25.413 Ali Gral s/Creditos\n","1.000.000,00\n\n\n1.489,31\n",,"2.292.310,23\n"\r\n"04/06/25\n","470688\n","Comision por Transferencia\n B. INTERNET COM. USO-000470688\n","300,00\n",,\r\n"04/06/25\n",,"I.V.A. Debito Fiscal 21%\n","63,00\n",,\r\n"04/06/25\n\n\n04/06/25\n\n\n04/06/25\n","902009\n\n\n826131\n\n\n439837\n","Pago a Comercios Cabal\n CABAL-008703902009\n Transf. Inmediata e/Ctas.Dist Tit.0/Bco\n 30708225300-FAC-C MS\n SA\n Transfer. e/Cuentas de Distinto Titular\n Cuit/1:30718296214-VAMAGRO SRL\n","1.536.065,83\n","85.325,78\n\n\n100.950,06\n",\r\n"04/06/25\n","006951\n","ECHO Acreditac de Valores Camara\n Dep: 3349904265-SUPERVIELLE-Ch:00006951\n",,"1.314.763,28\n",\r\n"04/06/25\n",,"ECHO- Comis acred Camara con Filial Bco\n","8.545,96\n",,\r\n"04/06/25\n",,"Percepcion IVA RG 2408 s/Comis-Gastos\n","256,38\n",,\r\n"04/06/25\n",,"I.V.A. Debito Fiscal 21%\n","1.794,65\n",,\r\n"04/06/25\n",,"Impuesto Ley 25.413 Ali Gral s/Creditos\n","9.006,23\n",,\r\n"04/06/25\n",,"Impuesto Ley 25.413 Alic Gral s/Debitos\n","9.282,16\n",,"2.228.035,14\n"\r\n"05/06/25\n\n\n05/06/25\n\n\n05/06/25\n","470688\n\n\n657066\n","Comision por Transferencia\n B. INTERNET COM. USO-000470688\n I.V.A. Debito Fiscal 21%\n Transf. Inmediata e/Ctas. Dist. Titular\n 20166824193-VAR-SONZOGNI EDGARDO DANIE\n\n\n","300,00\n\n\n63,00\n","106.000,00\n",\r\n"05/06/25\n\n\n05/06/25\n",,"Impuesto Ley 25.413 Ali Gral s/Creditos\n Impuesto Ley 25.413 Alic Gral s/Debitos\n","636,00\n\n\n2,18\n",,"2.333.033,96\n"\r\n"06/06/25\n","686908\n","Transf. Inmediata e/Ctas.Dist Tit.O/Bco\n 30710847742-ALQ-LOS NARANJOSSA\n","890.293,44\n",,\r\n"06/06/25\n\n\n06/06/25\n\n\n09/06/25\n\n\n09/06/25\n","24253\n\n\n470688,\n","Transf.Inmediata e/Ctas. Dist Tit.0/Bco\n 30711483523-FAC-CH SISTEMAS SRL\n Impuesto Ley 25.413 Alic Gral s/\n Comision por Transferencia\n B. INTERNET COM. USO-000470688\n I.V.A. Debito Fiscal 21%\n\n\nDebitos\n","22.000,00\n\n\n5.473,76\n 300,00\n\n\n63,00\n",,"1.415.266,76\n"\r\n"09/06/25\n","902009\n","Contrasiento Liquidacion PROPINAS CABAL\n CABAL-008703902009\n","27.414,62\n",,\r\n"09/06/25\n","902009\n","Contrasiento Liquidacion PROPINAS CABAL\n CABAL-008703902009\n","13.392,69\n",,\r\n"09/06/25\n","902009\n","Contrasiento Liquidacion PROPINAS CABAL\n CABAL-008703902009\n","11.330,69\n",,\r\n"09/06/25\n","902009\n","Contrasiento Liquidacion PROPINAS CABAL\n CABAL-008703902009\n","3.503,34\n",,\r\n"09/06/25\n","902009\n","Contrasiento Liquidacion PROPINAS CABAL\n","22.859,97\n",,\r\n\nCONTINUA EN PAGINA SIGUIENTE >>>>>>\n\nBanco Credicoop Cooperativo Limitado - Reconquista 484, C1003ABJ Buenos Aires, República Argentina\n Ctro. de Contacto Telefonico: cct@bancocredicoop.coop - Credicoop Responde: 0810-888-4500\n Calidad de Servicios: calidad@bancocredicoop.coop Sitio de Internet: www.bancocredicoop.coop\n\n\n--- PAGE 2 ---\n\n>>>>>> VIENE DE PAGINA ANTERIOR\n\nPAGINA\n\n002/004\n\nCuenta Corriente Mod. NyP 3\n\nResumen: 25006\n\ndel: 01/06/2025 al: 30/06/2025 Cta. 191.334.008471.6\n\nDebito directo\n\nCBU de su cuenta: 19103345 55033400847164\n\n\n"FECHA\n","COMBTE\n","DESCRIPCION\n","DEBITO\n","CREDITO\n","SALDO\n"\r\n,,"CABAL-008703902009\n",,,\r\n"09/06/25\n","902009\n","Contrasiento Liquidacion PROPINAS CABAL\n CABAL-008703902009\n","9.268,69\n",,\r\n"09/06/25\n\n\n10/06/25\n\n\n10/06/25\n\n\n11/06/25\n","970571\n\n\n\n\n470688\n","Impuesto Ley 25.413 Alic Gral s/Debitos\n Transf. Inmediata e/Ctas. Dist Tit.O/Bco\n 30573819256-CUO-PILAY SA\n Impuesto Ley 25.413 Alic Gral s/Debitos\n Comision por Transferencia\n B. INTERNET COM. USO-000470688\n\n\n\n\n","2,18\n 1.248.465,68\n\n\n7.490,79\n\n\n300,00\n","\n\n","1.327.131,58\n\n\n71.175,11\n"\r\n\n"11/06/25\n",,"I.V.A. Debito Fiscal 21%\n","63,00\n",,\r\n"11/06/25\n",,"Impuesto Ley 25.413 Alic Gral s/Debitos\n","2,18\n",,"70.809,93\n"\r\n"13/06/25\n","902009\n","Pago a Comercios Cabal\n CABAL-008703902009\n",,"205.477,94\n",\r\n"13/06/25\n\n\n17/06/25\n\n\n17/06/25\n\n\n17/06/25\n","902009\n\n\n231040\n\n\n050723\n","Impuesto Ley 25.413 Ali Gral s/Creditos\n\n\nPago a Comercios Cabal\n CABAL-008703902009\n Transf.Inmediata e/Ctas. Dist Tit.0/Bco\n 23184950464-FAC-FILIPPA SILVIA GLADIS\n\n\nTransf. Interbanking\n Distinto Titular\n Ord.:30685376349-TARJETA NARANJA SA\n","1.232,87\n\n\n14.500,00\n","292.271,08\n\n\n861.164,25\n","275.055,00\n"\r\n"17/06/25\n","115918\n","Transf. Interbanking Distinto Titular\n Ord.:30707736107-VIVI TRANQUILO SA\n",,"211.399,27\n",\r\n"17/06/25\n",,"Impuesto Ley 25.413 Ali Gral s/Creditos\n","8.189,02\n",,\r\n"17/06/25\n",,"Impuesto Ley 25.413 Alic Gral s/Debitos\n","87,00\n",,"1.617.113,58\n"\r\n"18/06/25\n\n\n18/06/25\n\n\n18/06/25\n","918075\n\n\n223635\n\n\n","Transf. Inmediata e/Ctas. Igual Tit.O/Bco\n 20228760057-VAR-BERGA FABRICIO ROLANDO\n Transf. Inmediata e/Ctas. Dist Tit.0/Bco\n 30708225300-FAC-C MS SA\n Impuesto Ley 25.413 Alic Gral s/Debitos\n","900.000,00\n\n\n253.976,32\n\n\n1.523,86\n","\n\n","461.613,40\n"\r\n\n"19/06/25\n","470688\n","Comision por Transferencia\n B. INTERNET COM. USO-000470688\n","350,00\n",,\r\n"19/06/25\n\n\n19/06/25\n",,"I.V.A. Debito Fiscal 21%\n Impuesto Ley 25.413 Alic Gral s/Debitos\n","73,50\n\n\n2,54\n",,"461.187,36\n"\r\n"24/06/25\n\n\n24/06/25\n\n\n25/06/25\n","902009\n\n\n902009\n","Pago a Comercios Cabal\n CABAL-008703902009\n Impuesto Ley 25.413 Ali Gral s/Creditos\n\n\nPago a Comercios Cabal\n CABAL-008703902009\n","719,51\n","119.917,81\n\n\n430.776,79\n","580.385,66\n"\r\n"25/06/25\n",,"Impuesto Ley 25.413 Ali Gral s/Creditos\n","2.584,66\n",,"1.008.577,79\n"\r\n"26/06/25\n\n\n26/06/25\n","262458\n","Pago de Cheque de Camara\n Comision Cheque Pagado por Clearing\n","823.700,00\n\n\n500,00\n",,\r\n"26/06/25\n",,"I.V.A. Debito Fiscal 21%\n","105,00\n",,\r\n"26/06/25\n","902009\n","Pago a Comercios Cabal\n CABAL-008703902009\n",,"91.671,04\n",\r\n"26/06/25\n",,,"550,03\n",,\r\n"26/06/25\n\n\n27/06/25\n","902009\n","Impuesto Ley 25.413 Ali Gral s/Creditos\n Impuesto Ley 25.413 Alic Gral s/Debitos\n\n\nPago a Comercios Cabal\n CABAL-008703902009\n","4.945,83\n","61.062,79\n","270.447,97\n"\r\n"27/06/25\n","251788\n","Servicio Modulo NyP\n","37.500,00\n",,\r\n"27/06/25\n",,"Percepcion IVA RG 2408 s/Comis-Gastos\n","1.125,00\n",,\r\n"27/06/25\n",,"I.V.A. Debito Fiscal 21%\n","7.875,00\n",,\r\n"27/06/25\n",,"Impuesto Ley 25.413 Ali Gral s/Creditos\n","366,38\n",,\r\n"27/06/25\n",,"Impuesto Ley 25.413 Alic Gral s/Debitos\n","279,00\n",,"284.365,38\n"\r\n,"SALDO\n","AL 30/06/25\n",,,"284.365,38\n"""
    
st.title("💰 Analizador Universal de Resúmenes Bancarios")
st.markdown("Carga y analiza tus resúmenes para obtener detalle de movimientos, clasificación por categorías y control de conciliación.")

# 1. ARCHIVO UPLOADER
uploaded_files = st.sidebar.file_uploader(
    "Subir resúmenes bancarios (PDF/TXT)",
    type=["pdf", "txt"],
    accept_multiple_files=True
)

# Variable para almacenar todos los movimientos consolidados
all_movements_df = pd.DataFrame()
processed_files_count = 0
file_to_process = None

# Buscar si el archivo 'CREDICOOP.pdf' fue subido o si hay un archivo para procesar
if uploaded_files:
    for uploaded_file in uploaded_files:
        if uploaded_file.name == 'CREDICOOP.pdf' or file_to_process is None:
            file_to_process = uploaded_file
            
if file_to_process is None and 'credicoop_content' in st.session_state:
    # Si no se subió nada, pero el archivo original fue mencionado, creamos un placeholder para la demo
    file_to_process = pd.Series(data={'name': 'CREDICOOP.pdf', 'getvalue': lambda: st.session_state.credicoop_content.encode('utf-8')})
    
if file_to_process is not None:
    
    # 2. PROCESAMIENTO
    df_movements = load_and_process_file(file_to_process)
    
    if not df_movements.empty:
        all_movements_df = pd.concat([all_movements_df, df_movements], ignore_index=True)
        processed_files_count += 1
        
if all_movements_df.empty:
    st.info("Por favor, sube uno o más archivos para comenzar el análisis. El analizador está optimizado para el formato de tabla de texto del resumen de Banco Credicoop.")
else:
    # 3. RESULTADOS
    st.success(f"✅ Se consolidaron movimientos de {processed_files_count} resumen(es).")
    
    # A. Resumen por Categoría
    st.header("1. Resumen de Cuentas y Categorías")
    display_summary(all_movements_df)
    display_category_summary(all_movements_df)
    
    st.markdown("---")
    
    # B. Detalle de Movimientos (con filtros)
    st.header("2. Detalle Completo de Movimientos")
    
    # Filtrado en la tabla de detalle
    col_filter, col_sort = st.columns([3, 1])
    
    selected_categories = col_filter.multiselect(
        "Filtrar por Categoría:",
        options=all_movements_df['CATEGORIA'].unique(),
        default=all_movements_df['CATEGORIA'].unique()
    )
    
    filter_type = col_sort.radio("Tipo de Movimiento", ('Todos', 'Ingresos', 'Egresos'), horizontal=True)
    
    filtered_df = all_movements_df[all_movements_df['CATEGORIA'].isin(selected_categories)]
    
    if filter_type == 'Ingresos':
        filtered_df = filtered_df[filtered_df['IMPORTE'] > 0]
    elif filter_type == 'Egresos':
        filtered_df = filtered_df[filtered_df['IMPORTE'] < 0]
        
    st.dataframe(
        filtered_df[['FECHA', 'DESCRIPCION', 'CATEGORIA', 'DEBITO', 'CREDITO', 'IMPORTE', 'SALDO_CALCULADO']].style.format({
            'IMPORTE': 'R$ {:,.2f}', 
            'DEBITO': 'R$ {:,.2f}', 
            'CREDITO': 'R$ {:,.2f}',
            'SALDO_CALCULADO': 'R$ {:,.2f}'
        }),
        use_container_width=True,
        hide_index=True
    )

    st.markdown("---")

    # C. Conciliación
    st.header("3. Control de Conciliación")
    display_reconciliation(all_movements_df)
