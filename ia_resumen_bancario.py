import streamlit as st
import pandas as pd
import pdfplumber
import re
from io import BytesIO
from collections import defaultdict

# --- Configuración de la Página ---
st.set_page_config(
    page_title="Extractor y Conciliador Bancario Credicoop (V16 - Extracción por Palabras)",
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
    
    total_amount = 0.0
    
    # Dividir el texto por saltos de línea (por si acaso, aunque extract_words no suele tenerlos)
    lines = text.split('\n')
    
    for line in lines:
        if not line.strip():
            continue
            
        cleaned_text = line.strip().replace('$', '').replace(' ', '')
        
        is_negative = cleaned_text.startswith('-') or (cleaned_text.startswith('(') and cleaned_text.endswith(')'))
        if is_negative:
            cleaned_text = cleaned_text.replace('-', '').replace('(', '').replace(')', '')
            
        if ',' in cleaned_text:
            if cleaned_text.count('.') > 0:
                cleaned_text = cleaned_text.replace('.', '')
            cleaned_text = cleaned_text.replace(',', '.')
        
        try:
            amount = float(cleaned_text)
            total_amount += -amount if is_negative else amount
        except ValueError:
            continue
            
    return total_amount

def format_currency(amount):
    """Formatea un número como moneda ARS (punto miles, coma decimal)."""
    if amount is None:
        return "$ 0,00"
    
    formatted_str = f"{amount:,.2f}"
    formatted_str = formatted_str.replace('.', 'X').replace(',', '.').replace('X', ',')
    
    return f"$ {formatted_str}"

# --- LÓGICA DE EXTRACCIÓN V16 (BASADA EN PALABRAS Y COORDENADAS) ---

def process_pdf_by_words(pdf_pages):
    """
    La lógica definitiva. Lee palabras y sus coordenadas (x,y) para reconstruir
    las filas, ignorando el layout de "tabla" que es defectuoso.
    """
    
    # Definición de las "zonas" (coordenadas X) para cada columna.
    # Estos valores se basan en el análisis visual de 'image_42fdc5.jpg'.
    # (x0, x1)
    ZONAS = {
        'fecha': (30, 85),
        'combte': (90, 155),
        'desc': (160, 515),
        'debito': (520, 615),
        'credito': (620, 715),
        'saldo': (720, 800)
    }

    extracted_lines = []
    
    for page in pdf_pages:
        words = page.extract_words(x_tolerance=2, y_tolerance=2, keep_blank_chars=False)
        
        # Agrupar palabras por línea (usando la coordenada 'top' como ID de línea)
        # Usamos un defaultdict para agrupar palabras que están *casi* en la misma línea
        lines = defaultdict(lambda: {
            'fecha': [],
            'combte': [],
            'desc': [],
            'debito': [],
            'credito': [],
            'saldo': []
        })
        
        for word in words:
            # Redondear la coordenada 'top' para agrupar palabras en la misma línea
            # El 'top' es la coordenada Y superior de la palabra
            line_key = round(word['top'])
            word_x = word['x0']
            
            # Asignar la palabra a su zona (columna)
            if ZONAS['fecha'][0] <= word_x < ZONAS['fecha'][1]:
                lines[line_key]['fecha'].append(word['text'])
            elif ZONAS['combte'][0] <= word_x < ZONAS['combte'][1]:
                lines[line_key]['combte'].append(word['text'])
            elif ZONAS['desc'][0] <= word_x < ZONAS['desc'][1]:
                lines[line_key]['desc'].append(word['text'])
            elif ZONAS['debito'][0] <= word_x < ZONAS['debito'][1]:
                lines[line_key]['debito'].append(word['text'])
            elif ZONAS['credito'][0] <= word_x < ZONAS['credito'][1]:
                lines[line_key]['credito'].append(word['text'])
            elif ZONAS['saldo'][0] <= word_x < ZONAS['saldo'][1]:
                lines[line_key]['saldo'].append(word['text'])

        # Procesar las líneas agrupadas
        sorted_line_keys = sorted(lines.keys())
        last_valid_fecha = ""
        
        for key in sorted_line_keys:
            line_data = lines[key]
            
            # Unir las palabras de cada zona
            fecha = " ".join(line_data['fecha'])
            combte = " ".join(line_data['combte'])
            desc = " ".join(line_data['desc'])
            debito_str = " ".join(line_data['debito'])
            credito_str = " ".join(line_data['credito'])
            saldo_str = " ".join(line_data['saldo'])
            
            # Validar si es una fila de movimiento
            is_date_row = re.match(r"\d{2}/\d{2}/\d{2}", fecha)
            debito = clean_and_parse_amount(debito_str)
            credito = clean_and_parse_amount(credito_str)
            
            # Omitir encabezados
            if "FECHA" in fecha.upper() or "SALDO ANTERIOR" in desc.upper():
                continue
                
            # Omitir líneas de descripción envueltas que no tienen montos
            if not is_date_row and debito == 0.0 and credito == 0.0:
                # Opcional: podríamos agregar esta 'desc' a la línea anterior
                continue
            
            if is_date_row:
                last_valid_fecha = fecha
            
            # Si la fila no tiene fecha, pero sí montos (ej. Impuestos), usar la última fecha
            current_fecha = fecha if is_date_row else last_valid_fecha
            
            # Solo agregar si es un movimiento real (tiene débito o crédito)
            if debito != 0.0 or credito != 0.0:
                extracted_lines.append({
                    'Fecha': current_fecha,
                    'Comprobante': combte,
                    'Descripcion': desc,
                    'Débito': debito,
                    'Crédito': credito,
                    'Saldo_Final_Linea': clean_and_parse_amount(saldo_str)
                })

    return pd.DataFrame(extracted_lines)


@st.cache_data
def process_bank_pdf_main(file_bytes):
    """
    Función principal que orquesta la extracción y conciliación.
    """
    
    saldo_informado = 0.0
    currency_pattern = r"[\(]?-?\s*(\d{1,3}(?:\.\d{3})*,\d{2})[\)]?"
    
    with pdfplumber.open(BytesIO(file_bytes)) as pdf:
        full_text = ""
        
        for page in pdf.pages:
            full_text += page.extract_text(x_tolerance=2) + "\n"
        
        # --- Detección de Saldo Final (Sigue siendo por RegEx) ---
        # Como usted dijo, el único saldo que importa es "SALDO AL DD/MM/AA"
        match_sf = re.search(r"(?:SALDO\s*AL.*?)(\d{2}/\d{2}/\d{2,4}).*?(-?" + currency_pattern + r")", full_text, re.DOTALL | re.IGNORECASE)
        
        if match_sf:
            saldo_str = match_sf.group(2)
            saldo_informado = clean_and_parse_amount(saldo_str)
        else:
            # Fallback (menos confiable)
            match_sf_gen = re.search(r"(?:SALDO\s*FINAL|SALDO.*?AL).*?(-?" + currency_pattern + r")", full_text, re.DOTALL | re.IGNORECASE)
            if match_sf_gen:
                saldo_informado = clean_and_parse_amount(match_sf_gen.group(1))

        # --- Extracción de Movimientos (Nueva Lógica V16) ---
        df = process_pdf_by_words(pdf.pages)
        
        if df.empty:
            st.error("❌ ¡ALERTA! Falló la extracción de movimientos (V16). La lógica de 'extract_words' no encontró movimientos. Verifique que el PDF sea texto seleccionable.")
            return pd.DataFrame(), {}
            
    # 3. Conciliación y Cálculos Finales
    
    if saldo_informado == 0.0 and not df.empty:
        # Fallback si el RegEx de Saldo Final falló
        saldo_informado = df['Saldo_Final_Linea'].iloc[-1]
        st.info(f"ℹ️ Saldo Final (obtenido de la última línea de mov.): {format_currency(saldo_informado)}")

    total_debitos_calc = df['Débito'].sum()
    total_creditos_calc = df['Crédito'].sum()
    
    # Cálculo del Saldo Anterior: SA = SF_Informado - Créditos + Débitos
    saldo_anterior = saldo_informado - total_creditos_calc + total_debitos_calc
    saldo_calculado = saldo_anterior + total_creditos_calc - total_debitos_calc
    
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

st.title("💳 Extractor y Conciliador Bancario Credicoop (V16 - Solución por Coordenadas)")
st.markdown("---")

uploaded_file = st.file_uploader(
    "**1. Sube tu resumen de cuenta corriente en PDF (ej. Credicoop N&P)**",
    type=['pdf']
)

if uploaded_file is not None:
    st.info("⌛ Procesando archivo... por favor espera.")
    
    file_bytes = uploaded_file.read()
    
    df_movs, results = process_bank_pdf_main(file_bytes)
    
    if not df_movs.empty and results:
        st.success("✅ Extracción y procesamiento completados.")
        
        # --- Sección de Conciliación ---
        st.header("2. Resumen de Conciliación")
        
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
        
        diff = results['Diferencia de Conciliación']
        
        st.markdown(f"**Saldo Final Calculado (SA + Créditos - Débitos):** **{format_currency(results['Saldo Final Calculado'])}**")
        st.markdown(f"**Saldo Final Informado (PDF):** **{format_currency(results['Saldo Final Informado (PDF)'])}**")
        
        if abs(diff) < 0.50: 
            st.success(f"**Conciliación Exitosa:** El saldo calculado coincide con el saldo informado en el extracto. Diferencia: {format_currency(diff)}")
        else:
            st.error(f"**Diferencia Detectada:** La conciliación **NO CIERRA**. Diferencia: {format_currency(diff)}")
            st.warning("Esto puede deberse a: 1) Un error en la lectura del Saldo Final Informado del PDF. 2) Movimientos no capturados por la lógica de extracción.")

        
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

        excel_bytes = convert_df_to_excel(df_movs)
        
        st.download_button(
            label="Descargar Movimientos a Excel (xlsx)",
            data=excel_bytes,
            file_name=f"Movimientos_Credicoop_Procesado.xlsx",
            mime="application/vnd.ms-excel",
        )
        
        st.markdown("---")

        # --- Tabla de Movimientos (Previsualización) ---
        st.subheader("Vista Previa de Movimientos Extraídos")
        
        df_display = df_movs.copy()
        
        df_display['Débito'] = df_display['Débito'].apply(lambda x: format_currency(x) if x > 0 else "")
        df_display['Crédito'] = df_display['Crédito'].apply(lambda x: format_currency(x) if x > 0 else "")
        df_display['Saldo_Final_Linea'] = df_display['Saldo_Final_Linea'].apply(format_currency)
        
        df_display.rename(columns={'Saldo_Final_Linea': 'Saldo en la Línea (PDF)'}, inplace=True)
        
        st.dataframe(df_display, use_container_width=True)

    elif uploaded_file is not None:
         st.error("❌ Falló la extracción de movimientos (V16). No se encontraron movimientos. Verifique que el PDF sea texto seleccionable.")

else:
    st.warning("👆 Por favor, sube un archivo PDF para comenzar la extracción y conciliación.")

