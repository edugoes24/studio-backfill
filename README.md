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
| `EVENT_ID_PREFIX` | `studio-row` | Prefijo del `event_id` (`<prefijo>-<fila>`). Cámbialo (ej. `studio-row-v2`) para forzar reproceso: xAI trata un `event_id` nuevo como sesión nueva |

Después de tocar el `.env_backfill`, el siguiente comando lo lee automático
(no hace falta reiniciar nada).

---

## Queries útiles del lado xAI

El sistema xAI tiene 2 capas de datos:
- **Cloud SQL operacional** (`xai-dev-goes:us-central1:goes-staging-db`, DB `observability_app_db`) — donde el webhook hace inserts y donde podemos UPDATE-ar manualmente.
- **BigQuery dataset** (`xai-dev-goes.conformed_education_obs_staging`) — donde el ETL promueve la data y de donde lee el backend de reportes para generar el PDF.

Las dos se ven en distintas consolas:
- Cloud SQL: `console.cloud.google.com/sql/instances/goes-staging-db` → Studio
- BigQuery: `console.cloud.google.com/bigquery` con proyecto `xai-dev-goes`

### Cloud SQL — diagnóstico operacional

```sql
-- 1) Estado de una sesión específica
SELECT id, scheduling_app_event_id, school_code, teacher_code, coach_code,
       grade, subject, pipeline_status, status, recorded_at, created_at
FROM sessions
WHERE scheduling_app_event_id = 'studio-row-4';

-- 2) Resumen del progreso del batch en xAI
SELECT pipeline_status, COUNT(*) AS n
FROM sessions
WHERE scheduling_app_event_id LIKE 'studio-row-%'
GROUP BY pipeline_status
ORDER BY n DESC;

-- 3) Sesiones que fallaron en xAI
SELECT scheduling_app_event_id, pipeline_status, error_message, updated_at
FROM sessions
WHERE scheduling_app_event_id LIKE 'studio-row-%'
  AND (pipeline_status = 'failed' OR status = 'failed')
ORDER BY updated_at DESC;

-- 4) ¿Cuántos transcripts tenemos cargados en xAI por nuestro backfill?
SELECT COUNT(*) AS total_studio_sessions
FROM sessions
WHERE scheduling_app_event_id LIKE 'studio-row-%';

-- 5) Escuelas creadas por el backfill (con/sin nombre)
SELECT id, code, name, department, district, created_at
FROM schools
WHERE code LIKE 'studio-school-%'
ORDER BY created_at DESC;

-- 6) Users creados por el backfill (teachers + coaches)
SELECT code, first_name, last_name, role, school_code, created_at
FROM users
WHERE code LIKE 'studio-%'
ORDER BY created_at DESC;

-- 7) UPDATE manual de nombres (cuando staging no tiene la feature de names)
-- Reemplazá los valores literales con los del Excel para esa fila.
UPDATE schools
SET name = 'CENTRO ESCOLAR LA PAZ',
    department = 'La Paz',
    district = 'San Miguel Tepezontes',
    updated_at = NOW()
WHERE code = 'studio-school-12001';

UPDATE users
SET first_name = 'ZEPEDA, LOIDA REBECA', last_name = '', updated_at = NOW()
WHERE code = 'studio-teacher-ZEPEDA-LOIDA-REBECA' AND role = 'teacher';
```

### BigQuery — lo que el reports backend lee

```sql
-- 1) Verificar que el ETL migró la sesión a BQ
SELECT scheduling_app_event_id, school_code, teacher_code, coach_code,
       pipeline_status, session_status, is_deleted
FROM `xai-dev-goes.conformed_education_obs_staging.sessions`
WHERE scheduling_app_event_id = 'studio-row-4';

-- 2) ¿Los nombres llegaron a dim_schools?
SELECT school_code, school_name, department, district
FROM `xai-dev-goes.conformed_education_obs_staging.dim_schools`
WHERE school_code LIKE 'studio-school-%';

-- 3) ¿Y a dim_users? (full_name = CONCAT(first_name, ' ', last_name))
-- Ojo: el JOIN del reports backend usa la columna `user_code`, no `code`
SELECT user_code, full_name, role
FROM `xai-dev-goes.conformed_education_obs_staging.dim_users`
WHERE user_code LIKE 'studio-%';

-- 4) Simular EXACTAMENTE lo que el backend de reportes ve cuando genera el PDF
SELECT
    s.session_id, s.pipeline_status, s.session_status,
    NULLIF(TRIM(tu.full_name), '') AS teacher_name,
    NULLIF(TRIM(cu.full_name), '') AS coach_name,
    sc.school_name, sc.department, sc.district,
    s.grade, s.section, s.subject, s.shift,
    ar.status AS analysis_status, ares.overall_score
FROM `xai-dev-goes.conformed_education_obs_staging.sessions` s
LEFT JOIN `xai-dev-goes.conformed_education_obs_staging.dim_users` tu ON s.teacher_code = tu.user_code
LEFT JOIN `xai-dev-goes.conformed_education_obs_staging.dim_users` cu ON s.coach_code = cu.user_code
LEFT JOIN `xai-dev-goes.conformed_education_obs_staging.dim_schools` sc ON s.school_code = sc.school_code
LEFT JOIN `xai-dev-goes.conformed_education_obs_staging.analysis_runs` ar ON s.session_id = ar.session_id
LEFT JOIN `xai-dev-goes.conformed_education_obs_staging.analysis_results` ares ON ar.analysis_run_id = ares.analysis_run_id
WHERE s.scheduling_app_event_id = 'studio-row-4'
QUALIFY ROW_NUMBER() OVER (ORDER BY ar.completed_at DESC NULLS LAST) = 1;

-- 5) Resumen agregado del batch (qué % de filas tienen análisis disponible)
SELECT
  COUNT(*) AS total_sessions,
  COUNTIF(ares.overall_score IS NOT NULL) AS with_analysis,
  COUNTIF(ares.overall_score IS NULL) AS without_analysis
FROM `xai-dev-goes.conformed_education_obs_staging.sessions` s
LEFT JOIN `xai-dev-goes.conformed_education_obs_staging.analysis_runs` ar ON s.session_id = ar.session_id
LEFT JOIN `xai-dev-goes.conformed_education_obs_staging.analysis_results` ares ON ar.analysis_run_id = ares.analysis_run_id
WHERE s.scheduling_app_event_id LIKE 'studio-row-%';

-- 6) Buscar sesiones con análisis "incompleto" (datos faltantes que harían que el PDF dé 422)
SELECT s.scheduling_app_event_id, s.pipeline_status, s.session_status,
       ares.overall_score, ar.status AS analysis_status
FROM `xai-dev-goes.conformed_education_obs_staging.sessions` s
LEFT JOIN `xai-dev-goes.conformed_education_obs_staging.analysis_runs` ar ON s.session_id = ar.session_id
LEFT JOIN `xai-dev-goes.conformed_education_obs_staging.analysis_results` ares ON ar.analysis_run_id = ares.analysis_run_id
WHERE s.scheduling_app_event_id LIKE 'studio-row-%'
  AND (ares.overall_score IS NULL OR ares.results_data IS NULL);
```

### Notas de detalle

- **dim_users.user_code vs users.code**: el ETL renombra `code` → `user_code` en la migración a BigQuery. En consultas a la dim, siempre `user_code`.
- **dim_users.full_name**: el silver ETL hace `CONCAT(first_name, ' ', last_name)`. Si en operacional dejaste `last_name = ''`, full_name = first_name con un espacio al final (algunas versiones del ETL trimean, otras no).
- **ETL es horario** (Cloud Scheduler): si UPDATE-aste en Cloud SQL pero la dim aún muestra valores viejos, hay que esperar la próxima corrida del ETL (~1h máximo) o que pidan al equipo xAI dispararlo manualmente.
- **PDF cacheado en GCS**: el backend de reportes guarda el PDF generado en `gs://ia-observabilidad-docentes-bucket/reports_observability/reporte_sesion_<event_id>.pdf`. Si ya hay un PDF cacheado de una corrida vieja (con em-dashes), borralo para forzar regeneración:
  ```bash
  gcloud storage rm gs://ia-observabilidad-docentes-bucket/reports_observability/reporte_sesion_studio-row-4.pdf
  ```

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
