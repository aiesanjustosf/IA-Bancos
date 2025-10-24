import streamlit as st
import pandas as pd
import pdfplumber
import re
from io import BytesIO

# --- Configuración de la Página ---
st.set_page_config(
    page_title="Extractor y Conciliador Bancario",
    layout="wide",
    initial_sidebar_state="expanded"
)

# --- Funciones de Utilidad ---

def clean_and_parse_amount(text):
    """
    Limpia una cadena de texto y la convierte a un número flotante.
    Maneja el formato europeo/argentino (punto como separador de miles, coma como decimal).
    """
    if not isinstance(text, str):
        return 0.0
    
    # 1. Eliminar espacios y símbolos no numéricos (excepto punto y coma)
    cleaned_text = text.strip().replace('$', '').replace('.', '').replace(' ', '')
    
    # 2. Reemplazar la coma decimal por punto decimal
    if ',' in cleaned_text:
        cleaned_text = cleaned_text.replace(',', '.')
    
    try:
        # 3. Intentar convertir a float
        return float(cleaned_text)
    except ValueError:
        return 0.0

def format_currency(amount):
    """Formatea un número como moneda ARS."""
    if amount is None:
        return "$ 0,00"
    return f"$ {amount:,.2f}".replace('.', 'X').replace(',', '.').replace('X', ',')


# --- Lógica Principal de Extracción del PDF ---

@st.cache_data
def process_bank_pdf(file_bytes):
    """
    Extrae, limpia y concilia los movimientos de un extracto bancario Credicoop.
    Retorna el DataFrame de movimientos y el diccionario de saldos de conciliación.
    """
    
    # Inicialización de variables
    extracted_data = []
    saldo_anterior = 0.0
    saldo_informado = 0.0
    
    # Patrones para encontrar saldos y totales específicos en el texto (Credicoop N&P)
    # Busca el patrón de número con separadores (ej: 1.234.567,89 o 1.234,56)
    currency_pattern = r"(\d{1,3}(?:\.\d{3})*,\d{2})"
    
    # Patrones de búsqueda de texto clave
    patron_saldo_anterior = r"(?:SALDO\s*ANTERIOR)(?:\s+PAGINA\s+SIGUIENTE)?\s*(-?" + currency_pattern + r")"
    patron_saldo_al = r"SALDO AL\s*\d{2}/\d{2}/\d{4}\s*(-?" + currency_pattern + r")"
    patron_total_debito = r"TOTAL DEBITOS\s*" + currency_pattern
    patron_total_credito = r"TOTAL CREDITOS\s*" + currency_pattern
    
    
    with pdfplumber.open(BytesIO(file_bytes)) as pdf:
        full_text = ""
        
        # 1. Extraer todo el texto para buscar saldos (más fiable que las tablas)
        for page in pdf.pages:
            full_text += page.extract_text() + "\n"
        
        # 2. Intento de extracción de Saldos del texto completo
        
        # Saldo Anterior (a veces viene después de SALDO ANTERIOR)
        match_sa = re.search(r"SALDO ANTERIOR\s*(-?" + currency_pattern + r")", full_text, re.DOTALL | re.IGNORECASE)
        if match_sa:
             # El grupo 1 es el valor capturado. Puede ser ARS 352.167,18
             saldo_anterior = clean_and_parse_amount(match_sa.group(1).replace('ARS', ''))
        
        # Saldo Final (Busca SALDO AL DD/MM/AAAA)
        match_sf = re.search(r"SALDO AL\s*\d{2}/\d{2}/\d{4}\s*(-?" + currency_pattern + r")", full_text, re.DOTALL | re.IGNORECASE)
        if match_sf:
            saldo_informado = clean_and_parse_amount(match_sf.group(1))
        
        # Si no se encuentra con el patrón específico, intentar una búsqueda genérica
        if saldo_informado == 0.0:
            # Buscar el último saldo en la parte inferior de la última página (un último intento)
            last_page_text = pdf.pages[-1].extract_text()
            match_last_saldo = re.findall(currency_pattern, last_page_text)[-1:]
            if match_last_saldo:
                saldo_informado = clean_and_parse_amount(match_last_saldo[0])


        # 3. Extraer Movimientos Usando Tablas
        
        # Definición de la estructura de la tabla (ajustada al PDF de Credicoop)
        # FECHA | COMBTE | DESCRIPCION | DEBITO | CREDITO | SALDO
        table_settings = {
            "vertical_strategy": "explicit",
            "horizontal_strategy": "lines",
            "explicit_vertical_lines": [30, 80, 160, 440, 520, 600, 720], # Coordenadas aproximadas
            "snap_tolerance": 3
        }
        
        for page in pdf.pages:
            # Buscar tablas en la página
            tables = page.extract_tables(table_settings)
            
            for table in tables:
                for row in table:
                    # Una fila de movimiento debería tener al menos 6 columnas
                    if len(row) >= 6:
                        # La primera columna debe ser la fecha (DD/MM/YY)
                        fecha = row[0]
                        if re.match(r"\d{2}/\d{2}/\d{2}", str(fecha).strip()):
                            # Es una fila de movimiento
                            mov = {
                                'fecha': str(row[0]).strip(),
                                'comprobante': str(row[1]).strip(),
                                'descripcion': str(row[2]).strip(),
                                'debito_raw': str(row[3]).strip(),
                                'credito_raw': str(row[4]).strip(),
                                'saldo_raw': str(row[5]).strip()
                            }
                            
                            # Limpieza y parsing de valores
                            debito = clean_and_parse_amount(mov['debito_raw'])
                            credito = clean_and_parse_amount(mov['credito_raw'])
                            
                            # Asegurar que el débito o el crédito sean 0.0 si la columna tiene texto 'VACIO'
                            if 'VACIO' in mov['debito_raw'].upper():
                                debito = 0.0
                            if 'VACIO' in mov['credito_raw'].upper():
                                credito = 0.0

                            extracted_data.append({
                                'Fecha': mov['fecha'],
                                'Comprobante': mov['comprobante'],
                                'Descripcion': mov['descripcion'],
                                'Débito': debito,
                                'Crédito': credito,
                                'Saldo_Final_Linea': clean_and_parse_amount(mov['saldo_raw'])
                            })
                            
    if not extracted_data:
        st.warning("⚠️ No se pudieron extraer movimientos tabulares. Intenta con un PDF con mejor calidad.")
        return pd.DataFrame(), {}
        
    # Crear DataFrame
    df = pd.DataFrame(extracted_data)
    
    # 4. Conciliación y Cálculos Finales
    
    # Totales calculados
    total_debitos_calc = df['Débito'].sum()
    total_creditos_calc = df['Crédito'].sum()
    
    # Saldo calculado (Saldo Anterior + Créditos - Débitos)
    saldo_calculado = saldo_anterior + total_creditos_calc - total_debitos_calc
    
    # Armar diccionario de resultados
    conciliation_results = {
        'Saldo Anterior (PDF)': saldo_anterior,
        'Créditos Totales (Movimientos)': total_creditos_calc,
        'Débitos Totales (Movimientos)': total_debitos_calc,
        'Saldo Final Calculado': saldo_calculado,
        'Saldo Final Informado (PDF)': saldo_informado,
        'Diferencia de Conciliación': saldo_informado - saldo_calculado if saldo_informado != 0 else 0
    }
    
    return df, conciliation_results


# --- Interfaz de Streamlit ---

st.title("💳 Extractor y Conciliador Bancario Credicoop")
st.markdown("---")

uploaded_file = st.file_uploader(
    "**1. Sube tu resumen de cuenta corriente en PDF (ej. Credicoop N&P)**",
    type=['pdf']
)

if uploaded_file is not None:
    st.info("⌛ Procesando archivo... por favor espera.")
    
    # Convertir el archivo cargado a bytes para pasarlo a la función
    file_bytes = uploaded_file.read()
    
    # Ejecutar la extracción y conciliación (usando caché de Streamlit)
    df_movs, results = process_bank_pdf(file_bytes)
    
    if not df_movs.empty:
        st.success("✅ Extracción y procesamiento completados.")
        
        # --- Sección de Conciliación ---
        st.header("2. Resumen de Conciliación")
        
        # Mostrar las métricas clave en columnas (usando st.metric)
        col1, col2, col3, col4 = st.columns(4)
        
        col1.metric("Saldo Anterior (PDF)", format_currency(results['Saldo Anterior (PDF)']))
        col2.metric("Créditos Totales", format_currency(results['Créditos Totales (Movimientos)']), 
                    delta_color="normal")
        col3.metric("Débitos Totales", format_currency(results['Débitos Totales (Movimientos)']),
                    delta_color="inverse")
        col4.metric("Movimientos Extraídos", len(df_movs))
        
        
        st.markdown("---")
        
        # --- Conciliación Final ---
        st.subheader("Resultado Final")
        
        # Cálculos para la alerta final
        diff = results['Diferencia de Conciliación']
        
        if abs(diff) < 0.50: # Tolerancia de 50 centavos
            alert_type = "success"
            alert_message = f"**Conciliación Exitosa:** El saldo calculado coincide con el saldo informado en el extracto."
        else:
            alert_type = "warning"
            alert_message = f"**Diferencia Detectada:** Hay una diferencia en la conciliación."

        st.markdown(f"**Saldo Final Calculado:** {format_currency(results['Saldo Final Calculado'])}")
        st.markdown(f"**Saldo Final Informado (PDF):** {format_currency(results['Saldo Final Informado (PDF)'])}")
        
        st.markdown(f"**Diferencia de Conciliación:** :red[{format_currency(diff)}]")

        st.alert(alert_type, alert_message)
        
        # --- Sección de Exportación ---
        st.header("3. Movimientos Detallados y Exportación")
        
        @st.cache_data
        def convert_df_to_excel(df):
            """Convierte el DataFrame a formato BytesIO para descarga en Excel."""
            output = BytesIO()
            with pd.ExcelWriter(output, engine='xlsxwriter') as writer:
                # Hoja 1: Movimientos
                df.to_excel(writer, sheet_name='Movimientos', index=False)
                
                # Hoja 2: Resumen/Conciliación
                resumen_df = pd.DataFrame(list(results.items()), columns=['Concepto', 'Valor'])
                resumen_df.to_excel(writer, sheet_name='Resumen', index=False)
                
                # Formato de valores en ARS en la hoja de resumen (opcional, avanzado)
                workbook = writer.book
                currency_format = workbook.add_format({'num_format': '[$$-es-AR]#,##0.00'})
                worksheet = writer.sheets['Resumen']
                
                # Aplicar formato de moneda a la columna 'Valor' (columna B)
                worksheet.set_column('B:B', 15, currency_format)
                
            return output.getvalue()

        # Botón de Descarga
        excel_bytes = convert_df_to_excel(df_movs)
        
        st.download_button(
            label="Descargar Movimientos a Excel (xlsx)",
            data=excel_bytes,
            file_name="Movimientos_Conciliados.xlsx",
            mime="application/vnd.ms-excel",
        )
        
        st.markdown("---")

        # --- Tabla de Movimientos (Previsualización) ---
        st.subheader("Vista Previa de Movimientos Extraídos")
        
        # Preparar DF para mostrarlo limpio en Streamlit
        df_display = df_movs.copy()
        df_display['Débito'] = df_display['Débito'].apply(lambda x: format_currency(x) if x > 0 else "")
        df_display['Crédito'] = df_display['Crédito'].apply(lambda x: format_currency(x) if x > 0 else "")
        df_display['Saldo_Final_Linea'] = df_display['Saldo_Final_Linea'].apply(format_currency)
        df_display.rename(columns={'Saldo_Final_Linea': 'Saldo en la Línea (PDF)'}, inplace=True)
        
        st.dataframe(df_display, use_container_width=True)

    elif uploaded_file is not None:
         st.error("❌ No se pudo extraer ningún dato. Verifica el formato del PDF.")

else:
    st.warning("👆 Por favor, sube un archivo PDF para comenzar la extracción y conciliación.")
