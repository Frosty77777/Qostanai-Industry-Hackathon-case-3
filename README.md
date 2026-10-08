# AI Exam Guard

Local AI-assisted proctoring system combining computer vision, behavior monitoring,
and secure exam environment monitoring. It highlights **suspicious events** for
**human review**; it does not establish cheating or replace an instructor's judgment.

The Python 3.12 desktop application runs on CPU, uses a webcam, and saves reports
and evidence locally. The original OpenCV-only CLI remains available.

## Features

- YOLO11n person/phone detection with bounding boxes and confidence labels.
- MediaPipe face presence/count and head states: CENTER, LEFT, RIGHT, UP, DOWN, UNKNOWN.
- Confidence/geometry filtering, duration thresholds, state tracking, and cooldowns.
- Persistent face-missing and camera-obstruction warnings; live head direction and duration.
- Event-based session risk from 0 to 100.
- Instructor-PIN-authorized breaks with a visible countdown.
- Windows focus-loss and selected keyboard-shortcut detection.
- Full final report, complete timeline, evidence thumbnails and larger image viewer.
- Separate local JSON/evidence folders; New Session preserves previous completed sessions.
- Camera/model preflight, clear module statuses, background CV, and cooperative cleanup.

## Architecture

```text
Webcam -> YOLO11n + MediaPipe -> shared filtering -> Event Engine
                               raw image quality -> obstruction state
Windows focus/keyboard monitor ------------------> normalized events
Authorized break manager ------------------------> session transitions
                                                   |
                                                   v
                                         Risk Engine + Evidence
                                                   |
                        QThread -> latest-frame mailbox -> Exam page
                                                   |
                                           complete event history
                                                   |
                                       Session JSON + Final Report
```

CV, Event Engine, Risk Engine, EvidenceManager, and security remain separate
modules. `VisionWorker` owns capture, inference, engines, evidence, and security
polling in a `QThread`. A locked mailbox coalesces display frames while retaining
every pending event. Detached `QImage` data crosses threads; widgets and
`QPixmap` update only on the UI thread.

Finish Exam and window close request cooperative shutdown. The worker stops/joins
the keyboard listener, closes MediaPipe, releases the camera, completes session
writes, and exits before the report/window destruction. No QThread is
force-terminated. New Session creates fresh engines.

## Technology Stack

| Component | Version / role |
| --- | --- |
| Python | 3.12; also compatible with 3.11 |
| PySide6 | 6.11.2, desktop widgets/QThread |
| Ultralytics | 8.3.221, pretrained YOLO11n COCO |
| PyTorch / torchvision | 2.8.0 / 0.23.0, CPU builds |
| OpenCV | opencv-python and opencv-contrib-python 4.12.0.88 |
| MediaPipe | 0.10.32, Tasks Face Landmarker CPU delegate |
| NumPy | 2.2.6 |
| Windows security | Standard-library ctypes/Win32 APIs |
| Persistence | Local JSON/JPEG; no database |

MediaPipe and Ultralytics declare OpenCV distributions overlapping in the `cv2`
namespace. Both are pinned to the same version. Do not independently
upgrade/uninstall either, or mix headless variants into this environment. If
repairing OpenCV, uninstall both together and reinstall the matching pins.
The tracker uses `mp.tasks`, rather than legacy `mp.solutions.face_mesh`.

## Installation

Open PowerShell in the project directory. For an existing environment:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

For a fresh Python 3.12 installation:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install torch==2.8.0 torchvision==0.23.0 --index-url https://download.pytorch.org/whl/cpu
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Packages download during setup. No activation is needed with explicit executable
paths. Do not install `opencv-python-headless`: the CLI needs desktop window support.

## Model setup

Download or copy these official pretrained files once:

| Local file | Official model |
| --- | --- |
| `models/yolo11n.pt` | [YOLO11n COCO weights](https://github.com/ultralytics/assets/releases/download/v8.3.0/yolo11n.pt) |
| `models/face_landmarker.task` | [MediaPipe Face Landmarker](https://storage.googleapis.com/mediapipe-models/face_landmarker/face_landmarker/float16/1/face_landmarker.task) |

The application requires local assets; it does not train or download models.
YOLO returns only COCO `person` (ID 0) and `cell phone` (ID 67).
Ultralytics connectivity checks, dependency installation, and telemetry/HUB
synchronization are disabled before inference.

## How to run desktop app

```powershell
.\.venv\Scripts\python.exe src\desktop.py
```

Setup accepts student/exam names and a camera. RECHECK SYSTEM probes OpenCV camera
indices 0–4 in a background worker, validates frames, and releases each probe.
Camera labels use actual indices. Model readiness is preflight; camera and YOLO
must initialize before the exam timer starts.

Exam displays annotated video, CV states, FPS, prominent SESSION RISK, persistent
warnings, and the latest 80 timeline events. This limit does not discard history.
Runtime indicators cover **CAMERA, YOLO, FACE TRACKING, SECURITY, EVIDENCE,
SESSION**, with readiness/activity/unavailability/error details. Technical
failures do not create student violations. Face/security failures degrade
independently; fatal camera/YOLO failure completes a partial session with an error.

FINISH EXAM, or **Q on the Exam page**, opens SESSION REPORT after cleanup.
The report contains student/exam, start/end/duration, final risk/level, all event
counts, total/critical/high/medium totals, full timeline, and evidence. Timeline
items show timestamp, readable event label, severity, risk delta, and message.
Evidence cards add confidence when available; opening a card shows a larger image.
The complete chronological timeline uses a Qt model rather than building one
widget per event. Evidence decodes eight thumbnails per page with PREVIOUS/NEXT
controls; obstruction cards identify cached-image age. Missing/corrupt images
display an explanation without crashing. Security events have no webcam image.
INFO break transitions are included in total events and counted separately.
NEW SESSION resets risk/history and retains saved sessions.

The dark layout targets 1280x800 and 1366x768. Compact warnings overlay the bottom
of the dominant webcam preview, leaving most video visible; long report content
scrolls.

Optional local overrides apply to both entry points:

```powershell
.\.venv\Scripts\python.exe src\desktop.py --imgsz 640 --conf 0.35 --threads 4 --head-evidence
.\.venv\Scripts\python.exe src\desktop.py --phone-conf 0.60 --secondary-person-min-area 0.025 --secondary-person-min-width 0.05 --secondary-person-min-height 0.15
```

## How to run CLI

```powershell
.\.venv\Scripts\python.exe src\main.py
```

Focus OpenCV and press **Q** to exit. Window close/Ctrl+C also release resources.
The CLI retains its CV/event/risk/security behavior and shared
`sessions/current/evidence/`. Breaks, obstruction sampling, full report, and
per-session JSON persistence are desktop features.

```powershell
.\.venv\Scripts\python.exe src\main.py --imgsz 640 --conf 0.35 --threads 4
.\.venv\Scripts\python.exe src\main.py --weights C:\models\yolo11n.pt
.\.venv\Scripts\python.exe src\main.py --face-model C:\models\face_landmarker.task
```

## How monitoring works

Each condition follows **inactive -> tracking -> emitted/active -> inactive**.
Duration starts on the first true sample. One false sample resets tracking; no
interruption grace period is enabled. A sustained episode emits once even after
cooldown expires. A later episode must satisfy threshold and cooldown, measured
from the last emission. Timing is monotonic; report timestamps are separate.

Phone application confidence defaults to **0.55**. Weaker boxes are hidden and
cannot affect events/risk/evidence. Persistence remains 0.7 seconds. The effective
floor is the higher of YOLO's `--conf` and application confidence.

Raw person boxes/counts remain displayed. MULTIPLE_PERSONS qualification requires
secondary confidence **>=0.60**, visible area >=**2%**, width >=**5%**, height
>=**15%** of the frame. The largest visible person box is exempt from secondary
geometry checks; this is a size heuristic, not identity tracking. Coordinates
are clipped before checks. Qualified second-person presence requires **3 seconds**,
or **1 second of continuous joint corroboration** with two MediaPipe faces.
A real second person can qualify without a visible face. Thresholds remain
centralized in configuration dataclasses.

Face states are FACE_DETECTED, NO_FACE, MULTIPLE_FACES, UNAVAILABLE. The tracker
returns at most two faces. Head orientation uses the transform's forward axis:
horizontal threshold 20 degrees, vertical 15 degrees, validity limit 75 degrees.
Directions refer to the subject's own left/right in an **unmirrored** image.
CENTER/UNKNOWN never emit look events; no single unambiguous face means UNKNOWN.
This estimates head pose, not eye gaze; MediaPipe thresholds are unchanged.

The live head badge shows state and deviation duration against the event
threshold. Regression tests cover all six states through FaceTracker -> worker
-> UI, and timed events -> timeline/banner/risk. No dropped head signal was
reproduced in the earlier reliability pass; natural pose/lighting needs a live check.

Three seconds of valid NO_FACE tracking displays **WARNING / STUDENT LEFT CAMERA
VIEW** until a face returns. The warning can qualify during cooldown without a
duplicate event. An unavailable tracker never causes an absence violation.

Raw camera quality is sampled at 160x120 grayscale before overlays. Default
unusable condition: variance <=4, or mean brightness <=12 **and** variance <=64
(8-bit intensity units). Darkness alone is insufficient. Two continuous seconds
qualify CAMERA_OBSTRUCTED and **WARNING / CAMERA VIEW OBSTRUCTED**. One usable
frame clears the warning. Obstruction suppresses FACE_MISSING tracking so an
unusable view is not also counted as absence. The heuristic cannot establish intent.

## Event types

| Event | Continuous duration | Cooldown | Severity |
| --- | --- | --- | --- |
| PHONE_DETECTED | 0.7 s | 10 s | CRITICAL |
| MULTIPLE_PERSONS | 1 s jointly corroborated; otherwise 3 s | 10 s | CRITICAL |
| MULTIPLE_FACES | 1 s | 10 s | CRITICAL |
| FACE_MISSING | 3 s | 10 s | HIGH |
| CAMERA_OBSTRUCTED | 2 s | 10 s | HIGH |
| LOOK_LEFT / LOOK_RIGHT / LOOK_UP / LOOK_DOWN | 4 s each | 8 s each | MEDIUM |
| ALT_TAB_ATTEMPT / WINDOW_FOCUS_LOST / PRINTSCREEN_ATTEMPT | input/focus transition | 3 s each | HIGH |
| COPY_ATTEMPT / PASTE_ATTEMPT / ESCAPE_ATTEMPT | input transition | 2 s each | MEDIUM |
| AUTHORIZED_BREAK_STARTED / ENDED / EXPIRED | authorized transition | — | INFO |

Events contain type, timestamp, severity, duration, message, optional confidence/
metadata, `risk_delta`, and `evidence_path`. Console output includes applied risk
and current session score.

## Risk scoring

Only emitted events change score. `RiskConfig` centralizes first/repeat weights;
counts are per type. Applied `risk_delta` reflects capping, including zero at 100.

| Event | First | Repeat |
| --- | --- | --- |
| PHONE_DETECTED | +30 | +10 |
| MULTIPLE_PERSONS | +30 | +15 |
| MULTIPLE_FACES | +25 | +10 |
| FACE_MISSING | +15 | +8 |
| CAMERA_OBSTRUCTED | +20 | +10 |
| LOOK_LEFT / LOOK_RIGHT | +8 | +8 |
| LOOK_UP | +5 | +5 |
| LOOK_DOWN | +10 | +10 |
| ALT_TAB_ATTEMPT / WINDOW_FOCUS_LOST / PRINTSCREEN_ATTEMPT | +15 | +8 |
| COPY_ATTEMPT / PASTE_ATTEMPT / ESCAPE_ATTEMPT | +5 | +3 |
| Authorized break transitions | 0 | 0 |

Levels: **LOW 0–20**, **MODERATE 21–45**, **HIGH 46–70**, **CRITICAL 71–100**.
Score stays within 0–100 and does not decay. It is a review aid, not a calibrated
cheating probability. Different events from one action (Alt+Tab and focus loss,
for example) can each contribute.

## Evidence

Each emitted PHONE_DETECTED, MULTIPLE_PERSONS, MULTIPLE_FACES, FACE_MISSING, or
CAMERA_OBSTRUCTED attempts at most one JPEG. Head snapshots are disabled by
default; enable `--head-evidence`. Security/break events never capture webcam
evidence. No-event frames do not encode/write JPEGs.

Images include boxes/confidences and the permanent CV footer, without temporary
event/risk/warning banners. Obstruction prefers the latest usable annotated
frame within **10 seconds**; otherwise it attempts the current frame. Metadata
records source/age and quality statistics. One usable frame is cached.
Saving failure retains the event/risk with `evidence_path=None`, logs the error,
and exposes a technical EVIDENCE status.

Desktop sessions have separate directories:

```text
sessions/
  20261008_143200_Demo_Student/
    session.json
    events.json
    evidence/
      PHONE_DETECTED_20261008_143215_481.jpg
```

Student names are sanitized; folder collisions add suffixes. JPEG names include
event type/time to milliseconds and use exclusive creation with collision
suffixes. NEW SESSION never overwrites/deletes completed evidence. CLI retains
`sessions/current/evidence/`.

`session.json` stores student, exam, start/end, duration, final risk/level, counts,
and session errors. `events.json` is the complete normalized event array,
including metadata, risk deltas, and evidence paths. Managed paths are relative
to the session, such as `evidence/PHONE_DETECTED_...jpg`. JSON writes use temporary
files, flush/fsync, and atomic replacement. `events.json` is published before
`session.json`, the completion marker. Technical storage failures preserve the
in-memory report. There is no database, PDF/export, remote synchronization, or
saved-session browser.

## Authorized break

AUTHORIZE BREAK opens an inline instructor panel with a masked PIN. Duration
choices appear after verification; the worker revalidates PIN/duration before
accepting requests. Centralized demo PIN: **1234**. Override before launch:

```powershell
$env:AI_EXAM_TEACHER_PIN = "your-teacher-pin"
.\.venv\Scripts\python.exe src\desktop.py
```

`BreakConfig.teacher_pin` and `durations_seconds` also allow local configuration.
PINs are omitted from events/logs and serialized session configuration.

Choose **1, 3, or 5 minutes**. **AUTHORIZED BREAK / MM:SS remaining** stays visible.
FACE_MISSING tracking, violation, warning, risk, and absence evidence are
suppressed. Security, phones, and real obstruction stay active. Normal absence
with a usable room image does not qualify as obstruction.

END BREAK ends early; expiration ends automatically. Both resume fresh absence
timing with the configured threshold and existing cooldown. Break transitions
are INFO/zero risk/no evidence. Existing violations remain. Monotonic deadlines
are sampled per processed frame. This is a trusted-machine hackathon PIN, not
account authentication or tamper protection.

## Security monitoring

Windows detection covers **Alt+Tab, Ctrl+C, Ctrl+V, PrintScreen, Escape**, and
protected-window focus loss. Desktop passes its native HWND; CLI locates its own
OpenCV window. Focus comparison validates process ownership and runs per frame;
a very short switch away/back can be missed.

An isolated `WH_KEYBOARD_LL` listener tracks selected key transitions, ignores
held-key repeats, and queues inputs. It records no typed text, clipboard contents,
screenshots, or unrelated key history. Detection is session-wide by default;
`detect_only_when_focused=True` restricts it. Metadata identifies foreground
focus and injected inputs.

**Default behavior blocks nothing.** `SecurityConfig.blocked_events` can
optionally suppress Ctrl+C, Ctrl+V, PrintScreen, or bare Escape while the protected
window is foreground. BLOCKED metadata is used only for actual callback
suppression. Alt+Tab/focus loss and modified Escape remain passed through.
This does not prevent alternate clipboard/screenshot methods or lock the OS.
Ctrl+Alt+Del, Task Manager, policies, registry, and kernel hooks are outside scope.

API/hook failures warn and continue CV; partial availability is shown. Security
is unavailable outside Windows. Stop wakes the listener, removes the hook, joins
the thread, and clears inputs. No listener remains after normal cleanup.
Real shortcut delivery/suppression requires manual Windows verification;
automated tests fake native calls.

## Privacy/local processing

Inference, scoring, evidence, and JSON stay local. There is no application
networking, cloud upload, database, login, or remote monitoring service.
Package/model downloads are installation steps. Evidence contains webcam images
and student/exam metadata; instructors should control access and retention.
Files are not encrypted and have no automatic deletion policy. `.gitignore`
excludes sessions/evidence, logs, model weights, virtual environments, and IDE files.

## Known limitations

- Detection can miss/misclassify occluded objects, profiles, and poorly lit faces.
  Filtering reduces brief/weak false positives, not identity verification.
  Persistent high-confidence false detections can qualify.
- Head orientation is not eye gaze. Natural head motion and multiple-person
  scenes need validation under intended camera/lighting conditions.
- Textured covers can evade the obstruction heuristic; blank scenes can be
  flagged. The heuristic cannot establish intent.
- Risk is a configurable review heuristic. No perfect cheating detection or
  secure operating-system lockdown is claimed.
- Focus is sampled at CV FPS. Mouse/menu clipboard, Win+Shift+S, other capture
  tools, secure desktops, and alternate shortcuts are outside scope. Windows
  can remove a slow keyboard hook.
- CPU FPS depends on hardware/scene/driver. Defaults are FP32 CPU, image size
  416, OpenCV one thread, up to four PyTorch threads. Larger inference images
  may improve small-object visibility at a speed cost. JPEG/JSON writes add
  occasional latency; no sustained FPS guarantee is made.
- Native camera/model calls cannot be interrupted mid-call. A stuck driver
  can delay cooperative shutdown until it returns.
- Local JSON is not a transactional database. Storage errors preserve the
  in-memory report; failed desktop folder creation does not fall back to shared
  current-session evidence.
- PIN authorization is an MVP. There is no login, tamper resistance,
  saved-session browser, PDF export, or OS-policy enforcement.

If the camera fails, close other camera applications and check Windows
**Settings > Privacy & security > Camera** desktop access. Capture tries
DirectShow, Media Foundation, then the default backend and validates a frame.

## Testing

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -m compileall -q src tests
.\.venv\Scripts\python.exe -m pip check
.\.venv\Scripts\python.exe src\desktop.py --help
.\.venv\Scripts\python.exe src\main.py --help
```

Tests use controlled time, synthetic frames, temporary directories, and fake
camera/model/security adapters. They do not require webcams, wait for real
duration thresholds, press OS keys, or install native hooks. Coverage includes
head states, thresholds/cooldowns, filtering, evidence failures, risk bounds,
security/cleanup, breaks, warnings, report totals/timeline/viewer, JSON session
isolation, actual QThread lifecycle, close cleanup, and New Session reset.

All **380 tests pass** on Python **3.12.0**, preserving the prior 320-test
monitoring baseline and adding 60 product/report/storage regressions.
Compilation of src/tests, both entry-point help commands, and `pip check` pass.
Fusion-style offscreen renders were visually checked at 1280x800 and 1366x768,
including the dark full timeline and a larger 1020x720 evidence dialog.

Offscreen layout checks cover 1280x800 and 1366x768 with synthetic data. A real
webcam smoke opened camera 0 for 20 frames at 640x480 and initialized CPU
YOLO/MediaPipe. It observed NO_FACE/UNKNOWN and an unusable view; no valid
face/head movement or shortcuts were verified. The inference-only sample
measured **18.59 FPS** after five warmup frames, excluding capture/display/GUI;
this is not desktop FPS.

Manual acceptance: start a desktop session, verify natural head states and hold
deviations past four seconds, leave/return after three seconds, cover/uncover
after two seconds, authorize/end/expire a break, and check shortcut/focus events.
Finish Exam, review timeline/thumbnails/larger images and both JSON files, then
start a second session and confirm prior evidence remains. Close during
initialization/monitoring and verify camera/listener cleanup. These live
scenarios remain manual checks; synthetic tests cannot establish detection accuracy.

## Project structure

```text
src/
  desktop.py                    # PySide6 entry point
  main.py                       # Preserved OpenCV-only entry point
  vision/
    yolo_detector.py
    face_tracker.py
    detection_filters.py
    camera_quality.py
  monitoring/
    event_engine.py
    risk_engine.py
    break_manager.py
  evidence/
    evidence_manager.py
  security/
    security_monitor.py
  session_storage/
    session_store.py
  ui/
    main_window.py
    setup_page.py
    exam_page.py
    report_page.py
    evidence_viewer.py
    vision_worker.py
    session.py
    theme.py
tests/                          # Unit/integration/offscreen UI tests
models/                         # Required local assets; weights ignored
sessions/                       # Generated session JSON/evidence; ignored
requirements.txt
README.md
.gitignore
```

References: [Ultralytics YOLO11](https://docs.ultralytics.com/models/yolo11/),
[prediction arguments](https://docs.ultralytics.com/modes/predict/),
[MediaPipe Face Landmarker](https://ai.google.dev/edge/mediapipe/solutions/vision/face_landmarker/python),
[Qt QThread](https://doc.qt.io/qtforpython-6/PySide6/QtCore/QThread.html),
[Windows keyboard hooks](https://learn.microsoft.com/en-us/windows/win32/winmsg/lowlevelkeyboardproc).
