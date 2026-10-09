(() => {
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

  const startFaceDetectionGuide = ({ video, frame, status, endpoint }) => {
    let active = true;
    const csrfToken = document.querySelector("[name=csrfmiddlewaretoken]")?.value;

    const update = (state, text) => {
      frame.classList.remove("face-guide-detected", "face-guide-multiple", "face-guide-error");
      if (state) frame.classList.add(`face-guide-${state}`);
      status.textContent = text;
    };

    const detect = async () => {
      if (!active) return;
      let delay = 1200;
      if (!video.paused && video.videoWidth && video.videoHeight) {
        try {
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
          const response = await fetch(endpoint, {
            method: "POST",
            body,
            credentials: "same-origin",
            headers: { "X-Requested-With": "XMLHttpRequest" },
          });
          const result = await readFaceApiResponse(response);
          if (!active) return;
          if (result.face_count === 1) {
            update("detected", "Face detected. Keep your face inside the green outline.");
          } else if (result.face_count > 1) {
            update("multiple", "More than one face detected. Only one person should be in frame.");
          } else {
            update("", "No face detected yet. Position your face inside the white outline.");
          }
        } catch (error) {
          if (!active) return;
          update("error", error instanceof Error ? error.message : "Live face detection is unavailable.");
          delay = 5000;
        }
      }
      if (active) window.setTimeout(detect, delay);
    };

    update("", "Looking for one face…");
    detect();
    return () => {
      active = false;
    };
  };

  window.startFaceDetectionGuide = startFaceDetectionGuide;
})();
