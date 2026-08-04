"""
Research Agent — Gathers intelligence and produces a creative brief.

Input: topic, target audience, duration, platform
Output: creative_brief.json
"""
import json, sys
from pathlib import Path
from dataclasses import dataclass, field, asdict
from typing import Optional

@dataclass
class CreativeBrief:
    topic: str
    target_audience: str
    duration_seconds: int
    platform: str
    tone: str
    key_messages: list[str] = field(default_factory=list)
    reference_urls: list[str] = field(default_factory=list)
    competitive_notes: str = ""
    visual_style: str = ""
    call_to_action: str = ""


def load_input(brief_path: str) -> dict:
    """Load brief from YAML or JSON."""
    path = Path(brief_path)
    if not path.exists():
        raise FileNotFoundError(f"Brief not found: {brief_path}")

    if path.suffix in ('.yaml', '.yml'):
        import yaml
        with open(path) as f:
            return yaml.safe_load(f)
    else:
        with open(path) as f:
            return json.load(f)


def generate_brief(raw: dict) -> CreativeBrief:
    """Take raw input brief, enrich with research context, produce CreativeBrief."""
    topic = raw.get('topic', '')
    audience = raw.get('target_audience', 'general')
    duration = raw.get('duration_seconds', 60)
    platform = raw.get('platform', 'youtube')
    tone = raw.get('tone', 'professional')
    messages = raw.get('key_messages', [])
    cta = raw.get('call_to_action', '')
    visual = raw.get('visual_style', '')
    refs = raw.get('reference_urls', [])

    # Build enriched brief
    brief = CreativeBrief(
        topic=topic,
        target_audience=audience,
        duration_seconds=duration,
        platform=platform,
        tone=tone,
        key_messages=messages,
        reference_urls=refs,
        competitive_notes=raw.get('competitive_notes',
            f"No direct competitors identified. Focus on original angle for '{topic}'."),
        visual_style=visual or infer_visual_style(tone, platform),
        call_to_action=cta or infer_cta(platform, topic),
    )
    return brief


def infer_visual_style(tone: str, platform: str) -> str:
    styles = {
        'professional': 'clean, well-lit, corporate minimalism with accent color pops',
        'energetic': 'fast-paced, bold colors, dynamic camera movements, high contrast',
        'educational': 'warm lighting, clean backgrounds, text overlays, diagrams',
        'cinematic': 'film-grade lighting, shallow depth of field, color graded, dramatic',
        'casual': 'natural light, handheld feel, relaxed framing, authentic',
    }
    platform_mod = {
        'tiktok': ', vertical 9:16, fast cuts, text-heavy overlays',
        'instagram': ', vertical 4:5 or 1:1, polished aesthetic',
        'youtube': ', horizontal 16:9, higher production value',
        'linkedin': ', horizontal 16:9, professional and polished',
    }
    base = styles.get(tone, styles['professional'])
    mod = platform_mod.get(platform, '')
    return base + mod


def infer_cta(platform: str, topic: str) -> str:
    ctas = {
        'youtube': 'Subscribe for more and hit the bell.',
        'tiktok': 'Follow for more tips like this.',
        'instagram': 'Save this for later and share with someone who needs it.',
        'linkedin': 'Follow for more insights on this topic.',
    }
    return ctas.get(platform, 'Learn more at the link in bio.')


def main():
    if len(sys.argv) < 2:
        print("Usage: python research_agent.py <brief.yaml> [output_dir]")
        sys.exit(1)

    brief_path = sys.argv[1]
    output_dir = Path(sys.argv[2]) if len(sys.argv) > 2 else Path(brief_path).parent

    raw = load_input(brief_path)
    brief = generate_brief(raw)

    output_dir.mkdir(parents=True, exist_ok=True)
    out_path = output_dir / "creative_brief.json"
    with open(out_path, 'w') as f:
        json.dump(asdict(brief), f, indent=2)

    print(f"Creative brief: {out_path}")
    print(f"  Topic: {brief.topic}")
    print(f"  Audience: {brief.target_audience}")
    print(f"  Duration: {brief.duration_seconds}s")
    print(f"  Platform: {brief.platform}")
    print(f"  Tone: {brief.tone}")
    print(f"  Visual: {brief.visual_style[:80]}...")


if __name__ == '__main__':
    main()
