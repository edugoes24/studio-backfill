# studio-backfill

Backfill one-shot: toma un Excel con ~9,386 filas de Studio y para cada una
manda el transcript a la pipeline de xAI, recoge el PDF del backend de
reportes, y lo sube al Shared Drive `Reportes xAI - studio excel file`.

## Pre-requisitos en el bastion

```bash
cd ~/studio-backfill
source .venv/bin/activate   # ya está activo si ves "(.venv)" en el prompt
```

Archivos que tienen que estar en el directorio:
- `.env_backfill` (configuración + secretos; gitignored)
- `auditoria-clases-sa-key.json` (SA key; gitignored)
- `studio_results_final.xlsx` (Excel fuente)

---

## Comandos por uso

### Validar que todo está bien (antes de cualquier corrida)

```bash
# Tests unitarios del resolver (8 casos de Drive)
python3 -m pytest studio_backfill/tests/test_resolver.py -q

# ¿La SA puede leer Drives de tutores?
python3 scripts/probe_sa_access.py

# ¿La SA puede escribir al Shared Drive destino?
python3 scripts/probe_shared_drive.py

# ¿La ADC del bastion puede subir al bucket y firmar URLs?
# (esta es la prueba crítica para Fase 1)
python3 scripts/probe_dual_cred_upload.py
```

### Ver la distribución de casos del Excel + descubrir filas piloto

```bash
python3 -m studio_backfill.cli find-pilot-rows --sample-size 1
```

Imprime cuántas filas hay por caso (A, B, C, D1, D2, D3, E, F) y sugiere un
comando `pilot --rows X,Y,Z,...` con una fila de cada caso.

### Fase 1 — Enviar filas a xAI (encolar)

```bash
# Una sola fila (más seguro para pruebas)
python3 -m studio_backfill.cli pilot --rows 4

# Varias filas específicas
python3 -m studio_backfill.cli pilot --rows 4,66,505,9,35,1046,298,1

# Todas las filas del Excel (toma horas; corré dentro de tmux)
python3 -m studio_backfill.cli pilot --all

# Reintentar solo las que fallaron
python3 -m studio_backfill.cli pilot --retry-failed
```

**Tiempos esperables a distintos RPS** (sobre 6,270 filas que sí van al webhook;
las 3,116 "Sin dato" se skipean al instante):

| `WEBHOOK_RPS` en `.env_backfill` | Tiempo total |
|---|---|
| 0.2 (default, conservador) | ~8.7h |
| 1 | ~1.7h |
| 5 | ~21min |

Cambiar el RPS:
```bash
sed -i 's/^WEBHOOK_RPS=.*/WEBHOOK_RPS=1/' .env_backfill
```

### Fase 2 — Recoger PDFs cuando xAI termine

```bash
# Una pasada (chequea cada fila submitted una vez y termina)
python3 -m studio_backfill.cli collect --once

# Loop continuo: poll cada 15 min hasta que no quede nada pendiente
python3 -m studio_backfill.cli collect
```

xAI tarda **horas o días** en procesar las sesiones (modo batch). Hasta
entonces, `collect` recibe 404 ó 422 "still processing" y deja las filas
para la próxima vuelta.

### Fase 3 — Generar Excel de salida (cuando todas las filas estén listas)

```bash
# Genera studio_results_final_with_reports.xlsx con 3 columnas nuevas:
#   - event_id_xai       (studio-row-N)
#   - pdf_drive_link     (URL del PDF en el Shared Drive, o "" si skip/fail)
#   - backfill_status    (completed | skipped_no_link | failed_pdf_404 | ...)
python3 -m studio_backfill.cli write-excel

# Salida custom
python3 -m studio_backfill.cli write-excel --out /tmp/reporte.xlsx
```

---

## Inspección / debug

```bash
# Resumen de filas por estado
python3 -m studio_backfill.cli status

# Listar todas las filas en estado failed_*
python3 -m studio_backfill.cli failures

# Detalle completo de UNA fila (todo el JSON del state.sqlite)
python3 -m studio_backfill.cli inspect studio-row-4

# SQLite directo (raw)
sqlite3 state.sqlite "SELECT event_id, state, drive_case FROM rows LIMIT 20;"
sqlite3 state.sqlite "SELECT state, COUNT(*) FROM rows GROUP BY state;"
```

### Estados posibles

| Estado | Significa | Reintentable? |
|---|---|---|
| `completed` | PDF en Drive, todo OK | No |
| `submitted` | Posteado a xAI, esperando análisis | Pasa solo a `completed` o `failed_*` |
| `skipped_no_link` | `transcriptLink` vacío o "Sin dato" | No |
| `skipped_unsupported_format` | Formato F (sheet, redirect, etc.) | No |
| `skipped_no_transcript` | Case B/D2 con solo `.mp4` | No |
| `skipped_no_transcript_in_folder` | Folder C/D1 sin Doc adentro | No |
| `failed_drive_read` | Drive devolvió 404/403 al SA | Sí — revisar permisos del Doc |
| `failed_gcs_upload` | Falla al subir transcript a GCS | Sí — usualmente transient |
| `failed_webhook` | xAI rechazó el POST | Sí — `last_error` indica causa |
| `failed_pdf_analysis` | xAI marcó la sesión como `failed` | No — depende de xAI |
| `failed_pdf_404` | ETL aún no movió la data a BQ | Sí (polling automático) |
| `failed_pdf_fetch` | 5xx del backend de reportes | Sí (hasta 5 attempts) |
| `failed_drive_upload` | Subir PDF al Shared Drive falló | Sí — transient |
| `failed_timeout_pending_analysis` | >48h en submitted (xAI trabado) | Solo manual |
| `failed_too_many_attempts` | >5 reintentos automáticos | Solo manual |

---

## Recovery

```bash
# Reintentar filas puntuales (vuelven a pending y se vuelven a procesar)
python3 -m studio_backfill.cli retry --events studio-row-4,studio-row-66

# Reset TOTAL (borra el SQLite local). NO borra cosas en xAI ni en Drive.
# Solo usar si querés empezar de cero por algún cambio del código.
python3 -m studio_backfill.cli reset --confirm
```

---

## Operación en producción (con tmux para SSH disconnect)

```bash
# Sesión persistente
tmux new -s backfill
source .venv/bin/activate
python3 -m studio_backfill.cli pilot --all

# Detach (script sigue corriendo, podés cerrar SSH):
#   Ctrl-B luego D

# Re-attach desde cualquier SSH al bastion:
tmux attach -t backfill

# Listar sesiones tmux activas
tmux ls
```

Mientras corre Phase 1, podés monitorear desde **otra SSH session**:

```bash
watch -n 30 'python3 -m studio_backfill.cli status'
```

---

## Configuración (`.env_backfill`)

Vive en el root del repo (gitignored). Variables relevantes:

| Variable | Default | Para qué |
|---|---|---|
| `WEBHOOK_RPS` | 0.2 | Velocidad del envío al webhook (1 por cada 1/RPS segundos) |
| `PIPELINE_TIMEOUT_HOURS` | 48 | Tras este tiempo en `submitted`, una fila pasa a `failed_timeout_pending_analysis` |
| `POLL_INTERVAL_SECONDS` | 900 | Cada cuánto re-chequea Phase 2 en loop continuo |
| `MAX_ATTEMPTS` | 5 | Reintentos automáticos antes de dar por perdida una fila |
| `GCS_SIGNED_URL_TTL_SECONDS` | 21600 | TTL del signed URL del transcript (6h cubre retries de Pub/Sub) |

Después de tocar el `.env_backfill`, el siguiente comando lo lee automático
(no hace falta reiniciar nada).

---

## Estructura del proyecto

```
studio-backfill/
├── .env_backfill                 ← secretos + config (gitignored)
├── .env_backfill_example         ← template
├── auditoria-clases-sa-key.json  ← SA key (gitignored)
├── studio_results_final.xlsx     ← Excel fuente
├── state.sqlite                  ← checkpoint local (gitignored, se crea solo)
├── scripts/                      ← probes de diagnóstico
│   ├── probe_sa_access.py        ← SA puede leer Drives
│   ├── probe_shared_drive.py     ← SA puede escribir Shared Drive
│   ├── probe_dual_cred_upload.py ← ADC bastion: bucket + signed URL
│   └── ...
└── studio_backfill/              ← paquete Python
    ├── cli.py                    ← CLI entrypoint
    ├── pipeline.py               ← orquestador Phase 1/2
    ├── drive_reader.py           ← resolver de 8 casos
    ├── gcs_uploader.py           ← upload + signed URL
    ├── webhook_client.py         ← HMAC + POST a xAI
    ├── reports_client.py         ← GET PDF al backend de reportes
    ├── drive_writer.py           ← upload PDF al Shared Drive
    ├── state.py                  ← SQLite checkpoint
    ├── excel_io.py               ← read/write del Excel
    ├── config.py                 ← lee .env_backfill
    └── tests/test_resolver.py    ← 26 unit tests
```
