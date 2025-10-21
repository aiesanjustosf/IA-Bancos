# === Reemplazo COMPLETO de credicoop_parse_from_pdf ===
def credicoop_parse_from_pdf(file_like):
    """
    Parser por columnas con detección DINÁMICA de las 3 columnas numéricas
    (débito, crédito, saldo) usando k-means 1D sobre los X de los importes.
    - Nunca mezcla débito/crédito.
    - 'SALDO ANTERIOR' sale del saldo de esa fila.
    - Líneas sin fecha ni importes -> se anexan a la descripción anterior.
    - Si en un renglón aparece solo un importe NUMÉRICO, se lo ubica en la
      columna más cercana de las 3 descubiertas para esa página.
    """
    rows = []
    saldo_anterior = np.nan
    cierre_fecha, saldo_final_pdf = pd.NaT, np.nan

    with pdfplumber.open(_rewind(file_like)) as pdf:
        for p in pdf.pages:
            words = p.extract_words(extra_attrs=["x0","x1","top","bottom"])
            if not words:
                continue

            # 1) Candidatos numéricos (con coma y 2 decimales) en mitad derecha
            num_words = []
            for w in words:
                if w["x0"] > p.width * 0.35 and MONEY_RE.fullmatch(w["text"].replace(" ", "")):
                    num_words.append(((w["x0"] + w["x1"]) / 2.0))
            if len(num_words) == 0:
                # página sin tabla; puede ser portada/avisos
                continue

            # 2) Estimar centros de columnas (izq->der = deb, cred, saldo)
            cx = _kmeans_1d(num_words, k=3)
            cx = sorted(cx)
            # rangos generosos para capturar números en cada columna
            tol = max(10.0, 0.02 * p.width)
            col_ranges = [(cx[0]-tol, cx[0]+tol), (cx[1]-tol, cx[1]+tol), (cx[2]-tol, cx[2]+tol)]

            # 3) Agrupar renglones por Y
            by_rows = _group_rows_by_y(words, ytol=2.0)

            last_idx = None
            for row in by_rows:
                # armar textos por columna
                left_tokens, num_tokens = [], {"deb": [], "cre": [], "sal": []}
                for w in row:
                    xmid = (w["x0"] + w["x1"]) / 2.0
                    txt = w["text"].strip()

                    # ¿es monto?
                    if MONEY_RE.fullmatch(txt.replace(" ", "")):
                        if col_ranges[0][0] <= xmid <= col_ranges[0][1]:
                            num_tokens["deb"].append(txt)
                        elif col_ranges[1][0] <= xmid <= col_ranges[1][1]:
                            num_tokens["cre"].append(txt)
                        elif col_ranges[2][0] <= xmid <= col_ranges[2][1]:
                            num_tokens["sal"].append(txt)
                        else:
                            # fuera de ventana: acercarlo a la más próxima
                            d = [abs(xmid - c) for c in cx]
                            idx = int(np.argmin(d))
                            ("deb","cre","sal")[idx]  # no-op, por claridad
                            if idx == 0: num_tokens["deb"].append(txt)
                            elif idx == 1: num_tokens["cre"].append(txt)
                            else: num_tokens["sal"].append(txt)
                    else:
                        left_tokens.append(txt)

                left_txt = " ".join(left_tokens).strip()
                deb_txt  = num_tokens["deb"][-1] if num_tokens["deb"] else ""
                cre_txt  = num_tokens["cre"][-1] if num_tokens["cre"] else ""
                sal_txt  = num_tokens["sal"][-1] if num_tokens["sal"] else ""

                U = left_txt.upper()

                # Saldos explícitos (encabezado/cola)
                if "SALDO ANTERIOR" in U and sal_txt:
                    v = normalize_money(sal_txt)
                    if not np.isnan(v): saldo_anterior = v
                    continue
                if U.startswith("SALDO AL") and sal_txt:
                    m = DATE_RE.search(left_txt)
                    if m:
                        cierre_fecha = pd.to_datetime(m.group(0), dayfirst=True, errors="coerce")
                    v = normalize_money(sal_txt)
                    if not np.isnan(v): saldo_final_pdf = v
                    continue

                # Movimiento con fecha
                md = DATE_RE.match(left_txt)
                if md:
                    fecha = pd.to_datetime(md.group(0), dayfirst=True, errors="coerce")
                    tail  = left_txt[md.end():].strip()

                    m2 = re.match(r'^(\d{3,})?\s*(.*)$', tail)
                    combte = (m2.group(1) or "").strip() if m2 else ""
                    desc   = (m2.group(2) if m2 else tail).strip()

                    deb = normalize_money(deb_txt) if deb_txt else np.nan
                    cre = normalize_money(cre_txt) if cre_txt else np.nan
                    sal = normalize_money(sal_txt) if sal_txt else np.nan

                    # Si por ruido quedaron 2 montos en deb/cre, aplica regla semántica
                    if pd.notna(deb) and pd.notna(cre):
                        key = desc.upper()
                        prefer_deb = any(k in key for k in ("PAGO","DEBIT","COMISION","IMPUESTO","SERVICIO","PERCEPCION","DEBIN"))
                        if prefer_deb: cre = np.nan
                        else: deb = np.nan

                    rows.append({
                        "fecha": fecha,
                        "combte": combte or None,
                        "descripcion": desc,
                        "debito": float(deb) if pd.notna(deb) else 0.0,
                        "credito": float(cre) if pd.notna(cre) else 0.0,
                        "saldo": float(sal) if pd.notna(sal) else np.nan,
                    })
                    last_idx = len(rows) - 1
                else:
                    # Continuación de descripción (sin fecha y sin importes)
                    has_importe = deb_txt or cre_txt or sal_txt
                    if (not has_importe) and last_idx is not None:
                        s = left_txt.strip()
                        if s:
                            rows[last_idx]["descripcion"] = (rows[last_idx]["descripcion"] + " " + s).strip()

    df = pd.DataFrame(rows)
    if df.empty:
        return df, cierre_fecha, saldo_final_pdf, saldo_anterior

    # Reconstruir saldo corrido
    if not np.isnan(saldo_anterior):
        running = float(saldo_anterior)
    else:
        first = df.iloc[0]
        base = float(first.get("saldo", np.nan)) if pd.notna(first.get("saldo", np.nan)) else 0.0
        running = base - float(first.get("credito",0.0)) + float(first.get("debito",0.0))

    sal_calc = []
    for _, r in df.iterrows():
        if pd.isna(r["saldo"]):
            running = running + float(r["credito"]) - float(r["debito"])
        else:
            running = float(r["saldo"])
        sal_calc.append(running)
    df["saldo"] = sal_calc

    if np.isnan(saldo_final_pdf):
        saldo_final_pdf = float(df["saldo"].iloc[-1])

    df["desc_norm"] = df["descripcion"].map(normalize_desc)
    return df, cierre_fecha, saldo_final_pdf, saldo_anterior
