"""
Render Agent - Turns the generated production package into a final MP4.

Input: storyboard.json + creative_brief.json + captions/manifest artifacts
Output: final_video.mp4 + render_manifest.json + generated scene media
"""
import json
import math
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


WIDTH = 1920
HEIGHT = 1080
OUTPUT_WIDTH = 1280
OUTPUT_HEIGHT = 720
FPS = 12
MIN_SCENE_SECONDS = 2.5
SCENE_TAIL_SECONDS = 0.35
TRANSITION_SECONDS = 0.45


STYLE_PROFILES = {
    "cartoon": {
        "background": ("#fef3c7", "#93c5fd"),
        "accent": "#ef4444",
        "ink": "#111827",
        "panel": "#ffffff",
        "motif": "bubbles",
    },
    "cinematic": {
        "background": ("#020617", "#3f1d38"),
        "accent": "#f59e0b",
        "ink": "#f8fafc",
        "panel": "#111827",
        "motif": "letterbox",
    },
    "documentary": {
        "background": ("#1f2937", "#92400e"),
        "accent": "#facc15",
        "ink": "#f9fafb",
        "panel": "#111827",
        "motif": "grain",
    },
    "educational": {
        "background": ("#0f766e", "#1d4ed8"),
        "accent": "#fbbf24",
        "ink": "#f8fafc",
        "panel": "#0f172a",
        "motif": "grid",
    },
    "social": {
        "background": ("#7c3aed", "#db2777"),
        "accent": "#22d3ee",
        "ink": "#ffffff",
        "panel": "#111827",
        "motif": "rays",
    },
    "professional": {
        "background": ("#0f172a", "#334155"),
        "accent": "#38bdf8",
        "ink": "#f8fafc",
        "panel": "#111827",
        "motif": "grid",
    },
}


def load_json(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save_json(path: Path, data: dict):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def infer_profile(storyboard: dict, brief: dict) -> str:
    text = " ".join([
        storyboard.get("title", ""),
        brief.get("topic", ""),
        brief.get("tone", ""),
        brief.get("platform", ""),
        brief.get("visual_style", ""),
    ]).lower()

    if any(word in text for word in ("cartoon", "anime", "animated", "kids", "children")):
        return "cartoon"
    if any(word in text for word in ("cinematic", "movie", "film", "trailer", "drama")):
        return "cinematic"
    if any(word in text for word in ("documentary", "docuseries", "investigation", "archive")):
        return "documentary"
    if any(word in text for word in ("educational", "tutorial", "course", "lesson", "explainer")):
        return "educational"
    if any(word in text for word in ("tiktok", "instagram", "shorts", "reels", "vertical")):
        return "social"
    return "professional"


def hex_to_rgb(value: str) -> tuple[int, int, int]:
    value = value.lstrip("#")
    return tuple(int(value[i:i + 2], 16) for i in (0, 2, 4))


def interpolate(a: int, b: int, t: float) -> int:
    return int(a + (b - a) * t)


def draw_gradient(draw: ImageDraw.ImageDraw, start: str, end: str):
    rgb_a = hex_to_rgb(start)
    rgb_b = hex_to_rgb(end)
    for y in range(HEIGHT):
        t = y / max(1, HEIGHT - 1)
        color = tuple(interpolate(rgb_a[i], rgb_b[i], t) for i in range(3))
        draw.line([(0, y), (WIDTH, y)], fill=color)


def font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "C:/Windows/Fonts/arialbd.ttf" if bold else "C:/Windows/Fonts/arial.ttf",
    ]
    for candidate in candidates:
        if Path(candidate).exists():
            return ImageFont.truetype(candidate, size=size)
    return ImageFont.load_default()


def text_width(draw: ImageDraw.ImageDraw, value: str, face: ImageFont.ImageFont) -> int:
    box = draw.textbbox((0, 0), value, font=face)
    return box[2] - box[0]


def wrap_text(draw: ImageDraw.ImageDraw, value: str, face: ImageFont.ImageFont, max_width: int) -> list[str]:
    lines = []
    for paragraph in value.splitlines() or [value]:
        current = ""
        for word in paragraph.split():
            candidate = f"{current} {word}".strip()
            if current and text_width(draw, candidate, face) > max_width:
                lines.append(current)
                current = word
            else:
                current = candidate
        if current:
            lines.append(current)
    return lines or [""]


def draw_motif(draw: ImageDraw.ImageDraw, profile: dict):
    accent = hex_to_rgb(profile["accent"])
    motif = profile["motif"]

    if motif == "letterbox":
        draw.rectangle((0, 0, WIDTH, 110), fill=(0, 0, 0))
        draw.rectangle((0, HEIGHT - 110, WIDTH, HEIGHT), fill=(0, 0, 0))
    elif motif == "grid":
        for x in range(0, WIDTH, 120):
            draw.line((x, 0, x, HEIGHT), fill=(*accent, 35), width=1)
        for y in range(0, HEIGHT, 120):
            draw.line((0, y, WIDTH, y), fill=(*accent, 35), width=1)
    elif motif == "bubbles":
        for i in range(12):
            r = 70 + i * 8
            x = 130 + (i * 173) % (WIDTH - 260)
            y = 120 + (i * 97) % (HEIGHT - 240)
            draw.ellipse((x, y, x + r, y + r), outline=(*accent, 95), width=6)
    elif motif == "rays":
        center = (WIDTH // 2, HEIGHT // 2)
        for angle in range(0, 360, 18):
            rad = math.radians(angle)
            end = (center[0] + int(math.cos(rad) * WIDTH), center[1] + int(math.sin(rad) * WIDTH))
            draw.line((center, end), fill=(*accent, 45), width=8)
    elif motif == "grain":
        for i in range(0, WIDTH, 36):
            draw.line((i, 0, i + HEIGHT, HEIGHT), fill=(*accent, 18), width=2)


def draw_centered_lines(
    draw: ImageDraw.ImageDraw,
    lines: list[str],
    face: ImageFont.ImageFont,
    y: int,
    fill: str,
    spacing: int,
):
    for line in lines:
        box = draw.textbbox((0, 0), line, font=face)
        width = box[2] - box[0]
        height = box[3] - box[1]
        draw.text(((WIDTH - width) / 2, y), line, font=face, fill=fill)
        y += height + spacing
    return y


def create_scene_card(scene: dict, storyboard: dict, brief: dict, profile_name: str, path: Path):
    profile = STYLE_PROFILES[profile_name]
    image = Image.new("RGB", (WIDTH, HEIGHT), "#000000")
    draw = ImageDraw.Draw(image, "RGBA")
    draw_gradient(draw, *profile["background"])
    draw_motif(draw, profile)

    accent = profile["accent"]
    ink = profile["ink"]
    panel = hex_to_rgb(profile["panel"])

    scene_number = scene.get("scene_number", 0)
    chapter = scene.get("chapter", "")
    narration = scene.get("narration", "")
    visual = scene.get("visual_description", "")
    overlay = scene.get("text_overlay") or chapter or storyboard.get("title", "Solo Studio")

    # Main content panel.
    draw.rounded_rectangle((180, 175, WIDTH - 180, HEIGHT - 170), radius=36, fill=(*panel, 210))
    draw.rectangle((180, 175, WIDTH - 180, 190), fill=accent)

    eyebrow = f"Scene {scene_number:02d} / {profile_name.title()} Production"
    draw.text((230, 225), eyebrow, font=font(34, bold=True), fill=accent)

    title_lines = wrap_text(draw, overlay.upper(), font(72, bold=True), WIDTH - 520)
    y = draw_centered_lines(draw, title_lines[:3], font(72, bold=True), 310, ink, 18)

    narration_lines = wrap_text(draw, narration, font(42), WIDTH - 560)
    y += 45
    draw_centered_lines(draw, narration_lines[:5], font(42), y, ink, 14)

    visual_summary = textwrap.shorten(visual, width=160, placeholder="...")
    visual_lines = wrap_text(draw, visual_summary, font(28), WIDTH - 560)
    y2 = HEIGHT - 290
    draw.text((230, y2), "Visual direction", font=font(26, bold=True), fill=accent)
    draw_centered_lines(draw, visual_lines[:3], font(28), y2 + 44, ink, 10)

    image.save(path, quality=95)


def run(cmd: list[str], cwd: Path | None = None):
    result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"Command failed: {' '.join(cmd)}\n{result.stderr[-1200:]}")
    return result


def require_ffmpeg() -> str:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg is required to render final_video.mp4 but was not found on PATH")
    return ffmpeg


def require_ffprobe() -> str:
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        raise RuntimeError("ffprobe is required to time generated narration but was not found on PATH")
    return ffprobe


def probe_duration(ffprobe: str, path: Path) -> float:
    result = run([
        ffprobe,
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(path),
    ])
    try:
        return max(0.0, float(result.stdout.strip()))
    except ValueError:
        return 0.0


def generate_scene_audio(ffmpeg: str, ffprobe: str, scene: dict, planned_duration: float, path: Path) -> float:
    narration = scene.get("narration", "").strip()
    tts = shutil.which("espeak-ng") or shutil.which("espeak")
    if narration and tts:
        text_path = path.with_suffix(".txt")
        text_path.write_text(narration, encoding="utf-8")
        run([
            tts,
            "-v", "en-us",
            "-w", str(path),
            "-s", "138",
            "-p", "38",
            "-a", "135",
            "-g", "7",
            "-f", str(text_path),
        ])
        duration = probe_duration(ffprobe, path)
        return max(MIN_SCENE_SECONDS, duration + SCENE_TAIL_SECONDS)

    run([
        ffmpeg,
        "-y",
        "-hide_banner",
        "-loglevel", "error",
        "-f", "lavfi",
        "-i", "anullsrc=r=48000:cl=stereo",
        "-t", f"{planned_duration:.3f}",
        str(path),
    ])
    return planned_duration


def create_scene_clip(ffmpeg: str, image_path: Path, audio_path: Path, duration: float, output_path: Path):
    fade = min(0.30, duration / 5)
    fade_out = max(0, duration - fade)
    run([
        ffmpeg,
        "-y",
        "-hide_banner",
        "-loglevel", "error",
        "-loop", "1",
        "-framerate", "1",
        "-t", f"{duration:.3f}",
        "-i", str(image_path),
        "-i", str(audio_path),
        "-filter_complex",
        (
            f"[0:v]scale={OUTPUT_WIDTH}:{OUTPUT_HEIGHT},fps={FPS},format=yuv420p,"
            f"fade=t=in:st=0:d={fade:.3f},fade=t=out:st={fade_out:.3f}:d={fade:.3f}[v];"
            f"[1:a]loudnorm=I=-18:TP=-2:LRA=11,afade=t=in:st=0:d=0.080,"
            f"afade=t=out:st={max(0, duration - 0.18):.3f}:d=0.180,apad[a]"
        ),
        "-map", "[v]",
        "-map", "[a]",
        "-t", f"{duration:.3f}",
        "-c:v", "libx264",
        "-preset", "ultrafast",
        "-tune", "stillimage",
        "-crf", "30",
        "-c:a", "aac",
        "-b:a", "96k",
        "-movflags", "+faststart",
        str(output_path),
    ])


def concat_clips_copy(ffmpeg: str, clips: list[Path], output_path: Path):
    list_path = output_path.parent / "clips" / "concat.txt"
    with open(list_path, "w", encoding="utf-8") as f:
        for clip in clips:
            safe = clip.resolve().as_posix().replace("'", "'\\''")
            f.write(f"file '{safe}'\n")

    run([
        ffmpeg,
        "-y",
        "-hide_banner",
        "-loglevel", "error",
        "-f", "concat",
        "-safe", "0",
        "-i", str(list_path),
        "-c", "copy",
        str(output_path),
    ])


def concat_clips(ffmpeg: str, clips: list[Path], durations: list[float], output_path: Path):
    if len(clips) == 1:
        shutil.copyfile(clips[0], output_path)
        return

    transition = min(TRANSITION_SECONDS, min(durations) / 4)
    cmd = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error"]
    for clip in clips:
        cmd.extend(["-i", str(clip)])

    filters = []
    for idx in range(len(clips)):
        filters.append(f"[{idx}:v]setpts=PTS-STARTPTS[v{idx}]")
        filters.append(f"[{idx}:a]asetpts=PTS-STARTPTS[a{idx}]")

    video_label = "v0"
    audio_label = "a0"
    elapsed = durations[0]
    for idx in range(1, len(clips)):
        next_video = f"vx{idx}"
        next_audio = f"ax{idx}"
        offset = max(0, elapsed - transition)
        filters.append(
            f"[{video_label}][v{idx}]xfade=transition=fade:duration={transition:.3f}:offset={offset:.3f}[{next_video}]"
        )
        filters.append(
            f"[{audio_label}][a{idx}]acrossfade=d={transition:.3f}:c1=tri:c2=tri[{next_audio}]"
        )
        video_label = next_video
        audio_label = next_audio
        elapsed += durations[idx] - transition

    cmd.extend([
        "-filter_complex", ";".join(filters),
        "-map", f"[{video_label}]",
        "-map", f"[{audio_label}]",
        "-c:v", "libx264",
        "-preset", "ultrafast",
        "-crf", "30",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        "-b:a", "96k",
        "-movflags", "+faststart",
        str(output_path),
    ])

    try:
        run(cmd)
    except RuntimeError:
        concat_clips_copy(ffmpeg, clips, output_path)


def render_video(storyboard_path: Path, output_dir: Path) -> dict:
    ffmpeg = require_ffmpeg()
    ffprobe = require_ffprobe()
    storyboard = load_json(storyboard_path)
    brief_path = output_dir / "creative_brief.json"
    brief = load_json(brief_path) if brief_path.exists() else {}
    profile_name = infer_profile(storyboard, brief)

    renders_dir = output_dir / "renders"
    clips_dir = output_dir / "clips"
    audio_dir = output_dir / "audio"
    renders_dir.mkdir(parents=True, exist_ok=True)
    clips_dir.mkdir(parents=True, exist_ok=True)
    audio_dir.mkdir(parents=True, exist_ok=True)

    clips = []
    durations = []
    rendered_scenes = []
    scenes = storyboard.get("scenes", [])
    if not scenes:
        raise RuntimeError("storyboard.json does not contain any scenes to render")

    for scene in scenes:
        scene_number = scene.get("scene_number", len(clips) + 1)
        planned_duration = max(MIN_SCENE_SECONDS, float(scene.get("duration_seconds", 5.0)))
        image_path = renders_dir / f"scene_{scene_number:02d}.png"
        audio_path = audio_dir / f"scene_{scene_number:02d}.wav"
        clip_path = clips_dir / f"scene_{scene_number:02d}.mp4"

        create_scene_card(scene, storyboard, brief, profile_name, image_path)
        duration = generate_scene_audio(ffmpeg, ffprobe, scene, planned_duration, audio_path)
        create_scene_clip(ffmpeg, image_path, audio_path, duration, clip_path)

        clips.append(clip_path)
        durations.append(duration)
        rendered_scenes.append({
            "scene_number": scene_number,
            "planned_duration_seconds": planned_duration,
            "rendered_duration_seconds": duration,
            "image": str(image_path.relative_to(output_dir)),
            "audio": str(audio_path.relative_to(output_dir)),
            "clip": str(clip_path.relative_to(output_dir)),
        })

    final_video = output_dir / "final_video.mp4"
    concat_clips(ffmpeg, clips, durations, final_video)

    manifest = {
        "title": storyboard.get("title", "Untitled"),
        "profile": profile_name,
        "source_resolution": f"{WIDTH}x{HEIGHT}",
        "resolution": f"{OUTPUT_WIDTH}x{OUTPUT_HEIGHT}",
        "fps": FPS,
        "final_video": final_video.name,
        "scenes": rendered_scenes,
        "notes": [
            "This renderer creates a complete stitched MP4 from generated scene cards.",
            "Scene timing follows generated narration to avoid dead air before transitions.",
            "Soft fades, audio normalization, and crossfades are applied between scenes.",
            "Provider-backed image, voice, and video generation can replace these local assets later.",
        ],
    }
    save_json(output_dir / "render_manifest.json", manifest)
    return manifest


def main():
    if len(sys.argv) < 2:
        print("Usage: python render_agent.py <storyboard.json> [output_dir]")
        sys.exit(1)

    storyboard_path = Path(sys.argv[1])
    output_dir = Path(sys.argv[2]) if len(sys.argv) > 2 else storyboard_path.parent

    try:
        manifest = render_video(storyboard_path, output_dir)
    except Exception as exc:
        print(f"Render failed: {exc}")
        sys.exit(1)

    print(f"Final video: {output_dir / manifest['final_video']}")
    print(f"  Profile: {manifest['profile']}")
    print(f"  Scenes: {len(manifest['scenes'])}")


if __name__ == "__main__":
    main()
