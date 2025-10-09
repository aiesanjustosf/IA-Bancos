# 🧾 IA Resumen Bancario

Aplicación **Streamlit** para analizar resúmenes bancarios en PDF de forma automática y precisa.  
No inventa números: lee los débitos y créditos reales tal como aparecen en el documento.

---

## 🚀 Características
- Detecta automáticamente:
  - **Saldo inicial** y **saldo final** (buscando “Saldo al dd/mm/yyyy”)
  - **Transferencias de terceros** (recibidas y realizadas)
  - **Transferencias entre cuentas propias**
  - **Débitos automáticos** (API, ARCA, seguros)
  - **SIRCREB**, **DyC**
  - **Comisiones**, **IVA (21% / 10,5%)**, **Percepciones de IVA**
- Muestra listados completos:
  - Todos los **débitos**
  - Todos los **créditos**
- Agrupa transferencias **por CUIT** si aparece en el texto.

---

## 🖥️ Interfaz
Pantalla principal:
- Logo (`logo_aie.png`)
- Título: **IA Resumen Bancario**
- Favicon (`favicon-aie.ico`)
- Opción para subir PDF
- Vista de resumen con métricas y tablas.

---

## ⚙️ Instalación

1. Clonar el repositorio:
   ```bash
   git clone https://github.com/<tu_usuario>/ia-resumen-bancario.git
   cd ia-resumen-bancario
   ```

2. Instalar dependencias:
   ```bash
   pip install -r requirements.txt
   ```

3. Ejecutar la aplicación:
   ```bash
   streamlit run ia_resumen_bancario.py
   ```

4. Subir un archivo PDF (o usar el ejemplo incluido en `/example`).

---

## 🧩 Configuración
Por defecto:
- **Crédito resta** y **Débito suma**.  
  (Puede cambiarse desde un interruptor en pantalla).

El **saldo final** se toma del texto que diga “Saldo al dd/mm/yyyy”.

---

## 📊 Ejemplo de salida

```
Saldo inicial: $ 125.000,00
Saldo final: $ 155.320,00
Transferencias recibidas: $ 25.000,00
Transferencias realizadas: $ 8.000,00
Débitos API: $ 2.200,00
Débitos ARCA: $ 1.050,00
Sircreb: $ 800,00
DyC: $ 250,00
Comisiones (neto): $ 540,00
IVA 21%: $ 113,40
IVA 10,5%: $ 0,00
Percepciones IVA: $ 0,00
```

---

## 🧱 Estructura de Datos

Cada movimiento se registra con:
| Fecha | Descripción | Débito | Crédito | Importe | Saldo | Tipo | CUIT | Página | Fila |
|--------|--------------|--------|----------|----------|--------|------|-------|--------|

---

## 🔒 Notas
- No inventa importes ni porcentajes.
- Si el PDF no detalla el IVA o el neto, no lo infiere.
- Ideal para uso contable o conciliaciones mensuales.

---

## 📎 Autor
**Herramienta para uso interno - AIE San Justo**  
**Developer: Alfonso Alderete**
