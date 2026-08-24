"""
Editor Export Agent — Generates professional editor project files.

Input: storyboard.json + assembly_manifest.json
Output: timeline.fcpxml (DaVinci Resolve / Premiere Pro compatible)
"""
import json, sys
from fractions import Fraction
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from xml.etree.ElementTree import Element, SubElement, tostring
from xml.dom import minidom
from package_utils import atomic_write_text, read_json_artifact


def load_json(path: str) -> dict:
    return read_json_artifact(path)


def _timebase(value: object) -> tuple[int, int, float]:
    """Return a valid FCPXML timebase and numeric fps."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("export fps must be numeric")
    fps = float(value)
    if fps <= 0 or not fps == fps or fps in (float("inf"), float("-inf")):
        raise ValueError("export fps must be finite and positive")
    if abs(fps - 29.97) < 0.01:
        rate = Fraction(1001, 30000)
    else:
        rate = Fraction(1, 1) / Fraction(str(fps))
    return rate.numerator, rate.denominator, fps


def generate_fcpxml(storyboard: dict, manifest: dict) -> str:
    """Generate Final Cut Pro XML compatible with DaVinci Resolve and Premiere Pro."""
    scenes = storyboard.get('scenes', [])
    if not isinstance(scenes, list) or not scenes:
        raise ValueError("storyboard scenes must be a non-empty list")
    scene_numbers = []
    for scene in scenes:
        if not isinstance(scene, dict):
            raise ValueError("storyboard scene must be an object")
        number = scene.get('scene_number')
        if isinstance(number, bool) or not isinstance(number, int) or number <= 0:
            raise ValueError("scene numbers must be positive integers")
        scene_numbers.append(number)
    if scene_numbers != list(range(1, len(scene_numbers) + 1)):
        raise ValueError("scene numbers must be contiguous starting at 1")
    title = storyboard.get('title', 'Untitled')

    fps_num, fps_den, fps = _timebase(manifest.get('export', {}).get('fps', 30))
    frame_duration = f'{fps_num}/{fps_den}s'

    def frames(seconds: object) -> int:
        if isinstance(seconds, bool) or not isinstance(seconds, (int, float)):
            raise ValueError("scene duration must be numeric")
        value = float(seconds)
        if value <= 0 or not value == value or value in (float("inf"), float("-inf")):
            raise ValueError("scene duration must be finite and positive")
        # Quantize using the exact rational frame rate declared in the XML.
        # For 29.97 fps this is 30000/1001, not the display approximation.
        exact_fps = Fraction(fps_den, fps_num)
        return max(1, int(round(value * exact_fps)))

    def timecode(frame_count: int) -> str:
        value = Fraction(frame_count * fps_num, fps_den)
        return f'{value.numerator}/{value.denominator}s'

    scene_frame_counts = [frames(scene.get('duration_seconds', 5)) for scene in scenes]
    total_frames = sum(scene_frame_counts)

    # Build FCPXML structure
    fcpxml = Element('fcpxml', version='1.8')

    # Resources
    resources = SubElement(fcpxml, 'resources')
    # Video format
    fmt = SubElement(resources, 'format', {
        'id': 'r1',
        'name': f'FFVideoFormat1080p{fps:g}',
        'frameDuration': frame_duration,
        'width': '1920',
        'height': '1080',
        'colorSpace': '1-1-1 (Rec. 709)',
    })
    SubElement(resources, 'effect', {
        'id': 'r2',
        'name': 'Basic Title',
        'uid': '.../Titles.localized/Basic Title.localized/Basic Title.moti',
    })
    SubElement(resources, 'asset', {
        'id': 'r_voiceover',
        'name': 'Voiceover',
        'src': 'file:///__SOLO_STUDIO_PLACEHOLDER__/voiceover.mp3',
        'hasAudio': '1',
        'hasVideo': '0',
    })
    SubElement(resources, 'asset', {
        'id': 'r_music',
        'name': 'Background Music',
        'src': 'file:///__SOLO_STUDIO_PLACEHOLDER__/music.mp3',
        'hasAudio': '1',
        'hasVideo': '0',
    })
    for number in scene_numbers:
        text_style = SubElement(resources, 'text-style-def', {'id': f'ts{number}'})
        SubElement(text_style, 'text-style', {'font': 'Helvetica', 'fontSize': '42'})

    # Library
    library = SubElement(fcpxml, 'library')
    event = SubElement(library, 'event', {'name': title})
    project = SubElement(event, 'project', {'name': title, 'id': 'p1'})
    sequence = SubElement(project, 'sequence', {
        'duration': timecode(total_frames),
        'format': 'r1',
        'tcStart': '0s',
        'tcFormat': 'NDF',
        'audioLayout': 'stereo',
        'audioRate': '48k',
    })

    # Spine (main timeline)
    spine = SubElement(sequence, 'spine')

    # Add each scene as a gap clip (placeholder) with title
    time_frames = 0
    for scene, dur_frames in zip(scenes, scene_frame_counts):
        sn = scene['scene_number']
        dur = float(scene.get('duration_seconds', 5))

        # Scene as a title clip. Its effect and text-style resources are
        # declared above, so every emitted ref is resolvable by an editor.
        clip = SubElement(spine, 'title', {
            'name': f'Scene {sn:03d}',
            'offset': timecode(time_frames),
            'duration': timecode(dur_frames),
            'ref': 'r2',
            'lane': '0',
        })

        # Title text
        text = SubElement(clip, 'text')
        text_style = SubElement(text, 'text-style', {'ref': f'ts{sn}'})
        text_style.text = scene.get('narration', '')[:100]

        # Add transition
        transition = scene.get('transition', 'cut')
        if transition in ('dissolve', 'fade_in', 'fade_out'):
            dur_trans = min(0.5, dur / 2)
            trans_frames = frames(dur_trans)
            SubElement(clip, 'param', {
                'name': 'Transition',
                'key': transition,
                'value': timecode(trans_frames),
            })

        time_frames += dur_frames

    # Add audio roles for voiceover and music as connected clips on the same
    # sequence spine. FCPXML permits one primary spine per sequence.
    audio_spine = spine
    vo_clip = SubElement(audio_spine, 'asset-clip', {
        'name': 'Voiceover',
        'offset': '0s',
        'duration': timecode(total_frames),
        'ref': 'r_voiceover',
        'audioRole': 'dialogue',
        'lane': '1',
    })

    # Audio lane 2: Music (starts at 0, ducks during narration)
    music_clip = SubElement(audio_spine, 'asset-clip', {
        'name': 'Background Music',
        'offset': '0s',
        'duration': timecode(total_frames),
        'ref': 'r_music',
        'audioRole': 'music',
        'lane': '2',
    })

    # Add audio volume keyframes for ducking
    vol = SubElement(music_clip, 'audio-volume')
    # Duck during narration scenes
    for i, scene in enumerate(scenes):
        start_frame = sum(scene_frame_counts[:i])
        level = '-20dB' if scene.get('narration', '').strip() else '0dB'
        SubElement(vol, 'keyframe', {
            'time': timecode(start_frame),
            'value': level,
            'interpolation': 'linear',
        })

    # Format output nicely
    rough = tostring(fcpxml, 'utf-8')
    parsed = minidom.parseString(rough)
    return parsed.toprettyxml(indent='  ')


def main():
    if len(sys.argv) < 2:
        print("Usage: python editor_export.py <storyboard.json> [output_dir]")
        sys.exit(1)

    sb_path = sys.argv[1]
    output_dir = Path(sys.argv[2]) if len(sys.argv) > 2 else Path(sb_path).parent

    storyboard = load_json(sb_path)

    # Load assembly manifest
    manifest_path = output_dir / "assembly_manifest.json"
    manifest = load_json(str(manifest_path)) if manifest_path.exists() else {'export': {'fps': 30}}

    # Generate FCPXML
    fcpxml = generate_fcpxml(storyboard, manifest)

    out_path = output_dir / "timeline.fcpxml"
    atomic_write_text(out_path, fcpxml)

    print(f"Editor project: {out_path}")
    print(f"  Format: DaVinci Resolve / Premiere Pro (FCPXML)")
    print(f"  Duration: {storyboard.get('total_duration', 0):.0f}s")
    print(f"  Scenes with titles: {len(storyboard.get('scenes', []))}")
    print(f"  Audio: Voiceover (lane 1) + Music (lane 2, auto-ducked)")


if __name__ == '__main__':
    main()
