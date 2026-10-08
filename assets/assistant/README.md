# AI Proctor Assistant assets

The desktop widget works without image files by drawing a small local placeholder.
Add transparent PNGs here to replace it. Asset filenames and state mapping are
centralized in `src/ui/assistant_model.py`:

| State | File | Session risk |
| --- | --- | --- |
| CALM | `calm.png` | 0–20 |
| NEUTRAL | `neutral.png` | 21–45 |
| ALERT | `warning.png` | 46–70 |
| SERIOUS | `serious.png` | 71–85 |
| CRITICAL | `critical.png` | 86–100 |

Use transparent backgrounds, consistent dimensions, and a readable silhouette
at the widget's small display size. Images remain static during the exam.
