# Long to Shorts AI

A local AI-powered web application that turns long videos into subtitled,
vertical short-form clips between 30 and 60 seconds.

The application transcribes speech locally with `faster-whisper`, scores and
suggests up to 10 strong moments, and exports selected clips as 1080x1920 MP4
videos. In the default workflow, your videos and transcripts are not sent to
external services.

## Features

- Fully local, CPU-based speech recognition
- Hybrid highlight selection using heuristic candidate generation, semantic
  reranking, deterministic boundary optimization, and a final critic pass
- Support for MP4, MOV, MKV, M4V, AVI, and WEBM files
- Editable subtitle text and timing
- Automatic detection and removal of non-speech sections
- Insert any source-video range at a chosen point in another clip
- Manual clip creation using custom start and end times
- Three vertical framing modes:
  - **Fit:** Preserves the full image over a blurred background
  - **Balanced:** Enlarges the image while keeping some space above and below
  - **Fill:** Crops the sides to fill the entire 9:16 frame
- Automatic face tracking in Fill mode
- Configurable export speed from 1.00x to 1.50x
- Configurable subtitle position and font
- A YouTube SEO Markdown file for every exported clip
- Optional Ollama integration for enhanced SEO suggestions
- A local web interface for managing videos, clips, and exports

## Requirements

- Windows 10 or Windows 11
- Node.js 20.19 or later
- FFmpeg and FFprobe available in `PATH`
- At least 2 GB of free disk space in addition to the source video size
- An internet connection for the initial setup and first model downloads

You do not need to install Python separately. The startup script automatically
prepares `uv` and a managed Python 3.12 environment.

> [!NOTE]
> Transcription runs on the CPU. Processing long videos may take some time
> depending on your processor and the length of the video.

## Quick Start

### 1. Download the repository

Using Git:

```powershell
git clone https://github.com/TechGeeksClub/Long-to-Shorts-AI.git
cd Long-to-Shorts-AI
```

Alternatively, select **Code > Download ZIP** on GitHub and extract the archive.

### 2. Verify the requirements

Open a new PowerShell window and make sure these commands work:

```powershell
node --version
npm --version
ffmpeg -version
ffprobe -version
```

If you have just installed FFmpeg, close and reopen the terminal so Windows can
detect the updated `PATH`.

### 3. Start the application

Run the following command from the project directory:

```powershell
.\start.ps1
```

On the first run, the script:

1. Downloads the pinned `uv` version into the project.
2. Creates a Python 3.12 environment and installs the Python dependencies.
3. Installs the frontend dependencies and builds the web interface.
4. Opens the application at `http://127.0.0.1:8000`.

Press `Ctrl+C` in the PowerShell window to stop the application.

If PowerShell prevents scripts from running, use the following command to bypass
the execution policy for this run only:

```powershell
powershell -ExecutionPolicy Bypass -File .\start.ps1
```

## Usage

1. Upload a supported video file from the home screen.
2. Wait for transcription and clip analysis to finish.
3. Select the suggested clips you want to export.
4. Adjust clip boundaries, subtitles, removed ranges, and inserted timeline
   segments if needed.
5. Choose Fit, Balanced, or Fill framing. Face tracking is available only in
   Fill mode.
6. Configure the playback speed, subtitle position, font, and other export
   settings.
7. Select **Export selected clips**.
8. When processing is complete, download the files using the **MP4** and
   **SEO MD** buttons.

Use **Reanalyze topics** to run the latest topic segmentation logic on an
existing video.

### Inserting a Source Segment

Use **Add segment from another time** in the clip editor to splice a section
from the same source video into the active clip. All three values use absolute
source-video timestamps.

For example:

- Source start: `01:20`
- Source end: `01:30`
- Insert before: `10:44`

When the active clip covers `10:30–11:30`, the exported order becomes:

```text
10:30–10:44 → 01:20–01:30 → 10:44–11:30
```

The operation is non-destructive: it does not remove the original source
section. Inserted segments are composed during export, and their transcript
subtitles are shifted to the new output timeline automatically.

## How Clip Selection Works

1. Whisper produces a word-timestamped transcript. The application assigns a
   stable ID and sentence ID to every word.
2. The heuristic engine creates a broad candidate pool using complete
   sentences, topic boundaries, pauses, vocabulary changes, speech density,
   and audio energy. Up to 60 top candidates continue to semantic reranking.
3. If a compatible Ollama model is installed, candidates are scored for hook,
   standalone clarity, context, and payoff. The model selects word IDs rather
   than estimating timestamps.
4. Content and clip-integrity scores are calculated separately and combined
   with the heuristic score.
5. The boundary optimizer converts selected word IDs to exact timestamps, snaps
   cuts to natural pauses, and adds safe start/end padding.
6. A critic pass checks unresolved references and incomplete endings. The final
   selector removes overlaps and keeps clips from different parts of the video.

## Outputs and Local Data

By default, the application stores its working data in the following structure:

```text
data/
├── app.db
├── hf_cache/
├── models/
└── jobs/
    └── <job-id>/
        ├── <source-video>
        ├── transcript.json
        └── exports/
            └── short-*/
                ├── short-*.mp4
                └── youtube-seo.md
```

Each export produces:

- A 1080x1920 MP4 video
- A `youtube-seo.md` file containing title, description, tag, hashtag, thumbnail
  text suggestions, and the clip transcript

Deleting a project from the interface permanently removes its source video,
transcript, and generated exports.

## Semantic Clip Selection and SEO with Ollama

Ollama is optional. When a compatible local model is available, the application
uses it to evaluate candidate clips for meaning, standalone clarity, context,
and payoff. The model selects exact word IDs instead of timestamps; the
application then converts those IDs to deterministic Whisper timestamps and
snaps the boundaries to natural pauses.

If Ollama or a compatible model is unavailable, clip analysis automatically
falls back to the heuristic selector without failing the job.

The **Professional LLM SEO** option controls only enhanced SEO generation.
When it is disabled, the application still creates local SEO suggestions
without an LLM.

For semantic selection and enhanced SEO, the application connects to Ollama at
`http://127.0.0.1:11434` by default and first attempts to use `qwen2.5:7b`.
If that model is unavailable, it can use an installed `llama3.1:8b` or
`llama3.2:3b` model.

Recommended model:

```powershell
ollama pull qwen2.5:7b
```

Lower-resource alternative:

```powershell
ollama pull llama3.2:3b
```

If Ollama is unavailable, the video export still completes and the SEO file
uses locally generated fallback suggestions.

## Configuration

You can customize the application with PowerShell environment variables before
starting it:

```powershell
$env:WHISPER_MODEL = "base"
$env:LTS_CANDIDATE_COUNT = "6"
.\start.ps1
```

| Variable | Default | Description |
| --- | --- | --- |
| `LTS_DATA_DIR` | `./data` | Local directory for the database, jobs, and models |
| `LTS_HF_CACHE_DIR` | `./data/hf_cache` | Hugging Face and Whisper model cache |
| `LTS_CANDIDATE_COUNT` | `10` | Maximum number of suggested clips |
| `WHISPER_MODEL` | `small` | faster-whisper model to use |
| `FFMPEG_PATH` | `ffmpeg` | FFmpeg executable name or full path |
| `FFPROBE_PATH` | `ffprobe` | FFprobe executable name or full path |
| `OLLAMA_BASE_URL` | `http://127.0.0.1:11434` | Ollama API address |
| `OLLAMA_SEO_MODEL` | `qwen2.5:7b` | Preferred Ollama model for SEO generation |
| `OLLAMA_SEO_ENABLED` | `1` | Set to `0` to disable LLM-based SEO completely |
| `LTS_SEMANTIC_CLIP_ENABLED` | `1` | Set to `0` to use heuristic clip selection only |
| `LTS_SEMANTIC_CLIP_MODEL` | `qwen2.5:7b` | Preferred Ollama model for semantic clip evaluation |
| `LTS_SEMANTIC_CANDIDATES` | `60` | Number of heuristic candidates sent for semantic reranking, up to 100 |
| `OLLAMA_TIMEOUT_SECONDS` | `180` | Number of seconds to wait for an Ollama response |

## Development

Complete the initial setup with `start.ps1` at least once, then start the
development servers with:

```powershell
.\dev.ps1
```

- Frontend: `http://127.0.0.1:5173`
- API: `http://127.0.0.1:8000`
- Swagger API documentation: `http://127.0.0.1:8000/docs`

Main technologies:

- FastAPI and Uvicorn
- faster-whisper
- FFmpeg
- OpenCV YuNet
- React, TypeScript, and Vite
- SQLite

### Tests

Run the backend tests:

```powershell
.\.tools\uv\uv.exe run pytest
```

Run the frontend type check and production build:

```powershell
cd frontend
npm run lint
npm run build
```

## Troubleshooting

### `FFmpeg was not found in PATH`

Add the `bin` directory from your FFmpeg installation to the Windows `PATH`,
open a new terminal, and try `ffmpeg -version` again.

### The first analysis fails while downloading a model

The Whisper model is downloaded during the first transcription. The YuNet model
is downloaded when face tracking is used for the first time. Check your internet
connection and write access to the `data/` directory, then try again.

### Analysis is slower than expected

Transcription uses CPU-based `int8` computation. You can use a smaller model:

```powershell
$env:WHISPER_MODEL = "base"
.\start.ps1
```

Smaller models may run faster but can reduce transcription accuracy.

### Port 8000 is already in use

Close the application using port `8000`, then run `start.ps1` again.

## Privacy and Security

- Videos, audio, transcripts, and standard SEO outputs are processed locally.
- When semantic clip selection is enabled and a compatible model is installed,
  candidate transcript text is sent to the configured Ollama server. Enhanced
  SEO sends the selected clip text when its interface option is enabled. The
  default Ollama address points to the local machine.
- The server listens only on `127.0.0.1` by default and is not designed to be
  exposed directly to the internet.
- You are responsible for complying with copyright law and the terms of any
  platform where processed content is published.

## Contributing

Bug reports and feature requests are welcome through GitHub Issues. To
contribute code:

1. Fork the repository.
2. Create a branch for your change.
3. Run the tests.
4. Submit a Pull Request with a clear description.
