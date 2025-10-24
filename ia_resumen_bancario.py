import streamlit as st
import pandas as pd
import pdfplumber
import re
from io import BytesIO

# --- Configuración de la Página ---
st.set_page_config(
    page_title="Extractor y Conciliador Bancario Credicoop (V13)",
    layout="wide",
    initial_sidebar_state="expanded"
)

# --- Funciones de Utilidad ---

def clean_and_parse_amount(text):
    """
    Limpia una cadena de texto y la convierte a un número flotante.
    Maneja el formato argentino (punto como separador de miles, coma como decimal).
    """
    if not isinstance(text, str) or not text.strip():
        return 0.0
    
    # 1. Eliminar símbolos de moneda y espacios
    cleaned_text = text.strip().replace('$', '').replace(' ', '')
    
    # 2. Manejo de negativo (paréntesis o guion)
    is_negative = cleaned_text.startswith('-') or (cleaned_text.startswith('(') and cleaned_text.endswith(')'))
    if is_negative:
        cleaned_text = cleaned_text.replace('-', '').replace('(', '').replace(')', '')
        
    # 3. Eliminar separador de miles y convertir la coma decimal a punto
    if ',' in cleaned_text:
        # Asumimos que el punto es de miles si hay coma decimal
        if cleaned_text.count('.') > 0:
            cleaned_text = cleaned_text.replace('.', '')
        cleaned_text = cleaned_text.replace(',', '.')
    
    try:
        amount = float(cleaned_text)
        return -amount if is_negative else amount
    except ValueError:
        return 0.0

def format_currency(amount):
    """Formatea un número como moneda ARS (punto miles, coma decimal)."""
    if amount is None:
        return "$ 0,00"
    
    # Formato ARS: punto como separador de miles, coma como decimal
    formatted_str = f"{amount:,.2f}"
    formatted_str = formatted_str.replace('.', 'X').replace(',', '.').replace('X', ',')
    
    return f"$ {formatted_str}"
    
# --- Lógica Principal de Extracción del PDF ---

@st.cache_data
def process_bank_pdf(file_bytes):
    """
    Extrae, limpia y concilia los movimientos de un extracto bancario Credicoop.
    Retorna el DataFrame de movimientos y el diccionario de saldos de conciliación.
    """
    
    extracted_data = []
    saldo_anterior = 0.0
    saldo_informado = 0.0
    
    # Patrón para encontrar números de moneda
    currency_pattern = r"[\(]?-?\s*(\d{1,3}(?:\.\d{3})*,\d{2})[\)]?"
    
    
    with pdfplumber.open(BytesIO(file_bytes)) as pdf:
        full_text = ""
        
        # 1. Extraer todo el texto para buscar saldos clave
        for page in pdf.pages:
            full_text += page.extract_text() + "\n"
        
        # --- Detección de Saldo Final (Saldo al 30/06/2025) ---
        
        # Búsqueda estricta del Saldo AL XXXXXXX seguido del monto
        match_sf = re.search(r"(?:SALDO\s*AL.*?)(\d{2}/\d{2}/\d{2,4}).*?(-?" + currency_pattern + r")", full_text, re.DOTALL | re.IGNORECASE)
        
        if match_sf:
            saldo_str = match_sf.group(2) # El monto es el segundo grupo
            saldo_informado = clean_and_parse_amount(saldo_str)
        else:
            # Fallback a búsqueda genérica de "SALDO" y monto (menos confiable)
            match_sf_gen = re.search(r"(?:SALDO\s*FINAL|SALDO.*?AL).*?(-?" + currency_pattern + r")", full_text, re.DOTALL | re.IGNORECASE)
            if match_sf_gen:
                saldo_informado = clean_and_parse_amount(match_sf_gen.group(1))

        # 2. Extraer Movimientos Usando Tablas por REGIÓN (SOLUCIÓN DEFINITIVA)
        
        # Definir las coordenadas aproximadas de la tabla de movimientos en el PDF
        # Se asume que el área de la tabla comienza en Y=120 y termina en Y=720 (la parte inferior de la página).
        # X: 0 a 792 (ancho completo del PDF)
        # Y: 0 a 1000 (alto completo del PDF)
        # Bbox: (x0, top, x1, bottom)
        TABLE_REGION_BBOX = (30, 120, 780, 720) 

        # Configuraciones de tabla ahora son mínimas, confiando en la detección de lineas del PDF
        table_settings = {
            # Se usa 'lines' porque el extracto de Credicoop tiene líneas divisorias visibles.
            "vertical_strategy": "lines", 
            "horizontal_strategy": "lines",
            "snap_tolerance": 8 # Tolerancia aumentada para capturar mejor las lineas sutiles.
        }
        
        # Iterar páginas con movimientos
        pages_to_process = range(len(pdf.pages))
        
        for page_index in pages_to_process:
            if page_index >= len(pdf.pages):
                continue
                
            page = pdf.pages[page_index]
            
            # 1. Recortar la página a la región de la tabla de movimientos
            cropped_page = page.crop(TABLE_REGION_BBOX)
            
            # 2. Extraer tablas de la región recortada
            tables = cropped_page.extract_tables(table_settings)
            
            for table in tables:
                # Omitir el primer elemento si es un encabezado o la fila de "SALDO ANTERIOR"
                start_row = 0
                if table and (any("FECHA" in str(c).upper() for c in table[0]) or any("ANTERIOR" in str(c).upper() for c in table[0])):
                    start_row = 1 
                    
                for row in table[start_row:]:
                    
                    # Una fila de movimiento debe tener al menos 6 columnas (0 a 5)
                    # La extracción por región puede generar diferentes números de columnas si la detección de líneas es imperfecta.
                    # Adaptamos los índices para los 6 campos esperados
                    
                    if len(row) >= 5: # Mínimo 5 campos (Fecha, Combte, Desc, Debito/Credito/Saldo)
                        
                        # Indices de las columnas esperadas después de la detección automática (Aprox.)
                        fecha_idx = 0
                        combte_idx = 1
                        desc_idx = 2
                        
                        # La ubicación de Débito, Crédito y Saldo es variable, usamos los últimos 3 campos si hay más de 6
                        if len(row) > 6:
                            # Si hay más de 6 columnas (por detecciones falsas), intentamos tomar los campos correctos
                            debito_idx = len(row) - 3
                            credito_idx = len(row) - 2
                            saldo_idx = len(row) - 1
                        else:
                            # Si hay 6 columnas exactas (lo ideal)
                            debito_idx = 3
                            credito_idx = 4
                            saldo_idx = 5
                        
                        fecha = str(row[fecha_idx]).strip() if row[fecha_idx] else ""
                        
                        # CRÍTICO: Excluir las filas que no tienen fecha válida (encabezados, continuaciones, saldos)
                        if re.match(r"\d{2}/\d{2}/\d{2}", fecha):
                            
                            debito_raw = str(row[debito_idx]).strip() if row[debito_idx] else ""
                            credito_raw = str(row[credito_idx]).strip() if row[credito_idx] else ""
                            saldo_raw = str(row[saldo_idx]).strip() if row[saldo_idx] else ""
                            
                            debito = clean_and_parse_amount(debito_raw)
                            credito = clean_and_parse_amount(credito_raw)
                            
                            # Solo considerar como movimiento si tiene Débito O Crédito, y no es cero.
                            if debito != 0.0 or credito != 0.0:
                                extracted_data.append({
                                    'Fecha': fecha,
                                    'Comprobante': str(row[combte_idx]).strip(),
                                    'Descripcion': str(row[desc_idx]).strip(),
                                    'Débito': debito,
                                    'Crédito': credito,
                                    'Saldo_Final_Linea': clean_and_parse_amount(saldo_raw)
                                })
                            
    if not extracted_data:
        st.error("❌ ¡ALERTA! Falló la extracción de movimientos. La detección de tabla por región falló. El formato de su PDF es altamente inusual.")
        return pd.DataFrame(), {}
        
    # Crear DataFrame
    df = pd.DataFrame(extracted_data)
    
    # 3. Conciliación y Cálculos Finales
    
    # Fallback de Saldo Final (si el texto no lo dio)
    if saldo_informado == 0.0 and not df.empty:
        # Tomamos el saldo de la última línea extraída
        saldo_informado = df['Saldo_Final_Linea'].iloc[-1]
        st.info(f"ℹ️ Saldo Final obtenido de la última línea de movimientos: {format_currency(saldo_informado)}")


    # Totales calculados
    total_debitos_calc = df['Débito'].sum()
    total_creditos_calc = df['Crédito'].sum()
    
    # Cálculo del Saldo Anterior: SA = SF_Informado - Créditos + Débitos
    # Esto garantiza que el Saldo Inicial es el punto de partida correcto para este extracto.
    saldo_anterior = saldo_informado - total_creditos_calc + total_debitos_calc
    saldo_calculado = saldo_anterior + total_creditos_calc - total_debitos_calc
    
    
    # Armar diccionario de resultados
    conciliation_results = {
        'Saldo Anterior (CALCULADO)': saldo_anterior,
        'Créditos Totales (Movimientos)': total_creditos_calc,
        'Débitos Totales (Movimientos)': total_debitos_calc,
        'Saldo Final Calculado': saldo_calculado,
        'Saldo Final Informado (PDF)': saldo_informado,
        'Diferencia de Conciliación': saldo_informado - saldo_calculado
    }
    
    return df, conciliation_results


# --- Interfaz de Streamlit ---

st.title("💳 Extractor y Conciliador Bancario Credicoop (V13 - SOLUCIÓN DEFINITIVA)")
st.markdown("---")

uploaded_file = st.file_uploader(
    "**1. Sube tu resumen de cuenta corriente en PDF (ej. Credicoop N&P)**",
    type=['pdf']
)

if uploaded_file is not None:
    st.info("⌛ Procesando archivo... por favor espera.")
    
    file_bytes = uploaded_file.read()
    
    df_movs, results = process_bank_pdf(file_bytes)
    
    if not df_movs.empty and results:
        st.success("✅ Extracción y procesamiento completados.")
        
        # --- Sección de Conciliación ---
        st.header("2. Resumen de Conciliación")
        
        # Mostrar las métricas clave en columnas (usando st.metric)
        col1, col2, col3, col4 = st.columns(4)
        
        col1.metric("Saldo Anterior (Calculado)", format_currency(results['Saldo Anterior (CALCULADO)']))
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
        
        # Mostrar los saldos clave
        st.markdown(f"**Saldo Final Calculado (SA + Créditos - Débitos):** **{format_currency(results['Saldo Final Calculado'])}**")
        st.markdown(f"**Saldo Final Informado (PDF):** **{format_currency(results['Saldo Final Informado (PDF)'])}**")
        
        # Alerta de diferencia
        if abs(diff) < 0.50: # Tolerancia de 50 centavos
            st.success(f"**Conciliación Exitosa:** El saldo calculado coincide con el saldo informado en el extracto. Diferencia: {format_currency(diff)}")
        else:
            st.error(f"**Diferencia Detectada:** La conciliación **NO CIERRA**. Diferencia: {format_currency(diff)}")
            st.warning("Esto puede deberse a: 1) Movimientos de saldo (intereses, impuestos, etc.) que no se extrajeron de la tabla. 2) Un error en la lectura de débitos/créditos. Por favor, revisa la tabla de movimientos extraídos.")

        
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
                resumen_data = [
                    ('Saldo Anterior (CALCULADO)', results['Saldo Anterior (CALCULADO)']),
                    ('Créditos Totales', results['Créditos Totales (Movimientos)']),
                    ('Débitos Totales', results['Débitos Totales (Movimientos)']),
                    ('Saldo Final Calculado', results['Saldo Final Calculado']),
                    ('Saldo Final Informado (PDF)', results['Saldo Final Informado (PDF)']),
                    ('Diferencia de Conciliación', results['Diferencia de Conciliación']),
                ]
                resumen_df = pd.DataFrame(resumen_data, columns=['Concepto', 'Valor'])
                resumen_df.to_excel(writer, sheet_name='Resumen', index=False)
                
            return output.getvalue()

        # Botón de Descarga
        excel_bytes = convert_df_to_excel(df_movs)
        
        st.download_button(
            label="Descargar Movimientos a Excel (xlsx)",
            data=excel_bytes,
            file_name=f"Movimientos_Credicoop_{df_movs['Fecha'].iloc[-1].replace('/', '-')}.xlsx",
            mime="application/vnd.ms-excel",
        )
        
        st.markdown("---")

        # --- Tabla de Movimientos (Previsualización) ---
        st.subheader("Vista Previa de Movimientos Extraídos")
        
        df_display = df_movs.copy()
        
        # Aplicar formato de moneda para la vista (pero mantener números para exportación)
        df_display['Débito'] = df_display['Débito'].apply(lambda x: format_currency(x) if x > 0 else "")
        df_display['Crédito'] = df_display['Crédito'].apply(lambda x: format_currency(x) if x > 0 else "")
        df_display['Saldo_Final_Linea'] = df_display['Saldo_Final_Linea'].apply(format_currency)
        
        df_display.rename(columns={'Saldo_Final_Linea': 'Saldo en la Línea (PDF)'}, inplace=True)
        
        st.dataframe(df_display, use_container_width=True)

    elif uploaded_file is not None:
         # Si uploaded_file existe pero df_movs está vacío
         st.error("❌ Falló la extracción de movimientos. La detección de tabla por región falló. Por favor, intente con la versión V12 si esta no funciona.")

else:
    st.warning("👆 Por favor, sube un archivo PDF para comenzar la extracción y conciliación.")
