Prometheus Gesture Control v2
=============================

What this version does
----------------------
- Uses /dev/video0 through OpenCV
- Does NOT show the camera feed
- Draws a transparent desktop HUD overlay instead
- Point = move mouse
- Quick pinch = click
- Pinch and hold = drag
- Two-finger scroll pose = scroll
- Three-finger pose = take screenshot
- Saves screenshots into ../screenshots/

Expected project layout
-----------------------
Jarvis.v5.1/
├── gesture_control/
│   ├── camera_input.py
│   ├── hand_tracker.py
│   ├── gesture_engine.py
│   ├── mouse_router.py
│   ├── overlay_hud.py
│   ├── gesture_service.py
│   ├── requirements.txt
│   └── README.txt
├── models/
│   └── hand_landmarker.task
└── screenshots/

Install
-------
cd /home/tatel/Desktop/Jarvis.v5.1/gesture_control
pip install -r requirements.txt

Model file
----------
You need the MediaPipe hand model here:
/home/tatel/Desktop/Jarvis.v5.1/models/hand_landmarker.task

Run
---
cd /home/tatel/Desktop/Jarvis.v5.1/gesture_control
python3 gesture_service.py

Gestures
--------
1) Mouse move
   - Point with index finger

2) Click
   - Quick thumb + index pinch

3) Drag
   - Hold pinch for about 8 stable frames
   - Release fingers to drop

4) Scroll
   - Index + middle extended
   - Ring + pinky folded
   - Move hand up/down

5) Screenshot
   - Index + middle + ring extended
   - Pinky folded
   - No pinch
   - Screen will flash briefly
   - Screenshot saves into ../screenshots/

Notes
-----
- This version is best-effort and tuned for your current setup.
- If overlay appears but clicks/scroll feel off, tune thresholds in gesture_engine.py.
- If PyQt6 transparency behaves oddly under KDE/X11, the overlay still usually works.
- If scrolling is too fast or too slow, change:
    scroll_delta = int(delta * 2500)
- If drag starts too easily or too slowly, adjust:
    self.same_count >= 8
