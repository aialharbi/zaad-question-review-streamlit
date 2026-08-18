# Streamlit Question Review App

تطبيق خفيف لمراجعة أسئلة منتقاة من ملف اختبار الاسترجاع لزاد المعاد. يدخل المراجع باسمه فقط، ثم يحفظ حكمه وتعديلاته على السؤال والجواب باسمه في قاعدة البيانات.

## التشغيل المحلي

```bash
cd streamlit_question_review_app
python3 -m streamlit run streamlit_app.py
```

إذا لم تضبط قاعدة PostgreSQL سيستخدم التطبيق SQLite محليًا في ملف `local_review_state.sqlite3`.

## النشر على Streamlit Community Cloud

1. ارفع مجلد `streamlit_question_review_app` إلى GitHub ضمن المستودع.
2. في Streamlit Cloud اختر ملف الدخول:
   `streamlit_question_review_app/streamlit_app.py`
3. أضف سر قاعدة البيانات في إعدادات التطبيق المتقدمة:

```toml
DATABASE_URL = "postgresql://USER:PASSWORD@HOST:PORT/DATABASE?sslmode=require"
```

أو:

```toml
[database]
url = "postgresql://USER:PASSWORD@HOST:PORT/DATABASE?sslmode=require"
```

## قاعدة البيانات

يوصى بـ PostgreSQL مثل Neon أو Supabase لهذه المرحلة؛ لأنه يحفظ مراجعات عدة أشخاص بالتزامن. التطبيق ينشئ الجداول تلقائيًا:

- `question_source`: نسخة مرجعية من الأسئلة ومقاطعها.
- `review_state`: آخر مراجعة لكل سؤال حسب اسم المراجع.
- `review_events`: سجل أحداث للحفظ وفتح الأسئلة.

لا ترفع ملف `.streamlit/secrets.toml` الحقيقي إلى GitHub.
