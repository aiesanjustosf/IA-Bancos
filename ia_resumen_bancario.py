import streamlit as st
import pandas as pd
import pdfplumber
import re
from io import BytesIO

# --- Configuración de la Página ---
st.set_page_config(
    page_title="Conversor PDF a Excel (V17 - Extracción Cruda)",
    layout="wide",
    initial_sidebar_state="expanded"
)

# --- Lógica Principal de Extracción ---

@st.cache_data
def convert_pdf_to_excel_raw(file_bytes):
    """
    Extrae todas las tablas y texto crudo de un PDF y lo guarda en
    múltiples hojas de un archivo Excel.
    """
    
    output_excel = BytesIO()
    
    with pd.ExcelWriter(output_excel, engine='xlsxwriter') as writer:
        
        try:
            with pdfplumber.open(BytesIO(file_bytes)) as pdf:
                
                for i, page in enumerate(pdf.pages):
                    page_num = i + 1
                    
                    # 1. Intentar extraer tablas
                    # Usamos la configuración de tabla más simple
                    tables = page.extract_tables(table_settings={
                        "vertical_strategy": "lines",
                        "horizontal_strategy": "lines",
                        "snap_tolerance": 3,
                    })
                    
                    if tables:
                        for j, table in enumerate(tables):
                            table_num = j + 1
                            if table:
                                # Convertir la lista de listas a DataFrame
                                df = pd.DataFrame(table[1:], columns=table[0])
                                sheet_name = f"Pagina_{page_num}_Tabla_{table_num}"
                                df.to_excel(writer, sheet_name=sheet_name, index=False)
                    
                    else:
                        # 2. Si no hay tablas, extraer texto crudo
                        raw_text = page.extract_text(x_tolerance=2, y_tolerance=2)
                        
                        if raw_text:
                            # Guardar el texto crudo en una hoja
                            df_text = pd.DataFrame([line for line in raw_text.split('\n')], columns=["Texto Crudo"])
                            sheet_name = f"Pagina_{page_num}_Texto"
                            df_text.to_excel(writer, sheet_name=sheet_name, index=False)
                        else:
                            # Página vacía o imagen
                            df_empty = pd.DataFrame(["Página sin texto extraíble."])
                            sheet_name = f"Pagina_{page_num}_Vacia"
                            df_empty.to_excel(writer, sheet_name=sheet_name, index=False)
                            
        except Exception as e:
            st.error(f"Error fatal durante la conversión: {e}")
            df_error = pd.DataFrame([f"Error: {e}"])
            df_error.to_excel(writer, sheet_name="Error", index=False)

    return output_excel.getvalue()


# --- Interfaz de Streamlit ---

st.title("💳 Conversor de PDF Bancario a Excel (V17)")
st.markdown("Sube el PDF de tu resumen bancario. Esta herramienta extraerá todas las tablas y texto crudo que encuentre y los guardará en un archivo Excel, con una hoja por cada tabla o página.")
st.markdown("---")

uploaded_file = st.file_uploader(
    "**1. Sube tu resumen de cuenta corriente en PDF**",
    type=['pdf']
)

if uploaded_file is not None:
    
    file_name = uploaded_file.name
    excel_file_name = f"{file_name.replace('.pdf', '')}_Extraido.xlsx"
    
    st.info("⌛ Procesando archivo... convirtiendo a Excel.")
    
    excel_bytes = convert_pdf_to_excel_raw(uploaded_file.read())
    
    st.success("✅ Conversión completada.")
    
    st.download_button(
        label="Descargar Archivo Excel (xlsx)",
        data=excel_bytes,
        file_name=excel_file_name,
        mime="application/vnd.ms-excel",
    )
    
    st.markdown("---")
    st.warning("**Nota:** Esta versión no concilia los datos, solo extrae la información cruda del PDF a Excel. Deberá revisar las hojas de Excel para encontrar los movimientos.")

else:
    st.warning("👆 Por favor, sube un archivo PDF para comenzar la conversión.")
