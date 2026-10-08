# Fieldwork Internship Management

A Django portal for interns, host organizations, and program coordinators.

## Local setup

Use Python 3.12 or newer. Create and activate a virtual environment, install `requirements.txt`, and configure environment values from `.env.example`. Then run:

```powershell
python manage.py migrate
python manage.py createsuperuser
python manage.py runserver
```

The local default enables `DEBUG` and uses SQLite. Production requires `SECRET_KEY`, `DATABASE_URL`, and a non-empty `ALLOWED_HOSTS`. Render deployment is configured in `render.yaml` and runs migrations during the build.

## Roles and services

Public registration supports intern and company accounts. Coordinator accounts should be created by an administrator and assigned the coordinator role. CAPTCHA is enabled with `CAPTCHA_ENABLED=true`. Failed-login account lockouts are disabled; use strong passwords and enable CAPTCHA if additional login-abuse protection is needed.

Set `USE_S3=true` with an AWS bucket and region to store uploads in S3. Sentry activates when `SENTRY_DSN` is set. The ML predictors use optional joblib artifacts in `ml_engine/models/` and deterministic rule-based fallbacks otherwise.

`python manage.py seed_demo_data --password <temporary-password>` creates an example intern, company, and published opportunity. Without the option, the demo accounts are created with unusable passwords.

## OJT document readiness

Newly registered interns receive seven required school, academic, legal, and health documents. Coordinators review and approve these baseline documents before an intern can record their first time-in. Partner companies can maintain their own required-document list; those items are assigned only after the baseline is approved and the company accepts the intern, and they do not block attendance. The coordinator reviews the submissions. Accepted partner companies can preview or download their interns' submitted OJT documents; interns and coordinators can access their respective documents as well. Uploads are limited to supported document formats and 5 MB.

Interns can set their placement type in their profile. For external companies that do not use Fieldwork, the intern enters the host name, records attendance in Fieldwork, and submits a daily progress report for coordinator review. Coordinators review external interns' attendance, daily reports, and OJT documents and can monitor approved OJT hours in the intern directory. Partner-company interns continue using company supervisor attendance and weekly report workflows; coordinators can monitor all interns and their progress. The current section is highlighted in the navigation.

Run the project checks and tests with:

```powershell
python manage.py check
python manage.py test accounts.tests portal.tests ml_engine.tests
```