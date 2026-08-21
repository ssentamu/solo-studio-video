# Solo / Soslo Studio PRD — AI Video & Motion Production Pipeline

**Status:** Draft v1.0  
**Canonical product URL:** https://edgescout.tech/video/  
**Canonical codebase:** `/opt/data/solo-studio-video/`  
**Wrong prior artifact:** `/opt/data/solo-studio/` and https://edgescout.tech/studio/ are the static-site misroute, not the core Solo Studio product.

---

## 1. Source Evidence & Recovery Notes

This PRD is grounded in the original archive session and subsequent refinements, not the later static-site detour.

### 1.1 Originating request

Archive session: `@session:default/20260803_082239_9417bf`  
Original user request:

> `https://x.com/zeuuss_01/status/2080408363507036432. Design a working model from this story`

The linked X article was titled:

> **How to Run a Design Studio Solo with Claude Code + Higgsfield MCP**

### 1.2 Original idea from the story

The article described a solo operator replacing a small delivery team with an agentic production stack:

1. **Fable 5 — design engine**  
   Takes a client brief and produces the design system: layouts, components, responsive states, visual language.

2. **Claude Code — build engine**  
   Turns the approved design/spec into implementation: components, CSS, animations, forms, CMS wiring, deployment.

3. **Higgsfield MCP — media engine**  
   Generates the media normally requiring shoots, motion designers, stock licensing, and video editors.

The critical media line from the article:

> “Every site needs visuals: a hero video, product shots, motion, avatar spots for landing pages. Instead of a shoot or stock licenses, Higgsfield generates them from prompts. And with the MCP connector, your agent reaches them without leaving the chat — it plans, generates, and pulls the finished clips straight into the build.”

The example prompt was explicitly video-first:

> “Using Higgsfield, generate this as a video — 16:9, ~6 seconds, high quality…”

The story also said the toolkit can:

- turn reference clips into ready-to-run prompts,
- build a marketing video from a product page,
- train a consistent character,
- cut a long clip into shorts,
- score a hook before posting.

### 1.3 Corrected product interpretation

Solo Studio is not primarily a static website generator. The intended product is:

> **A productized AI video/motion studio that turns a brief, URL, product page, or source pack into a finished video-production package — and ultimately real generated video clips / final MP4s — using an orchestrated research, script, visual, voiceover, generation, and editing pipeline.**

The “motion website” story is the business context. The product opportunity Daniel wanted is the **video/motion production pipeline** inside that model.

---

## 2. Product Summary

### 2.1 Product name

**Solo Studio** / **Soslo Studio**

### 2.2 One-line description

Solo Studio turns a topic, product page, or content brief into a complete AI-generated video package: research brief, script, storyboard, scene prompts, generated clips, voiceover, captions, timeline, and export-ready final assets.

### 2.3 Product promise

A solo operator should be able to go from:

> “Here is the product / story / URL / reference. Make the video.”

To:

> “Here is the finished video package, with generated clips, voiceover, captions, timeline, and editor-ready exports.”

without manually coordinating writers, designers, voice actors, motion designers, video generators, and editors.

### 2.4 Positioning

Solo Studio is an **AI creative operations system**, not just a prompt generator.

It competes with manual video-production workflows and lightweight AI video tools by owning the full production chain:

1. source intake,
2. creative strategy,
3. scripting,
4. storyboard,
5. shot prompting,
6. model routing,
7. clip generation,
8. voiceover,
9. captions,
10. timeline assembly,
11. export handoff.

---

## 3. Problem

High-quality short-form and marketing video production is still operationally heavy. A single client video may require:

- a strategist/researcher,
- a copywriter,
- a storyboard artist,
- a designer,
- a motion/video generation specialist,
- a voiceover artist,
- an editor,
- a captioner,
- QA and delivery coordination.

Existing AI tools reduce pieces of the work but leave the operator stitching everything together manually:

- one tool writes scripts,
- another generates images,
- another generates video clips,
- another handles TTS,
- another creates captions,
- another assembles the final timeline.

The original ZEUS/Higgsfield story identified the key business shift: the production layer moves from headcount to models. The product opportunity is to operationalize that shift into a repeatable studio system.

---

## 4. Target Users

### 4.1 Primary users

1. **Solo creative operators / AI agencies**  
   Need to deliver client videos quickly without hiring a full production team.

2. **Content entrepreneurs / YouTube builders**  
   Need repeatable production packages for long-form videos, shorts, thumbnails, and clips.

3. **Product marketers / startup founders**  
   Need product explainer videos, launch clips, ads, and landing-page motion assets.

4. **Social media operators**  
   Need platform-specific short videos with hooks, captions, and variants.

### 4.2 Secondary users

- Real-estate marketers needing overlay/motion videos.
- Course creators needing educational animations.
- Local businesses needing ads and social clips.
- Agencies needing production speed and reskinnable templates.

---

## 5. Core Jobs To Be Done

### JTBD-1 — Generate a video package from a topic

When I have a topic or thesis, I want Solo Studio to research, script, storyboard, and package a video so I can publish or hand it to an editor.

### JTBD-2 — Generate a marketing video from a URL/product page

When I have a product page or landing page, I want Solo Studio to extract the product positioning, audience, hooks, proof, CTA, and visual style so it can generate a video ad or explainer.

### JTBD-3 — Generate motion assets for a page or campaign

When I need a hero video, product shots, motion loops, or avatar clips, I want Solo Studio to generate scene-level prompts and clips that can be pulled into a build or campaign.

### JTBD-4 — Turn reference videos into prompts

When I see a winning ad or visual style, I want Solo Studio to reverse-engineer it into reusable prompts, shot structure, pacing, and production rules.

### JTBD-5 — Produce publish-ready deliverables

When the generation is done, I want a downloadable package containing clips, voiceover, captions, timeline, final MP4, and source manifests so I can ship or revise quickly.

---

## 6. Product Scope

### 6.1 In scope

Solo Studio must support:

- manual brief input,
- template-based video creation,
- URL/product page reverse-briefing,
- source document ingestion,
- research brief generation,
- video script generation,
- storyboard generation,
- visual prompt generation,
- video model prompt generation,
- Seedance/Higgsfield-style 4-beat prompt plans,
- actual clip generation when provider auth is configured,
- clear dry-run/prompt-only state when generation is not configured,
- voiceover script generation,
- TTS voiceover generation when provider auth is configured,
- music prompt generation,
- captions/SRT generation,
- assembly manifest generation,
- FCPXML/editor timeline export,
- downloadable project package,
- job queue/status/progress UI,
- output state labels that distinguish prompts vs real clips vs final video.

### 6.2 Out of scope for v1

- Full collaborative editor.
- Native browser-based nonlinear video editing.
- Real-time multiplayer review.
- Enterprise DAM permissions.
- Automatic public posting to social accounts.
- Full CMS/static website generation as the primary product.
- Claiming “final video complete” unless clips and final MP4 are actually generated and verified.

---

## 7. Product Architecture

### 7.1 Canonical pipeline

```text
Input Brief / URL / Source Pack
        ↓
[1] Source Ingest / Reverse Brief Agent
        ↓
[2] Research Agent
        ↓
[3] Script Agent
        ↓
[4] Storyboard / Scene Planner
        ↓
[5] Design / Visual Direction Agent
        ↓
[6] Production Agent
        ↓
[7] Video Generation Agent
        ↓
[8] Voiceover / Audio Agent
        ↓
[9] Editing Agent
        ↓
[10] Export / Delivery Agent
```

### 7.2 Existing local implementation

Verified local codebase: `/opt/data/solo-studio-video/`

Currently present locally:

- `api.py` — FastAPI app and job API.
- `worker.py` — background job processor.
- `pipeline.py` — CLI pipeline orchestrator.
- `frontend/index.html` — UI.
- `templates.json` — eight prebuilt templates.
- `engines/research_agent.py` — brief to creative brief.
- `engines/script_agent.py` — creative brief to script/storyboard.
- `engines/design_agent.py` — visual prompts and thumbnail prompts.
- `engines/production_agent.py` — voiceover script, video prompts, music prompt.
- `engines/editing_agent.py` — captions and assembly manifest.
- `engines/editor_export.py` — `timeline.fcpxml` export.

### 7.3 Refinements recorded in production notes

The Solo Studio skill/reference adds these intended or previously deployed refinements:

- `generation_agent.py` between Production and Editing.
- Seedance/Higgsfield generation plan:
  - reads `video_prompts.json`,
  - writes `clips/generation_plan.json`,
  - emits Seedance-ready 4-beat prompts,
  - optionally calls Higgsfield CLI when `SOLO_STUDIO_ENABLE_HIGGSFIELD=1`,
  - downloads `clips/scene_NN.mp4` when authenticated,
  - otherwise records clear setup-needed/dry-run reason.
- `source_ingest_agent.py` for document/source ingestion using Firecrawl Anydoc where available.
- FCPXML export for DaVinci Resolve / Premiere / Final Cut style handoff.

Important local-state gap: the local repo currently does **not** contain `engines/generation_agent.py` or `engines/source_ingest_agent.py`, despite production notes describing them. This must be reconciled before implementation is considered complete.

---

## 8. Functional Requirements

### 8.1 Intake

#### FR-INTAKE-1 — Manual brief creation

The user can create a video job by entering:

- topic,
- audience,
- platform,
- duration,
- tone,
- key messages,
- visual style,
- CTA,
- optional references.

#### FR-INTAKE-2 — Template creation

The user can start from prebuilt templates such as:

- AI developer tools deep dive,
- startup fundraising playbook,
- product-market fit explainer,
- remote team culture,
- AI/ML crash course,
- solo founder survival guide,
- system design interview prep,
- 60-second tech news.

#### FR-INTAKE-3 — URL reverse brief

The user can paste a product page, landing page, article, or winning ad reference. Solo Studio extracts:

- target audience,
- value proposition,
- proof points,
- hooks,
- objections,
- CTA,
- visual tone,
- brand vocabulary,
- suggested video angle.

#### FR-INTAKE-4 — Source pack ingestion

The user can provide documents such as PDF, DOCX, PPTX, CSV, EPUB, or web pages. Solo Studio converts them into normalized Markdown/source context and cites which source informed each major claim.

### 8.2 Research and creative strategy

#### FR-RESEARCH-1 — Creative brief

The system generates `creative_brief.json` containing:

- title,
- thesis,
- audience,
- tone,
- platform,
- hook candidates,
- key messages,
- proof points,
- story angle,
- risk/claim notes,
- visual direction,
- CTA.

#### FR-RESEARCH-2 — Claim discipline

For factual or educational videos, the system must separate:

- sourced claims,
- inferred claims,
- creative copy,
- unsupported claims that need user review.

### 8.3 Script and storyboard

#### FR-SCRIPT-1 — Format detection

The system must classify video format by duration:

- short: under 2 minutes,
- medium: 2–10 minutes,
- long: 10–30 minutes,
- documentary: over 30 minutes.

#### FR-SCRIPT-2 — Chapter plan

The script engine must produce a chapter structure appropriate to format and platform.

#### FR-SCRIPT-3 — Scene storyboard

The storyboard must include, per scene:

- scene number,
- chapter,
- duration,
- visual description,
- narration,
- text overlay,
- B-roll suggestion,
- transition,
- camera direction.

#### FR-SCRIPT-4 — Full script

The system must generate `script.txt` and include narration in `storyboard.json`.

### 8.4 Visual direction and prompts

#### FR-VISUAL-1 — Scene visual prompts

For each scene, Solo Studio generates image/reference prompts that specify:

- subject,
- action,
- setting,
- camera,
- lighting,
- style,
- negative constraints.

#### FR-VISUAL-2 — Thumbnail prompt

For YouTube-style outputs, Solo Studio generates `thumbnail_prompt.json` with:

- title overlay,
- visual prompt,
- maximum visible text constraint,
- CTR-oriented composition notes.

#### FR-VISUAL-3 — Reference management

The system must support locking reference assets and instructing the video model what each reference controls and what it must not change.

### 8.5 Video generation prompts

#### FR-PROD-1 — Multi-provider prompts

For each scene, Solo Studio generates prompts for:

- Runway,
- Pika,
- Kling,
- Higgsfield/Seedance.

#### FR-PROD-2 — Seedance/Higgsfield 4-beat structure

For 30-second or longer Seedance-style clips, prompts should follow:

- 0–6s: set scene,
- 6–14s: build,
- 14–24s: turn,
- 24–30s: ending.

Each beat should specify:

1. what is in the shot,
2. what it is doing,
3. where it is,
4. camera behavior,
5. visual style,
6. hard rules / negative constraints.

#### FR-PROD-3 — Prompt-only honesty

If no video generation backend is authenticated, the system must explicitly mark the output as:

> `prompt_package_only`

and must not imply generated clips exist.

### 8.6 Actual video clip generation

#### FR-GEN-1 — Generation agent

The system must include a generation agent that:

- reads `video_prompts.json`,
- chooses provider/model or uses configured provider,
- creates `clips/generation_plan.json`,
- submits scene prompts to provider,
- polls generation status,
- downloads generated clips,
- writes `clips/scene_NN.mp4`,
- records provider metadata and errors.

#### FR-GEN-2 — Higgsfield CLI integration

When enabled:

```text
SOLO_STUDIO_ENABLE_HIGGSFIELD=1
```

and the Higgsfield CLI is installed/authenticated, Solo Studio should call Higgsfield to generate actual scene clips.

#### FR-GEN-3 — Safe dry-run

When CLI/auth/provider is missing, the generation agent must still produce a valid generation plan and a clear `setup_needed` reason.

#### FR-GEN-4 — Clip verification

A clip is considered generated only if:

- file exists,
- file size is non-zero,
- duration can be probed by `ffprobe`,
- manifest records scene-to-file mapping,
- status UI reports generated clip count.

### 8.7 Voiceover and audio

#### FR-AUDIO-1 — Voiceover script

The production stage writes `audio/voiceover_script.txt`.

#### FR-AUDIO-2 — TTS generation

When TTS provider auth is configured, the system generates `audio/voiceover.mp3`.

#### FR-AUDIO-3 — Music prompt

The system generates `music_prompt.txt` for Suno/Udio/other music tools.

#### FR-AUDIO-4 — Audio state honesty

If voiceover audio is not generated, UI/package must state:

> `voiceover_script_only`

### 8.8 Editing and export

#### FR-EDIT-1 — Captions

The system generates `captions.srt` with timed subtitle chunks.

#### FR-EDIT-2 — Assembly manifest

The system generates `assembly_manifest.json` with:

- timeline timings,
- scene durations,
- transition instructions,
- text overlays,
- visual assets,
- video assets,
- voiceover/music references,
- export settings,
- editing notes.

#### FR-EDIT-3 — Editor timeline

The system generates `timeline.fcpxml` compatible with professional editor import workflows.

#### FR-EDIT-4 — Final MP4 assembly

When all clips and audio exist, the system should assemble a final MP4 using FFmpeg or a dedicated renderer. The assembled final file should be named:

```text
final/video.mp4
```

A final MP4 is valid only if `ffprobe` confirms duration, codec, and non-zero streams.

### 8.9 UI and job management

#### FR-UI-1 — Job creation

The UI supports creating jobs manually and from templates.

#### FR-UI-2 — Progress tracking

The UI shows:

- queued/running/completed/failed,
- active stage,
- progress percentage,
- scene count,
- chapter count,
- duration,
- whether visuals exist,
- whether voiceover exists,
- whether clips exist,
- whether final MP4 exists.

#### FR-UI-3 — Download package

Completed jobs provide downloads for:

- full project zip,
- script,
- storyboard,
- video prompts,
- captions,
- FCPXML timeline,
- clips when available,
- final MP4 when available.

#### FR-UI-4 — Failure transparency

Failures must show:

- failing stage,
- exact error summary,
- whether partial package is usable,
- next action to fix.

---

## 9. Data Model / Artifact Contract

Each job output directory should contain:

```text
output/<job_id>/
├── brief.yaml
├── sources_md/
│   ├── manifest.json
│   └── *.md
├── creative_brief.json
├── script.txt
├── storyboard.json
├── visual_prompts.json
├── thumbnail_prompt.json
├── visuals/
│   └── scene_NN.png
├── video_prompts.json
├── clips/
│   ├── generation_plan.json
│   ├── scene_NN.mp4
│   └── provider_metadata.json
├── audio/
│   ├── voiceover_script.txt
│   ├── voiceover.mp3
│   └── background_music.mp3
├── music_prompt.txt
├── captions.srt
├── assembly_manifest.json
├── timeline.fcpxml
├── final/
│   └── video.mp4
└── package_manifest.json
```

### 9.1 Package status enum

Each job must expose one canonical package status:

- `failed`
- `research_only`
- `script_package`
- `prompt_package_only`
- `editor_package`
- `clips_generated`
- `final_video_ready`

The UI must never label a project as complete/final video unless status is `final_video_ready`.

---

## 10. Non-Functional Requirements

### 10.1 Reliability

- Job state must persist across process restarts.
- Each stage should be idempotent or safely resumable.
- Partial artifacts should be retained on failure.
- Worker errors must be visible in API/UI.

### 10.2 Verification

- Generated files must be verified before status changes.
- MP4 files require `ffprobe` verification.
- ZIP/package downloads require manifest checks.
- Final status must be derived from actual files, not agent claims.

### 10.3 Security

- No credentials in job output.
- Environment variables exposed only by name in diagnostics.
- Uploaded source files must not be publicly exposed unless included in deliberate package export.
- Provider errors must not leak tokens.

### 10.4 Cost control

- Estimate generation cost before running actual video generation.
- Allow prompt-only dry run.
- Allow per-job budget cap.
- Record provider/model/cost metadata per scene.

### 10.5 Performance

Target v1 performance:

- Prompt/editor package under 2 minutes for short videos.
- Clip generation asynchronous with progress updates.
- UI remains responsive while jobs run.

---

## 11. Success Metrics

### Product metrics

- Time from brief to prompt/editor package: under 2 minutes for short videos.
- Time from brief to final generated clips: provider-dependent, but fully asynchronous.
- Percentage of jobs reaching `editor_package` without failure: >95%.
- Percentage of generated clips passing file verification: >90%.
- Manual intervention required per short video: under 10 minutes.

### Business metrics

- Solo operator can produce at least 5 client-ready video packages per day.
- Package output is good enough for sales demos without manual recreation.
- Cost per generated short stays below a configurable budget cap.

---

## 12. MVP Definition

The MVP is not “a pretty prompt page.” The MVP is a verified package generator.

### MVP must include

1. Live UI at `/video`.
2. Manual and template job creation.
3. Job queue and progress status.
4. Research brief output.
5. Script and storyboard output.
6. Scene visual prompts.
7. Multi-provider video prompts.
8. Voiceover script.
9. Music prompt.
10. Captions SRT.
11. Assembly manifest.
12. FCPXML timeline export.
13. Package manifest with honest status.
14. Downloadable package.
15. Clear distinction between prompt-only and generated-video states.

### MVP acceptance test

Given a 60-second TikTok template, when a user creates a job, the system must produce:

- `creative_brief.json`
- `script.txt`
- `storyboard.json`
- `visual_prompts.json`
- `video_prompts.json`
- `audio/voiceover_script.txt`
- `music_prompt.txt`
- `captions.srt`
- `assembly_manifest.json`
- `timeline.fcpxml`
- `package_manifest.json`

and the UI must show `prompt_package_only` or higher with no false claim that MP4 clips exist.

---

## 13. V1 Definition — Real Video Generation

V1 upgrades MVP from “video production package” to “generated clip pipeline.”

### V1 must include

1. `generation_agent.py` in repo and container.
2. Higgsfield/Seedance provider integration.
3. Dry-run mode and enabled mode.
4. Scene-level generation plan.
5. Actual `clips/scene_NN.mp4` downloads.
6. Clip verification with `ffprobe`.
7. UI clip availability state.
8. Regenerate individual scene.
9. Provider error surface.
10. Cost estimate and per-job budget cap.

### V1 acceptance test

Given provider auth and generation enabled, when a 3-scene video job runs, the system must produce:

- `clips/generation_plan.json`,
- `clips/scene_01.mp4`,
- `clips/scene_02.mp4`,
- `clips/scene_03.mp4`,
- provider metadata,
- verified clip durations,
- job status `clips_generated`.

---

## 14. V2 Definition — Final Video Assembly

V2 turns generated assets into a publishable final video.

### V2 must include

1. FFmpeg assembly from clips, voiceover, music, and captions.
2. Final MP4 export.
3. Burned-in caption option.
4. Platform presets:
   - 16:9 YouTube,
   - 9:16 TikTok/Reels/Shorts,
   - 1:1 square,
   - 4:5 social feed.
5. Thumbnail generation.
6. Scene replacement/regeneration.
7. Download final video.
8. Export package zip.

### V2 acceptance test

Given verified clips and audio, the system must create `final/video.mp4`; `ffprobe` must confirm:

- non-zero duration,
- expected resolution,
- at least one video stream,
- expected audio stream when voiceover/music exists.

Job status becomes `final_video_ready` only after this verification passes.

---

## 15. V3 Definition — Studio Operating System

V3 turns the pipeline into a productized solo-agency operating system.

### V3 capabilities

- Client/project workspaces.
- Brand kits and reusable visual identities.
- Reference clip library.
- Consistent character profiles.
- Campaign templates.
- Hook scoring.
- Long-video-to-shorts repurposing.
- Revision requests.
- Approval links.
- Retainer workflow: recurring monthly video generation and update queue.
- Public portfolio/demo builder.

---

## 16. Critical Fixes Required Before Next Build

### P0 — Stop presenting static `/studio` as Solo Studio

The `/studio` static website generator is the wrong branch. Public product references should point to `/video` unless explicitly discussing the misroute.

### P0 — Fix worker completion bug

Current `worker.py` imports:

```python
from datetime import datetime
```

but uses:

```python
datetime.now(timezone.utc)
```

Required:

```python
from datetime import datetime, timezone
```

Without this, jobs can fail at the final assembly/completion stage after generating most artifacts.

### P0 — Add honest package status

Current UI/API should not rely only on `status=completed`. It needs artifact-derived package status:

- prompts only,
- editor package,
- clips generated,
- final video ready.

### P1 — Reconcile missing generation/source ingest agents

Production notes mention `generation_agent.py` and `source_ingest_agent.py`, but the current local codebase does not contain them. Decide whether to:

1. recover from VPS/container,
2. reconstruct from notes,
3. or reimplement cleanly.

### P1 — Implement actual clip generation

Add Higgsfield/Seedance backend and artifact verification.

### P1 — Package download

Create zip downloads and package manifests so output is usable outside the app.

---

## 17. Risks

1. **Provider path maturity risk:** Real clip generation is available but remains **provider-dependent and opt-in** (`SOLO_STUDIO_ENABLE_HIGGSFIELD=1` + authenticated Higgsfield CLI). Without explicit enablement, output intentionally stays in prompt/editor-package mode (`editor_package` / `prompt_package_only`).
   **Guardrail:** artifact-derived package states and explicit `setup_needed` reasons keep status truthful.

2. **Auth/session risk:** token persistence in browser storage is no longer used. Operator auth now uses server-issued HttpOnly cookies and short-lived session auth, but this is still **single-operator, in-memory session state**.
   **Guardrail:** keep production use constrained; avoid multi-tenant exposure until distributed session store + hardened tenancy controls are added.

3. **Rollback risk:** previous deploy scripts lacked explicit rollback tagging. Deploy/recover flow in-repo now tags `release-*` and `rollback-*` images and runs bounded smoke checks.
   **Guardrail:** operationally enforce use of the in-repo deploy script only; avoid ad-hoc replacement commands.

4. **Misclassification risk:** The product can drift back into static-site generation. Guardrail: Solo Studio defaults to `/video`; `/studio` is explicitly a separate static-site detour.

5. **False completion risk:** Prompt packages may be mistaken for final videos. Guardrail: artifact-derived statuses and file verification.

6. **Cost risk:** Video generation can become expensive. Guardrail: budget caps and preflight cost estimates.

7. **Quality risk:** Generated clips may be inconsistent across scenes. Guardrail: locked references, character profiles, regenerate-scene workflow.

8. **Source fidelity risk:** URL/source-derived claims may be hallucinated. Guardrail: source manifests and unsupported-claim flags.

---

## 18. Open Questions

1. Should Solo Studio focus first on **short ads/social clips** or **YouTube deep-dive packages**?
2. Which provider is canonical for first real clip generation: Higgsfield CLI, Seedance direct, Runway, Pika, Kling, or a provider abstraction?
3. Should final MP4 assembly happen inside the app, or should v1 stop at editor-ready FCPXML plus verified clips?
4. Should `/studio` be removed, redirected, or retained as a separate “motion website builder” demo?
5. What is the first customer-facing template category Daniel wants to sell: startup ads, YouTube explainers, local business ads, product launch videos, or AI agency demos?

---

## 19. Recommended Build Order

1. Fix `worker.py` timezone bug.
2. Add `package_manifest.json` and artifact-derived status.
3. Add package zip download.
4. Reconcile/rebuild `generation_agent.py`.
5. Add Higgsfield/Seedance dry-run plus enabled mode.
6. Add clip verification.
7. Add UI distinction between prompt package / clips / final video.
8. Add final MP4 assembly.
9. Add URL reverse-brief and source ingest to the UI.
10. Add regenerate-scene workflow.

---

## 20. Final Product Definition

Solo Studio is successful when a solo operator can take a URL, product idea, topic, or reference video and produce a verified, downloadable video package — and eventually a final MP4 — with minimal manual coordination.

The key product standard is:

> **No fake completion. No “video” claims for prompt-only output. Real artifacts, verified files, clear status, and a repeatable pipeline from brief to publishable motion.**
