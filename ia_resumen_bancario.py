# ===== Resumen Operativo (IVA + Otros) =====
st.caption("Resumen Operativo: Registración Módulo IVA")

# Valores por defecto (otros bancos siguen igual)
iva21_mask  = df_sorted["Clasificación"].eq("IVA 21% (sobre comisiones)")
iva105_mask = df_sorted["Clasificación"].eq("IVA 10,5% (sobre comisiones)")
iva21  = float(df_sorted.loc[iva21_mask,  "debito"].sum())
iva105 = float(df_sorted.loc[iva105_mask, "debito"].sum())
net21  = round(iva21  / 0.21,  2) if iva21  else 0.0
net105 = round(iva105 / 0.105, 2) if iva105 else 0.0
percep_iva = float(df_sorted.loc[df_sorted["Clasificación"].eq("Percepciones de IVA"), "debito"].sum())
ley_25413  = float(df_sorted.loc[df_sorted["Clasificación"].eq("LEY 25413"),          "debito"].sum())
sircreb    = float(df_sorted.loc[df_sorted["Clasificación"].eq("SIRCREB"),            "debito"].sum())

# ---- OVERRIDE ESPECÍFICO PARA GALICIA ----
if banco_slug == "galicia":
    # Trabajamos sobre descripción normalizada
    udesc = df_sorted["desc_norm"].fillna("").str.upper()

    # Percepciones de IVA (no se mezclan con IVA de comisiones)
    mask_percep_iva = udesc.str.contains(r"PERCEP\.?\s*IVA")
    percep_iva = float(df_sorted.loc[mask_percep_iva, "debito"].sum())

    # IVA sobre comisiones: cualquier línea que contenga IVA excluyendo percepciones
    mask_iva_gal = udesc.str.contains(r"\bIVA\b") & (~mask_percep_iva)
    iva21 = float(df_sorted.loc[mask_iva_gal, "debito"].sum())

    # Neto de comisiones 21%: si hay IVA, dividir por 0.21
    if iva21 > 0:
        net21 = round(iva21 / 0.21, 2)
    else:
        # Fallback cuando el PDF no discrimina IVA: sumar conceptos de comisión específicos
        mask_comisiones_fallback = (
            udesc.str.contains(r"COM\.?\s*DEP[ÓO]SITO\s+DE\s+CHEQUE") |
            udesc.str.contains(r"COM\.?\s*GESTI[ÓO]N\s+TRANSF\.?FDOS\s+ENTRE\s+BCOS")
        )
        net21 = float(df_sorted.loc[mask_comisiones_fallback, "debito"].sum())
        iva21 = 0.0  # no hay IVA explícito en este caso
    # Galicia no usa 10,5% en estos conceptos (dejar en 0)
    iva105 = 0.0
    net105 = 0.0

# ----- MÉTRICAS EN PANTALLA (idénticas para todos) -----
m1, m2, m3 = st.columns(3)
with m1: st.metric("Neto Comisiones 21%", f"$ {fmt_ar(net21)}")
with m2: st.metric("IVA 21%", f"$ {fmt_ar(iva21)}")
with m3: st.metric("Bruto 21%", f"$ {fmt_ar(net21 + iva21)}")

n1, n2, n3 = st.columns(3)
with n1: st.metric("Neto Comisiones 10,5%", f"$ {fmt_ar(net105)}")
with n2: st.metric("IVA 10,5%", f"$ {fmt_ar(iva105)}")
with n3: st.metric("Bruto 10,5%", f"$ {fmt_ar(net105 + iva105)}")

o1, o2, o3 = st.columns(3)
with o1: st.metric("Percepciones de IVA (RG 3337 / RG 2408)", f"$ {fmt_ar(percep_iva)}")
with o2: st.metric("Ley 25.413", f"$ {fmt_ar(ley_25413)}")
with o3: st.metric("SIRCREB", f"$ {fmt_ar(sircreb)}")

# (el resto del código de descargas/PDF permanece igual)
total_operativo = (
    net21 + iva21 + net105 + iva105 + percep_iva + ley_25413 + sircreb
)
