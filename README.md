# Fieldwork Internship Management

A Django portal for interns, host organizations, and admins.

## Local setup

Use Python 3.12 or newer. Create and activate a virtual environment, install `requirements.txt`, and configure environment values from `.env.example`. Then run:

```powershell
python manage.py migrate
python manage.py createsuperuser
python manage.py runserver
```

The local default enables `DEBUG` and uses SQLite. Production requires `SECRET_KEY`, `DATABASE_URL`, and a non-empty `ALLOWED_HOSTS`. On Render, set `DATABASE_URL` to the database's internal connection string when the web service and database share a region. The build collects static files with a temporary in-memory SQLite configuration; `start.sh` verifies the configured database connection before applying migrations and starting Gunicorn.

## Roles and services

Public registration supports intern and company accounts. Company partners have dedicated sign-in and registration pages under `/accounts/company/`. Admin accounts use the existing `coordinator` role value; existing accounts and stored role values are preserved. CAPTCHA is enabled with `CAPTCHA_ENABLED=true`. Failed-login account lockouts are disabled; use strong passwords and enable CAPTCHA if additional login-abuse protection is needed.

Set `USE_S3=true` with an AWS bucket and region to store uploads in S3. Sentry activates when `SENTRY_DSN` is set. The ML predictors use optional joblib artifacts in `ml_engine/models/` and deterministic rule-based fallbacks otherwise.

## Face detection

The supplied YOLO11n face detector checkpoint is `ml_engine/models/best.pt`. Production inference uses its exported `ml_engine/models/best.onnx` model with ONNX Runtime on CPU; the Ultralytics and PyTorch packages are only needed when regenerating that ONNX file. NumPy is pinned to 2.5.3, and Pillow (`>=10.4,<12`) handles in-memory camera images.

Run the detector against an image with:

```powershell
python manage.py detect_faces path\to\image.jpg
```

The command prints JSON containing `face_detected`, `face_count`, and a `detections` array. Each detection has a confidence score and an `[x1, y1, x2, y2]` pixel bounding box. The ONNX session is loaded once per process. Attendance and camera behavior are unchanged; application code can call `ml_engine.face_detector.detect_faces` with an image path or a PIL image.

## Face-verified attendance

Interns use the Attendance page for face-verified time-in and time-out and the Daily reports page to submit their progress for Admin review. OJT document approval is not required for these workflows.

Interns can create or update their own face enrollment from Profile after informed consent. The enrollment camera uses the same face guide as attendance, runs live YOLO detection (green outline for one detected face, white when none is detected, red for multiple faces or a detection error), and permits capture feedback before saving. Only an encrypted face embedding is stored; the enrollment image is discarded after processing.

Interns perform face verification for both time-in and time-out by clicking the corresponding attendance button. The camera opens with an oval face-position guide and live face-detection feedback; the intern captures a frame and receives feedback before submitting. Exactly one detected face and a similarity score of at least 0.65 against the current enrollment are required; missing enrollment, no/multiple faces, a mismatch, or a camera/model failure blocks attendance. Enrollment checks the new embedding against every other saved enrollment and rejects a match at the same threshold. Preview checks do not store image captures, and the frame is checked again on submission. A verified face inside the assigned worksite radius is approved automatically; an out-of-radius or unavailable-GPS event requires an explanation and remains pending for company/Admin review. Time-out is unavailable until time-in is approved. YOLO detects faces; OpenCV Zoo's SFace model generates embeddings, and its YuNet model supplies alignment landmarks. The ONNX weights and their Apache 2.0/MIT licenses are in `ml_engine/models/` (`SFACE_LICENSE` and `YUNET_LICENSE`). Face embeddings and submitted attendance camera captures are encrypted in the database.

Only verified faces can create attendance events. A supervisor may review a face-verified event with a location exception, but face mismatches and invalid captures are rejected and are not submitted for review. Images are automatically deleted after 30 days; a scheduled daily invocation of `python manage.py purge_face_attendance_images` is required in each production environment. The command removes expired image bytes but leaves the attendance and review metadata. Configure this command in the host's scheduler (for example, a daily cron job).

Interns can enroll or update their face from Profile using the camera and face guide. Enrollment requires their explicit consent; only an encrypted face embedding is stored and the enrollment image is discarded after processing. Existing Admin enrollment remains available for assisted setup, including updating older consent versions. Attendance captures are viewable from the Admin DTR queue and the accepted company's DTR queue while retained; image access remains scoped to authorized reviewers. Deleting an enrollment from Admin tools also deletes that intern's attendance captures.

The cosine threshold (0.65) is a strict initial setting and is not calibrated for this intern population. This flow has no liveness/anti-spoof check, so a displayed photo may pass; positive face scores must not be treated as proof of liveness or identity certainty. Obtain informed consent, assess applicable privacy requirements, and validate false-match and false-reject rates before relying on face verification for consequential decisions.

For production, configure a stable `FACE_EMBEDDING_ENCRYPTION_KEY` secret. It encrypts both embeddings and attendance images. Generate a Fernet key locally with:

```powershell
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Set it in the deployment's secret environment variables; do not commit the key. When `DEBUG=true`, local development can derive a key from `SECRET_KEY`; otherwise the explicit Fernet key is required. Rotating either key requires deleting and recreating face enrollments. Camera capture requires browser permission and HTTPS except on localhost. The `.pt` checkpoint must come from a trusted source when running the export script because loading PyTorch checkpoints can execute serialized code.

`python manage.py seed_demo_data --password <temporary-password>` creates an example intern, company, and published opportunity. Without the option, the demo accounts are created with unusable passwords.

## Worksite enrollment and location-verified attendance

Company users can request new official worksites from **Worksite locations**. Requests remain inactive until Admin approval; an Admin may optionally replace an existing active worksite as part of approving a request. Admins can also register an approved worksite directly for a selected Company and manage external-placement locations. Interns accepted by a Fieldwork company can record attendance only at an active, approved worksite belonging to that assigned Company. Intern profile worksite information is read-only; Admins assign approved locations to external-placement interns, and all Interns must contact their Company or Admin about worksite changes.

The address search and draggable pin use Leaflet and OpenStreetMap when no existing Google Maps key is configured; no paid map service is required. Authorized Company and Admin users can capture their device location, review its reported accuracy, and confirm the address and pin before submission. The configurable `GPS_MAX_ACCURACY_METERS` threshold defaults to 30 meters; a worse reading must be retried before it can be submitted as a GPS-captured location. Geolocation requires HTTPS in production (localhost is permitted for development), and browser-reported accuracy is an estimate, not proof of physical presence.

Attendance uses the intern's submitted current-device coordinates and accuracy, but obtains the official worksite coordinates and configured radius from active, Admin-approved database records for the intern's accepted placement. The server recalculates Haversine distance; submitted Company coordinates, client-side distance, and client-side verification flags are ignored. A face match, an inside-geofence result, and GPS accuracy at or better than `GPS_MAX_ACCURACY_METERS` are all required for automatic approval. Outside, missing, or too-imprecise GPS requires an explanation and sends a flagged event to the existing supervisor review queue. Worksite and attendance coordinate fields retain the existing six-decimal-place schema. Browser-provided GPS is not tamper-proof and should not be treated as independent proof of a user's physical location; use appropriate organizational controls for consequential decisions.

Worksite registration uses Leaflet, OpenStreetMap map tiles, and click-to-search Nominatim address suggestions. The address search needs an internet connection and follows the public service's attribution and usage policies; keep requests user-initiated and rate-limited. Device location capture remains available as an optional aid.

Each time-in and time-out stores submitted coordinates, distance to the nearest applicable site, allowed radius, geofence result, and any explanation. Older attendance records retain an unknown (`NULL`) location-check status.

## DTR hours and academic periods

The intern attendance page groups entries into Monday–Sunday weeks and displays only fully approved time-in/time-out hours toward OJT completion. Rendered hours are allocated oldest-first and capped at 486.0 per intern; unapproved entries remain visible but contribute zero. Company users can open **Intern DTR summaries** for their accepted interns, while Admins can search all interns by name/student ID and filter by placement type or accepted company. DTR summaries are paginated.

Configure academic grading ranges in Django admin under **Academic periods**. Create one date-range record for each Prelim, Midterm, Pre-Final, and Final period, with an academic-year label; ranges must not overlap. The DTR period filter assigns attendance dates to these ranges. Existing attendance remains in **All periods** even if it falls outside configured dates.

Admins can open **User Directory** at `/coordinator/users/` to search/filter interns, companies, and admins. Intern rows show face-enrollment status and approved hours against the same 486-hour cap, with links to their DTR and Admin reset actions. Resetting face enrollment also deletes its associated attendance face captures; resetting a worksite deactivates active locations and rejects pending requests.

To test locally with Chrome/Edge location emulation:

1. Start the Django server and sign in as an intern with an accepted company placement or an external placement.
2. Open **Profile → OJT worksite location**, enter the site name/address, and choose **Get current location** while at the site. Review the coordinates and accuracy, then submit. The request should show as pending and should not yet enable attendance.
3. Sign in as the accepted host company's user and open **Worksite locations**, or sign in as an Admin and open **External intern worksites**. Approve the request; it becomes active and is used ahead of company-wide sites for that intern. Reset the approved location to allow the intern to submit a replacement.
4. Open DevTools, choose **More tools → Sensors**, and set **Location** to **Custom location** using the registered site's latitude and longitude. Reload Attendance, allow location access, and submit a face capture. A successful face match should be approved immediately.
5. Change the emulated coordinates to a point beyond the worksite radius. Capture/check again, choose **Provide reason and submit**, and enter an explanation of at least 10 characters. Confirm that the event appears as a yellow location exception in the supervisor's **Attendance review** queue and can be approved or rejected.
6. In Sensors, choose **Location unavailable** (or deny location permission). Repeat the submission with a required explanation; it should be queued as a GPS-unavailable exception.
7. To test the backend independently of browser emulation, POST `clock_in_latitude`/`clock_in_longitude` or `clock_out_latitude`/`clock_out_longitude` with the attendance action and include the matching `*_location_reason` field for outside/unavailable GPS. The server requires at least 10 reason characters and recalculates the geofence itself.

For temporary Render face-capture troubleshooting, set `FACE_CAPTURE_DIAGNOSTICS=true` in the service environment and inspect the service logs. Diagnostics report decoded image type, mode, dimensions, NumPy shape/dtype, and face count without logging image contents; disable the setting after troubleshooting.

The Render face detector runs `best.onnx` with ONNX Runtime's CPU provider, with one inference thread and one Gunicorn worker recommended for 512 MB instances. PyTorch and Ultralytics are not installed in the production requirements. To export or regenerate the model locally, install the export-only dependencies and run:

```powershell
python -m pip install -r requirements-export.txt
python ml_engine/export_face_detector.py
```

Commit or otherwise deploy the generated `ml_engine/models/best.onnx` file with the application. The export script embeds NMS and fixes the input size and detection limit to match the runtime implementation.

All interns can submit one daily report per workday for Admin review. Historical OJT document records remain in the database, but OJT pages have been retired and their old URLs redirect to the relevant core workflow. New interns are not given default requirements, and OJT approval does not block attendance or reports.

Run the project checks and tests with:

```powershell
python manage.py check
python manage.py test accounts.tests portal.tests ml_engine.tests
```