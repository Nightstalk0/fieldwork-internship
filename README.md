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

Run the project checks and tests with:

```powershell
python manage.py check
python manage.py test accounts.tests portal.tests ml_engine.tests
```