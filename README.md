# Local webcam proctoring prototype

Python **3.12** (also compatible with 3.11), OpenCV webcam preview, and pretrained Ultralytics **YOLO11n**
COCO detection. Only `person` (COCO ID 0) and `cell phone` (ID 67) are returned.
Boxes show the class name and confidence.
Application filtering hides phone boxes below confidence 0.55. Person boxes
and raw counts remain visible; secondary geometry validation affects events only.
The overlay shows the current frame's
person count, phone detection status, measured preview FPS, MediaPipe face
presence/count, and basic head direction. Status text sits below the camera
image so it cannot cover YOLO bounding boxes or confidence labels.
An in-memory Event Engine qualifies sustained signals, prints new events to
the console, and briefly shows the latest event below the existing status text.
Important emitted events also save local annotated JPEG evidence snapshots.
A configurable Risk Engine accumulates emitted events into a 0–100 session
score and displays the score and level below the CV status area.
An isolated Windows security monitor detects focus loss and selected keyboard
shortcuts, contributing normalized events to the same history/risk/banner flow.
A PySide6 desktop shell, **AI Exam Guard**, adds setup, live exam monitoring,
and an in-memory completion summary. The OpenCV-only CLI is also preserved.

## Install on Windows

Keep the existing working Python 3.12 environment. Update its dependencies:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

For a fresh installation, install Python 3.12 and run these commands here.
The setup downloads packages; the application itself runs locally.

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install torch==2.8.0 torchvision==0.23.0 --index-url https://download.pytorch.org/whl/cpu
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

The first installation command selects the CPU PyTorch build. Do not substitute
`opencv-python-headless`: the preview requires OpenCV's desktop window support.
Environment activation is unnecessary with these explicit executable paths.

Download the official pretrained
[yolo11n.pt COCO weights](https://github.com/ultralytics/assets/releases/download/v8.3.0/yolo11n.pt)
once and save them as `models/yolo11n.pt`. You can also copy this file from
another computer for offline use. The application requires an existing local
file and never downloads weights or trains a model. Camera snapshots are saved
only for eligible emitted events, as described below.
Ultralytics connectivity checks, automatic dependency installation, and
telemetry/HUB synchronization are disabled before inference.

MediaPipe **0.10.32** uses the Tasks Face Landmarker API. Download the official
[face_landmarker.task model](https://storage.googleapis.com/mediapipe-models/face_landmarker/face_landmarker/float16/1/face_landmarker.task)
once to `models/face_landmarker.task`, or copy it from another computer. This
local model asset is required by the Tasks API; the application has no download
code and does not require a service. It uses an explicit CPU delegate.

MediaPipe 0.10.32 works with the existing NumPy 2.2.6; older MediaPipe versions
can require NumPy 1.x. MediaPipe also declares `opencv-contrib-python`, while
Ultralytics declares `opencv-python`. Both are pinned to **4.12.0.88** to avoid
an automatic OpenCV upgrade and mismatched shared `cv2` binaries. These packages
overlap in the `cv2` namespace: do not independently upgrade/uninstall either
one. If repairing OpenCV, uninstall both together and reinstall both matching
pins. This dependency overlap is a packaging limitation, not a Python 3.12
restriction. Avoid installing headless variants in the same environment.
This version exposes `mp.tasks`, not the legacy `mp.solutions.face_mesh` API;
the face tracker deliberately uses Tasks. No exact Python 3.11 check is added.

## Run the desktop application

From the project directory, launch:

```powershell
.\.venv\Scripts\python.exe src\desktop.py
```

The dark window targets 1280x800 and fits the available desktop on a laptop;
layout was also checked at 1366x728. Setup accepts a student name and exam name
(defaults: `Student`, `Practice Exam`) and selects a camera. A background
preflight opens OpenCV camera indices **0–4**, verifies a frame from each usable
camera, and releases every probe. Camera labels use their actual OpenCV indices
rather than potentially mismatched hardware names. Use **RECHECK SYSTEM** after
connecting a camera. Scanning does not run YOLO inference or install hooks.

Camera, AI Detection, Face Tracking and Security Monitor display component
statuses with explanatory tooltips. Setup model readiness means dependencies
and local model assets are present; actual initialization is checked on start.
Camera and YOLO must initialize before the exam page or session timer starts.
Initialization failure retains Setup with an error and releases the camera.
Missing MediaPipe or unavailable security monitoring are shown as unavailable,
while the rest of monitoring continues.

The Exam page displays annotated video with bounding boxes/confidences, current
face/person/phone/head state, FPS, security status, risk score and level, and a
scrollable timeline of the latest **80 events**. Full emitted history remains in
memory; limiting the timeline does not discard events or affect scoring.
Transient event banners use a Qt timer and do not block the UI. The separate red
**WARNING / STUDENT LEFT CAMERA VIEW** banner follows the duration-qualified
FACE_MISSING condition and clears on returned faces or an unavailable tracker.
Warnings can qualify during cooldown without emitting a repeated event.
The session clock uses monotonic elapsed time rather than counting timer ticks.

### Desktop monitoring reliability

Head direction now has a prominent live preview badge and colored sidebar value.
For LEFT/RIGHT/UP/DOWN the badge shows continuous duration and the configured
event threshold (currently 4 seconds). CENTER and UNKNOWN never emit head events.
Pose-matrix regression tests verify FaceTracker's summarization -> worker ->
desktop status -> Event Engine -> timeline/banner/risk for all six directions.
Code inspection found this data path already connected; the previous desktop
only exposed an understated sidebar value, while events required sustained
deviation. No dropped head-direction signal was reproduced. MediaPipe thresholds
and pose estimation remain unchanged. A real-camera UNKNOWN/neutral-only issue
still needs a live pose/lighting check; the tracker estimates head pose, not eyes.

`src/vision/camera_quality.py` analyzes raw frames before overlays using a small
160x120 grayscale sample. `CameraQualityConfig` centralizes all image thresholds.
A frame is unusable when variance is <=4, or when brightness is <=12 **and**
variance is <=64 (8-bit grayscale, variance in squared intensity units).
Darkness alone is insufficient; textured low-light scenes remain usable in the
synthetic regressions. No extra ML model or YOLO inference is added.
`EventEngineConfig.rules[CAMERA_OBSTRUCTED]` requires 2 continuous seconds,
with HIGH severity and a 10-second cooldown. One uninterrupted episode emits
once, even after cooldown. Risk defaults are first +20 / repeat +10. A persistent
red **WARNING / CAMERA VIEW OBSTRUCTED** overlay clears on the first usable frame.
Covered frames suppress FACE_MISSING tracking to avoid also accusing the student
of leaving when the camera view itself is unusable. Fresh absence timing starts
when a usable view returns. This image heuristic identifies unusable views;
it cannot establish intent and may miss a textured cover or flag a blank wall.

Obstruction evidence prefers one cached, detached, annotated usable frame from
the preceding 10 seconds (`last_usable_max_age_seconds`). If unavailable/stale,
the current frame is attempted. Event metadata records the frame source, age,
brightness and variance. Evidence failure retains the event, risk and warning.
The cache holds one image; encoding/writing still occurs only on emitted events.

**AUTHORIZE BREAK** opens an inline instructor panel in the protected window.
Enter the teacher PIN, verify it, then choose 1, 3, or 5 minutes and START BREAK.
The default demo PIN is **1234**. Configure it before desktop launch:

```powershell
$env:AI_EXAM_TEACHER_PIN = "your-teacher-pin"
.\.venv\Scripts\python.exe src\desktop.py
```

Alternatively supply `SessionConfig(breaks=BreakConfig(teacher_pin="..."))`;
`BreakConfig.durations_seconds` controls choices. The PIN is masked, omitted
from configuration repr/events/console, and cleared on cancel/start/shutdown.
Duration choices remain hidden until PIN verification. The worker independently
revalidates the PIN/duration via a bounded command queue before entering a break.
Invalid requests cannot alter monitoring. The inline panel avoids switching
native windows; Q is disabled only while entering authorization text, then restored.

`BreakManager` owns a monotonic deadline. During a break the green
**AUTHORIZED BREAK / MM:SS remaining** overlay stays visible, FACE_MISSING
tracking/warnings are reset and suppressed, and absence adds no risk/evidence.
Other checks, including real obstruction, phones, and security, stay active;
ordinary absence with a usable room image does not qualify as obstruction.
END BREAK ends early; the deadline automatically expires an unfinished break.
Both transitions resume fresh FACE_MISSING duration tracking while retaining
the existing event cooldown and historical risk. STARTED/ENDED/EXPIRED break
events enter the timeline/history with INFO severity, zero risk and no evidence.
Existing violations are retained. Timers/transitions are sampled on processed
frames, so slow inference can delay the visible update by one processing cycle.
This local demo PIN is an MVP authorization mechanism on a trusted machine;
it is not account authentication or tamper protection. Breaks and obstruction
sampling are desktop features; the OpenCV CLI and its existing behavior remain
available. No final report/export is implemented.

`VisionWorker` runs capture, existing YOLO/MediaPipe inference, Event Engine,
Risk Engine, evidence saving and security polling in a `QThread`. A locked
mailbox retains only the newest display frame and accumulates all pending
events, with at most one queued update notification. Frame updates include a
detached `QImage`, face/head state, counts, phone state, FPS, enriched events,
risk score/level, warning state and component statuses. `QPixmap` creation and
widget updates happen only on the UI thread. This follows Qt's
[QThread guidance](https://doc.qt.io/qtforpython-6/PySide6/QtCore/QThread.html) and
[QImage ownership API](https://doc.qt.io/qtforpython-6/PySide6/QtGui/QImage.html).

The renderer is reused once per frame. The desktop displays only its annotated
camera area and shows status information in the side panel. Evidence keeps the
existing annotated camera plus permanent CV footer, with no temporary banners
or risk footer. The same EvidenceManager rules, paths and failure handling apply;
security events still never create webcam snapshots.

During the exam, the security backend receives the Qt window's native HWND,
validates process ownership, and reuses foreground comparison and its separate
keyboard-hook thread. Focus is sampled per processed frame. Security remains
detection-only by default; partial availability (for example focus only) appears
in the status tooltip. Hooks are active during initialization after the CV
components load and throughout the exam, then stop before completing the session.

**FINISH EXAM** (or **Q** on the Exam page) requests cooperative worker shutdown.
The UI remains responsive and shows a stopping message until the current native
operation returns, the hook listener stops and joins, the face tracker closes,
and the camera releases. The controller waits for the finished worker before
opening Report. Closing the application during initialization or an exam uses
the same cleanup and defers window destruction. Q does not exit the desktop
application or affect text entry on Setup. No QThread is force-terminated.

Report is a placeholder containing student, exam, duration, final risk and total
event count. **VIEW REPORT** is disabled until the full report UI exists.
**NEW SESSION** clears the previous in-memory UI/history/risk summary, rechecks
cameras, and constructs fresh engines and a worker when started. Saved evidence
is retained in the existing shared `sessions/current/evidence/` directory.
There is no persistent event log, session rotation, report export or evidence viewer.

Camera loss or YOLO runtime failure ends the exam with its partial summary and
retained events/evidence. Face-tracker, security or evidence failures degrade
independently with visible statuses/notices and leave CV processing running.
Native camera drivers/model calls cannot be interrupted mid-call; a stuck native
call can delay shutdown until it returns. The app prioritizes releasing resources
cooperatively instead of killing a thread.

The desktop accepts the same local CV/evidence flags as the CLI, for example:

```powershell
.\.venv\Scripts\python.exe src\desktop.py --imgsz 640 --conf 0.35 --threads 4 --head-evidence
```

## Run the preserved OpenCV-only CLI

```powershell
.\.venv\Scripts\python.exe src\main.py
```

Focus the OpenCV preview and press **Q** to exit. Closing the preview window or
pressing Ctrl+C also releases the camera. Errors are printed to stderr and exit
with status 1 for fatal YOLO/dependency/camera errors. If MediaPipe or its model
cannot initialize, a warning is printed and YOLO continues. The face overlay
shows `unavailable`, with an unknown head direction and face count. A face
processing error likewise disables that tracker for the rest of the run and
releases its resources. No face-detector failure is represented as `NO_FACE`.

Optional local configuration:

```powershell
.\.venv\Scripts\python.exe src\main.py --imgsz 640 --conf 0.35 --threads 4
.\.venv\Scripts\python.exe src\main.py --weights C:\models\yolo11n.pt
.\.venv\Scripts\python.exe src\main.py --face-model C:\models\face_landmarker.task
```

## Face states and head direction

`src/vision/face_tracker.py` exposes `FaceResult` containing `status`,
`face_count`, and `head_direction`:

| Face status | Meaning |
| --- | --- |
| `FACE_DETECTED` | Exactly one face has landmarks in this frame. |
| `NO_FACE` | The functioning tracker found no face landmarks. |
| `MULTIPLE_FACES` | At least two landmarked faces were returned. |
| `UNAVAILABLE` | Tracker initialization or processing failed; count is unknown. |

The default maximum is **two faces** to limit CPU work while detecting the
presence of multiple faces. The displayed count is the number returned, capped
at this limit; it is not a count of everyone in the room or a persistent ID.
Multiple-face support is provided by the API, but can miss small, occluded,
profile, or poorly lit faces. It is not a guarantee of detecting every person.

The landmarker estimates a transform from its canonical face geometry to the
observed landmarks. We take its forward (+Z) axis, ignore translation, and
compute approximate horizontal yaw and downward pitch with `atan2`. Uniform
scale cancels in this calculation. These angles are used only for broad state
classification; they are not calibrated gaze measurements.

All face confidences, maximum face count, and direction thresholds live in
`FaceTrackerConfig` / `DEFAULT_CONFIG` in that module. Defaults:

| Head state | Meaning for one detected face |
| --- | --- |
| `CENTER` | Yaw within 20 degrees and vertical tilt within 15 degrees of the canonical frontal orientation. |
| `LEFT` | At least 20 degrees toward the subject's own left. |
| `RIGHT` | At least 20 degrees toward the subject's own right. |
| `UP` | Upward pitch of at least 15 degrees (`up_threshold_degrees`); takes priority over left/right. |
| `DOWN` | Downward pitch of at least 15 degrees; takes priority over left/right. |
| `UNKNOWN` | No single unambiguous face, missing/invalid transform, or pose beyond the 75-degree validity limit. |

Directions assume the existing **unmirrored** webcam image. Subject-left
generally points toward preview-right. With no face or multiple faces, the
single head direction is `UNKNOWN`; no person is arbitrarily selected.
Camera placement, face shape, roll, lighting, and occlusion can bias the rough
orientation. Looking with the eyes alone may leave `CENTER` unchanged. There
is no precise eye tracking, gaze coordinate, personalized calibration, or
temporal decision smoothing; labels can fluctuate at a threshold.

## Local Event Engine

`src/monitoring/event_engine.py` contains a standard-library-only engine.
`FrameSignals` adapts the existing detector results; neither vision module
changes. All rules live in `EventEngineConfig.rules` as `EventRule` dataclasses:

| Event | Minimum continuous duration | Cooldown | Severity |
| --- | --- | --- | --- |
| `PHONE_DETECTED` | 0.7 s | 10 s | critical |
| `MULTIPLE_PERSONS` | 1 s with sustained two-face corroboration; otherwise 3 s | 10 s | critical |
| `MULTIPLE_FACES` | 1 s | 10 s | critical |
| `FACE_MISSING` | 3 s | 10 s | high |
| `LOOK_LEFT` | 4 s | 8 s | medium |
| `LOOK_RIGHT` | 4 s | 8 s | medium |
| `LOOK_UP` | 4 s | 8 s | medium |
| `LOOK_DOWN` | 4 s | 8 s | medium |
| `CAMERA_OBSTRUCTED` (desktop) | 2 s | 10 s | high |

Phone application logic now requires confidence **>=0.55**, centralized in
`EventEngineConfig.phone` (`PhoneDetectionConfig.minimum_confidence`). The
same policy is used by the CLI and desktop for phone state, boxes, event
qualification and evidence overlays. Phones below the application floor are
not drawn and cannot emit PHONE_DETECTED, increase risk or cause a snapshot.
The Event Engine also rejects low/missing/invalid phone confidence in directly
supplied frame signals. Phone persistence remains **0.7 seconds**, with the
existing 10-second cooldown and episode latch. A confidence dip below the floor
resets duration like any other false condition. YOLO's class-wide `--conf` is
unchanged; the effective phone floor is the higher of YOLO and application floors.

`MULTIPLE_PERSONS` uses separate conservative event validation in
`EventEngineConfig.multiple_persons` (`MultiplePersonsConfig`). Raw YOLO person
boxes, confidence labels, person counts and its detection threshold are unchanged.
The shared CV adapter exempts the **largest visible person box** from secondary
geometry checks and evaluates the remaining candidates. This size reference is
a heuristic, with no student identity tracking. Only a secondary box whose
visible area is >=**2%** of frame area, width >=**5%** of frame width and height
>=**15%** of frame height can qualify. Coordinates are clipped to the frame
before validation, so offscreen regions cannot inflate visible size. These
conservative ratios allow sufficiently large partial people while rejecting
small palm/background boxes. A higher-confidence small hand cannot become the
exempt reference when a larger student box exists, and a rejected candidate
does not prevent a valid third detection from qualifying.

The weaker confidence of the reference person and strongest geometry-qualified
secondary person must remain >=**0.60**. Missing/nonfinite/invalid confidence,
invalid secondary geometry or a drop below two persons resets event tracking.
All original person boxes and raw counts remain displayed, including a rejected
secondary box; the geometry rules affect only MULTIPLE_PERSONS qualification.

With one visible face, no visible faces or an unavailable face tracker, the
qualified secondary detection must persist for **3 seconds**. If at least two
YOLO persons qualify and MediaPipe reports `MULTIPLE_FACES` with count >=2,
**1 second** of continuous joint corroboration allows the shorter path. The
corroboration timer starts when the second face appears, not when YOLO first
reported two persons. A second-face flicker cannot prematurely shorten the wait.
Losing face corroboration retains YOLO-only duration, so a turned-away second
person can still qualify through the longer path. Both paths preserve the existing
10-second cooldown and one-event-per-qualified-episode latch. The event duration
reports continuous confidence-qualified YOLO presence; event metadata records
the qualification path and required persistence. The independent `MULTIPLE_FACES`
event is unchanged.

Confidence, persistence and secondary geometry thresholds are configurable:

```python
from monitoring import EventEngine, EventEngineConfig, MultiplePersonsConfig, PhoneDetectionConfig

event_config = EventEngineConfig(phone=PhoneDetectionConfig(minimum_confidence=0.55), multiple_persons=MultiplePersonsConfig(
    minimum_secondary_confidence=0.65,
    uncorroborated_persistence_seconds=4.0,
    corroborated_persistence_seconds=1.5,
    minimum_secondary_area_ratio=0.02,
    minimum_secondary_width_ratio=0.05,
    minimum_secondary_height_ratio=0.15,
))
event_engine = EventEngine(event_config)
# The desktop uses the same policy through SessionConfig(events=event_config).
```

Both entry points also accept local filter overrides:

```powershell
.\.venv\Scripts\python.exe src\desktop.py --phone-conf 0.60 --secondary-person-min-area 0.025 --secondary-person-min-width 0.05 --secondary-person-min-height 0.15
```

Geometry ratios must be between 0 and 1; zero disables an individual size gate.
`vision/detection_filters.py` performs only confidence/box comparisons, with no
new YOLO call, retraining or dependency. Both CV loops pass actual `frame.shape`
and their EventEngineConfig to `frame_signals`. External producers of aggregated
FrameSignals must likewise validate secondary geometry before supplying confidence.

The existing `MULTIPLE_PERSONS` EventRule threshold remains a minimum for either
path: raising it cannot be bypassed by face corroboration. Corroborated persistence
must be positive and no longer than uncorroborated persistence. This is sampled
confidence/count continuity, with no person identity tracking or new inference
pass. Persistent high-confidence false YOLO detections can still qualify through
the longer path; these rules reduce brief/weak false positives rather than
establishing person identity.

Each condition independently follows **inactive -> tracking -> emitted/active
-> inactive**. Tracking starts at the first true sample. The engine emits on
the first processed frame at or beyond its threshold and latches that episode:
it never repeats while the condition remains continuously true, even after
the cooldown expires. One false frame resets the duration and latch. There is
no interruption tolerance or debounce grace period. The cooldown survives
resets and is measured from the last emission for that event type. A new
episode can emit once both its threshold and cooldown have elapsed, provided
it is still active. Disappearing episodes never produce delayed events.

`CENTER` and `UNKNOWN` produce no events. Look events require `FACE_DETECTED`;
multiple or missing faces cannot select a head to monitor. `UNAVAILABLE`
resets face-related tracking and produces no face-missing event, while YOLO
events continue. Frame signals are assumed to hold between samples; long
processing gaps count toward elapsed duration. This is sampled continuity,
not proof that the condition remained true between frames.

Each `ProctoringEvent` contains type, timezone-aware local timestamp, severity,
continuous duration in seconds, message, optional confidence, and frame-state
metadata. `to_dict()` returns a JSON-compatible dictionary with an ISO 8601
timestamp. Phone confidence is the strongest phone detection in the emitting
frame above the application floor. Multiple-person confidence is the weaker of
the reference person and strongest geometry-qualified secondary candidate.
Face/head confidence is omitted because the tracker does not
expose it. These are detector confidences, not misconduct probabilities.

Durations/cooldowns use `perf_counter()` independently of wall-clock changes.
Tests inject a clock or pass `update(signals, now=...)`; time must be finite
and nondecreasing. Configuration can be supplied when constructing the engine:

```python
from dataclasses import replace
from monitoring import EventEngine, EventEngineConfig, EventType

rules = dict(EventEngineConfig().rules)
rules[EventType.PHONE_DETECTED] = replace(
    rules[EventType.PHONE_DETECTED], threshold_seconds=1.0, cooldown_seconds=12.0,
)
event_engine = EventEngine(EventEngineConfig(rules=rules))
```

The webcam loop owns one engine per run. Every emitted event is appended to
`event_engine.history` and printed once, for example:

```text
[14:32:05] CRITICAL | PHONE_DETECTED | Cell phone detected | duration=0.82s
```

Only the latest event appears in an additional preview footer, for **3 seconds**
(`EventEngineConfig.latest_event_display_seconds`). If several events emit on
one frame, all print and enter history; the last in `EventType` declaration
order appears in the footer. The footer temporarily expands the preview;
all original boxes, confidences, counts, face/head labels, FPS and Q exit remain.
When `FACE_MISSING` emits, a separate red footer displays **WARNING: STUDENT
LEFT CAMERA VIEW**. It remains visible throughout that `NO_FACE` episode,
including after the three-second latest-event banner expires. Returned faces
(`FACE_DETECTED` or `MULTIPLE_FACES`) immediately clear it. A tracker failure
(`UNAVAILABLE`) also clears it and retains the technical unavailable labels.
A new absence starts a new warning at its duration threshold even during an
event cooldown; the cooldown continues to prevent repeated history/risk/evidence.
History lasts for the current run and grows with emitted events. Evidence paths
are attached to history entries; the event log itself remains in memory. No
database, networking, reporting, or final event-log UI is added.

## Session risk scoring

`src/monitoring/risk_engine.py` provides `RiskEngine`, `RiskConfig`, `RiskWeight`,
and `RiskLevel`, with no native CV dependencies. Every newly emitted event is
processed once before evidence capture, gets a `risk_delta`, and is retained
with both its risk delta and evidence path in session history. Frames without
events cannot change the score. The Event Engine owns CV duration/cooldown
tracking; the separate SecurityMonitor owns shortcut/focus debounce and cooldowns.

All default weights are centralized in `RiskConfig.weights`:

| Event | First occurrence | Repeated occurrence |
| --- | --- | --- |
| `PHONE_DETECTED` | +30 | +10 |
| `MULTIPLE_PERSONS` | +30 | +15 |
| `MULTIPLE_FACES` | +25 | +10 |
| `FACE_MISSING` | +15 | +8 |
| `LOOK_LEFT` | +8 | +8 |
| `LOOK_RIGHT` | +8 | +8 |
| `LOOK_UP` | +5 | +5 |
| `LOOK_DOWN` | +10 | +10 |
| `ALT_TAB_ATTEMPT` | +15 | +8 |
| `WINDOW_FOCUS_LOST` | +15 | +8 |
| `PRINTSCREEN_ATTEMPT` | +15 | +8 |
| `COPY_ATTEMPT` | +5 | +3 |
| `PASTE_ATTEMPT` | +5 | +3 |
| `ESCAPE_ATTEMPT` | +5 | +3 |
| `CAMERA_OBSTRUCTED` | +20 | +10 |
| `AUTHORIZED_BREAK_STARTED` | 0 | 0 |
| `AUTHORIZED_BREAK_ENDED` | 0 | 0 |
| `AUTHORIZED_BREAK_EXPIRED` | 0 | 0 |

The score starts at **0**, accumulates nonnegative integer contributions, and
is clamped to **100**. Occurrence counts are independent for each event type.
If `RiskWeight.repeated` is omitted, repeated events use the first weight.
Default levels (inclusive score ranges) are:

| Score | Level |
| --- | --- |
| 0–20 | `LOW` |
| 21–45 | `MODERATE` |
| 46–70 | `HIGH` |
| 71–100 | `CRITICAL` |

`risk_delta` reports the increase actually applied after clamping: a nominal
+30 event at score 95 records +5; events at score 100 record +0 but still
increment their occurrence counts. It is included by `to_dict()` on processed
events. Unprocessed events have `risk_delta=None` and omit it from their
serialized dictionaries. Event severity remains separate from session risk
level. For example, the first phone event plus a left-look event gives 38 and
`MODERATE`.

`current_score` and `current_level` are read-only properties. `event_counts`
exposes a read-only mapping with zero-initialized entries for all supported
event types. `reset()` clears score and counts and restores `LOW`; it does not
delete evidence or history or reset the Event Engine or security cooldowns.
The webcam loop creates a fresh Risk Engine on each run. No decay, duration or
confidence multipliers, frame-based increments, or replay deduplication is
implemented. Call `process()` exactly once per newly emitted event.
`CENTER`, `UNKNOWN`, and unavailable tracker states are not risk event types.

Configure weights and, optionally, level boundaries when creating the engine:

```python
from monitoring import EventType, RiskConfig, RiskEngine, RiskWeight

weights = dict(RiskConfig().weights)
weights[EventType.PHONE_DETECTED] = RiskWeight(first=20, repeated=5)
risk_engine = RiskEngine(RiskConfig(weights=weights))
```

Weights must be nonnegative integers; zero disables that event's contribution
while keeping its count. `low_max`, `moderate_max`, and `high_max` configure the
level boundaries, retaining default values 20, 45, and 70 unless changed.
Configuration copies the supplied weights so later caller changes cannot alter
an active session.

The webcam preview always displays a simple risk footer such as:

```text
SESSION RISK: 38 / 100
LEVEL: MODERATE
```

New event console lines retain the message and duration and append the applied
risk delta and running session score. Simultaneous events log their individual
running totals in the Event Engine's declaration order:

```text
[14:32:05] CRITICAL | PHONE_DETECTED | Cell phone detected | duration=0.82s | +30 risk | Session risk: 30/100
```

## Local Windows security monitoring

`src/security/security_monitor.py` separates the Win32 backend from normalization
and the webcam loop. It uses Python's standard-library `ctypes`, queues and
threading; no new dependency or installation is needed. The monitor starts once
the OpenCV window is created. These six events have duration **0**, with severity,
message and independent cooldown centralized in `SecurityConfig.rules`:

| Event | Input/condition | Severity | Cooldown |
| --- | --- | --- | --- |
| `WINDOW_FOCUS_LOST` | Protected window transitions from foreground to background | high | 3 s |
| `ALT_TAB_ATTEMPT` | Alt+Tab | high | 3 s |
| `COPY_ATTEMPT` | Ctrl+C | medium | 2 s |
| `PASTE_ATTEMPT` | Ctrl+V | medium | 2 s |
| `PRINTSCREEN_ATTEMPT` | PrintScreen | high | 3 s |
| `ESCAPE_ATTEMPT` | Escape | medium | 2 s |

Focus monitoring finds the exact OpenCV window title, verifies ownership by the
current process, and compares its root HWND with `GetForegroundWindow()` once
per processed frame. It arms after the protected window has gained focus,
avoiding an initial startup violation while another window is still active.
Continuous background focus produces one event, even after cooldown; a fresh
focus-loss transition can emit after cooldown. A transient NULL foreground
handle is treated as unknown and does not itself produce a violation.
[Microsoft documents this NULL return during activation changes](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-getforegroundwindow).

The user-mode `WH_KEYBOARD_LL` hook runs on its own named, non-daemon message-pump
thread. Its callback tracks key down/up state, ignores held-key auto-repeat,
classifies only the selected shortcuts, and queues inputs. No typed text,
clipboard contents, screenshots, or unrelated key history are recorded. Keyboard
detection remains active throughout the run, including while another window is
foreground; metadata includes `protected_window_focused` (true/false/unknown),
modifier flags and whether the input was injected. To restrict shortcut
detection to the protected window, set
`SecurityConfig(detect_only_when_focused=True)`.

`SecurityMonitor.poll()` emits normalized `ProctoringEvent` objects on the webcam
thread. `EventEngine.record_security_events()` validates and appends them to
session history without changing CV duration tracking. CV events are processed
first in each frame, then the monitor's focus/keyboard batch; each event enters
RiskEngine exactly once. The existing banner shows the latest emitted event,
and the same console formatter appends its applied risk change and session score:

```text
[18:44:12] HIGH | ALT_TAB_ATTEMPT | Alt+Tab attempt detected | duration=0.00s | +15 risk | Session risk: 53/100
```

Security events always have `evidence_path=None`; they never encode or save a
webcam image, even when optional head evidence is enabled. A shortcut that also
causes focus loss can generate two separate event types and risk contributions.
Keyboard cooldowns use captured monotonic input time; wall timestamps are set
when the monitor emits the normalized event.

**Default behavior blocks nothing.** Detected shortcuts are passed to Windows
and other applications normally. Optional suppression is available through
`SecurityConfig.blocked_events` for Ctrl+C, Ctrl+V, PrintScreen and bare Escape, and
applies only when the protected window is foreground. The native callback
actually returns a nonzero hook result for selected key downs/repeats and their
paired releases; it is not just a detection flag. Metadata reports
`action="BLOCKED"`/`blocked=True` only for this suppression path; ordinary events
report `action="DETECTED"`/`blocked=False`. Alt+Tab and focus changes are never
blocked. Modified Escape shortcuts, including Ctrl+Shift+Esc, are passed through even
when bare Escape suppression is enabled. Suppression of held-key repeats ends
if focus moves to another window. For example, construct the application's monitor with:

```python
from monitoring import EventType
from security import SecurityConfig, SecurityMonitor

security_monitor = SecurityMonitor(SecurityConfig(
    blocked_events=frozenset({EventType.COPY_ATTEMPT, EventType.PASTE_ATTEMPT}),
))
```

Suppression affects the observed hook input while the hook is active; it does
not guarantee a protected OS environment or prevent other screenshot/clipboard
methods. A Windows hook can be silently removed after a timeout. The callback
performs no image processing, risk updates or console/disk I/O, and its dedicated
message pump normally waits 10 ms between polls rather than spinning.
[Microsoft describes the hook result and timeout behavior](https://learn.microsoft.com/en-us/windows/win32/winmsg/lowlevelkeyboardproc).

Focus/window lookup, API and hook initialization failures print a clear
`[SECURITY] Warning` and keep webcam CV processing alive. If only the keyboard
hook fails, native focus comparison can continue. Runtime failures also degrade
without generating technical-state proctoring violations. Repeated warnings for
the same component are suppressed. Outside Windows, security monitoring warns
and disables itself while the existing CV application continues.

Shutdown on Q, Ctrl+C, window close or fatal CV error calls `SecurityMonitor.stop()`
in `finally`, before destroying the preview. A stop Event wakes the listener's
message-pump wait; its `finally` removes the hook with `UnhookWindowsHookEx`.
The caller joins the thread, then clears pressed-key state and pending inputs.
Stop is idempotent, and partially initialized listeners are cleaned up too.
No worker listener remains after normal shutdown.

Limitations: focus is sampled at CV frame rate, so a very short switch away and
back between frames can be missed. Exact title lookup requires the native
OpenCV window and may differ with other GUI backends. Only the named shortcuts
are detected; mouse/menu copy and paste, Win+Shift+S, other screenshot tools,
secure desktops and alternate navigation methods are outside this scope.
The current focus check can later be replaced by native PySide6 focus events
through the isolated `SecurityBackend` interface. No PySide6, registry/policy
changes, Task Manager controls, Ctrl+Alt+Del blocking, kernel-level hooks,
database, login, networking or report export is introduced.

## Automatic local evidence

`src/evidence/evidence_manager.py` saves one JPEG when an emitted event is
`PHONE_DETECTED`, `MULTIPLE_PERSONS`, `MULTIPLE_FACES`, `FACE_MISSING`, or
`CAMERA_OBSTRUCTED` (desktop; recent usable frame preferred).
Other frames perform no JPEG encoding or filesystem operations. Head-direction
snapshots (`LOOK_LEFT`, `LOOK_RIGHT`, `LOOK_UP`, `LOOK_DOWN`) are disabled by
default. Enable them explicitly:

```powershell
.\.venv\Scripts\python.exe src\main.py --head-evidence
```

`EvidenceConfig` centralizes the directory, `head_direction_enabled`, and JPEG
quality (default **90**). The default directory is anchored to the project,
independent of the working directory, and created on the first successful
snapshot:

```text
sessions/
  current/
    evidence/
      PHONE_DETECTED_20261007_173512_481.jpg
```

Names use the event type and timestamp to milliseconds. Exclusive file creation
prevents overwrites; collisions append `_1`, `_2`, and so on, including across
application restarts. Saved files remain in this shared current directory;
there is no automatic deletion, session rotation, or full session persistence.
`/sessions/` is excluded by `.gitignore`.

Evidence reuses the current preview's annotated camera image and permanent
status footer: boxes/confidences, person count, phone state, face state, head
direction, and FPS remain visible. The crop excludes the temporary latest-event
and red face-missing warning footers, as well as the session-risk footer. At the requested 640x480 resolution,
snapshots are 640x580. Several simultaneous eligible events each get their own
file with the same frame; drawing and inference are not repeated.

The event's `evidence_path` is a project-relative string such as
`sessions/current/evidence/PHONE_DETECTED_20261007_173512_481.jpg`.
`to_dict()` includes this field, using `None` before capture, for disabled event
types, or after failure. Evidence enrichment preserves the other event fields
and replaces the immutable event in history and the latest-event reference.
Duration tracking, cooldowns, and one-event-per-continuous-episode behavior are
unchanged. The console confirms each successful snapshot:

```text
[EVIDENCE] Saved: sessions/current/evidence/PHONE_DETECTED_20261007_173512_481.jpg
```

Encoding, directory creation, and writing failures log an `[EVIDENCE]` error,
retain the event with `evidence_path=None`, and let monitoring continue.
Incomplete files are removed where possible. Failed saves are not retried on
subsequent frames. Saving is synchronous only on eligible emitted events;
an event frame can take longer due to JPEG encoding and disk I/O.

## CPU behavior and limitations

- A single reusable predictor runs in FP32 on CPU; there is no GPU requirement.
- Default inference size is 416 with minimum-rectangle padding. The camera is
  requested at 640x480 / 30 FPS; a camera driver may choose different settings.
- OpenCV uses one thread and PyTorch uses up to four by default. `--threads`
  allows adjustment for the actual CPU.
- All frames run YOLO and the same reusable MediaPipe video-mode landmarker.
  Video mode uses tracking with monotonically increasing frame timestamps.
  Blendshapes and dense landmark drawing are disabled. The two-face limit
  requires more work than one face; MediaPipe only enables its built-in
  landmark smoothing with `num_faces=1`.
- No asynchronous frame queue is added. A one-frame camera buffer is requested
  where the driver supports it.
- FPS measures capture, both vision modules, drawing, and display/key processing. The
  overlay averages the last 30 processed frames after five warmup frames; the
  console reports the average on exit. Actual FPS must be measured on the
  target computer; it is not the requested camera FPS. Adding face inference
  has a CPU cost; the previous YOLO-only FPS is not guaranteed.
- Increasing `--imgsz` to 640 may improve small-phone detection at a CPU speed
  cost. Detections can flicker or miss occluded objects. Counts describe visible
  detections in the current frame, not unique people or proof of misconduct.

If the webcam cannot open, connect/enable it, close other camera applications,
and allow desktop camera access in Windows **Settings > Privacy & security >
Camera**. The application tries camera index 0 using DirectShow, Media
Foundation, then the default backend, checking the first frame on each attempt.

## Files and basic checks

```text
src/
  desktop.py
  main.py
  evidence/
    __init__.py
    evidence_manager.py
  monitoring/
    __init__.py
    event_engine.py
    risk_engine.py
  security/
    __init__.py
    security_monitor.py
  ui/
    __init__.py
    main_window.py
    setup_page.py
    exam_page.py
    report_page.py
    vision_worker.py
    session.py
    theme.py
  vision/
    __init__.py
    yolo_detector.py
    face_tracker.py
    detection_filters.py
tests/
  test_face_tracker.py
  test_event_engine.py
  test_event_integration.py
  test_multiple_persons.py
  test_detection_filters.py
  test_evidence.py
  test_risk_engine.py
  test_security_monitor.py
  test_desktop.py
sessions/                  # generated and ignored
  current/
    evidence/
models/
  .gitkeep
requirements.txt
README.md
.gitignore
```

```powershell
.\.venv\Scripts\python.exe -m compileall -q src
.\.venv\Scripts\python.exe -m pip check
.\.venv\Scripts\python.exe src\main.py --help
.\.venv\Scripts\python.exe src\desktop.py --help
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

Manual smoke check: run the application, verify the count with one/two people,
hold a visible phone, check the box labels, face/head states, and press Q. Turn
your head left/right/down, then leave the frame and try a second visible face.
Hold each signal past its event threshold, verify a single console event and
brief footer, keep the condition active to check that events do not repeat,
then reset and repeat after cooldown. Check sustained upward head motion too.
Verify the evidence confirmation, saved JPG, attached path, and absence of
repeated snapshots while a condition stays active. Use `--head-evidence` to
check optional head snapshots.
Verify the score stays unchanged between emitted events, the first/repeat
increments differ for phone/person/face events, and levels follow the ranges
above. Risk text, the latest event and the persistent face warning should all
remain readable together.
On Windows, focus the preview, use each listed shortcut, switch to another
window and return, and verify the security banner/console/history/risk changes
with no keyboard evidence JPGs. Repeat after cooldown; holding a key should
not generate repeated events. Default mode should leave shortcuts functional.
Quit with Q and verify the process exits with no security listener remaining.
Temporarily point `--face-model` at a nonexistent file to verify that YOLO
continues with the face overlay unavailable. Disconnect or disable
the webcam to verify the actionable error and nonzero exit. No
database, application networking, training, or additional service is included.

Reference: [Ultralytics YOLO11](https://docs.ultralytics.com/models/yolo11/),
[prediction arguments](https://docs.ultralytics.com/modes/predict/), and
[PyTorch CPU wheel installation](https://pytorch.org/get-started/previous-versions/).
[MediaPipe Face Landmarker](https://ai.google.dev/edge/mediapipe/solutions/vision/face_landmarker/python)
documents the Tasks API, video tracking, multi-face option, and transformation output.

### Verification

CV, Event Engine, face warning, evidence, risk, security and desktop: all **320 tests** pass in the existing **Python 3.12.0**
environment, including the original 16 vision tests. Compilation, CLI help,
and `pip check` also pass. New deterministic tests cover every duration
threshold, single-frame resets, cooldown boundaries for every event type,
continuous-condition suppression, independent simultaneous events, unavailable
face tracking, configuration, event serialization/history, console formatting,
preservation of overlays, three-second footer expiry, and Q exit in a mocked
webcam loop. Tests do not sleep or access the webcam.
Warning tests also cover configurable thresholds, persistence past banner
expiry and cooldown, disappearance when faces return, unavailable trackers and
runtime failures, repeat episodes during cooldown, and the red footer alongside
the original overlays.
Evidence tests cover all five default event types, all four optional head
directions, event-field preservation and path serialization, readable real
JPEGs in temporary directories, exclusive collision handling across manager
instances, repeat-save suppression, encoding/directory/open/partial-write
failures, and cleanup. Mocked webcam-loop tests cover enriched history,
snapshot cropping, simultaneous events, optional CLI configuration, continued
processing and Q exit after save failure, and no capture without an event.
Risk tests cover every default first/repeat weight, accumulation, all level
boundaries, score capping and partial/zero applied deltas, occurrence counts,
reset, custom/invalid configurations, event-field preservation, exclusion of
raw frames and CENTER/UNKNOWN, and serialization. Mocked webcam-loop tests
verify event-only scoring through cooldown/reset episodes, per-event console
totals, preview risk text alongside the face warning, preserved evidence crops
and paths, continued scoring after evidence save failure, and Q exit.
The 44 security tests cover all requested detections, independent cooldowns,
focus arming/re-entry/unknown states, session-wide and focus-only shortcut
detection, Win32 HWND ownership/comparison, modifier tracking and auto-repeat,
default pass-through, opt-in suppression, fail-open behavior for modified
Escape/OS shortcuts and focus changes, callback errors, API/hook startup and
runtime failures, normalized history, default first/repeat risk weights and
capping, evidence exclusion, mixed CV/security frames, preservation of the
FACE_MISSING warning, and cleanup after Q or CV failure. Native calls are faked;
tests never press OS-level keys or install actual keyboard hooks. Fake hooks
run through a real thread to verify unhooking and joining. Actual shortcut
delivery/suppression still needs the manual Windows smoke check above.

A read-only native focus-comparison benchmark measured **1.79 microseconds/poll**
(five runs of 10,000 calls). It installed no hook and pressed no keys. No live
webcam FPS or real-key hook latency was remeasured for this update; CV inference
settings are unchanged, and the keyboard worker uses an interruptible wait
rather than a busy loop. The security backend itself adds no dependencies or
generated files; the desktop dependency is described below.

Desktop verification: all **54 desktop tests** pass with PySide6 **6.11.2**
on Python 3.12. They run offscreen with fake camera/model/security adapters and
cover Setup validation, defaults/camera selection, initialization gating, timer
state, page transitions, timeline/risk display, bounded timeline and frame
delivery, detached BGR QImage ownership, transient banners, persistent face
warning and unavailable trackers, evidence preservation/exclusion/failure,
component initialization/runtime/cleanup errors, Finish Exam, New Session,
window close and Q behavior. Tests exercise an actual QThread and queued Qt
signals for finish/close cleanup without pressing OS keys or installing hooks.
Setup, Exam, warning and Report were also rendered offscreen and visually checked
at 1280x800 and a 1366x728 laptop layout. The original 160 tests still pass.
The desktop launch help, compilation and dependency checks pass too.

Live desktop webcam FPS and real-key delivery were not remeasured in this task.
Qt adds frame copying, pixmap scaling and widget refresh work; latest-frame
coalescing prevents a slow UI from accumulating old video frames. Existing CPU
inference settings, threshold/cooldown logic, risk weights and evidence rules
are unchanged. A full real-camera Windows session remains a manual smoke check.
For that check, start from Setup, verify preview/status/events/risk/evidence and
focus/shortcut detection, Finish Exam, then New Session and repeat. Close once
during initialization and once during an exam to verify normal resource release.
Only `PySide6` is added to requirements; generated QA screenshots remain under
the already ignored `sessions/` directory, so `.gitignore` needs no changes.

Multiple-person reliability: 22 dedicated deterministic tests cover weak and
short-lived secondary detections, confidence boundaries/missing values, stable
YOLO-only persistence, corroborated and late/flickering face signals, unavailable
trackers, one-person scenes, confidence interruptions, both custom persistence
paths, EventRule minimums, cooldown/reset/latch behavior and unchanged phone
events. Additional CLI/desktop regressions preserve raw boxes/counts/phone
confidence, suppress weak-person risk/evidence, and retain normal risk/evidence
for a qualified multiple-person event. YOLO and MediaPipe inference remain
unchanged, with no extra CV pass.

Application filtering: 23 dedicated tests cover weak chair/phone detections,
phone floor boundaries and configuration, preserved 0.7-second timing/reset/
cooldown, hidden weak boxes, missing/invalid confidence, hand-like secondary
boxes, real and partial people, primary exemption, higher-confidence hand
candidates, valid third candidates, visible clipping, resolution scaling,
geometry boundaries and configuration, persistence resets and unchanged face
corroboration. New desktop and CLI regressions verify zero risk/evidence for
rejected phones/persons and preserved risk/evidence for accepted real/partial
people and phones. All **320 tests** pass; compilation and both entry-point
help commands pass. Automated tests use fakes and no webcam or OS-level keys.
Live FPS and real-world false-positive rates were not remeasured for these filters.

Monitoring reliability adds **56 deterministic tests**, preserving the preceding
264 tests (one warning assertion now reflects threshold visibility during cooldown).
They cover all live head directions and timed timeline/risk/banner events;
face warning persistence/return/unavailability; short/sustained/repeated cover,
dark textured frames and custom thresholds; recent/stale evidence and save
failure; PIN verification, all durations, deadline/early end, fresh absence
timing with preserved cooldown, zero-risk break history, security/phones during
breaks, PIN queue cleanup and laptop warning layout. All use fake time/synthetic
frames; none sleep or press OS-level keys. Head, absence, obstruction/break,
PIN and duration panels were rendered offscreen and visually checked at 1366x728.
Quality sampling plus copying the 640x580 evidence cache measured **0.769 ms/frame
median**, **1.156 ms/frame p95** over 500 synthetic samples. This excludes YOLO,
MediaPipe, Qt rendering and event JPEG writes. Live webcam FPS was not remeasured.

Files for this pass: new `src/vision/camera_quality.py`,
`src/monitoring/break_manager.py`, `tests/test_monitoring_reliability.py`; updated
`src/monitoring/event_engine.py`, `src/monitoring/risk_engine.py`,
`src/evidence/evidence_manager.py`, `src/main.py`, `src/ui/session.py`,
`src/ui/vision_worker.py`, `src/ui/exam_page.py`, `src/ui/main_window.py`,
`src/ui/theme.py`, `tests/test_desktop.py`, `tests/test_event_engine.py`,
`tests/test_event_integration.py`, and this README. Requirements and .gitignore
are unchanged; synthetic QA artifacts remain under ignored `sessions/current/`.

Evidence performance: 100 synthetic 640x580 JPEG snapshots at quality 90 on
temporary local storage measured **6.709 ms/event median** and **8.135 ms/event
at the 95th percentile**, including encoding, writing, and path enrichment.
The test image produced a 118.5 KiB JPEG. A disabled head-event check measured
**0.192 microseconds/call**; frames without emitted events do not call capture.
Snapshots therefore add occasional event-frame latency rather than continuous
encoding work. Multiple simultaneous events add one save cost per eligible
event. Storage speed and image content affect this measurement; live webcam
FPS was not remeasured for the evidence update.

A local synthetic benchmark before the persistent face warning (five runs) measured signal adaptation plus an
engine update at **3.91 microseconds/frame** (20,000 updates/run). Drawing with
the event footer measured **0.899 ms/frame**, compared with **0.766 ms/frame**
without it (500 draws/run). Estimated added processing while the footer is
visible is **0.136 ms/frame**, equivalent to roughly **19.95 FPS** from a
20 FPS baseline. This estimate excludes live capture/display and occasional
console output; live webcam FPS was not remeasured for this update. YOLO and
MediaPipe inference settings and calls are unchanged.

The following vision verification describes the earlier CV implementation:

The automated checks cover direction signs and configurable thresholds,
scale/translation invariance, invalid/missing poses, zero/one/multiple faces,
missing MediaPipe and initialization/runtime failure, RGB conversion, monotonic
timestamps, CPU/video options, tracker cleanup, all YOLO/face overlay labels,
and continued YOLO execution when the face tracker is unavailable. Native
webcam scenes still require the manual smoke check above; synthetic orientation
tests do not establish gaze accuracy or real-world detection reliability.

Verified in the existing Windows **Python 3.12.0** environment: compilation,
CLI help, all **16 tests**, and `pip check` passed. Both native models initialized
and processed blank frames successfully (about **27.9 FPS** combined inference,
excluding capture/display). A 40-frame real webcam preview smoke check measured
about **15.8 FPS** after five warmup frames and observed `FACE_DETECTED` and
`NO_FACE`. This short measurement is not a sustained FPS guarantee. Multiple
faces and head-direction signs were checked synthetically; natural left/right/
up/down motion and multiple-person scenes still need manual validation.

MediaPipe printed native XNNPACK/feedback-tensor initialization warnings but
continued normally. Webcam access failed inside the execution sandbox and
succeeded outside it. An existing Ultralytics settings-directory fallback caused
permission warnings; creating the local configuration directory before its
import resolved those warnings without changing YOLO detection behavior.
