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

Interns submit their own face enrollment from Profile after informed consent. The enrollment camera uses the local YOLO and YuNet detectors, maps the detected face onto the visible video crop, and captures automatically after two consecutive usable frames show a centered, correctly sized face. The guide checks the mapped face center rather than requiring every bounding-box corner to fit inside the oval; a manual capture fallback appears after four seconds when a usable face remains visible but automatic stability is delayed. The captured image is previewed locally and checked by the backend before Submit is enabled, so the fallback does not bypass image-quality or identity checks. Re-registration must match the Intern's existing encrypted embedding; a first enrollment has no reference to match and is checked for image quality only. The encrypted embedding is saved as a pending submission; Admin approval is required before it becomes the active attendance reference. Rejected submissions or Admin-requested retakes unlock another submission. Existing approved embeddings remain active until a replacement is approved, and prior encrypted submissions are retained as history.

Interns start time-in or time-out to open the front-facing camera; verification begins automatically after a single face is centered, sized correctly, steady, and aligned in the guide. Live box coordinates are mapped to the visible video crop, and the backend checks face count, detection confidence, facial landmarks, head tilt, lighting, blur, and image alignment before recognition. SFace compares the normalized embedding with the authenticated Intern's approved enrollment. A non-match or unusable capture is rejected and the camera allows another attempt. The backend issues a short-lived signed token bound to the Intern, attendance action, and exact image after a successful match; the attendance submission validates that token rather than repeating the expensive face comparison. Frontend status is never trusted as proof of a match. A match inside the assigned worksite radius is approved automatically; an out-of-radius or unavailable-GPS event requires an explanation and remains pending for company/Admin review. Time-out is unavailable until time-in is approved. YOLO and YuNet provide local ONNX detection/landmarks, and OpenCV Zoo's SFace model generates embeddings. The ONNX weights and their Apache 2.0/MIT licenses are in `ml_engine/models/` (`SFACE_LICENSE` and `YUNET_LICENSE`). Face embeddings and submitted attendance camera captures are encrypted in the database.

Only verified faces can create attendance events. A supervisor may review a face-verified event with a location exception, but face mismatches and invalid captures are rejected and are not submitted for review. Images are automatically deleted after 30 days; a scheduled daily invocation of `python manage.py purge_face_attendance_images` is required in each production environment. The command removes expired image bytes but leaves the attendance and review metadata. Configure this command in the host's scheduler (for example, a daily cron job).

Interns can submit or retake their face from Profile using the camera and face guide. Enrollment requires their explicit consent; captured face images are held only for the current page submission and are discarded after processing, while only encrypted face embeddings are stored. Existing Admin enrollment remains available for assisted setup, including updating older consent versions. Attendance captures are viewable from the Admin DTR queue and the accepted company's DTR queue while retained; image access remains scoped to authorized reviewers. Resetting an enrollment archives its encrypted template and preserves attendance captures and attendance records.

The cosine threshold defaults to 0.65 and can be raised with `FACE_COSINE_MATCH_THRESHOLD`; configuration below 0.65 is rejected. This starting threshold is not calibrated for this intern population and must be tested with legitimate enrolled-user samples before relying on it. Quality thresholds also need validation against representative cameras and lighting. This flow has no liveness/anti-spoof check, so a displayed photo may pass; positive face scores must not be treated as proof of liveness or identity certainty. Obtain informed consent, assess applicable privacy requirements, and validate false-match and false-reject rates before relying on face verification for consequential decisions.

For production, configure a stable `FACE_EMBEDDING_ENCRYPTION_KEY` secret. It encrypts both embeddings and attendance images. Generate a Fernet key locally with:

```powershell
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Set it in the deployment's secret environment variables; do not commit the key. When `DEBUG=true`, local development can derive a key from `SECRET_KEY`; otherwise the explicit Fernet key is required. Rotating either key requires deleting and recreating face enrollments. Camera access requires browser permission and may require HTTPS or localhost depending on the browser and device. The responsive preview uses the front-facing camera where available, avoids stretching, and stops its stream when verification ends or the page closes. The `.pt` checkpoint must come from a trusted source when running the export script because loading PyTorch checkpoints can execute serialized code.

`python manage.py seed_demo_data --password <temporary-password>` creates an example intern, company, and published opportunity. Without the option, the demo accounts are created with unusable passwords.

## Worksite enrollment and location-verified attendance

Every Intern, including Fieldwork and External Company placements, submits their own worksite name, complete address, coordinates, and available GPS accuracy from **Worksite Location**. The submission is locked while pending and after approval. Admins review it in **Admin approvals** and can approve, reject with a reason, or request a correction with instructions; only rejection or an Admin correction request unlocks resubmission. Company users can view approved locations in read-only mode but cannot submit, approve, or change them. Admins may still manage Company-wide reference geofences, but these do not bypass the Intern's required submission or serve as that Intern's attendance geofence.

The address search and draggable pin use Leaflet and OpenStreetMap when no existing Google Maps key is configured; no paid map service is required. Interns and authorized Admin users can capture device location, review its reported accuracy, and confirm the address and pin before submission. The configurable `GPS_MAX_ACCURACY_METERS` threshold defaults to 30 meters; a worse reading must be retried before it can be submitted as a GPS-captured location. Geolocation requires HTTPS in production (localhost is permitted for development), and browser-reported accuracy is an estimate, not proof of physical presence.

Attendance uses the intern's submitted current-device coordinates and accuracy, but obtains the official worksite coordinates and configured radius only from that Intern's active, Admin-approved submission. Pending, rejected, and Company-wide locations cannot verify that Intern's attendance. The server recalculates Haversine distance; submitted Company coordinates, client-side distance, and client-side verification flags are ignored. A face match, an inside-geofence result, and GPS accuracy at or better than `GPS_MAX_ACCURACY_METERS` are all required for automatic approval. Outside, missing, or too-imprecise GPS requires an explanation and sends a flagged event to the existing supervisor review queue. Worksite and attendance coordinate fields retain the existing six-decimal-place schema. Browser-provided GPS is not tamper-proof and should not be treated as independent proof of a user's physical location; use appropriate organizational controls for consequential decisions.

Worksite registration uses Leaflet, OpenStreetMap map tiles, and click-to-search Nominatim address suggestions. The address search needs an internet connection and follows the public service's attribution and usage policies; keep requests user-initiated and rate-limited. Device location capture remains available as an optional aid.

Each time-in and time-out stores submitted coordinates, distance to the nearest applicable site, allowed radius, geofence result, and any explanation. Older attendance records retain an unknown (`NULL`) location-check status.

## DTR hours and academic periods

The intern attendance page groups entries into Monday–Sunday weeks and displays only fully approved time-in/time-out hours toward OJT completion. Rendered hours are allocated oldest-first and capped at 486.0 per intern; unapproved entries remain visible but contribute zero. Company users can open **Intern DTR summaries** for their accepted interns, while Admins can search all interns by name/student ID and filter by placement type or accepted company. DTR summaries are paginated.

Configure academic grading ranges in Django admin under **Academic periods**. Create one date-range record for each Prelim, Midterm, Pre-Final, and Final period, with an academic-year label; ranges must not overlap. The DTR period filter assigns attendance dates to these ranges. Existing attendance remains in **All periods** even if it falls outside configured dates.

Admins can open **User Directory** at `/coordinator/users/` to search/filter interns, companies, and admins. Intern rows show face-enrollment status and approved hours against the same 486-hour cap, with links to their DTR and Admin reset actions. Resetting a face enrollment archives the encrypted template without deleting attendance captures or records. Resetting a worksite requires Admin instructions, preserves the submission record, and unlocks a corrected Intern submission.

To test locally with Chrome/Edge location emulation:

1. Start the Django server and sign in as an intern with an accepted company placement or an external placement.
2. Open **Profile → OJT worksite location**, enter the site name/address, and choose **Get current location** while at the site. Review the coordinates and accuracy, then submit. The request should show as pending and should not yet enable attendance.
3. Sign in as an Admin and open **Admin approvals**. Approve the request; it becomes the official attendance geofence for that Intern. Reject it or request a correction with instructions to unlock resubmission. Company users can view approved information but have no review actions.
4. Open **Profile** and start the camera. Hold a centered face steady inside the oval until the stability indicator completes; use the manual capture fallback if it is offered. For a retake, use the same enrolled face. Review the captured preview and submit it for Admin review. The pending enrollment is not used for attendance; approve it before testing face-verified attendance. Request a retake or reject with a reason to unlock a new submission while preserving the previous approved enrollment.
5. Open DevTools, choose **More tools → Sensors**, and set **Location** to **Custom location** using the approved Intern site's latitude and longitude. Reload Attendance, allow location access, and submit a face capture. A successful face match should be approved immediately.
6. Change the emulated coordinates to a point beyond the worksite radius. Capture/check again, choose **Provide reason and submit**, and enter an explanation of at least 10 characters. Confirm that the event appears as a yellow location exception in the supervisor's **Attendance review** queue and can be approved or rejected.
7. In Sensors, choose **Location unavailable** (or deny location permission). Repeat the submission with a required explanation; it should be queued as a GPS-unavailable exception.
8. To test the backend independently of browser emulation, POST `clock_in_latitude`/`clock_in_longitude` or `clock_out_latitude`/`clock_out_longitude` with the attendance action and include the matching `*_location_reason` field for outside/unavailable GPS. The server requires at least 10 reason characters and recalculates the geofence itself.

For face-capture troubleshooting, in Chrome or Edge open DevTools with **F12**, select **Console**, and reproduce once. By default, technical diagnostics are not shown to interns. Temporarily set `FACE_CAPTURE_DIAGNOSTICS=true` to log detector lifecycle/errors and capture decisions to the browser console; this also enables the similarity score in the UI only after the backend check. Share the first relevant console error and user-facing status, but never share camera images or biometric embeddings. Server logs report decoded image metadata and face count without logging image contents. Disable the setting after troubleshooting.

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