"""
Editing Agent — Script + Storyboard → Captions + Assembly Manifest.

Input: script.txt + storyboard.json
Output: captions.srt + assembly_manifest.json
"""
import json, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from package_utils import atomic_write_json, atomic_write_text, read_json_artifact


def load_storyboard(path: str) -> dict:
    return read_json_artifact(path)


def generate_captions_srt(storyboard: dict) -> str:
    """Generate SRT subtitle file from the script."""
    lines = []
    counter = 1
    current_time = 0.0

    for scene in storyboard.get('scenes', []):
        narration = scene.get('narration', '').strip()
        duration = scene.get('duration_seconds', 5.0)

        if not narration:
            current_time += duration
            continue

        # Split long narration into subtitle-sized chunks (~70 chars)
        chunks = _split_into_caption_chunks(narration, max_chars=70)
        chunk_duration = duration / len(chunks)

        for chunk in chunks:
            start = current_time
            end = current_time + chunk_duration

            start_ts = _format_srt_time(start)
            end_ts = _format_srt_time(end)

            lines.append(f"{counter}")
            lines.append(f"{start_ts} --> {end_ts}")
            lines.append(chunk)
            lines.append("")

            counter += 1
            current_time = end

    return "\n".join(lines)


def _split_into_caption_chunks(text: str, max_chars: int = 70) -> list[str]:
    """Split text into readable caption chunks."""
    words = text.split()
    chunks = []
    current = []

    for word in words:
        if sum(len(w) for w in current) + len(current) + len(word) > max_chars:
            if current:
                chunks.append(" ".join(current))
                current = []
        current.append(word)

    if current:
        chunks.append(" ".join(current))

    return chunks if chunks else [text]


def _format_srt_time(seconds: float) -> str:
    """Format seconds as SRT timestamp: HH:MM:SS,mmm"""
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    ms = int((seconds % 1) * 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def generate_assembly_manifest(storyboard: dict) -> dict:
    """Generate assembly instructions for the video editor."""
    scenes_manifest = []
    cumulative_time = 0.0

    for scene in storyboard.get('scenes', []):
        sn = scene['scene_number']
        dur = scene['duration_seconds']
        transition = scene.get('transition', 'cut')
        overlay = scene.get('text_overlay', '')

        scenes_manifest.append({
            'scene': sn,
            'start_time': round(cumulative_time, 2),
            'end_time': round(cumulative_time + dur, 2),
            'duration': dur,
            'transition_in': transition,
            'text_overlay': overlay,
            'visual_asset': f"visuals/scene_{sn:02d}.png",
            'video_asset': f"clips/scene_{sn:02d}.mp4",
        })
        cumulative_time += dur

    return {
        'title': storyboard.get('title', 'Untitled'),
        'total_duration': round(cumulative_time, 2),
        'scenes': scenes_manifest,
        'assets': {
            'voiceover': 'audio/voiceover.mp3',
            'music': 'audio/background_music.mp3',
            'captions': 'captions.srt',
        },
        'export': {
            'resolution': '1920x1080',
            'fps': 30,
            'codec': 'h264',
            'format': 'mp4',
        },
        'editing_notes': [
            "Apply voiceover to audio track 1, sync to scene timings.",
            "Apply background music to audio track 2, duck -18dB during narration.",
            "Import captions.srt and verify timing against voiceover.",
            "Add text overlays per scene manifest.",
            "Apply transitions between scenes as specified.",
            "Color grade for consistency across all scenes.",
            "Add brand logo watermark in bottom-right corner (last 5s).",
        ],
    }


def main():
    if len(sys.argv) < 2:
        print("Usage: python editing_agent.py <storyboard.json> [output_dir]")
        sys.exit(1)

    sb_path = sys.argv[1]
    output_dir = Path(sys.argv[2]) if len(sys.argv) > 2 else Path(sb_path).parent

    storyboard = load_storyboard(sb_path)

    # Generate captions
    srt = generate_captions_srt(storyboard)
    captions_path = output_dir / "captions.srt"
    atomic_write_text(captions_path, srt)

    # Generate assembly manifest
    manifest = generate_assembly_manifest(storyboard)
    manifest_path = output_dir / "assembly_manifest.json"
    atomic_write_json(manifest_path, manifest)

    caption_count = srt.count("-->")
    print(f"Captions: {captions_path} ({caption_count} entries)")
    print(f"Assembly manifest: {manifest_path} ({len(manifest['scenes'])} scenes, {manifest['total_duration']:.1f}s)")
    print(f"  Export: {manifest['export']['resolution']} @ {manifest['export']['fps']}fps")


if __name__ == '__main__':
    main()
