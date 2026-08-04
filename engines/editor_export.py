"""
Editor Export Agent — Generates professional editor project files.

Input: storyboard.json + assembly_manifest.json
Output: timeline.fcpxml (DaVinci Resolve / Premiere Pro compatible)
"""
import json, sys
from pathlib import Path
from xml.etree.ElementTree import Element, SubElement, tostring
from xml.dom import minidom


def load_json(path: str) -> dict:
    with open(path) as f:
        return json.load(f)


def generate_fcpxml(storyboard: dict, manifest: dict) -> str:
    """Generate Final Cut Pro XML compatible with DaVinci Resolve and Premiere Pro."""
    scenes = storyboard.get('scenes', [])
    total_dur = storyboard.get('total_duration', 60)
    title = storyboard.get('title', 'Untitled')

    # Calculate frame count (30fps)
    fps = manifest.get('export', {}).get('fps', 30)
    total_frames = int(total_dur * fps)

    # Build FCPXML structure
    fcpxml = Element('fcpxml', version='1.8')

    # Resources
    resources = SubElement(fcpxml, 'resources')
    # Video format
    fmt = SubElement(resources, 'format', {
        'id': 'r1',
        'name': 'FFVideoFormat1080p30',
        'frameDuration': f'{int(100/fps)}/100s',
        'width': '1920',
        'height': '1080',
        'colorSpace': '1-1-1 (Rec. 709)',
    })

    # Library
    library = SubElement(fcpxml, 'library')
    event = SubElement(library, 'event', {'name': title})
    project = SubElement(event, 'project', {'name': title, 'id': 'p1'})
    sequence = SubElement(project, 'sequence', {
        'duration': f'{total_frames}/{fps}s',
        'format': 'r1',
        'tcStart': '0/1s',
        'tcFormat': 'NDF',
        'audioLayout': 'stereo',
        'audioRate': '48k',
    })

    # Spine (main timeline)
    spine = SubElement(sequence, 'spine')

    # Add each scene as a gap clip (placeholder) with title
    time = 0
    for scene in scenes:
        sn = scene['scene_number']
        dur = scene.get('duration_seconds', 5)
        dur_frames = int(dur * fps)

        # Scene as a title/gap clip
        clip = SubElement(spine, 'title', {
            'name': f'Scene {sn:03d}',
            'offset': f'{int(time * fps)}/{fps}s',
            'duration': f'{dur_frames}/{fps}s',
            'ref': f'r{sn}',
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
            trans_frames = int(dur_trans * fps)
            SubElement(clip, 'transition', {
                'name': 'Cross Dissolve' if transition == 'dissolve' else 'Fade',
                'duration': f'{trans_frames}/{fps}s',
            })

        time += dur

    # Add audio roles for voiceover and music
    # Audio lane 1: Voiceover
    audio_spine = SubElement(sequence, 'spine')
    vo_clip = SubElement(audio_spine, 'asset-clip', {
        'name': 'Voiceover',
        'offset': f'0/{fps}s',
        'duration': f'{total_frames}/{fps}s',
        'ref': 'r_voiceover',
        'audioRole': 'dialogue',
        'lane': '1',
    })

    # Audio lane 2: Music (starts at 0, ducks during narration)
    music_clip = SubElement(audio_spine, 'asset-clip', {
        'name': 'Background Music',
        'offset': f'0/{fps}s',
        'duration': f'{total_frames}/{fps}s',
        'ref': 'r_music',
        'audioRole': 'music',
        'lane': '2',
    })

    # Add audio volume keyframes for ducking
    vol = SubElement(music_clip, 'audio-volume')
    # Duck during narration scenes
    for i, scene in enumerate(scenes):
        start_t = sum(s.get('duration_seconds', 5) for s in scenes[:i])
        end_t = start_t + scene.get('duration_seconds', 5)
        level = '-20dB' if scene.get('narration', '').strip() else '0dB'
        SubElement(vol, 'keyframe', {
            'time': f'{int(start_t * fps)}/{fps}s',
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
    with open(out_path, 'w') as f:
        f.write(fcpxml)

    print(f"Editor project: {out_path}")
    print(f"  Format: DaVinci Resolve / Premiere Pro (FCPXML)")
    print(f"  Duration: {storyboard.get('total_duration', 0):.0f}s")
    print(f"  Scenes with titles: {len(storyboard.get('scenes', []))}")
    print(f"  Audio: Voiceover (lane 1) + Music (lane 2, auto-ducked)")


if __name__ == '__main__':
    main()
