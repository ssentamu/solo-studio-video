"""
Research Agent — Gathers intelligence and produces a creative brief.

Input: topic, target audience, duration, platform
Output: creative_brief.json
"""
import hashlib, json, os, sys
from pathlib import Path
import stat
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dataclasses import dataclass, field, asdict
from typing import Any, Optional
from package_utils import _entry_cleanup_identity_at, _open_directory_no_follow, _open_regular_descriptor, _parse_strict_json, _read_bounded_utf8, _remove_entry_at, atomic_write_json, read_json_artifact, read_text_artifact, validate_output_profile_contract
from engines import source_ingest_agent

MAX_BRIEF_BYTES = 1_048_576

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
    output_profile: str = "landscape"
    aspect_ratio: str = "16:9"
    source_context: str = ""
    reverse_brief: dict[str, Any] = field(default_factory=dict)


def _read_bounded_brief(path: Path) -> str:
    descriptor = _open_regular_descriptor(path)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > MAX_BRIEF_BYTES:
            raise ValueError("brief exceeds the bounded input limit")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, min(64 * 1024, MAX_BRIEF_BYTES + 1 - total))
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_BRIEF_BYTES:
                raise ValueError("brief exceeds the bounded input limit")
            chunks.append(chunk)
        return b"".join(chunks).decode("utf-8")
    finally:
        os.close(descriptor)


def load_input(brief_path: str) -> dict:
    """Load a bounded brief from YAML or strict JSON."""
    path = Path(brief_path)
    text = _read_bounded_brief(path)
    if path.suffix in ('.yaml', '.yml'):
        import yaml
        raw = yaml.safe_load(text)
    else:
        raw = _parse_strict_json(text)
    if not isinstance(raw, dict):
        raise ValueError("brief must be an object")
    return raw


def generate_brief(
    raw: dict,
    *,
    source_context: str = "",
    reverse_brief: dict[str, Any] | None = None,
) -> CreativeBrief:
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
    if not isinstance(refs, list) or len(refs) > source_ingest_agent.MAX_REFERENCE_URLS:
        raise ValueError("reference_urls must be a list of at most 3 URLs")
    for url in refs:
        source_ingest_agent.validate_url_syntax(url)
    profile = validate_output_profile_contract(raw, "creative brief")

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
        output_profile=profile["output_profile"],
        aspect_ratio=profile["aspect_ratio"],
        source_context=source_context,
        reverse_brief=reverse_brief or {},
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




def _clear_source_artifacts(output_dir: Path) -> None:
    names = ("source_manifest.json", "source_context.md", "reverse_brief.json")
    try:
        root_fd = _open_directory_no_follow(output_dir, create=False)
    except FileNotFoundError:
        return
    try:
        first_error: BaseException | None = None
        for name in names:
            try:
                identity = _entry_cleanup_identity_at(root_fd, name)
                _remove_entry_at(root_fd, name, identity)
            except FileNotFoundError:
                continue
            except (OSError, TimeoutError, ValueError) as exc:
                first_error = first_error or exc
        if first_error is not None:
            raise ValueError("stale source artifacts could not be cleared") from first_error
    finally:
        os.close(root_fd)


def load_source_artifacts(output_dir: Path, reference_urls: list[str] | None = None) -> tuple[str, dict[str, Any]]:
    """Load source evidence only when it is bound to the current references."""
    if reference_urls is None:
        raise ValueError("reference_urls must be a list of at most 3 URLs")
    references = reference_urls
    if not isinstance(references, list) or len(references) > source_ingest_agent.MAX_REFERENCE_URLS:
        raise ValueError("reference_urls must be a list of at most 3 URLs")
    if not references:
        _clear_source_artifacts(output_dir)
        return "", {}
    context_path = output_dir / "source_context.md"
    reverse_path = output_dir / "reverse_brief.json"
    manifest_path = output_dir / "source_manifest.json"
    if not manifest_path.exists():
        if not context_path.exists() and not reverse_path.exists():
            return "", {}
        raise ValueError("source manifest is required for existing source artifacts")
    manifest = read_json_artifact(manifest_path)
    artifact_hashes = manifest.get("artifacts") if isinstance(manifest, dict) else None
    if not isinstance(artifact_hashes, dict):
        raise ValueError("source artifact integrity metadata is missing")
    sources = manifest.get("sources") if isinstance(manifest, dict) else None
    if not isinstance(sources, list) or len(sources) != len(references):
        raise ValueError("source manifest does not match reference URLs")
    manifest_urls = [source.get("source_url") if isinstance(source, dict) else None for source in sources]
    if manifest_urls != references:
        raise ValueError("source manifest does not match reference URLs")
    context_path = output_dir / "source_context.md"
    reverse_path = output_dir / "reverse_brief.json"
    if not context_path.exists() or not reverse_path.exists():
        raise ValueError("source artifacts are incomplete")
    context = read_text_artifact(context_path)
    reverse_text = read_text_artifact(reverse_path)
    reverse = _parse_strict_json(reverse_text)
    if not isinstance(reverse, dict):
        raise ValueError("reverse brief must be an object")
    if artifact_hashes.get("source_context.md") != hashlib.sha256(context.encode("utf-8")).hexdigest():
        raise ValueError("source context integrity check failed")
    reverse_bytes = reverse_text.encode("utf-8")
    if artifact_hashes.get("reverse_brief.json") != hashlib.sha256(reverse_bytes).hexdigest():
        raise ValueError("reverse brief integrity check failed")
    return context, reverse


def main():
    if len(sys.argv) < 2:
        print("Usage: python research_agent.py <brief.yaml> [output_dir]")
        sys.exit(1)

    brief_path = sys.argv[1]
    output_dir = Path(sys.argv[2]) if len(sys.argv) > 2 else Path(brief_path).parent

    raw = load_input(brief_path)
    references = raw.get("reference_urls", [])
    source_context, reverse_brief = load_source_artifacts(output_dir, references)
    brief = generate_brief(raw, source_context=source_context, reverse_brief=reverse_brief)

    output_dir.mkdir(parents=True, exist_ok=True)
    out_path = output_dir / "creative_brief.json"
    atomic_write_json(out_path, asdict(brief))

    print(f"Creative brief: {out_path}")
    print(f"  Topic: {brief.topic}")
    print(f"  Audience: {brief.target_audience}")
    print(f"  Duration: {brief.duration_seconds}s")
    print(f"  Platform: {brief.platform}")
    print(f"  Tone: {brief.tone}")
    print(f"  Visual: {brief.visual_style[:80]}...")


if __name__ == '__main__':
    main()
