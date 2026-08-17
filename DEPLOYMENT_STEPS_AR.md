# دليل النشر السريع على Streamlit Cloud

## 1. GitHub

أنشئ مستودعًا خاصًا وفارغًا باسم مقترح:

`zaad-question-review-streamlit`

لا تضف README أو `.gitignore` من GitHub؛ لأن الملفات موجودة محليًا.

بعد إنشاء المستودع أعطِ Codex رابط المستودع، أو نفذ من داخل هذا المجلد:

```bash
git remote add origin https://github.com/USERNAME/zaad-question-review-streamlit.git
git push -u origin main
```

## 2. قاعدة البيانات

الاختيار الموصى به: PostgreSQL، مثل Neon أو Supabase.

سبب الاختيار: التطبيق سيستخدمه عدة مراجعين، ونحتاج حفظًا متزامنًا وآمنًا للمراجعات. SQLite مناسب للتجربة المحلية فقط، وليس مناسبًا كقاعدة دائمة في Streamlit Cloud.

انسخ رابط الاتصال بصيغة قريبة من:

```text
postgresql://USER:PASSWORD@HOST:PORT/DATABASE?sslmode=require
```

## 3. Streamlit Cloud

1. افتح Streamlit Cloud.
2. اختر New app.
3. اختر مستودع GitHub.
4. Branch: `main`.
5. Main file path إذا كان المستودع هو هذا المجلد فقط:

`streamlit_app.py`

إذا وضع التطبيق داخل مستودع أكبر فالمسار هو:

`streamlit_question_review_app/streamlit_app.py`

## 4. Secrets

في Advanced settings ضع:

```toml
DATABASE_URL = "postgresql://USER:PASSWORD@HOST:PORT/DATABASE?sslmode=require"
```

ولا ترفع ملف `secrets.toml` الحقيقي إلى GitHub.

## 5. اختبار بعد النشر

- افتح التطبيق.
- اكتب اسم مراجع تجريبي.
- افتح سؤالًا.
- عدل كلمة في السؤال أو الجواب.
- احفظ.
- غيّر الاسم ثم ارجع للاسم الأول للتأكد أن المراجعة بقيت مرتبطة به.

## ملاحظة مهمة

الحفظ مبني على اسم المراجع فقط كما طلبت. لذلك ينبغي الاتفاق مع الأخوة على كتابة الاسم بصيغة ثابتة، مثل: الاسم الثنائي أو البريد المختصر، حتى لا تتكرر هويات المراجع بسبب اختلاف الكتابة.
