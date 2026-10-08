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

## Face detection

The supplied YOLO11n face detector is stored at `ml_engine/models/best.pt` and runs directly with Ultralytics on CPU; no model conversion or retraining is needed. The inference stack is pinned to Ultralytics 8.4.174, PyTorch 2.14.1 CPU, Torchvision 0.29.1 CPU, and NumPy 2.5.3 in `requirements.txt`. The existing Pillow range (`>=10.4,<12`; currently 11.3.0) is compatible and is sufficient for in-memory image inputs.

Run the detector against an image with:

```powershell
python manage.py detect_faces path\to\image.jpg
```

The command prints JSON containing `face_detected`, `face_count`, and a `detections` array. Each detection has a confidence score and an `[x1, y1, x2, y2]` pixel bounding box. The model is loaded once per process. Attendance and camera behavior are unchanged; application code can call `ml_engine.face_detector.detect_faces` with an image path or a PIL image.

The `.pt` checkpoint must come from a trusted source because loading PyTorch checkpoints can execute serialized code. CPU inference adds PyTorch and Ultralytics to the web-service environment; verify the deployment's available disk and memory before enabling inference there.

`python manage.py seed_demo_data --password <temporary-password>` creates an example intern, company, and published opportunity. Without the option, the demo accounts are created with unusable passwords.

## OJT document readiness

Newly registered interns receive seven required school, academic, legal, and health documents. Coordinators review and approve these baseline documents before an intern can record their first time-in. Partner companies can maintain their own required-document list; those items are assigned only after the baseline is approved and the company accepts the intern, and they do not block attendance. The coordinator reviews the submissions. Accepted partner companies can preview or download their interns' submitted OJT documents; interns and coordinators can access their respective documents as well. Uploads are limited to supported document formats and 5 MB.

Interns can set their placement type in their profile. For external companies that do not use Fieldwork, the intern enters the host name, records attendance in Fieldwork, and submits a daily progress report for coordinator review. Coordinators review external interns' attendance, daily reports, and OJT documents and can monitor approved OJT hours in the intern directory. Partner-company interns continue using company supervisor attendance and weekly report workflows; coordinators can monitor all interns and their progress. The current section is highlighted in the navigation.

Run the project checks and tests with:

```powershell
python manage.py check
python manage.py test accounts.tests portal.tests ml_engine.tests
```