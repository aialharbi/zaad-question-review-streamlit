from __future__ import annotations

import csv
from difflib import SequenceMatcher
import html
import io
import json
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote

import streamlit as st

APP_DIR = Path(__file__).resolve().parent
DATA_FILE = APP_DIR / "data" / "questions_retrieval_test_best.jsonl"
LOCAL_DB_FILE = APP_DIR / "local_review_state.sqlite3"

DEFAULT_USERS: dict[str, dict[str, str]] = {
    "reviewer1": {"display_name": "المحكم 1", "role": "reviewer"},
    "reviewer2": {"display_name": "المحكم 2", "role": "reviewer"},
    "reviewer3": {"display_name": "المحكم 3", "role": "reviewer"},
    "reviewer4": {"display_name": "المحكم 4", "role": "reviewer"},
    "reviewer5": {"display_name": "المحكم 5", "role": "reviewer"},
    "admin": {"display_name": "الأدمن", "role": "admin"},
}
REVIEWER_ORDER = tuple(username for username, user in DEFAULT_USERS.items() if user["role"] == "reviewer")

DECISIONS = {
    "approved": "اعتماد",
    "rejected": "رفض",
}
LEGACY_DECISIONS = {
    "approved_with_edit": "اعتماد",
    "approved_prefer_edit": "اعتماد",
    "needs_revision": "اعتماد",
    "approved_must_edit": "اعتماد",
}
DECISION_LABELS = {**DECISIONS, **LEGACY_DECISIONS}
LEGACY_DECISION_MAP = {
    "approved_with_edit": "approved",
    "approved_prefer_edit": "approved",
    "needs_revision": "approved",
    "approved_must_edit": "approved",
}

STATUS_OPTIONS = {
    "all": "كل الأسئلة",
    "pending": "لم أراجعها",
    "reviewed": "راجعتها",
}
ADMIN_STATUS_OPTIONS = {
    "all": "كل الأسئلة",
    "pending": "لم يراجعها المراجع المكلف",
    "reviewed": "راجعها المراجع المكلف",
    "any_review": "عليها أي مراجعة",
}
TEXT_KEYS = (
    "question_id",
    "review_bank_index",
    "question",
    "short_answer",
    "answer_span",
    "supporting_context",
    "primary_discipline",
    "topic_level_1",
    "topic_level_2",
    "topic_level_3",
    "primary_topic",
    "question_type",
    "difficulty",
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def clean_text(value: str | None) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def as_int(value: Any, fallback: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def as_float(value: Any, fallback: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return fallback


def normalize_for_change(text_value: str | None) -> str:
    normalized = normalize_arabic(text_value or "")
    normalized = normalized.replace("ـ", "")
    normalized = re.sub(r"[^\w\s؀-ۿ]", " ", normalized)
    return re.sub(r"\s+", " ", normalized).strip()


def text_change_rate(original: str | None, edited: str | None) -> float:
    original_norm = normalize_for_change(original)
    edited_norm = normalize_for_change(edited)
    if original_norm == edited_norm:
        return 0.0
    if not original_norm or not edited_norm:
        return 100.0

    sequence_similarity = SequenceMatcher(None, original_norm, edited_norm).ratio()
    original_words = set(original_norm.split())
    edited_words = set(edited_norm.split())
    word_union = original_words | edited_words
    word_overlap = (len(original_words & edited_words) / len(word_union)) if word_union else sequence_similarity
    length_gap = abs(len(original_norm) - len(edited_norm)) / max(len(original_norm), len(edited_norm), 1)
    length_similarity = 1 - min(length_gap, 1)
    smart_similarity = (0.60 * sequence_similarity) + (0.30 * word_overlap) + (0.10 * length_similarity)
    return round(max(0.0, min(100.0, (1 - smart_similarity) * 100)), 1)


def review_change_metrics(question: dict[str, Any], edited_question: str | None, edited_answer: str | None) -> dict[str, float]:
    question_rate = text_change_rate(question.get("question", ""), edited_question or "")
    answer_rate = text_change_rate(question.get("short_answer", ""), edited_answer or "")
    question_weight = max(len(normalize_for_change(question.get("question", ""))), len(normalize_for_change(edited_question or "")), 1)
    answer_weight = max(len(normalize_for_change(question.get("short_answer", ""))), len(normalize_for_change(edited_answer or "")), 1)
    overall = ((question_rate * question_weight) + (answer_rate * answer_weight)) / (question_weight + answer_weight)
    return {
        "question_change_rate": question_rate,
        "answer_change_rate": answer_rate,
        "overall_change_rate": round(overall, 1),
    }


def metric_or_computed(review: dict[str, Any], key: str, computed: dict[str, float]) -> float:
    stored = review.get(key)
    stored_rate = as_float(stored, computed[key])
    if stored in (None, "", 0, 0.0, "0", "0.0") and computed[key] > 0:
        return computed[key]
    return round(stored_rate, 1)


def review_metrics_for_export(question: dict[str, Any], review: dict[str, Any] | None) -> dict[str, float]:
    if not review:
        return {"question_change_rate": 0.0, "answer_change_rate": 0.0, "overall_change_rate": 0.0}
    computed = review_change_metrics(question, review.get("edited_question", ""), review.get("edited_answer", ""))
    return {
        "question_change_rate": metric_or_computed(review, "question_change_rate", computed),
        "answer_change_rate": metric_or_computed(review, "answer_change_rate", computed),
        "overall_change_rate": metric_or_computed(review, "overall_change_rate", computed),
    }


def configured_users() -> dict[str, dict[str, str]]:
    users = {username: config.copy() for username, config in DEFAULT_USERS.items()}
    try:
        auth = st.secrets.get("auth", {})
        secret_users = auth.get("users", {}) if hasattr(auth, "get") else {}
        for username, config in secret_users.items():
            base = users.get(str(username), {}).copy()
            if hasattr(config, "get"):
                base.update({
                    "display_name": str(config.get("display_name", base.get("display_name", username))),
                    "role": str(config.get("role", base.get("role", "reviewer"))),
                    "password": str(config.get("password", "")),
                })
            users[str(username)] = base
    except Exception:
        pass
    return users


def display_name(username: str) -> str:
    return configured_users().get(username, {}).get("display_name", username)


def reviewer_usernames() -> tuple[str, ...]:
    users = configured_users()
    ordered = [username for username in REVIEWER_ORDER if users.get(username, {}).get("role") == "reviewer"]
    extras = sorted(
        username
        for username, config in users.items()
        if config.get("role") == "reviewer" and username not in ordered
    )
    return tuple(ordered + extras)


def sanitize_reviewer_selection(selected: list[str] | tuple[str, ...] | None) -> list[str]:
    available = reviewer_usernames()
    seen: set[str] = set()
    result: list[str] = []
    for username in selected or []:
        if username in available and username not in seen:
            seen.add(username)
            result.append(username)
    return result


def assignment_reviewers() -> tuple[str, ...]:
    current = st.session_state.get("_assignment_reviewers")
    if current:
        return tuple(current)
    return reviewer_usernames()


def reviewer_for_index(index: int, reviewers: tuple[str, ...] | list[str]) -> str:
    if not reviewers:
        return ""
    return reviewers[(max(index, 1) - 1) % len(reviewers)]


def get_secret_database_url() -> str | None:
    try:
        database = st.secrets.get("database", {})
        if hasattr(database, "get") and database.get("url"):
            return str(database.get("url"))
        if st.secrets.get("DATABASE_URL"):
            return str(st.secrets["DATABASE_URL"])
    except Exception:
        pass
    return os.environ.get("DATABASE_URL")


def database_url() -> str:
    url = get_secret_database_url()
    if url:
        if url.startswith("postgres://"):
            return "postgresql://" + url.removeprefix("postgres://")
        return url
    return f"sqlite:///{LOCAL_DB_FILE.as_posix()}"


class ReviewDB:
    def __init__(self, url: str) -> None:
        self.url = url
        self.backend = "sqlite" if url.startswith("sqlite") else "postgresql"
        self.path = LOCAL_DB_FILE
        self.engine = None
        self.sa_text = None
        if self.backend == "postgresql":
            try:
                from sqlalchemy import create_engine, text as sqlalchemy_text
            except ModuleNotFoundError:
                st.error("تحتاج قاعدة PostgreSQL إلى تثبيت SQLAlchemy. على Streamlit Cloud سيُثبتها ملف requirements.txt تلقائيًا.")
                st.stop()
            self.sa_text = sqlalchemy_text
            self.engine = create_engine(url, pool_pre_ping=True)

    def is_postgres(self) -> bool:
        return self.backend == "postgresql"

    def run(self, sql: str, params: dict[str, Any] | None = None) -> None:
        self.run_many([(sql, params or {})])

    def run_many(self, statements: list[tuple[str, dict[str, Any]]]) -> None:
        if self.backend == "sqlite":
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with sqlite3.connect(self.path) as conn:
                for sql, params in statements:
                    conn.execute(sql, params)
            return
        assert self.engine is not None and self.sa_text is not None
        with self.engine.begin() as conn:
            for sql, params in statements:
                conn.execute(self.sa_text(sql), params)

    def fetchall(self, sql: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        if self.backend == "sqlite":
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with sqlite3.connect(self.path) as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(sql, params or {}).fetchall()
                return [dict(row) for row in rows]
        assert self.engine is not None and self.sa_text is not None
        with self.engine.begin() as conn:
            rows = conn.execute(self.sa_text(sql), params or {}).fetchall()
            return [dict(row._mapping) for row in rows]


@st.cache_resource(show_spinner=False)
def db() -> ReviewDB:
    return ReviewDB(database_url())


@st.cache_data(show_spinner=False)
def load_questions() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with DATA_FILE.open("r", encoding="utf-8") as handle:
        for idx, line in enumerate(handle, 1):
            if not line.strip():
                continue
            row = json.loads(line)
            row["question_id"] = row.get("question_id") or row.get("golden_question_id") or f"q_{idx:04d}"
            row["review_bank_index"] = as_int(row.get("review_bank_index"), idx) or idx
            rows.append(row)
    return rows


def create_schema(database: ReviewDB) -> None:
    event_pk = "BIGSERIAL PRIMARY KEY" if database.is_postgres() else "INTEGER PRIMARY KEY AUTOINCREMENT"
    database.run(
        """
        CREATE TABLE IF NOT EXISTS question_source (
            question_id TEXT PRIMARY KEY,
            bank_index INTEGER,
            question TEXT NOT NULL,
            short_answer TEXT,
            answer_span TEXT,
            child_text TEXT,
            parent_text TEXT,
            metadata_json TEXT NOT NULL,
            source_record_json TEXT NOT NULL,
            imported_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    database.run(
        """
        CREATE TABLE IF NOT EXISTS review_state (
            question_id TEXT NOT NULL,
            reviewer_name TEXT NOT NULL,
            decision TEXT NOT NULL,
            edited_question TEXT NOT NULL,
            edited_answer TEXT NOT NULL,
            question_change_rate REAL NOT NULL DEFAULT 0,
            answer_change_rate REAL NOT NULL DEFAULT 0,
            overall_change_rate REAL NOT NULL DEFAULT 0,
            notes TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (question_id, reviewer_name)
        )
        """
    )
    database.run(
        f"""
        CREATE TABLE IF NOT EXISTS review_events (
            event_id {event_pk},
            question_id TEXT NOT NULL,
            reviewer_name TEXT NOT NULL,
            event_type TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    database.run(
        """
        CREATE TABLE IF NOT EXISTS app_settings (
            setting_key TEXT PRIMARY KEY,
            setting_value TEXT NOT NULL,
            updated_by TEXT,
            updated_at TEXT NOT NULL
        )
        """
    )
    ensure_review_metric_columns(database)


def ensure_review_metric_columns(database: ReviewDB) -> None:
    metric_columns = {
        "question_change_rate": "REAL NOT NULL DEFAULT 0",
        "answer_change_rate": "REAL NOT NULL DEFAULT 0",
        "overall_change_rate": "REAL NOT NULL DEFAULT 0",
    }
    if database.is_postgres():
        for column in metric_columns:
            database.run(
                f"ALTER TABLE review_state ADD COLUMN IF NOT EXISTS {column} DOUBLE PRECISION NOT NULL DEFAULT 0"
            )
        return

    existing = {row.get("name") for row in database.fetchall("PRAGMA table_info(review_state)")}
    for column, definition in metric_columns.items():
        if column not in existing:
            database.run(f"ALTER TABLE review_state ADD COLUMN {column} {definition}")


def import_questions(database: ReviewDB, questions: list[dict[str, Any]]) -> None:
    stamp = utc_now()
    statements: list[tuple[str, dict[str, Any]]] = []
    for row in questions:
        metadata = {k: v for k, v in row.items() if k not in {"child_text", "parent_text"}}
        statements.append((
            """
            INSERT INTO question_source (
                question_id, bank_index, question, short_answer, answer_span,
                child_text, parent_text, metadata_json, source_record_json,
                imported_at, updated_at
            )
            VALUES (
                :question_id, :bank_index, :question, :short_answer, :answer_span,
                :child_text, :parent_text, :metadata_json, :source_record_json,
                :imported_at, :updated_at
            )
            ON CONFLICT (question_id) DO UPDATE SET
                bank_index = excluded.bank_index,
                question = excluded.question,
                short_answer = excluded.short_answer,
                answer_span = excluded.answer_span,
                child_text = excluded.child_text,
                parent_text = excluded.parent_text,
                metadata_json = excluded.metadata_json,
                source_record_json = excluded.source_record_json,
                updated_at = excluded.updated_at
            """,
            {
                "question_id": row["question_id"],
                "bank_index": row.get("review_bank_index"),
                "question": row.get("question", ""),
                "short_answer": row.get("short_answer", ""),
                "answer_span": row.get("answer_span", ""),
                "child_text": row.get("child_text", ""),
                "parent_text": row.get("parent_text", ""),
                "metadata_json": json.dumps(metadata, ensure_ascii=False),
                "source_record_json": json.dumps(row, ensure_ascii=False),
                "imported_at": stamp,
                "updated_at": stamp,
            },
        ))
    database.run_many(statements)


@st.cache_resource(show_spinner=False)
def bootstrap() -> ReviewDB:
    questions = load_questions()
    database = db()
    create_schema(database)
    import_questions(database, questions)
    return database


def fetch_setting(database: ReviewDB, key: str) -> str | None:
    rows = database.fetchall(
        "SELECT setting_value FROM app_settings WHERE setting_key = :setting_key",
        {"setting_key": key},
    )
    return rows[0]["setting_value"] if rows else None


def save_setting(database: ReviewDB, key: str, value: str, updated_by: str) -> None:
    database.run(
        """
        INSERT INTO app_settings (setting_key, setting_value, updated_by, updated_at)
        VALUES (:setting_key, :setting_value, :updated_by, :updated_at)
        ON CONFLICT (setting_key) DO UPDATE SET
            setting_value = excluded.setting_value,
            updated_by = excluded.updated_by,
            updated_at = excluded.updated_at
        """,
        {
            "setting_key": key,
            "setting_value": value,
            "updated_by": updated_by,
            "updated_at": utc_now(),
        },
    )


def active_reviewer_usernames(database: ReviewDB) -> tuple[str, ...]:
    raw = fetch_setting(database, "active_reviewers")
    selected: list[str] = []
    if raw:
        try:
            value = json.loads(raw)
            if isinstance(value, list):
                selected = [str(item) for item in value]
        except json.JSONDecodeError:
            selected = []
    sanitized = sanitize_reviewer_selection(selected)
    if sanitized:
        return tuple(sanitized)
    return reviewer_usernames()


def save_active_reviewer_usernames(database: ReviewDB, reviewers: list[str], updated_by: str) -> None:
    sanitized = sanitize_reviewer_selection(reviewers)
    if not sanitized:
        raise ValueError("at_least_one_reviewer")
    save_setting(database, "active_reviewers", json.dumps(sanitized, ensure_ascii=False), updated_by)
    record_event(database, "__settings__", updated_by, "save_active_reviewers", {"active_reviewers": sanitized})


def record_event(database: ReviewDB, question_id: str, reviewer_name: str, event_type: str, payload: dict[str, Any] | None = None) -> None:
    database.run(
        """
        INSERT INTO review_events (
            question_id, reviewer_name, event_type, payload_json, created_at
        )
        VALUES (:question_id, :reviewer_name, :event_type, :payload_json, :created_at)
        """,
        {
            "question_id": question_id,
            "reviewer_name": reviewer_name,
            "event_type": event_type,
            "payload_json": json.dumps(payload or {}, ensure_ascii=False),
            "created_at": utc_now(),
        },
    )


def record_view(database: ReviewDB, question_id: str, reviewer_name: str) -> None:
    view_key = f"last_viewed_question_{reviewer_name}"
    if st.session_state.get(view_key) == question_id:
        return
    st.session_state[view_key] = question_id
    record_event(database, question_id, reviewer_name, "view_question")


def fetch_reviews(database: ReviewDB, reviewer_name: str) -> dict[str, dict[str, Any]]:
    rows = database.fetchall(
        """
        SELECT question_id, reviewer_name, decision, edited_question, edited_answer,
               question_change_rate, answer_change_rate, overall_change_rate,
               notes, created_at, updated_at
        FROM review_state
        WHERE reviewer_name = :reviewer_name
        """,
        {"reviewer_name": reviewer_name},
    )
    return {row["question_id"]: row for row in rows}


def fetch_all_reviews(database: ReviewDB) -> list[dict[str, Any]]:
    return database.fetchall(
        """
        SELECT question_id, reviewer_name, decision, edited_question, edited_answer,
               question_change_rate, answer_change_rate, overall_change_rate,
               notes, created_at, updated_at
        FROM review_state
        ORDER BY updated_at DESC
        """
    )


def save_review(
    database: ReviewDB,
    *,
    question_id: str,
    reviewer_name: str,
    decision: str,
    edited_question: str,
    edited_answer: str,
    question_change_rate: float,
    answer_change_rate: float,
    overall_change_rate: float,
    notes: str,
) -> None:
    stamp = utc_now()
    payload = {
        "decision": decision,
        "edited_question": edited_question,
        "edited_answer": edited_answer,
        "question_change_rate": question_change_rate,
        "answer_change_rate": answer_change_rate,
        "overall_change_rate": overall_change_rate,
        "notes": notes,
    }
    database.run_many([
        (
            """
            INSERT INTO review_state (
                question_id, reviewer_name, decision, edited_question,
                edited_answer, question_change_rate, answer_change_rate,
                overall_change_rate, notes, created_at, updated_at
            )
            VALUES (
                :question_id, :reviewer_name, :decision, :edited_question,
                :edited_answer, :question_change_rate, :answer_change_rate,
                :overall_change_rate, :notes, :created_at, :updated_at
            )
            ON CONFLICT (question_id, reviewer_name) DO UPDATE SET
                decision = excluded.decision,
                edited_question = excluded.edited_question,
                edited_answer = excluded.edited_answer,
                question_change_rate = excluded.question_change_rate,
                answer_change_rate = excluded.answer_change_rate,
                overall_change_rate = excluded.overall_change_rate,
                notes = excluded.notes,
                updated_at = excluded.updated_at
            """,
            {
                "question_id": question_id,
                "reviewer_name": reviewer_name,
                "decision": decision,
                "edited_question": edited_question,
                "edited_answer": edited_answer,
                "question_change_rate": question_change_rate,
                "answer_change_rate": answer_change_rate,
                "overall_change_rate": overall_change_rate,
                "notes": notes,
                "created_at": stamp,
                "updated_at": stamp,
            },
        ),
        (
            """
            INSERT INTO review_events (
                question_id, reviewer_name, event_type, payload_json, created_at
            )
            VALUES (:question_id, :reviewer_name, :event_type, :payload_json, :created_at)
            """,
            {
                "question_id": question_id,
                "reviewer_name": reviewer_name,
                "event_type": "save_review",
                "payload_json": json.dumps(payload, ensure_ascii=False),
                "created_at": stamp,
            },
        ),
    ])


def assigned_reviewer(question: dict[str, Any]) -> str:
    index = as_int(question.get("review_bank_index"), 1)
    return reviewer_for_index(index, assignment_reviewers())


def source_files(question: dict[str, Any]) -> list[str]:
    files = question.get("source_files") or []
    if isinstance(files, str):
        return [files]
    return [str(item) for item in files]


def normalize_arabic(text_value: str) -> str:
    text_value = re.sub(r"[\u0610-\u061A\u064B-\u065F\u0670\u06D6-\u06ED]", "", text_value or "")
    text_value = text_value.replace("أ", "ا").replace("إ", "ا").replace("آ", "ا")
    text_value = text_value.replace("ى", "ي").replace("ة", "ه")
    return re.sub(r"\s+", " ", text_value).strip().lower()


def contains_search(question: dict[str, Any], query: str) -> bool:
    if not query:
        return True
    haystack = " ".join(str(question.get(key) or "") for key in TEXT_KEYS)
    haystack += " " + " ".join(source_files(question))
    return normalize_arabic(query) in normalize_arabic(haystack)


def filtered_for_reviewer(
    questions: list[dict[str, Any]],
    reviews: dict[str, dict[str, Any]],
    username: str,
    status_filter: str,
    search: str,
) -> list[dict[str, Any]]:
    result = []
    for question in questions:
        if assigned_reviewer(question) != username:
            continue
        reviewed = question["question_id"] in reviews
        if status_filter == "pending" and reviewed:
            continue
        if status_filter == "reviewed" and not reviewed:
            continue
        if not contains_search(question, search):
            continue
        result.append(question)
    return result


def reviews_by_question(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(row["question_id"], []).append(row)
    return grouped


def assigned_review_for(question: dict[str, Any], grouped_reviews: dict[str, list[dict[str, Any]]]) -> dict[str, Any] | None:
    reviewer = assigned_reviewer(question)
    for row in grouped_reviews.get(question["question_id"], []):
        if row.get("reviewer_name") == reviewer:
            return row
    return None


def filtered_for_admin(
    questions: list[dict[str, Any]],
    grouped_reviews: dict[str, list[dict[str, Any]]],
    status_filter: str,
    search: str,
) -> list[dict[str, Any]]:
    result = []
    for question in questions:
        assigned_review = assigned_review_for(question, grouped_reviews)
        has_any_review = bool(grouped_reviews.get(question["question_id"]))
        if status_filter == "pending" and assigned_review:
            continue
        if status_filter == "reviewed" and not assigned_review:
            continue
        if status_filter == "any_review" and not has_any_review:
            continue
        if not contains_search(question, search):
            continue
        result.append(question)
    return result


def get_query_param(name: str) -> str | None:
    try:
        value = st.query_params.get(name)
    except Exception:
        return None
    if isinstance(value, list):
        return value[0] if value else None
    return value


def select_question(question_id: str) -> None:
    st.session_state["selected_question_id"] = question_id
    try:
        st.query_params["qid"] = question_id
    except Exception:
        pass


def highlighted_html(text_value: str, needle: str) -> str:
    text_value = text_value or ""
    needle = (needle or "").strip()
    escaped_text = html.escape(text_value)
    if needle and needle in text_value:
        escaped_needle = html.escape(needle)
        escaped_text = escaped_text.replace(
            escaped_needle,
            f'<mark class="answer-mark">{escaped_needle}</mark>',
            1,
        )
    return f'<div class="text-panel">{escaped_text.replace(chr(10), "<br>")}</div>'


def question_meta_pills(question: dict[str, Any], extra: list[str] | None = None) -> str:
    meta = [
        question.get("primary_discipline"),
        question.get("topic_level_1"),
        question.get("question_type"),
        question.get("difficulty"),
        " / ".join(source_files(question)),
        f"ص {question.get('page_start')} - {question.get('page_end')}",
    ]
    if extra:
        meta.extend(extra)
    return "".join(f'<span class="meta-pill">{html.escape(str(item))}</span>' for item in meta if item)


def inject_css() -> None:
    st.markdown(
        """
        <style>
        html, body, .stApp, [data-testid="stAppViewContainer"], [data-testid="stSidebar"],
        [data-testid="stSidebarContent"], [data-testid="stHeader"], [data-testid="stToolbar"],
        [data-testid="stMarkdownContainer"], [data-testid="stForm"], [data-testid="stDataFrame"],
        [data-testid="stMetric"], [data-testid="stExpander"], label, p, span, div, input, textarea, table, th, td {
            direction: rtl !important;
            text-align: right !important;
            font-family: "Noto Naskh Arabic", "Tajawal", "Arial", sans-serif;
        }
        .block-container { padding-top: 1.35rem; max-width: 1320px; }
        [data-testid="stSidebarContent"] { padding-top: 1rem; }
        [data-baseweb="select"], [data-baseweb="popover"], [role="listbox"], [role="option"] {
            direction: rtl !important;
            text-align: right !important;
        }
        input, textarea { unicode-bidi: plaintext !important; line-height: 1.8 !important; }
        button { direction: rtl !important; }
        .app-title { font-size: 1.55rem; font-weight: 750; margin-bottom: .25rem; color: #1f2428; }
        .section-title { font-size: 1.12rem; font-weight: 750; margin: .6rem 0 .45rem; color: #1f2428; }
        .subtle { color: #5c6470; font-size: .92rem; }
        .account-card {
            border: 1px solid #e5e1d8;
            border-radius: 8px;
            background: #fffdfa;
            padding: .85rem .95rem;
            margin-bottom: .75rem;
        }
        .status-strip { display: flex; flex-wrap: wrap; gap: .45rem; margin: .75rem 0 .55rem; align-items: center; }
        .status-chip {
            display: inline-flex;
            align-items: center;
            border-radius: 999px;
            padding: .18rem .62rem;
            font-size: .84rem;
            border: 1px solid #dde2e6;
            background: #fff;
            color: #344054;
        }
        .status-chip.reviewed { background: #dff3e9; border-color: #b9dfcf; color: #14523d; }
        .status-chip.pending { background: #fff1c7; border-color: #ead28a; color: #6f4a00; }
        .status-chip.current { background: #26312f; border-color: #26312f; color: #fff; }
        .qgrid {
            direction: rtl !important;
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(46px, 1fr));
            gap: .38rem;
            max-height: 285px;
            overflow: auto;
            padding: .35rem .1rem .55rem .2rem;
            margin-bottom: .75rem;
        }
        .qbox {
            display: inline-flex;
            justify-content: center;
            align-items: center;
            min-height: 38px;
            border-radius: 8px;
            border: 1px solid transparent;
            font-weight: 750;
            font-size: .92rem;
            text-decoration: none !important;
            transition: transform .08s ease, box-shadow .08s ease;
        }
        .qbox:hover { transform: translateY(-1px); box-shadow: 0 4px 12px rgba(31, 36, 40, .12); }
        .qbox.reviewed { background: #dff3e9; border-color: #b9dfcf; color: #14523d !important; }
        .qbox.pending { background: #fff1c7; border-color: #ead28a; color: #6f4a00 !important; }
        .qbox.selected { background: #26312f; border-color: #26312f; color: #ffffff !important; }
        .answer-mark { background: #fff1a8; color: #161616; padding: .08rem .16rem; border-radius: .2rem; }
        .text-panel {
            background: #fbfbf8;
            border: 1px solid #e5e1d8;
            border-radius: 8px;
            padding: 1rem 1.1rem;
            line-height: 2.05;
            font-size: 1.05rem;
            color: #1f2428;
            max-height: 520px;
            overflow: auto;
            white-space: normal;
        }
        .answer-panel {
            background: #f6faf8;
            border: 1px solid #d8e8de;
            border-radius: 8px;
            padding: .85rem 1rem;
            line-height: 1.9;
            font-size: 1.05rem;
        }
        .question-panel {
            background: #fffdfa;
            border: 1px solid #e5e1d8;
            border-radius: 8px;
            padding: 1rem 1.1rem;
            line-height: 1.9;
            font-size: 1.08rem;
            margin: .65rem 0 .85rem;
        }
        .meta-pill {
            display: inline-block;
            border: 1px solid #dde2e6;
            border-radius: 999px;
            padding: .18rem .55rem;
            margin: .12rem;
            color: #344054;
            background: #fff;
            font-size: .82rem;
        }
        div[data-testid="stRadio"] > div { gap: .6rem; }
        div[data-testid="stHorizontalBlock"] { direction: rtl !important; }
        </style>
        """,
        unsafe_allow_html=True,
    )


def login_screen() -> None:
    users = configured_users()
    st.markdown('<div class="app-title">مراجعة أسئلة زاد المعاد</div>', unsafe_allow_html=True)
    st.markdown('<div class="subtle">دخول المراجعين والإدارة لمتابعة الأسئلة المنتقاة وتعديل السؤال والجواب عند الحاجة.</div>', unsafe_allow_html=True)
    if not any(user.get("password") for user in users.values()):
        st.error("لم تُضبط حسابات الدخول في Streamlit Secrets بعد. أضف قسم [auth.users] كما في ملف secrets.example.toml.")
        st.stop()
    with st.form("login_form"):
        username = st.text_input("اسم المستخدم", placeholder="reviewer1")
        password = st.text_input("كلمة المرور", type="password")
        submitted = st.form_submit_button("دخول", type="primary", width="stretch")
    if submitted:
        username = clean_text(username)
        user = users.get(username)
        if not user or not user.get("password") or password != user["password"]:
            st.error("اسم المستخدم أو كلمة المرور غير صحيحة.")
            st.stop()
        st.session_state["auth_user"] = {
            "username": username,
            "display_name": user.get("display_name", username),
            "role": user.get("role", "reviewer"),
        }
        st.session_state.pop("selected_question_id", None)
        st.rerun()
    st.stop()


def logout_button() -> None:
    if st.sidebar.button("خروج", width="stretch"):
        st.session_state.pop("auth_user", None)
        st.session_state.pop("selected_question_id", None)
        try:
            st.query_params.clear()
        except Exception:
            pass
        st.rerun()


def render_reviewer_sidebar(questions: list[dict[str, Any]], reviews: dict[str, dict[str, Any]], username: str) -> dict[str, str]:
    assigned_questions = [q for q in questions if assigned_reviewer(q) == username]
    reviewed_count = sum(1 for q in assigned_questions if q["question_id"] in reviews)
    st.sidebar.markdown(
        f'<div class="account-card"><b>{html.escape(display_name(username))}</b><br><span class="subtle">{html.escape(username)}</span></div>',
        unsafe_allow_html=True,
    )
    logout_button()
    st.sidebar.metric("راجعتها", reviewed_count, f"من {len(assigned_questions)}")
    st.sidebar.progress(reviewed_count / len(assigned_questions) if assigned_questions else 0)
    st.sidebar.divider()
    status_filter = st.sidebar.selectbox("حالة مراجعتي", list(STATUS_OPTIONS), format_func=STATUS_OPTIONS.get)
    search = st.sidebar.text_input("بحث", placeholder="كلمة في السؤال أو الموضوع")
    st.sidebar.download_button(
        "تنزيل مراجعاتي CSV",
        data=export_reviews_csv(questions, list(reviews.values()), reviewer_name=username).encode("utf-8-sig"),
        file_name=f"reviews_{username}.csv",
        mime="text/csv",
        width="stretch",
    )
    return {"status_filter": status_filter, "search": search}


def render_admin_sidebar(questions: list[dict[str, Any]], grouped_reviews: dict[str, list[dict[str, Any]]]) -> dict[str, str]:
    assigned_done = sum(1 for q in questions if assigned_review_for(q, grouped_reviews))
    active_count = len(assignment_reviewers())
    st.sidebar.markdown('<div class="account-card"><b>الأدمن</b><br><span class="subtle">admin</span></div>', unsafe_allow_html=True)
    logout_button()
    st.sidebar.metric("المراجعون النشطون", active_count)
    st.sidebar.metric("المعتمد في سير العمل", assigned_done, f"من {len(questions)}")
    st.sidebar.progress(assigned_done / len(questions) if questions else 0)
    st.sidebar.divider()
    status_filter = st.sidebar.selectbox("حالة المراجعة", list(ADMIN_STATUS_OPTIONS), format_func=ADMIN_STATUS_OPTIONS.get)
    search = st.sidebar.text_input("بحث", placeholder="كلمة في السؤال أو الموضوع")
    return {"status_filter": status_filter, "search": search}


def sync_selected_question(visible: list[dict[str, Any]]) -> str | None:
    if not visible:
        return None
    visible_ids = [q["question_id"] for q in visible]
    query_qid = get_query_param("qid")
    selected = query_qid or st.session_state.get("selected_question_id")
    if selected not in visible_ids:
        selected = visible_ids[0]
    select_question(selected)
    return selected


def render_number_grid(visible: list[dict[str, Any]], selected_id: str, is_reviewed: Callable[[dict[str, Any]], bool]) -> None:
    st.markdown(
        '<div class="status-strip">'
        '<span class="status-chip current">السؤال الحالي</span>'
        '<span class="status-chip reviewed">تمت مراجعته</span>'
        '<span class="status-chip pending">لم يراجع</span>'
        '</div>',
        unsafe_allow_html=True,
    )
    boxes = []
    for question in visible:
        question_id = question["question_id"]
        state = "reviewed" if is_reviewed(question) else "pending"
        if question_id == selected_id:
            state += " selected"
        index = html.escape(str(question.get("review_bank_index") or question_id))
        tooltip = html.escape(clean_text(question.get("question", ""))[:140])
        boxes.append(f'<a class="qbox {state}" href="?qid={quote(question_id)}" title="{tooltip}">{index}</a>')
    st.markdown(f'<div class="qgrid">{"".join(boxes)}</div>', unsafe_allow_html=True)


def render_jump_controls(allowed: list[dict[str, Any]], selected_id: str) -> None:
    allowed_by_index = {as_int(q.get("review_bank_index"), 0): q["question_id"] for q in allowed}
    selected_question = next((q for q in allowed if q["question_id"] == selected_id), allowed[0])
    current_index = as_int(selected_question.get("review_bank_index"), 1)
    max_index = max(allowed_by_index) if allowed_by_index else current_index
    with st.form("jump_to_number"):
        cols = st.columns([1.2, 1, 4])
        with cols[0]:
            target_index = st.number_input("رقم السؤال", min_value=1, max_value=max_index, value=current_index, step=1)
        with cols[1]:
            jump = st.form_submit_button("انتقال", width="stretch")
        with cols[2]:
            st.markdown(" ")
    if jump:
        target_id = allowed_by_index.get(as_int(target_index))
        if not target_id:
            st.warning("هذا الرقم ليس ضمن الأسئلة المتاحة لهذا الحساب أو ضمن نطاق البحث الحالي.")
        else:
            select_question(target_id)
            st.rerun()


def render_prev_next(visible: list[dict[str, Any]], selected_id: str) -> None:
    ids = [q["question_id"] for q in visible]
    current_index = ids.index(selected_id)
    previous_col, next_col = st.columns(2)
    with previous_col:
        if st.button("السابق", disabled=current_index == 0, width="stretch"):
            select_question(ids[current_index - 1])
            st.rerun()
    with next_col:
        if st.button("التالي", disabled=current_index == len(ids) - 1, width="stretch"):
            select_question(ids[current_index + 1])
            st.rerun()


def next_question_id(visible: list[dict[str, Any]], current_id: str) -> str | None:
    ids = [q["question_id"] for q in visible]
    if current_id not in ids:
        return ids[0] if ids else None
    pos = ids.index(current_id)
    if pos + 1 < len(ids):
        return ids[pos + 1]
    if pos > 0:
        return ids[pos - 1]
    return None


def current_decision_value(review: dict[str, Any] | None) -> str:
    value = (review or {}).get("decision") or "approved"
    value = LEGACY_DECISION_MAP.get(value, value)
    return value if value in DECISIONS else "approved"


def render_question_reference(question: dict[str, Any]) -> None:
    st.divider()
    st.subheader("الجواب من المقطع")
    answer_text = question.get("answer_span") or question.get("short_answer", "")
    st.markdown(f'<div class="answer-panel">{html.escape(answer_text)}</div>', unsafe_allow_html=True)

    needle = question.get("answer_span") or question.get("short_answer", "")
    with st.expander("المقطع المسترجع", expanded=True):
        st.markdown(highlighted_html(question.get("child_text", ""), needle), unsafe_allow_html=True)
    with st.expander("السياق كاملا", expanded=True):
        st.markdown(highlighted_html(question.get("parent_text", ""), needle), unsafe_allow_html=True)
    with st.expander("بيانات الربط والمصدر", expanded=False):
        st.write(
            {
                "question_id": question.get("question_id"),
                "retrieval_chunk_id": question.get("retrieval_chunk_id"),
                "parent_author_chunk_id": question.get("parent_author_chunk_id"),
                "parent_chunk_id": question.get("parent_chunk_id"),
                "assigned_reviewer": assigned_reviewer(question),
                "topic_level_2": question.get("topic_level_2"),
                "topic_level_3": question.get("topic_level_3"),
                "primary_topic": question.get("primary_topic"),
                "answer_mode": question.get("answer_mode"),
            }
        )


def render_reviewer_question(
    database: ReviewDB,
    question: dict[str, Any],
    review: dict[str, Any] | None,
    username: str,
    visible: list[dict[str, Any]],
) -> None:
    record_view(database, question["question_id"], username)
    status_label = "تمت مراجعته" if review else "لم يراجع"
    status_class = "reviewed" if review else "pending"
    pills = question_meta_pills(question, [f"المراجع المكلف: {display_name(username)}"])
    st.markdown(
        f'<div class="app-title">السؤال {question.get("review_bank_index")}</div>'
        f'<div class="status-strip"><span class="status-chip {status_class}">{status_label}</span></div>{pills}',
        unsafe_allow_html=True,
    )

    with st.form(f"review_form_{question['question_id']}"):
        edited_question = st.text_area(
            "السؤال المعدل",
            value=(review.get("edited_question") if review else None) or question.get("question", ""),
            height=92,
        )
        edited_answer = st.text_area(
            "الإجابة المعدلة",
            value=(review.get("edited_answer") if review else None) or question.get("short_answer", ""),
            height=118,
        )
        decision = st.radio(
            "الحكم",
            list(DECISIONS),
            index=list(DECISIONS).index(current_decision_value(review)),
            format_func=DECISIONS.get,
            horizontal=True,
        )
        notes = st.text_area("ملاحظة مختصرة عند الحاجة", value=(review.get("notes") if review else "") or "", height=82)
        saved = st.form_submit_button("حفظ المراجعة والانتقال للتالي", type="primary", width="stretch")

    if saved:
        if not clean_text(edited_question) or not clean_text(edited_answer):
            st.warning("لا يمكن حفظ سؤال أو جواب فارغ.")
        else:
            metrics = review_change_metrics(question, edited_question, edited_answer)
            save_review(
                database,
                question_id=question["question_id"],
                reviewer_name=username,
                decision=decision,
                edited_question=edited_question.strip(),
                edited_answer=edited_answer.strip(),
                question_change_rate=metrics["question_change_rate"],
                answer_change_rate=metrics["answer_change_rate"],
                overall_change_rate=metrics["overall_change_rate"],
                notes=notes.strip(),
            )
            target_id = next_question_id(visible, question["question_id"])
            if target_id:
                select_question(target_id)
            st.session_state["save_notice"] = "تم حفظ المراجعة والانتقال للسؤال التالي."
            st.rerun()

    render_question_reference(question)


def assignment_progress_rows(questions: list[dict[str, Any]], grouped_reviews: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    rows = []
    for question in questions:
        assigned = assigned_reviewer(question)
        assigned_review = assigned_review_for(question, grouped_reviews)
        all_reviews = grouped_reviews.get(question["question_id"], [])
        metrics = review_metrics_for_export(question, assigned_review)
        rows.append(
            {
                "رقم السؤال": question.get("review_bank_index"),
                "question_id": question.get("question_id"),
                "المراجع المكلف": display_name(assigned),
                "اسم المستخدم": assigned,
                "حالة التكليف": "تم" if assigned_review else "لم يراجع",
                "الحكم": DECISION_LABELS.get((assigned_review or {}).get("decision", ""), ""),
                "آخر تحديث": (assigned_review or {}).get("updated_at", ""),
                "عدد المراجعات على السؤال": len(all_reviews),
                "المراجعون الذين راجعوه": ", ".join(display_name(r.get("reviewer_name", "")) for r in all_reviews),
                "السؤال الأصلي": question.get("question", ""),
                "السؤال المعدل": (assigned_review or {}).get("edited_question", ""),
                "معدل تغيير السؤال %": metrics["question_change_rate"],
                "الإجابة الأصلية": question.get("short_answer", ""),
                "الإجابة المعدلة": (assigned_review or {}).get("edited_answer", ""),
                "معدل تغيير الإجابة %": metrics["answer_change_rate"],
                "معدل التغيير الكلي %": metrics["overall_change_rate"],
                "المجال": question.get("primary_discipline", ""),
                "الموضوع": question.get("primary_topic", ""),
                "الملف": " / ".join(source_files(question)),
                "الصفحات": f"{question.get('page_start', '')}-{question.get('page_end', '')}",
            }
        )
    return rows


def reviewer_summary_rows(questions: list[dict[str, Any]], grouped_reviews: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    rows = []
    active = set(assignment_reviewers())
    for username in reviewer_usernames():
        assigned = [q for q in questions if assigned_reviewer(q) == username]
        reviewed = sum(1 for q in assigned if any(r.get("reviewer_name") == username for r in grouped_reviews.get(q["question_id"], [])))
        saved_reviews = sum(1 for reviews in grouped_reviews.values() for row in reviews if row.get("reviewer_name") == username)
        rows.append(
            {
                "المراجع": display_name(username),
                "اسم المستخدم": username,
                "حالة التوزيع": "نشط" if username in active else "غير نشط",
                "المكلف بها": len(assigned),
                "راجع من تكليفه": reviewed,
                "المتبقي": len(assigned) - reviewed,
                "كل مراجعاته المحفوظة": saved_reviews,
                "نسبة الإنجاز": f"{(reviewed / len(assigned) * 100):.1f}%" if assigned else "0%",
            }
        )
    return rows


def export_rows_csv(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return ""
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=list(rows[0].keys()))
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue()


def export_reviews_csv(questions: list[dict[str, Any]], review_rows: list[dict[str, Any]], reviewer_name: str | None = None) -> str:
    question_by_id = {q["question_id"]: q for q in questions}
    rows = []
    for review in review_rows:
        if reviewer_name and review.get("reviewer_name") != reviewer_name:
            continue
        question = question_by_id.get(review["question_id"], {})
        metrics = review_metrics_for_export(question, review)
        rows.append(
            {
                "question_id": review.get("question_id", ""),
                "review_bank_index": question.get("review_bank_index", ""),
                "assigned_reviewer": assigned_reviewer(question) if question else "",
                "reviewer_name": review.get("reviewer_name", ""),
                "reviewer_display_name": display_name(review.get("reviewer_name", "")),
                "decision": review.get("decision", ""),
                "الحكم": DECISION_LABELS.get(review.get("decision", ""), review.get("decision", "")),
                "السؤال الأصلي": question.get("question", ""),
                "السؤال المعدل": review.get("edited_question", ""),
                "معدل تغيير السؤال %": metrics["question_change_rate"],
                "الإجابة الأصلية": question.get("short_answer", ""),
                "الإجابة المعدلة": review.get("edited_answer", ""),
                "معدل تغيير الإجابة %": metrics["answer_change_rate"],
                "معدل التغيير الكلي %": metrics["overall_change_rate"],
                "الملاحظات": review.get("notes", ""),
                "primary_discipline": question.get("primary_discipline", ""),
                "primary_topic": question.get("primary_topic", ""),
                "source_files": ", ".join(source_files(question)) if question else "",
                "page_start": question.get("page_start", ""),
                "page_end": question.get("page_end", ""),
                "updated_at": review.get("updated_at", ""),
            }
        )
    return export_rows_csv(rows)


def export_questions_csv(questions: list[dict[str, Any]]) -> str:
    rows = []
    for question in questions:
        rows.append(
            {
                "review_bank_index": question.get("review_bank_index", ""),
                "question_id": question.get("question_id", ""),
                "assigned_reviewer": assigned_reviewer(question),
                "question": question.get("question", ""),
                "short_answer": question.get("short_answer", ""),
                "answer_span": question.get("answer_span", ""),
                "primary_discipline": question.get("primary_discipline", ""),
                "topic_level_1": question.get("topic_level_1", ""),
                "topic_level_2": question.get("topic_level_2", ""),
                "topic_level_3": question.get("topic_level_3", ""),
                "primary_topic": question.get("primary_topic", ""),
                "question_type": question.get("question_type", ""),
                "difficulty": question.get("difficulty", ""),
                "retrieval_chunk_id": question.get("retrieval_chunk_id", ""),
                "parent_author_chunk_id": question.get("parent_author_chunk_id", ""),
                "parent_chunk_id": question.get("parent_chunk_id", ""),
                "source_files": ", ".join(source_files(question)),
                "page_start": question.get("page_start", ""),
                "page_end": question.get("page_end", ""),
            }
        )
    return export_rows_csv(rows)


def distribution_preview_rows(questions: list[dict[str, Any]], reviewers: list[str]) -> list[dict[str, Any]]:
    rows = []
    for username in reviewers:
        count = sum(
            1
            for question in questions
            if reviewer_for_index(as_int(question.get("review_bank_index"), 1), reviewers) == username
        )
        rows.append({"المراجع": display_name(username), "اسم المستخدم": username, "عدد الأسئلة المتوقع": count})
    return rows


def render_active_reviewer_controls(database: ReviewDB, questions: list[dict[str, Any]]) -> None:
    available = list(reviewer_usernames())
    current = [username for username in assignment_reviewers() if username in available]
    st.markdown('<div class="section-title">تحديد المراجعين النشطين للتوزيع</div>', unsafe_allow_html=True)
    st.caption("الأدمن يحدد من سيُحسب عليهم توزيع الأسئلة في هذه الدورة. تغيير القائمة يعيد حساب التكليفات الحالية، مع بقاء أي مراجعات محفوظة في قاعدة البيانات بأسماء أصحابها.")
    with st.form("active_reviewers_form"):
        selected = st.multiselect(
            "المراجعون النشطون",
            options=available,
            default=current,
            format_func=lambda username: f"{display_name(username)} ({username})",
        )
        submitted = st.form_submit_button("حفظ توزيع المراجعين", type="primary", width="stretch")
    if submitted:
        sanitized = sanitize_reviewer_selection(selected)
        if not sanitized:
            st.warning("اختر مراجعًا واحدًا على الأقل حتى يمكن توزيع الأسئلة.")
        else:
            save_active_reviewer_usernames(database, sanitized, "admin")
            st.session_state["_assignment_reviewers"] = tuple(sanitized)
            st.session_state.pop("selected_question_id", None)
            st.success("تم حفظ قائمة المراجعين النشطين وإعادة حساب التوزيع.")
            st.rerun()

    preview = distribution_preview_rows(questions, current)
    if preview:
        st.dataframe(preview, width="stretch", hide_index=True)


def render_admin_downloads(questions: list[dict[str, Any]], all_reviews: list[dict[str, Any]], grouped_reviews: dict[str, list[dict[str, Any]]]) -> None:
    cols = st.columns(3)
    with cols[0]:
        st.download_button("تنزيل سير المراجعة", export_rows_csv(assignment_progress_rows(questions, grouped_reviews)).encode("utf-8-sig"), "assignment_progress.csv", "text/csv", width="stretch")
    with cols[1]:
        st.download_button("تنزيل كل المراجعات", export_reviews_csv(questions, all_reviews).encode("utf-8-sig"), "all_reviews.csv", "text/csv", width="stretch")
    with cols[2]:
        st.download_button("تنزيل الأسئلة الأصلية", export_questions_csv(questions).encode("utf-8-sig"), "source_questions.csv", "text/csv", width="stretch")


def render_admin_question(question: dict[str, Any], grouped_reviews: dict[str, list[dict[str, Any]]]) -> None:
    assigned = assigned_reviewer(question)
    assigned_review = assigned_review_for(question, grouped_reviews)
    status_label = "راجعها المراجع المكلف" if assigned_review else "لم يراجعها المراجع المكلف"
    status_class = "reviewed" if assigned_review else "pending"
    st.markdown(
        f'<div class="app-title">السؤال {question.get("review_bank_index")}</div>'
        f'<div class="status-strip"><span class="status-chip {status_class}">{status_label}</span>'
        f'<span class="status-chip">المكلف: {html.escape(display_name(assigned))}</span></div>'
        f'{question_meta_pills(question)}',
        unsafe_allow_html=True,
    )
    st.markdown(f'<div class="question-panel">{html.escape(question.get("question", ""))}</div>', unsafe_allow_html=True)

    rows = grouped_reviews.get(question["question_id"], [])
    if rows:
        st.markdown('<div class="section-title">مراجعات هذا السؤال</div>', unsafe_allow_html=True)
        review_table = []
        for row in rows:
            metrics = review_metrics_for_export(question, row)
            review_table.append(
                {
                    "المراجع": display_name(row.get("reviewer_name", "")),
                    "اسم المستخدم": row.get("reviewer_name", ""),
                    "الحكم": DECISION_LABELS.get(row.get("decision", ""), row.get("decision", "")),
                    "السؤال المعدل": row.get("edited_question", ""),
                    "معدل تغيير السؤال %": metrics["question_change_rate"],
                    "الإجابة المعدلة": row.get("edited_answer", ""),
                    "معدل تغيير الإجابة %": metrics["answer_change_rate"],
                    "معدل التغيير الكلي %": metrics["overall_change_rate"],
                    "الملاحظات": row.get("notes", ""),
                    "آخر تحديث": row.get("updated_at", ""),
                }
            )
        st.dataframe(review_table, width="stretch", hide_index=True)
    else:
        st.info("لا توجد مراجعة محفوظة لهذا السؤال بعد.")

    render_question_reference(question)


def render_reviewer_app(database: ReviewDB, questions: list[dict[str, Any]], auth: dict[str, str]) -> None:
    username = auth["username"]
    reviews = fetch_reviews(database, username)
    sidebar = render_reviewer_sidebar(questions, reviews, username)
    allowed = [q for q in questions if assigned_reviewer(q) == username]
    visible = filtered_for_reviewer(questions, reviews, username, sidebar["status_filter"], sidebar["search"])
    st.markdown('<div class="app-title">مراجعة الأسئلة المكلف بها</div>', unsafe_allow_html=True)
    if username not in assignment_reviewers():
        st.info("حسابك غير مفعّل حاليًا ضمن توزيع هذه الدورة. يمكن للأدمن إضافتك من لوحة الإدارة عند الحاجة.")
        return
    if st.session_state.get("save_notice"):
        st.success(st.session_state.pop("save_notice"))
    if not visible:
        st.info("لا توجد أسئلة مطابقة للحالة أو البحث الحالي.")
        return
    selected_id = sync_selected_question(visible)
    assert selected_id is not None
    render_number_grid(visible, selected_id, lambda q: q["question_id"] in reviews)
    render_jump_controls(allowed, selected_id)
    render_prev_next(visible, selected_id)
    question = next(q for q in visible if q["question_id"] == selected_id)
    render_reviewer_question(database, question, reviews.get(selected_id), username, visible)


def render_admin_app(database: ReviewDB, questions: list[dict[str, Any]]) -> None:
    all_reviews = fetch_all_reviews(database)
    grouped = reviews_by_question(all_reviews)
    sidebar = render_admin_sidebar(questions, grouped)
    visible = filtered_for_admin(questions, grouped, sidebar["status_filter"], sidebar["search"])
    reviewed_assigned = sum(1 for q in questions if assigned_review_for(q, grouped))
    total_reviews = len(all_reviews)

    st.markdown('<div class="app-title">لوحة إدارة مراجعة الأسئلة</div>', unsafe_allow_html=True)
    render_active_reviewer_controls(database, questions)
    st.divider()
    metric_cols = st.columns(4)
    metric_cols[0].metric("إجمالي الأسئلة", len(questions))
    metric_cols[1].metric("راجعها المكلف", reviewed_assigned)
    metric_cols[2].metric("المتبقي", len(questions) - reviewed_assigned)
    metric_cols[3].metric("كل المراجعات المحفوظة", total_reviews)

    render_admin_downloads(questions, all_reviews, grouped)
    st.divider()
    st.markdown('<div class="section-title">توزيع العمل على المراجعين</div>', unsafe_allow_html=True)
    st.dataframe(reviewer_summary_rows(questions, grouped), width="stretch", hide_index=True)

    st.divider()
    st.markdown('<div class="section-title">الأسئلة</div>', unsafe_allow_html=True)
    if not visible:
        st.info("لا توجد أسئلة مطابقة للحالة أو البحث الحالي.")
        return
    selected_id = sync_selected_question(visible)
    assert selected_id is not None
    render_number_grid(visible, selected_id, lambda q: assigned_review_for(q, grouped) is not None)
    render_jump_controls(questions, selected_id)
    render_prev_next(visible, selected_id)
    question = next(q for q in visible if q["question_id"] == selected_id)
    record_view(database, question["question_id"], "admin")
    render_admin_question(question, grouped)


def main() -> None:
    st.set_page_config(page_title="مراجعة أسئلة زاد المعاد", layout="wide")
    inject_css()

    auth = st.session_state.get("auth_user")
    if not auth:
        login_screen()

    database = bootstrap()
    create_schema(database)
    st.session_state["_assignment_reviewers"] = active_reviewer_usernames(database)
    questions = load_questions()
    if auth.get("role") == "admin":
        render_admin_app(database, questions)
    else:
        render_reviewer_app(database, questions, auth)


if __name__ == "__main__":
    main()
