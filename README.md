# Long to Shorts AI

Long to Shorts AI is a local web app called Shorts Studio. Use it in your
browser to turn long videos into subtitled vertical clips, find and mute spoken
sections, cut or merge videos, and rehearse a transcript with a word-by-word
teleprompter.

Speech recognition runs on your computer with `faster-whisper`. When you request
topic analysis, the signed-in Codex CLI sends the transcript to OpenAI to map
topics and suggest up to 10 self-contained clips. Most clips target 30–60 seconds
and can run up to 180 seconds to finish an idea. The app renders 1080 × 1920 MP4s
locally. **İçerik Stüdyosu** also uses Codex to generate Turkish scripts, editing
plans, and YouTube metadata.

## Quick Start

### Requirements

- Windows 10 or 11 on x64, or macOS 14 or newer on Apple Silicon or Intel
- An internet connection for setup, first model downloads, and Codex features
- Free disk space for work files and exports. When using **Kopyalayarak yükle**,
  allow roughly twice the upload size plus 2 GB if the temporary and project
  folders share a drive.

### 1. Get the project

Clone the repository in a terminal.

```sh
git clone https://github.com/TechGeeksClub/Long-to-Shorts-AI.git
cd Long-to-Shorts-AI
```

You can also choose **Code > Download ZIP** on GitHub and extract it. Open a
terminal in the extracted folder that contains `start.cmd` and `start.sh`.

### 2. Start Shorts Studio

**Windows**

Double-click `start.cmd` in File Explorer or run this in PowerShell.

```powershell
.\start.cmd
```

The launcher uses compatible Node.js and FFmpeg installations if available.
Otherwise it downloads portable copies into `.tools` inside the project.

**macOS**

```sh
bash start.sh
```

The launcher uses Homebrew to install Node.js and FFmpeg if needed. Homebrew
setup can ask for your Mac password and install the Xcode Command Line Tools.
The availability of prebuilt packages depends on your macOS version and
processor. [Homebrew installation requirements](https://docs.brew.sh/Installation)

Both launchers prepare `uv` and a managed Python 3.12 environment, install
dependencies from the lockfiles, and build the web interface. The first launch
can take several minutes.

The browser opens at [http://127.0.0.1:8000](http://127.0.0.1:8000) when the
server is ready. Leave the terminal open while you use the app. Press `Ctrl+C`
to stop it and use the same launch command to start it again. You can add
`--port 8001` to choose another port and `--no-browser` to open the URL yourself.

### Optional Codex sign-in

Topic analysis, transcript proofreading, Codex-generated YouTube metadata, and
**İçerik Stüdyosu** use the saved sign-in from your Codex CLI. Install the CLI if
needed and run `codex login` in a terminal. If Shorts Studio cannot find Codex,
set `LTS_CODEX_PATH` to the native CLI executable before launching the app.
[Codex CLI setup](https://developers.openai.com/codex/cli)

You can find and mute speech, cut and merge videos, and use **Canlı oku** with a
saved transcript before signing in.

## Features

- Fully local, CPU-based speech recognition
- Codex topic maps and clip generation from the entire transcript
- Source-derived boundaries, specific titles, and editorial explanations
- Reanalysis preserves manual clips, edits, hooks, effects, and exported files
- Context-aware transcript proofreading with an inspectable correction log
- Word-by-word teleprompter for saved transcripts and generated scripts
- Create a manual clip directly from any topic, including topics without suggestions
- Per-clip YouTube studio with selectable Codex titles/descriptions and designed frame thumbnails
- İçerik Stüdyosu for choosing a video outline and generating a Turkish script, edit plan, and YouTube SEO package
- Support for MP4, MOV, MKV, M4V, AVI, and WEBM files
- Browse a YouTube channel and import a selected video as a new project
- Editable subtitle text and timing
- Automatic detection and removal of non-speech sections
- Separate full-video Speech Cleanup workspace: detect spoken ranges, remove or
  restore their audio individually or all at once, and export a muted video
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
- Multiple videos per project with a shared active-video selector for every tool
- A separate Video Merge tab with ordered inputs, mixed-size video support,
  silent-source support, and selectable merged results
- Remove multiple time ranges from individual or merged videos, then export a
  single cut video or combine the remaining parts in one operation
- Fast cut exports copy the original video stream; optional precise exports
  cut at the selected times by re-encoding
- Per-clip spoken motovlog editing presets: Clean, Dynamic, and Cinematic
- A timed video/audio hook from anywhere in the source, with optional removal of its repeated appearance
- Separate hook-to-main, opening and closing transitions, including FFmpeg xfade
  dissolve, smooth wipe, circle reveal and pixel transitions
- A video effects panel with cinematic, vivid, warm, cool, monochrome and vintage
  color presets and adjustable intensity
- Editable opening text with suggestions quoted from the clip's saved transcript
- Short caption groups, restrained camera accents, and in-app playback of exports
- Modern subscription cards with channel identity, a red subscribe button, bell and fade/slide animations

## Usage

1. Select one or more local video files for a project, or use
   **YouTube'dan al** to choose a video from your channel.
2. Choose the video in **İşlem yapılacak video**, open **LongToShorts**, and choose
   **Codex ile konuları çıkar**. Wait for transcription and clip analysis to finish.
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

Use **Codex ile konuları çıkar** to analyze an existing video. Its cached
transcript is reused without running transcription or extracting audio again.
New suggestions are added alongside existing clips; previous work is retained.

Transcription runs on the CPU. The time it takes depends on your processor and
the length of the video.

### Live reading with Canlı oku

For an extracted transcript, select your video and choose **Canlı oku** next to
the transcript download in **LongToShorts** or **Konuşma Temizleme**. To create
a transcript in **Konuşma Temizleme**, use **Konuşmaları bul**. For a generated
script, choose **Canlı oku** in the **Konuşma metni** card in **İçerik Stüdyosu**.

Press **Başlat** and read the highlighted word as it advances. For example,
`ALİ topu at.` → `Ali TOPU at.` → `Ali topu AT.` The reader gives longer words
more time and pauses at punctuation. Set **Okuma hızı** between 80 and 200
words per minute, pause or resume at any time, and use the slider to jump to a
word.

### İçerik Stüdyosu

1. Open **İçerik Stüdyosu** and describe the video you want to make. Include
   your own experiences and footage you can shoot. Set a target length or
   leave it on **Otomatik** to use the duration in your notes or a six-minute default.
2. Optionally give a YouTube video link, upload an example video, or paste a
   reference transcript. For video inputs, FFmpeg and Whisper extract and
   transcribe the speech locally. Codex receives the brief and transcript text.
3. Ask for options. Compare three outlines by their openings, what viewers take
   away, what each approach requires, and how the scenes connect. Then select an outline.
4. Generate the package. Codex plans the scenes, then writes the Turkish script
   to fit the target length. Use each scene's spoken lines, visuals, cuts, audio,
   on-screen text, and transitions with the YouTube title and description suggestions.

### Multiple videos and merging

On Windows, use **Bilgisayardan seç** to link one or more videos from your
computer. On either platform, you can paste one absolute video path per line.
Use **Projeye video ekle** to add more videos later. Linked videos are read from
their original location. Keep the files in that location while using the project.
Transcripts, work files, and exports go in the project data directory.
**Kopyalayarak yükle** stores a separate project copy when you need one.

Each video retains its own transcription, clips, speech cleanup selections,
and exports. Adding local videos does not start a Codex analysis automatically.

The **İşlem yapılacak video** selector applies to both **LongToShorts** and
**Konuşma Temizleme**. You can use either tool on an individual video without
merging anything.

In the separate **Video Birleştirme** tab, select at least two videos, arrange
their playback order with the arrow buttons, and choose **Seçilen videoları
birleştir**. Matching MP4 or MOV sources can be merged without re-encoding their
video and audio. For sources with different formats or dimensions, the app fits
each video to the first video's frame without cropping and normalizes frame rate
and audio. Sources without audio receive silence when this conversion is needed.

Before exporting, set **Kayıt konumu** by typing the absolute path of an existing
folder or using **Klasör seç** on Windows. Leave it blank to use the project's
default storage.
The selected folder is remembered in this browser for that project. The app
writes the completed MP4 directly to the chosen folder with a unique filename;
it does not create a second video copy in the project folder.

When the output is ready, its full saved path appears in the result. Choose
**Dosyayı klasörde göster** to select the existing file in Windows File Explorer.
There is no need to download it again. **Kopyasını indir** is optional and saves
another copy through the browser. Completed files saved to a chosen folder are
preserved if you delete their project from the app.

Use **Bu videoyu kullan** to select the output for the other tools. It is added
as a new video within the same project; merging does not replace the currently
selected source or carry over its edits. You can switch back to any original
video at any time. Keep externally saved videos in place while using them in
the project.

To remove unwanted sections, open a selected video's cut editor in **Video
Birleştirme**. Scrub the preview and mark the beginning and end, or enter source
timestamps directly, then add the range to the cut list. Add as many ranges as
needed and remove a range from the list to restore it. You can export cuts from
one video alone, cut multiple inputs before combining them, or select an existing
merged result and cut it.

Cut exports default to **Hızlı**. This mode copies the original compressed video
without rendering every frame, preserving picture quality; audio may be encoded
once to keep the joins synchronized. Removed ranges expand to surrounding video
keyframes, so the actual cut may remove more than the selected interval. The
estimated remaining duration can therefore differ from the exported duration.
The time required depends on the source size and disk speed; an entire output
file still has to be written. Unsupported sources fail with a visible message
instead of silently starting a long video conversion.

Choose **Hassas** when the selected cut boundaries must be retained. This mode
re-encodes the remaining video and takes longer. Ordinary merges without cuts
keep their automatic compatibility conversion for mixed sources. Every output
is added as a new project video and can be previewed, downloaded as MP4, or
selected for further editing. Retrying a failed export retains its original
input order, cut ranges, export mode, and save location.

### Speech Cleanup

Open a project and choose **Konuşma Temizleme** beside **LongToShorts**.
Choose **Konuşmaları bul** to list spoken intervals with source timestamps and
their recognized text. Saved transcription is reused locally, including when
Codex topic analysis failed. If the initial project analysis is still running,
wait for it to finish before starting speech detection.

Use **Manuel aralık ekle** to add a section yourself, even before automatic
detection. Enter the start and end as seconds, `mm:ss`, or `hh:mm:ss`, or use
**Videodan al** to capture the current playback position. An optional note helps
identify the section. New manual ranges are selected for muting immediately;
use **Geri al** to keep their audio or **Sil** to remove the range. Manual ranges
and their selections survive rescanning and reopening the project.

Listen to any interval, mark its audio for muting, or mute every detected interval
with the bulk action. Restore individual intervals or the whole selection at
any time; choices are saved with the project. Preview the result, then choose
**Temizlenmiş videoyu dışa aktar**. The picture and video duration are preserved.
All audio inside selected intervals is silenced, including any background sound
within those intervals.

Muting starts 0.25 seconds before each selected interval and ends 0.4 seconds
after it to cover clipped initial and final sounds. Short gaps between selected
intervals are also silenced. The preview uses these same expanded ranges.
**Yeniden tara** adds a local acoustic speech pass to find short or quiet speech
that the transcript missed. Newly found speech without recognized text is shown
as **Konuşma (metin çözümlenemedi)**. Existing individual selections are retained;
if every detected interval was selected, newly detected intervals are selected too.
Export a new MP4 to apply these changes to previously generated videos.

For compatible H.264 or HEVC sources, the MP4 export copies the video stream
and encodes the edited audio. This keeps the original picture quality and avoids
re-encoding every frame. The new MP4 still needs free disk space close to the
source size. You can choose a different drive for speech cleanup exports by
setting `LTS_SPEECH_EXPORT_DIR` before starting the app:

```powershell
$env:LTS_SPEECH_EXPORT_DIR = "E:\ShortsStudio-Exports"
.\start.cmd
```

Outputs preserve the source frame dimensions and are listed in this workspace.
Speech selections and exports are independent of LongToShorts clips, subtitles,
and exports. The original video remains available. Detection uses recognized
speech timestamps; listen to the boundaries before exporting important edits.

### Importing from your YouTube channel

On the home screen, choose **YouTube'dan al**, paste your channel URL (for example,
`https://www.youtube.com/@your-channel`), and list its videos. Select a video to
download it as a new local project. The app then starts the same transcription
and Codex analysis used for uploaded files. The browser remains usable while
the background worker downloads the source; its progress appears in the project.

The channel browser loads 30 videos at a time; use **Daha fazla video göster**
to browse older uploads. You can also paste a direct video link to import an
unlisted video. Public channel browsing
does not require a YouTube API key or account connection. Private, members-only,
age-restricted, unavailable videos and current live streams may require access
the app cannot provide; it does not read browser cookies or sign in to YouTube.
Use your own videos or videos you have permission to process.

FFmpeg combines the downloaded video and audio locally. YouTube imports also
need Node.js 22+ in `PATH` for yt-dlp's JavaScript challenges; see the
[yt-dlp runtime requirements](https://github.com/yt-dlp/yt-dlp/wiki/EJS#node).
The original YouTube link is saved with the project. Failed downloads can be
retried from the project without creating a second project.

### Transcript proofreading and topic clips

After transcription, Codex checks for obvious recognition mistakes using the
surrounding conversation. It returns sparse single-word substitutions, preserving
all original word timestamps. The original transcript is saved as
`transcript.original.json`; the UI's **Altyazı kontrolü** section shows the before/
after text and reason for each change. Numbers and uncertain words are preserved.
This text-based proofread cannot establish what was spoken with certainty; review
the correction list when a name or phrase matters. Existing videos can run this
step with **Altyazıyı Codex ile düzelt**. Saved manual text that differs from its
recognized words is kept intact. Already rendered MP4 files require a new export.

Every topic card has **Bu konudan kesit oluştur**, including unselected topics.
This creates an editable manual clip covering that topic and opens the existing
clip editor. It does not require Codex to recommend the topic first.

### YouTube publishing package

Below each clip editor, **YouTube · Yayına hazırla** asks for audience, focus,
tone, channel name, frame time, thumbnail style, accent color and portrait or
landscape format. **Codex ile başlık, açıklama ve kapak yazısı üret** creates four
title choices, three description choices and four short thumbnail captions.
Choose or edit these options, then save to render a thumbnail from a real frame.
The three local layout templates are bold gradient, editorial card and diagonal
neon. No artificial people/events or third-party image provider is involved.

**Bu kesiti YouTube paketi olarak dışa aktar** uses the current export speed,
framing, captions, hook and effects for that clip only. The export folder includes
the MP4, `thumbnail.jpg`, `title.txt`, `description.txt`, `youtube.json`, existing
SEO Markdown and `youtube-package.zip`. The chosen YouTube settings are captured
when the export is queued; subsequent edits do not change that package. Normal
exports also receive a basic package if no YouTube choices have been saved.
The app prepares files for manual uploading; it does not publish to YouTube.

### Spoken Motovlog Editing

In a clip's editor, use **Klibine karakter kat** to configure a source hook,
opening/closing effects, and a caption/movement style. **Sade** uses shorter caption groups with a fixed picture;
Dynamic adds a small caption entrance and occasional centered punch-ins at
sentence boundaries; Cinematic uses a warm caption accent and a slow, gentle
zoom. Camera movement can be disabled separately.

Enable **Videodan hook bölümü ekle** and enter absolute source start/end times,
in seconds or `mm:ss` (also `hh:mm:ss`). The selected 0.3–15 second range plays
first, with its original audio and synchronized subtitles, followed by the main
clip. The default removes the matching portion from the main clip to avoid a
repeat; turn **Aynı bölümü ana klipte tekrar oynatma** off to keep it there too.
Existing manual cuts and inserted segments remain part of the main timeline.

Use **Hook → Ana video** to make the return from the hook visible. A 0.5-second
white flash is selected by default when a video hook is enabled; choose another
transition or disable it. Opening, hook-to-main and closing transitions have
separate selections and 0.1–2 second durations, measured in final output seconds
after playback speed changes. Very short clips cap transitions to the available
time. FFmpeg's local [xfade filter](https://ffmpeg.org/ffmpeg-filters.html#xfade)
provides dissolve, smooth wipe, circle reveal and pixel transitions alongside the
existing fades, zoom-blur, panel, diagonal and RGB effects. Transition blending
preserves the edited timeline duration and audio/subtitle synchronization.
**Seçilen geçişlerde sesi de yumuşat** fades selected opening/closing edges and
adds a brief, restrained volume dip at the hook transition.

In **Video efektleri**, select cinematic, vivid, warm, cool, monochrome or vintage
and adjust **Yoğunluk**. The selected color treatment applies across the clip,
including its hook, before subtitles and subscription cards are added. Choose
the original look to disable grading. The small color sample is illustrative;
**Kaydet ve önizle** renders the actual effect and all selected transitions.
Video effects and transitions also work with the **Orijinal** caption/movement
style. Everything renders locally with the existing FFmpeg installation; no
external account or paid effects service is required.

Enter an opening text of up to 90 characters, or select **Konuşmadan açılış öner**
for up to three short excerpts from the saved subtitles. Suggestions are local
text heuristics, so review transcription errors and context before choosing one.
The opening is a text overlay, displayed for 1–5 seconds of the final output;
it is independent of the video hook range and does not generate speech. Leave
it empty to omit the overlay.

Select **Kurgu ayarlarını kaydet**, then export the clip. Export requests save a
snapshot of these editing settings. Existing clips default to **Orijinal** with
no hook or edge effects. **Kaydet ve önizle** saves the settings and queues a real
540×960 render of the entire edited clip using the current export speed, font,
subtitle position, and framing settings. The preview appears in the clip editor
and export list; normal MP4 exports remain 1080×1920 and include SEO metadata.
**Videoyu izle** under an export plays that rendered result. The separate text
sample only illustrates caption styling. Editing preserves original files and
does not automatically remove non-speaking riding segments.

### Subscription cards

In **Klibine karakter kat**, enable **Aboneliği teşvik et**. The modern dark card
includes the channel photo, channel name (default **Oban Otağı**), a red **Abone Ol**
button and a bell icon. The supplied photo is bundled locally in `app/assets` and
cropped around both faces at render time; the original JPEG remains unchanged.
Disable **Kanal fotoğrafını kullan** to use initials instead. A single local renderer
draws the photograph, lettering and animation for both the editor and MP4 output.
Button text and icons are measured together and centered in each state. The whole
card uses equal left and right margins in the 9:16 video.

**Abone Ol ve zil tıklaması** animates a cursor clicking the subscribe button,
switches it to **Abone Olundu**, then clicks and shakes the bell. The sequence adapts
to the chosen duration; 4–5 seconds is recommended. Use **Animasyonu tekrar oynat**
to replay it in the editor. The editor holds the completed state so the card remains
visible; the exported video fades it out at the selected end time. **Yerleşimi göster**
adds a small 9:16 diagram with the centerline and subtitle area.

An optional supporting line is available under **Alt satırı özelleştir**. Existing
long subscription messages stay hidden until that option is enabled. Codex can
suggest three supporting lines and times using the saved transcript, cuts, inserted
segments, current hook and export speed. The card does not generate speech.

Start and duration use seconds in the final output, after playback speed changes.
Choose a soft fade or a slight upward slide. **Altyazıların üstü · önerilen** keeps
the centered card above the captions, with space around the player controls. **Üst
bölüm** is available when it better fits the shot. Review faces and opening text in
the real preview. If a shortened clip cannot fit the requested timing, the
card is moved earlier and its duration is limited to the available video.

Use **Kurgu ayarlarını kaydet** or **Kaydet ve önizle**. The same overlay is included
in previews, regular MP4 exports and YouTube packages. Queued exports retain their
saved card settings. Existing clips default to disabled.

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

1. Whisper produces local word timestamps. The app builds stable, timed
   utterance units without discarding transcript text.
2. Codex reads the complete transcript and maps chronological topics, each with
   a title, summary, and source unit boundaries. Very long transcripts are read
   in consecutive parts, then their topic maps are unified.
3. Codex proposes complete question/answer pairs, explanations, comparisons,
   and stories directly from those topics. There is no heuristic candidate pool
   and no fabricated numerical performance score.
4. The app validates every source ID, topic coverage, duration, and boundary.
   Invalid structured plans get one correction attempt. Words are mapped back
   to their original Whisper timestamps, with small pause-limited padding.
5. Long-transcript suggestions receive a final editorial selection across all
   parts. Repetitive/overlapping clips are removed. A topic can have no useful
   clip; the model is not forced to fill a quota.
6. The UI shows the topic map, current suggestions, and saved older/manual clips.
   Existing IDs, edits, exports and selections remain intact. Repeated identical
   suggestions do not overwrite an edited clip or create duplicate IDs.

Codex availability, login, network, usage-limit, timeout and plan-validation
failures are visible. The app does **not** silently switch back to heuristic
clips. A saved transcript and previous work remain available for retry.

The runner uses `codex exec` with the saved Codex login, an isolated ephemeral
directory, structured JSON, read-only sandboxing, and disabled tools/connectors.
It does not inherit user/project configuration. With no `LTS_CODEX_MODEL`, the
CLI chooses its own default model; the app does not claim it is this chat's model.

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
        ├── analysis/
        │   └── agent-plan.json
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

## Optional SEO with Ollama

Ollama is optional and is used for enhanced SEO suggestions. Topic segmentation
and automatic clip generation use Codex, independently of the SEO toggle.

The **Professional LLM SEO** option controls only enhanced SEO generation.
When it is disabled, the application still creates local SEO suggestions
without an LLM.

For enhanced SEO, the application connects to Ollama at
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
.\start.cmd
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
| `LTS_CODEX_PATH` | Auto-detect | Native Codex executable; desktop app and PATH installations are detected |
| `LTS_CODEX_MODEL` | CLI default | Optional explicit Codex model; requires access through the signed-in account |
| `LTS_CODEX_TIMEOUT_SECONDS` | `600` | Maximum wait for each Codex analysis step |
| `LTS_CODEX_TRANSCRIPT_CHUNK_CHARS` | `100000` | Full transcripts exceeding this size are analyzed in consecutive parts |
| `LTS_AGENT_MIN_CLIP_SECONDS` | `8` | Minimum proposed duration, except complete speech in a shorter video |
| `LTS_AGENT_MAX_CLIP_SECONDS` | `180` | Maximum proposed duration; manual clip ranges remain independent |
| `LTS_TRANSCRIPT_CORRECTION` | `1` | Set to `0` to skip automatic Codex proofreading during analysis |
| `OLLAMA_TIMEOUT_SECONDS` | `180` | Number of seconds to wait for an Ollama response |

## Development

On Windows, complete the initial setup with `start.cmd` and ensure Node.js
and FFmpeg are available in your development shell's `PATH`. Start the
development servers with the following command.

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
npm.cmd run lint
npm.cmd run build
```

## Troubleshooting

### `FFmpeg was not found in PATH`

Start the app using the launcher for your operating system so it can set up
FFmpeg. If you set `FFMPEG_PATH` or `FFPROBE_PATH`, check that they point to
working executables. Clip exports need an FFmpeg build with the ASS subtitle
filter and `libx264` encoder.

### The first analysis fails while downloading a model

The Whisper model is downloaded during the first transcription. The YuNet model
is downloaded when face tracking is used for the first time. Check your internet
connection and write access to the `data/` directory, then try again.

### Analysis is slower than expected

Transcription uses CPU-based `int8` computation. You can use a smaller model:

```powershell
$env:WHISPER_MODEL = "base"
.\start.cmd
```

Smaller models may run faster but can reduce transcription accuracy.

### Port 8000 is already in use

If Shorts Studio is already running, open the existing instance. If a different
app is using port `8000`, run `bash start.sh --port 8001` on macOS or
`.\start.cmd --port 8001` on Windows.

## Privacy and Security

- Video and audio processing, transcription, live reading, and rendered files
  stay on this computer.
- Topic analysis sends transcript text and its source timings to OpenAI through
  the signed-in Codex CLI, using that account's access and usage limits.
- **İçerik Stüdyosu** sends your brief and any reference transcript to OpenAI
  through the same CLI.
- Enhanced SEO sends selected clip text to the configured Ollama server when
  its interface option is enabled. The default address is local.
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
