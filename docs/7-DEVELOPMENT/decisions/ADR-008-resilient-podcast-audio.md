# ADR-008: Resilient podcast audio: normalize short sentences, resume per clip

- **Status**: Accepted
- **Date**: 2026-10
- **Related**: [ADR-004](ADR-004-background-workers.md), [podcasts.md](../podcasts.md)

## Context

On 2026-09-30, 5 of 6 podcast jobs failed with `HTTP 500: Internal Server Error` from the text-to-speech engine, and the same happened on 2026-09-24. The library (`podcast-creator` 0.12.0) retries a clip 3 times within ~15 seconds, regenerates every clip on each run, and fails the whole job when one clip fails. Retrying longer looked like the fix.

Measured on 2026-10-01 from the production container, with the production credentials, calling the engine sequentially (`/tmp` probes, 6 calls per text unless noted):

- `Oui. L'humain reste décideur.` failed 6/6, and so did a longer sentence starting with `Oui.` (6/6).
- `Oui, l'humain reste décideur.` and a control sentence succeeded 6/6; 5 concurrent calls succeeded 5/5.
- A sentence shorter than 7 characters (`Oui.`, `Non.`, `Hmm.`) fails reliably, including in the middle of a longer line. `Ah oui.`, `Oui, bien sûr.` and `Exactement.` pass.

The failure is deterministic and depends on the text, so retrying the same text longer cannot help.

## Decision

1. Normalize the text sent to the engine (`open_notebook/podcasts/tts_text.py`): merge any sentence under 7 characters with its neighbour. The transcript stored and returned is never changed, only the text sent to the engine.
2. Replace the library's audio node with a resilient one (`open_notebook/podcasts/resilient_audio.py`), installed under the same node name so the routing is unchanged: each clip is tried with several writings of its text, then with progressive waits; a clip already on disk is reused; one failing clip never discards the others.
3. Add a resume path: `POST /api/podcasts/episodes/{id}/retry` keeps the episode when `transcript.json` exists, and the new job redoes only the missing clips (plan and transcript are not regenerated).

## Alternatives considered

- **Longer retries only** (library settings `PODCAST_RETRY_*`): rejected, the failure is not random.
- **Fix the engine**: out of scope for this repository; the workaround stays valid if the engine is fixed.
- **Fork the library**: rejected, the graph is already extended at runtime by `vocalization.py`, the same technique is used here.

## Consequences

- A job survives the known failure and a failed job can be finished without paying again for the plan and transcript.
- The 7-character threshold is an empirical measurement on one engine and one voice, to be re-measured if the engine or model changes.
- A lone reply shorter than 7 characters is padded as a last resort (`Oui.` becomes `Ah oui.`), which slightly changes what is spoken.
- Cancellation is not covered here: the worker overwrites a `canceled` status at the end of a job, so cancelling needs its own flag.
