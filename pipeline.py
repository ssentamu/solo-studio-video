#!/usr/bin/env python3
"""
Solo Studio Video Pipeline — Orchestrator

Runs all 7 stages:
  1. Research Agent  → Creative Brief
  2. Script Agent    → Script + Storyboard
  3. Design Agent    → Visual Prompts + Generated Images
  4. Production Agent → Voiceover + Video Prompts + Music Prompt
  5. Generation Agent → Provider-ready generation plan / clips when enabled
  6. Editing Agent   → Captions + Assembly Manifest
  7. Editor Export    → FCPXML editor package

Usage:
  python pipeline.py briefs/my_video.yaml
  python pipeline.py briefs/my_video.yaml --skip-visuals
  python pipeline.py briefs/my_video.yaml --output output/my-project
"""
import argparse, json, subprocess, sys
from pathlib import Path

from package_utils import (
    _run_bounded_subprocess,
    atomic_write_json,
    clear_generated_artifacts,
    read_json_artifact,
    write_package_manifest,
)
from media_assembly import MediaError
from worker import _artifact_digest, _assemble_verified_output

ENGINES = Path(__file__).resolve().parent / "engines"
STAGE_TIMEOUT_SECONDS = 900.0


def run_stage(name: str, script: str, *args) -> bool:
    """Run a pipeline stage. Returns True on success."""
    cmd = [sys.executable, str(ENGINES / script), *args]
    print(f"\n{'='*60}")
    print(f"  STAGE: {name}")
    print(f"{'='*60}")
    try:
        result = _run_bounded_subprocess(cmd, timeout=STAGE_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired as exc:
        print(f"  ✗ FAILED: stage deadline exceeded ({exc})")
        return False
    except subprocess.SubprocessError:
        print("  ✗ FAILED: bounded subprocess supervision failed")
        return False
    except TimeoutError as exc:
        print(f"  ✗ FAILED: stage deadline exceeded ({exc})")
        return False
    except OSError as exc:
        print(f"  ✗ FAILED to start: {exc}")
        return False
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
    parser.add_argument(
        "--skip-voiceover",
        action="store_true",
        help="Reserved; production currently writes a voiceover script only",
    )
    args = parser.parse_args()
    if args.skip_voiceover:
        print("Note: --skip-voiceover is reserved; this slice only writes a voiceover script, not rendered audio.")

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
    try:
        clear_generated_artifacts(out)
    except OSError as exc:
        _fail_pipeline(out, brief_path, f"Failed to clear stale generated artifacts: {exc}")

    print(f"\n{'#'*60}")
    print(f"  SOLO STUDIO — Video Production Pipeline")
    print(f"  Brief: {brief_path}")
    print(f"  Output: {out}")
    print(f"{'#'*60}")

    # Stage 1: Research
    if not run_stage("1. Research Agent", "research_agent.py", str(brief_path), str(out)):
        _fail_pipeline(out, brief_path, "Research agent failed")

    brief_json = out / "creative_brief.json"

    # Stage 2: Script
    if not run_stage("2. Script Agent", "script_agent.py", str(brief_json), str(out)):
        _fail_pipeline(out, brief_path, "Script agent failed")

    storyboard_json = out / "storyboard.json"

    # Stage 3: Design (Visuals)
    if not args.skip_visuals:
        if not run_stage("3. Design Agent", "design_agent.py", str(storyboard_json), str(out)):
            _fail_pipeline(out, brief_path, "Design agent failed")
    else:
        print(f"\n  ⏭ Skipping visuals (--skip-visuals)")

    # Stage 4: Production (Voiceover + Video Prompts + Music)
    if not run_stage("4. Production Agent", "production_agent.py", str(storyboard_json), str(out)):
        _fail_pipeline(out, brief_path, "Production agent failed")

    # Stage 5: Video Generation Plan / safe dry-run
    video_prompts_json = out / "video_prompts.json"
    if video_prompts_json.exists():
        if not run_stage("5. Generation Agent", "generation_agent.py", str(video_prompts_json), str(out)):
            _fail_pipeline(out, brief_path, "Generation plan failed")
    else:
        print("  ✗ Video prompts missing before generation stage")
        _fail_pipeline(out, brief_path, "Video prompts missing before generation stage")

    # Stage 6: Editing (Captions + Assembly)
    if not run_stage("6. Editing Agent", "editing_agent.py", str(storyboard_json), str(out)):
        _fail_pipeline(out, brief_path, "Editing agent failed")

    # Stage 7: Editor Export (DaVinci Resolve / Premiere Pro)
    if not run_stage("7. Editor Export", "editor_export.py", str(storyboard_json), str(out)):
        _fail_pipeline(out, brief_path, "Editor export failed")

    # Generate thumbnail prompt (always, even with --skip-visuals)
    _generate_thumbnail_prompt(out, brief_json)

    storyboard = read_json_artifact(storyboard_json)
    manifest_job = {
        "status": "completed",
        "brief": str(brief_path),
        "output_profile": storyboard.get("output_profile", "landscape"),
        "aspect_ratio": storyboard.get("aspect_ratio", "16:9"),
    }
    try:
        final_media = _assemble_verified_output(out, storyboard)
    except MediaError as exc:
        _fail_pipeline(out, brief_path, f"Final media assembly failed: {exc}")
    if final_media:
        manifest_job.update({
            "final_video_sha256": final_media["sha256"],
            "final_video_plan_sha256": _artifact_digest(out / "clips" / "generation_plan.json"),
            "final_video_duration_seconds": final_media["duration_seconds"],
        })

    # Write an artifact-derived manifest so CLI output uses the same honesty
    # contract as web jobs.
    try:
        manifest = write_package_manifest(out, manifest_job)
    except Exception as exc:
        _fail_pipeline(out, brief_path, f"Failed to write package manifest: {exc}")
        raise

    # Summary
    print(f"\n{'#'*60}")
    print(f"  PIPELINE COMPLETE")
    print(f"{'#'*60}")
    print(f"\n  Output: {out}/")
    print(f"  Package status: {manifest['package_status']}")
    _print_output_tree(out)
    print(f"\n  Import {out}/timeline.fcpxml into DaVinci Resolve or Premiere Pro.")
    print(f"  To generate visuals: run with image generation enabled.")
    print(f"  To generate voiceover: configure ELEVENLABS_API_KEY and rerun.")


def _fail_pipeline(out: Path, brief_path: Path, error: str):
    """Write a failure manifest for CLI runs before exiting non-zero."""
    try:
        manifest = write_package_manifest(out, {"status": "failed", "brief": str(brief_path), "error": error})
        print(f"  Package status: {manifest['package_status']}")
        print(f"  Failure manifest: {out / 'package_manifest.json'}")
    except Exception as exc:
        print(f"  ⚠ Failed to write failure manifest: {exc}")
    sys.exit(1)


def _generate_thumbnail_prompt(out: Path, brief_json: Path):
    """Generate a YouTube thumbnail prompt."""
    try:
        brief = read_json_artifact(brief_json)
        sb_path = out / "storyboard.json"
        if not sb_path.exists():
            return
        sb = read_json_artifact(sb_path)

        topic = brief.get('topic', '')
        tone = brief.get('tone', 'professional')
        dur = sb.get('total_duration', 60)
        aspect_ratio = sb.get('aspect_ratio') or (
            '9:16' if sb.get('output_profile') == 'vertical' else '16:9'
        )
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
            f"High contrast, vibrant, 4K, {aspect_ratio}. Maximum 4 words. Clickable."
        )
        tp = out / "thumbnail_prompt.json"
        atomic_write_json(tp, {'title_overlay': overlay, 'prompt': prompt, 'filename': 'youtube_thumbnail.png'})
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
