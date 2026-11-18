# MicroPixl

MicroPixl is a lightweight, fully local Python desktop utility for three common image tasks: extracting text, tracing raster images into vectors, and generating application icon sets. It is designed to run comfortably on low-specification machines (4 GB of RAM) and has no AI model dependencies, no cloud APIs, and no network access at runtime.

## Table of Contents

- [Features](#features)
- [Architecture and Performance](#architecture-and-performance)
- [User Interface](#user-interface)
- [System Requirements](#system-requirements)
- [Installation](#installation)
- [Running the Application](#running-the-application)
- [Usage](#usage)
- [Local Fonts](#local-fonts)
- [Directory Structure](#directory-structure)
- [Configuration Reference](#configuration-reference)
- [Troubleshooting](#troubleshooting)
- [Known Limitations](#known-limitations)

## Features

MicroPixl performs exactly three functions.

| Tool | Engine | Purpose |
|---|---|---|
| OCR Text Extraction | Tesseract via `pytesseract` | Extract text from photos, scans, and screenshots without templates |
| Raster to SVG | `vtracer` | Trace PNG and JPG images into clean vector files |
| App and PWA Icons | Pillow | Resize one image into standard device and web icon dimensions |

Supported input formats: PNG, JPG/JPEG, BMP, TIFF, WebP, and GIF.

### OCR Text Extraction

- Processes images one at a time and shows results as each file completes.
- Language selection through Tesseract language codes, including combinations such as `eng+ara+fra`. The corresponding language data must be installed.
- Four page segmentation modes: automatic layout, single uniform block, single line, and sparse text.
- Preprocessing before recognition:
  - Conversion to grayscale, with transparent regions flattened onto white.
  - Automatic contrast adjustment.
  - Downscaling of images whose longest side exceeds 3500 px.
  - 2x upscaling of images whose longest side is below 1000 px.
- A 180 second timeout per image prevents a single file from blocking the queue.
- Results can be copied to the clipboard or saved as a `.txt` file. When several images are processed, each result is prefixed with its file name.

### Raster to SVG Conversion

- Three presets:

  | Preset | Intended use |
  |---|---|
  | Color - detailed | Photographs and illustrations |
  | Color - simple | Logos and flat artwork |
  | Black and white | Line art, text, and scans |

- Images larger than 2000 px on the longest side are downscaled before tracing. Formats other than PNG and JPG are re-encoded to a temporary PNG first.
- Output files are named after the source image. Existing files are never overwritten; a numeric suffix is appended instead.

### App and PWA Icon Resizer

- 17 preset sizes: 16, 32, 48, 64, 72, 96, 128, 144, 152, 167, 180, 192, 256, 300, 384, 512, and 1024 px. The common PWA, iOS, and Android sizes are selected by default.
- Custom sizes from 1 to 4096 px, entered as `300, 310x310, 620`.
- Two square modes:
  - Fit keeps the whole image and pads with transparency.
  - Fill crops the image to a square.
- Optional `favicon.ico` containing 16, 32, and 48 px images.
- Each source image is opened once, every size is derived from it, and each generated icon is released immediately after it is saved.
- Output layout: `<output folder>/<image name>_icons/<image name>_<size>x<size>.png`.

If no output folder is chosen, SVG and icon output is written to a `micropixl_output` folder next to the first selected image.

## Architecture and Performance

MicroPixl follows strict resource rules so that it remains responsive on machines with limited memory.

| Rule | Implementation |
|---|---|
| One background thread per job | Each job runs in a single worker thread. The UI thread only polls a message queue every 100 ms and never performs heavy work. |
| One job at a time | While a job runs, starting another is blocked, so memory use never stacks. |
| One file at a time | Files in a batch are processed sequentially and are never held in memory together. |
| Immediate image closure | Images are opened with context managers and closed as soon as they are no longer needed. |
| Garbage collection between files | `gc.collect()` runs after every file and when a job ends. |
| Bounded input sizes | Large images are downscaled before OCR (3500 px), vectorization (2000 px), and icon generation (4096 px). |
| Bounded UI queue | The queue column renders 20 rows at a time. Further rows are added only when the user selects "Show 20 more". |
| Cooperative cancellation | A Cancel control sets an event that workers check between files and between icon sizes. |
| Failure isolation | An error on one file is logged and the batch continues with the next file. |

### Threading Model

```
UI thread (CustomTkinter)                 Worker thread
-------------------------                 -------------
start_job() ----------------------------> worker(job, params)
poll queue every 100 ms  <--- queue ----- job.log / progress / file_status / result_text
update widgets                            job.cancel (Event) checked between files
```

Workers never touch widgets. All communication goes through the queue, which keeps the interface from freezing during heavy operations.

## User Interface

The interface is built with CustomTkinter and follows a fixed three-region layout.

| Region | Width | Role |
|---|---|---|
| Rail | 72 px, fixed | Icon-only navigation between the three tools, and the theme toggle |
| Queue column | 360 px, fixed | Header, queue actions, the list of selected images, and the pagination footer |
| Tool pane | Fluid, minimum 480 px | Tool options, the single primary action, progress, and output. Content width is capped at 720 px |

The minimum window size is 960 by 640 px.

Design characteristics:

- Light and dark themes. The theme is taken from the operating system at launch and can then be switched with the rail toggle.
- Flat surfaces separated by 1 px structural borders. No shadows, gradients, or animation.
- Corner radii between 0 and 4 px.
- Spacing restricted to the 4, 8, 16, 24, 32, and 48 px scale.
- Each queue row shows a pentagonal state indicator and a status chip: queued, working, done, or failed.
- A single primary action (Start) per view. All other controls are secondary.
- Keyboard focus is shown with a 2 px square outline.

## System Requirements

### Software

| Requirement | Version or note |
|---|---|
| Python | 3.8 or later |
| Tkinter | Included with most Python distributions; see the platform notes below |
| Tesseract OCR engine | Installed on the host and reachable on `PATH` |

### Python Packages

| Package | Purpose |
|---|---|
| `customtkinter` (5.2 or later) | Graphical interface |
| `pillow` | Image loading, resizing, and saving |
| `pytesseract` | Python interface to the Tesseract executable |
| `vtracer` | Raster to vector tracing |

### Hardware

MicroPixl targets machines with 4 GB of RAM. No GPU is required.

## Installation

### 1. Install the Tesseract OCR engine

`pytesseract` is only a wrapper. The Tesseract program itself must be installed separately.

| Platform | Command or source |
|---|---|
| Windows | Download the installer from the UB Mannheim Tesseract builds and enable any additional languages you need |
| macOS | `brew install tesseract` |
| Debian and Ubuntu | `sudo apt install tesseract-ocr` |
| Fedora | `sudo dnf install tesseract` |

Additional languages are installed separately. For example, on Debian and Ubuntu:

```bash
sudo apt install tesseract-ocr-ara tesseract-ocr-fra
```

Verify the installation:

```bash
tesseract --version
tesseract --list-langs
```

If `tesseract` is not on `PATH`, MicroPixl also checks these common locations:

- `C:\Program Files\Tesseract-OCR\tesseract.exe`
- `C:\Program Files (x86)\Tesseract-OCR\tesseract.exe`
- `/opt/homebrew/bin/tesseract`
- `/usr/local/bin/tesseract`
- `/usr/bin/tesseract`

On Debian and Ubuntu, Tkinter is packaged separately:

```bash
sudo apt install python3-tk
```

### 2. Clone the repository

```bash
git clone <repository-url>
cd micropixl
```

### 3. Create a virtual environment

Linux and macOS:

```bash
python3 -m venv .venv
source .venv/bin/activate
```

Windows (PowerShell):

```powershell
py -m venv .venv
.venv\Scripts\Activate.ps1
```

Windows (Command Prompt):

```bat
py -m venv .venv
.venv\Scripts\activate.bat
```

### 4. Install the Python dependencies

```bash
python -m pip install --upgrade pip
pip install "customtkinter>=5.2" pillow pytesseract vtracer
```

## Running the Application

With the virtual environment active:

```bash
python3 main.py
```

On Windows, use `python main.py` or `py main.py`.

## Usage

1. Select a tool from the rail: text extraction, raster to SVG, or app icons.
2. Select **Add images** in the queue column and choose one or more files. Each tool keeps its own queue.
3. Adjust the tool options in the tool pane.
4. For SVG and icon output, optionally choose an output folder.
5. Select **Start**. Progress is shown in the progress bar, and each queue row updates as its file moves through the states queued, working, done, or failed.
6. Select **Cancel** to stop after the current step.

Queue controls:

| Control | Behavior |
|---|---|
| Add images | Adds files to the queue. Duplicates are ignored |
| Remove | Removes the selected row. Select a row by clicking it |
| Clear | Empties the queue |
| Show 20 more | Renders the next page of queue rows |

Queue editing is disabled while a job is running.

## Local Fonts

MicroPixl is designed for IBM Plex Sans Arabic and IBM Plex Sans. To honor its offline requirement, fonts are never downloaded. Place static font files in a `fonts` directory next to `main.py`:

```
fonts/
    IBMPlexSans-Regular.ttf
    IBMPlexSans-Bold.ttf
    IBMPlexSansArabic-Regular.ttf
    IBMPlexSansArabic-Bold.ttf
```

Details:

- Both `.ttf` and `.otf` files are loaded at startup.
- Font selection order: IBM Plex Sans Arabic, then IBM Plex Sans, then the operating system default.
- Tkinter cannot use `.woff2` files or variable fonts. Use static font files.
- Tkinter provides only regular and bold weights, so medium and semibold weights render as bold.
- Runtime font loading is provided by CustomTkinter and does not work on every platform. If the bundled fonts are not applied, install them system-wide; MicroPixl will detect the installed family by name.

The application runs normally without the `fonts` directory.

## Directory Structure

```
micropixl/
    main.py        Application: backend workers, interface, and entry point
    README.md      Project documentation
    fonts/         Optional local font files (IBM Plex Sans, IBM Plex Sans Arabic)
```

Structure of `main.py`:

| Section | Contents |
|---|---|
| Backend constants | Size limits, icon presets, vtracer presets, pagination size |
| Design tokens | Light and dark color pairs, spacing scale, type scale |
| Helpers | Tesseract discovery, image flattening, size parsing, unique file names |
| `Job` | Thread-to-UI message channel and cancellation flag |
| UI primitives | Pentagon indicator, rail buttons, flat buttons, chips, form fields |
| `FileRow` | Queue row with state indicator and status chip |
| `ToolPage` | Shared layout for the queue column and tool pane |
| `OCRPage`, `SVGPage`, `IconPage` | One page per tool, each with its own worker function |
| `App` | Main window, rail, page switching, job control, and queue polling |

## Configuration Reference

These module-level constants in `main.py` control resource limits and defaults.

| Constant | Default | Meaning |
|---|---|---|
| `MAX_OCR_SIDE` | 3500 | Longest side, in pixels, above which images are downscaled before OCR |
| `MIN_OCR_SIDE` | 1000 | Longest side below which images are upscaled 2x before OCR |
| `MAX_SVG_SIDE` | 2000 | Longest side above which images are downscaled before tracing |
| `MAX_ICON_SOURCE` | 4096 | Longest side above which icon sources are downscaled once |
| `PAGE_SIZE` | 20 | Number of queue rows rendered per page |
| `PRESET_ICON_SIZES` | 16 to 1024 | Icon sizes offered as toggles |
| `DEFAULT_CHECKED` | 48, 72, 96, 128, 144, 152, 167, 180, 192, 256, 384, 512 | Icon sizes selected by default |
| `SVG_PRESETS` | Three presets | Keyword arguments passed to `vtracer` |

Lowering `MAX_OCR_SIDE` and `MAX_SVG_SIDE` reduces memory use further on very constrained machines, at some cost in detail.

## Troubleshooting

| Symptom | Cause and resolution |
|---|---|
| "Tesseract OCR engine was not found" | Install Tesseract and make sure it is on `PATH`, then restart MicroPixl. Confirm with `tesseract --version` |
| OCR returns an error mentioning a language | The requested language data is not installed. Check `tesseract --list-langs` and install the missing package |
| "The 'vtracer' package is not installed" | Run `pip install vtracer` inside the active virtual environment |
| `ModuleNotFoundError: No module named 'tkinter'` | Install the Tkinter package for your platform, for example `sudo apt install python3-tk` |
| The interface uses a generic font | The `fonts` directory is missing or the files are not static `.ttf` or `.otf` fonts. See [Local Fonts](#local-fonts) |
| A file is marked failed in the queue | The image could not be read or processed. The log or status line shows the reason, and the rest of the batch continues |
| OCR on a very large scan is slow | Lower `MAX_OCR_SIDE` or crop the image before processing |

## Known Limitations

- Only one job can run at a time.
- The queue is paginated, so rows beyond the current page are not displayed until requested. All queued files are still processed.
- Right-to-left interface mirroring is not implemented. Arabic text is supported as OCR input and output, subject to the fonts available on the system.
- The theme follows the operating system only at launch. Later system theme changes do not apply until the application is restarted or the rail toggle is used.
- Icon output is PNG, plus an optional multi-size `favicon.ico`. Other formats are not generated.
