"""Generation Agent — video prompts to verified generation plan.

This pass is deliberately safe: it creates a provider-ready generation plan and
records why real clip generation did or did not run. It does not pretend that
MP4 clips exist when the provider/CLI is unavailable.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def load_json(path: str | Path) -> dict[str, Any]:
    with open(path) as f:
        return json.load(f)


def _scene_prompt(scene: dict[str, Any]) -> str:
    base = (
        scene.get("seedance_prompt")
        or scene.get("kling_prompt")
        or scene.get("runway_prompt")
        or scene.get("pika_prompt")
        or ""
    ).strip()
    duration = float(scene.get("duration_seconds", 6) or 6)
    transition = scene.get("transition", "cut")

    if duration >= 20:
        return (
            f"Generate a {duration:.0f}s cinematic video clip. "
            f"0-6s set the scene: {base} "
            f"6-14s build motion and depth while preserving subject identity. "
            f"14-24s introduce the visual turn or strongest product moment. "
            f"24-{duration:.0f}s resolve cleanly for a {transition} transition. "
            "For every beat specify subject, action, setting, camera, style, and hard negative rules. "
            "No unwanted logos, no unreadable text, no warped hands or faces, no camera shake unless requested."
        )
    return (
        f"Generate a {duration:.0f}s high-quality video clip: {base} "
        f"End cleanly for a {transition} transition. Preserve subject consistency. "
        "No unwanted logos, no text artifacts, no jitter, no warped faces."
    )


def build_generation_plan(video_prompts: dict[str, Any], output_dir: str | Path) -> dict[str, Any]:
    output_dir = Path(output_dir)
    clips_dir = output_dir / "clips"
    clips_dir.mkdir(parents=True, exist_ok=True)

    enable_higgsfield = os.getenv("SOLO_STUDIO_ENABLE_HIGGSFIELD") == "1"
    higgsfield_path = shutil.which("higgsfield")
    scenes = []

    for scene in video_prompts.get("scenes", []):
        scene_number = int(scene.get("scene_number", len(scenes) + 1))
        scenes.append(
            {
                "scene_number": scene_number,
                "duration_seconds": scene.get("duration_seconds", 6),
                "provider": "higgsfield_seedance",
                "model": "text2video_seedance_2_5",
                "prompt": _scene_prompt(scene),
                "source_prompts": {
                    "seedance": scene.get("seedance_prompt", ""),
                    "runway": scene.get("runway_prompt", ""),
                    "pika": scene.get("pika_prompt", ""),
                    "kling": scene.get("kling_prompt", ""),
                },
                "target_file": f"clips/scene_{scene_number:02d}.mp4",
                "status": "planned",
            }
        )

    if not enable_higgsfield:
        status = "dry_run"
        setup_needed = "Set SOLO_STUDIO_ENABLE_HIGGSFIELD=1 and authenticate/install Higgsfield CLI to generate real clips."
    elif not higgsfield_path:
        status = "setup_needed"
        setup_needed = "SOLO_STUDIO_ENABLE_HIGGSFIELD=1 but `higgsfield` CLI is not installed in this runtime."
    else:
        # This safe implementation intentionally stops before provider mutation.
        # The plan is ready for the next integration pass that submits jobs,
        # polls, downloads MP4s, and verifies each clip.
        status = "ready_for_provider_submission"
        setup_needed = "Higgsfield CLI detected, but real provider submission is intentionally not enabled in this safe build."

    return {
        "status": status,
        "setup_needed": setup_needed,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "provider_enabled": enable_higgsfield,
        "higgsfield_cli": higgsfield_path,
        "total_scenes": len(scenes),
        "scenes": scenes,
    }


def generate_plan(video_prompts_path: str | Path, output_dir: str | Path) -> dict[str, Any]:
    prompts = load_json(video_prompts_path)
    plan = build_generation_plan(prompts, output_dir)
    clips_dir = Path(output_dir) / "clips"
    clips_dir.mkdir(parents=True, exist_ok=True)
    plan_path = clips_dir / "generation_plan.json"
    plan_path.write_text(json.dumps(plan, indent=2))
    return plan


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: python generation_agent.py <video_prompts.json> [output_dir]")
        sys.exit(1)

    video_prompts_path = Path(sys.argv[1])
    output_dir = Path(sys.argv[2]) if len(sys.argv) > 2 else video_prompts_path.parent
    if not video_prompts_path.exists():
        print(f"Video prompts not found: {video_prompts_path}", file=sys.stderr)
        sys.exit(1)

    plan = generate_plan(video_prompts_path, output_dir)
    print(f"Generation plan: {Path(output_dir) / 'clips' / 'generation_plan.json'}")
    print(f"  Status: {plan['status']}")
    print(f"  Scenes: {plan['total_scenes']}")
    if plan.get("setup_needed"):
        print(f"  Setup: {plan['setup_needed']}")


if __name__ == "__main__":
    main()
