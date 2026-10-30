`lipsync.json` is not generated during `python3 main.py narrate ...` because lip-sync is a separate pipeline stage (`cmd_lipsync` / `narration/lipsync.py`).

Here is why it isn't in your project folder:

1. **`narrate` only builds audio, timing, and captions:** `cmd_narrate` calls `narrate_project()`, which writes `project.json`, `audio/*.wav`, `narration.json`, `timeline.json`, and `captions/*`. It never calls `lipsync_project()`.


2. **Explainer scripts have no character kit:** In `config/channels/explainer.json`, `"character"` and `"default_character"` are `""` (empty) because Explainer videos use `full_scene` layout without an animated mascot. Even in `cmd_build`, if no character kit exists for the channel, the build stops cleanly after `narrate` before running `mix`, `lipsync`, or `render`.


3. **Project Charter scope:** In your project charter, this pipeline is narration-only (audio + timing/caption/marker files for manual editing), and `lipsync` / `render` are kept intact only so existing tests pass.



If you ever explicitly want to generate `lipsync.json` for a narrated project folder, you have to run the `lipsync` subcommand directly:

```bash
# With Rhubarb installed at ~/.local/opt/rhubarb/rhubarb (or on PATH):
python3 main.py lipsync projects/<name>

# Or using the deterministic fake lip-sync generator (no Rhubarb binary needed):
python3 main.py lipsync projects/<name> --rhubarb fake

```

Reply **"next"** whenever you are ready for **GROUP 2 of 4** (`markers.csv`) of Task 3.
