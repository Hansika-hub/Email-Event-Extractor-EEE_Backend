# Event Email Extraction (EEE) — Backend API

The Flask web service backend that powers the **Event Email Extraction (EEE) - Event Reminder** application. 

This backend acts as the extraction pipeline, communicating with the Gmail API to pull unread messages and processing their content to extract event information (`date`, `time`, and `venue`) via a multi-tiered pipeline consisting of rules, a custom NER (Named Entity Recognition) model, and a generative LLM (Gemini) fallback.

---

## 🚀 Core Features

*   **Secure Session & OAuth Flow**: Verifies Google Identity ID tokens and validates user authentication.
*   **Gmail Integration**: Reads unread mail headers, decodes text/html payloads, and extracts attached or embedded ICS calendar events.
*   **Hybrid Extraction Pipeline**:
    1.  **Rule-based parser**: Uses `dateparser` and regex heuristics to look for dates, times, and venue keywords (hall, room, lab, etc.) within line proximity.
    2.  **NER Model**: Submits texts to a custom DistilBERT model (`Thiyaga158/Distilbert_Ner_Model_For_Email_Event_Extraction`) hosted on the Hugging Face Inference API.
    3.  **LLM Fallback (Gemini)**: Utilizes `gemini-1.5-flash` to extract structured details from complex or unstructured email layouts.
*   **SQLite Storage**: Saves successfully parsed event objects to a local sqlite database (`events.db`).
*   **Auto-Cleanup Scheduler**: Deletes expired event entries (older than 1 hour past the event execution window).
*   **Cache Optimization**: Caches Gmail messages by message ID to prevent duplicate extraction requests.

---

## 🛠️ Technology Stack

*   **Language**: Python 3.10
*   **Framework**: Flask (with Flask-CORS)
*   **Libraries**:
    *   `google-api-python-client` & `google-auth` (Gmail API client)
    *   `google-generativeai` (Gemini API integration)
    *   `dateparser` (semantic date searches)
    *   `beautifulsoup4` (cleaning HTML formatting)
    *   `icalendar` (ICS attachments parsing)
    *   `sqlite3` (database storage)
*   **Deployment Configuration**: Render blueprint (`render.yaml`)

---

## 🗺️ API Endpoints

| Method | Endpoint | Description |
| :--- | :--- | :--- |
| `GET` | `/` | Health check endpoint. |
| `POST` | `/` | Google ID Token authentication. |
| `GET/POST`| `/fetch_emails` | Retrieves subjects and IDs of up to 10 unread emails. |
| `GET/POST`| `/process_emails`| Pulls emails, decodes text/ICS, runs extraction pipeline, writes to DB, returns list of events. |
| `GET/POST`| `/debug_token` | Debugs credentials by querying Google Tokeninfo. |
| `POST` | `/cleanup_reminders` | Clears expired event entries from SQLite. |

---

## ⚙️ Environment Variables

Create a `.env` file in the root folder to configure the backend:

```env
PORT=5000
GOOGLE_API_KEY=your_gemini_api_key_here
LLM_FALLBACK_ENABLED=true
LLM_FIRST=false
HUGGINGFACE_API_TOKEN=your_huggingface_token_here
DEBUG_NER=0
```

---

## 💻 Local Setup

1.  **Clone the Repository**:
    ```bash
    git clone https://github.com/Hansika-hub/Email_Backend.git
    cd Email_Backend
    ```

2.  **Set Up Virtual Environment**:
    ```bash
    python3 -m venv venv
    source venv/bin/activate
    ```

3.  **Install Dependencies**:
    ```bash
    pip install -r requirements.txt
    ```

4.  **Configure Environment**:
    Rename/create `.env` and fill in your API tokens.

5.  **Run the Server**:
    ```bash
    python app.py
    ```
    The application will launch on `http://localhost:5000`.

---

## ☁️ Deployment

This service can be easily deployed to **Render** using the included `render.yaml` configuration:
*   The blueprint provisions a python web service named `eee-backend`.
*   Includes build scripting and registers target environment variables (`HF_MODEL_ID`, etc.).
