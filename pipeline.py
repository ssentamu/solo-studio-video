#!/usr/bin/env python3
"""
Solo Studio Video Pipeline — Orchestrator

Runs all 5 stages:
  1. Research Agent  → Creative Brief
  2. Script Agent    → Script + Storyboard
  3. Design Agent    → Visual Prompts + Generated Images
  4. Production Agent → Voiceover + Video Prompts + Music Prompt
  5. Editing Agent   → Captions + Assembly Manifest

Usage:
  python pipeline.py briefs/my_video.yaml
  python pipeline.py briefs/my_video.yaml --skip-visuals
  python pipeline.py briefs/my_video.yaml --output output/my-project
"""
import argparse, json, subprocess, sys
from pathlib import Path

ENGINES = Path(__file__).resolve().parent / "engines"


def run_stage(name: str, script: str, *args) -> bool:
    """Run a pipeline stage. Returns True on success."""
    cmd = [sys.executable, str(ENGINES / script), *args]
    print(f"\n{'='*60}")
    print(f"  STAGE: {name}")
    print(f"{'='*60}")
    result = subprocess.run(cmd, capture_output=False)
    if result.returncode != 0:
        print(f"  ✗ FAILED (exit {result.returncode})")
        return False
    print(f"  ✓ Done")
    return True


def main():
    parser = argparse.ArgumentParser(description="Solo Studio Video Pipeline")
    parser.add_argument("brief", help="Path to brief YAML file")
    parser.add_argument("--output", "-o", help="Output directory (default: derived from brief name)")
    parser.add_argument("--skip-visuals", action="store_true", help="Skip visual asset generation")
    parser.add_argument("--skip-voiceover", action="store_true", help="Skip voiceover generation")
    args = parser.parse_args()

    brief_path = Path(args.brief)
    if not brief_path.exists():
        print(f"Brief not found: {brief_path}")
        sys.exit(1)

    # Determine output directory
    if args.output:
        out = Path(args.output)
    else:
        out = Path("output") / brief_path.stem

    out.mkdir(parents=True, exist_ok=True)

    print(f"\n{'#'*60}")
    print(f"  SOLO STUDIO — Video Production Pipeline")
    print(f"  Brief: {brief_path}")
    print(f"  Output: {out}")
    print(f"{'#'*60}")

    # Stage 1: Research
    if not run_stage("1. Research Agent", "research_agent.py", str(brief_path), str(out)):
        sys.exit(1)

    brief_json = out / "creative_brief.json"

    # Stage 2: Script
    if not run_stage("2. Script Agent", "script_agent.py", str(brief_json), str(out)):
        sys.exit(1)

    storyboard_json = out / "storyboard.json"

    # Stage 3: Design (Visuals)
    if not args.skip_visuals:
        if not run_stage("3. Design Agent", "design_agent.py", str(storyboard_json), str(out)):
            print("  ⚠ Design agent failed — continuing with remaining stages")
    else:
        print(f"\n  ⏭ Skipping visuals (--skip-visuals)")

    # Stage 4: Production (Voiceover + Video Prompts + Music)
    if not run_stage("4. Production Agent", "production_agent.py", str(storyboard_json), str(out)):
        print("  ⚠ Production agent failed — continuing with remaining stages")

    # Stage 5: Editing (Captions + Assembly)
    if not run_stage("5. Editing Agent", "editing_agent.py", str(storyboard_json), str(out)):
        print("  ⚠ Editing agent failed")

    # Stage 6: Editor Export (DaVinci Resolve / Premiere Pro)
    if not run_stage("6. Editor Export", "editor_export.py", str(storyboard_json), str(out)):
        print("  ⚠ Editor export failed")

    # Generate thumbnail prompt (always, even with --skip-visuals)
    _generate_thumbnail_prompt(out, brief_json)

    # Summary
    print(f"\n{'#'*60}")
    print(f"  PIPELINE COMPLETE")
    print(f"{'#'*60}")
    print(f"\n  Output: {out}/")
    _print_output_tree(out)
    print(f"\n  Import {out}/timeline.fcpxml into DaVinci Resolve or Premiere Pro.")
    print(f"  To generate visuals: run with image generation enabled.")
    print(f"  To generate voiceover: configure ELEVENLABS_API_KEY and rerun.")


def _generate_thumbnail_prompt(out: Path, brief_json: Path):
    """Generate a YouTube thumbnail prompt."""
    try:
        with open(brief_json) as f:
            brief = json.load(f)
        sb_path = out / "storyboard.json"
        if not sb_path.exists():
            return
        with open(sb_path) as f:
            sb = json.load(f)

        topic = brief.get('topic', '')
        tone = brief.get('tone', 'professional')
        dur = sb.get('total_duration', 60)
        hooks = {
            'educational': f"The TRUTH About {topic[:35]}",
            'professional': f"{topic[:35]} — {int(dur)}s",
            'cinematic': topic[:35].upper(),
            'energetic': f"DON'T Ignore {topic[:35]}!",
            'casual': f"I Tried {topic[:35]}",
        }
        overlay = hooks.get(tone, topic[:50])
        prompt = (
            f"YouTube thumbnail: '{topic[:60]}'. Bold text: '{overlay}'. "
            f"Style: {brief.get('visual_style', 'professional')}. "
            f"High contrast, vibrant, 4K, 16:9. Maximum 4 words. Clickable."
        )
        tp = out / "thumbnail_prompt.json"
        with open(tp, 'w') as f:
            json.dump({'title_overlay': overlay, 'prompt': prompt, 'filename': 'youtube_thumbnail.png'}, f, indent=2)
        print(f"  Thumbnail prompt: {tp}")
    except Exception as e:
        print(f"  ⚠ Thumbnail prompt failed: {e}")


def _print_output_tree(out: Path):
    """Print the output file tree."""
    for f in sorted(out.rglob("*")):
        if f.is_file():
            size = f.stat().st_size
            size_str = f"{size:,}B" if size < 1024 else f"{size/1024:.1f}KB"
            rel = f.relative_to(out)
            print(f"    {rel} ({size_str})")


if __name__ == "__main__":
    main()
