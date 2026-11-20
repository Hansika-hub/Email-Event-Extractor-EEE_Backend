import os
import re
import json
from typing import Any, Dict, List, Optional, Tuple
from bs4 import BeautifulSoup
from dateparser.search import search_dates
import datetime as _dt
from functools import lru_cache
import logging

# Debug logging toggle
DEBUG_NER = os.getenv("DEBUG_NER", "0") not in (None, "", "0", "false", "False")
logger = logging.getLogger("ner")
if DEBUG_NER:
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        _h = logging.StreamHandler()
        _h.setLevel(logging.INFO)
        _h.setFormatter(logging.Formatter("[NER] %(message)s"))
        logger.addHandler(_h)
    logger.propagate = False

def _dlog(msg: str):
    if DEBUG_NER:
        logger.info(msg)

# ---------- Event Name Extraction from Subject ----------
def clean_event_name(subject):
    if not subject:
        return None
    
    # Remove common prefixes
    subject = re.sub(r'^(re:|fwd:|\[.*?\])\s*', '', subject, flags=re.IGNORECASE)
    
    # Remove trailing date/time if present
    subject = re.sub(
        r'\b(\d{1,2}(\s|-|/)\d{1,2}(\s|-|/)\d{2,4}|\d{1,2}:\d{2}(\s?(AM|PM))?)$',
        '',
        subject,
        flags=re.IGNORECASE
    )
    
    # Remove excessive spaces
    subject = re.sub(r'\s+', ' ', subject).strip()
    
    # Capitalize properly
    return subject.title()


def _normalize_date(d: _dt.date) -> str:
    return d.strftime("%Y-%m-%d")

def _normalize_time(t: _dt.time) -> str:
    return t.strftime("%H:%M")

def _clean_text(html_or_text: Optional[str]) -> str:
    if not html_or_text:
        return ""
    # If it looks like HTML, strip tags conservatively
    txt = html_or_text
    if "<" in txt and ">" in txt:
        try:
            soup = BeautifulSoup(txt, "html.parser")
            txt = soup.get_text(" ")
        except Exception:
            pass
    # Collapse whitespace
    txt = "\n".join(re.sub(r"[^\S\n]+", " ", line).strip() for line in txt.splitlines()).strip()
    return txt

def _extract_date_and_time(text: str, reference=None) -> Tuple[Optional[str], Optional[str], Optional[int]]:
    """Extract date and time using a simple, reliable approach."""
    if not text:
        return None, None, None
    
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    
    # Step 1: Find the best date using dateparser with better settings
    try:
        results = search_dates(text, settings={
            "RETURN_AS_TIMEZONE_AWARE": False,
            "PREFER_DATES_FROM": "future",
            **({"RELATIVE_BASE": reference} if reference else {}),
            "DATE_ORDER": "DMY",  # Handle DD.MM.YYYY format better
            "PREFER_DAY_OF_MONTH": "first",
        }) or []
    except Exception:
        results = []
    
    if not results:
        return None, None, None
    
    # Get the best date (prefer future, then first)
    now = reference or _dt.datetime.now()
    future_dates = [dt for _, dt in results if dt >= now]
    best_date = future_dates[0] if future_dates else results[0][1]
    date_str = _normalize_date(best_date.date())
    
    # Step 2: Find the best time in the entire text
    time_str = _extract_best_time(text)
    
    # Step 3: Find the line index for venue proximity
    anchor_idx = None
    for i, line in enumerate(lines):
        if any(match_text in line for match_text, _ in results):
            anchor_idx = i
            break
    
    return date_str, time_str, anchor_idx

def _extract_best_time(text: str) -> Optional[str]:
    """Extract the best time from text using priority order."""
    if not text:
        return None
    
    # Priority 1: Time ranges like "12:30pm - 2pm" (take start time)
    range_pattern = re.compile(r"\b(\d{1,2})(?::([0-5]\d))?\s*[\-–—to]+\s*(\d{1,2})(?::([0-5]\d))?\s*(AM|PM)\b", re.IGNORECASE)
    m = range_pattern.search(text)
    if m:
        hh = int(m.group(1))
        mm = int(m.group(2) or 0)
        ampm = m.group(5).upper()
        if ampm == "PM" and hh < 12:
            hh += 12
        if ampm == "AM" and hh == 12:
            hh = 0
        return f"{hh:02d}:{mm:02d}"
    
    # Priority 2: Explicit AM/PM times
    ampm_pattern = re.compile(r"\b(\d{1,2})(?::([0-5]\d))?\s*(AM|PM)\b", re.IGNORECASE)
    m = ampm_pattern.search(text)
    if m:
        hh = int(m.group(1))
        mm = int(m.group(2) or 0)
        ampm = m.group(3).upper()
        if ampm == "PM" and hh < 12:
            hh += 12
        if ampm == "AM" and hh == 12:
            hh = 0
        return f"{hh:02d}:{mm:02d}"
    
    # Priority 3: 24-hour format
    hhmm_pattern = re.compile(r"\b(\d{1,2}):([0-5]\d)\b")
    m = hhmm_pattern.search(text)
    if m:
        hh = int(m.group(1))
        mm = int(m.group(2))
        if 0 <= hh <= 23:
            return f"{hh:02d}:{mm:02d}"
    
    return None


# ---------- Venue Extraction Using Regex (fallback) ----------
VENUE_KEYWORDS = [
    "hall", "auditorium", "room", "centre", "center", "complex",
    "stadium", "building", "block", "lab", "library", "theatre",
    "theater", "gym", "campus", "conference room", "banquet", "park",
    "ground", "lawn"
]

VENUE_REGEX = re.compile(
    r"\b(?:Hall|Room|Block|Building|Centre|Center|Auditorium|Stadium|Theatre|Theater|Lab|Library|Gym|Campus|Park|Ground|Lawn|Conference Room|Seminar Hall)"
    r"(?:\s+[A-Za-z0-9&\-]+){0,5}",
    re.IGNORECASE
)

def extract_venue(text: str, anchor_line_index: Optional[int] = None) -> Optional[str]:
    candidates: List[str] = []
    lines = [l.strip() for l in (text or "").splitlines() if l.strip()]
    # 1) Explicit labels
    label_prefixes = ("venue:", "where:", "location:", "address:")
    for l in lines:
        lower = l.lower()
        if any(lower.startswith(p) for p in label_prefixes):
            val = re.sub(r"^(venue:|where:|location:|address:)\s*", "", lower, flags=re.IGNORECASE)
            candidates.append(val.strip())
    # 2) Regex place-like
    for match in VENUE_REGEX.findall(text or ""):
        cleaned = match.strip()
        if cleaned:
            candidates.append(cleaned)
    # 3) Proximity heuristic near date/time line
    if anchor_line_index is not None and 0 <= anchor_line_index < len(lines):
        start = max(0, anchor_line_index - 3)
        end = min(len(lines), anchor_line_index + 4)
        for l in lines[start:end]:
            if any(kw in l.lower() for kw in VENUE_KEYWORDS):
                candidates.append(l.strip())
    # Post-clean
    for i in range(len(candidates)):
        v = candidates[i]
        # Remove date/time patterns more aggressively
        v = re.sub(r"\b\d{1,2}[./-]\d{1,2}[./-]\d{2,4}\b", "", v)  # Remove dates like 27.09.2025
        v = re.sub(r"\bat\s+\d{1,2}(:[0-5]\d)?\s?(AM|PM|am|pm)\b", "", v)
        v = re.sub(r"\b\d{1,2}(:[0-5]\d)?\s?(AM|PM|am|pm)\b", "", v)
        v = re.sub(r"\bon\s+\d{1,2}[./-]\d{1,2}[./-]\d{2,4}\b", "", v)  # Remove "on 27.09.2025"
        v = re.sub(r"\b\d{1,2}[./-]\d{1,2}[./-]\d{2,4}\b", "", v)  # Remove any remaining dates
        v = v.strip(",;:- ")
        candidates[i] = v
    # Return the first reasonable candidate
    for v in candidates:
        if v and len(v) >= 2 and not re.search(r"\d{1,2}[./-]\d{1,2}", v):  # Final check: no date patterns
            return v
    return None


MODEL_ID = os.getenv("HF_MODEL_ID", "Hansika08/Distilbert-case-based_Fine-tuned_NER")
LABEL_FIELDS = {
    "EVENT": "event_name", "EVENT_NAME": "event_name", "EVENTNAME": "event_name",
    "TITLE": "event_name", "DATE": "date", "TIME": "time", "LOCATION": "venue",
    "LOC": "venue", "VENUE": "venue", "GPE": "venue", "FAC": "venue",
}

@lru_cache(maxsize=1)
def load_ner():
    from transformers import AutoModelForTokenClassification, AutoTokenizer, pipeline
    options = {"revision": os.getenv("HF_MODEL_REVISION", "main"),
               "token": os.getenv("HF_TOKEN") or os.getenv("HUGGINGFACE_API_TOKEN")}
    tokenizer = AutoTokenizer.from_pretrained(MODEL_ID, use_fast=True, **options)
    model, info = AutoModelForTokenClassification.from_pretrained(
        MODEL_ID, output_loading_info=True, **options)
    if any("classifier" in key for key in info["missing_keys"]):
        raise ValueError("Missing trained token classifier weights")
    if model.config.model_type != "distilbert":
        raise ValueError("Expected a DistilBERT checkpoint")
    mapping = {**LABEL_FIELDS, **json.loads(os.getenv("NER_LABEL_MAP", "{}"))}
    labels = {re.sub(r"^[BI]-", "", label).upper() for label in model.config.id2label.values()}
    unknown = labels - {"O"} - mapping.keys()
    if unknown:
        raise ValueError(f"Configure NER_LABEL_MAP for {sorted(unknown)}")
    return pipeline("token-classification", model=model, tokenizer=tokenizer,
                    aggregation_strategy="simple", stride=64, device=-1), mapping


def extract_event_details(subject, body, reference=None):
    text = _clean_text(body)
    input_text = (subject or "") + "\n" + text
    date, time, anchor = _extract_date_and_time(input_text, reference)
    fields = {"event_name": None, "date": date, "time": time,
              "venue": extract_venue(input_text, anchor)}
    sources = {k: "rules" for k, v in fields.items() if v}
    trace = ["rules"]
    try:
        predict, mapping = load_ner()
        spans = sorted(predict(input_text), key=lambda span: -span["score"])
        for span in spans:
            key = mapping.get(span["entity_group"].upper())
            if key not in fields or fields[key]:
                continue
            value = input_text[span["start"]:span["end"]].strip()
            if key == "date":
                value = _extract_date_and_time(value, reference)[0]
            elif key == "time":
                value = _extract_best_time(value)
            if value:
                fields[key], sources[key] = value, "ner"
        trace.append("ner" if spans else "ner (no entities)")
    except Exception as error:
        logger.warning("NER unavailable (%s)", type(error).__name__)
        trace.append("ner unavailable")
    if not fields["event_name"] and subject:
        fields["event_name"] = clean_event_name(subject)
        sources["event_name"] = "subject"
    # A venue is optional. A specified start time should be resolved when possible.
    needs_help = not fields["date"] or not fields["event_name"] or (
        not fields["time"] and bool(re.search(r"\b(?:noon|midnight|o.clock|am|pm)\b", input_text, re.I)))
    if needs_help and os.getenv("LLM_FALLBACK_ENABLED", "false").lower() == "true":
        from llm_fallback import extract_with_gemini
        prediction = extract_with_gemini(subject, text, reference=reference)
        trace.append("gemini" if prediction else "gemini unavailable")
        for key in fields:
            value = (prediction or {}).get(key)
            if not fields[key] and isinstance(value, str) and value.strip():
                fields[key], sources[key] = value.strip(), "gemini"
    return {**fields, "event": fields["event_name"], "source": "+".join(dict.fromkeys(sources.values())),
            "field_sources": sources, "trace": trace}

def count_event_fields(details: Dict[str, Optional[str]]) -> int:
    present = 0
    for key in ("date", "time", "venue"):
        value = details.get(key)
        if value and str(value).strip():
            present += 1
    return present


def is_event_like(details: Dict[str, Optional[str]], minimum_required: int = 2) -> bool:
    return count_event_fields(details) >= max(0, int(minimum_required))


# ---------- Test ----------
if __name__ == "__main__":
    subj = "Re: [Reminder] Climate Action 2025 - 19 Nov 2025 10:00 AM"
    body = "Join us for the Climate Action 2025 conference on 19 Nov 2025 at 10:00 AM at Global Sustainability Center."
    print(extract_event_details(subj, body))
