# pyuvc Examples with PyUSB / libusb1

This workspace provides a lightweight toolkit to inspect UVC cameras via
PyUSB **and** libusb1.  Après quelques soucis sur des caméras capricieuses, la
pile a été alignée sur le pilote `uvcvideo` du noyau Linux : PROBE/COMMIT,
sélection de l’alternate setting et streaming ISO sont désormais effectués sur
le même handle libusb1, ce qui stabilise les flux continue (ELP6USB4KCAM01H-CF
testée).  Le dépôt contient :

- `uvc_usb.py` – high level helpers: descriptor parsing, PROBE/COMMIT negotiation,
  synchronous frame capture, and an asynchronous capture API built on libusb1.
- `uvc_async.py` – tiny wrapper around libusb1 to keep multiple isochronous
  transfers in flight and forward every ISO packet to Python callbacks.
- `pyusb_uvc_info.py` – lists all streaming interfaces/modes; optionally runs a
  PROBE and shows negotiated bandwidth.
- `pyusb_capture_frame.py` – captures a single frame (YUYV or MJPEG) and writes
  the payload to disk.
- `pyusb_capture_display.py` – captures a single YUYV frame asynchronously and
  displays it with matplotlib.
- `udev/99-hp-5mp-camera.rules` – example udev rule granting plugdev users RW
  access to an HP integrated webcam.

## 1. Requirements

### System packages

```
sudo apt-get install -y     python3     python3-pip     libusb-1.0-0     v4l-utils
```

Ajoutez les paquets GStreamer (`python3-gi`, `gir1.2-gst-1.0`,
`gstreamer1.0-plugins-good`) si vous souhaitez utiliser l’aperçu MJPEG.
`v4l-utils` reste pratique pour inspecter la caméra (`v4l2-ctl --list-formats-ext`).

### Python dependencies

Toutes les dépendances Python sont listées dans `requirements.txt` :

```
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

`usb1` gère le dialogue bas niveau, `pyusb` est utilisé pour les outils CLI,
`numpy`/`opencv-python` convertissent YUYV→RGB et `pillow`/`matplotlib`
permettent les aperçus et exports.

### Udev rule (access without sudo)

### Udev rule (access without sudo)

Copy the provided rule and reload udev so that members of the `plugdev` group can
access the camera:

```
sudo cp udev/99-hp-5mp-camera.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules
sudo udevadm trigger
```

Unplug / replug the camera (or toggle it in BIOS) to apply the permissions.
Verify that your user belongs to `plugdev` (`id -nG`); otherwise add yourself
and re-login.

## 2. Usage overview

> Always run the scripts with `/usr/bin/python3` if you need the installed
> `usb1` binding.

### Enumerate UVC formats

```
/usr/bin/python3 pyusb_uvc_info.py --vid 0x0408 --pid 0x5473
```

Add `--probe-interface`, `--probe-format`, `--probe-frame`, `--probe-fps`,
`--commit` to negotiate a stream configuration and print the resulting
bandwidth/alt-setting.

### Capture a raw frame to disk

```
/usr/bin/python3 pyusb_capture_frame.py \
    --vid 0x0408 --pid 0x5473 \
    --width 640 --height 480 --fps 30 \
    --output frame.raw
```

By default the script auto-selects the first YUYV mode.  Provide `--format` / `--frame`
if you want a specific bFormatIndex/bFrameIndex (e.g. MJPEG).

### Capture & display a YUYV frame (asynchronous ISO)

Le HP 5MP Camera n’expose YUYV qu’en 640×480 ou 640×360.  Utilise un framerate
modeste (15 fps est sûr) et laisse le script assembler les paquets via libusb1 :

```
/usr/bin/python3 pyusb_capture_display.py \
    --vid 0x0408 --pid 0x5473 \
    --width 640 --height 480 \
    --fps 15 --skip-frames 0 --timeout 5000
```

- `--skip-frames` can warm up the sensor before saving the first frame.
- `--log-level DEBUG` provides detailed packet headers (FID/EOF/SCR/PTS) if you
  need to troubleshoot.
- In headless environments the frame is written to `uvc_frame.png`.
- Si 30 fps reste instable, réduis à 15 fps ou moins.

Pour un flux **MJPEG** (résolutions supérieures), utilise GStreamer :

```
/usr/bin/python3 pyusb_capture_video.py \
    --vid 0x0408 --pid 0x5473 \
    --width 1920 --height 1080 \
    --fps 30 --codec mjpeg --timeout 5000
```

Le script pousse alors les images JPEG dans un pipeline
`appsrc ! jpegdec ! videoconvert ! autovideosink`.
- If 30 fps still drops frames, reduce to 15 fps or lower to match the available
  USB bandwidth/pipeline on your machine.

### Video preview avec OpenCV

```
/usr/bin/python3 pyusb_capture_video.py \
    --vid 0x0408 --pid 0x5473 \
    --width 640 --height 480 \
    --fps 15 --skip-frames 2 --timeout 5000
```

Une fenêtre OpenCV s’ouvre et affiche le flux temps-réel. Appuie sur `q` ou
`ESC` pour quitter. Ajuste `--fps` en fonction de la stabilité de ton système.
Utilise `--codec mjpeg` pour forcer MJPEG, `--codec yuyv` pour imposer YUYV.

### Notes on asynchronous capture

- The code keeps 12 isochronous transfers × 32 packets (3060 B) in flight.
- Each packet is parsed individually; frames are accepted only when 614 400 B of
  payload are collected without the ERR flag.
- `uvc_async.py` relies on `USBTransfer.getISOSetupList()` to read the actual
  byte count per packet. If your camera delivers short packets frequently, this
  ensures we do not misalign the payload.
- Falling back to MJPEG is as simple as choosing a format with `description == "MJPEG"`
  and decoding the resulting payload via Pillow before display.

### Troubleshooting tips

- Use `v4l2-ctl --device /dev/video0 --list-formats-ext` to confirm which YUYV
  modes are supported.
- If you see continuous frame drops with the asynchronous capture, try reducing
  `--fps` or ensure no other process is reading the camera simultaneously.
- To debug the UVC negotiation, run `pyusb_uvc_info.py --probe-interface ... --log-level DEBUG`
  to print the `dwMaxPayloadTransferSize`, `dwFrameInterval`, and the selected
  alt-setting.

## 3. Code structure & comments

- `uvc_usb.py` documents each step of the UVC PROBE/COMMIT agreement and exposes
  convenience properties (`current_resolution`, `active_alt_setting`, etc.).
- `uvc_async.py` is heavily commented to highlight where transfers are resubmitted
  and how actual packet lengths are extracted.
- Both capture scripts include verbose logging to make it easier to trace the
  frame assembly and to diagnose truncated payloads.

Feel free to extend the module with MJPEG decoding or bulk endpoint support if
needed. Contributions with usbmon traces from other devices are very welcomed to
refine the negotiation heuristics.
