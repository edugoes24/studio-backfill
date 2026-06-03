# Colección de Postman — Webhook de xAI

Permite disparar manualmente el webhook de xAI (`POST /webhook`) con un payload
firmado por HMAC, replicando lo que hace `studio_backfill/webhook_client.py`. Útil
para depurar el formato del payload y para pruebas de carga.

## Qué necesitas

El webhook **no recibe el archivo**, recibe un **enlace firmado (`signed_url`)** que
apunta a un transcript en el bucket; xAI lo descarga con ese enlace. Así que el flujo
de uso es: **generar una signed URL de un objeto del bucket → pegarla en Postman → enviar.**

## 1. Importar la colección

En Postman: *Import* → `postman/xai-webhook.postman_collection.json`.

## 2. Rellenar las variables de la colección

| Variable | Cómo obtenerla |
|---|---|
| `webhook_url` | `gcloud secrets versions access latest --secret=class-observability-backend-function-webhook-url --project=g-edu-room-mon-prd-prj-65cd` |
| `webhook_secret` | `gcloud secrets versions access latest --secret=class-observability-backend-function-webhook-secret --project=g-edu-room-mon-prd-prj-65cd` |

> **Sobre el secreto HMAC:** lo ideal es ponerlo en la variable `webhook_secret`
> (pestaña *Variables* → columna **Current Value**; al ser tipo `secret` no se
> importa con la colección, hay que escribirlo). Como atajo para pruebas, también
> puedes pegarlo directo en la constante `SECRET_RAW` al inicio del *pre-request
> script* — si tiene valor, tiene prioridad sobre la variable. **No subas el secreto
> al repo en ninguno de los dos casos.**
| `signed_url` | Generarla (paso 3) |
| `event_id` | Uno **nuevo** cada vez (ej. `loadtest-0001`). Si repites uno existente, xAI lo salta. |
| `teacher_code`, `coach_code`, `school_code`, `grade`, `subject`, `section`, `shift` | Valores de prueba (ya traen defaults) |

`recorded_at` se rellena solo en el pre-request si lo dejas vacío.

## 3. Generar la `signed_url` de un objeto del bucket

Una signed URL solo la puede firmar una **service account**. Lo más fácil es generarla
**desde el bastion** (su SA firma vía IAM `signBlob`), sobre un transcript que ya exista
en el bucket (p. ej. los que subió el backfill en `studio-backfill/<event_id>/transcript.docx`):

```bash
# En el bastion
python3 -c "
import google.auth, google.auth.transport.requests
from datetime import timedelta
from google.cloud import storage
creds, _ = google.auth.default()
creds.refresh(google.auth.transport.requests.Request())
c = storage.Client(credentials=creds, project='g-edu-room-mon-prd-prj-65cd')
blob = c.bucket('videos-and-transcripts-bucket-prod').blob('studio-backfill/studio-row-1/transcript.docx')
url = blob.generate_signed_url(
    version='v4', method='GET', expiration=timedelta(hours=6),
    service_account_email=creds.service_account_email, access_token=creds.token,
)
print(url)
"
```

Copia la URL impresa y pégala en la variable `signed_url`. (Vigencia: 6 h.)

> ¿Subir un transcript de prueba propio? Súbelo al bucket primero
> (`gcloud storage cp mi_transcript.txt gs://videos-and-transcripts-bucket-prod/loadtest/t1.txt`)
> y firma esa ruta.

## 4. Enviar

Envía la request **POST /webhook**. El *pre-request script*:
1. Fija `recorded_at` (si está vacío) como variable normal.
2. Calcula `X-Webhook-Timestamp` (Unix segundos).
3. Resuelve las `{{variables}}` del body para firmar **exactamente** lo que se enviará.
4. Calcula `X-Webhook-Signature = sha256=HMAC_SHA256(secret, "{ts}.{body}")`.

Respuesta esperada: **202 Accepted** con `message_id`. El *test script* lo guarda en
`last_message_id` y lo imprime en la consola de Postman.

## Notas importantes (firma)

- **No uses variables dinámicas `{{$isoTimestamp}}`/`{{$guid}}` dentro del body.** Se
  resolverían dos veces (al firmar y al enviar) con valores distintos → la firma no
  cuadra → `401`. Por eso `recorded_at` se fija como variable normal en el pre-request.
- El body que se firma debe ser **byte-idéntico** al enviado. El script firma
  `pm.variables.replaceIn(pm.request.body.raw)`, que es justo lo que Postman manda.
- El servidor tolera **±300 s** en el timestamp; ten el reloj sincronizado.

## Verificar del lado de xAI

Tras el 202, en Cloud SQL / BigQuery:
```sql
SELECT scheduling_app_event_id, pipeline_status, status, created_at
FROM sessions WHERE scheduling_app_event_id = 'loadtest-0001';
```

## Para pruebas de carga

- Cada `event_id` nuevo = sesión nueva en xAI = **~107 requests al Batch API** por sesión.
- Usa `event_id` únicos (`loadtest-<n>`) y un entorno acordado con xAI; contempla la
  amplificación y el costo (llamadas LLM reales). Ver `PIPELINE.md` (apéndice del contrato).
