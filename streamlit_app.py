from __future__ import annotations

import csv
import html
import io
import json
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import streamlit as st

APP_DIR = Path(__file__).resolve().parent
DATA_FILE = APP_DIR / "data" / "questions_core_answer_first_500.jsonl"
LOCAL_DB_FILE = APP_DIR / "local_review_state.sqlite3"

DECISIONS = {
    "approved": "اعتماد",
    "approved_with_edit": "اعتماد بعد تعديل السؤال أو الجواب",
    "needs_revision": "يحتاج تعديلًا جوهريًا",
    "rejected": "رفض",
}

STATUS_OPTIONS = {
    "all": "كل الأسئلة",
    "pending": "لم أراجعها",
    "reviewed": "راجعتها",
}

TEXT_KEYS = (
    "question",
    "short_answer",
    "answer_span",
    "supporting_context",
    "primary_discipline",
    "topic_level_1",
    "topic_level_2",
    "topic_level_3",
    "primary_topic",
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def clean_name(name: str) -> str:
    return re.sub(r"\s+", " ", name or "").strip()


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
            row["review_bank_index"] = row.get("review_bank_index") or idx
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


def fetch_reviews(database: ReviewDB, reviewer_name: str) -> dict[str, dict[str, Any]]:
    rows = database.fetchall(
        """
        SELECT question_id, reviewer_name, decision, edited_question, edited_answer,
               notes, created_at, updated_at
        FROM review_state
        WHERE reviewer_name = :reviewer_name
        """,
        {"reviewer_name": reviewer_name},
    )
    return {row["question_id"]: row for row in rows}


def save_review(
    database: ReviewDB,
    *,
    question_id: str,
    reviewer_name: str,
    decision: str,
    edited_question: str,
    edited_answer: str,
    notes: str,
) -> None:
    stamp = utc_now()
    payload = {
        "decision": decision,
        "edited_question": edited_question,
        "edited_answer": edited_answer,
        "notes": notes,
    }
    database.run_many([
        (
                """
                INSERT INTO review_state (
                    question_id, reviewer_name, decision, edited_question,
                    edited_answer, notes, created_at, updated_at
                )
                VALUES (
                    :question_id, :reviewer_name, :decision, :edited_question,
                    :edited_answer, :notes, :created_at, :updated_at
                )
                ON CONFLICT (question_id, reviewer_name) DO UPDATE SET
                    decision = excluded.decision,
                    edited_question = excluded.edited_question,
                    edited_answer = excluded.edited_answer,
                    notes = excluded.notes,
                    updated_at = excluded.updated_at
                """,
            {
                "question_id": question_id,
                "reviewer_name": reviewer_name,
                "decision": decision,
                "edited_question": edited_question,
                "edited_answer": edited_answer,
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


def record_view(database: ReviewDB, question_id: str, reviewer_name: str) -> None:
    if st.session_state.get("last_viewed_question") == question_id:
        return
    st.session_state["last_viewed_question"] = question_id
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
            "event_type": "view_question",
            "payload_json": "{}",
            "created_at": utc_now(),
        },
    )


def export_my_reviews(database: ReviewDB, reviewer_name: str) -> str:
    reviews = fetch_reviews(database, reviewer_name)
    rows = []
    for question in load_questions():
        review = reviews.get(question["question_id"])
        if not review:
            continue
        rows.append(
            {
                "question_id": question["question_id"],
                "reviewer_name": reviewer_name,
                "decision": review["decision"],
                "decision_label": DECISIONS.get(review["decision"], review["decision"]),
                "original_question": question.get("question", ""),
                "edited_question": review.get("edited_question", ""),
                "original_answer": question.get("short_answer", ""),
                "edited_answer": review.get("edited_answer", ""),
                "notes": review.get("notes", ""),
                "source_files": ", ".join(question.get("source_files") or []),
                "page_start": question.get("page_start", ""),
                "page_end": question.get("page_end", ""),
                "updated_at": review.get("updated_at", ""),
            }
        )
    buffer = io.StringIO()
    fieldnames = [
        "question_id",
        "reviewer_name",
        "decision",
        "decision_label",
        "original_question",
        "edited_question",
        "original_answer",
        "edited_answer",
        "notes",
        "source_files",
        "page_start",
        "page_end",
        "updated_at",
    ]
    writer = csv.DictWriter(buffer, fieldnames=fieldnames)
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue()


def normalize_arabic(text_value: str) -> str:
    text_value = re.sub(r"[\u0610-\u061A\u064B-\u065F\u0670\u06D6-\u06ED]", "", text_value or "")
    text_value = text_value.replace("أ", "ا").replace("إ", "ا").replace("آ", "ا")
    text_value = text_value.replace("ى", "ي").replace("ة", "ه")
    return re.sub(r"\s+", " ", text_value).strip().lower()


def contains_search(question: dict[str, Any], query: str) -> bool:
    if not query:
        return True
    haystack = " ".join(str(question.get(key) or "") for key in TEXT_KEYS)
    return normalize_arabic(query) in normalize_arabic(haystack)


def source_files(question: dict[str, Any]) -> list[str]:
    files = question.get("source_files") or []
    if isinstance(files, str):
        return [files]
    return [str(item) for item in files]


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


def question_label(question: dict[str, Any], review: dict[str, Any] | None) -> str:
    status = "تم" if review else "بانتظار"
    index = question.get("review_bank_index") or question.get("question_id")
    short = question.get("question", "")
    return f"{index} - {status} - {short[:95]}"


def filtered_questions(
    questions: list[dict[str, Any]],
    reviews: dict[str, dict[str, Any]],
    *,
    status_filter: str,
    file_filter: str,
    discipline_filter: str,
    type_filter: str,
    difficulty_filter: str,
    search: str,
) -> list[dict[str, Any]]:
    result = []
    for question in questions:
        question_id = question["question_id"]
        review = reviews.get(question_id)
        if status_filter == "pending" and review:
            continue
        if status_filter == "reviewed" and not review:
            continue
        if file_filter != "الكل" and file_filter not in source_files(question):
            continue
        if discipline_filter != "الكل" and question.get("primary_discipline") != discipline_filter:
            continue
        if type_filter != "الكل" and question.get("question_type") != type_filter:
            continue
        if difficulty_filter != "الكل" and question.get("difficulty") != difficulty_filter:
            continue
        if not contains_search(question, search):
            continue
        result.append(question)
    return result


def inject_css() -> None:
    st.markdown(
        """
        <style>
        html, body, [class*="css"], [data-testid="stAppViewContainer"] {
            direction: rtl;
            text-align: right;
            font-family: "Noto Naskh Arabic", "Tajawal", "Arial", sans-serif;
        }
        [data-testid="stSidebar"] * { direction: rtl; text-align: right; }
        .block-container { padding-top: 1.5rem; max-width: 1280px; }
        .app-title { font-size: 1.55rem; font-weight: 750; margin-bottom: .25rem; }
        .subtle { color: #5c6470; font-size: .92rem; }
        .metric-row { margin: .4rem 0 1rem 0; }
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
        textarea { line-height: 1.85 !important; }
        div[data-testid="stRadio"] > div { gap: .6rem; }
        </style>
        """,
        unsafe_allow_html=True,
    )


def login_screen() -> str:
    st.markdown('<div class="app-title">مراجعة الأسئلة الذهبية لكتاب زاد المعاد</div>', unsafe_allow_html=True)
    st.markdown('<div class="subtle">الدخول بالاسم فقط. سيُحفظ كل حكم وتعديل وملاحظة باسم المراجع.</div>', unsafe_allow_html=True)
    default_name = st.session_state.get("reviewer_name", "")
    name = st.text_input("اسم المراجع", value=default_name, placeholder="اكتب اسمك هنا")
    if st.button("دخول", type="primary", use_container_width=True):
        cleaned = clean_name(name)
        if len(cleaned) < 2:
            st.warning("اكتب اسمًا واضحًا للمراجع.")
            st.stop()
        st.session_state["reviewer_name"] = cleaned
        st.rerun()
    st.stop()


def render_sidebar(questions: list[dict[str, Any]], reviews: dict[str, dict[str, Any]], reviewer_name: str) -> dict[str, Any]:
    st.sidebar.markdown(f"**المراجع:** {reviewer_name}")
    if st.sidebar.button("تغيير الاسم", use_container_width=True):
        st.session_state.pop("reviewer_name", None)
        st.rerun()

    reviewed_count = len(reviews)
    st.sidebar.metric("راجعتها", reviewed_count, f"من {len(questions)}")
    st.sidebar.progress(reviewed_count / len(questions) if questions else 0)

    all_files = sorted({file for question in questions for file in source_files(question)})
    disciplines = sorted({str(q.get("primary_discipline")) for q in questions if q.get("primary_discipline")})
    types = sorted({str(q.get("question_type")) for q in questions if q.get("question_type")})
    difficulties = sorted({str(q.get("difficulty")) for q in questions if q.get("difficulty")})

    st.sidebar.divider()
    status_filter = st.sidebar.selectbox("حالة مراجعتي", list(STATUS_OPTIONS), format_func=STATUS_OPTIONS.get)
    file_filter = st.sidebar.selectbox("الملف", ["الكل", *all_files])
    discipline_filter = st.sidebar.selectbox("المجال", ["الكل", *disciplines])
    type_filter = st.sidebar.selectbox("نوع السؤال", ["الكل", *types])
    difficulty_filter = st.sidebar.selectbox("المستوى", ["الكل", *difficulties])
    search = st.sidebar.text_input("بحث", placeholder="كلمة في السؤال أو الموضوع")

    csv_text = export_my_reviews(bootstrap(), reviewer_name)
    st.sidebar.download_button(
        "تنزيل مراجعاتي CSV",
        data=csv_text.encode("utf-8-sig"),
        file_name=f"reviews_{reviewer_name}.csv",
        mime="text/csv",
        use_container_width=True,
    )

    return {
        "status_filter": status_filter,
        "file_filter": file_filter,
        "discipline_filter": discipline_filter,
        "type_filter": type_filter,
        "difficulty_filter": difficulty_filter,
        "search": search,
    }


def render_question(database: ReviewDB, question: dict[str, Any], review: dict[str, Any] | None, reviewer_name: str) -> None:
    record_view(database, question["question_id"], reviewer_name)

    meta = [
        question.get("primary_discipline"),
        question.get("topic_level_1"),
        question.get("question_type"),
        question.get("difficulty"),
        " / ".join(source_files(question)),
        f"ص {question.get('page_start')} - {question.get('page_end')}",
    ]
    pills = "".join(f'<span class="meta-pill">{html.escape(str(item))}</span>' for item in meta if item)

    st.markdown(f'<div class="app-title">السؤال {question.get("review_bank_index")}</div>{pills}', unsafe_allow_html=True)

    current_decision = review.get("decision") if review else "approved"
    if current_decision not in DECISIONS:
        current_decision = "approved"

    with st.form(f"review_form_{question['question_id']}"):
        edited_question = st.text_area(
            "السؤال",
            value=(review.get("edited_question") if review else None) or question.get("question", ""),
            height=92,
        )
        edited_answer = st.text_area(
            "الإجابة المختصرة",
            value=(review.get("edited_answer") if review else None) or question.get("short_answer", ""),
            height=118,
        )
        decision = st.radio(
            "الحكم",
            list(DECISIONS),
            index=list(DECISIONS).index(current_decision),
            format_func=DECISIONS.get,
            horizontal=True,
        )
        notes = st.text_area(
            "ملاحظة مختصرة عند الحاجة",
            value=(review.get("notes") if review else "") or "",
            height=82,
            placeholder="مثال: السؤال يحتاج ضبط المصطلح، أو الجواب أوسع من موضع السؤال.",
        )
        saved = st.form_submit_button("حفظ المراجعة", type="primary", use_container_width=True)

    if saved:
        if not clean_name(edited_question) or not clean_name(edited_answer):
            st.warning("لا يمكن حفظ سؤال أو جواب فارغ.")
        else:
            save_review(
                database,
                question_id=question["question_id"],
                reviewer_name=reviewer_name,
                decision=decision,
                edited_question=edited_question.strip(),
                edited_answer=edited_answer.strip(),
                notes=notes.strip(),
            )
            st.success("تم حفظ المراجعة باسمك.")
            st.cache_data.clear()
            st.rerun()

    st.divider()
    st.subheader("الجواب من المقطع")
    answer_text = question.get("answer_span") or question.get("short_answer", "")
    st.markdown(f'<div class="answer-panel">{html.escape(answer_text)}</div>', unsafe_allow_html=True)

    child_text = question.get("child_text", "")
    parent_text = question.get("parent_text", "")
    needle = question.get("answer_span") or question.get("short_answer", "")

    with st.expander("المقطع التشايلد", expanded=True):
        st.markdown(highlighted_html(child_text, needle), unsafe_allow_html=True)

    with st.expander("السياق الأكبر الأب", expanded=True):
        st.markdown(highlighted_html(parent_text, needle), unsafe_allow_html=True)

    with st.expander("بيانات الربط والمصدر", expanded=False):
        st.write(
            {
                "question_id": question.get("question_id"),
                "retrieval_chunk_id": question.get("retrieval_chunk_id"),
                "parent_author_chunk_id": question.get("parent_author_chunk_id"),
                "parent_chunk_id": question.get("parent_chunk_id"),
                "topic_level_2": question.get("topic_level_2"),
                "topic_level_3": question.get("topic_level_3"),
                "primary_topic": question.get("primary_topic"),
                "answer_mode": question.get("answer_mode"),
            }
        )


def main() -> None:
    st.set_page_config(page_title="مراجعة أسئلة زاد المعاد", layout="wide")
    inject_css()

    reviewer_name = st.session_state.get("reviewer_name")
    if not reviewer_name:
        login_screen()

    database = bootstrap()
    questions = load_questions()
    reviews = fetch_reviews(database, reviewer_name)
    filters = render_sidebar(questions, reviews, reviewer_name)
    visible = filtered_questions(questions, reviews, **filters)

    if not visible:
        st.info("لا توجد أسئلة مطابقة للمرشحات الحالية.")
        return

    ids = [question["question_id"] for question in visible]
    selected = st.session_state.get("selected_question_id")
    if selected not in ids:
        selected = ids[0]
        st.session_state["selected_question_id"] = selected

    selected = st.selectbox(
        "اختر السؤال",
        ids,
        index=ids.index(selected),
        format_func=lambda qid: question_label(next(q for q in visible if q["question_id"] == qid), reviews.get(qid)),
        key="selected_question_id",
    )

    current_index = ids.index(selected)
    previous_col, next_col = st.columns(2)
    with previous_col:
        if st.button("السابق", disabled=current_index == 0, use_container_width=True):
            st.session_state["selected_question_id"] = ids[current_index - 1]
            st.rerun()
    with next_col:
        if st.button("التالي", disabled=current_index == len(ids) - 1, use_container_width=True):
            st.session_state["selected_question_id"] = ids[current_index + 1]
            st.rerun()

    question = next(q for q in visible if q["question_id"] == selected)
    render_question(database, question, reviews.get(selected), reviewer_name)


if __name__ == "__main__":
    main()
