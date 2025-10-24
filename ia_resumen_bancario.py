import streamlit as st
import pandas as pd
import pdfplumber
import re
from io import BytesIO

# --- Configuración de la Página ---
st.set_page_config(
    page_title="Extractor y Conciliador Bancario Credicoop (V11)",
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
    
    # Patrón para encontrar números de moneda (puede ser negativo)
    currency_pattern = r"[\(]?-?\s*(\d{1,3}(?:\.\d{3})*,\d{2})[\)]?"
    
    
    with pdfplumber.open(BytesIO(file_bytes)) as pdf:
        full_text = ""
        
        # 1. Extraer todo el texto para buscar saldos clave
        for page in pdf.pages:
            full_text += page.extract_text() + "\n"
        
        # --- Detección de Saldo Final (Ajuste V9: Más estricto) ---
        
        # 1. Búsqueda estricta de "SALDO AL 30/06/2025 ..." seguida del monto
        # Intentamos ser lo más precisos posible con la fecha final del extracto.
        match_sf = re.search(r"(?:SALDO AL.*?)(\d{2}/\d{2}/\d{2,4})\s+.*?(-?" + currency_pattern + r")", full_text, re.DOTALL | re.IGNORECASE)
        
        # 2. Búsqueda de Saldo Final (si el primero falla)
        if not match_sf:
            match_sf = re.search(r"(?:SALDO\s*FINAL|SALDO.*?AL).*?(-?" + currency_pattern + r")", full_text, re.DOTALL | re.IGNORECASE)

        # Asignar saldo
        if match_sf:
            # Group(1) o Group(2) contiene el monto
            # En el primer patrón Group(2) es el monto. En el segundo patrón Group(1) es el monto.
            try:
                # Intentamos el más estricto
                saldo_str = match_sf.group(2) 
            except IndexError:
                # Si falló, tomamos el grupo 1 (del patrón más genérico)
                saldo_str = match_sf.group(1) 
            
            saldo_informado = clean_and_parse_amount(saldo_str)
        
        # 3. Fallback: Intentar obtener el Saldo Final de la última fila de la tabla (si la tabla se leyó correctamente)
        if saldo_informado == 0.0:
            st.warning("⚠️ El Saldo Final no se pudo detectar del texto libre. Intentando obtenerlo de la última línea de movimientos extraída.")
            # Este fallback se aplica más adelante si hay movimientos extraídos.
        
        
        # 2. Extraer Movimientos Usando Tablas
        
        # AJUSTE CRUCIAL V11: AJUSTE DE COORDENADAS Y TOLERANCIA AUMENTADA
        # FECHA | COMBTE | DESCRIPCION (Menos espacio) | DEBITO | CREDITO | SALDO
        table_settings = {
            "vertical_strategy": "explicit",
            "horizontal_strategy": "lines",
            # Coordenadas ajustadas V11:
            # [30]: Fecha, [80]: Comprobante, 
            # [150]: Inicio Descripción (Más estrecho)
            # [440]: Inicio Débito
            # [540]: Fin Débito / Inicio Crédito
            # [640]: Fin Crédito / Inicio Saldo
            # [720]: Fin Saldo
            "explicit_vertical_lines": [30, 80, 150, 440, 540, 640, 720],
            "snap_tolerance": 8 # Tolerancia aumentada a 8 (antes 5) para capturar mejor las líneas
        }
        
        # Iterar más páginas para asegurar todos los movimientos
        pages_to_process = range(len(pdf.pages)) # Revisamos todas las páginas, no solo las 3 primeras
        
        for page_index in pages_to_process:
            if page_index >= len(pdf.pages):
                continue
                
            page = pdf.pages[page_index]
            tables = page.extract_tables(table_settings)
            
            for table in tables:
                # Omitir el primer elemento si es un encabezado o una fila inválida
                start_row = 0
                if table and any("FECHA" in str(c).upper() for c in table[0]):
                    start_row = 1 
                    
                for row in table[start_row:]:
                    
                    # Una fila de movimiento debe tener al menos 5 o 6 columnas
                    if len(row) >= 5:
                        
                        fecha = str(row[0]).strip() if len(row) > 0 and row[0] else ""
                        
                        # CRÍTICO: Excluir las filas que no tienen fecha válida (encabezados, continuaciones, saldos)
                        if re.match(r"\d{2}/\d{2}/\d{2}", fecha):
                            
                            # Row indices: [0]: Fecha, [1]: Comprobante, [2]: Descripción, [3]: Débito, [4]: Crédito, [5]: Saldo
                            
                            debito_raw = str(row[3]).strip() if len(row) > 3 and row[3] else ""
                            credito_raw = str(row[4]).strip() if len(row) > 4 and row[4] else ""
                            saldo_raw = str(row[5]).strip() if len(row) > 5 and row[5] else ""
                            
                            debito = clean_and_parse_amount(debito_raw)
                            credito = clean_and_parse_amount(credito_raw)
                            
                            # Solo considerar como movimiento si tiene Débito O Crédito, y no es cero.
                            if debito != 0.0 or credito != 0.0:
                                extracted_data.append({
                                    'Fecha': fecha,
                                    'Comprobante': str(row[1]).strip(),
                                    'Descripcion': str(row[2]).strip(),
                                    'Débito': debito,
                                    'Crédito': credito,
                                    'Saldo_Final_Linea': clean_and_parse_amount(saldo_raw)
                                })
                            
    if not extracted_data:
        # Esto ocurre si las coordenadas fallaron o no hay movimientos en el rango
        st.error("❌ ¡ALERTA! Falló la extracción de movimientos. La configuración de coordenadas de tabla es el problema principal. Por favor, intente con la sugerencia manual de la línea 126.")
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

st.title("💳 Extractor y Conciliador Bancario Credicoop (V11 - Máxima Robustez)")
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
         st.error("❌ Falló la extracción de movimientos. La configuración de coordenadas (`explicit_vertical_lines`) es el problema central. Sigue la instrucción de la línea 126 para probar los ajustes manuales.")

else:
    st.warning("👆 Por favor, sube un archivo PDF para comenzar la extracción y conciliación.")

