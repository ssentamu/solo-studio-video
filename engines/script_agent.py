"""
Script Agent v2 — Long-form video script + storyboard engine.

Input: creative_brief.json
Output: script.txt + storyboard.json

Supports: short (<2min), medium (2-10min), long (10-30min), documentary (30+min)
"""
import json, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dataclasses import dataclass, field, asdict
from package_utils import atomic_write_json, atomic_write_text, read_json_artifact, validate_output_profile_contract
from enum import Enum


class VideoFormat(Enum):
    SHORT = "short"           # < 2 min — TikTok/Reels/Shorts
    MEDIUM = "medium"         # 2-10 min — YouTube standard
    LONG = "long"             # 10-30 min — YouTube deep dives
    DOCUMENTARY = "documentary"  # 30+ min — full episodes


@dataclass
class Scene:
    scene_number: int
    chapter: str
    duration_seconds: float
    visual_description: str
    narration: str
    text_overlay: str = ""
    b_roll_suggestion: str = ""
    transition: str = "cut"
    camera: str = "medium shot"


@dataclass
class Chapter:
    title: str
    start_time: float
    duration: float
    purpose: str  # hook, context, deep_dive, example, synthesis, cta, etc.


@dataclass
class Storyboard:
    title: str
    format: str
    total_duration: float
    chapters: list[Chapter] = field(default_factory=list)
    scenes: list[Scene] = field(default_factory=list)
    full_script: str = ""


def detect_format(duration_seconds: float) -> VideoFormat:
    if duration_seconds <= 120:
        return VideoFormat.SHORT
    elif duration_seconds <= 600:
        return VideoFormat.MEDIUM
    elif duration_seconds <= 1800:
        return VideoFormat.LONG
    else:
        return VideoFormat.DOCUMENTARY


def load_brief(brief_path: str) -> dict:
    return read_json_artifact(brief_path)


def generate_chapter_plan(duration: float, fmt: VideoFormat, messages: list[str]) -> list[Chapter]:
    """Build a chapter structure based on format and duration."""
    chapters = []
    t = 0.0

    if fmt == VideoFormat.SHORT:
        # Hook (10%) → Context (15%) → Messages (50%) → Solution (15%) → CTA (10%)
        plan = [
            ("HOOK", duration * 0.10, "hook"),
            ("CONTEXT", duration * 0.15, "context"),
            ("KEY INSIGHTS", duration * 0.50, "deep_dive"),
            ("THE SHIFT", duration * 0.15, "synthesis"),
            ("CTA", duration * 0.10, "cta"),
        ]
    elif fmt == VideoFormat.MEDIUM:
        # Intro (8%) → Problem (10%) → Deep Dive 1 (18%) → Deep Dive 2 (18%)
        # → Deep Dive 3 (18%) → Real Example (12%) → Synthesis (10%) → CTA (6%)
        plan = [
            ("INTRO", duration * 0.08, "hook"),
            ("THE PROBLEM", duration * 0.10, "context"),
            ("DEEP DIVE: The Data", duration * 0.18, "deep_dive"),
            ("DEEP DIVE: The Mechanics", duration * 0.18, "deep_dive"),
            ("DEEP DIVE: The Implications", duration * 0.18, "deep_dive"),
            ("REAL-WORLD EXAMPLE", duration * 0.12, "example"),
            ("WHAT THIS MEANS FOR YOU", duration * 0.10, "synthesis"),
            ("NEXT STEPS", duration * 0.06, "cta"),
        ]
    elif fmt == VideoFormat.LONG:
        # For 10-30 min: richer chapter structure with more depth
        msg_count = max(3, len(messages))
        msg_duration = duration * 0.50  # 50% of time on deep dives
        per_msg = msg_duration / msg_count

        plan = [
            ("INTRO: Why This Matters", duration * 0.05, "hook"),
            ("THE CURRENT LANDSCAPE", duration * 0.08, "context"),
        ]
        for i in range(msg_count):
            title = messages[i][:60] if i < len(messages) else f"DEEP DIVE {i+1}"
            plan.append((f"DEEP DIVE: {title}", per_msg, "deep_dive"))
        plan.extend([
            ("CASE STUDY / REAL EXAMPLE", duration * 0.08, "example"),
            ("COUNTER-ARGUMENTS & NUANCE", duration * 0.06, "context"),
            ("SYNTHESIS: The Big Picture", duration * 0.08, "synthesis"),
            ("ACTIONABLE TAKEAWAYS", duration * 0.06, "cta"),
            ("OUTRO & NEXT VIDEO", duration * 0.09, "cta"),
        ])
    else:  # DOCUMENTARY
        # For 30+ min: full episode structure
        plan = [
            ("COLD OPEN", duration * 0.03, "hook"),
            ("TITLE SEQUENCE", duration * 0.01, "hook"),
            ("PART 1: THE ORIGIN STORY", duration * 0.12, "context"),
            ("PART 2: HOW IT WORKS", duration * 0.14, "deep_dive"),
            ("PART 3: THE DATA", duration * 0.12, "deep_dive"),
            ("INTERLUDE: A STORY", duration * 0.06, "example"),
            ("PART 4: THE CONTROVERSY", duration * 0.10, "deep_dive"),
            ("PART 5: THE HUMAN ELEMENT", duration * 0.10, "context"),
            ("PART 6: WHERE THIS IS GOING", duration * 0.10, "deep_dive"),
            ("PART 7: WHAT TO DO ABOUT IT", duration * 0.08, "synthesis"),
            ("CLOSING ARGUMENT", duration * 0.06, "synthesis"),
            ("CREDITS & CALL TO ACTION", duration * 0.08, "cta"),
        ]

    # Keep the chapter contract exact even if a future percentage edit drifts.
    if plan:
        preceding = sum(item[1] for item in plan[:-1])
        title, _, purpose = plan[-1]
        plan[-1] = (title, duration - preceding, purpose)

    for title, dur, purpose in plan:
        chapters.append(Chapter(title=title, start_time=t, duration=dur, purpose=purpose))
        t += dur

    return chapters


def generate_scenes(chapters: list[Chapter], topic: str, tone: str, platform: str) -> list[Scene]:
    """Generate scenes within each chapter."""
    scenes = []
    scene_num = 0

    # Visual variety rotation
    visual_types = [
        "talking_head", "b_roll", "infographic", "screen_recording",
        "interview_style", "animation", "text_on_screen", "diagram",
    ]
    camera_types = ["medium shot", "close-up", "wide shot", "medium shot", "close-up", "wide shot"]

    for ch in chapters:
        purpose = ch.purpose
        ch_dur = ch.duration

        # Number of scenes in this chapter: roughly 1 scene per 15-60s depending on format
        if ch_dur < 30:
            scene_count = 1
        elif ch_dur < 120:
            scene_count = max(1, int(ch_dur / 20))
        else:
            scene_count = max(2, int(ch_dur / 30))

        scene_durations = [round(ch_dur / scene_count, 1) for _ in range(scene_count - 1)]
        scene_durations.append(ch_dur - sum(scene_durations))

        for i in range(scene_count):
            scene_num += 1
            visual_idx = (scene_num - 1) % len(visual_types)
            camera_idx = (scene_num - 1) % len(camera_types)

            visual = _chapter_visual(purpose, topic, tone, i, scene_count, visual_types[visual_idx])
            narration = _chapter_narration(purpose, topic, tone, i, scene_count, ch.title)
            overlay = _chapter_overlay(purpose, ch.title, i)
            b_roll = _b_roll_suggestion(purpose, topic, visual_types[visual_idx])
            transition = _chapter_transition(purpose, i, scene_count)

            scenes.append(Scene(
                scene_number=scene_num,
                chapter=ch.title,
                duration_seconds=scene_durations[i],
                visual_description=visual,
                narration=narration,
                text_overlay=overlay,
                b_roll_suggestion=b_roll,
                transition=transition,
                camera=camera_types[camera_idx],
            ))

    return scenes


def _chapter_visual(purpose: str, topic: str, tone: str, idx: int, total: int, visual_type: str) -> str:
    """Generate visual description based on chapter purpose and visual type rotation."""

    visuals_by_type = {
        "talking_head": "Speaker addresses camera directly. {tone_lighting}. Clean background with subtle depth.",
        "b_roll": "B-roll footage: {b_roll_content}. Smooth camera movement. Natural lighting.",
        "infographic": "Animated infographic displaying {topic_concept}. Clean data visualization. Dark mode with accent colors.",
        "screen_recording": "Screen recording of {topic_action}. Mouse cursor highlights key areas. Clean UI.",
        "interview_style": "Interview-style setup. Two-camera angle. {tone_lighting}. Professional backdrop.",
        "animation": "2D/3D animation illustrating {topic_concept}. Abstract geometric shapes. Smooth motion design.",
        "text_on_screen": "Key quote or statistic on screen. Bold typography. {tone} color palette. Minimal background.",
        "diagram": "Architecture/system diagram showing how {topic_concept}. Clean lines, labeled components. Animated build-up.",
    }

    tone_lighting = {
        'professional': 'Soft three-point studio lighting',
        'educational': 'Warm, inviting lighting',
        'cinematic': 'Dramatic Rembrandt lighting',
        'energetic': 'High-key bright lighting',
        'casual': 'Natural window light',
    }.get(tone, 'Soft even lighting')

    base = visuals_by_type.get(visual_type, visuals_by_type["talking_head"])
    return base.format(
        topic_concept=topic[:60],
        topic_action=topic[:60],
        tone_lighting=tone_lighting,
        tone=tone,
        b_roll_content=_b_roll_for_topic(topic),
    )


def _b_roll_for_topic(topic: str) -> str:
    """Suggest b-roll content based on topic keywords."""
    topic_lower = topic.lower()
    if any(w in topic_lower for w in ['ai', 'agent', 'code', 'developer', 'software', 'tech']):
        return "modern office, developers at workstations, code scrolling on screens, data center server racks"
    elif any(w in topic_lower for w in ['business', 'startup', 'founder', 'company', 'revenue']):
        return "busy city streets, office buildings, team meetings, whiteboard sessions"
    elif any(w in topic_lower for w in ['health', 'fitness', 'medical', 'doctor']):
        return "hospital corridors, lab equipment, patient consultations, medical technology"
    else:
        return "relevant industry settings, people working, establishing shots, detail close-ups"


def _b_roll_suggestion(purpose: str, topic: str, visual_type: str) -> str:
    if visual_type in ("talking_head", "text_on_screen"):
        return ""
    return _b_roll_for_topic(topic)


def _chapter_narration(purpose: str, topic: str, tone: str, idx: int, total: int, chapter_title: str) -> str:
    """Generate narration text for a scene within a chapter."""

    narrations = {
        "hook": [
            f"Most people have no idea what's really happening with {topic}.",
            f"Here's a number that should stop you in your tracks.",
            f"I've spent weeks researching {topic}, and what I found changes everything.",
            f"Let me tell you a story about {topic} that nobody's telling.",
        ],
        "context": [
            f"To understand {topic}, you have to go back to where this all started.",
            f"The conventional wisdom about {topic} is wrong. Here's why.",
            f"Before we dive in, we need to establish one thing about {topic}.",
            f"Three forces are converging to reshape {topic}. Let me break them down.",
        ],
        "deep_dive": [
            f"This is where {topic} gets really interesting.",
            f"The data on {topic} tells a story that surprised even me.",
            f"Here's the mechanism behind {topic} that most analyses miss.",
            f"Let me show you exactly how {topic} works in practice.",
            f"When you look under the hood of {topic}, you find something unexpected.",
            f"There's a specific pattern in {topic} that repeats across every industry.",
        ],
        "example": [
            f"Let me give you a concrete example of {topic} in action.",
            f"Here's a case study that illustrates {topic} perfectly.",
            f"I spoke to someone who's been on the front lines of {topic}.",
            f"This real-world example of {topic} shows why this matters so much.",
        ],
        "synthesis": [
            f"So what does all of this mean for {topic}?",
            f"When you connect the dots on {topic}, a clear picture emerges.",
            f"The implications of {topic} go far beyond what most people realize.",
            f"Here's the synthesis: everything we've covered about {topic} points to one conclusion.",
        ],
        "cta": [
            f"Here's what you should do with everything you've learned about {topic}.",
            f"The next step is clear. If {topic} matters to you, here's your move.",
            f"Thank you for watching this deep dive on {topic}.",
            f"If this changed how you think about {topic}, here's what to do next.",
        ],
    }

    options = narrations.get(purpose, [f"Let's explore {topic} in depth."])
    return options[idx % len(options)]


def _chapter_overlay(purpose: str, chapter_title: str, idx: int) -> str:
    """Generate text overlay for a scene."""
    if idx == 0:
        return chapter_title.upper()
    return ""


def _chapter_transition(purpose: str, idx: int, total: int) -> str:
    """Determine transition type."""
    if idx == total - 1 and purpose == "cta":
        return "fade_out"
    elif idx == 0:
        return "fade_in"
    elif purpose in ("synthesis",) and idx == 0:
        return "dissolve"
    return "cut"


def build_full_script(storyboard: Storyboard) -> str:
    """Build the complete script text from all scenes."""
    parts = []
    current_chapter = None

    for s in storyboard.scenes:
        if s.chapter != current_chapter:
            current_chapter = s.chapter
            # Find chapter duration
            ch = next((c for c in storyboard.chapters if c.title == s.chapter), None)
            ch_info = f" [{ch.duration:.0f}s]" if ch else ""
            parts.append(f"\n{'='*60}")
            parts.append(f"CHAPTER: {s.chapter}{ch_info}")
            parts.append(f"{'='*60}\n")

        parts.append(
            f"[SCENE {s.scene_number:03d} — {s.duration_seconds:.1f}s — {s.camera}]\n"
            f"VISUAL: {s.visual_description}\n"
            f"B-ROLL: {s.b_roll_suggestion}\n"
            f"NARRATION: {s.narration}\n"
            f"OVERLAY: {s.text_overlay}\n"
            f"TRANSITION: {s.transition}\n"
        )

    return "\n".join(parts)


def generate_script(brief: dict) -> Storyboard:
    """Generate a structured script and storyboard from a creative brief."""
    topic = brief['topic']
    tone = brief.get('tone', 'professional')
    platform = brief.get('platform', 'youtube')
    duration = brief.get('duration_seconds', 60)
    messages = brief.get('key_messages', [])
    profile = validate_output_profile_contract(brief, "creative brief")

    fmt = detect_format(duration)

    # Build chapter plan
    chapters = generate_chapter_plan(duration, fmt, messages)

    # Generate scenes within chapters
    scenes = generate_scenes(chapters, topic, tone, platform)

    # Build full script
    sb = Storyboard(
        title=topic,
        format=fmt.value,
        total_duration=duration,
        chapters=chapters,
        scenes=scenes,
        full_script="",
    )
    sb.full_script = build_full_script(sb)
    sb.output_profile = profile["output_profile"]
    sb.aspect_ratio = profile["aspect_ratio"]
    sb.resolution = profile["resolution"]
    return sb


def main():
    if len(sys.argv) < 2:
        print("Usage: python script_agent.py <creative_brief.json> [output_dir]")
        sys.exit(1)

    brief_path = sys.argv[1]
    output_dir = Path(sys.argv[2]) if len(sys.argv) > 2 else Path(brief_path).parent

    brief = load_brief(brief_path)
    storyboard = generate_script(brief)

    output_dir.mkdir(parents=True, exist_ok=True)

    # Write script text
    script_path = output_dir / "script.txt"
    atomic_write_text(script_path, storyboard.full_script)

    # Write storyboard JSON
    sb_path = output_dir / "storyboard.json"
    atomic_write_json(sb_path, {
        'title': storyboard.title,
        'format': storyboard.format,
        'output_profile': getattr(storyboard, 'output_profile', 'landscape'),
        'aspect_ratio': getattr(storyboard, 'aspect_ratio', '16:9'),
        'resolution': getattr(storyboard, 'resolution', '1920x1080'),
        'total_duration': storyboard.total_duration,
        'chapters': [asdict(c) for c in storyboard.chapters],
        'scenes': [asdict(s) for s in storyboard.scenes],
    })

    print(f"Script: {script_path}")
    print(f"  Format: {storyboard.format}")
    print(f"  Duration: {storyboard.total_duration:.0f}s ({storyboard.total_duration/60:.1f} min)")
    print(f"  Chapters: {len(storyboard.chapters)}")
    print(f"  Scenes: {len(storyboard.scenes)}")
    print(f"Storyboard: {sb_path}")


if __name__ == '__main__':
    main()
