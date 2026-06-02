# Cómo funciona el pipeline (explicación por fases, con código)

Este documento recorre, paso a paso y con el código real, qué hace `studio_backfill`
desde que lee el Excel hasta que entrega los reportes. Cada sección apunta al
archivo y las líneas para que puedas seguirlo en el editor.

> **Idea general:** cada **fila del Excel es una clase grabada**. El script toma la
> **transcripción** de cada clase, la deja accesible en la nube, le pide a **xAI** que
> la analice, y luego recoge el **PDF** del reporte y lo guarda en un Shared Drive.

---

## Mapa de archivos

| Archivo | Rol |
|---|---|
| `studio_backfill/cli.py` | Punto de entrada. Interpreta el subcomando y llama al orquestador. |
| `studio_backfill/config.py` | Carga la configuración desde `.env_backfill`. |
| `studio_backfill/excel_io.py` | **Lee** el Excel de entrada y **escribe** el Excel de salida. |
| `studio_backfill/drive_reader.py` | Clasifica el enlace de transcripción y **descarga** el texto de Drive. |
| `studio_backfill/gcs_uploader.py` | **Sube** la transcripción a GCS y genera la **signed URL**. |
| `studio_backfill/webhook_client.py` | Arma el payload, lo **firma (HMAC)** y hace el **POST** a xAI. |
| `studio_backfill/pipeline.py` | **Orquestador**: hila todos los pasos de la Fase 1 y la Fase 2. |
| `studio_backfill/reports_client.py` | Le **pide el PDF** al backend de reportes. |
| `studio_backfill/drive_writer.py` | **Sube el PDF** al Shared Drive. |
| `studio_backfill/state.py` | Lleva el **checkpoint** en SQLite (estado de cada fila). |

---

## Diagrama del flujo completo

```
TU SCRIPT (bastion)                          xAI (video-engine)
─────────────────                            ──────────────────
[Fase 1 — encolar]
 leer Excel ─► clasificar ─► descargar de Drive
            ─► subir a GCS ─► firmar signed URL
            ─► POST /webhook (HMAC) ──────────►  webhook (Cloud Run)
                                                 └─ valida HMAC → Pub/Sub → 202
                                                 backend: descarga transcript,
                                                 crea Session, manda ~107 requests
                                                 al Batch API → pipeline=submitted
                                                 collector (cron horario): batch listo
                                                 → analyzed/processed
                                                 ETL (horario): Cloud SQL → BigQuery
                                                 reports backend: BQ → genera PDF
[Fase 2 — recoger]
 collect ─► GET PDF ◄─────────────────────────┘
         ─► subir PDF al Shared Drive → completed
[Fase 3 — salida]
 write-excel ─► Excel + columnas (event_id, pdf_link, status)
```

---

## Cómo arranca todo — `cli.py`

Tú corres, por ejemplo:

```bash
python -m studio_backfill.cli pilot --all
```

El `main()` interpreta el subcomando y enruta a la función correspondiente
(`cli.py:312-319`):

```python
args = p.parse_args(argv)                 # "pilot --all"
_setup_logging(args.verbose)
settings = Settings.load()                # lee .env_backfill
state = _open_state(settings)             # abre state.sqlite
try:
    return args.func(args, settings, state)   # ← llama cmd_pilot / cmd_collect / ...
finally:
    state.close()
```

Cada subcomando se registró con `set_defaults(func=...)` (`cli.py:278-310`):
`pilot → cmd_pilot`, `collect → cmd_collect`, `write-excel → cmd_write_excel`, etc.

---

# FASE 1 — Encolar (mandar a xAI)

La dispara `cmd_pilot` (`cli.py:119`). Para `--all`:

```python
elif args.all:
    excel_rows = list(excel_io.read_rows(settings.excel_path))
...
pipeline = Pipeline(settings)
pipeline.submit_all(state, excel_rows)     # corre la Fase 1 para todas las filas
```

`Pipeline.__init__` (`pipeline.py:50-61`) crea de una vez los clientes que se
reutilizan en todas las filas (Drive, GCS, webhook, reports, drive_writer).

## Paso 1 — Leer el Excel · `excel_io.read_rows` (`excel_io.py:66`)

`read_rows` es un **generador** (`yield`): entrega una fila a la vez. Se ejecuta
cuando alguien la **consume** (el `list(...)` de arriba o un `for`).

```python
def read_rows(excel_path: str) -> Iterator[dict]:
    wb = openpyxl.load_workbook(excel_path, read_only=True, data_only=True)
    ws = wb[wb.sheetnames[0]]                       # solo la primera hoja (ignora "Hoja1")

    rows_iter = ws.iter_rows(values_only=True)
    raw_header = list(next(rows_iter))              # primera fila = encabezados

    # Traduce encabezados (español o inglés) al nombre interno canónico.
    alias = {**{_norm_header(c): c for c in REQUIRED_COLUMNS}, **HEADER_ALIASES}
    header = [alias.get(_norm_header(h), h) for h in raw_header]
    missing = [c for c in REQUIRED_COLUMNS if c not in header]
    if missing:
        raise RuntimeError(f"Excel missing required columns: {missing}")

    position = 0
    for raw in rows_iter:
        if raw is None or all(v is None for v in raw):
            continue                                # salta filas vacías
        position += 1
        row = dict(zip(header, raw))                # {columna: valor}
        for k in ("infrastructureCode", "grade"):
            if k in row:
                row[k] = _denumber(row[k])          # 11769.0 -> "11769", 4.0 -> "4"
        row["_row_position"] = position             # identificador de la fila
        yield row
```

Apoyos:
- `REQUIRED_COLUMNS` (`excel_io.py:17`): las 12 columnas obligatorias (nombres internos).
- `HEADER_ALIASES` (`excel_io.py:37`): mapa español → interno (lo agregamos para tu plantilla).
- `_norm_header` (`excel_io.py:53`): quita tildes/mayúsculas/espacios para comparar encabezados.
- `_denumber` (`excel_io.py:59`): limpia los `.0` que Excel pone a los números.

**Salida:** un diccionario por fila, p. ej.
`{'infrastructureCode': '11769', 'teacher': 'VALENCIA MEZA, ...', 'transcriptLink': 'https://docs.google.com/...', '_row_position': 1, ...}`.
La columna `Observación` se conserva con su nombre (no se usa en el procesamiento).

## Paso 2 — Clasificar la fila · `drive_reader.classify` (`drive_reader.py:95`)

Por cada fila, `submit_row` (`pipeline.py:64`) primero clasifica el par de enlaces
(transcripción/video) en **uno de 8 casos**:

```python
r = drive_reader.classify(
    transcript_link=excel_row.get("transcriptLink"),
    video_link=excel_row.get("videoLink"),
    drive=self.drive,
)
```

`classify` normaliza los enlaces (trata `"Sin dato"` como vacío) y, según el tipo de
URL, decide el caso (`drive_reader.py:109-196`):

| Caso | Condición | Qué es |
|---|---|---|
| **A** | `transcriptLink` es Google Doc y ≠ video | Doc directo (lo más común) |
| **B** | `/file/d/...` ≠ video | Archivo Drive (mime se averigua después) |
| **C** | `/folders/...` ≠ video | Carpeta → se busca un Doc/.docx/.txt adentro |
| **D1/D2/D3** | transcript == video (carpeta/archivo/Doc) | Mismo enlace en ambas columnas |
| **E** | enlace vacío o "Sin dato" | **Se salta** (no hay transcripción) |
| **F** | URL de formato no soportado (sheet, redirect, etc.) | **Se salta** |

La detección del tipo de URL está en `_identify_url` (`drive_reader.py:75`) con las
regex `_RE_DOC` / `_RE_FILE` / `_RE_FOLDER` (`drive_reader.py:38-46`). Para carpetas
(C/D1) se mira adentro con `_pick_transcript_from_folder` (`drive_reader.py:199`),
que prefiere **Google Doc > .docx > .txt**.

En `submit_row`, los casos que no se pueden procesar se marcan y se saltan
(`pipeline.py:86-109`): `E → skipped_no_link`, `F → skipped_unsupported_format`,
carpeta sin transcripción → `skipped_no_transcript_in_folder`, archivo que resultó
ser `.mp4` → `skipped_no_transcript`.

## Paso 3 — Descargar la transcripción de Drive · `drive_reader.download_transcript` (`drive_reader.py:248`)

Para los casos válidos, se baja el texto (`pipeline.py:114-117`):

```python
bytes_, content_type, extension = drive_reader.download_transcript(
    self.drive, r.file_id, mime
)
```

`download_transcript` normaliza el formato según el mime (`drive_reader.py:267-286`):

```python
if mime_hint == MIME_GOOGLE_DOC:
    req = drive.files().export_media(fileId=file_id, mimeType=MIME_DOCX)   # Doc -> .docx
elif mime_hint == MIME_DOCX:
    req = drive.files().get_media(fileId=file_id, supportsAllDrives=True)  # .docx tal cual
elif mime_hint == MIME_TXT:
    req = drive.files().get_media(fileId=file_id, supportsAllDrives=True)  # .txt tal cual
...
buf = io.BytesIO()
downloader = MediaIoBaseDownload(buf, req)
done = False
while not done:
    _, done = downloader.next_chunk()
return buf.getvalue(), content_type, ext
```

El cliente de Drive (`make_drive_client`, `drive_reader.py:231`) usa la **service
account** con scope `drive.readonly` (la llave `auditoria-clases-sa-key.json`).
Si falla la descarga → `failed_drive_read` (`pipeline.py:118-121`).

## Paso 4 — Subir a GCS + firmar la URL · `gcs_uploader.GcsUploader.upload` (`gcs_uploader.py:45`)

xAI no recibe el texto; recibe un **enlace para descargarlo**. Por eso se sube a un
bucket y se genera una **signed URL** (`pipeline.py:124-132`):

```python
gcs_uri, signed_url = self.gcs.upload(
    bytes_, event_id, content_type=content_type, extension=extension,
)
```

```python
def upload(self, content, event_id, content_type=None, extension="docx"):
    blob_name = self.blob_path(event_id, ext=extension)
    blob = self.bucket.blob(blob_name)
    blob.upload_from_string(content, content_type=...)
    gcs_uri = f"gs://{self.bucket.name}/{blob_name}"
    signed_url = self._sign(blob)                 # URL temporal firmada (v4)
    return gcs_uri, signed_url
```

La firma de la URL (`_sign`, `gcs_uploader.py:64`) funciona distinto según dónde corra:
- **En el bastion (GCE):** las credenciales no traen llave privada, así que firma vía
  **IAM `signBlob`** (pasa `service_account_email` + `access_token`).
- **Local con llave SA:** firma offline con la llave privada.

> Este paso es justo el que requirió los permisos IAM de la SA del bastion
> (`storage.objectAdmin` en el bucket + `serviceAccountTokenCreator` para firmar).

## Paso 5 — Armar el payload, firmar y POST · `pipeline._build_payload` + `webhook_client` (`pipeline.py:178`, `webhook_client.py:42`)

Se construye el "mensaje" para xAI a partir de las columnas (`pipeline.py:190-206`):

```python
return build_payload(
    event_id=self._event_id(row_position),    # f"{EVENT_ID_PREFIX}-{N}" — prefijo configurable
    teacher_code=f"studio-teacher-{slug(excel_row.get('teacher'))}",
    coach_code=f"studio-tutor-{slug(excel_row.get('coach'))}",
    school_code=f"studio-school-{excel_row.get('infrastructureCode')}",
    grade=str(excel_row.get("grade") or self.settings.default_grade),
    subject=str(excel_row.get("subject") or "No informado"),
    recorded_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    signed_url=signed_url,
    section=..., shift=...,
    teacher_name=_clean_str(...), coach_name=..., school_name=..., ...
)
```

- `slug` (`pipeline.py:23`) convierte nombres a códigos: `"LÓPEZ, CARLOS"` → `LOPEZ-CARLOS`.
- El `event_id` se arma en `Pipeline._event_id` (`pipeline.py`): `f"{EVENT_ID_PREFIX}-{N}"`, con
  `EVENT_ID_PREFIX` configurable (default `studio-row`) y `N = _row_position`. Es la **clave de
  idempotencia**: cambiar el prefijo (ej. `studio-row-v2`) hace que xAI lo trate como sesión nueva.

El POST con firma HMAC está en `WebhookClient.post` (`webhook_client.py:42-60`):

```python
body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
ts = str(int(time.time()))
headers = {
    "Content-Type": "application/json",
    "X-Webhook-Signature": self._sign(ts, body),   # sha256=<hmac>
    "X-Webhook-Timestamp": ts,
}
resp = requests.post(self.url, data=body, headers=headers, timeout=self.timeout)
```

La firma (`_sign`, `webhook_client.py:37`):

```python
signing_string = f"{timestamp}.".encode() + body   # OJO: body EXACTO, byte a byte
sig = hmac.new(self._secret, signing_string, hashlib.sha256).hexdigest()
return f"sha256={sig}"
```

Esto coincide exactamente con la validación del servidor en
`video-engine/webhook/src/security/hmac.py:45-58` (tolerancia ±300s en el timestamp).
xAI responde **202** y el script guarda el estado `submitted` (`pipeline.py:148-153`).

## Paso 6 — Estado + throttle · `state.py` y `submit_all` (`pipeline.py:160`)

Cada fila se registra en `state.sqlite` con su estado (`pending`, `submitted`,
`skipped_*`, `failed_*`, `completed`). Esto da **idempotencia** (no reenvía lo ya
enviado, `pipeline.py:75-77`) y permite **reanudar**.

El control de velocidad está en `submit_all` (`pipeline.py:160-176`): solo espera
entre filas que **realmente** pegan al webhook (las saltadas pasan al instante):

```python
delay = 1.0 / max(self.settings.webhook_rps, 0.0001)   # WEBHOOK_RPS=0.2 → 5s
...
did_post = self.submit_row(state, row)
if did_post:
    last_webhook = time.monotonic()
```

---

# FASE 2 — Recoger los PDFs · `pipeline.collect_once` (`pipeline.py:209`)

La dispara `cmd_collect` (`cli.py:156`) con `collect --once` (una pasada) o `collect`
(loop hasta drenar, `collect_until_drained`, `pipeline.py:266`).

`collect_once` toma las filas en `submitted` / `failed_pdf_404` / `failed_pdf_fetch`
y, por cada una, le pide el PDF al backend de reportes (`pipeline.py:225-262`):

```python
resp = self.reports.get_pdf(row.event_id)            # GET /api/reports/session-pdf

if resp.status == 200 and resp.signed_pdf_url:
    pdf_bytes = self.reports.download_signed(resp.signed_pdf_url)
    drive_id, drive_link = self.drive_writer.upload(pdf_bytes, row.event_id)
    state.update(row.event_id, pdf_drive_id=..., pdf_drive_link=..., state="completed")
elif resp.status == 422 and resp.is_terminal_422():
    state.update(row.event_id, state="failed_pdf_analysis", ...)   # xAI falló (terminal)
elif resp.status == 422:
    state.update(row.event_id, state="submitted")                  # aún procesando
elif resp.status == 404:
    state.update(row.event_id, state="failed_pdf_404", ...)        # aún no en BigQuery
elif resp.status == 429:
    time.sleep(30)                                                 # rate limited
```

- `ReportsClient.get_pdf` (`reports_client.py:52`) hace el GET y empaqueta el resultado.
- `is_terminal_422` (`reports_client.py:27`) distingue "todavía procesando" de "falló de
  verdad" (`pipeline_status=failed` o `reason=data_incomplete`).
- `DriveWriter.upload` (`drive_writer.py:22`) sube el PDF como `reporte_<event_id>.pdf`
  al Shared Drive y devuelve `(id, webViewLink)`.

> **Importante:** que xAI responda 202 en la Fase 1 no significa que el PDF exista. La
> cadena es asíncrona (Batch API → collector horario → ETL horario → reportes). Por eso
> `collect` puede recibir 404/422 varias veces antes de que el PDF esté listo.

---

# FASE 3 — Excel de salida · `excel_io.write_output_excel` (`excel_io.py`)

La dispara `cmd_write_excel` (`cli.py:168`). Lee del `state.sqlite` el resultado de
cada fila y reescribe el Excel **original** agregando 3 columnas al final:
`event_id_xai`, `pdf_drive_link`, `backfill_status`. Las columnas originales (incluida
`Observación`) se conservan, y se agrega una hoja `_backfill_meta` con el SHA del
archivo fuente para trazabilidad.

```python
ws.cell(row=1, column=start_col + 0, value="event_id_xai")
ws.cell(row=1, column=start_col + 1, value="pdf_drive_link")
ws.cell(row=1, column=start_col + 2, value="backfill_status")
```

---

## Estados posibles (máquina de estados)

| Estado | Significa | ¿Reintentable? |
|---|---|---|
| `completed` | PDF en el Shared Drive | No |
| `submitted` | Posteado a xAI, esperando análisis | Pasa solo a `completed` o `failed_*` |
| `skipped_no_link` | `transcriptLink` vacío / "Sin dato" (caso E) | No |
| `skipped_unsupported_format` | Formato no soportado (caso F) | No |
| `skipped_no_transcript` | Archivo era `.mp4` (B/D2) | No |
| `skipped_no_transcript_in_folder` | Carpeta (C/D1) sin Doc/.docx/.txt | No |
| `failed_drive_read` | Drive devolvió error al leer | Sí |
| `failed_gcs_upload` | Falló subir el transcript a GCS | Sí |
| `failed_webhook` | xAI rechazó el POST | Sí |
| `failed_pdf_404` | Aún no está en BigQuery (ETL pendiente) | Sí (polling) |
| `failed_pdf_fetch` | 5xx del backend de reportes | Sí (hasta MAX_ATTEMPTS) |
| `failed_pdf_analysis` | xAI marcó la sesión como fallida | No |
| `failed_drive_upload` | Falló subir el PDF al Shared Drive | Sí |
| `failed_timeout_pending_analysis` | >48h en `submitted` | Manual |
| `failed_too_many_attempts` | Excedió reintentos automáticos | Manual |

---

## Apéndice — Contrato del webhook de xAI (para replicar)

Quien quiera disparar xAI solo necesita hacer este `POST` (lo hace `webhook_client.py`):

- **URL:** `WEBHOOK_URL` (de Secret Manager; ya incluye `/webhook`).
- **Headers:** `Content-Type: application/json`, `X-Webhook-Timestamp: <unix>`,
  `X-Webhook-Signature: sha256=<hmac>`.
- **Firma:** `HMAC_SHA256(secret, f"{timestamp}." + body_bytes)`, hex. El `body` firmado
  debe ser **exactamente** el enviado (JSON compacto). Tolerancia ±300s.
- **Body:**
  ```json
  {
    "kwargs": {
      "signed_url": "https://storage.googleapis.com/.../transcript.txt?X-Goog-Signature=...",
      "params": {
        "event_id": "studio-row-1",
        "teacher_code": "studio-teacher-...",
        "coach_code": "studio-tutor-...",
        "school_code": "studio-school-11769",
        "grade": "4",
        "subject": "Lenguaje",
        "recorded_at": "2026-06-01T23:10:00+00:00",
        "section": "A",
        "shift": "Matutino"
      }
    }
  }
  ```
- **Idempotencia:** repetir un `event_id` existente → xAI lo salta. El prefijo del `event_id`
  es configurable vía `EVENT_ID_PREFIX` (default `studio-row`); cambiarlo fuerza reproceso.
- **Costo/escala:** cada sesión genera ~107 requests en el Batch API de xAI
  (límites a nivel equipo). Una prueba de carga debe contemplar esa amplificación,
  usar un entorno no-prod, transcripts sintéticos y `event_id` únicos.
