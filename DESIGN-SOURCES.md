# Design Sources

This repo is the published site repo for Signal Monitor. A lot of the HTML here is generated.

## Source Of Truth

- Hub layout, deep dive page layout, KB page layout, generated footer version:
  `projects/signal-monitor-standalone/scripts/build_hub.py`
- Deep dive "mark as read and go to next" behavior:
  `deep-dives/deep-dive-navigation.js`
- Publish flow and pre-push verification:
  `projects/signal-monitor-standalone/scripts/x_push.py`
- Pipeline sync / launch behavior:
  `projects/signal-monitor-standalone/scripts/run_pipeline.sh`

## Generated Files

Do not treat these as the long-term source of truth:

- `index.html`
- `deep-dives/*.html`
- `kb/*.html`

If one of those files is hotfixed directly, the same change must be ported back to the source script immediately or the next pipeline run can overwrite it.

## Safe Workflow

1. Edit the source file in the pipeline workspace first.
2. Rebuild generated pages.
3. Run the build verification.
4. Publish only after the verification passes.

## Current Guardrails

- Generated pages include an `AUTO-GENERATED FILE` marker.
- Generated pages include a visible build version in the footer.
- The pipeline verifies the generated hub before publishing.
- The pipeline now refuses to wipe or overwrite a dirty publish repo.
