# ===== Credicoop: parser (UN solo movimiento por renglón; si hay 2 montos, el de la derecha es SALDO) =====
# NOTA: ya NO exigimos que la fecha esté al inicio del renglón.
DATE_ANY = DATE_RE
ONLY_DIGITS = re.compile(r'^\d{3,}$')

def _group_lines_words(page, ytol=3.5):
    """Agrupa las 'words' del PDF en renglones por coordenada Y (tolerancia más amplia)."""
    words = page.extract_words(extra_attrs=["x0","x1","top","bottom","text"])
    if not words:
        return []
    words.sort(key=lambda w: (round(w["top"]/ytol), w["x0"]))
    rows, cur, band = [], [], None
    for w in words:
        b = round(w["top"]/ytol)
        if band is None or b == band:
            cur.append(w)
        else:
            rows.append(cur); cur = [w]
        band = b
    if cur: rows.append(cur)
    return rows

def _cluster_two(xs):
    """Devuelve (c_izq, c_der) o None si no hay suficientes puntos para distinguir columnas."""
    xs = sorted(float(x) for x in xs if x is not None)
    if len(xs) < 4:
        return None
    # inicializo por percentiles 30/70
    c1, c2 = np.quantile(xs, [0.3, 0.7])
    for _ in range(25):
        g1 = [x for x in xs if abs(x-c1) <= abs(x-c2)]
        g2 = [x for x in xs if abs(x-c2) <  abs(x-c1)]
        if not g1 or not g2: break
        nc1, nc2 = float(np.mean(g1)), float(np.mean(g2))
        if abs(nc1-c1) < 0.2 and abs(nc2-c2) < 0.2: break
        c1, c2 = nc1, nc2
    return (min(c1, c2), max(c1, c2))

def credicoop_parse_from_words(file_like):
    """
    Reglas:
      - Cada renglón con fecha tiene como máximo 1 movimiento.
      - Si hay 2 importes: el de la DERECHA es SALDO DEL DÍA -> se ignora; el otro es el movimiento.
      - Si hay 1 importe: es el movimiento.
      - Débito/Crédito:
          * Si podemos inferir columnas por 'x0' de muchos renglones, usamos centro izquierdo=DEB, centro derecho=CRED.
          * Si no, fallback por keywords en descripción (ACRED/CRÉDIT/CREDITO/`CR ` => crédito; si no => débito).
      - Líneas sin fecha y sin importes: se pegan a la descripción anterior.
      - El saldo corrido se reconstruye desde SALDO ANTERIOR si aparece.
    """
    with pdfplumber.open(_rewind(file_like)) as pdf:
        # 1) Buscar SALDO ANTERIOR y SALDO FINAL en texto plano (encabezado/pie)
        full_text_lines = []
        for p in pdf.pages:
            t = p.extract_text() or ""
            full_text_lines += [l for l in t.splitlines() if l.strip()]

        saldo_anterior = np.nan
        fecha_cierre = pd.NaT
        saldo_final_pdf = np.nan

        for ln in full_text_lines:
            U = ln.upper()
            if "SALDO ANTERIOR" in U and _only_one_amount(ln):
                v = _first_amount_value(ln)
                if not np.isnan(v): saldo_anterior = v
            if SALDO_FINAL_PREFIX.match(ln) and _only_one_amount(ln):
                d = DATE_RE.search(ln)
                if d:
                    fecha_cierre = pd.to_datetime(d.group(0), dayfirst=True, errors="coerce")
                    saldo_final_pdf = _first_amount_value(ln)

        # 2) Primera pasada: recolecto posiciones x0 de movimientos (excluyendo el saldo derecho)
        mov_xs = []
        all_rows = []
        for page in pdf.pages:
            for words in _group_lines_words(page, ytol=3.5):
                all_rows.append(words)
                amts = []
                for w in words:
                    raw = w["text"].replace("\u00A0"," ").replace("\u202F"," ").strip()
                    m = MONEY_RE.search(raw)
                    if m:
                        amts.append((float(w["x0"]), m.group(0)))
                if len(amts) == 1:
                    mov_xs.append(amts[0][0])
                elif len(amts) >= 2:
                    amts_sorted = sorted(amts, key=lambda t: t[0])
                    for x0, _ in amts_sorted[:-1]:  # todo excepto el más a la derecha (saldo)
                        mov_xs.append(x0)

        # infiero columnas (si hay suficientes datos)
        col_centers = _cluster_two(mov_xs)  # (x_izq, x_der) o None

        def guess_side(x0, descU):
            """Devuelve 'deb' o 'cre'."""
            if col_centers and x0 is not None:
                left, right = col_centers
                return 'deb' if abs(x0-left) <= abs(x0-right) else 'cre'
            # fallback por keywords
            return 'cre' if any(k in descU for k in ("ACRED","CRÉDIT","CREDITO","CR ")) else 'deb'

        # 3) Segunda pasada: parseo renglón a renglón
        rows_out = []
        last_idx = None

        for words in all_rows:
            # armo texto del renglón (tolerante a NBSP / espacios finos)
            line_text = " ".join(w["text"] for w in words).replace("\u00A0"," ").replace("\u202F"," ").strip()
            has_amount = bool(MONEY_RE.search(line_text))

            # >>> diferencia clave: aceptar fecha en cualquier parte del renglón
            mdate = DATE_ANY.search(line_text)
            if mdate:
                fecha = pd.to_datetime(mdate.group(0), dayfirst=True, errors="coerce")

                # ubico índice de la word que contiene la fecha para separar a partir de ahí
                di = None
                for i, w in enumerate(words):
                    if DATE_RE.search(w["text"] or ""):  # search, no fullmatch
                        di = i; break
                after_date = words[di+1:] if di is not None else words  # si no la encuentro, no corto

                # combte opcional (primer token numérico seguido)
                combte = None
                if after_date and ONLY_DIGITS.fullmatch(after_date[0]["text"]):
                    combte = after_date[0]["text"].strip()
                    after_date = after_date[1:]

                # separo descripción y montos con posición
                desc_parts, amts = [], []
                for w in after_date:
                    raw = w["text"].replace("\u00A0"," ").replace("\u202F"," ").strip()
                    m = MONEY_RE.search(raw)
                    if m:
                        amts.append((float(w["x0"]), m.group(0)))
                    else:
                        desc_parts.append(w["text"])
                desc = " ".join(desc_parts).strip()
                descU = desc.upper()

                # determinar el ÚNICO movimiento de la línea
                mov_val = 0.0
                mov_x0  = None
                if len(amts) == 1:
                    mov_x0, mov_txt = amts[0]
                    mov_val = float(normalize_money(mov_txt))
                elif len(amts) >= 2:
                    # el más a la derecha es saldo => ignoro; el más a la izquierda es el movimiento
                    amts_sorted = sorted(amts, key=lambda t: t[0])
                    mov_x0, mov_txt = amts_sorted[0]
                    mov_val = float(normalize_money(mov_txt))
                else:
                    mov_val = 0.0; mov_x0 = None  # sin monto en la línea con fecha

                # asigno a débito o crédito (nunca ambos)
                deb = cre = 0.0
                side = guess_side(mov_x0, descU)
                if side == 'cre':
                    cre = mov_val
                else:
                    deb = mov_val

                rows_out.append({
                    "fecha": fecha,
                    "combte": combte,
                    "descripcion": desc,
                    "debito": float(deb),
                    "credito": float(cre),
                })
                last_idx = len(rows_out) - 1

            else:
                # renglón de continuación: sin fecha y sin importes => se pega a la descripción anterior
                if (not has_amount) and last_idx is not None:
                    s = line_text.strip()
                    if s:
                        rows_out[last_idx]["descripcion"] = (rows_out[last_idx]["descripcion"] + " " + s).strip()

        df = pd.DataFrame(rows_out)
        if df.empty:
            return df, fecha_cierre, saldo_final_pdf, saldo_anterior

        df["desc_norm"] = df["descripcion"].map(normalize_desc)

        # 4) Reconstruyo saldo corrido SOLO con débito/crédito
        running = float(saldo_anterior) if not np.isnan(saldo_anterior) else 0.0
        saldos = []
        for _, r in df.iterrows():
            running = running + float(r["credito"]) - float(r["debito"])
            saldos.append(running)
        df["saldo"] = saldos

        return df, fecha_cierre, saldo_final_pdf, saldo_anterior
