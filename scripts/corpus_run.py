#!/usr/bin/env python3
"""EarWitness corpus runner: RSS feeds -> transcripts -> ad-read detections
-> [Jev window screen] -> [brand naming] -> [speaker diarization] -> [LLM
screen] -> evidence clips.

Crash-safe, resumable, memory-bounded. All state lives in a separate corpus
directory (<corpus>/queue.sqlite3 is the source of truth).

  shows     --out shows.json [--country us] [--apple-top 200] [--apple-genres all]
            [--apple-genre-top 100] [--spotify-top 200] [--spotify-categories all]
            [--spotify-category-top 50] [--verify [--workers 8] [--feed-max-mb 128]
            [--languages en]]
  init      --corpus DIR --shows shows.json --per-show N
  add-shows --corpus DIR --shows shows.json
  discover  --corpus DIR [--per-show N] [--workers 8] [--feed-max-mb 128]
            [--since YYYY-MM-DD] [--until YYYY-MM-DD | --keep-current] [--new-shows]
            [--languages en]
  work      --corpus DIR --worker-id ID --stages fetch,transcribe,detect,clip
            [--no-language-hint] [--repair-backend parakeet --repair-python PY]
            [--backend parakeet --stt-python PY [--gap-fill-model REPO --gap-fill-min-s 3
             --allow-gap-fill-change]]
            [--stages ...,screen,clip --screen-model claude-haiku-5-5]
            [--stages jev[,clip] --jev-threshold 0.8 --jev-clip-pad-s 45 --jev-clip-max-s 240]
            [--stages name --allow-no-clip --name-gliner small --jev-context-windows 0]
            [--stages diarize --allow-no-clip --diarize-device mps|cpu
             --diarize-mem-cap-mb 4500 --diarize-python PY --diarize-window-s 1800]
  status    --corpus DIR [--json]
  export    --corpus DIR [--corpus DIR ...] --out DIR2 [--corrections FILE]
  export-delta   --config FILE   (one signed package of new revisions for the host)
  export-receipt --config FILE RECEIPT   (a signed host receipt or reconcile-<epoch>.json)
  export-tidy    --config FILE   (withdraw removed/paused offered packages; builds nothing)
  screen    --corpus DIR (--enable [--model M] | --disable)
  requeue-dead --corpus DIR (--episodes ID,ID | --all)
  mark-dead --corpus DIR --episodes ID,ID --reason TEXT [--dry-run]
  repair-seams --corpus DIR [--dry-run]
  jev-backfill --corpus DIR [--threshold 0.8] [--dry-run] [--rate 10]
  jev-budget (init | status | recover --reason TEXT) [--ledger FILE] [--pin FILE]
  name-backfill --corpus DIR [--jev-threshold 0.8] [--jev-context-windows 0]
            [--name-gliner small|none] [--brand-list FILE]
  rename    --corpus DIR [--corpus DIR ...] [--episodes ID,ID | --episodes-file F]
            [--any-stage] [--dry-run] [--merge-rule both|deployed]
            [--second-brand-min-score 7.0 | --no-second-brand] [--max-span-windows 3]
            [--name-sub-window-s 20] [--name-gliner small|none]
            [--brand-list FILE|recorded] [--canon] [--corrections FILE]
            [--cue-spans tags,promos] [--promo-shows FILE] [--display-spelling FILE]
  score     (--export DIR | --corpus DIR ...) --labels DIR [--labels DIR ...]
            [--label-transcripts DIR] [--out FILE]
  align-labels --labels DIR [--labels DIR ...] --label-transcripts DIR
            --transcripts DIR --out DIR
  diarize-backfill --corpus DIR [--diarize-device mps|cpu] [--diarize-mem-cap-mb N]
            [--diarize-python PY] [--diarize-window-s 1800] [--max-episodes N]
  refine    --corpus DIR [--model large-v3-turbo] [--backend mlx] [--stt-python PY]
            [--only-tiny|--no-only-tiny] [--max-episodes N] [--rerefine] [--dry-run]
            [--clip-timeout-s 300] [--name-gliner small|none] [--brand-list FILE]
            [--backend groq|openrouter [--env-file PATH] [--max-cost-usd N] [--api-rpm N]]
            [--corrections FILE]
  repair-loops --corpus DIR [--backend parakeet|mlx] [--model M] [--stt-python PY]
            [--requeue [--requeue-anyway]] [--max-episodes N] [--dry-run]
  canon     --corpus DIR [--corpus DIR ...] [--out FILE] [--apply] [--corrections FILE]
  show-identity --corpus DIR [--max-fetches 200] [--refresh] [--corrections FILE]
  shownotes --corpus DIR [--corpus DIR ...]
  corrections-labels --corrections FILE --out DIR
  transcribe-one   (internal; run by `work` as a supervised child)
  repair-one       (internal; run by `work --repair-backend` after transcribe-one)
  refine-stt       (internal; run by `refine` as its speech child)

`shows` builds a show-list file from public charts (Apple top + genre
charts via the legacy iTunes RSS chart, Spotify Podcast Charts top +
category charts), resolves each show to its RSS feed through the iTunes
lookup / search API, and merges them; each show keeps every chart tag and
rank (earwitness/pipeline/show_sources.py). A chart set to 0 is skipped.
--verify loads every feed and drops shows whose feed fails or has no audio,
with the reason. `init` creates a corpus (re-running it adds shows and
updates their title/category/source); `add-shows` only adds the shows a
corpus lacks: a feed already there under another spelling (http/https,
"www.", trailing slash) is skipped and a show whose Apple id is already
there with another feed (a host move) is reported, not added; rows
without an Apple id (older corpora) get theirs from the list by feed
first, and a show with the same title as such a row is reported as a
possible_move, not added; the only change to an existing show is filling
its empty apple_id. `discover`
enqueues episodes: by
default each show's --per-show newest (the corpus's per_show when not
given); --since/--until pull the audio episodes dated in that window
(UTC dates, inclusive);
--keep-current takes only episodes dated on or after the show's newest
queued one (a show with none gets the default); --new-shows limits the run
to shows with no episode yet. With --since/--until/--keep-current,
--per-show caps the episodes per show per run (default: no cap). discover
only inserts (one short transaction per show), so it is idempotent and safe
beside running workers. init, add-shows and discover each append their
arguments and counts to the corpus's discovery_log table (`status` shows
the last run). Feeds over --feed-max-mb are refused. Parsing a feed takes
~18x its size in memory (~1.5 GB for an 85 MB feed); feeds parsed at once
share a 40 MB byte budget and a bigger feed parses alone. Downloaded feeds
wait for the budget in memory, so peak memory is ~18x max(40 MB, biggest
feed) plus up to --workers x --feed-max-mb of waiting raw feeds (in practice
--workers x the biggest feeds).

Show notes: every discover stores the RSS item notes (description,
content:encoded, itunes:summary) of the episodes it enqueues and of the
episodes the corpus already holds that are still in the feed (matched by
episode id, else guid) in <corpus>/shownotes/<ep>.json; `discover
--per-show 0` backfills them without enqueuing. add-shows only adds show
rows: its shows' notes come with their first discover. `shownotes` then
confirms or corrects the brand of each named read from its episode's
listed sponsors (shownotes-reads/<ep>.json; earwitness/pipeline/shownotes.py)
and export carries shownotes_status / _match / _brand / _evidence on the
named reads. It never creates a read and never changes brand / span
identity: a correction sets brand_canonical, and only an exact, alias or
spoken-domain match clears needs_review.

Show language: `shows --verify`, init / add-shows (from the show list) and
every discover store each show's RSS channel <language> on its show row
(discover --per-show 0 refreshes show rows, language included, without
enqueuing). `--languages en` (shows --verify, discover) keeps only shows in
those languages; shows with no clear language tag are kept and counted.
Whisper transcribe workers decode in the show's language when it is a clear
code, unless every language probe of a window votes another language at
0.8 or more (--no-language-hint: vote every window as before).

Transcript quality: every transcript carries a "quality" record of repeat
loops (a 1-8 word phrase repeated back to back, 20+ words) and, with a show
language, Whisper windows voted into another language with 100+ words.
`work --repair-backend parakeet --repair-python PY` re-transcribes each
flagged stretch +-30 s while the audio is still here (a repair-one child
after transcribe-one) and splices it in (segments "src": "repair",
provenance "repairs" keeps the replaced text); a failed repair keeps the
transcript unrepaired (quality "repair_error"). `repair-loops` does the same
for done episodes: from the audio when this host still has it, else from an
evidence clip holding the whole stretch (coverage reported). By default it
only rewrites transcripts and lists the episodes to re-name; --requeue sends
them back to detect, only while detect, jev and name workers are live (or
with --requeue-anyway), and never re-runs the paid screen.

Audio is deleted only after the clip stage, so `work` refuses --stages with
transcribe/detect/screen but no clip unless --allow-no-clip (another worker
runs clip). `screen` is optional; a worker started with it enables it for
the corpus, and detect routes to screen only while a screen worker has been
seen in the last 10 minutes (else straight to clip, screen_status "skipped:
no screen worker"). `screen --disable` turns routing off. Screen workers need
`pip install anthropic` and ANTHROPIC_API_KEY (or ANTHROPIC_AUTH_TOKEN); the
key and model are checked before claiming. A rejected key, permission or
credit error releases the lease and exits with status 3.

`jev` (TypeSafe Jev, ~$0.0008/episode) scores every 60-s transcript window
for ad-read likelihood (noul). It runs between detect and screen, with the
same routing rule as screen (a jev worker seen in the last 10 minutes, else
jev_status "skipped: no jev worker"). Jev workers need TYPESAFE_API_KEY; the
key is checked (GET /v1/models) before claiming and a rejected key or
payment error exits with status 3. The clip stage then cuts clips around Jev
windows at noul >= --jev-threshold plus rule candidates that overlap them or
are confidence "high"; episodes without a Jev result keep every rule
candidate. `jev-backfill` screens finished episodes' transcripts (no audio,
no clips); `export` writes Jev windows where `app_workspace.py seed` reads
them (experiments/jev-ad-triage-20260921/results.json).

`name` (local, no API) names the brand of each Jev-flagged ad span:
domain/CTA extraction, then GLiNER-small when extraction is weak, then
sound-alike repair against a brand list harvested from the corpus's
transcripts (rebuilt at each worker start, kept in <corpus>/brand-list.json).
It runs after a successful jev, routed only while a name worker was seen in
the last 10 minutes (else name_status "skipped: no name worker"). GLiNER
needs `pip install -r requirements-namer.txt`; without it the stage runs
extraction + repair and records that in every names/<ep>.json. Each flagged
window is named on its own text (plus --jev-context-windows windows each
side; default 0, measured best on dev); neighbouring windows merge into one
span by the merge rule (default "both": the same brand, or either name needs
review, or either brand is among the other window's candidates; never two
confident different brands; joins other than on the same brand stop at 3
windows; the most confident window names the span), and a span can carry
second brands (ad pods; exported as one more named read each, id
"<span id>.b-<brand>"). Clips
then follow the named spans; `export` writes them as candidates
(experiments/named-reads/output/candidates_normalized.json, source
"jev+namer") which `app_workspace.py seed` imports. `name-backfill` names
done episodes that already have Jev results (transcripts only). Each brand
list a namer is built from is kept by digest in <corpus>/brand-lists/ (names
docs record the digest), because brand-list.json is rebuilt at every start.

`rename` runs the name stage again on done episodes (or --episodes; never
episodes still in the queue unless --any-stage) from their transcripts and
Jev results, with the current rules; refined episodes are re-named on their
refined text as `refine` does, so refine still skips them. It writes a names
file only when the result differs (a rerun changes nothing), takes no leases
and writes no queue state (safe beside a live fleet; a names file that
changes meanwhile is left alone), and --dry-run only reports. --brand-list
recorded re-uses the archived list each episode was named with; the default
harvests the corpus now, like a worker start (corpora named before the
archive existed fall back to brand-list.json when its digest matches). Export recomputes self-promo
and voice attrs from names, so they follow; the brand canon map goes stale:
--canon rebuilds and applies it over every --corpus. Re-naming can change
span ranges, so the app's named-read ids (episode + span + transcript
version): re-name before the app's first real load.

`score` scores the named reads of an export (or of --corpus dirs, exported
to a temporary dir) against a labels folder: location recall, found + brand
right precision / recall (lenient = the labeling protocol, strict), each
with its denominators, for ads / confident ads / all named reads, per
transcript source (tiny, parakeet, +gapfill, +refine), and the self-promo
flag against labeled self-promo. `align-labels` moves labels made on one
fetch of an episode onto another fetch's transcript (dynamic ad insertion
shifts and replaces spots; an episode whose audio checksum is unchanged keeps
its label times): reads whose words are gone are marked absent_in_audio; labels-matched/ makes them uncertain and the new audio
with no counterpart unresolved, so neither counts.

`diarize` (local, no API) labels who speaks when with NVIDIA
Nemotron-3-Diarization (open weights, OpenMDW license) in a supervised child
process: --diarize-python is a python with requirements-diarize.txt (default:
this interpreter), --diarize-device mps (Apple GPU, ~180x real time) or cpu
(~70x), --diarize-mem-cap-mb the child's RSS cap (killed above it). Audio is
diarized in --diarize-window-s windows (default 1800) overlapping 120 s, so
memory stays bounded for 5-hour episodes; speakers are linked across windows
by their shared speech in the overlap (earwitness/pipeline/diarize.py lists
the limits). It runs after detect/jev/name and before screen/clip (it needs
the audio), routed only while a diarize worker was seen in the last 10
minutes (else diarize_status "skipped: no diarize worker"). A failure never
kills an episode: OOM or timeout moves on at once, other errors retry, and
the episode continues without speakers (diarize_status "failed: ..."). The
python is checked before claiming: it must load the pinned weights from the
local Hugging Face cache and diarize a few seconds under the RSS cap (exit 2
otherwise). A worker whose child fails the same way on 3 different episodes
in a row stops (fatal, exit 3) and gives the lease back uncharged.
Run diarize workers WITHOUT an auto-restart loop (no `while true`, launchd
KeepAlive or supervisor restart): exit 3 means the diarize python, device or
weights cache is broken, and a restarted worker would fail the next episodes
the same way. A stopped worker is safe: once no diarize worker has been seen
for ~600 s, the queue skips diarize for the waiting episodes ("skipped: no
diarize worker") and they go on to screen/clip without speakers; fix the
cause, then restart by hand (diarize-backfill covers episodes that still hold
their audio).
Results: speakers/<ep>.json (turns, per-speaker seconds, model revision,
device). `export` then adds dominant_speaker / voice_share_outside_ads /
voice_ad_only to each named read (seed keeps them outside evidence identity)
and writes speaker-transcripts/<ep>.json (word-level turns when the transcript
has word timestamps, else segment-level with mixed_speakers).
`diarize-backfill` diarizes episodes that still hold their full audio on this
host (e.g. --keep-audio corpora); episodes with only clips are skipped,
because clips are too short for episode-level speaker identity.

`refine` re-transcribes the evidence clips around Jev windows / named spans
of done episodes with a stronger model (default large-v3-turbo on mlx;
--only-tiny, the default, limits it to episodes first transcribed by a tiny
model) and re-names their spans on that text. It takes no leases and writes
no queue state, so it can run beside a live fleet. Speech runs in a child
process (`--stt-python`, e.g. a venv with mlx-whisper); naming runs in this
process (the venv with GLiNER). Results: refined/<ep>.json, and names/<ep>.json
rewritten with "refined": true and the earlier naming under "previous";
`export` then carries the refined names.

Hosted speech (`--backend groq|openrouter`, earwitness/pipeline/api_stt.py)
runs refine's clip transcription, or the `work` transcribe stage (chunked
upload of 600-s FLAC windows, seam-stitched), on a provider API instead of
a local model. The key comes from GROQ_API_KEY / OPENROUTER_API_KEY, else
from --env-file (default <repo>/.env); it is never printed. --max-cost-usd
is required: a hard cap on the corpus's total hosted speech spend (ledger
table api_spend in queue.sqlite3, shared by every refine/work process and
kept across restarts; raise it to continue). Each request's estimated cost
is reserved before it is sent and a request that would pass the cap is not
sent; the run stops cleanly (exit 0, "budget_stopped" / "budget_exhausted"
in the report). Episodes refined by another backend are skipped unless
--rerefine. A rejected
key, payment error or persistent rate limit stops with exit 3; `refine
--dry-run` adds the estimated list-price cost. --model defaults to the
provider's whisper-large-v3-turbo.

`work --backend parakeet` runs the transcribe stage on NVIDIA Parakeet TDT
0.6B v3 (parakeet-mlx, Apple GPU, bf16) in a child under --stt-python (a venv
with parakeet-mlx; the model comes from the HF cache, downloaded on first
use). Defaults for this backend: --model mlx-community/parakeet-tdt-0.6b-v3,
--chunk-s 120 --overlap-s 15. Windows are seam-stitched like Whisper's, and
the transcript also carries "words" (native word timestamps) plus the model
revision, weights sha256, dtype and parakeet-mlx version in its provenance.
Before claiming, the worker runs `parakeet-preflight` under --stt-python
(parakeet_mlx imports, the model resolves, weights hashed) and exits 2 with
the reason if that fails; every child of that worker then loads the
preflight's pinned revision offline. The corpus records the model of its
first preflight (repo, revision, weights sha256); later preflights resolve
that revision, and a worker that would load other weights (another repo,
or a local dir with other weights) exits 2 before downloading anything,
unless --allow-model-change.
Sentences (segments) split at 1.5 s silences and are capped at 30 s (a
punctuation-only sentence joins the one before); windows of digital silence
(below -60 dBFS for windows under 15 s, -80 dBFS otherwise) are not decoded. Like mlx, it takes first transcribe
attempts only (retries go to a CPU worker); a worker running transcribe and
no later stage exits when only such retries are left. Its first-pass text is not tiny, so
`refine --only-tiny` skips it. --mem-cap-mb watches the child's RSS, which
does not include Metal (GPU) memory: mlx and parakeet GPU allocations are
not capped (provenance records parakeet's mlx_peak_memory_mb).

`--gap-fill-model REPO` (parakeet only; off by default) re-transcribes every
gap of at least --gap-fill-min-s (default 3) between Parakeet words, and at
the episode's start and end, with that mlx-whisper model (e.g.
large-v3-turbo = mlx-community/whisper-large-v3-turbo; --stt-python then
also needs mlx-whisper). Each gap is padded 1.5 s each side for context;
only Whisper words inside the gap that pass the hallucination guards are
spliced in, marked "src": "gapfill" on the word and its segment
(earwitness/pipeline/gap_fill.py). The preflight resolves the model too and
the children load it offline at that revision. Provenance "gap_fill"
records the model, revision, guards and counts (gaps found / filled, words
added); a gap-fill failure is recorded there as "error" (with the
traceback's tail) and the Parakeet transcript is kept, `status` counts
gap-fill transcripts, gaps, words added and errors, and a worker whose gap
fill fails on 3 episodes in a row stops (exit 3). Gaps over 120 s are
decoded in 120-s windows; an episode whose gaps cover over half of it (and
over 300 s) is not gap-filled, with the reason recorded. The corpus holds
gap fill like the Parakeet model (record parakeet_model.gap_fill: repo,
revision, min gap; a record from before gap fill means off): turning it on
or off, or another Whisper model, revision or min gap, needs
--allow-gap-fill-change (the Parakeet model stays held). Limits: shows not
in English get no gap fill (Whisper must detect English), and sung lyrics
in a gap are spliced in like speech (Whisper scores lyrics as confidently
as speech, so no guard separates them).

`canon` (local) builds the corpus-wide brand canonicalization map from
names/*.json and transcripts/*.json of every --corpus
(earwitness/pipeline/brand_canon.py: spacing variants and sound-alike
garbles merge onto the best-supported spoken-domain anchor; each entry
carries its reason and support). It prints a summary and writes the map to
--out for review. --apply also writes it to <corpus>/brand-canon.json in
every --corpus; `export` then adds brand_canonical + canon_reason to the
named reads it moves (brand, brand_spoken, brand_raw stay as named: they are
evidence identity on seed). The map records a digest of its inputs; export
warns when the corpora no longer match it (rerun canon over the same --corpus
list). Rebuilding on the same inputs writes the same file.

`show-identity` fetches each show's public RSS feed once and caches its own
web identity in <corpus>/show-identity/<sha>.json: own domains from the
channel link and episode links (other feed domains only when they carry the
show title or host's full name; never sponsors in show notes or the owner
email), plus show-title and itunes:author names (networks excluded). Failed
fetches are retried after 24 h, older identity versions are refetched,
--refresh refetches all, --max-fetches caps the requests per run. `export`
then gives the named reads of those shows the display attrs self_promo /
self_promo_reason (earwitness/pipeline/self_promo.py, on the span's own
text: own name / own domain / own CTA / own membership page, a recurring
brand containing a full own name, or "our Patreon" / "my book" style
phrases; a sponsor domain spoken in the span vetoes all of them, a
sponsor-read cue the name and phrase rules). Every span stays in the named
reads file; seed stores the attrs outside evidence identity and refreshes
them only on rows without human work. Promos for other shows (the
network's too) are ads, not self-promo.

`--corrections FILE` (rename, refine, canon, show-identity, export; default
off, and without it every output is as before) reads the app's ad review
corrections file (`app_workspace.py corrections export`;
earwitness/pipeline/corrections.py, the only reader of that contract):
the namer never picks a stoplisted word (next candidate, needs review)
and knows each new brand as one exact spelling (aliases never reach the
namer: names stay as heard); names docs and the canon map record the
digest of the rules they read; canon maps aliased spellings over any
merge; show-identity reports which
shows each self-promo rule covers; export gives aliased reads their
brand_canonical, marks rule self-promo ("network:<network>") and leaves
out the reads marked not an ad, and warns when names or canon predate the
rules. `corrections-labels --corrections FILE --out DIR` writes the
operator's labeled examples as label files (a partial-coverage dev set
`score --labels DIR` reads). SETUP.md, "Ad review corrections file", gives
the order to run them.

`fleet --corpus DIR [--fetch 1 --cpu 1 --gpu parakeet|mlx|none --gap-fill
--jev N --jev-env-file F --name N --diarize N --stop-at ISO ...]` starts the
corpus's workers from arguments and supervises them until the queue is
drained (earwitness/pipeline/fleet.py lists the fallbacks, restart rules,
adoption after its own restart, logs and alerts); `fleet --dry-run` prints
the worker commands, `fleet --stop` stops the supervisor and its workers
gracefully. `post-run --corpus DIR [--deadline ISO] [--refine-max N]
[--canon-corpus DIR ...] [--show-identity-passes 3] [--export DIR]` waits
until every episode is done or dead, then runs refine, canon, show identity,
export (swapped in whole) and status; it is safe to re-run. Workers' disk
guard: below --min-free-gb (default 10) free on the corpus volume a worker
claims no fetch, below 1 GB only clip; it resumes when space returns.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import resource
import socket
import sys
import threading
import time
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from earwitness.pipeline import advertisers  # noqa: E402
from earwitness.pipeline import api_stt  # noqa: E402
from earwitness.pipeline import brand_canon  # noqa: E402
from earwitness.pipeline import chunked_stt as stt  # noqa: E402
from earwitness.pipeline import corpus as cr  # noqa: E402
from earwitness.pipeline import corrections as corr  # noqa: E402
from earwitness.pipeline import export_delta as ed  # noqa: E402
from earwitness.pipeline import cue_spans  # noqa: E402
from earwitness.pipeline import gap_fill as gf  # noqa: E402
from earwitness.pipeline import jev_budget  # noqa: E402
from earwitness.pipeline import jev_screen  # noqa: E402
from earwitness.pipeline import llm_screen  # noqa: E402
from earwitness.pipeline import pod_naming  # noqa: E402
from earwitness.pipeline import promos  # noqa: E402
from earwitness.pipeline import refine as rf  # noqa: E402
from earwitness.pipeline import show_sources as ss  # noqa: E402
from earwitness.pipeline import spelling  # noqa: E402
from earwitness.pipeline import transcript_quality as tq  # noqa: E402


def parse_iso(value: str) -> float:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.astimezone()  # naive = local time
    return dt.timestamp()


def parse_day(value: str):
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a YYYY-MM-DD date: {value!r}")


def bounded(top: int):
    def check(value: str) -> int:
        n = int(value)
        if not 0 <= n <= top:
            raise argparse.ArgumentTypeError(f"must be 0..{top} (the chart's size)")
        return n
    return check


def names(known):
    def check(value: str) -> str:
        try:
            ss.resolve_names(value, known)
        except ValueError as exc:
            raise argparse.ArgumentTypeError(str(exc))
        return value
    return check


def parse_languages(value: str):
    """'en' or 'en,de' -> ("de", "en"): Whisper language codes."""
    out = set()
    for raw in (v.strip() for v in value.split(",") if v.strip()):
        code = stt.whisper_language(raw)
        if code is None:
            raise argparse.ArgumentTypeError(f"not a language code: {raw!r}")
        out.add(code)
    if not out:
        raise argparse.ArgumentTypeError("no language given")
    return tuple(sorted(out))


def parse_shard(value: str | None):
    if not value:
        return None
    k, n = (int(x) for x in value.split("/"))
    if not 0 <= k < n:
        raise argparse.ArgumentTypeError("--shard must be k/N with 0 <= k < N")
    return (k, n)


def regex(value: str) -> str:
    try:
        re.compile(value)
    except re.error as exc:
        raise argparse.ArgumentTypeError(f"not a regular expression: {value!r} ({exc})")
    return value


def parse_stages(value: str) -> tuple:
    stages = tuple(s.strip() for s in value.replace("|", ",").split(",") if s.strip())
    bad = [s for s in stages if s not in cr.STAGES]
    if bad or not stages:
        raise argparse.ArgumentTypeError(f"unknown stage(s) {bad}; use {cr.STAGES}")
    return stages


TOP_ARGS = ("apple_top", "apple_genre_top", "spotify_top", "spotify_category_top")


def logged_args(a) -> dict:
    """The command's arguments as recorded in the discovery log / show list."""
    return {k: v for k, v in vars(a).items() if k != "fn"}


def read_show_list(path) -> tuple[list, dict]:
    doc = json.loads(Path(path).read_text())
    meta = {k: doc.get(k) for k in ("built_at", "created", "selection", "args") if k in doc}
    return doc["shows"], meta


def cmd_init(a):
    shows, meta = read_show_list(a.shows)
    print(json.dumps(cr.init_corpus(a.corpus, shows, a.per_show,
                                    args=dict(logged_args(a), show_list=meta))))


def cmd_add_shows(a):
    shows, meta = read_show_list(a.shows)
    print(json.dumps(cr.add_shows(a.corpus, shows, args=dict(logged_args(a), show_list=meta),
                                  tier=a.tier)))


def cmd_set_tier(a):
    shows, meta = read_show_list(a.shows) if a.shows else (None, None)
    top = {k: getattr(a, k) for k in TOP_ARGS}
    try:
        report = cr.set_tier(a.corpus, a.tier, shows=shows, top=top, others=a.others,
                             stages=a.stages, include_done=a.include_done,
                             keep_audio=a.keep_audio, dry_run=a.dry_run,
                             args=dict(logged_args(a), show_list=meta))
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    missing = report["not_in_corpus"]  # a count; the feeds only with --verbose
    report["not_in_corpus"] = len(missing)
    if a.verbose:
        report["not_in_corpus_feeds"] = missing
    print(json.dumps(report))


def cmd_discover(a):
    print(json.dumps(cr.discover(
        a.corpus, a.per_show, a.workers, since=a.since, until=a.until,
        keep_current=a.keep_current, new_shows=a.new_shows,
        feed_max_bytes=int(a.feed_max_mb * 1024 * 1024), languages=a.languages,
        tier=a.tier, args=logged_args(a))))


def cmd_shows(a):
    http = ss.Polite()
    doc = ss.build_show_list(
        http, country=a.country, apple_top=a.apple_top,
        apple_genres=ss.resolve_names(a.apple_genres, ss.APPLE_GENRES),
        apple_genre_top=a.apple_genre_top, spotify_top=a.spotify_top,
        spotify_categories=ss.resolve_names(a.spotify_categories, ss.SPOTIFY_CATEGORIES),
        spotify_category_top=a.spotify_category_top, args=logged_args(a))
    print(f"{len(doc['shows'])} shows from {len(doc['charts'])} charts", file=sys.stderr,
          flush=True)
    if a.languages is not None and not a.verify:
        print("ERROR: --languages needs --verify (a show's language comes from its feed)",
              file=sys.stderr)
        return 2
    if a.verify:
        cap = int(a.feed_max_mb * 1024 * 1024)
        ss.verify_shows(doc, ss.Polite(lambda url: cr.http_get_capped(url, max_bytes=cap)),
                        workers=a.workers, languages=a.languages,
                        log=lambda m: print(m, file=sys.stderr, flush=True))
    if a.tier:
        for show in doc["shows"]:
            show["tier"] = a.tier
    stt.atomic_write_json(a.out, doc)
    reasons = {}
    for d in doc["dropped"]:
        reasons[d["reason"].split(":")[0]] = reasons.get(d["reason"].split(":")[0], 0) + 1
    print(json.dumps({"out": a.out, "shows": len(doc["shows"]), "charts": doc["charts"],
                      "errors": doc["errors"], "dropped": reasons}))
    return 1 if doc["errors"] else 0


def cmd_work(a):
    holding = [s for s in ("transcribe", "detect", "jev", "name", "diarize", "screen")
               if s in a.stages]
    if holding and "clip" not in a.stages and not a.allow_no_clip:
        print(f"ERROR: --stages {','.join(a.stages)} hands audio to the clip stage, which this "
              "worker does not run. Audio is deleted only after clipping, so without a clip "
              "worker the buffer fills and fetching stops. Add `clip` to --stages, or pass "
              "--allow-no-clip if another worker on this corpus runs clip.", file=sys.stderr)
        return 2
    api = a.backend in API_BACKENDS
    parakeet = a.backend == "parakeet"
    if a.gap_fill_model and not parakeet:
        print("ERROR: --gap-fill-model needs --backend parakeet", file=sys.stderr)
        return 2
    if a.gap_fill_model and a.gap_fill_min_s < gf.PAD_S:
        print(f"ERROR: --gap-fill-min-s must be at least {gf.PAD_S:g} (the gap pad)",
              file=sys.stderr)
        return 2
    model = a.model or (_api_model(a.backend) if api else
                        stt.PARAKEET_DEFAULT_REPO if parakeet else "tiny")
    chunk_s = a.chunk_s if a.chunk_s is not None else (
        PARAKEET_CHUNK_S if parakeet else 600.0)
    overlap_s = a.overlap_s if a.overlap_s is not None else (
        PARAKEET_OVERLAP_S if parakeet else 20.0)
    cfg = cr.WorkerConfig(
        worker_id=a.worker_id, stages=a.stages, model=model, beam=a.beam,
        threads=a.threads, chunk_s=chunk_s, overlap_s=overlap_s,
        lang_windows=a.lang_windows, mem_cap_mb=a.mem_cap_mb,
        max_attempts=a.max_attempts, lease_s=a.lease_s, max_buffered=a.max_buffered,
        max_buffered_transcript=a.max_buffered_transcript,
        yield_while_pgrep=a.yield_while_pgrep,
        stop_at=parse_iso(a.stop_at) if a.stop_at else None, max_episodes=a.max_episodes,
        shard=a.shard, keep_audio=a.keep_audio, timeout_factor=a.timeout_factor,
        idle_poll_s=a.idle_poll_s, exit_when_drained=not a.wait_forever, backend=a.backend,
        stt_python=a.stt_python, allow_model_change=a.allow_model_change,
        gap_fill_model=stt.mlx_repo(a.gap_fill_model) if a.gap_fill_model else None,
        gap_fill_min_s=a.gap_fill_min_s, allow_gap_fill_change=a.allow_gap_fill_change,
        screen_model=a.screen_model, screen_effort=a.screen_effort,
        stall_log_s=a.stall_log_s,
        jev_model=a.jev_model, jev_threshold=a.jev_threshold,
        jev_context_windows=a.jev_context_windows, name_sub_window_s=a.name_sub_window_s,
        name_gliner=a.name_gliner,
        name_threads=a.name_threads, brand_list=a.brand_list,
        jev_clip_pad_s=a.jev_clip_pad_s, jev_clip_max_s=a.jev_clip_max_s,
        max_cost_usd=a.max_cost_usd, api_env_file=a.env_file, api_rpm=a.api_rpm,
        api_timeout_s=a.api_timeout_s, api_language=a.api_language,
        diarize_device=a.diarize_device, diarize_mem_cap_mb=a.diarize_mem_cap_mb,
        diarize_python=a.diarize_python, diarize_window_s=a.diarize_window_s,
        language_hint=a.language_hint, repair_backend=a.repair_backend,
        repair_model=a.repair_model, repair_python=a.repair_python,
        min_free_gb=a.min_free_gb or None, tier_priority=a.tier_priority)
    try:
        worker = cr.Worker(a.corpus, cfg, log=_log)
    except (llm_screen.ScreenUnavailable, jev_screen.JevUnavailable,
            cr.DiarizeUnavailable) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except cr.SttUnavailable as exc:  # local GPU backend failed its preflight
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except rf.SpeechUnavailable as exc:  # hosted backend: no key (2) / key rejected (3)
        fatal = isinstance(exc, api_stt.ApiFatal)
        print(f"{'FATAL' if fatal else 'ERROR'}: {exc}", file=sys.stderr)
        return 3 if fatal else 2
    worker.install_signal_handlers()
    try:
        result = worker.run()
        print(json.dumps(result), flush=True)
    except KeyboardInterrupt:
        print(json.dumps({"worker": a.worker_id, "interrupted": True}), flush=True)
        return 130
    if result.get("fatal"):
        print(f"FATAL: {result['fatal']}", file=sys.stderr)
        return 3
    if result.get("budget_exhausted"):
        print(f"BUDGET: {result['budget_exhausted']}", file=sys.stderr)
    return 0


def _api_model(backend: str) -> str:
    return api_stt.PROVIDERS[backend]["model"]


def _api_args(s):
    s.add_argument("--env-file", default=str(cr.REPO / ".env"),
                   help="hosted backends: .env with the provider key, read when the key is "
                        "not in the environment (default: <repo>/.env)")
    s.add_argument("--max-cost-usd", type=float, default=None,
                   help="hosted backends (required): hard cap on the corpus's total hosted "
                        "speech spend, shared by every refine/work process on it and kept "
                        "across restarts (ledger: api_spend in queue.sqlite3)")
    s.add_argument("--api-language", default=None,
                   help="hosted backends: ISO-639-1 language hint (default: auto-detect)")
    s.add_argument("--api-rpm", type=float, default=None,
                   help="hosted backends: requests per minute (default: provider free tier, 20)")
    s.add_argument("--api-timeout-s", type=float, default=300.0,
                   help="hosted backends: per-request timeout")


def _log(msg):
    print(f"{datetime.now().strftime('%H:%M:%S')} {msg}", flush=True)


def cmd_jev_backfill(a):
    transport = None
    if not a.dry_run:
        try:
            transport = jev_screen.transport_from_env(
                min_interval_s=1.0 / max(a.rate, 0.01), purpose="jev-backfill",
                alert=lambda code, msg: cr.jev_budget_alert(a.corpus, code, msg, _log))
            jev_screen.preflight(transport)
        except jev_screen.JevUnavailable as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 2
    try:
        rep = cr.jev_backfill(a.corpus, transport=transport, threshold=a.threshold,
                              dry_run=a.dry_run, model=a.model, log=_log)
    except jev_screen.JevFatal as exc:
        print(f"FATAL: {exc}", file=sys.stderr)
        return 3
    print(json.dumps(rep), flush=True)
    return 0


WORKSPACE_EXPORT_KIND = "earwitness-workspace-transcript-copies"  # jev_rescreen.EXPORT_KIND


def cmd_jev_budget(a):
    """The Jev spend-cap ledger (design section 10): init a first ledger,
    print its status, or recover after a loss or restore."""
    try:
        if a.action == "init":
            rep = jev_budget.init(a.ledger, a.pin, first_activation=a.first_activation)
        elif a.action == "recover":
            rep = jev_budget.recover(a.ledger, a.pin, a.reason or "")
            for j in rep["journals_moved"]:
                print(f"WARNING: a SQLite journal beside the old ledger was moved aside with "
                      f"it: {j}", file=sys.stderr)
        else:
            rep = jev_budget.JevBudget(a.ledger, a.pin).status()
    except jev_budget.BudgetError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(rep, sort_keys=True), flush=True)
    return 0


def cmd_name_backfill(a):
    manifest = Path(a.corpus) / "manifest.json"
    try:
        export = json.loads(manifest.read_text("utf-8")).get("kind") == WORKSPACE_EXPORT_KIND
    except (OSError, ValueError, AttributeError):
        export = False
    if export and a.name_gliner != "none":
        print("ERROR: a workspace export is named without GLiNER (no model load, no network):"
              " pass --name-gliner none", file=sys.stderr)
        return 2
    namer = cr.build_namer(a.corpus, gliner=a.name_gliner, brand_list=a.brand_list,
                           threads=a.name_threads, log=lambda m: _log(f"[name-backfill] {m}"))
    try:
        rep = cr.name_backfill(a.corpus, namer=namer, threshold=a.jev_threshold,
                               context_windows=a.jev_context_windows,
                               sub_window_s=a.name_sub_window_s, log=_log)
    except cr.bn.NamesLockUnavailable as exc:
        print(f"FATAL: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(rep), flush=True)
    return 0


def cmd_diarize_backfill(a):
    cmd = [a.diarize_python or sys.executable, str(Path(cr.dz.__file__).resolve())]
    try:
        cr.diarize_preflight(cmd, a.diarize_device, mem_cap_mb=a.diarize_mem_cap_mb)
    except cr.DiarizeUnavailable as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    rep = cr.diarize_backfill(a.corpus, cmd=cmd, device=a.diarize_device,
                              mem_cap_mb=a.diarize_mem_cap_mb, window_s=a.diarize_window_s,
                              max_episodes=a.max_episodes, log=_log)
    print(json.dumps(rep), flush=True)
    return 0


def _diarize_args(s):
    s.add_argument("--diarize-device", choices=("mps", "cpu"), default="mps",
                   help="diarize child: mps (Apple GPU) or cpu")
    s.add_argument("--diarize-mem-cap-mb", type=float, default=cr.DIARIZE_MEM_CAP_MB,
                   help="diarize child: RSS cap in MB (killed above it; the episode "
                        "continues without speakers)")
    s.add_argument("--diarize-python", default=None,
                   help="python with requirements-diarize.txt for the diarize child "
                        "(default: this interpreter)")
    s.add_argument("--diarize-window-s", type=float, default=cr.dz.WINDOW_S,
                   help="diarize child: seconds of audio per model pass (memory scales "
                        "with it, not with episode length)")


def _corrections_arg(s):
    s.add_argument("--corrections", default=None, metavar="FILE",
                   help="the app's ad review corrections file (app_workspace.py corrections "
                        "export); default: off")


def _name_args(s):
    s.add_argument("--jev-context-windows", type=int, default=cr.DEFAULT_JEV_CONTEXT_WINDOWS,
                   help="name stage: 60-s windows of context on each side of a flagged window")
    s.add_argument("--name-sub-window-s", type=lambda v: float(v) or None,
                   default=cr.DEFAULT_NAME_SUB_WINDOW_S,
                   help="name stage: name flagged windows in sub-windows of this many seconds, "
                        "one span per brand (default/0: whole 60-s windows; 20 was measured)")
    s.add_argument("--name-gliner", choices=("small", "none"), default="small",
                   help="name stage fallback model (none = domain/CTA extraction + repair)")
    s.add_argument("--name-threads", type=int, default=2, help="torch threads for GLiNER")
    s.add_argument("--brand-list", default=None,
                   help="repair brand list JSON (default: harvest the corpus's transcripts)")


def _corrections(a):
    """The --corrections file (None when not given); exits 2 on a file the
    pipeline cannot read (corrections.CorrectionsError)."""
    path = getattr(a, "corrections", None)
    if not path:
        return None
    try:
        return corr.load(path)
    except corr.CorrectionsError as exc:
        print(f"ERROR: {exc}", file=sys.stderr, flush=True)
        raise SystemExit(2)


def cmd_canon(a, corrections=None):
    corrections = corrections or _corrections(a)
    spans, index = brand_canon.corpus_inputs(a.corpus)
    doc = {"corpora": [Path(c).resolve().name for c in a.corpus],
           "inputs_sha256": brand_canon.inputs_digest(a.corpus),
           "inputs_sha256_each": [brand_canon.inputs_digest([c]) for c in a.corpus],
           **brand_canon.build_canon(spans, None, index, corrections=corrections)}
    written = []
    for path in ([a.out] if a.out else []) + (
            [str(Path(c) / brand_canon.CANON_NAME) for c in a.corpus] if a.apply else []):
        brand_canon.write_canon(path, doc)
        written.append(path)
    print(json.dumps({"written": written, "applied": bool(a.apply),
                      **brand_canon.summarize(doc)}), flush=True)
    return 0


def cmd_screen(a):
    q = cr.Queue(a.corpus)
    try:
        if a.enable:
            q.enable_screen(a.model)
        elif a.disable:
            q.disable_screen()
        print(json.dumps({"screen_model": q.screen_model(),
                          "live_screen_worker": q.live_screen_worker()}))
    finally:
        q.close()


def cmd_refine(a):
    corrections = _corrections(a)
    namer = transcriber = cost_estimate = None
    api = a.backend in API_BACKENDS
    model = a.model or (_api_model(a.backend) if api else rf.DEFAULT_MODEL)
    if api:
        try:
            api_stt.price(a.backend, model)
        except ValueError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 2
        cost_estimate = lambda s: api_stt.request_cost(s, a.backend, model)["usd"]  # noqa: E731
    if not a.dry_run:
        def namer():  # built after transcription: its repair list sees the refined text
            return rf.build_refine_namer(a.corpus, gliner=a.name_gliner,
                                         brand_list=a.brand_list, threads=a.name_threads,
                                         log=lambda m: _log(f"[refine] {m}"),
                                         corrections=corrections)
        if api:
            try:
                transcriber = api_stt.from_args(
                    a.backend, model=model, env_file=a.env_file, max_cost_usd=a.max_cost_usd,
                    rpm=a.api_rpm, timeout_s=a.api_timeout_s, language=a.api_language,
                    ledger=Path(a.corpus) / "queue.sqlite3", owner="refine", log=_log)
            except api_stt.ApiUnavailable as exc:
                print(f"ERROR: {exc}", file=sys.stderr)
                return 2
        else:
            transcriber = rf.ChildTranscriber(
                rf.stt_command(a.stt_python, model, a.backend, a.stt_threads),
                clip_timeout_s=a.clip_timeout_s, log=_log)
    try:
        rep = rf.refine(a.corpus, transcriber=transcriber, namer=namer, model=model,
                        backend=a.backend, only_tiny=a.only_tiny, max_episodes=a.max_episodes,
                        dry_run=a.dry_run, threshold=a.jev_threshold,
                        context_windows=a.jev_context_windows, cost_estimate=cost_estimate,
                        rerefine=a.rerefine, sub_window_s=a.name_sub_window_s, log=_log)
    except rf.SpeechUnavailable as exc:  # the speech child never started
        print(f"FATAL: {exc}", file=sys.stderr)
        return 3
    except rf.bn.NamesLockUnavailable as exc:
        print(f"FATAL: {exc}", file=sys.stderr)
        return 2
    finally:
        if transcriber is not None:
            transcriber.close()
    print(json.dumps(rep), flush=True)
    print(rf.summary_line(rep), flush=True)
    if rep.get("aborted"):
        print(f"FATAL: {rep['aborted']}", file=sys.stderr)
        return 3
    if rep.get("budget_stopped"):
        print(f"BUDGET: {rep['budget_stopped']}", file=sys.stderr)
    return 0


def cmd_refine_stt(a):
    _parent_watchdog()
    proto = os.fdopen(os.dup(1), "w")  # answers only; library chatter goes to stderr
    os.dup2(2, 1)
    try:
        model, info = rf.load_stt_model(a.model, a.backend, a.threads,
                                        pin=json.loads(a.parakeet_pin) if a.parakeet_pin
                                        else None)
    except Exception as exc:
        proto.write(json.dumps({"ready": False, "error": f"{type(exc).__name__}: {exc}"[:500]})
                    + "\n")
        proto.flush()
        return 2
    rf.serve_stt(model, info, sys.stdin, proto)
    return 0


def _data_ref(path, sha256: str) -> dict:
    """A data file as a names doc records it: repo-relative when inside the repo."""
    p = Path(path).resolve()
    try:
        rel = str(p.relative_to(cr.REPO))
    except ValueError:
        rel = str(p)
    return {"path": rel, "sha256": sha256}


def _data_path(ref: dict) -> Path:
    p = Path(ref["path"])
    return p if p.is_absolute() else cr.REPO / p


def _pod_config_of(a):
    """The pod / round-3 options given on the command line, as recorded in
    each names doc (renaming.rename_corpus pod_config); None when none is."""
    kinds = [x for x in a.cue_spans.split(",") if x]
    if not (a.pod_jev or a.respell or a.pod_shownotes or a.advertisers or kinds
            or a.promo_shows or a.display_spelling):
        return None
    cfg = {"pod_jev": bool(a.pod_jev), "pod_shownotes": bool(a.pod_shownotes),
           "respell": bool(a.respell), "cue_spans": kinds, "cue_min_noul": a.cue_min_noul,
           "promo_own_network_self_promo": bool(a.promo_own_network_self_promo)}
    if a.pod_jev:
        cfg["pod_max_usd"] = a.pod_max_usd
        cfg["pod_options"] = {"piece_s": a.pod_piece_s, "none_policy": a.pod_none_policy,
                              "review_on": [x for x in a.pod_review.split(",") if x],
                              "min_p": a.pod_min_p, "min_s": a.pod_min_s,
                              "tidy": [x for x in a.pod_tidy.split(",") if x]}
    for key, path, load in (("advertisers", a.advertisers, advertisers.Advertisers.load),
                            ("promo_shows", a.promo_shows, promos.ShowTitles.load),
                            ("display_spelling", a.display_spelling, spelling.Spelling.load)):
        if path:
            cfg[key] = _data_ref(path, load(path).sha256)
    return cfg


def _pod_options(cfg) -> dict:
    """rename_corpus keyword arguments (plus "advertisers" and "pod_jev" for
    the namer and the Jev ask) from a pod_config; {} options when None.
    ValueError when a recorded data file is missing or changed."""
    cfg = cfg or {}
    loaded = {}
    for key, load in (("advertisers", advertisers.Advertisers.load),
                      ("promo_shows", promos.ShowTitles.load),
                      ("display_spelling", spelling.Spelling.load)):
        ref = cfg.get(key)
        if not ref:
            loaded[key] = None
            continue
        try:
            obj = load(_data_path(ref))
        except (OSError, ValueError) as exc:
            raise ValueError(f"{key} {ref['path']}: {exc}") from None
        if obj.sha256 != ref["sha256"]:
            raise ValueError(f"{key} {ref['path']} changed ({obj.sha256[:12]} != "
                             f"{ref['sha256'][:12]})")
        loaded[key] = obj
    po = cfg.get("pod_options") or {}
    return {"advertisers": loaded["advertisers"], "pod_jev": bool(cfg.get("pod_jev")),
            "pod_options": {"piece_s": po.get("piece_s", pod_naming.PIECE_S),
                            "none_policy": po.get("none_policy", "join"),
                            "review_on": tuple(po.get("review_on", pod_naming.DEFAULT_REVIEW)),
                            "min_p": po.get("min_p", 0.6), "min_s": po.get("min_s", 8.0),
                            "tidy": tuple(po.get("tidy", ()))},
            "pod_shownotes": bool(cfg.get("pod_shownotes")), "respell": bool(cfg.get("respell")),
            "cue_kinds": tuple(cfg.get("cue_spans") or ()),
            "cue_min_noul": cfg.get("cue_min_noul", cue_spans.MIN_NOUL),
            "promo_shows": loaded["promo_shows"],
            "promo_self_promo_own_network": bool(cfg.get("promo_own_network_self_promo")),
            "spelling": loaded["display_spelling"]}


def cmd_rename(a):
    from earwitness.pipeline import renaming
    corrections = _corrections(a)
    episodes = None
    if a.episodes or a.episodes_file:
        episodes = [e.strip() for e in (a.episodes or "").split(",") if e.strip()]
        if a.episodes_file:
            episodes += [ln.strip() for ln in Path(a.episodes_file).read_text().splitlines()
                         if ln.strip() and not ln.startswith("#")]
    bad = [k for k in a.cue_spans.split(",") if k and k not in cue_spans.KINDS]
    if bad:
        print(f"FATAL: --cue-spans: unknown kind(s) {bad} (known: {cue_spans.KINDS})",
              file=sys.stderr)
        return 2
    if "promos" in a.cue_spans.split(",") and not a.promo_shows:
        print("FATAL: --cue-spans promos needs --promo-shows", file=sys.stderr)
        return 2
    explicit = _pod_config_of(a)
    if explicit is not None and a.pod_config == "reset":
        print("FATAL: --pod-config reset takes no pod / round-3 flags", file=sys.stderr)
        return 2
    reps = []
    for corpus in a.corpus:
        if not a.dry_run:
            try:
                rf.bn.check_names_lock(cr.corpus_dirs(corpus)["names"])
            except rf.bn.NamesLockUnavailable as exc:
                print(f"FATAL: {exc}", file=sys.stderr)
                return 2
        # explicit flags (or reset) name every episode alike; else each episode
        # is named again with the options its names doc records (sticky), and a
        # doc with no record takes the corpus's recorded config, if it has one
        record = Path(corpus) / renaming.CORPUS_POD_CONFIG
        if a.pod_config == "reset":
            groups = [(None, episodes)]
            if not a.dry_run:
                record.unlink(missing_ok=True)
        elif explicit is not None:
            groups = [(explicit, episodes)]
            if not a.dry_run:
                renaming.write_corpus_config(corpus, explicit)
        else:
            groups = renaming.recorded_configs(corpus, episodes,
                                               default=renaming.corpus_config(corpus))
            if len(groups) == 1 and groups[0][0] is None:
                groups = [(None, episodes)]      # no pod config in play: as before
        reuse = explicit is None and a.pod_config != "reset"
        pods = [cfg for cfg, _ in groups if cfg and cfg.get("pod_jev")]
        ask = None
        if pods:   # one Jev ask (and one cap) per corpus run, across config groups
            cap = a.pod_max_usd if not reuse else max(
                float(c.get("pod_max_usd", a.pod_max_usd)) for c in pods)
            offline = a.pod_offline or (reuse and not os.environ.get("TYPESAFE_API_KEY"))
            if offline and not a.pod_offline:
                _log(f"[rename] WARNING: {corpus}: recorded pod naming from the cache only (no "
                     "TYPESAFE_API_KEY); an episode with an uncached run is left as it is "
                     "(skipped_pod_unavailable)")
            transport = None if offline else jev_screen.transport_from_env(
                purpose="pod-naming", alert=lambda code, msg, c=corpus: cr.jev_budget_alert(
                    c, code, msg, _log))
            ask = pod_naming.CachedAsk(corpus, transport, max_usd=cap, offline=offline)
        parts = []
        for cfg, eps in groups:
            try:
                opts = _pod_options(cfg)
            except ValueError as exc:
                print(f"FATAL: {corpus}: recorded pod_config cannot be reused: {exc} (pass "
                      "the flags again, or --pod-config reset)", file=sys.stderr)
                return 2
            nf = renaming.namer_factory(corpus, brand_list=a.brand_list, gliner=a.name_gliner,
                                        threads=a.name_threads, write=not a.dry_run,
                                        log=lambda m: _log(f"[rename] {m}"),
                                        advertisers=opts.pop("advertisers"),
                                        corrections=corrections)
            pod_on = opts.pop("pod_jev")
            parts.append(renaming.rename_corpus(
                corpus, namer_for=nf, episodes=eps, any_stage=a.any_stage,
                dry_run=a.dry_run, threshold=a.jev_threshold,
                context_windows=a.jev_context_windows, merge_rule=a.merge_rule,
                second_brand_min_score=None if a.no_second_brand else a.second_brand_min_score,
                max_span_windows=a.max_span_windows or None,
                sub_window_s=a.name_sub_window_s, pod_ask=ask if pod_on else None,
                pod_config=cfg, pod_strict=reuse and pod_on, log=_log, **opts))
            if parts[-1].get("pod_budget_reached"):
                break
        reps.append(renaming.merge_reports(parts))
    changed = sum(r["renamed"] for r in reps) and not a.dry_run
    canon_files = [str(Path(c) / brand_canon.CANON_NAME) for c in a.corpus
                   if (Path(c) / brand_canon.CANON_NAME).is_file()]
    if a.canon and not a.dry_run:
        cmd_canon(argparse.Namespace(corpus=a.corpus, out=None, apply=True), corrections)
    elif changed and canon_files:
        print(f"WARNING: names changed; {', '.join(canon_files)} is now stale: rerun "
              "`canon --apply` over the same --corpus list (or rename --canon)",
              file=sys.stderr, flush=True)
    moved = sum(r["ranges_changed"] for r in reps)
    if moved:
        print(f"NOTE: span ranges{' would change' if a.dry_run else ' changed'} in {moved} "
              "episodes, and named-read ids follow span ranges: re-name before the app's "
              "first real load, or re-named spans arrive as new evidence",
              file=sys.stderr, flush=True)
    for r in reps:
        print(json.dumps(r), flush=True)
    failed = sum(r.get("failed") or 0 for r in reps)
    budget = [r["pod_budget_reached"] for r in reps if r.get("pod_budget_reached")]
    skipped = sum(r.get("skipped_pod_unavailable") or 0 for r in reps)
    if failed or budget or skipped:
        # a partial pass: scripts (set -e) must see it
        print(f"RENAME INCOMPLETE: {failed} episodes failed"
              + (f"; {skipped} episodes skipped, left as they were: their recorded pod naming "
                 "needs Jev (give TYPESAFE_API_KEY) or a cached answer" if skipped else "")
              + (f"; {'; '.join(budget)} (the cap is per corpus run: --pod-max-usd)"
                 if budget else ""), file=sys.stderr, flush=True)
        return 3
    return 0


def cmd_score(a):
    import corpus_eval
    rep = corpus_eval.score(export=a.export, corpora=a.corpus, labels=a.labels,
                            label_transcripts=a.label_transcripts)
    if a.out:
        Path(a.out).parent.mkdir(parents=True, exist_ok=True)
        Path(a.out).write_text(json.dumps(rep, indent=1))
    print(corpus_eval.format_score(rep), flush=True)
    return 0


def cmd_align_labels(a):
    import corpus_eval
    rep = corpus_eval.align_labels(a.labels, a.label_transcripts, a.transcripts, a.out,
                                   min_insert_s=a.min_insert_s,
                                   label_audio_sums=a.label_audio_sums)
    print(corpus_eval.format_alignment(rep), flush=True)
    return 0


def cmd_repair_loops(a):
    transcriber = None
    if not a.dry_run:
        model = a.model or (stt.PARAKEET_DEFAULT_REPO if a.backend == "parakeet"
                            else rf.DEFAULT_MODEL)
        transcriber = rf.ChildTranscriber(rf.stt_command(a.stt_python, model, a.backend),
                                          clip_timeout_s=a.span_timeout_s, log=_log)
    try:
        rep = tq.repair_loops(a.corpus, transcriber=transcriber, backend=a.backend,
                              dry_run=a.dry_run, requeue=a.requeue,
                              requeue_anyway=a.requeue_anyway,
                              max_episodes=a.max_episodes, log=_log)
    except tq.RequeueRefused as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except rf.SpeechUnavailable as exc:
        print(f"FATAL: {exc}", file=sys.stderr)
        return 3
    finally:
        if transcriber is not None:
            transcriber.close()
    print(json.dumps(rep), flush=True)
    print(tq.summary_line(rep), flush=True)
    return 0


def cmd_repair_one(a):
    """(internal) Run by `work --repair-backend` as a supervised child after
    the transcribe child: repair the transcript's quality flags from the
    episode audio with the repair backend; write the result to --out."""
    _parent_watchdog()
    t0 = time.monotonic()
    doc = json.loads(Path(a.transcript).read_text(encoding="utf-8"))
    q = tq.quality_of(doc, a.show_language)
    model, info = rf.load_stt_model(a.model, a.backend,
                                    pin=json.loads(a.parakeet_pin) if a.parakeet_pin else None)
    hint = None if a.backend == "parakeet" else stt.whisper_language(a.show_language)
    rep = tq.repair_doc(doc, source_for=tq.audio_source(a.audio),
                        transcribe=tq.transcribe_with(model, a.backend, hint),
                        backend={"backend": info["backend"], "model": info["model"],
                                 **info["versions"]},
                        log=lambda m: print(m, flush=True))
    q["repair"] = {**{k: rep[k] for k in ("flags", "repaired", "uncovered", "failed")},
                   "audio_s": rep["audio_s"], "wall_s": round(time.monotonic() - t0, 3)}
    stt.atomic_write_json(a.out, doc)
    return 0


def cmd_status(a):
    s = cr.status(a.corpus)
    print(json.dumps(s, indent=1) if a.json else cr.format_status(s))


def cmd_repair_seams(a):
    print(json.dumps(cr.repair_seams(a.corpus, dry_run=a.dry_run,
                                     below_version=a.below_version)))


def cmd_mark_dead(a):
    ids = [e.strip() for e in a.episodes.split(",") if e.strip()]
    if not ids:
        print("ERROR: give --episodes", file=sys.stderr)
        return 2
    if not (a.reason or "").strip():
        print("ERROR: give --reason", file=sys.stderr)
        return 2
    rep = cr.mark_dead(a.corpus, ids, a.reason, dry_run=a.dry_run)
    print(json.dumps(rep), flush=True)
    return 0 if len(rep["would_mark" if a.dry_run else "marked"]) == len(set(ids)) else 1


def cmd_requeue_dead(a):
    ids = [e.strip() for e in (a.episodes or "").split(",") if e.strip()] or None
    if ids is None and not a.all:
        print("ERROR: give --episodes or --all", file=sys.stderr)
        return 2
    print(json.dumps(cr.requeue_dead(a.corpus, ids)), flush=True)
    return 0


def cmd_show_identity(a):
    corrections = _corrections(a)
    rep = cr.show_identity(a.corpus, max_fetches=a.max_fetches, refresh=a.refresh,
                           corrections=corrections)
    for r in rep.get("self_promo_rules_unmatched") or ():
        print(f"WARNING: self-promo rule {r['brand']!r} / {r['network']!r} matches no show "
              "title in this corpus", file=sys.stderr, flush=True)
    print(json.dumps(rep), flush=True)


def cmd_shownotes(a):
    print(json.dumps(cr.shownotes_pass(a.corpus)), flush=True)
    return 0


def _export_delta_cfg(a, cmd):
    try:
        return ed.load_config(a.config), None
    except ed.Refused as exc:
        print(json.dumps({"cmd": cmd, "code": f"refused:{exc.code}", "counts": {}}), flush=True)
        return None, ed.EXIT_REFUSED


def cmd_export_delta(a):
    """Fresh-data design 6.2, 8.1, 12.1: one JSON line of codes and counts."""
    cfg, code = _export_delta_cfg(a, "export-delta")
    if cfg is None:
        return code
    code, res = ed.export_delta(cfg)
    print(json.dumps(res, sort_keys=True), flush=True)
    return code


def cmd_export_receipt(a):
    """Fresh-data design 8.4, 8.5: one JSON line of codes and counts."""
    cfg, code = _export_delta_cfg(a, "export-receipt")
    if cfg is None:
        return code
    code, res = ed.export_receipt(cfg, a.file)
    print(json.dumps(res, sort_keys=True), flush=True)
    return code


def cmd_export_tidy(a):
    """Fresh-data design 8.5, 14: one JSON line of codes and counts."""
    cfg, code = _export_delta_cfg(a, "export-tidy")
    if cfg is None:
        return code
    code, res = ed.export_tidy(cfg)
    print(json.dumps(res, sort_keys=True), flush=True)
    return code


def cmd_export(a):
    corrections = _corrections(a)
    rep = cr.export(a.corpus, a.out, corrections=corrections)
    if rep.get("brand_canon_stale"):
        print(f"WARNING: {rep['brand_canon']} was built from other names/transcripts; "
             "rerun `canon --apply` over the same --corpus list", file=sys.stderr, flush=True)
    c = rep.get("corrections") or {}
    if c.get("names_stale"):
        print(f"WARNING: {c['names_stale']} episodes were named without these corrections; "
              "rerun `rename --corrections` (the stoplist and new brands act in the "
              "namer)", file=sys.stderr, flush=True)
    if c.get("canon_stale"):
        print("WARNING: the brand canon map was built without these corrections; rerun "
              "`canon --apply --corrections`", file=sys.stderr, flush=True)
    if c.get("unmatched"):
        print(f"WARNING: {len(c['unmatched'])} not-an-ad reads match no named read: "
              + ", ".join(c["unmatched"][:10]), file=sys.stderr, flush=True)
    print(json.dumps(rep))


def cmd_corrections_labels(a):
    corrections = _corrections(a)
    try:
        rep = corr.write_labels(corrections, a.out)
    except corr.CorrectionsError as exc:
        print(f"ERROR: {exc}", file=sys.stderr, flush=True)
        return 2
    print(json.dumps(rep), flush=True)
    return 0


def cmd_fleet(a):
    from earwitness.pipeline import fleet
    return fleet.cmd_fleet(a)


def cmd_post_run(a):
    from earwitness.pipeline import fleet
    return fleet.cmd_post_run(a)


BACKENDS = ("faster-whisper", "mlx")
WORK_BACKENDS = BACKENDS + ("parakeet",)  # local transcribe-stage backends (not refine)
PARAKEET_CHUNK_S, PARAKEET_OVERLAP_S = 120.0, 15.0  # measured setting (stt-bakeoff.md)
API_BACKENDS = api_stt.API_BACKENDS  # hosted: work (transcribe stage) and refine only

_MAXRSS_DIV = 1024 * 1024 if sys.platform == "darwin" else 1024


def _parent_watchdog():
    """Exit when the supervising worker dies (macOS has no PDEATHSIG)."""
    parent = os.getppid()

    def watch():
        while True:
            time.sleep(1.0)
            if os.getppid() != parent:
                os._exit(70)
    threading.Thread(target=watch, daemon=True).start()


def cmd_transcribe_one(a):
    if a.gap_fill_model and a.backend != "parakeet":
        print("ERROR: --gap-fill-model needs --backend parakeet", file=sys.stderr)
        return 2
    if a.gap_fill_model and a.gap_fill_min_s < gf.PAD_S:
        print(f"ERROR: --gap-fill-min-s must be at least {gf.PAD_S:g} (the gap pad)",
              file=sys.stderr)
        return 2
    _parent_watchdog()
    t0 = time.monotonic()
    if a.backend == "parakeet":
        model = stt.ParakeetModel(a.model, pin=json.loads(a.parakeet_pin)
                                  if a.parakeet_pin else None)
        backend_prov = {
            "backend": "parakeet-mlx", "parakeet_mlx": model.version,
            "mlx": stt.mlx_versions()["mlx"], "model": a.model, "repo": model.repo,
            "revision": model.revision, "weights_sha256": model.weights_sha256,
            "device": str(model.mx.default_device()), "dtype": model.dtype,
            "decoding": "greedy", "beam": a.beam, "language_detection": None,
            "sentence": {"silence_gap_s": stt.PARAKEET_SENTENCE["silence_gap"],
                         "max_duration_s": stt.PARAKEET_SENTENCE["max_duration"]},
            "silence_peak_floor": stt.PARAKEET_SILENCE_PEAK,
            "silence_peak_floor_short": stt.PARAKEET_SILENCE_PEAK_SHORT,
            "silence_short_window_s": stt.PARAKEET_SHORT_WINDOW_S,
        }
    elif a.backend == "mlx":
        repo = stt.mlx_repo(a.model)
        model = stt.MlxWhisperModel(repo)
        versions = stt.mlx_versions()
        backend_prov = {
            "backend": "mlx-whisper",
            "mlx_whisper": getattr(model.mlx_whisper, "__version__", None)
            or versions["mlx_whisper"],
            "mlx": versions["mlx"], "model": a.model, "repo": repo, "device": "gpu",
            "dtype": "float16", "beam": a.beam,
            "language_detection": "vote" if model.can_detect_language else "mlx_whisper",
        }
    else:
        import ctranslate2
        import faster_whisper
        model = stt.load_model(a.model, a.threads)
        backend_prov = {
            "backend": "faster-whisper", "faster_whisper": faster_whisper.__version__,
            "ctranslate2": ctranslate2.__version__, "model": a.model,
            "compute_type": "int8", "beam": a.beam, "cpu_threads": a.threads,
        }
    hint = stt.whisper_language(a.show_language) if a.language_hint \
        and getattr(model, "can_detect_language", True) else None  # Parakeet: no language
    result = stt.transcribe_file(
        a.audio, model=model, chunk_s=a.chunk_s, overlap_s=a.overlap_s, beam=a.beam,
        lang_windows=a.lang_windows, language_hint=hint,
        progress=lambda k, off, n: print(
            f"chunk {k} @{off:.0f}s ({n:.0f}s) maxrss_mb="
            f"{resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / _MAXRSS_DIV:.0f}",
            flush=True))
    if a.backend == "parakeet":
        backend_prov["mlx_peak_memory_mb"] = round(model.mx.get_peak_memory() / 2 ** 20, 1)
        if a.gap_fill_model:
            model = None  # Parakeet's weights leave the GPU before Whisper's load
            pin = json.loads(a.parakeet_pin) if a.parakeet_pin else {}
            backend_prov["gap_fill"] = gf.apply(
                a.audio, result, stt.mlx_repo(a.gap_fill_model), min_s=a.gap_fill_min_s,
                pin=pin.get("gap_fill"), log=lambda m: print(m, flush=True))
        else:
            backend_prov["gap_fill"] = {"enabled": False}
    self_ru = resource.getrusage(resource.RUSAGE_SELF)
    kids_ru = resource.getrusage(resource.RUSAGE_CHILDREN)
    decoded = result["decoded_duration_s"]
    doc = {
        "ep_id": a.episode_id, "model": a.model,
        "transcribed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "duration_min": round(decoded / 60, 2), "language": result["language"],
        "segments": result["segments"],
        **({"words": result["words"]} if "words" in result else {}),
        "provenance": {
            **backend_prov,
            "condition_on_previous_text": False, "vad_filter": False,
            "chunk_s": a.chunk_s, "overlap_s": a.overlap_s,
            "lang_windows": None if a.backend == "parakeet" else a.lang_windows,
            "seam_dedupe": stt.SEAM_REPAIR_VERSION,
            "decoder": "ffmpeg sequential f32le 16k mono",
            "audio_sha256": a.audio_sha256 or None, "audio_bytes": a.audio_bytes or None,
            "probed_duration_s": a.probed_duration or None, "decoded_duration_s": decoded,
            "fetch_observation_id": int(a.fetch_observation_id)
            if a.fetch_observation_id else None,
            "chunk_languages": result["chunk_languages"],
            "show_language": a.show_language or None, "language_hint": hint,
            "wall_s": round(time.monotonic() - t0, 3),
            "cpu_s": round(self_ru.ru_utime + self_ru.ru_stime
                           + kids_ru.ru_utime + kids_ru.ru_stime, 3),
            "host": socket.gethostname(),
        },
    }
    doc["quality"] = tq.assess(doc, a.show_language or None)
    stt.atomic_write_json(a.out, doc)
    return 0


def cmd_parakeet_preflight(a):
    """Run under --stt-python by `work --backend parakeet` before it claims
    anything: parakeet_mlx imports, the model resolves (HF cache or download;
    offline needs it cached) and its weights are hashed. Prints the pin
    (parakeet_pin) every transcribe child then loads."""
    try:
        import parakeet_mlx  # noqa: F401
    except ImportError as exc:
        print(f"ERROR: {sys.executable} cannot import parakeet_mlx ({exc}); pass "
              "--stt-python <python of a venv with parakeet-mlx>", file=sys.stderr)
        return 2
    try:
        pin = stt.parakeet_pin(a.model, a.revision)
    except Exception as exc:
        print(f"ERROR: Parakeet model {a.model} does not resolve ({type(exc).__name__}: "
              f"{exc}); offline it must already be in the HF cache", file=sys.stderr)
        return 2
    if a.gap_fill_model:
        repo = stt.mlx_repo(a.gap_fill_model)
        try:
            import mlx_whisper  # noqa: F401
        except ImportError as exc:
            print(f"ERROR: {sys.executable} cannot import mlx_whisper ({exc}) for "
                  "--gap-fill-model; install mlx-whisper into the --stt-python venv",
                  file=sys.stderr)
            return 2
        try:
            pin["gap_fill"] = gf.whisper_snapshot(repo, a.gap_fill_revision)
        except Exception as exc:
            print(f"ERROR: gap-fill model {repo} does not resolve ({type(exc).__name__}: "
                  f"{exc}); offline it must already be in the HF cache", file=sys.stderr)
            return 2
    print(json.dumps(pin), flush=True)
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("shows", help="build a show-list file from public podcast charts")
    s.add_argument("--out", required=True)
    s.add_argument("--country", default="us", help="two-letter chart market (default us)")
    s.add_argument("--apple-top", type=bounded(ss.APPLE_CHART_MAX), default=200)
    s.add_argument("--apple-genres", type=names(ss.APPLE_GENRES), default="all",
                   help="'all', '' or a comma list, e.g. 'News,True Crime,tv-film'")
    s.add_argument("--apple-genre-top", type=bounded(ss.APPLE_CHART_MAX), default=100)
    s.add_argument("--spotify-top", type=bounded(ss.SPOTIFY_TOP_MAX), default=200)
    s.add_argument("--spotify-categories", type=names(ss.SPOTIFY_CATEGORIES), default="all")
    s.add_argument("--spotify-category-top", type=bounded(ss.SPOTIFY_CATEGORY_MAX), default=50)
    s.add_argument("--verify", action="store_true",
                   help="load every feed; drop failing / audio-less ones with the reason")
    s.add_argument("--workers", type=int, default=8)
    s.add_argument("--feed-max-mb", type=float, default=cr.FEED_MAX_BYTES / 1024 / 1024)
    s.add_argument("--languages", type=parse_languages, default=None,
                   help="with --verify: keep only shows whose RSS language is one of these "
                        "(e.g. 'en'); shows without a clear language are kept")
    s.add_argument("--tier", choices=cr.TIERS, default=None,
                   help="tag every show with this coverage tier (init / add-shows read it)")
    s.set_defaults(fn=cmd_shows)

    s = sub.add_parser("init")
    s.add_argument("--corpus", required=True)
    s.add_argument("--shows", required=True)
    s.add_argument("--per-show", type=int, default=2)
    s.set_defaults(fn=cmd_init)

    s = sub.add_parser("add-shows", help="add the shows of a show list the corpus lacks")
    s.add_argument("--corpus", required=True)
    s.add_argument("--shows", required=True)
    s.add_argument("--tier", choices=cr.TIERS, default=None,
                   help="coverage tier of the added shows (default: the list's tier tags, "
                        "else full)")
    s.set_defaults(fn=cmd_add_shows)

    s = sub.add_parser("set-tier", help="move shows to a coverage tier and/or set a tier's "
                                        "stages")
    s.add_argument("--corpus", required=True)
    s.add_argument("--tier", choices=cr.TIERS, required=True)
    s.add_argument("--shows", default=None,
                   help="a show list: its shows (those within the rank limits, when given)")
    s.add_argument("--apple-top", type=bounded(ss.APPLE_CHART_MAX), default=0,
                   help="shows ranked within this on Apple's overall chart (0: not used)")
    s.add_argument("--apple-genre-top", type=bounded(ss.APPLE_CHART_MAX), default=0,
                   help="... within this on any Apple genre chart")
    s.add_argument("--spotify-top", type=bounded(ss.SPOTIFY_TOP_MAX), default=0,
                   help="... within this on Spotify's top chart")
    s.add_argument("--spotify-category-top", type=bounded(ss.SPOTIFY_CATEGORY_MAX), default=0,
                   help="... within this on any Spotify category chart")
    s.add_argument("--others", choices=cr.TIERS, default=None,
                   help="move every other show of the corpus to this tier")
    s.add_argument("--stages", type=parse_stages, default=None,
                   help="the tier's stage set (must include fetch,transcribe; default: "
                        "full all stages, transcript fetch,transcribe)")
    s.add_argument("--include-done", action="store_true",
                   help="re-tier the selected shows' done episodes too (never --others'): "
                        "one lacking the result of a stage its new tier adds goes back to the "
                        "first of them after transcribe (to full: detect; works from the "
                        "transcript, clip skips without audio); others are re-labelled, "
                        "keeping their results")
    s.add_argument("--keep-audio", action="store_true",
                   help="keep the audio of episodes the move makes done (as workers run with "
                        "--keep-audio)")
    s.add_argument("--dry-run", action="store_true",
                   help="write nothing; print the counts and the estimated Jev cost")
    s.add_argument("--verbose", action="store_true",
                   help="list the feeds of --shows that are not in the corpus (default: a count)")
    s.set_defaults(fn=cmd_set_tier)

    s = sub.add_parser("discover")
    s.add_argument("--corpus", required=True)
    s.add_argument("--per-show", type=int, default=None,
                   help="episodes per show (default: the corpus's per_show; with "
                        "--since/--until/--keep-current: no cap)")
    s.add_argument("--workers", type=int, default=8)
    s.add_argument("--since", type=parse_day, default=None, help="YYYY-MM-DD, inclusive")
    s.add_argument("--until", type=parse_day, default=None, help="YYYY-MM-DD, inclusive")
    s.add_argument("--keep-current", action="store_true",
                   help="only episodes newer than the show's newest queued one")
    s.add_argument("--new-shows", action="store_true", help="only shows with no episode yet")
    s.add_argument("--feed-max-mb", type=float, default=cr.FEED_MAX_BYTES / 1024 / 1024)
    s.add_argument("--languages", type=parse_languages, default=None,
                   help="enqueue only shows whose RSS language is one of these (e.g. 'en'); "
                        "shows without a clear language are kept and counted")
    s.add_argument("--tier", choices=cr.TIERS, default=None,
                   help="only shows of this coverage tier")
    s.set_defaults(fn=cmd_discover)

    s = sub.add_parser("work")
    s.add_argument("--corpus", required=True)
    s.add_argument("--worker-id", required=True)
    s.add_argument("--stages", type=parse_stages, default=cr.DEFAULT_STAGES,
                   help=f"comma list of {cr.STAGES}; default {','.join(cr.DEFAULT_STAGES)}")
    s.add_argument("--model", default=None,
                   help="speech model (default tiny; parakeet: "
                        f"{stt.PARAKEET_DEFAULT_REPO}; hosted backends: the provider's turbo)")
    s.add_argument("--beam", type=int, default=1)
    s.add_argument("--threads", type=int, default=2)
    s.add_argument("--chunk-s", type=float, default=None,
                   help=f"transcribe window (default 600; parakeet {PARAKEET_CHUNK_S:g})")
    s.add_argument("--overlap-s", type=float, default=None,
                   help=f"window overlap (default 20; parakeet {PARAKEET_OVERLAP_S:g})")
    s.add_argument("--lang-windows", type=int, default=3,
                   help="30 s language probes voted per chunk (0 = whisper's own first-30 s)")
    s.add_argument("--mem-cap-mb", type=float, default=3500.0)
    s.add_argument("--max-attempts", type=int, default=3)
    s.add_argument("--lease-s", type=float, default=300.0)
    s.add_argument("--max-buffered", type=cr.buffer_cap, default=6,
                   help="fetch buffer: episodes holding audio (default: both tiers; "
                        "full tier only with --max-buffered-transcript). 0 pauses fetch")
    s.add_argument("--max-buffered-transcript", type=cr.buffer_cap, default=None,
                   help="a separate fetch buffer for transcript-tier audio, so full-tier "
                        "audio waiting for diarize never stalls transcript-tier fetch "
                        "(default: one buffer shared by both tiers). 0 pauses "
                        "transcript-tier fetch")
    s.add_argument("--yield-while-pgrep", type=regex, default=None, metavar="PATTERN",
                   help="claim nothing new while a process's full command line matches "
                        "this extended regex (as `pgrep -f`; processes carrying this flag "
                        "are left out), e.g. 'release-builds/build/'; re-checked every "
                        "60 s, in-flight work finishes (default: off)")
    s.add_argument("--timeout-factor", type=float, default=1.5,
                   help="transcribe wall timeout = 300 s + factor x audio seconds")
    s.add_argument("--stop-at", default=None, help="ISO8601; claim nothing new after this")
    s.add_argument("--max-episodes", type=int, default=None)
    s.add_argument("--shard", type=parse_shard, default=None, help="k/N")
    s.add_argument("--keep-audio", action="store_true")
    s.add_argument("--idle-poll-s", type=float, default=10.0)
    s.add_argument("--wait-forever", action="store_true",
                   help="keep polling when the queue is drained (default: exit)")
    s.add_argument("--backend", choices=WORK_BACKENDS + API_BACKENDS, default="faster-whisper",
                   help="mlx = Apple-GPU mlx-whisper (run under a python that has it); "
                        "parakeet = Apple-GPU Parakeet TDT via parakeet-mlx (pass --stt-python); "
                        "groq / openrouter = hosted API (transcribe stage)")
    s.add_argument("--stt-python", default=None,
                   help="python for the transcribe child, e.g. a venv with parakeet-mlx "
                        "(default: this interpreter)")
    s.add_argument("--gap-fill-model", default=None,
                   help="parakeet: re-transcribe word gaps with this mlx-whisper model "
                        "(e.g. large-v3-turbo); off by default. English only: shows not "
                        "in English get no gap fill. Sung lyrics in a gap are spliced in "
                        "like speech (no guard separates them)")
    s.add_argument("--gap-fill-min-s", type=float, default=gf.DEFAULT_MIN_S,
                   help="parakeet gap fill: shortest word gap re-transcribed "
                        f"(default 3, at least {gf.PAD_S:g})")
    s.add_argument("--allow-gap-fill-change", action="store_true",
                   help="parakeet: switch the gap fill this corpus holds (on/off, model, "
                        "revision, min gap); the Parakeet model stays held")
    s.add_argument("--allow-model-change", action="store_true",
                   help="parakeet: accept a model revision other than the one this corpus's "
                        "first preflight recorded (the corpus then mixes revisions)")
    s.add_argument("--no-language-hint", dest="language_hint", action="store_false",
                   help="Whisper: vote every window's language even when the show's RSS "
                        "language is known (default: decode in it unless the vote is sure)")
    s.add_argument("--repair-backend", choices=tq.REPAIR_BACKENDS, default=None,
                   help="transcribe: re-transcribe repeat loops / wrong-language windows "
                        "with this backend while the audio is here (off by default)")
    s.add_argument("--repair-model", default=None,
                   help=f"repair model (default {stt.PARAKEET_DEFAULT_REPO}; mlx: "
                        "large-v3-turbo)")
    s.add_argument("--repair-python", default=None,
                   help="python for the repair child (default: --stt-python, else this one)")
    _api_args(s)
    s.add_argument("--screen-model", default=llm_screen.DEFAULT_MODEL,
                   help="model for the screen stage (prices: llm_screen.PRICES_PER_MTOK)")
    s.add_argument("--screen-effort", default="low",
                   choices=("low", "medium", "high", "xhigh", "max"))
    s.add_argument("--jev-model", default=jev_screen.DEFAULT_MODEL)
    s.add_argument("--jev-threshold", type=float, default=jev_screen.DEFAULT_THRESHOLD,
                   help="clip/name stages: use Jev windows with noul >= this")
    s.add_argument("--jev-clip-pad-s", type=float, default=cr.JEV_CLIP_PAD_S,
                   help=f"clip stage: seconds of context around Jev windows / named spans "
                        f"(other sources: {cr.CLIP_PAD_S:g})")
    s.add_argument("--jev-clip-max-s", type=float, default=cr.JEV_CLIP_MAX_S,
                   help=f"clip stage: split cap for clips holding a Jev window / named span "
                        f"(others: {cr.CLIP_MAX_S:g})")
    _name_args(s)
    _diarize_args(s)
    s.add_argument("--allow-no-clip", action="store_true",
                   help="run audio-holding stages without clip (another worker runs clip)")
    s.add_argument("--stall-log-s", type=float, default=300.0,
                   help="when idle with work left, log why this often")
    s.add_argument("--tier-priority", choices=tuple(cr.TIER_PRIORITIES), default="full",
                   help="claim order across coverage tiers: full (default) claims "
                        "transcript-tier work only when no full-tier work is claimable; "
                        "transcript the reverse; none ignores tiers")
    s.add_argument("--min-free-gb", type=float, default=10.0,
                   help="disk guard: below this many GB free on the corpus volume claim no "
                        f"fetch, below {cr.DISK_CLIP_ONLY_GB:g} GB only clip; resumes when "
                        "space returns (0 = off)")
    s.set_defaults(fn=cmd_work)

    s = sub.add_parser("screen", help="enable/disable routing detect -> screen")
    s.add_argument("--corpus", required=True)
    g = s.add_mutually_exclusive_group(required=True)
    g.add_argument("--enable", action="store_true")
    g.add_argument("--disable", action="store_true")
    s.add_argument("--model", default=llm_screen.DEFAULT_MODEL)
    s.set_defaults(fn=cmd_screen)

    s = sub.add_parser("status")
    s.add_argument("--corpus", required=True)
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_status)

    s = sub.add_parser("export")
    s.add_argument("--corpus", required=True, action="append",
                   help="repeat to merge shard corpora from several machines")
    s.add_argument("--out", required=True)
    _corrections_arg(s)
    s.set_defaults(fn=cmd_export)

    s = sub.add_parser("export-delta", help="build tonight's signed package of new episode "
                       "revisions (fresh-data design 6.2, 8.1)")
    s.add_argument("--config", required=True, help="export-delta configuration JSON")
    s.set_defaults(fn=cmd_export_delta)

    s = sub.add_parser("export-receipt", help="accept a signed host receipt or "
                       "reconcile-<epoch>.json (fresh-data design 8.4, 8.5)")
    s.add_argument("--config", required=True, help="export-delta configuration JSON")
    s.add_argument("file", help="the receipt; its signature is <file>.sig")
    s.set_defaults(fn=cmd_export_receipt)

    s = sub.add_parser("export-tidy", help="withdraw offered packages that hold removed "
                       "items, or all of them while paused; builds nothing")
    s.add_argument("--config", required=True, help="export-delta configuration JSON")
    s.set_defaults(fn=cmd_export_tidy)

    s = sub.add_parser("show-identity",
                       help="cache each show's own web identity from its RSS feed (once)")
    s.add_argument("--corpus", required=True)
    s.add_argument("--max-fetches", type=int, default=200)
    s.add_argument("--refresh", action="store_true", help="refetch every cached feed")
    _corrections_arg(s)
    s.set_defaults(fn=cmd_show_identity)

    s = sub.add_parser("requeue-dead",
                       help="send dead episodes back to fetch (e.g. after a code fix)")
    s.add_argument("--corpus", required=True)
    g = s.add_mutually_exclusive_group(required=True)
    g.add_argument("--episodes", default=None, help="comma-separated episode ids")
    g.add_argument("--all", action="store_true", help="every dead episode")
    s.set_defaults(fn=cmd_requeue_dead)

    s = sub.add_parser("mark-dead",
                       help="retire fetch-stage ready episodes to dead (other stages, leased, "
                            "done and dead are left alone; exit 1 if any id is refused); "
                            "undo: requeue-dead --episodes ... (sends them back to a fresh fetch)")
    s.add_argument("--corpus", required=True)
    s.add_argument("--episodes", required=True, help="comma-separated episode ids")
    s.add_argument("--reason", required=True, help="why; kept in last_error and the log")
    s.add_argument("--dry-run", action="store_true", help="report only; write nothing")
    s.set_defaults(fn=cmd_mark_dead)

    s = sub.add_parser("repair-seams",
                       help="de-duplicate chunk seams in done transcripts; requeue detect")
    s.add_argument("--corpus", required=True)
    s.add_argument("--dry-run", action="store_true")
    s.add_argument("--below-version", type=int, default=None,
                   help="only repair transcripts whose seam version is below N (1 = midpoint only)")
    s.set_defaults(fn=cmd_repair_seams)

    s = sub.add_parser("jev-backfill",
                       help="Jev-screen transcripts of done episodes that lack a Jev result")
    s.add_argument("--corpus", required=True)
    s.add_argument("--threshold", type=float, default=jev_screen.DEFAULT_THRESHOLD,
                   help="noul threshold used when reporting flagged windows")
    s.add_argument("--dry-run", action="store_true",
                   help="print episode count and estimated tokens/cost; no requests")
    s.add_argument("--model", default=jev_screen.DEFAULT_MODEL)
    s.add_argument("--rate", type=float, default=10.0, help="max requests per second (> 0)")
    s.set_defaults(fn=cmd_jev_backfill)

    s = sub.add_parser("jev-budget", help="the Jev spend-cap ledger: init, status, recover")
    s.add_argument("action", choices=("init", "status", "recover"))
    s.add_argument("--ledger", default=os.environ.get(jev_budget.ENV_LEDGER)
                   or jev_budget.DEFAULT_LEDGER)
    s.add_argument("--pin", default=os.environ.get(jev_budget.ENV_PIN) or jev_budget.DEFAULT_PIN)
    s.add_argument("--reason", default=None,
                   help="recover: why the ledger or pin was replaced (recorded); stop the "
                        "jev workers first")
    s.add_argument("--first-activation", action="store_true",
                   help="init: this is the first activation (design 10.4); required")
    s.set_defaults(fn=cmd_jev_budget)

    s = sub.add_parser("name-backfill",
                       help="name the Jev-flagged spans of done episodes (transcripts only)")
    s.add_argument("--corpus", required=True)
    s.add_argument("--jev-threshold", type=float, default=jev_screen.DEFAULT_THRESHOLD,
                   help="name Jev windows with noul >= this")
    _name_args(s)
    s.set_defaults(fn=cmd_name_backfill)

    s = sub.add_parser("diarize-backfill",
                       help="diarize episodes that still hold their full audio on this host")
    s.add_argument("--corpus", required=True)
    s.add_argument("--max-episodes", type=int, default=None)
    _diarize_args(s)
    s.set_defaults(fn=cmd_diarize_backfill)

    s = sub.add_parser("rename", help="re-run brand naming on existing corpora from their "
                       "transcripts and Jev results (no audio, no API)")
    s.add_argument("--corpus", required=True, action="append", help="repeatable")
    s.add_argument("--episodes", default=None, help="comma-separated episode ids")
    s.add_argument("--episodes-file", default=None, help="one episode id per line")
    s.add_argument("--any-stage", action="store_true",
                   help="also episodes not yet done (a live name worker may overwrite them)")
    s.add_argument("--dry-run", action="store_true", help="report what would change; write "
                   "nothing (harvested brand lists are not written either)")
    s.add_argument("--merge-rule", choices=rf.bn.MERGE_RULES, default=rf.bn.DEFAULT_MERGE_RULE,
                   help="span merge rule (default: %(default)s)")
    s.add_argument("--second-brand-min-score", type=float,
                   default=rf.bn.SECOND_BRAND_MIN_SCORE,
                   help="score a runner-up spoken domain needs to become a second brand "
                        "(default: %(default)s, chosen on dev)")
    s.add_argument("--no-second-brand", action="store_true", help="no second brands")
    s.add_argument("--max-span-windows", type=int, default=rf.bn.MAX_SPAN_WINDOWS,
                   help="windows a span grows to by joins other than on the same brand "
                        "(merge rule both; 0: no cap; default: %(default)s)")
    s.add_argument("--name-sub-window-s", type=lambda v: float(v) or None,
                   default=cr.DEFAULT_NAME_SUB_WINDOW_S,
                   help="name flagged windows in sub-windows of this many seconds, one span "
                        "per brand (default/0: whole 60-s windows; 20 was measured)")
    s.add_argument("--jev-threshold", type=float, default=None,
                   help="default: each episode's recorded threshold")
    s.add_argument("--jev-context-windows", type=int, default=None,
                   help="default: each episode's recorded context windows")
    s.add_argument("--name-gliner", choices=("small", "none"), default="small")
    s.add_argument("--name-threads", type=int, default=2, help="torch threads for GLiNER")
    s.add_argument("--brand-list", default=None,
                   help="FILE (both texts), or 'recorded' (the archived list each episode "
                        "was named with); default: harvest each corpus now, like a worker "
                        "start")
    s.add_argument("--canon", action="store_true",
                   help="then rebuild and apply brand-canon.json over every --corpus")
    s.add_argument("--pod-jev", action="store_true",
                   help="re-name each run of flagged windows spot by spot with Jev choice "
                        "questions (paid: needs TYPESAFE_API_KEY unless --pod-offline; "
                        "responses cached in <corpus>/jev-pod/)")
    s.add_argument("--pod-offline", action="store_true",
                   help="--pod-jev from cached responses only (runs without one are kept)")
    s.add_argument("--pod-max-usd", type=float, default=0.25,
                   help="stop the pass before Jev spending passes this, per --corpus (each "
                        "corpus run has its own cap); a stop exits 3 (default: %(default)s)")
    s.add_argument("--pod-piece-s", type=float, default=pod_naming.PIECE_S,
                   help="piece length for --pod-jev (default: %(default)s)")
    s.add_argument("--pod-none-policy", choices=pod_naming.NONE_POLICIES, default="join",
                   help="pieces Jev calls no ad: join the neighbouring spans, or drop")
    s.add_argument("--pod-review", default=",".join(pod_naming.DEFAULT_REVIEW),
                   help="comma list of pod review reasons that set needs_review "
                        "(jev_unsure, local_disagrees, not_spoken, short, off_list)")
    s.add_argument("--pod-min-p", type=float, default=0.6,
                   help="jev_unsure below this mean choice probability")
    s.add_argument("--pod-min-s", type=float, default=8.0, help="short below this many s")
    s.add_argument("--pod-tidy", default="",
                   help="comma list of piece-label rules: bridge_any, merge_same, "
                        "absorb_unsure (default: none)")
    s.add_argument("--pod-shownotes", action="store_true",
                   help="show-notes sponsors (shownotes-reads/<ep>.json) join the pod "
                        "candidates and the respelling targets")
    s.add_argument("--advertisers", default=None,
                   help="advertiser list JSON (earwitness/data/advertisers-v1.json): product "
                        "phrases collapse to their brand and names repair onto real "
                        "advertisers before the harvested brand list (default: off)")
    s.add_argument("--respell", action="store_true",
                   help="span brands take a show-notes sponsor's spelling (with "
                        "--pod-shownotes) or the episode's spoken spacing")
    s.add_argument("--cue-spans", default="",
                   help="comma list of cue-span kinds (tags, promos): add spans for short "
                        "sponsor tags / show promos that no flagged window covers (default: "
                        "none)")
    s.add_argument("--cue-min-noul", type=float, default=cue_spans.MIN_NOUL,
                   help="--cue-spans: the window around the cue must score at least this "
                        "(default: %(default)s)")
    s.add_argument("--promo-shows", default=None,
                   help="show-title gazetteer JSON (earwitness/data/show-titles-v1.json): name "
                        "promos for other shows after the show (default: off)")
    s.add_argument("--promo-own-network-self-promo", action="store_true",
                   help="with --promo-shows: a promo for the show's own network is "
                        "self-promo (as an operator network rule marks it)")
    s.add_argument("--display-spelling", default=None,
                   help="display-spelling map JSON (earwitness/data/spelling-v1.json): brands "
                        "in the brand's own spelling, exact up to case, spacing, punctuation "
                        "and a TLD (default: off)")
    s.add_argument("--pod-config", choices=("reuse", "reset"), default="reuse",
                   help="with no pod / round-3 flag, each episode is re-named with the options "
                        "its names doc records (reuse, the default), or with none (reset)")
    _corrections_arg(s)
    s.set_defaults(fn=cmd_rename)

    s = sub.add_parser("score", help="score named reads (an export or corpora) against a "
                       "labels folder")
    g = s.add_mutually_exclusive_group(required=True)
    g.add_argument("--export", default=None, help="an export dir (from `export`)")
    g.add_argument("--corpus", action="append", default=None,
                   help="corpus dir(s): exported to a temporary dir first")
    s.add_argument("--labels", required=True, action="append",
                   help="labels dir (repeatable), e.g. from align-labels")
    s.add_argument("--label-transcripts", action="append", default=None,
                   help="transcripts the labels' seg_idx refer to (default: the export's)")
    s.add_argument("--out", default=None, help="write the full report (JSON) here")
    s.set_defaults(fn=cmd_score)

    s = sub.add_parser("align-labels", help="move labels made on one fetch of an episode "
                       "onto another fetch's transcript")
    s.add_argument("--labels", required=True, action="append", help="labels dir (repeatable)")
    s.add_argument("--label-transcripts", required=True,
                   help="transcripts of the fetch the labels were made on")
    s.add_argument("--transcripts", required=True,
                   help="transcripts of the new fetch (a corpus's transcripts/ dir)")
    s.add_argument("--out", required=True,
                   help="writes labels/, labels-matched/ and alignment.json here")
    s.add_argument("--min-insert-s", type=float, default=15.0,
                   help="shortest stretch of new audio counted as inserted / unmatched")
    s.add_argument("--label-audio-sums", default=None,
                   help="sha256sum listing of the audio the labels were made on; an episode "
                        "whose sha equals the new transcript's audio_sha256 keeps its label times")
    s.set_defaults(fn=cmd_align_labels)

    s = sub.add_parser("refine", help="re-transcribe evidence clips of done episodes with a "
                       "stronger model and re-name their ad spans")
    s.add_argument("--corpus", required=True)
    s.add_argument("--model", default=None,
                   help="default large-v3-turbo (hosted backends: the provider's turbo id)")
    s.add_argument("--backend", choices=BACKENDS + API_BACKENDS, default="mlx")
    _api_args(s)
    s.add_argument("--stt-python", default=None,
                   help="python for the speech child (a venv with mlx-whisper); "
                        "default: this interpreter")
    s.add_argument("--stt-threads", type=int, default=4, help="faster-whisper CPU threads")
    s.add_argument("--only-tiny", action=argparse.BooleanOptionalAction, default=True,
                   help="only episodes whose first-pass transcript model is tiny (default on)")
    s.add_argument("--max-episodes", type=int, default=None)
    s.add_argument("--rerefine", action="store_true",
                   help="also redo episodes refined at this version by another model/backend. "
                        "Lossy by design: once an episode completes, its refined file and "
                        "names are replaced and the earlier refine is not kept (names keep "
                        "the first-pass naming under 'previous'); a failed or budget-stopped "
                        "episode keeps the earlier refine")
    s.add_argument("--dry-run", action="store_true",
                   help="count episodes, clips and audio; load no model")
    s.add_argument("--clip-timeout-s", type=float, default=300.0,
                   help="per-clip speech timeout (the child is restarted after one)")
    s.add_argument("--jev-threshold", type=float, default=jev_screen.DEFAULT_THRESHOLD,
                   help="for episodes with Jev windows but no names doc")
    _name_args(s)
    _corrections_arg(s)
    s.set_defaults(fn=cmd_refine)

    s = sub.add_parser("refine-stt")
    s.add_argument("--model", default="large-v3-turbo")
    s.add_argument("--backend", choices=BACKENDS + ("parakeet",), default="mlx")
    s.add_argument("--threads", type=int, default=4)
    s.add_argument("--parakeet-pin", default=None)
    s.set_defaults(fn=cmd_refine_stt)

    s = sub.add_parser("repair-loops", help="re-transcribe repeat loops / wrong-language "
                       "windows of done episodes (from audio, else a covering evidence clip)")
    s.add_argument("--corpus", required=True)
    s.add_argument("--backend", choices=tq.REPAIR_BACKENDS, default="parakeet",
                   help="parakeet (default; Apple GPU, run under --stt-python) or mlx "
                        "(mlx-whisper, e.g. large-v3-turbo)")
    s.add_argument("--model", default=None,
                   help=f"default {stt.PARAKEET_DEFAULT_REPO} (mlx: large-v3-turbo)")
    s.add_argument("--stt-python", default=None,
                   help="python for the speech child (a venv with parakeet-mlx / mlx-whisper)")
    s.add_argument("--requeue", action=argparse.BooleanOptionalAction, default=False,
                   help="send repaired episodes back to detect (refused unless detect, jev "
                        "and name workers are live). Default: only rewrite transcripts and "
                        "list the episodes to re-name (`rename --episodes`)")
    s.add_argument("--requeue-anyway", action="store_true",
                   help="requeue without the live-worker check (jev_status / name_status may "
                        "then record 'no worker' while old jev/ and names/ files stay)")
    s.add_argument("--max-episodes", type=int, default=None)
    s.add_argument("--span-timeout-s", type=float, default=600.0,
                   help="per-stretch speech timeout (the child is restarted after one)")
    s.add_argument("--dry-run", action="store_true",
                   help="count flags, coverage and audio to decode; load no model")
    s.set_defaults(fn=cmd_repair_loops)

    s = sub.add_parser("repair-one")
    s.add_argument("--audio", required=True)
    s.add_argument("--transcript", required=True)
    s.add_argument("--out", required=True)
    s.add_argument("--backend", choices=tq.REPAIR_BACKENDS, default="parakeet")
    s.add_argument("--model", default=stt.PARAKEET_DEFAULT_REPO)
    s.add_argument("--parakeet-pin", default=None)
    s.add_argument("--show-language", default=None)
    s.set_defaults(fn=cmd_repair_one)

    s = sub.add_parser("shownotes", help="confirm / correct named-read brands from the "
                       "episodes' show notes (run discover --per-show 0 first to store them)")
    s.add_argument("--corpus", required=True, action="append",
                   help="repeat for several corpora (as export)")
    s.set_defaults(fn=cmd_shownotes)

    s = sub.add_parser("canon", help="build the corpus-wide brand canonicalization map")
    s.add_argument("--corpus", required=True, action="append",
                   help="repeat to build one map over several corpora")
    s.add_argument("--out", default=None, help="also write the map here (review copy)")
    s.add_argument("--apply", action="store_true",
                   help="write <corpus>/brand-canon.json in every --corpus (export applies it)")
    _corrections_arg(s)
    s.set_defaults(fn=cmd_canon)

    s = sub.add_parser("corrections-labels",
                       help="write the ad review's labeled examples as label files (a dev set)")
    s.add_argument("--corrections", required=True,
                   help="the app's corrections file (app_workspace.py corrections export)")
    s.add_argument("--out", required=True, help="labels dir (one <episode_id>.json each)")
    s.set_defaults(fn=cmd_corrections_labels)

    from earwitness.pipeline import fleet
    s = sub.add_parser("fleet", help="start and supervise this corpus's workers from arguments")
    fleet.add_fleet_args(s)
    s.set_defaults(fn=cmd_fleet)

    s = sub.add_parser("post-run", help="after the run: refine, canon, show identity, export")
    fleet.add_post_run_args(s)
    s.set_defaults(fn=cmd_post_run)

    s = sub.add_parser("transcribe-one")
    s.add_argument("--audio", required=True)
    s.add_argument("--out", required=True)
    s.add_argument("--episode-id", required=True)
    s.add_argument("--model", default="tiny")
    s.add_argument("--beam", type=int, default=1)
    s.add_argument("--threads", type=int, default=2)
    s.add_argument("--chunk-s", type=float, default=600.0)
    s.add_argument("--overlap-s", type=float, default=20.0)
    s.add_argument("--lang-windows", type=int, default=3)
    s.add_argument("--probed-duration", type=float, default=0.0)
    s.add_argument("--audio-sha256", default="")
    s.add_argument("--audio-bytes", type=int, default=0)
    s.add_argument("--fetch-observation-id", default="")
    s.add_argument("--backend", choices=WORK_BACKENDS, default="faster-whisper")
    s.add_argument("--parakeet-pin", default=None,
                   help="JSON from parakeet-preflight: load this revision offline")
    s.add_argument("--gap-fill-model", default=None)
    s.add_argument("--gap-fill-min-s", type=float, default=gf.DEFAULT_MIN_S)
    s.add_argument("--show-language", default=None,
                   help="the show's RSS language: Whisper language hint + quality check")
    s.add_argument("--no-language-hint", dest="language_hint", action="store_false",
                   help="record the show language but let Whisper vote every window")
    s.set_defaults(fn=cmd_transcribe_one)

    s = sub.add_parser("parakeet-preflight",
                       help="(internal) check this python can run Parakeet; print the model pin")
    s.add_argument("--model", default=stt.PARAKEET_DEFAULT_REPO)
    s.add_argument("--revision", default=None,
                   help="resolve this HF revision (the corpus's) instead of the latest")
    s.add_argument("--gap-fill-model", default=None,
                   help="also check mlx_whisper imports and resolve this model")
    s.add_argument("--gap-fill-revision", default=None,
                   help="resolve the gap-fill model at this HF revision (the corpus's)")
    s.set_defaults(fn=cmd_parakeet_preflight)

    a = p.parse_args(argv)
    if a.command == "discover":
        if a.keep_current and (a.since or a.until):
            p.error("--keep-current cannot be combined with --since/--until")
        if a.since and a.until and a.since > a.until:
            p.error("--since must not be after --until")
    if getattr(a, "diarize_window_s", None) is not None \
            and not a.diarize_window_s > cr.dz.OVERLAP_S:
        print(f"ERROR: --diarize-window-s must be longer than the {cr.dz.OVERLAP_S:g}-s "
              "window overlap", file=sys.stderr)
        return 2
    return a.fn(a) or 0


if __name__ == "__main__":
    raise SystemExit(main())
