(() => {
  const maximumConsecutiveErrors = 3;

  const readFaceApiResponse = async (response) => {
    const contentType = response.headers.get("content-type") || "";
    if (response.redirected) {
      throw new Error("Your session may have expired. Refresh the page and sign in again.");
    }
    if (!contentType.includes("application/json")) {
      if (response.status === 403) {
        throw new Error("The security check failed. Refresh the page and try again.");
      }
      if (response.status === 404) {
        throw new Error("The face-capture service is unavailable. Refresh the page and try again.");
      }
      throw new Error(`The server returned an unexpected response (HTTP ${response.status}). Try again shortly.`);
    }

    let result;
    try {
      result = await response.json();
    } catch {
      throw new Error(`The server returned an unreadable response (HTTP ${response.status}). Try again shortly.`);
    }
    if (!response.ok) throw new Error(result.error || result.message || `Face capture failed (HTTP ${response.status}).`);
    return result;
  };

  window.readFaceApiResponse = readFaceApiResponse;

  const startFaceDetectionGuide = ({
    video,
    frame,
    status,
    endpoint,
    initialMessage,
    intervalMs = 1200,
    diagnosticsEnabled = false,
    onDetection,
    onDiagnostic,
  }) => {
    let active = true;
    let timerId = null;
    let controller = null;
    let requestInFlight = false;
    let inFlightPromise = Promise.resolve();
    let resolveInFlight = null;
    let consecutiveErrors = 0;
    const csrfToken = document.querySelector("[name=csrfmiddlewaretoken]")?.value;
    const reportDiagnostic = (state, error = "") => {
      if (diagnosticsEnabled && ["started", "stopped", "error"].includes(state)) {
        console.debug("[face-detection]", state, error);
      }
      if (typeof onDiagnostic !== "function") return;
      try {
        onDiagnostic({ state, error });
      } catch (diagnosticError) {
        if (diagnosticsEnabled) console.error("[face-detection] Diagnostic callback failed.", diagnosticError);
      }
    };

    const update = (state, text) => {
      try {
        frame.classList.remove(
          "face-guide-detected",
          "face-guide-ready",
          "face-guide-processing",
          "face-guide-mismatch",
          "face-guide-multiple",
          "face-guide-error",
        );
        if (state) frame.classList.add(`face-guide-${state}`);
        status.textContent = text;
      } catch (error) {
        if (diagnosticsEnabled) console.error("[face-detection] Could not update capture status.", error);
      }
    };

    const showDetectionBox = (result) => {
      frame.querySelectorAll(".face-detection-box").forEach((box) => box.remove());
      const detection = result.detections?.[0];
      if (!detection?.bbox || !result.image_width || !result.image_height) return;

      const frameRect = frame.getBoundingClientRect();
      const scale = Math.max(frameRect.width / video.videoWidth, frameRect.height / video.videoHeight);
      const offsetX = (video.videoWidth * scale - frameRect.width) / 2;
      const offsetY = (video.videoHeight * scale - frameRect.height) / 2;
      const sourceScaleX = video.videoWidth / result.image_width;
      const sourceScaleY = video.videoHeight / result.image_height;
      const [x1, y1, x2, y2] = detection.bbox;
      const left = Math.max(0, x1 * sourceScaleX * scale - offsetX);
      const top = Math.max(0, y1 * sourceScaleY * scale - offsetY);
      const right = Math.min(frameRect.width, x2 * sourceScaleX * scale - offsetX);
      const bottom = Math.min(frameRect.height, y2 * sourceScaleY * scale - offsetY);
      if (right <= left || bottom <= top) return;

      const box = document.createElement("span");
      box.className = "face-detection-box";
      box.setAttribute("aria-hidden", "true");
      box.style.left = `${left / frameRect.width * 100}%`;
      box.style.top = `${top / frameRect.height * 100}%`;
      box.style.width = `${(right - left) / frameRect.width * 100}%`;
      box.style.height = `${(bottom - top) / frameRect.height * 100}%`;
      frame.append(box);
    };

    const detect = async () => {
      if (!active || requestInFlight) return;
      requestInFlight = true;
      inFlightPromise = new Promise((resolve) => {
        resolveInFlight = resolve;
      });
      let delay = intervalMs;
      try {
        if (!video.paused && video.readyState >= 2 && video.videoWidth && video.videoHeight) {
          const canvas = document.createElement("canvas");
          const scale = Math.min(1, 640 / video.videoWidth);
          canvas.width = Math.max(1, Math.round(video.videoWidth * scale));
          canvas.height = Math.max(1, Math.round(video.videoHeight * scale));
          const context = canvas.getContext("2d");
          if (!context) throw new Error("Could not prepare the camera frame.");
          context.drawImage(video, 0, 0, canvas.width, canvas.height);

          const body = new FormData();
          body.set("face_image", canvas.toDataURL("image/jpeg", 0.65));
          if (csrfToken) body.set("csrfmiddlewaretoken", csrfToken);
          const requestController = new AbortController();
          controller = requestController;
          const requestTimeoutId = window.setTimeout(() => {
            requestController.abort();
          }, 15000);
          reportDiagnostic("detection-request-pending");
          let result;
          try {
            const response = await fetch(endpoint, {
              method: "POST",
              body,
              credentials: "same-origin",
              headers: { "X-Requested-With": "XMLHttpRequest" },
              signal: requestController.signal,
            });
            result = await readFaceApiResponse(response);
          } finally {
            window.clearTimeout(requestTimeoutId);
          }
          if (!active) return;
          consecutiveErrors = 0;
          reportDiagnostic("detection-response-success");
          showDetectionBox(result);
          reportDiagnostic("callback-running");
          const presentation = onDetection
            ? onDetection(result, { width: canvas.width, height: canvas.height })
            : null;
          if (presentation) {
            update(presentation.state, presentation.message);
          } else if (result.face_count > 1) {
            update("multiple", "More than one clearly visible face detected. Only one person should be in frame.");
          } else if (result.face_count === 1 && result.detections?.[0]?.quality_issue === "lighting") {
            update("", "Please move to a brighter area.");
          } else if (result.face_count === 1 && result.detections?.[0]?.quality_issue === "blur") {
            update("", "Keep your face steady; the image is blurry.");
          } else if (result.face_count === 1 && result.detections?.[0]?.quality_issue === "contrast") {
            update("", "Please move to a brighter area.");
          } else if (result.face_count === 1 && result.detections?.[0]?.quality_issue === "too_far") {
            update("", "Move closer to the camera.");
          } else if (
            result.face_count === 1
            && result.detections?.[0]?.roll_degrees !== null
            && Math.abs(result.detections[0].roll_degrees) > 20
          ) {
            update("", "Keep your head level and look directly at the camera.");
          } else if (result.face_count === 1 && !result.detections?.[0]?.alignment_ready) {
            update("", "Look directly at the camera and keep your full face visible.");
          } else if (result.face_count === 1) {
            update("detected", "Face detected. Keep it inside the guide.");
          } else {
            update("", "No face detected yet. Position your face inside the white outline.");
          }
          if (active) reportDiagnostic("callback-complete");
        } else {
          reportDiagnostic("video-not-ready");
          update("", "Waiting for a live video frame...");
        }
      } catch (error) {
        if (!active) return;
        const detail = error?.name === "AbortError"
          ? "Face detection request timed out."
          : error instanceof Error ? error.message : "Face detection failed.";
        reportDiagnostic("error", detail);
        consecutiveErrors += 1;
        if (consecutiveErrors >= maximumConsecutiveErrors) {
          active = false;
          update("error", "Face detection is unavailable. Please retry.");
          reportDiagnostic("unavailable", detail);
          return;
        }
        update("error", "Face detection is temporarily unavailable. Retrying...");
        delay = 5000;
      } finally {
        requestInFlight = false;
        resolveInFlight?.();
        resolveInFlight = null;
        inFlightPromise = Promise.resolve();
        if (active) timerId = window.setTimeout(detect, delay);
      }
    };

    update("", initialMessage || "Looking for one face…");
    reportDiagnostic("started");
    detect();
    const stop = (reason = "cleanup requested", { abortRequest = true } = {}) => {
      if (!active) return;
      active = false;
      window.clearTimeout(timerId);
      if (abortRequest) controller?.abort();
      frame.querySelectorAll(".face-detection-box").forEach((box) => box.remove());
      reportDiagnostic("stopped", reason);
    };
    stop.isActive = () => active;
    stop.whenIdle = () => inFlightPromise;
    return stop;
  };

  window.startFaceDetectionGuide = startFaceDetectionGuide;
})();
