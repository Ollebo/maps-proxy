# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Spridningstillstånd (mandatory for any map/imagery work)

All map data handling follows Ollebo's spreading policy (LMFS 2016:1). The full rules are in `map-maker/docs/spridningstillstand.md` (local copy: `~/projects/ollebo/docs/spridningstillstand.md`). Read it before changing rendering, tiles, storage, sharing, export, thumbnails or anything that serves map data.

The enforcement workflow (invariants, flow, where the code lives, deploy pitfalls, verification checklist and standing decisions) is the project standard in `map-maker/docs/publishing-workflow.md` (local copy: `~/projects/ollebo/docs/publishing-workflow.md`). Keep every invariant in it, and run its verification checklist after any change to map serving.

In short: high-res material (finer than 0.5 m) is visible only to members of the owning space. Anything public or external gets the coarse public version: resampled to 0.6 m in EPSG:3006, with maxzoom from the northernmost latitude. If unsure, keep it private and create a `compliance_review` row.

Never:
- publish or share externally anything finer than 0.5 m without a registered permit;
- publish DSM/DTM, point clouds, 3D, raw images, oblique images, video or detections;
- make a dataset public by only changing access/ACL (going public = a new coarse render);
- expose originals or high-res COGs/tiles via a public URL, cache or API;
- assume an area exemption (LMFS 2 kap. 3 §) applies without human review;
- remove or bypass audit logging (`publication_audit`), resolution checks or maxzoom gates.
