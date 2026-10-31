# Prescription OCR API

Python 3.12+ / FastAPI. One business endpoint: `POST /ocr`, no authentication.
Swagger: http://localhost:9512/docs. OpenAPI: http://localhost:9512/openapi.json.

## Setup and run

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
export GEMINI_API_KEY='your-key'
export OPENAI_API_KEY='your-key'
export DATABASE_URL='postgresql://user:password@host:5432/database'
.venv/bin/python print_ddl.py
# Have your database administrator execute the printed CREATE TABLE statement.
.venv/bin/python run.py
```

The application never installs, starts, creates, or migrates PostgreSQL. It opens a
connection pool only when a DSN is configured. The HTTP service can start without
credentials; OCR requests return 503 until the required dependencies are configured.
Environment variables are secret substitutions referenced by the single `config.yaml`;
there is no .env loader or secondary settings file. Restart after configuration changes.

## Requests

```bash
curl -X POST http://localhost:9512/ocr \
  -F type=image -F image=@prescription.png -F provider=gemini

curl -X POST http://localhost:9512/ocr \
  -F type=path -F path=/u04/py/vx_ocr_engine/prescription.png -F provider=openai
```

Both use multipart/form-data. `provider` is optional and defaults to configuration.
`type` selects the source if both image and path are supplied; sources are never combined.
Paths refer to files on the API server, not the caller's computer. Adjust
`image.allowed_path_roots` to your image directory. Keep these directories controlled
by the service operator; the path input is intended for trusted internal callers.

Flow: selected image -> Pillow validation -> `isThisImageAPrescription(image)`
(always true for now) -> Gemini/OpenAI adapter -> local schema/confidence validation
-> PostgreSQL transaction -> success response. A DB preflight avoids provider charges
when the configured DB is already unavailable. Original bytes are base64 encoded for
transport; there is no resize, EXIF rotation, enhancement, crop, or re-encoding.
Only single-frame JPEG/PNG/WebP are accepted by default, using detected format rather
than trusting filenames or upload MIME types. Pillow is used only for validation.

## Configuration and output

`config.yaml` contains model names, provider endpoints, temperature, maximum output
tokens, per-attempt and total provider timeouts, retries, exponential backoff, maximum
delay, retryable HTTP statuses, image/request limits, path roots, DB pool settings,
SQL statements, prompt/version, review thresholds, and the complete JSON schema.
Choose a vision model supporting strict JSON schema and temperature. Incompatible
model options produce a provider error; parameters are not silently dropped.

The extraction preserves the supplied prescription fields and adds:

- `field_confidence`: one entry per non-null scalar field, including scalar array
  items, with JSON Pointer `field_path`, `confidence` (0–1), and `accuracy_percentage`.
- `overall_confidence` (0–1) and `overall_accuracy_percentage` (0–100).

These are model estimates of transcription confidence, not measured accuracy or
clinical probabilities. Percentages are recalculated from confidence. Missing/null
fields have no confidence entry. Duplicate/missing/unexpected paths reject the
provider response. Review flags and confidence metadata do not need confidence entries.
Low-confidence fields, missing medicine name/dosage, medication uncertainty, force review. Invalid normalized dates reject the provider response. The API never clears provider review flags.
`visit_after` means explicitly recorded days; other follow-up notations remain in
`advice`, avoiding an invented unit. Prompt/schema can be edited together in YAML.

Success includes `id`, `extraction`, and `metadata` with provider/model, prompt version,
token counts and raw provider usage, attempts, provider latency (including retries),
total latency (including storage), and `storage_status=stored`. Missing token counts
are null, never fabricated. Gemini raw usage may include thinking tokens.
PostgreSQL stores extracted JSON and metadata plus the original image SHA-256 hash,
not the image bytes or filesystem path. There is no retrieval endpoint.

Errors use `{"status":"error","error":{"code":"...","message":"..."}}`:
413 size limit; 415 image format; 422 invalid input/image; 502 provider failure,
refusal, truncation or schema/confidence mismatch; 503 configuration, storage or
capacity failure; 504 provider timeout. Raw provider error bodies and prescription
contents are not exposed in errors. Connection errors/timeouts and configured HTTP
statuses retry; malformed JSON, refusal, schema failures and permanent HTTP errors
do not. Retry-After is respected up to the configured maximum delay. Retry count
means additional attempts. A total provider budget bounds calls plus retry waits.

A failed storage commit never returns success. A lost connection during commit can
leave its outcome unknown; client resubmission creates a new request and may incur
another provider call and stored record. Provider retries can also incur charges.

## Checks

```bash
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
```

Tests use mocked providers/storage and do not require credentials or a live database.
The provided DDL lives in `database.create_sql`; `print_ddl.py` emits it verbatim.

## Current workspace verification

Dependencies were installed from cached wheels using the available Python 3.15
interpreter. This interpreter lacks the standard `_ctypes` extension required by
psycopg. Use a complete Python installation (including ctypes) for a configured
PostgreSQL connection; recreate `.venv` with it using the setup instructions above.
The DB driver is loaded only when a DSN is configured. No live DB or provider call
was made. This execution sandbox also denies listening sockets, so it cannot host
the service on port 9512. Run `python run.py` in your normal server environment.

The sandbox blocks cross-thread socket wakeups used by the async HTTP tests. The
21-test suite was verified with a temporary selector polling wrapper, leaving
application code unchanged. In a normal environment use the pytest command above.

Provider references: [OpenAI structured output](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create)
and [Gemini structured output](https://ai.google.dev/gemini-api/docs/generate-content/structured-output).
