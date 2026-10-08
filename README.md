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
- Conservative phone tracking with a short, configurable occlusion grace period.
- Persistent face-missing and camera-obstruction warnings; live head direction and duration.
- Event-based session risk from 0 to 100.
- Instructor-PIN-authorized breaks with a visible countdown.
- Windows focus-loss and selected keyboard-shortcut detection.
- Built-in local exams with single/multiple choice, written answers, navigation,
  confirmed submission, optional time limits, and separate academic grading.
- Required maximized/fullscreen student sessions with instructor-PIN exit and
  best-effort focus recovery.
- Student completion screen; PIN-protected Teacher Review with full reports,
  complete timelines, evidence thumbnails and a larger image viewer.
- Optional AI Proctor Assistant with static risk expressions, supportive prompts,
  draggable safe-area placement, and saved visibility/message preferences.
- Separate local JSON/evidence folders; New Session preserves previous completed sessions.
- Camera/model preflight, clear module statuses, background CV, and cooperative cleanup.
- English, Russian, and Kazakh presentation with a persisted Setup language selector.
- A centralized light academic theme with readable status and warning colors.

Команда: Nurbik
Капитан команды :Бектасов Нұрбақ Нұрғалиұлы
Сокомнадники: Усербай Даниал Медетұлы, Жаныбек Алихан Жаныбекұлы, Аринов Рамазан Болатович , Әбдіқасым Зейін Әсетұлы.

## Architecture

```text
Webcam -> YOLO11n + MediaPipe -> filtering + phone tracking -> Event Engine
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
                                         Completed Session JSON
                                           /              \
                              Student completion       SessionRepository
                                                            |
                                              PIN -> Teacher Review / Report
```

CV, Event Engine, Risk Engine, EvidenceManager, and security remain separate
modules. `VisionWorker` owns capture, inference, engines, evidence, and security
polling in a `QThread`. A locked mailbox coalesces display frames while retaining
every pending event. Detached `QImage` data crosses threads; widgets and
`QPixmap` update only on the UI thread.

Confirmed submission, timer expiry, authorized window close, and external
application shutdown request cooperative cleanup. The worker stops/joins
the keyboard listener, closes MediaPipe, releases the camera, completes session
writes, and exits before the report/window destruction. No QThread is
force-terminated. New Session creates fresh engines.

`SecureWindowGuard` handles only window recovery; native hooks remain isolated in
`SecurityMonitor`. Application focus/Escape and editor paste attempts enter the
same normalized security-event queue and cooldown flow. `ReviewLoadWorker` reads
completed sessions in a separate QThread without initializing camera/models/hooks.
Review replies are checked against the current PIN authorization epoch so a late
reply cannot reopen instructor data after logout or reauthorization.

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
The desktop imports the optional MediaPipe runtime before importing Qt. This
avoids a Python 3.12/PySide6 inspection error in virtual `six.moves` modules
(`_SixMetaPathImporter` has no `_path`). Model/task creation still runs in
VisionWorker. Programmatic launchers should call
`prepare_face_tracking_runtime()` before importing PySide6/UI modules.

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

Setup accepts student/session names, a local exam, a camera, and maximized/fullscreen
window mode. The official desktop launcher requires a secure session; WINDOWED
is disabled for students.
Demo Exam is selected initially; LOAD EXAM JSON validates another local definition
and displays a clear error while retaining the previous valid selection if loading
fails. RECHECK SYSTEM probes OpenCV camera
indices 0–4 in a background worker, validates frames, and releases each probe.
Camera labels use actual indices. Model readiness is preflight; camera and YOLO
must initialize before the exam timer starts.

Exam gives approximately **65%** of the main width to questions and answer controls,
with approximately **35%** for monitoring. Question/option text and navigation are
larger. The right column puts the annotated webcam above compact Face, Persons,
Phone, Head, Camera, Security, and Risk diagnostics. Previous/Next and
numbered navigation retain answers and show answered state. Longer exams use a
compact question selector. The header includes the exam, timer, and Secure Session
indicator. FPS and persistent warnings remain visible. The latest 80 timeline
events live in a collapsible panel, initially collapsed during secure exams.
This display limit does not discard history.
Runtime indicators cover **CAMERA, YOLO, FACE TRACKING, SECURITY, EVIDENCE,
SESSION**, with readiness/activity/unavailability/error details. Technical
failures do not create student violations. Face/security failures degrade
independently; fatal camera/YOLO failure completes a partial session with an error.

**SUBMIT EXAM** opens an inline confirmation: “Submit exam? You will not be able
to change your answers.” CONFIRM SUBMISSION freezes answers, sends an immutable
snapshot to the monitoring worker, and opens the student **EXAM SUBMITTED** page after cleanup and
local writes. A time limit shows TIME REMAINING and automatically submits at
expiry. Untimed exams show SESSION TIME. Both timers start only when camera/YOLO
initialization completes; authorized breaks keep elapsed exam time running.

The student completion page shows only submission metadata and save status. It
shows no academic score, risk, violations, timeline, security events, or evidence.
**TEACHER REVIEW** opens a masked PIN challenge; instructors can also open it
from Setup. Failed storage shows a generic request to contact the instructor,
without exposing report data. Interrupted attempts show **SESSION ENDED**.

The instructor report separates **EXAM RESULT** (academic score, percentage, counts, manual
review, submission time) from **PROCTORING RESULT** (risk/level). Choice answers
are graded automatically; written answers remain available in the Answers tab
for manual review. The report also contains student/exam, start/end/duration, all event
counts, total/critical/high/medium totals, full timeline, and evidence. Timeline
items show timestamp, readable event label, severity, risk delta, and message.
Evidence cards add confidence when available; opening a card shows a larger image.
The complete chronological timeline uses a Qt model rather than building one
widget per event. Evidence decodes eight thumbnails per page with PREVIOUS/NEXT
controls; obstruction cards identify cached-image age. Missing/corrupt images
display an explanation without crashing. Security events have no webcam image.
INFO break transitions are included in total events and counted separately.
NEW SESSION resets risk/history and retains saved sessions. Opening an instructor
report never changes its recorded score, events, answers, or files.

Authorized window close, fatal camera/YOLO failure, external application shutdown, and the
legacy **Q** exit save the available answers as an **interrupted**, provisional
attempt. Q is disabled while typing a written answer, entering an instructor PIN,
or running secure window mode, so normal input cannot accidentally finish an exam.
During a secure student session, ordinary close requests are ignored and Escape
cannot exit the exam. Use SUBMIT EXAM for a confirmed submission, or have an
instructor authorize **EXIT SECURE MODE** before using normal exit controls.

The question-first light layout targets **1280x800, 1366x768, and 1920x1080**.
Warnings overlay the bottom of the right-column webcam, leaving most video
visible; long question/report content scrolls.

## AI Proctor Assistant

Setup shows a calm welcome beside the existing controls. The Exam page shows a
small, static assistant in a separate lane below the question controls. It is a UX layer:
it reads current risk, emitted event types, and authorized-break state without
changing events, risk, evidence, or monitoring. It has no animation loops,
network calls, or generated chat responses.

| Current risk | Visual state |
| --- | --- |
| 0–20 | CALM |
| 21–45 | NEUTRAL |
| 46–70 | ALERT |
| 71–85 | SERIOUS |
| 86–100 | CRITICAL |

Important emitted events temporarily show short fixed prompts: put the phone
away, return to camera view, clear the camera, or return to the exam window.
Break prompts use an informational tone. The image continues to follow risk
during an override or break. Event text lasts **4 seconds** before returning to
the current generic state message; rapid events are coalesced with a **0.75-second**
debounce. A single-shot timer handles deadlines; there is no periodic popup or
animation. Event messages use a type whitelist, never report metadata, evidence
paths, arbitrary event text, or teacher-only results.

Use **ASSISTANT OPTIONS** on Setup or Exam to toggle **Show Assistant** and
**Show Assistant Messages** independently. The options expand inline in the
reserved area, preserving the protected window's focus. Defaults show both. Hiding the whole
assistant collapses its lane while keeping the options control available; risk
updates, breaks, and new sessions never force it to reappear. Drag the portrait
or bubble to reposition both together. Dragging is bounded to the reserved lane,
so the assistant cannot cover questions, navigation, webcam warnings, or critical
monitoring indicators. Setup allows more vertical movement; the compact Exam
lane primarily allows horizontal movement. The webcam remains in its existing
layout; camera dragging is deferred.

`AssistantConfig` centralizes messages, timing, and PNG paths under
`assets/assistant/`: `calm.png`, `neutral.png`, `warning.png` (ALERT),
`serious.png`, and `critical.png`. Transparent PNGs can be added later without
page changes. Missing/corrupt images use a static painted robot placeholder.
Restart the application after adding/replacing PNGs to reload the full asset set.
`AssistantModel` provides deterministic state/message timing; `AssistantDock`
and `AssistantWidget` provide reusable page-owned controls, rendering, and drag
behavior. They are child widgets, preserving the protected exam window handle.

Visibility/message choices are shared between Setup and Exam. Positions are
saved separately per page as normalized coordinates and clamped after resizing.
Preferences use a small JSON file at **`%LOCALAPPDATA%/AIExamGuard/assistant.json`**
on Windows. Defaults are loaded without creating a file; explicit toggles or
dragging save it. Invalid/missing preferences fall back safely, and save errors
do not affect monitoring; failed saves keep the current user's choices in memory
for this application run. Persistence across restart requires a writable settings
directory. Tests inject temporary settings paths. The assistant
is omitted from student completion and Teacher Review to preserve report privacy
and keep instructor analysis focused. Hide it whenever it distracts from the exam.

Optional local overrides apply to both entry points:

```powershell
.\.venv\Scripts\python.exe src\desktop.py --imgsz 640 --conf 0.35 --threads 4 --head-evidence
.\.venv\Scripts\python.exe src\desktop.py --phone-conf 0.60 --secondary-person-min-area 0.025 --secondary-person-min-width 0.05 --secondary-person-min-height 0.15
.\.venv\Scripts\python.exe src\desktop.py --phone-occlusion-grace 0.60
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

## Local exam JSON and grading

The bundled `data/demo_exam.json` has nine neutral technical questions: seven
choice questions and two written answers. Exams are UTF-8 JSON with unique
question IDs and option strings. Choice answer keys reference the exact option
text. A minimal exam using all supported question types is:

```json
{
  "id": "local-practice-v1",
  "title": "Local Practice Exam",
  "time_limit_seconds": 600,
  "questions": [
    {
      "id": "q1",
      "type": "SINGLE_CHOICE",
      "text": "Which component executes instructions?",
      "options": ["CPU", "Monitor"],
      "correct_answer": "CPU"
    },
    {
      "id": "q2",
      "type": "MULTIPLE_CHOICE",
      "text": "Select the input devices.",
      "options": ["Keyboard", "Mouse", "Speaker"],
      "correct_answer": ["Keyboard", "Mouse"]
    },
    {
      "id": "q3",
      "type": "TEXT",
      "text": "Explain why backups are useful."
    }
  ]
}
```

Omit `time_limit_seconds` or set it to `null` for an untimed exam. Otherwise it
must be a positive integer number of seconds. Invalid types, duplicate IDs/JSON
fields/options, unknown answer keys/fields, missing data, unreadable files, and
invalid encoding are rejected. Definitions are limited to 5 MiB and 1,000
questions. TEXT questions have no automatic answer key or options.

Each SINGLE_CHOICE or MULTIPLE_CHOICE question is worth one point. Multiple choice
requires the exact set of correct options, with no partial credit. Unanswered
choice questions earn zero; answered incorrect and unanswered counts remain
separate. Written answers are stored verbatim and excluded from the automatic
score denominator and percentage. All-text exams display pending manual review
instead of a misleading zero percent. Academic scores never alter proctoring risk.

## Language and visual preferences

Setup's **Language** selector offers **English / Русский / Қазақша** (`en`, `ru`,
`kk`). Switching updates the existing pages, statuses, warnings, assistant,
navigation, break authorization, completion, instructor reports, and evidence
viewer without restarting the application. The choice is saved with the existing
assistant preferences at **`%LOCALAPPDATA%/AIExamGuard/assistant.json`**; the
existing home-directory fallback applies when LOCALAPPDATA is unavailable.
Older files default to English. A settings-write failure retains the chosen
language for the current application and logs the error.

`src/i18n/translations.py` provides the central language service and formatting
rules, with common/student/report catalog fragments. Every key has EN/RU/KK
text and matching placeholders. `ui/localized_widgets.py` retains canonical
presentation sources and updates them through weak language observers. Language
changes do not replay monitoring events, advance assistant deadlines, reset
answers, or decode evidence again. Event enums, stored records, risk logic,
student names, loaded exam content/answers, file paths, and technical exception
details remain unchanged; localized guidance surrounds technical diagnostics.

`ui/theme.py` centralizes the near-white background, white/light-gray cards, dark
text, blue controls, green readiness, amber warnings, red critical states, and
reusable panel/navigation/assistant styles. The camera keeps a dark neutral
frame to preserve image readability. Inference model, resolution, thresholds,
CPU settings, and QThread behavior are unchanged by this polish pass. The user's
reported live **approximately 12 FPS** is accepted; offscreen UI checks do not
measure live webcam performance.

## Secure exam window

The official desktop launcher defaults to **MAXIMIZED** and requires maximized
or fullscreen mode. The active student window is frameless. Ordinary close
requests and Q cannot finish a secure session, and Escape is accepted without
exiting. **EXIT SECURE MODE** requires the existing instructor PIN; a valid PIN
returns to normal window geometry while monitoring continues. The demo PIN is
**1234**, or use `AI_EXAM_TEACHER_PIN` as described below. Normal window chrome is
restored after monitoring stops so the native protected-window handle stays
stable throughout the exam. Trusted development launchers can explicitly use
`SecureWindowConfig(required=False)` with WINDOWED mode.

Focus loss is recorded before attempting recovery. Qt application-state changes
and the existing native foreground check share security cooldowns. Each loss
episode starts one bounded burst: **four attempts**, including the immediate
attempt, spaced **0.2 seconds** apart by a single-shot UI timer. Each attempt
shows the window, restores configured maximized/fullscreen mode, raises it, and
requests Qt activation. If Qt is insufficient, the isolated
`security/windows_focus.py` helper validates that the protected HWND belongs to
this process, then uses `ShowWindow`, `BringWindowToTop`, and
`SetForegroundWindow`. It verifies the actual foreground window.

Confirmed focus stops pending retries; exhausted attempts stay exhausted until
focus is regained. Repeated lost-focus frames do not renew the attempt budget or
postpone the timer. Instructor-authorized exit, submission, and shutdown cancel
recovery immediately. Recovery does not suppress security history, risk, or the
existing event cooldowns, and failures do not crash monitoring.

Windows foreground restrictions can still refuse activation, including while
another application owns input or a secure desktop is active. This is best-effort
recovery, not OS lockdown. There is no fake input, thread-input attachment,
process termination, registry/policy change, or secure-desktop interference. See
[Windows foreground activation restrictions](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-setforegroundwindow)
and [Qt's activateWindow limitations](https://doc.qt.io/qt-6/qwidget.html#activateWindow).

`SecureWindowConfig` centralizes required mode, focus recovery, retry interval,
attempt limit, native recovery enablement, and optional `suppressed_shortcuts`.
The default suppression set is empty. Trusted local
configuration can select existing supported COPY_ATTEMPT, PASTE_ATTEMPT,
PRINTSCREEN_ATTEMPT, and bare ESCAPE_ATTEMPT suppression, for example:

```python
from monitoring import EventType
from security.secure_window import SecureWindowConfig

secure_window = SecureWindowConfig(
    required=True,
    focus_recovery_retry_interval_seconds=0.2,
    focus_recovery_max_attempts=4,
    native_focus_recovery_enabled=True,
    suppressed_shortcuts=frozenset({EventType.PASTE_ATTEMPT}),
)
```

Assign this configuration to `SessionConfig.secure_window` in a trusted launcher.
Suppression uses the existing foreground-only Windows hook/editor behavior;
events still enter history and risk when suppression succeeds. Metadata reports
actual suppression, not an assumed OS block. Alt+Tab and focus loss remain
detect-only. Ctrl+Alt+Del, secure desktops, Task Manager, registry/policy changes,
kernel hooks, and externally terminating the process are outside this protection.

Written answers use a plain text editor. Default paste behavior is detect-only.
Configuring `SecurityConfig.blocked_events` with `PASTE_ATTEMPT` suppresses actual
Qt paste through Ctrl+V, Shift+Insert, and context-menu paste. Drag/drop is disabled.
The editor queues normalized paste attempts even if global hooks fail. Native
and local reports share one cooldown, so a single keyboard paste is not counted
twice. Accepted pending paste attempts are flushed into history/risk before
shutdown. In-app submission/PIN confirmations remain inline and preserve the
protected top-level window's focus.

## How monitoring works

Each condition follows **inactive -> tracking -> emitted/active -> inactive**.
Duration starts on the first true sample. One false sample resets ordinary
condition tracking. Phones have the explicit short tracking grace described
below. A sustained episode emits once even after
cooldown expires. A later episode must satisfy threshold and cooldown, measured
from the last emission. Timing is monotonic; report timestamps are separate.

Phone application confidence defaults to **0.55**. Weaker boxes are hidden and
cannot affect events/risk/evidence or establish/refresh a track. Persistence
remains **0.7 seconds of confident observed time**. The effective
floor is the higher of YOLO's `--conf` and application confidence.

`PhoneTracker` is a lightweight, single-phone box association layer over the
existing Ultralytics `predict()` results. It uses IoU or normalized center distance
with an area-change limit; it adds no inference pass or dependency. ByteTrack was
considered, but its additional tracker machinery was unnecessary for this local
single-object continuity requirement; this implementation does not claim to be
ByteTrack. [Ultralytics tracking documentation](https://docs.ultralytics.com/modes/track/)
describes that alternative.

Current phone state is **NONE**, **DETECTED**, or **TRACKED_OCCLUDED**. A confident
detection appears as DETECTED; at least **two matched confident observations**
are required before disappearance can retain a probable track. Temporary loss
keeps TRACKED_OCCLUDED for at most **0.6 seconds** from the last confident box,
then clears to NONE. Reappearance inside grace must match the retained geometry.
An unrelated confident box starts a fresh track/episode. Missing or weak detections
do not extend grace, count as observed time, or draw a predicted phone box.

A new PHONE_DETECTED event requires a current confident DETECTED observation,
the existing persistence threshold, and minimum tracking hits. Predicted state
alone never creates risk/evidence. Brief occlusion can retain an already-emitted
episode without another event. Full loss resets the episode while preserving
the Event Engine cooldown. Settings live in `PhoneDetectionConfig`: tracking
enablement, grace, minimum hits, association IoU, center distance, and area ratio.
Use `--phone-occlusion-grace` to override grace or `--no-phone-tracking` to retain
the original uninterrupted detection behavior.

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
The worker publishes this current condition on every frame; expiration of the
temporary event banner does not clear it. Tracker failures show their technical
reason on the Exam page and pause absence monitoring, while YOLO continues.

Raw camera quality is sampled at 160x120 grayscale before overlays. Default
unusable condition: variance <=4, or mean brightness <=12 **and** variance <=64
(8-bit intensity units). A configurable blur branch also identifies soft palm
or object covers: variance <=1600, denoised Laplacian variance <=8, and sharp
Sobel gradients (magnitude >=24) in <=2% of pixels. This branch can be disabled
with `CameraQualityConfig.blur_check_enabled`; its thresholds are centralized.
Sharp scene detail and higher contrast prevent this additional classification.
Darkness alone is insufficient. Two continuous seconds
qualify CAMERA_OBSTRUCTED and **WARNING / CAMERA VIEW OBSTRUCTED**. One usable
frame clears the warning. Confirmed obstruction suppresses FACE_MISSING tracking
so an unusable view is not also counted as absence. Short, unconfirmed quality
candidates do not reset continuous face absence. The heuristic cannot establish intent.

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
and session errors. Its nested `exam_result` stores exam ID/name/definition
reference, all question IDs/answers, answered status, grading status, academic
score/maximum/percentage, counts, start/submission times, duration, and submission
reason (`submitted`, `expired`, or `interrupted`). Answer checkpoints cross to
the worker as immutable copies during editing; orderly or technical shutdown
retains partial answers. No separate answers database is introduced.
`events.json` is the complete normalized event array,
including metadata, risk deltas, and evidence paths. Managed paths are relative
to the session, such as `evidence/PHONE_DETECTED_...jpg`. JSON writes use temporary
files, flush/fsync, and atomic replacement. `events.json` is published before
`session.json`, the completion marker. Technical storage failures preserve the
in-memory report. There is no database, PDF/export, or remote synchronization.

## Teacher Review and future transport

From Setup or student completion, select **TEACHER REVIEW**, enter the existing
Teacher PIN, and open a completed session. The list shows student, exam, local
date/time, duration, academic score where applicable, risk score, and level.
Text-only exams show **Manual review**; older sessions without academic results
show **—**. Select a session to open its full instructor report, including answers,
all security/break events, risk, timeline, and evidence. **BACK TO SESSIONS**
returns to the list. **LOCK TEACHER REVIEW** or leaving review revokes access, clears the
list, and closes open evidence dialogs. Returning requires PIN verification.
Teacher Review cannot be entered while a student exam is active.

`LocalSessionRepository` reads existing `session.json`, `events.json`, and evidence
without rewriting, regrading, or rescoring them. Listing/loading happens in
`ReviewLoadWorker` off the GUI thread. Incomplete, locked, corrupt, oversized, or
invalid session folders produce diagnostics without hiding other valid sessions.
Session IDs must identify direct child folders; traversal and escaping symlinks
are rejected. Evidence outside that session's managed evidence directory is
excluded. Missing/corrupt managed images retain the existing viewer explanation.
Centralized repository limits bound JSON sizes, event/question counts, and scan
entries. New Session does not alter earlier completed folders.

If the latest session could not be saved, authenticated review includes its
current in-memory result with a diagnostic. Review it before New Session or
closing the app: this fallback is not durable storage.

The `SessionRepository` protocol exposes `list_completed()` summaries and
`load(session_id)` returning the existing normalized `SessionResult`. The
controller/report page depend on that boundary rather than JSON paths. A future
teacher-PC/server transport can implement this interface and accept completed
session payloads plus managed evidence, preserving report rendering and recorded
scores. Transport authentication, uploads, acknowledgments, retries, and remote
retention would be separate work; no networking/cloud infrastructure is added.

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
OpenCV window. Native focus comparison validates process ownership and runs per
frame. The desktop also queues application deactivation before attempting secure
focus recovery, covering transitions between CV polls where Qt reports them.

An isolated `WH_KEYBOARD_LL` listener tracks selected key transitions, ignores
held-key repeats, and queues inputs. It records no typed text, clipboard contents,
screenshots, or unrelated key history. Detection is session-wide by default;
`detect_only_when_focused=True` restricts it. Metadata identifies foreground
focus and injected inputs.

**Native shortcut blocking is disabled by default.** The secure desktop still
refuses ordinary in-app close/Escape exits. `SecurityConfig.blocked_events` and
secure-mode `suppressed_shortcuts` can
optionally suppress Ctrl+C, Ctrl+V, PrintScreen, or bare Escape while the protected
window is foreground. BLOCKED metadata is used only for actual callback
suppression, or actual paste refusal inside the built-in editor. Alt+Tab/focus
loss and modified Escape remain passed through.
This does not prevent alternate clipboard/screenshot methods or lock the OS.
Ctrl+Alt+Del, Task Manager, policies, registry, and kernel hooks are outside scope.

Application focus/Escape and built-in editor paste fallbacks share native
cooldowns, event history, and risk. Accepted pending commands are flushed before
shutdown. They do not capture webcam evidence. Metadata distinguishes application
handling from native suppression.

API/hook failures warn and continue CV; partial availability is shown. Native security
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
- Phone continuity tracks one box by geometry. It cannot infer a previously
  unseen phone, recover indefinite occlusion, identify a phone uniquely, or
  reliably associate multiple phones/large sudden movements. Grace preserves a
  probable state; it does not improve the model's per-frame recall.
- Head orientation is not eye gaze. Natural head motion and multiple-person
  scenes need validation under intended camera/lighting conditions.
- Sharply textured covers can evade the obstruction heuristic; blank or severely
  out-of-focus scenes can be flagged. Soft textured palms/objects are covered by
  the added blur checks, but real lighting/cover combinations need calibration.
  The heuristic cannot establish intent.
- Risk is a configurable review heuristic. No perfect cheating detection or
  secure operating-system lockdown is claimed.
- Native focus is sampled at CV FPS with a desktop Qt deactivation fallback.
  Windows can refuse activation despite restore/raise requests. Clipboard paths outside the built-in editor,
  Win+Shift+S, other capture
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
- PIN authorization protects application navigation on a trusted local machine.
  It does not encrypt or prevent filesystem access to session JSON/images. There
  is no account login, tamper resistance, PDF export, or OS-policy enforcement.

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

Exam tests add JSON validation, all question types, answer restoration, exact-set
grading, immutable submission/checkpoints, fake-time expiry, confirmation,
separate academic/risk results, paste policy/cooldowns/shutdown flushing, secure
PIN exit, full JSON results, and actual QThread submission/save/cleanup.

All **932 tests pass** on Python **3.12.0**, preserving all **795** prior tests
and adding **137** final-polish regressions: 30 focus component/native, 17 focus
controller, 34 localization/lifecycle, 17 localized assistant, 17 localized report,
and 22 student preferences/theme checks. The prior assistant integration includes
116 regressions: 64 model/preferences, 36 widget, and 16 page/controller tests.
Assistant coverage includes all risk boundaries, timed
event overrides/debounce, late timers, authorized-break tone, hiding/messages,
safe dragging and interrupted mouse capture, preference reload/failure,
page transitions, secure HWND preservation, private completion/review, and cleanup.
The prior hardening regressions cover occlusion/grace, confidence,
event spam, secure activation/close/Escape/PIN/focus recovery, student privacy,
teacher authentication and late-reply rejection, real review QThread lifecycle,
correct archived reports/evidence, corrupt folders, and saved-session isolation.
Compilation of src/tests, both entry-point help commands, and `pip check` pass.
Fusion-style offscreen renders were visually checked at **1280x800, 1366x768,
and 1920x1080**, including the question-first layout, warning, completion, PIN,
and Teacher Review views. Automated evidence integration opens the actual
existing viewer on a synthetic saved image without modifying session files.
Assistant renders also cover Setup, Exam, critical messages, inline options, and
technical/obstruction warnings at these sizes. The legacy 1366x728 timeline and
break-warning regressions remain passing. No real camera/hooks run in these checks.

The final polish pass adds focus component/controller checks, EN/RU/KK catalog
and widget checks, assistant/report localization regressions, persisted-language
tests, raw-content/privacy checks, and light-theme contrast checks. There are
**456 complete translation keys**. Synthetic final UI QA covers all three
languages at the three sizes above, including persistent absence/obstruction,
tracker unavailability, PIN authorization, breaks, assistant options, teacher
pages, reports, and evidence. The longer Russian absence warning wraps inside
the camera panel. Real foreground recovery, physical camera behavior, Windows
DPI scaling, and native-language proofreading still need manual verification.
Language observers use weak references and constant-time token removal. This
avoids quadratic widget teardown during large page/test cleanup; the existing
QThread watchdog tests remain unchanged and pass. New UI fixtures explicitly
drain Qt deferred deletions. The final complete run took 51.4 seconds.

Files in the final polish pass:

- Created: `src/security/windows_focus.py`; `src/i18n/__init__.py`,
  `translations.py`, `common_translations.py`, `student_translations.py`,
  `report_translations.py`; `src/ui/localized_widgets.py`.
- Changed: `src/security/secure_window.py`; `src/ui/theme.py`,
  `assistant_model.py`, `assistant_widget.py`, `setup_page.py`, `exam_page.py`,
  `question_panel.py`, `main_window.py`, `completion_page.py`,
  `teacher_review_page.py`, `report_page.py`, `evidence_viewer.py`; this README.
- Added tests: `tests/test_focus_recovery.py`, `test_focus_controller.py`,
  `test_localization.py`, `test_localized_assistant.py`,
  `test_localized_reports.py`, `test_product_polish.py`.
- Generated QA scripts/screenshots live under ignored `sessions/current/`.
  No dependency or `.gitignore` change was needed.

The following checks predate this hardening pass. A real webcam smoke opened
camera 0 for 20 frames at 640x480 and initialized CPU
YOLO/MediaPipe. It observed NO_FACE/UNKNOWN and an unusable view; no valid
face/head movement or shortcuts were verified. The inference-only sample
measured **18.59 FPS** after five warmup frames, excluding capture/display/GUI;
this is historical inference-only data, not a new desktop benchmark.
The repaired startup order also passed a cold process with real local YOLO and
MediaPipe models, Qt and a QThread: 12 synthetic frames returned NO_FACE/UNKNOWN
with an available tracker and no error. No webcam/hooks were used in that check.
Hand/object-cover tests use synthetic frames; physical covers remain a live check.

This pass measured only the new tracker on synthetic boxes: median **0.0187 ms
per update**, across five runs of 20,000 updates. It excludes capture, YOLO,
MediaPipe, rendering, and disk writes, so it does not establish desktop FPS.
Only **YOLO11n** is present locally; **YOLO11s** is unavailable. No live/representative
phone-recall or n-versus-s CPU FPS comparison was completed, and the default
model remains YOLO11n. No confidence floor was lowered or model downloaded.

Manual acceptance: start a desktop session, verify natural head states and hold
deviations past four seconds, leave/return after three seconds, cover/uncover
after two seconds, authorize/end/expire a break, and check shortcut/focus events.
Submit Exam, verify the student completion screen, then authenticate Teacher
Review and inspect academic answers/score separately from risk, timeline,
thumbnails/larger images and both JSON files. Start a second session and confirm
prior evidence remains. Authorize exit before closing an active secure session,
and verify camera/listener cleanup. These live
scenarios remain manual checks; synthetic tests cannot establish detection accuracy.

Exam manual acceptance:

1. Start Demo Exam, answer all three question types, move Previous/Next and jump
   between questions; confirm choices and written text remain intact.
2. Watch webcam/status/risk/timeline while answering. Submit, cancel once, then
   confirm. Student completion must expose no report/risk/events/evidence. Open
   Teacher Review, reject a wrong PIN, verify a correct PIN, and open the right
   saved session. Review EXAM RESULT, PROCTORING RESULT, Answers, all events,
   evidence, and the nested `exam_result` in `session.json`. Return to the list,
   lock Teacher Review, and confirm details require PIN again.
3. Load a valid JSON with a short time limit and wait for automatic submission;
   then load invalid JSON and verify the clear error and previous selection.
4. Start maximized/fullscreen; try Escape, Q, Alt+F4, and ordinary close. They
   must not end the secure student session. Alt+Tab must record focus loss and
   attempt recovery; Windows may refuse activation. Reject a wrong instructor
   PIN and exit secure mode with the correct PIN while monitoring continues.
5. On Windows verify Ctrl+C/Ctrl+V/PrintScreen/focus events. With paste suppression
   configured, try Ctrl+V, Shift+Insert, and context-menu paste in a written answer.
6. Start a second session and verify fresh answers/risk and preserved old folders.
   After authorized exit, close during an exam and confirm partial answers plus
   camera/listener cleanup. Close during initialization and report loading too.
7. Show a real phone above the confidence floor, briefly cover it with a hand,
   and check DETECTED -> TRACKED_OCCLUDED -> DETECTED without duplicate events.
   Remove it beyond grace and verify NONE; chair/headrest below confidence must
   never establish a track. Check risk/evidence only change on emitted events.
8. Test natural face/head motion, absence/return, palm/object/shutter covers,
   and authorized breaks on the intended camera/lighting. Check the three target
   screen sizes under the intended Windows DPI scaling with a long exam.
9. Check the Setup greeting and five assistant expressions during an exam. Toggle
   messages alone, then hide/show the assistant; drag the image or bubble in its
   safe lane and restart to verify preferences. Check event prompts revert after
   four seconds, authorized breaks use informational text, and hiding stays in
   effect through updates/new sessions. The options are inline and should not
   produce security focus-loss events. Confirm completion/Teacher Review have no
   mascot and student completion still exposes no report details.

Real QThread tests use fake camera/model/security adapters. Physical occlusion,
native shortcuts/suppression, foreground recovery, and sustained live CPU FPS
remain manual Windows/webcam checks.

## Files in the window/report hardening pass

Created:

- `src/vision/phone_tracker.py`
- `src/security/secure_window.py`
- `src/session_storage/session_repository.py`
- `src/ui/completion_page.py`
- `src/ui/teacher_review_page.py`
- `src/ui/review_worker.py`
- `tests/test_phone_tracking.py`
- `tests/test_session_repository.py`
- `tests/test_hardened_ui.py`
- `tests/test_product_hardening.py`

Changed:

- `src/desktop.py`, `src/main.py`
- `src/monitoring/event_engine.py`
- `src/security/security_monitor.py`
- `src/ui/session.py`, `src/ui/vision_worker.py`, `src/ui/main_window.py`
- `src/ui/exam_page.py`, `src/ui/question_panel.py`, `src/ui/setup_page.py`,
  `src/ui/report_page.py`
- `README.md`

No dependency or `.gitignore` changes were necessary. Existing session output
and synthetic layout-check artifacts remain under ignored `sessions/`.

## Assistant files

Created:

- `src/ui/assistant_model.py`
- `src/ui/assistant_widget.py`
- `assets/assistant/README.md`
- `tests/test_assistant_model.py`
- `tests/test_assistant_widget.py`
- `tests/test_assistant_integration.py`

Changed:

- `src/ui/setup_page.py`
- `src/ui/exam_page.py`
- `src/ui/main_window.py`
- `README.md`

No monitoring/backend module or dependency changes were required. Synthetic
assistant layout-check screenshots are generated only under ignored `sessions/`.

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
    phone_tracker.py
  monitoring/
    event_engine.py
    risk_engine.py
    break_manager.py
  evidence/
    evidence_manager.py
  security/
    security_monitor.py
    secure_window.py
  session_storage/
    session_store.py
    session_repository.py
  exams/
    exam_model.py
  ui/
    main_window.py
    setup_page.py
    exam_page.py
    question_panel.py
    report_page.py
    assistant_model.py
    assistant_widget.py
    completion_page.py
    teacher_review_page.py
    review_worker.py
    evidence_viewer.py
    vision_worker.py
    session.py
    theme.py
tests/                          # Unit/integration/offscreen UI tests
data/demo_exam.json              # Bundled nine-question local exam
assets/assistant/               # Optional transparent PNGs; painted fallback
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
