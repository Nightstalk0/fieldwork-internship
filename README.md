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

The supplied YOLO11n face detector checkpoint is `ml_engine/models/best.pt`. Production inference uses its exported `ml_engine/models/best.onnx` model with ONNX Runtime on CPU; the Ultralytics and PyTorch packages are only needed when regenerating that ONNX file. NumPy is pinned to 2.5.3, and Pillow (`>=10.4,<12`) handles in-memory camera images.

Run the detector against an image with:

```powershell
python manage.py detect_faces path\to\image.jpg
```

The command prints JSON containing `face_detected`, `face_count`, and a `detections` array. Each detection has a confidence score and an `[x1, y1, x2, y2]` pixel bounding box. The ONNX session is loaded once per process. Attendance and camera behavior are unchanged; application code can call `ml_engine.face_detector.detect_faces` with an image path or a PIL image.

## Face-verified attendance

Interns use the Attendance page for face-verified time-in and time-out and the Daily reports page to submit their progress for coordinator review. OJT document approval is not required for these workflows.

Interns can create or update their own face enrollment from Profile after informed consent. The enrollment camera uses the same face guide as attendance, runs live YOLO detection (green outline for one detected face, white when none is detected, red for multiple faces or a detection error), and permits capture feedback before saving. Only an encrypted face embedding is stored; the enrollment image is discarded after processing.

Interns perform face verification for both time-in and time-out by clicking the corresponding attendance button. The camera opens with an oval face-position guide and live face-detection feedback; the intern captures a frame and receives feedback before submitting. A single detected face that passes the provisional SFace comparison is labeled a good match candidate; one detected face without a passing match is marked as needing review; no face, missing enrollment, multiple faces, or a camera/model failure is marked as a poor capture. Interns can retake any capture or submit it for review. Preview checks do not store image captures. On submission, the frame is checked again. A matched face inside the assigned worksite radius is approved automatically; an out-of-radius or unavailable-GPS event requires an explanation and remains pending for company/coordinator review. Face-match failures also remain pending for review. Time-out is unavailable until time-in is approved. YOLO still detects faces; OpenCV Zoo's SFace model generates embeddings, and its YuNet model supplies alignment landmarks. The ONNX weights and their Apache 2.0/MIT licenses are in `ml_engine/models/` (`SFACE_LICENSE` and `YUNET_LICENSE`). Face embeddings and submitted attendance camera captures are encrypted in the database.

If a capture is missing, the match fails, or the face models are unavailable, the time is not approved automatically and the request is listed as pending. A company supervisor can review requests for their accepted interns; coordinators/admins can review all requests. They can view the encrypted capture while retained and approve the time event at its original capture time or reject it. Company and coordinator attendance review pages show successful matches and exceptions in the same review queue. Images are automatically deleted after 30 days; a scheduled daily invocation of `python manage.py purge_face_attendance_images` is required in each production environment. The command removes expired image bytes but leaves the attendance and review metadata. Configure this command in the host's scheduler (for example, a daily cron job).

Interns can enroll or update their face from Profile using the camera and face guide. Enrollment requires their explicit consent; only an encrypted face embedding is stored and the enrollment image is discarded after processing. Existing coordinator enrollment remains available for assisted setup, including updating older consent versions. Attendance captures are viewable from the coordinator DTR queue and the accepted company's DTR queue while retained; image access remains scoped to authorized reviewers. Deleting an enrollment from coordinator tools also deletes that intern's attendance captures.

The SFace example cosine threshold (0.363) is provisional and is not calibrated for this intern population. This flow has no liveness/anti-spoof check, so a displayed photo may pass; positive face scores and review approvals must not be treated as proof of liveness or identity certainty. Obtain informed consent, assess applicable privacy requirements, and validate false-match and false-reject rates before relying on face verification for consequential decisions.

For production, configure a stable `FACE_EMBEDDING_ENCRYPTION_KEY` secret. It encrypts both embeddings and attendance images. Generate a Fernet key locally with:

```powershell
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Set it in the deployment's secret environment variables; do not commit the key. When `DEBUG=true`, local development can derive a key from `SECRET_KEY`; otherwise the explicit Fernet key is required. Rotating either key requires deleting and recreating face enrollments. Camera capture requires browser permission and HTTPS except on localhost. The `.pt` checkpoint must come from a trusted source when running the export script because loading PyTorch checkpoints can execute serialized code.

`python manage.py seed_demo_data --password <temporary-password>` creates an example intern, company, and published opportunity. Without the option, the demo accounts are created with unusable passwords.

## Worksite enrollment and location-verified attendance

Company users enroll each of their worksites from **Worksite locations**, using a device at the worksite to capture its coordinates. Each worksite has its own allowed radius, defaulting to 100 meters. Interns accepted by a Fieldwork company can record attendance at an active worksite for that company. Coordinators enroll and assign locations to external-placement interns. A location must be assigned before attendance is available.

Interns can also submit a personal worksite location from **Profile** while at the site. The browser requests high-accuracy geolocation, then shows the captured coordinates and reported accuracy for confirmation. Submissions use a 100-meter attendance radius and stay inactive until a supervisor approves them. Company supervisors review requests from accepted interns under **Worksite locations**; coordinators review external-placement requests under **External intern worksites**. An approved personal location takes precedence for that intern. Supervisors can reset it before the intern submits a replacement. Geolocation requires HTTPS in production (localhost is permitted for development), and browser-reported accuracy is an estimate, not proof of physical presence.

The server checks submitted coordinates against assigned worksite coordinates and radii. A face match within an allowed radius is automatically approved. If the intern is outside all assigned radii or browser GPS is unavailable, the page requires an explanation and sends a yellow-flagged attendance event to the host company's review queue (or the coordinator queue for external placements). The event stays pending until an authorized reviewer approves or rejects it. Browser-provided GPS is not tamper-proof and should not be treated as independent proof of a user's physical location; use appropriate organizational controls for consequential decisions.

Each time-in and time-out stores submitted coordinates, distance to the nearest applicable site, allowed radius, geofence result, and any explanation. Older attendance records retain an unknown (`NULL`) location-check status.

To test locally with Chrome/Edge location emulation:

1. Start the Django server and sign in as an intern with an accepted company placement or an external placement.
2. Open **Profile → OJT worksite location**, enter the site name/address, and choose **Get current location** while at the site. Review the coordinates and accuracy, then submit. The request should show as pending and should not yet enable attendance.
3. Sign in as the accepted host company's supervisor and open **Worksite locations**, or sign in as a coordinator and open **External intern worksites**. Approve the request; it becomes active and is used ahead of company-wide sites for that intern. Reset the approved location to allow the intern to submit a replacement.
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

All interns can submit one daily report per workday for coordinator review. Historical OJT document records remain in the database, but OJT pages have been retired and their old URLs redirect to the relevant core workflow. New interns are not given default requirements, and OJT approval does not block attendance or reports.

Run the project checks and tests with:

```powershell
python manage.py check
python manage.py test accounts.tests portal.tests ml_engine.tests
```