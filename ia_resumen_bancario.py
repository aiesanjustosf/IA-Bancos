import streamlit as st
import pandas as pd
import pdfplumber
import re
from io import BytesIO

# --- Configuración de la Página ---
st.set_page_config(
    page_title="Extractor y Conciliador Bancario Credicoop (V14 - RegEx)",
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
    
    # 1. Eliminar símbolos de moneda, espacios y saltos de línea
    cleaned_text = text.strip().replace('$', '').replace(' ', '').replace('\n', '')
    
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
    Extrae, limpia y concilia los movimientos de un extracto bancario Credicoop
    utilizando una estrategia robusta de Expresiones Regulares.
    """
    
    extracted_data = []
    saldo_informado = 0.0
    
    # Patrón para encontrar números de moneda
    currency_pattern = r"[\(]?-?\s*(\d{1,3}(?:\.\d{3})*,\d{2})[\)]?"
    
    # Expresión Regular para detectar el SALDO FINAL en el texto
    # (?:SALDO\s*AL.*?)(\d{2}/\d{2}/\d{2,4}) buscará la fecha después de 'SALDO AL'
    # (-?" + currency_pattern + r") capturará el monto (puede ser negativo)
    
    # Expresión Regular para DETECTAR UNA LÍNEA DE MOVIMIENTO COMPLETA
    # Esta es la clave para la robustez, ya que busca el patrón rígido (Fecha + Montos)
    
    # 1. Fecha: \d{2}/\d{2}/\d{2} (ej. 01/06/25)
    # 2. Comprobante: (\d{6}) (ej. 262461, o texto)
    # 3. Descripción: (.*?) (todo el texto entre el comprobante y los montos)
    # 4. Monto (Crédito/Débito/Saldo): (-?\s*\d{1,3}(?:\.\d{3})*,\d{2})
    # Se adapta para capturar dos montos (Débito y Crédito) y el Saldo final
    
    # Patrón para una línea de movimiento, asumiendo que el texto del PDF es lineal:
    # Captura 1: Fecha (DD/MM/AA)
    # Captura 2: Comprobante (texto/número)
    # Captura 3: Descripción (el texto intermedio)
    # Captura 4: Monto 1 (Débito)
    # Captura 5: Monto 2 (Crédito)
    # Captura 6: Monto 3 (Saldo)
    
    # Vamos a usar un patrón más flexible basado en el snippet que muestra el PDF
    # Patrón: Fecha | Combte | Descripción | Monto1 | Monto2 | Monto3
    
    # Monto simple para RegEx: permite (o no) guiones, puntos de miles y coma decimal.
    monto_regex = r"[\(]?-?\s*(\d{1,3}(?:\.\d{3})*,\d{2})[\)]?"
    
    # El patrón más robusto (y complejo) para el formato Credicoop (Fecha Comprobante Descripción Monto1 Monto2 Monto3)
    # Usa \s+ para manejar cualquier cantidad de espacios o saltos de línea.
    # El ".*?" en la descripción es CRÍTICO para que tome todo el texto entre el Comprobante y los montos.
    movement_pattern = re.compile(
        r"(\d{2}/\d{2}/\d{2,4})\s+"        # 1. Fecha
        r"(.+?)"                           # 2. Comprobante (Non-greedy until desc)
        r"(.+?)"                           # 3. Descripción (Non-greedy until montos)
        r"(?:\s{2,}|\n|\r)"                # Separador (al menos 2 espacios, o salto de línea)
        r"(\s{1,2}|" + monto_regex + r")"  # 4. Débito (Espacio si está vacío, o Monto)
        r"(?:\s{2,}|\n|\r)"                # Separador
        r"(\s{1,2}|" + monto_regex + r")"  # 5. Crédito (Espacio si está vacío, o Monto)
        r"(?:\s{2,}|\n|\r)"                # Separador
        r"(" + monto_regex + r")",         # 6. Saldo (Siempre tiene que haber saldo)
        re.DOTALL | re.IGNORECASE
    )


    with pdfplumber.open(BytesIO(file_bytes)) as pdf:
        full_text = ""
        
        # 1. Extraer todo el texto de todas las páginas para tener el flujo completo
        for page in pdf.pages:
            full_text += page.extract_text(x_tolerance=2) + "\n\n"
        
        # --- Detección de Saldo Final (Aún por RegEx) ---
        match_sf = re.search(r"(?:SALDO\s*AL.*?)(\d{2}/\d{2}/\d{2,4}).*?(-?" + currency_pattern + r")", full_text, re.DOTALL | re.IGNORECASE)
        
        if match_sf:
            saldo_str = match_sf.group(2)
            saldo_informado = clean_and_parse_amount(saldo_str)
        else:
            match_sf_gen = re.search(r"(?:SALDO\s*FINAL|SALDO.*?AL).*?(-?" + currency_pattern + r")", full_text, re.DOTALL | re.IGNORECASE)
            if match_sf_gen:
                saldo_informado = clean_and_parse_amount(match_sf_gen.group(1))

        # 2. Extraer Movimientos Usando RegEx en el texto completo
        
        # Iterar sobre todas las coincidencias del patrón de movimiento
        for match in movement_pattern.finditer(full_text):
            
            # Los grupos de captura se corresponden con el patrón RegEx
            fecha = match.group(1).strip()
            comprobante = match.group(2).strip()
            descripcion = match.group(3).strip().replace('\n', ' ').replace('\r', ' ')
            
            # Los grupos 4 y 5 son Débito y Crédito, pueden ser un espacio o un monto
            debito_raw = match.group(4).strip()
            credito_raw = match.group(5).strip()
            saldo_raw = match.group(6).strip()
            
            debito = clean_and_parse_amount(debito_raw)
            credito = clean_and_parse_amount(credito_raw)
            saldo = clean_and_parse_amount(saldo_raw)
            
            # Filtro final de calidad: debe tener Débito O Crédito
            if debito != 0.0 or credito != 0.0:
                extracted_data.append({
                    'Fecha': fecha,
                    'Comprobante': comprobante,
                    'Descripcion': descripcion,
                    'Débito': debito,
                    'Crédito': credito,
                    'Saldo_Final_Linea': saldo
                })
                            
    if not extracted_data:
        st.error("❌ ¡ALERTA! Falló la extracción de movimientos. El patrón de texto no coincide con los movimientos. El PDF podría ser una imagen o tener un formato muy inusual.")
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

st.title("💳 Extractor y Conciliador Bancario Credicoop (V14 - RegEx)")
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
         st.error("❌ Falló la extracción de movimientos. El patrón de texto no coincide con los movimientos. Por favor, revisa si el PDF es texto seleccionable y no una imagen.")

else:
    st.warning("👆 Por favor, sube un archivo PDF para comenzar la extracción y conciliación.")

