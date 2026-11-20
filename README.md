# Email Event Extraction — Backend

A Flask API that reads unread Gmail messages and extracts event names, dates, times, and venues. The frontend presents the results for review and handles saving to Google Calendar.

- [Frontend repository](https://github.com/Hansika-hub/Email)
- [Live app on Vercel](https://YOUR-FRONTEND.vercel.app)
- [Backend on Render](https://YOUR-BACKEND.onrender.com)

## Extraction pipeline

1. Fetch unread messages using the user's Gmail OAuth access token.
2. Read structured event details from calendar invitations when available. For email text, combine date and time parsing rules with a fine-tuned DistilBERT model.
3. Optionally call Gemini when important fields remain unresolved. Return the extracted fields and their sources when available.
4. Cache successful extractions in SQLite to avoid reprocessing the same messages on repeated scans.

The backend runs DistilBERT locally with 8-bit ONNX weights. On Render, Gunicorn uses one worker and the application allows one scan at a time to limit peak memory use.

## Run locally

Use Python 3.11 or newer and clone this repository.

```bash
git clone https://github.com/Hansika-hub/Email_Backend.git
cd Email_Backend
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python app.py
```

Place `model.onnx`, `config.json`, and `tokenizer.json` in `ner_model/` alongside `app.py`. Use the exported ONNX model and its matching tokenizer and label configuration.

```bash
curl http://localhost:5000/
```

## API

| Method and path | Purpose |
| --- | --- |
| `GET /` | Health check |
| `POST /` | Verify a Google ID token from the sign-in flow |
| `GET /fetch_emails` | List IDs and subjects for up to 10 unread messages |
| `GET /process_emails` | Scan up to 20 unread messages and return extracted events |

The Gmail endpoints take a Google OAuth **access token** with the `gmail.readonly` scope in the `Authorization: Bearer …` header.

## Deploy on Render

Use the settings from `render.yaml` when creating the Python web service. If the service is configured manually, copy its build and start commands into Render. Include the `ner_model/` files in the deployed revision and configure `app.py` with the frontend's OAuth client ID and allowed origin.

Gemini fallback is optional. To enable it, set `LLM_FALLBACK_ENABLED=true`, `GEMINI_MODEL_ID`, and `GOOGLE_API_KEY` in Render. Keep the key in environment variables.

## License

MIT — see [LICENSE](https://github.com/Hansika-hub/Email-Event-Extractor-EEE_Backend/blob/main/LICENSE.txt)
