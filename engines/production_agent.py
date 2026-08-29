"""
Production Agent — Script + Storyboard → Voiceover + Video Prompts + Music Prompt.

Input: script.txt + storyboard.json
Output: audio/voiceover.mp3 + video_prompts.json + music_prompt.txt
"""
import json, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from package_utils import atomic_write_json, atomic_write_text, read_json_artifact, read_text_artifact, validate_output_profile_contract


def load_storyboard(path: str) -> dict:
    return read_json_artifact(path)


def load_script(path: str) -> str:
    return read_text_artifact(path)


def generate_video_prompts(storyboard: dict, visual_style: str = "") -> list[dict]:
    """Generate video generation prompts for each scene (Runway/Pika/Kling ready)."""
    prompts = []
    profile = validate_output_profile_contract(storyboard, "storyboard")

    for scene in storyboard.get('scenes', []):
        sn = scene['scene_number']
        dur = scene['duration_seconds']
        visual = scene['visual_description']
        camera = scene.get('camera', 'medium shot')
        transition = scene.get('transition', 'cut')

        # Runway-style prompt
        runway = (
            f"{visual}. {camera}. {visual_style}. "
            f"Smooth {transition} transition. Professional lighting. High quality. "
            f"Compose for {profile['aspect_ratio']} {profile['output_profile']} video."
        )

        # Pika-style prompt (shorter, more action-oriented)
        pika = f"{visual}. {camera}. Cinematic quality. {profile['aspect_ratio']} frame."

        # Kling-style prompt (more detailed)
        kling = (
            f"Scene: {visual}. Camera movement: subtle {camera} with gentle drift. "
            f"Lighting: professional studio setup. Duration: {dur:.0f}s. "
            f"Aspect ratio: {profile['aspect_ratio']}."
        )

        prompts.append({
            'scene_number': sn,
            'duration_seconds': dur,
            'runway_prompt': runway,
            'pika_prompt': pika,
            'kling_prompt': kling,
            'transition': transition,
            'output_profile': profile['output_profile'],
            'aspect_ratio': profile['aspect_ratio'],
            'resolution': profile['resolution'],
        })

    return prompts


def generate_music_prompt(storyboard: dict, tone: str) -> str:
    """Generate a Suno/Udio music prompt matching the video's tone."""
    mood_map = {
        'professional': 'corporate ambient, motivational, clean production, moderate tempo',
        'energetic': 'upbeat electronic, driving beat, energetic drop at chorus, 120-130 BPM',
        'educational': 'lo-fi hip hop, calm and focused, gentle beat, warm tones',
        'cinematic': 'orchestral cinematic, gradual build, emotional crescendo, strings and piano',
        'casual': 'acoustic indie, relaxed guitar, warm and friendly, medium tempo',
    }

    mood = mood_map.get(tone, mood_map['professional'])
    duration = storyboard.get('total_duration', 60)

    return (
        f"Create a {mood} background track approximately {int(duration)} seconds long. "
        f"The music should start subtly, build energy through the middle section, "
        f"and resolve gently at the end. No vocals. Instrumental only. "
        f"Professional mixing quality. Suitable for {storyboard.get('title', 'video content')}."
    )


def build_voiceover_text(storyboard: dict) -> str:
    """Concatenate all narration into a single voiceover script."""
    lines = []
    for scene in storyboard.get('scenes', []):
        narration = scene.get('narration', '').strip()
        if narration:
            lines.append(narration)
    return " ".join(lines)


def main():
    if len(sys.argv) < 2:
        print("Usage: python production_agent.py <storyboard.json> [output_dir]")
        sys.exit(1)

    sb_path = sys.argv[1]
    output_dir = Path(sys.argv[2]) if len(sys.argv) > 2 else Path(sb_path).parent

    storyboard = load_storyboard(sb_path)

    # Load creative brief for tone / visual style
    brief_path = output_dir / "creative_brief.json"
    tone = 'professional'
    visual_style = ''
    if brief_path.exists():
        brief = read_json_artifact(brief_path)
        tone = brief.get('tone', 'professional')
        visual_style = brief.get('visual_style', '')

    # Generate video prompts
    video_prompts = generate_video_prompts(storyboard, visual_style)
    vp_path = output_dir / "video_prompts.json"
    profile = validate_output_profile_contract(storyboard, "storyboard")
    atomic_write_json(vp_path, {
        'scenes': video_prompts,
        'total_scenes': len(video_prompts),
        'output_profile': profile['output_profile'],
        'aspect_ratio': profile['aspect_ratio'],
        'resolution': profile['resolution'],
    })

    # Generate music prompt
    music_prompt = generate_music_prompt(storyboard, tone)
    music_path = output_dir / "music_prompt.txt"
    atomic_write_text(music_path, music_prompt)

    # Generate voiceover text
    vo_text = build_voiceover_text(storyboard)
    vo_text_path = output_dir / "audio" / "voiceover_script.txt"
    vo_text_path.parent.mkdir(exist_ok=True)
    atomic_write_text(vo_text_path, vo_text)

    print(f"Video prompts: {vp_path} ({len(video_prompts)} scenes)")
    print(f"Music prompt: {music_path}")
    print(f"Voiceover script: {vo_text_path} ({len(vo_text)} chars)")


if __name__ == '__main__':
    main()
