# Documentación técnica — `studio_backfill`

> Documento de referencia exhaustivo. Está pensado para que un dev que **nunca vio este proyecto** pueda entender qué hace, cómo arranca, por qué cada pieza existe, y cuáles son los matices no obvios del diseño.

---

## Índice

1. [¿Qué es este proyecto?](#1-qué-es-este-proyecto)
2. [Vista de pájaro: el flujo completo](#2-vista-de-pájaro-el-flujo-completo)
3. [El entrypoint: cómo arranca el programa](#3-el-entrypoint-cómo-arranca-el-programa)
4. [Configuración (`config.py` + `.env_backfill`)](#4-configuración-configpy--env_backfill)
5. [Lectura del Excel (`excel_io.py`)](#5-lectura-del-excel-excel_iopy)
6. [El resolver de 8 casos (`drive_reader.py`)](#6-el-resolver-de-8-casos-drive_readerpy)
7. [Fase 1 — Encolar (`pilot`)](#7-fase-1--encolar-pilot)
8. [Fase 2 — Recoger PDFs (`collect`)](#8-fase-2--recoger-pdfs-collect)
9. [Fase 3 — Excel de salida (`write-excel`)](#9-fase-3--excel-de-salida-write-excel)
10. [El checkpoint SQLite (`state.py`)](#10-el-checkpoint-sqlite-statepy)
11. [La máquina de estados completa](#11-la-máquina-de-estados-completa)
12. [Clientes externos: GCS, webhook, reportes, Drive writer](#12-clientes-externos-gcs-webhook-reportes-drive-writer)
13. [Comandos del CLI, uno por uno](#13-comandos-del-cli-uno-por-uno)
14. [Matices y decisiones de diseño no obvias](#14-matices-y-decisiones-de-diseño-no-obvias)
15. [Estructura de archivos](#15-estructura-de-archivos)

---

## 1. ¿Qué es este proyecto?

`studio_backfill` es una herramienta **one-shot** (de un solo uso, no un servicio permanente) cuyo trabajo es:

> Tomar un Excel con **~9,386 filas** de clases grabadas en "Studio" (cada fila tiene links a un video y a una transcripción en Google Drive), enviar cada transcripción a la **pipeline de análisis de xAI** (que evalúa la clase con IA y genera un reporte), esperar a que xAI termine, descargar el **PDF del reporte**, subirlo a un **Shared Drive** de Google, y finalmente producir un **Excel de salida** igual al original pero con 3 columnas nuevas: el ID del evento, el link al PDF, y el estado final de cada fila.

En otras palabras: es un **backfill** — datos históricos que nunca pasaron por la pipeline normal de xAI se "rellenan" hacia atrás, masivamente.

### Restricciones que moldearon el diseño

- **El proceso tarda horas o días.** xAI procesa en modo batch; entre que se envía una transcripción y existe el PDF pueden pasar 48 horas. Por eso el flujo está partido en **fases independientes** que se corren por separado.
- **Puede fallar a mitad de camino** (corte de SSH, crash, rate-limit). Por eso **todo el progreso se persiste en un SQLite local** (`state.sqlite`) y cada operación es **idempotente**: correr el mismo comando dos veces no duplica trabajo.
- **El webhook de xAI tiene rate-limit implícito** (es una Cloud Function de DEV con alertas de 4xx). Por eso hay un throttle configurable (`WEBHOOK_RPS`).
- **Los datos de origen son sucios.** Los links de transcripción vienen en ~8 formatos distintos (Google Doc, archivo suelto, carpeta, "Sin dato", spreadsheets que no sirven, etc.). Por eso existe un **resolver de casos** (`drive_reader.classify`) que es el corazón clasificatorio del sistema.

---

## 2. Vista de pájaro: el flujo completo

```
                         ┌────────────────────────────────────────────────┐
                         │       studio_results_final.xlsx (~9,386 filas) │
                         └────────────────────┬───────────────────────────┘
                                              │ excel_io.read_rows()
                                              ▼
 FASE 1 (pilot)          ┌────────────────────────────────────────────────┐
 "encolar"               │ Por cada fila:                                 │
                         │  1. classify() → caso A..F                     │
                         │  2. Si E/F/video-sin-transcript → skip         │
                         │  3. Descargar transcript de Google Drive       │
                         │  4. Subirlo a GCS + generar signed URL         │
                         │  5. POST firmado (HMAC) al webhook de xAI      │
                         │  6. Estado → "submitted" en state.sqlite       │
                         └────────────────────┬───────────────────────────┘
                                              │   (xAI procesa en batch,
                                              │    horas o días…)
                                              ▼
 FASE 2 (collect)        ┌────────────────────────────────────────────────┐
 "recoger"               │ Por cada fila en "submitted":                  │
                         │  1. GET /api/reports/session-pdf?event_id=…    │
                         │  2. 200 → bajar PDF → subirlo al Shared Drive  │
                         │     → estado "completed"                       │
                         │  3. 404/422 "procesando" → reintentar después  │
                         │  4. 422 terminal → "failed_pdf_analysis"       │
                         │  (loop con sleep de 15 min hasta vaciar)       │
                         └────────────────────┬───────────────────────────┘
                                              ▼
 FASE 3 (write-excel)    ┌────────────────────────────────────────────────┐
 "reportar"              │ Copia el Excel original y agrega 3 columnas:   │
                         │   event_id_xai | pdf_drive_link | backfill_status │
                         │ + hoja "_backfill_meta" con el SHA-256 fuente  │
                         └────────────────────────────────────────────────┘
```

Las tres fases comparten **un solo punto de verdad**: `state.sqlite`. La Fase 1 escribe filas ahí; la Fase 2 las lee y las va promoviendo de estado; la Fase 3 solo lee y vuelca al Excel.

### Sistemas externos involucrados

| Sistema | Rol | Credencial usada |
|---|---|---|
| **Google Drive (lectura)** | De ahí se bajan las transcripciones de los tutores | Service Account key (`auditoria-clases-sa-key.json`), scope `drive.readonly` |
| **GCS** (bucket `videos-and-transcripts-bucket`) | Almacén intermedio: el transcript se sube ahí y se le genera una signed URL que xAI puede descargar sin credenciales | **ADC** (Application Default Credentials — la SA adjunta a la VM bastion, o credencial local) |
| **Webhook xAI** (Cloud Function) | Recibe el evento `{signed_url, params}` y encola el análisis | HMAC-SHA256 con `WEBHOOK_SECRET` |
| **Backend de reportes** (Cloud Run) | Expone `GET /session-pdf` que devuelve la URL firmada del PDF cuando el análisis terminó | Sin auth (endpoint DEV) |
| **Google Drive (escritura)** | Shared Drive destino donde quedan los PDFs (`reporte_<event_id>.pdf`) | La misma SA key, scope `drive` completo |

Nótese el detalle de **doble credencial**: Drive usa la SA key explícita, pero GCS usa ADC. Esto es deliberado — ver [§14](#14-matices-y-decisiones-de-diseño-no-obvias).

---

## 3. El entrypoint: cómo arranca el programa

Hay dos formas de invocar el programa, ambas equivalentes:

```bash
python -m studio_backfill          # vía __main__.py
python -m studio_backfill.cli ...  # vía cli.py directamente (la usada en el README)
```

### `__main__.py` — 5 líneas

```python
from .cli import main
import sys

if __name__ == "__main__":
    sys.exit(main())
```

Solo delega en `cli.main()` y propaga su `int` de retorno como **exit code** del proceso (0 = OK, 1 = no encontrado, 2 = error de uso).

### `cli.main()` — el verdadero entrypoint (`cli.py:273`)

Es el corazón del arranque. Hace exactamente esto, en orden:

```python
def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="studio_backfill")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)   # ① subcomandos obligatorios
    # ... registra 9 subcomandos, cada uno con set_defaults(func=cmd_XXX) ②

    args = p.parse_args(argv)
    _setup_logging(args.verbose)        # ③ logging a stderr (INFO o DEBUG)
    settings = Settings.load()          # ④ carga y VALIDA el .env_backfill
    state = _open_state(settings)       # ⑤ abre/crea state.sqlite (WAL mode)
    try:
        return args.func(args, settings, state)   # ⑥ despacha al handler
    finally:
        state.close()                   # ⑦ cierra SQLite pase lo que pase
```

Puntos clave para entenderlo:

1. **`required=True` en los subparsers**: ejecutar sin subcomando es un error de argparse (no hay comando "default").
2. **Patrón `set_defaults(func=...)`**: cada subparser guarda su función handler en `args.func`. Es el idiom estándar de argparse para CLIs multi-comando — `main()` no necesita un `if/elif` gigante; simplemente llama `args.func(args, settings, state)`.
3. **El logging va con formato `%(asctime)s %(levelname)s [%(name)s] %(message)s`**; con `-v` se baja a DEBUG.
4. **`Settings.load()` falla rápido**: si falta una variable obligatoria (`WEBHOOK_URL`, `SA_KEY_PATH`, etc.) lanza `RuntimeError` *antes* de tocar nada. Esto significa que **todos los comandos, incluso `status`, requieren un `.env_backfill` completo**.
5. **Cada handler recibe la misma firma**: `(args, settings, state) -> int`. El `int` retornado es el exit code.
6. **`finally: state.close()`** garantiza que el SQLite se cierre limpio incluso si el handler explota.

Los 9 subcomandos registrados y su handler:

| Subcomando | Handler | Propósito |
|---|---|---|
| `find-pilot-rows` | `cmd_find_pilot_rows` | Explorar el Excel: distribución de casos + filas piloto sugeridas |
| `pilot` | `cmd_pilot` | **Fase 1** — enviar filas al webhook |
| `collect` | `cmd_collect` | **Fase 2** — recoger los PDFs |
| `write-excel` | `cmd_write_excel` | **Fase 3** — generar el Excel anotado |
| `status` | `cmd_status` | Resumen de filas por estado |
| `failures` | `cmd_failures` | Listar filas en estados `failed_*` |
| `inspect` | `cmd_inspect` | Volcar el JSON completo de una fila |
| `retry` | `cmd_retry` | Resetear filas puntuales a `pending` |
| `reset` | `cmd_reset` | Borrar y recrear el `state.sqlite` |

---

## 4. Configuración (`config.py` + `.env_backfill`)

`config.py` define un `@dataclass(frozen=True) Settings` — **inmutable** una vez cargado — y lo construye desde variables de entorno.

### Mecánica de carga

```python
_ROOT = Path(__file__).resolve().parent.parent    # raíz del repo
_ENV_FILE = _ROOT / ".env_backfill"
if _ENV_FILE.exists():
    load_dotenv(_ENV_FILE)        # ← se ejecuta AL IMPORTAR el módulo
```

Matiz importante: `load_dotenv` corre **en import time**, no cuando se llama `Settings.load()`. Y `python-dotenv` **no pisa** variables ya presentes en el entorno del proceso → podés sobreescribir cualquier valor del archivo exportando la variable antes de correr el comando.

Hay tres helpers de parsing:

- `_require(name)` — obligatoria; si falta o está vacía → `RuntimeError` con mensaje claro.
- `_optional(name, default)` — opcional con default.
- `_int` / `_float` — casting con default.

### Variables, agrupadas por subsistema

| Variable | Obligatoria | Default | Usada por | Para qué |
|---|---|---|---|---|
| `SA_KEY_PATH` | ✅ | — | `drive_reader`, `drive_writer` | Ruta al JSON de la Service Account |
| `SHARED_DRIVE_ID` | ✅ | — | `drive_writer` | ID del Shared Drive destino de PDFs |
| `GCS_BUCKET_TRANSCRIPTS` | ✅ | — | `gcs_uploader` | Bucket donde se suben los transcripts |
| `GCS_BUCKET_PROJECT` | ✅ | — | `gcs_uploader` | Proyecto GCP del bucket |
| `GCS_BUCKET_PREFIX` | — | `studio-backfill` | `gcs_uploader` | "Carpeta" dentro del bucket |
| `GCS_SIGNED_URL_TTL_SECONDS` | — | `21600` (6h) | `gcs_uploader` | TTL de la signed URL del transcript |
| `WEBHOOK_URL` | ✅ | — | `webhook_client` | URL completa del webhook xAI (incluye `/webhook`) |
| `WEBHOOK_SECRET` | ✅ | — | `webhook_client` | Secreto HMAC compartido con el servidor |
| `WEBHOOK_RPS` | — | `0.2` | `pipeline.submit_all` | Máx. requests/segundo al webhook (0.2 = 1 cada 5s) |
| `REPORTS_URL` | ✅ | — | `reports_client` | Base URL del backend de reportes |
| `PIPELINE_TIMEOUT_HOURS` | — | `48` | `pipeline.collect_once` | Tras N horas en `submitted` → timeout |
| `POLL_INTERVAL_SECONDS` | — | `900` (15 min) | `pipeline.collect_until_drained` | Sleep entre pasadas de polling |
| `MAX_ATTEMPTS` | — | `5` | `pipeline.collect_once` | Reintentos máx. de `failed_pdf_fetch` |
| `EXCEL_PATH` | ✅ | — | `excel_io` | Ruta al Excel fuente |
| `STATE_DB_PATH` | ✅ | — | `state` | Ruta al `state.sqlite` |
| `DEFAULT_GRADE` | — | `No informado` | `pipeline._build_payload` | Grade fallback si la celda está vacía |

También existe `Settings.load_partial()` → devuelve `None` en vez de explotar si faltan variables. Lo usan los **tests offline** del resolver, que no necesitan credenciales.

---

## 5. Lectura del Excel (`excel_io.py`)

### `read_rows(excel_path)` — generador de filas

Lee la **primera hoja** del workbook con `openpyxl` en modo `read_only` (streaming, no carga todo en RAM) y `data_only=True` (devuelve valores calculados, no fórmulas). Por cada fila de datos produce un `dict` `{nombre_columna: valor}` más una clave sintética `_row_position`.

#### `_row_position`: la clave primaria de facto

El Excel **no tiene columna `id`**. Entonces el sistema usa la **posición física de la fila** como identidad:

- `_row_position` es **1-indexed** y **excluye el header** (la primera fila de datos es la 1).
- Las filas completamente vacías se **saltan sin consumir posición**.
- De ahí derivan los `event_id`: `{EVENT_ID_PREFIX}-{_row_position}` (ej. `studio-row-4`). El prefijo es configurable vía la variable `EVENT_ID_PREFIX` (default `studio-row`); cambiarlo fuerza que xAI trate las filas como sesiones nuevas.

⚠️ **Consecuencia crítica**: si alguien inserta/borra filas del Excel fuente entre corridas, todas las posiciones se corren y los `event_id` dejan de corresponder. Por eso existe el **candado de SHA-256** ([§10](#10-el-checkpoint-sqlite-statepy)).

#### Soporte bilingüe de encabezados

El sistema espera 12 columnas canónicas (en inglés): `infrastructureCode, school, department, district, teacher, coach, subject, grade, section, shift, videoLink, transcriptLink`.

Pero el Excel real puede venir con la plantilla en **español** ("Código de infraestructura", "Docente", "Enlace de transcripción"…). El mapeo se hace así:

1. `_norm_header()` normaliza cada encabezado: quita acentos (NFKD → ASCII), pasa a minúsculas, colapsa espacios. Así `"Código"`, `"Codigo"` y `"  código "` son equivalentes.
2. `HEADER_ALIASES` mapea cada encabezado español normalizado → nombre canónico inglés.
3. Las columnas desconocidas (p. ej. "Observación") **se conservan con su nombre original** — no rompen nada.
4. Si después de traducir falta alguna de las 12 requeridas → `RuntimeError` inmediato.

#### Normalización numérica (`_denumber`)

openpyxl devuelve celdas numéricas como `float`: el código de infraestructura `11769` llega como `11769.0` y el grado `4` como `4.0`. `_denumber()` convierte floats enteros a string sin decimal (`11769.0 → "11769"`). Se aplica solo a `infrastructureCode` y `grade`, que terminan dentro de códigos y payloads donde `"studio-school-11769.0"` sería un bug.

### `write_output_excel(...)` — Fase 3

Abre el Excel fuente **completo** (no read-only, porque hay que editarlo), agrega 3 encabezados al final de la fila 1 (`event_id_xai`, `pdf_drive_link`, `backfill_status`), y por cada `row_position` escribe los valores en la fila de hoja `row_position + 1` (el +1 compensa el header).

Si se le pasa el SHA-256 fuente, crea además una hoja `_backfill_meta` con `source_excel_path`, `source_excel_sha256` y `generated_by` — trazabilidad de qué archivo exacto generó ese reporte.

⚠️ Matiz: el loop de escritura itera `range(1, ws.max_row)` — usa el row count del Excel cargado, no el del state. Filas vacías intermedias del Excel reciben columnas vacías (no hay entrada en `rows_with_links` para ellas).

---

## 6. El resolver de 8 casos (`drive_reader.py`)

Este módulo es la pieza más "de negocio" del sistema. Su trabajo: dado el par `(transcriptLink, videoLink)` de una fila, decidir **qué es realmente** ese link y **de dónde se baja el transcript**.

### ¿Por qué 8 casos?

Los datos los cargaron humanos durante meses, de formas inconsistentes. El análisis del Excel real (9,386 filas) dio esta distribución:

```
A  = 65.22%   Doc de Google directo                    ← el caso feliz
E  = 33.20%   "Sin dato" / vacío                       ← un tercio no tiene transcript
D2 =  0.82%   mismo file/d/<id> en transcript y video
B  =  0.30%   drive.google.com/file/d/<id> (mime desconocido)
D3 =  0.21%   mismo Doc en transcript y video
C  =  0.12%   carpeta de Drive (hay que buscar adentro)
F  =  0.10%   formato no soportado (sheets, redirects…)
D1 =  0.02%   carpeta, y video == transcript
```

### Tabla de decisión de `classify()`

`classify()` es **pura** (sin red) salvo el peek opcional a carpetas. La lógica:

| Condición | Caso | `file_id` | `mime` |
|---|---|---|---|
| transcript vacío o literal `"Sin dato"` | **E** | — | — |
| URL no es doc/file/folder (spreadsheet, `google.com/url?`, otro) | **F** | — | — |
| `docs.google.com/document/d/<id>` y ≠ video | **A** | el del Doc | Google Doc |
| `docs.google.com/document/d/<id>` e **igual** al video | **D3** | el del Doc | Google Doc |
| `drive.google.com/file/d/<id>` y ≠ video | **B** | el del file | ❓ se sondea después |
| `drive.google.com/file/d/<id>` e igual al video | **D2** | el del file | ❓ se sondea después |
| `drive.google.com/drive/folders/<id>` y ≠ video | **C** | el mejor hijo de la carpeta | el del hijo |
| carpeta e igual al video | **D1** | ídem C | ídem C |

Detalles finos:

- **Los regexes toleran `/u/<n>/`** en la URL (`/document/u/2/d/<id>` = sesión multi-cuenta de Google) y cualquier sufijo de vista (`/edit`, `/preview`, `/mobilebasic`) porque solo capturan hasta el ID.
- **El caso F tiene sub-variantes diagnósticas** (`F_sheet`, `F_redirect`, `F_other`) que solo sirven para el mensaje de error — las tres se tratan igual (skip).
- **`"Sin dato"`** es el literal que Studio pone cuando no hay archivo; `_normalize_link()` lo convierte en vacío → cae a caso E.
- **Para carpetas (C/D1)** hace falta el cliente de Drive: `_pick_transcript_from_folder()` lista los hijos y elige con preferencia **Google Doc > .docx > .txt**. Si la carpeta no tiene ninguno → `file_id=None` y la fila se skipea. Si se llama `classify()` sin cliente (`drive=None`, como hace `find-pilot-rows` por velocidad), la clasificación queda en "carpeta" sin resolver el hijo.
- **B y D2 quedan con `mime=None`**: un link `file/d/<id>` puede ser un `.docx`, un `.txt`… o el **video .mp4** (muy común en D2, donde alguien pegó el mismo link en ambas columnas). La pipeline llama a `probe_mime()` (un `files().get` de solo metadata) y si resulta `video/mp4` la fila se skipea como `skipped_no_transcript`.

### Descarga: `download_transcript()`

Normaliza las tres fuentes posibles a `(bytes, content_type, extension)`:

| Origen | Método Drive API | Resultado |
|---|---|---|
| Google Doc | `export_media(mimeType=DOCX)` — Google lo convierte | `.docx` |
| Archivo `.docx` | `get_media` tal cual | `.docx` |
| Archivo `.txt` | `get_media` tal cual | `.txt` |
| `.mp4` o cualquier otro mime | — | `ValueError` (el caller ya debió skipear) |

La descarga usa `MediaIoBaseDownload` en chunks (archivos potencialmente grandes). El webhook de xAI acepta tanto `.docx` como `.txt`.

`make_drive_client()` construye el cliente v3 con la SA key y scope **readonly** — el lector no puede escribir nada en Drive, por diseño.

---

## 7. Fase 1 — Encolar (`pilot`)

Comando: `python -m studio_backfill.cli pilot --rows 4,66 | --all | --retry-failed`

### Selección de filas (`cmd_pilot`, `cli.py:119`)

Primero, **siempre** se llama `_ensure_source_registered()`: lee el Excel completo, calcula su SHA-256 y lo registra en `source_metadata` (o verifica que coincida con el ya registrado — ver §10). Luego, según el flag:

- `--rows 4,66,505` → filtra el Excel por `_row_position`; si pedís una posición inexistente, warning y sigue.
- `--all` → todas las filas.
- `--retry-failed` → busca en SQLite las filas en `failed_drive_read | failed_gcs_upload | failed_webhook | failed_phase1`, las resetea a `pending` y reprocesa **la copia del Excel guardada en el state** (`excel_row_json`), no relee el archivo.

Después construye un `Pipeline` (que instancia los 5 clientes long-lived: Drive reader, GCS, webhook, reports, Drive writer) y llama `submit_all()`. Al final, `mark_phase_1_finished()` estampa el timestamp en `source_metadata`.

### `Pipeline.submit_row()` — el camino de UNA fila (`pipeline.py:64`)

Secuencia exacta, con cada salida posible:

```
1. event_id = f"{settings.event_id_prefix}-{row_position}"   # default prefix: "studio-row"
2. upsert_pending()                  → INSERT OR IGNORE (idempotente)
3. ¿ya está submitted/completed?     → SÍ: skip, return False  ← idempotencia clave
4. classify(transcript, video)
   ├─ caso E                         → skipped_no_link, return False
   ├─ caso F                         → skipped_unsupported_format, return False
   └─ caso C/D1 sin hijo válido      → skipped_no_transcript_in_folder, return False
5. caso B/D2: probe_mime()
   └─ es video/mp4                   → skipped_no_transcript, return False
6. update(drive_case, transcript_file_id)
7. download_transcript()             → falla: failed_drive_read, attempts+1, return False
8. gcs.upload() + signed URL         → falla: failed_gcs_upload, attempts+1, return False
   update(state="transcript_uploaded")
9. webhook.post(payload firmado)     → falla: failed_webhook, attempts+1, return True ⚠️
10. update(webhook_message_id, webhook_submitted_at, state="submitted")
    return True
(cualquier excepción no contemplada  → failed_phase1, return False)
```

El **valor de retorno booleano** tiene un significado muy específico: **"¿se intentó realmente un POST al webhook?"**. Fijate que un webhook *fallido* devuelve `True` (¡el POST ocurrió, consumió cuota del rate-limit!) mientras que todos los skips y fallas previas devuelven `False`.

### `submit_all()` — throttle inteligente

```python
delay = 1.0 / max(self.settings.webhook_rps, 0.0001)
last_webhook = 0.0
for row in excel_rows:
    if last_webhook > 0:
        wait = delay - (time.monotonic() - last_webhook)
        if wait > 0:
            time.sleep(wait)
    did_post = self.submit_row(state, row)
    if did_post:
        last_webhook = time.monotonic()
```

El matiz: **el sleep solo aplica entre POSTs reales**. Las ~3,116 filas "Sin dato" pasan volando sin esperar 5 segundos cada una — a 0.2 RPS, throttlear también los skips habría agregado ~4.3 horas de sleep inútil. Además usa `time.monotonic()` (inmune a cambios de reloj) y mide desde el *último* POST, así el tiempo de descarga Drive+GCS se descuenta de la espera.

### El payload del webhook (`_build_payload` + `build_payload`)

El formato final es `{"kwargs": {"signed_url": ..., "params": {...}}}` (es el shape que el consumer Pub/Sub de xAI espera). Los `params`:

| Campo | Origen | Ejemplo |
|---|---|---|
| `event_id` | `{EVENT_ID_PREFIX}-{posición}` (default `studio-row`) | `studio-row-4` |
| `teacher_code` | `studio-teacher-` + `slug(teacher)` | `studio-teacher-LOPEZ-VELASCO-CARLOS-MAURICIO` |
| `coach_code` | `studio-tutor-` + `slug(coach)` | `studio-tutor-PEREZ-ANA` |
| `school_code` | `studio-school-` + `infrastructureCode` | `studio-school-11769` |
| `grade` / `subject` | celda, o `"No informado"` si vacía | `4` |
| `recorded_at` | **UTC now** (no la fecha real de la clase — no está en el Excel) | `2026-06-02T15:04:05+00:00` |
| `section` / `shift` | celda, omitidos si vacíos | — |
| `teacher_name`, `coach_name`, `school_name`, `school_department`, `school_district` | celdas, limpiadas con `_clean_str` | — |

Sobre los últimos cinco (display names): son campos **opcionales** que solo el branch `feat/webhook-forward-entity-names` del webhook de xAI entiende (los usa para poblar `dim_users`/`dim_schools` y que el PDF muestre nombres reales en vez de rayas). Si el webhook desplegado es `main`, Pydantic v2 con `extra="ignore"` simplemente los descarta — **enviar de más es inofensivo**. `_clean_str()` filtra vacíos y el literal `"Sin dato"`.

`slug()` normaliza nombres con acentos/comas a ASCII seguro: `"LÓPEZ VELASCO, CARLOS MAURICIO"` → `"LOPEZ-VELASCO-CARLOS-MAURICIO"` (NFKD, drop no-ASCII, no-alfanuméricos → `-`, mayúsculas). Vacío → `"UNKNOWN"`.

---

## 8. Fase 2 — Recoger PDFs (`collect`)

Comando: `python -m studio_backfill.cli collect [--once]`

xAI tarda horas/días, así que esta fase es un **poller**. Dos modos:

- `--once`: una sola pasada (`collect_once`) y termina. Útil para chequeos manuales o cron.
- sin flag: `collect_until_drained` — loop infinito de `collect_once` + `sleep(POLL_INTERVAL_SECONDS)` hasta que **no quede ninguna fila** en estados re-chequeables.

### `collect_once()` paso a paso (`pipeline.py:209`)

**Paso 0 — barrido de timeouts.** Toda fila en `submitted` cuyo `webhook_submitted_at` sea más viejo que `PIPELINE_TIMEOUT_HOURS` (48h) pasa a `failed_timeout_pending_analysis`. Esto evita pollear para siempre filas que xAI nunca va a terminar.

**Paso 1 — selección.** Toma las filas en `submitted` + `failed_pdf_404` (reintentables sin límite — un 404 solo significa "el ETL horario aún no movió la data a BigQuery") + `failed_pdf_fetch` **con `attempts < MAX_ATTEMPTS`** (errores reales del backend: máximo 5 intentos).

**Paso 2 — por cada fila**, `GET {REPORTS_URL}/api/reports/session-pdf?event_id=…&lang=es`, y se decide por status code:

| Respuesta | Interpretación | Acción / nuevo estado |
|---|---|---|
| `200` con `{"url": ...}` | PDF listo | Descargar la signed URL → subir al Shared Drive → `completed` (si la subida falla → `failed_drive_upload`, attempts+1) |
| `422` **terminal** | xAI marcó la sesión como `failed` o `data_incomplete` | `failed_pdf_analysis` — definitivo, no se reintenta |
| `422` no terminal | xAI **todavía procesando** | Se re-marca `submitted` → vuelve al pool del próximo poll |
| `404` | La sesión aún no llegó a BigQuery (ETL pendiente) | `failed_pdf_404` → reintenta siempre |
| `429` | Rate limit del backend | **`sleep(30)` y NO cambia el estado** — la fila se reintenta en esta misma o próxima pasada |
| otro (5xx…) | Error transitorio | `failed_pdf_fetch`, attempts+1 (tope 5) |
| excepción de red | ídem | ídem |

El discriminador del 422 es `PdfFetchResult.is_terminal_422()`: parsea el body JSON y busca `detail.pipeline_status == "failed"`, `detail.session_status == "failed"` o `detail.reason == "data_incomplete"`. Cualquier otro 422 se asume "still processing".

⚠️ Matiz del nombre `failed_pdf_404`: aunque empieza con `failed_`, **no es una falla terminal** — es la forma de decir "esperando al ETL". El README lo documenta como "Sí (polling automático)".

### Condición de drenado

`collect_until_drained` termina cuando `submitted + failed_pdf_404 + failed_pdf_fetch(<5 attempts)` da cero. Las filas en `failed_pdf_analysis`, `failed_timeout_*` y todos los `skipped_*` **no** cuentan — son finales.

---

## 9. Fase 3 — Excel de salida (`write-excel`)

Comando: `python -m studio_backfill.cli write-excel [--out PATH]`

Lee **todas** las filas del SQLite (query directa `SELECT * FROM rows`, sin filtro de estado) y arma un dict `row_position → {event_id_xai, pdf_drive_link, backfill_status}` con tres reglas:

- `completed` → status `"completed"` + el link real del PDF.
- `skipped_*` → status = el nombre del estado, link vacío.
- cualquier otro → status `"{estado}: {primeros 200 chars de last_error}"`, link vacío — así el Excel mismo explica por qué cada fila falló.

Después delega en `excel_io.write_output_excel()` (§5). El path de salida por defecto es el del fuente con sufijo `_with_reports`: `studio_results_final.xlsx` → `studio_results_final_with_reports.xlsx`.

Se puede correr **en cualquier momento** (no hace falta esperar a que todo termine): genera una foto del estado actual.

---

## 10. El checkpoint SQLite (`state.py`)

Toda la resiliencia del sistema vive acá. `StateStore` envuelve un SQLite con dos tablas:

### Tabla `rows` — una fila por fila del Excel procesada

| Columna | Qué guarda |
|---|---|
| `row_position` | PK — la posición en el Excel |
| `event_id` | `studio-row-N`, UNIQUE — la identidad cross-sistema |
| `drive_case` | A/B/C/D1/D2/D3 resuelto |
| `transcript_file_id` | ID del archivo en Drive |
| `transcript_gcs_uri` | `gs://bucket/prefix/event_id/transcript.docx` |
| `webhook_message_id` / `webhook_submitted_at` | Respuesta del webhook + timestamp UTC del POST |
| `pdf_drive_id` / `pdf_drive_link` | El PDF final en el Shared Drive |
| `state` | El estado de la máquina (§11) |
| `last_error` | Último error humano-legible |
| `last_attempt_at` | Auto-estampado en **cada** `update()` |
| `attempts` | Contador de reintentos (solo lo incrementan las fallas) |
| `excel_row_json` | **Copia completa de la fila del Excel** serializada a JSON |

Esa última columna es importante: guarda un snapshot de la fila fuente, lo que permite a `pilot --retry-failed` reprocesar sin releer el Excel, y a `inspect` mostrar exactamente qué datos se usaron.

### Tabla `source_metadata` — el candado del Excel fuente

Una sola fila (`CHECK (id = 1)`): path, **SHA-256**, row count, timestamps de fase 1 y la URL del webhook usada.

`register_source()` implementa la protección contra el peor accidente posible:

> Si el SHA-256 del Excel actual **difiere** del registrado, `RuntimeError` y se niega a continuar.

¿Por qué tan agresivo? Porque la identidad de todo el sistema es la **posición de fila** (§5). Si el Excel cambió (alguien insertó una fila), `studio-row-500` ahora apuntaría a otra clase, y los PDFs quedarían cruzados con las filas equivocadas. La única salida legítima es `reset --confirm` y empezar de cero.

### Decisiones de implementación

- **`PRAGMA journal_mode=WAL`** — permite que mientras `pilot --all` corre en una terminal, otra terminal lea con `status` sin bloquearse.
- **`isolation_level=None` + transacciones explícitas** — el context manager `_tx()` hace `BEGIN/COMMIT/ROLLBACK` a mano; cada operación lógica es atómica. Si el proceso muere a mitad de un update, el SQLite queda consistente.
- **`upsert_pending()` usa `INSERT OR IGNORE`** — llamarlo sobre una fila ya existente no hace nada (no pisa el estado). Esa es la base de la idempotencia de Fase 1.
- **`update()` auto-estampa `last_attempt_at`** vía `fields.setdefault(...)` — nunca hay que acordarse de pasarlo.
- **`is_completed_or_submitted()`** considera "ya en curso" los estados `completed`, `submitted`, `pdf_in_drive`, `analyzed` (los dos últimos son estados legacy/futuros que no se generan en el código actual, pero el guard los respeta).
- `reset()` dropea y recrea las tablas. **No borra nada en xAI, GCS ni Drive** — solo el checkpoint local. La idempotencia del lado xAI (mismo `event_id`) evita trabajo duplicado si se re-envía.

---

## 11. La máquina de estados completa

```
                              ┌─────────┐
                              │ pending │  (recién upserteada)
                              └────┬────┘
            ┌──────────────┬──────┼──────────────────┬───────────────┐
            ▼              ▼      ▼                  ▼               ▼
   skipped_no_link  skipped_   skipped_no_   skipped_no_      (errores fase 1)
        (E)         unsupported transcript    transcript_      failed_drive_read
                    _format(F)  (B/D2=mp4)    in_folder        failed_gcs_upload
                                              (C/D1 vacía)     failed_phase1
                                                                    │
                                                                    │ pilot --retry-failed
                                                                    ▼ (vuelve a pending)
                              ┌──────────────────────┐
                              │ transcript_uploaded  │ (en GCS, pre-POST)
                              └──────────┬───────────┘
                                         │ POST ok
                  POST falla             ▼
            failed_webhook ◄──── ┌────────────┐
            (retry-able)         │ submitted  │◄───────────────┐
                                 └─────┬──────┘                │ 422 "processing"
                                       │                       │ (re-marca submitted)
                 ┌─────────────────────┼───────────────────────┤
                 ▼                     ▼                       │
        (>48h sin respuesta)    GET session-pdf ───────────────┘
   failed_timeout_pending_           │
        _analysis                    ├─ 404 → failed_pdf_404 ──► (se re-pollea siempre)
                                     ├─ 5xx → failed_pdf_fetch ─► (máx 5 attempts)
                                     ├─ 422 terminal → failed_pdf_analysis (FINAL)
                                     └─ 200 → bajar PDF → subir a Drive
                                                │            │ falla
                                                ▼            ▼
                                          ┌───────────┐  failed_drive_upload
                                          │ completed │  (retry-able)
                                          └───────────┘
```

### Tabla de estados (la misma del README, con contexto)

| Estado | Fase | Significa | ¿Se reintenta? |
|---|---|---|---|
| `pending` | 1 | Insertada, aún sin procesar | Es el punto de partida |
| `transcript_uploaded` | 1 | Transcript en GCS, POST aún no confirmado | Transitorio |
| `submitted` | 1→2 | xAI aceptó el evento; esperando análisis | Pasa solo a `completed`/`failed_*` |
| `completed` | 2 | PDF en el Shared Drive ✅ | Final feliz |
| `skipped_no_link` | 1 | Caso E (sin transcript) | No — dato fuente ausente |
| `skipped_unsupported_format` | 1 | Caso F | No |
| `skipped_no_transcript` | 1 | B/D2 que resultó ser solo el video .mp4 | No |
| `skipped_no_transcript_in_folder` | 1 | C/D1 con carpeta sin Doc/.docx/.txt | No |
| `failed_drive_read` | 1 | Drive devolvió 403/404 a la SA | `pilot --retry-failed` (revisar permisos) |
| `failed_gcs_upload` | 1 | Falla subiendo a GCS | `pilot --retry-failed` (transient) |
| `failed_webhook` | 1 | xAI rechazó el POST | `pilot --retry-failed` (`last_error` dice por qué) |
| `failed_phase1` | 1 | Excepción no contemplada | `pilot --retry-failed` |
| `failed_pdf_404` | 2 | ETL aún no llevó la sesión a BigQuery | Automático, sin límite |
| `failed_pdf_fetch` | 2 | 5xx / error de red del backend de reportes | Automático, hasta `MAX_ATTEMPTS`=5 |
| `failed_pdf_analysis` | 2 | xAI marcó la sesión `failed` / `data_incomplete` | No — el problema es del lado xAI |
| `failed_drive_upload` | 2 | El PDF no se pudo subir al Shared Drive | Automático en próxima pasada* |
| `failed_timeout_pending_analysis` | 2 | >48h en `submitted` | Solo manual (`retry --events`) |

*Nota: `failed_drive_upload` no está en el pool de fetch de `collect_once`; en la práctica se recupera con `retry --events` que la devuelve a `pending`… pero como ya está `submitted` del lado de xAI, lo correcto es resetearla a `submitted` a mano o re-pollearla. Para casos puntuales, `inspect` + SQL directo es el camino.

---

## 12. Clientes externos: GCS, webhook, reportes, Drive writer

### `gcs_uploader.py` — subida + signed URL con doble escenario de credenciales

El detalle más sutil del proyecto. El uploader corre con **ADC** (`google.auth.default()`), y hay dos mundos:

1. **En la VM bastion (GCE)**: las credenciales vienen del metadata server y **no incluyen clave privada local** → la librería no puede firmar URLs offline. Solución: pasar `service_account_email` + `access_token` a `generate_signed_url()`, lo que hace que la firma se delegue a la API **IAM signBlob** (requiere el rol `roles/iam.serviceAccountTokenCreator` sobre sí misma).
2. **Local con SA key file** (`GOOGLE_APPLICATION_CREDENTIALS`): hay clave privada, la firma es offline, y los parámetros extra son no-ops.

El mismo código cubre ambos: `_sign()` refresca el token si hace falta, detecta si hay `service_account_email`/`token` disponibles, y los agrega solo en ese caso.

El layout en el bucket es determinista: `{prefix}/{event_id}/transcript.{ext}` → re-subir el mismo evento **pisa** el blob anterior (idempotente, sin basura acumulada). La signed URL es **v4, GET, TTL 6h** — suficiente para absorber los reintentos de Pub/Sub del consumer de xAI, que descarga inmediatamente.

### `webhook_client.py` — POST firmado con HMAC

Replica byte a byte el algoritmo que valida el servidor de xAI:

```
signing_string = f"{timestamp}.".encode() + body_bytes
signature      = HMAC-SHA256(secret, signing_string).hexdigest()
headers:
  X-Webhook-Signature: sha256=<signature>
  X-Webhook-Timestamp: <unix epoch>
```

Matices:
- El body se serializa con `json.dumps(..., separators=(",", ":"))` — **sin espacios**. Importa porque la firma es sobre los bytes exactos; cualquier re-serialización del lado cliente rompería la verificación.
- El servidor tolera ±300s de skew en el timestamp (anti-replay).
- `WEBHOOK_URL` ya incluye el path `/webhook`; el cliente solo normaliza el trailing slash.
- No-2xx → `WebhookError(status, body)`; la pipeline guarda `"{status}: {body[:300]}"` en `last_error`.
- Respuesta esperada: `202` con JSON que incluye `message_id` (se persiste). Si el body no es JSON, se tolera con un dict sintético.

### `reports_client.py` — GET del PDF

`GET {base}/api/reports/session-pdf?event_id=…&lang=es`. Devuelve siempre un `PdfFetchResult(status, json_body, raw_text)` — **nunca lanza** por status code; la interpretación es 100% responsabilidad de `collect_once` (§8). El único método que sí lanza es `download_signed()` (`raise_for_status`), que baja el PDF binario desde la signed URL que devolvió el 200.

### `drive_writer.py` — subir el PDF al Shared Drive

Cliente Drive separado del reader, con scope **completo** (`auth/drive`) porque escribe. Sube `reporte_<event_id>.pdf` directo a la **raíz** del Shared Drive (`parents=[shared_drive_id]`, `supportsAllDrives=True`) y devuelve `(id, webViewLink)` — ese link es el que termina en el Excel final.

⚠️ Matiz: Google Drive **permite nombres duplicados**. Si re-subís el mismo evento, se crea un *hermano* con el mismo nombre, no se reemplaza. Existe `find_existing()` para detectar un reporte previo, pero el flujo actual de `collect_once` no lo invoca — la protección real contra duplicados es que una fila `completed` nunca vuelve a entrar al pool de colección.

---

## 13. Comandos del CLI, uno por uno

### `find-pilot-rows [--sample-size N]`
Herramienta de **exploración previa**. Recorre todo el Excel clasificando cada fila (sin tocar carpetas, por velocidad), imprime el conteo por caso, muestra hasta N ejemplos por caso (para C/D1 sí lista el contenido de la carpeta en Drive, para validar a ojo), y al final **imprime el comando `pilot --rows ...` sugerido** con una fila representativa de cada caso. El flujo recomendado es: correr esto → correr el pilot sugerido → verificar → recién entonces `--all`.

### `pilot --rows A,B,C | --all | --retry-failed`
Fase 1 (§7). Los tres flags son mutuamente excluyentes; sin ninguno → error de uso (exit 2).

### `collect [--once]`
Fase 2 (§8). Sin `--once` queda en loop hasta drenar — correrlo dentro de `tmux` en el bastion.

### `write-excel [--out PATH]`
Fase 3 (§9).

### `status`
Imprime el total de filas en el state y el desglose por estado con porcentajes. Ideal para `watch -n 30 'python -m studio_backfill.cli status'` desde otra sesión SSH.

### `failures`
Tabla de todas las filas en `failed_*`: `event_id`, estado, attempts y los primeros 80 chars del último error.

### `inspect <event_id>`
Vuelca el JSON completo de una fila: todos los campos del SQLite incluida la copia de la fila Excel original. La herramienta de debugging por excelencia.

### `retry --events id1,id2`
Resetea filas puntuales a `pending` con `attempts=0` y `last_error=None`. Después hay que correr `pilot --retry-failed` o `pilot --rows ...` para que se reprocesen. (Para fallas de fase 2 sobre filas ya `submitted`, ojo: resetear a `pending` re-ejecuta la fase 1 completa — la idempotencia del webhook de xAI por `event_id` evita análisis duplicado.)

### `reset --confirm`
Dropea y recrea el SQLite. Sin `--confirm` se niega (exit 2). No toca xAI/GCS/Drive.

---

## 14. Matices y decisiones de diseño no obvias

Resumen de las sutilezas dispersas en el código — las cosas que un dev nuevo rompería sin saberlo:

1. **`_row_position` es la identidad de todo.** No hay columna `id` en el Excel; la posición física manda. De ahí el candado SHA-256: **jamás editar el Excel fuente entre corridas** (ni "solo agregar una fila al final" sin entender las consecuencias — el SHA cambia y `register_source` se niega).

2. **Throttle solo en POSTs reales** (`submit_all`). El booleano que devuelve `submit_row` no significa "éxito": significa "consumí cuota del webhook". Un POST fallido devuelve `True`; un skip devuelve `False`. Cambiar esa semántica rompería el rate-limiting.

3. **Doble credencial deliberada**: Drive usa la SA key explícita (es la SA con permisos sobre los Drives de los tutores y el Shared Drive), GCS usa ADC (la SA de la VM, que es la que tiene permisos sobre el bucket). No unificar.

4. **Signed URLs en GCE requieren IAM signBlob** porque las credenciales del metadata server no traen clave privada. El código lo maneja transparente, pero si falla la firma en el bastion, lo primero a revisar es el rol `serviceAccountTokenCreator`.

5. **El JSON del webhook se firma byte a byte** — `separators=(",", ":")` no es estética, es parte del contrato HMAC.

6. **`"Sin dato"` es un valor mágico** del Excel de Studio. Se filtra en dos lugares: `_normalize_link` (links → caso E) y `_clean_str` (display names → omitidos del payload).

7. **`recorded_at` es mentira piadosa**: se manda el UTC del momento del POST porque el Excel no tiene la fecha real de la clase.

8. **Los display names son forward-compatible**: solo los entiende el branch `feat/webhook-forward-entity-names` del webhook; en `main`, Pydantic los ignora silenciosamente. Mandarlos siempre es seguro.

9. **`failed_pdf_404` no es una falla** — es "el ETL horario de xAI todavía no corrió". Se re-pollea sin límite de attempts, a diferencia de `failed_pdf_fetch` (5 máx).

10. **El 422 es ambiguo por diseño del backend**: puede ser "procesando" o "falló terminal". `is_terminal_422()` es el único lugar que sabe distinguirlos (mirando `pipeline_status`/`session_status`/`reason` en el body).

11. **El 429 no cambia estado** — solo `sleep(30)`. La fila queda en su estado actual y se reintenta naturalmente.

12. **Encabezados bilingües con normalización de acentos** (`excel_io`): el Excel puede venir en plantilla española o inglesa; cualquier otra columna extra se tolera.

13. **Floats de openpyxl**: `11769.0 → "11769"` vía `_denumber`, solo para `infrastructureCode` y `grade`. Sin esto, los `school_code` saldrían con `.0`.

14. **WAL mode en SQLite** permite monitorear (`status`) desde otra terminal mientras una corrida larga escribe.

15. **Re-subir un PDF al Shared Drive crea un duplicado**, no reemplaza (comportamiento de Drive). La defensa es que `completed` nunca vuelve al pool.

16. **`reset` solo borra lo local.** Lo enviado a xAI sigue ahí; re-enviar el mismo `event_id` no duplica análisis (idempotencia del lado xAI).

17. **Estados `pdf_in_drive` y `analyzed`** aparecen en `is_completed_or_submitted()` pero ningún código actual los escribe — son legacy/reservados. No eliminar el guard sin verificar.

18. **`Settings` se carga en cada invocación** → editar `.env_backfill` (p. ej. subir `WEBHOOK_RPS`) surte efecto en el **próximo** comando, sin reiniciar nada. Pero no afecta un proceso ya corriendo.

19. **Todos los comandos exigen el `.env_backfill` completo**, incluso los read-only como `status` — `Settings.load()` corre antes del dispatch.

20. **`collect` y `pilot` pueden convivir**: gracias a WAL y a que operan sobre conjuntos de estados disjuntos, en la práctica se puede encolar y recolectar en paralelo (el patrón del README es secuencial, que es lo más simple y seguro).

---

## 15. Estructura de archivos

```
studio_backfill/                      ← raíz del repo
├── .env_backfill                     ← config + secretos (gitignored)
├── .env_backfill_example             ← plantilla documentada de la config
├── auditoria-clases-sa-key.json      ← clave de la Service Account (gitignored)
├── state.sqlite                      ← checkpoint local (gitignored, se crea solo)
├── README.md                         ← guía operativa (comandos, queries SQL de xAI)
├── DOCUMENTACION.md                  ← este documento
├── scripts/                          ← probes de diagnóstico pre-corrida
│   ├── probe_sa_access.py            ← ¿la SA lee los Drives de los tutores?
│   ├── probe_shared_drive.py         ← ¿la SA escribe en el Shared Drive destino?
│   ├── probe_dual_cred_upload.py     ← ¿la ADC sube al bucket y firma URLs? (crítico)
│   ├── probe_bucket_write.py         │
│   ├── probe_buckets_all.py          ├─ variantes de exploración de permisos
│   └── probe_user_adc_buckets.py     │
└── studio_backfill/                  ← el paquete Python
    ├── __init__.py                   ← versión + referencia al plan original
    ├── __main__.py                   ← permite `python -m studio_backfill`
    ├── cli.py                        ← entrypoint: argparse + 9 subcomandos
    ├── config.py                     ← Settings (dataclass frozen) desde .env_backfill
    ├── excel_io.py                   ← lectura (bilingüe) y escritura del Excel
    ├── drive_reader.py               ← resolver de 8 casos + descarga de transcripts
    ├── pipeline.py                   ← orquestador de Fase 1 y Fase 2
    ├── gcs_uploader.py               ← subida a GCS + signed URL (dual-credential)
    ├── webhook_client.py             ← HMAC-SHA256 + POST al webhook xAI
    ├── reports_client.py             ← GET session-pdf + interpretación de statuses
    ├── drive_writer.py               ← subida del PDF al Shared Drive
    ├── state.py                      ← StateStore: SQLite WAL + máquina de estados
    ├── requirements.txt              ← 6 dependencias (google-api, gcs, openpyxl…)
    └── tests/test_resolver.py        ← unit tests del clasificador de casos
```

### Dependencias (`requirements.txt`)

| Paquete | Para qué |
|---|---|
| `google-api-python-client` | Drive API v3 (reader + writer) |
| `google-auth` | Credenciales SA + ADC |
| `google-cloud-storage` | GCS upload + signed URLs |
| `openpyxl` | Lectura/escritura de `.xlsx` |
| `python-dotenv` | Carga de `.env_backfill` |
| `requests` | HTTP del webhook y del backend de reportes |

---

## Apéndice: el "happy path" operativo completo

Para cerrar, la secuencia real de una corrida de producción en el bastion:

```bash
# 0. Validaciones previas
python3 -m pytest studio_backfill/tests/test_resolver.py -q
python3 scripts/probe_sa_access.py
python3 scripts/probe_shared_drive.py
python3 scripts/probe_dual_cred_upload.py

# 1. Explorar y elegir piloto
python3 -m studio_backfill.cli find-pilot-rows --sample-size 1

# 2. Piloto de una fila de cada caso (el comando lo sugiere el paso anterior)
python3 -m studio_backfill.cli pilot --rows 4,66,505,9,35,1046,298,1
python3 -m studio_backfill.cli status            # verificar que quedó "submitted"

# 3. Corrida completa (en tmux — tarda ~8.7h a 0.2 RPS)
tmux new -s backfill
python3 -m studio_backfill.cli pilot --all

# 4. Recoger (también en tmux; xAI tarda horas/días)
python3 -m studio_backfill.cli collect

# 5. Revisar y reintentar lo que haya fallado
python3 -m studio_backfill.cli failures
python3 -m studio_backfill.cli pilot --retry-failed

# 6. Excel final
python3 -m studio_backfill.cli write-excel
```
