# Shorts · Screenshot & Annotation Tool

[中文文档](README.md)

A cross-platform screenshot & annotation tool (macOS / Windows), built with Python + PyQt6 as a single codebase. Region / window / fullscreen / scrolling long screenshots, GIF & MP4 recording, OCR, and a dozen annotation tools.

## Download

Grab the installer for your platform from [Releases](https://github.com/geminird/shorts/releases):

| File | Platform |
|------|----------|
| `Shorts.exe` | Windows x64, single file, runs directly |
| `Shorts-Windows-x64.zip` | Windows x64 (same, zipped) |
| `Shorts-macOS-arm64.dmg` | macOS Apple Silicon, mount and drag to Applications |
| `Shorts-macOS-arm64.zip` | macOS Apple Silicon (unzips to .app) |

> macOS builds are adhoc-signed: if Gatekeeper blocks first launch, right-click the app → "Open", or run `xattr -cr /Applications/Shorts.app`. Windows builds are unsigned — choose "Run anyway" when SmartScreen prompts.

## Features

- **Region capture** — drag to select, auto-snaps to window edges
- **Window capture** — hover to highlight, click to grab the whole window
- **Fullscreen capture**
- **Scrolling capture** — stitches scrolled content into one long image (NCC + Sobel edge matching, with auto-scroll mode and wheel-odometry priors)
- **GIF / MP4 recording** — region recording with click ripples and in-recording annotations
- **OCR** — native macOS Vision (Chinese & English), results copied to clipboard
- **Annotation tools** — arrow, line (solid/dashed), rectangle, ellipse, text, freehand pen, highlighter, mosaic (block/gaussian), step numbers, color picker, annotation outlines
- Copy to clipboard / save to file, launch at login, Retina 2x crisp output

## Platform Differences

Core capture / annotation / GIF are identical across platforms. These differ:

| Feature | macOS | Windows |
|---------|:-----:|:-------:|
| Region / window / fullscreen capture | ✅ | ✅ |
| Annotation tools (full set) | ✅ | ✅ |
| Scrolling capture (manual scroll + auto-stitch) | ✅ | ✅ |
| Scrolling capture · auto-scroll mode | ✅ (synthetic CGEvent, requires Accessibility) | ✅ (pyautogui, lightly tested) |
| Wheel-odometry prior (fast-scroll stitch safety) | ✅ (NSEvent monitor, requires Input Monitoring) | ❌ (pure visual matching, still works) |
| OCR | ✅ native Vision (zh/en) | ❌ |
| GIF recording (click ripples + live annotations) | ✅ | ✅ |
| MP4 export | ✅ requires [ffmpeg](https://ffmpeg.org) | ✅ requires ffmpeg |
| Retina / HiDPI output | ✅ 2x | ✅ (follows system scaling) |
| Global hotkey | double-tap Cmd | Ctrl + Alt + A |
| Launch at login | ✅ | ✅ |
| System permissions | Accessibility / Screen Recording / Input Monitoring | none |

## Run from Source

```bash
pip install -r requirements.txt
python main.py
```

On macOS, OCR requires the Swift helper (a prebuilt arm64 binary is included; other architectures need to compile it):

```bash
swiftc core/ocr_helper.swift -o core/ocr_helper -O
```

## Hotkeys

| Platform | Global capture hotkey |
|----------|----------------------|
| Windows | Ctrl + Alt + A |
| macOS | double-tap Cmd |

## macOS Permissions (grant on first use)

The tool relies on macOS system capabilities. Grant these in **System Settings → Privacy & Security**:

| Permission | Used for | Without it |
|-----------|----------|-----------|
| Accessibility | global hotkey (double-tap Cmd), auto-scroll | hotkey won't fire; auto-scroll can't scroll the target |
| Screen Recording | capture / GIF / OCR | captures come out black |
| Input Monitoring | wheel odometry for scrolling capture | stitch falls back to pure visual matching (still works) |

> Note: adhoc re-signing during development changes the binary hash and may void TCC grants — toggle the switch off and on in System Settings if features stop working.

## Build

```bash
pyinstaller shorts_app.spec
```

Windows produces `dist/Shorts.exe`; macOS produces `dist/Shorts.app` (adhoc-signed; local use opens directly, distribution needs your own signing/notarization).

## Known Issues

- Scrolling capture is fundamentally ambiguous on extremely repetitive layouts (e.g. lists of identical unlabeled color blocks); auto-scroll mode avoids this
- Occasional crash on first capture (rare, not yet reproduced)

## License

[MIT](LICENSE)
