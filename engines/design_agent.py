"""
Design Agent — Generates visual assets from the storyboard.

Input: storyboard.json
Output: visuals/ directory with generated images
"""
import json, sys
from pathlib import Path


def load_storyboard(path: str) -> dict:
    with open(path) as f:
        return json.load(f)


def generate_visual_prompts(storyboard: dict, visual_style: str = "") -> list[dict]:
    """Generate image prompts for each scene that needs a visual asset."""
    prompts = []
    scenes = storyboard.get('scenes', [])

    for scene in scenes:
        visual = scene['visual_description']
        camera = scene.get('camera', 'medium shot')

        # Build a professional image prompt
        prompt = (
            f"A professional video scene: {visual}. "
            f"Camera: {camera}. "
            f"Style: {visual_style}. "
            f"High production quality, cinematic lighting, 16:9 aspect ratio. "
            f"No text overlay in the image itself. Clean composition."
        )

        prompts.append({
            'scene_number': scene['scene_number'],
            'prompt': prompt,
            'filename': f"scene_{scene['scene_number']:02d}.png",
            'description': visual,
        })

    return prompts


def write_prompts_manifest(prompts: list[dict], output_dir: Path) -> Path:
    """Write the prompts manifest that the pipeline reads for image generation."""
    manifest = {
        'total_scenes': len(prompts),
        'prompts': prompts,
    }
    path = output_dir / "visual_prompts.json"
    with open(path, 'w') as f:
        json.dump(manifest, f, indent=2)
    return path


def generate_thumbnail_prompt(storyboard: dict, brief: dict) -> dict:
    """Generate a YouTube thumbnail prompt optimized for CTR."""
    topic = brief.get('topic', storyboard.get('title', ''))
    tone = brief.get('tone', 'professional')
    duration = storyboard.get('total_duration', 60)

    hooks = {
        'educational': f"The TRUTH About {topic[:35]}",
        'professional': f"{topic[:35]} — Explained in {int(duration)}s",
        'cinematic': topic[:35].upper(),
        'energetic': f"STOP Ignoring {topic[:35]}!",
        'casual': f"I Tried {topic[:35]} — Here's Why",
    }
    title_overlay = hooks.get(tone, topic[:50])

    prompt = (
        f"YouTube thumbnail for '{topic[:60]}'. "
        f"Bold text: '{title_overlay}'. "
        f"Style: {brief.get('visual_style', 'professional')}. "
        f"High contrast, vibrant, strong focal point. 4K, 16:9. "
        f"Maximum 4 words visible. Clean, clickable composition."
    )
    return {'title_overlay': title_overlay, 'prompt': prompt, 'filename': 'youtube_thumbnail.png'}


def main():
    if len(sys.argv) < 2:
        print("Usage: python design_agent.py <storyboard.json> [output_dir]")
        sys.exit(1)

    sb_path = sys.argv[1]
    output_dir = Path(sys.argv[2]) if len(sys.argv) > 2 else Path(sb_path).parent

    storyboard = load_storyboard(sb_path)

    # Try to load creative brief for visual style
    brief_path = output_dir / "creative_brief.json"
    visual_style = ""
    if brief_path.exists():
        with open(brief_path) as f:
            brief = json.load(f)
            visual_style = brief.get('visual_style', '')

    # Generate thumbnail prompt
    if brief_path.exists():
        with open(brief_path) as f:
            brief = json.load(f)
            thumbnail = generate_thumbnail_prompt(storyboard, brief)
            with open(output_dir / "thumbnail_prompt.json", 'w') as tf:
                json.dump(thumbnail, tf, indent=2)
            print(f"  Thumbnail: {output_dir / 'thumbnail_prompt.json'}")

    prompts = generate_visual_prompts(storyboard, visual_style)
    manifest_path = write_prompts_manifest(prompts, output_dir)

    visuals_dir = output_dir / "visuals"
    visuals_dir.mkdir(exist_ok=True)

    print(f"Visual prompts manifest: {manifest_path}")
    print(f"  Scenes needing visuals: {len(prompts)}")
    for p in prompts:
        print(f"    Scene {p['scene_number']}: {p['description'][:60]}...")


if __name__ == '__main__':
    main()
