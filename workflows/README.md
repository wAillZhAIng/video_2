# ComfyUI H3 workflow contract

This directory intentionally does not contain a fabricated MiniMax H3 graph. Export the
API-format workflow from the installed ComfyUI instance, verify its nodes through
`/object_info`, and add a top-level `_video_factory` mapping alongside the `prompt` graph.

Required envelope:

```json
{
  "_video_factory": {
    "name": "installed-workflow-name",
    "version": "verified-version",
    "prompt_path": ["NODE_ID", "inputs", "text"],
    "negative_prompt_path": ["NODE_ID", "inputs", "text"],
    "duration_path": ["NODE_ID", "inputs", "duration"],
    "seed_path": ["NODE_ID", "inputs", "seed"],
    "reference_paths": []
  },
  "prompt": {
    "NODE_ID": {
      "class_type": "ACTUAL_INSTALLED_NODE_CLASS",
      "inputs": {}
    }
  }
}
```

Do not copy the illustrative node names above into a production graph. Reference upload is
kept disabled until the exact installed workflow's upload and node mapping is implemented
and verified. Without this configuration the API reports the real provider as not ready;
it never silently falls back to demo mode.

## Native MiniMax H3 Ref2VA profiles

`h3_ref2va_native_fast4_v2.json`, `h3_ref2va_native_quality20_v2.json`, and
`h3_ref2va_native_quality30_v2.json` are API workflows for the verified native
`MiniMaxH3ReferenceToVideo` route. They keep the Fast SMD4 recipe intact and pin
the two quality samplers independently. The adapter creates `LoadVideo` and
`GetVideoComponents` nodes only for the explicitly frozen reference videos, then
wires their frame output to `ref_videos.ref_video_N`. When the user explicitly
enables reference audio, the same component node's audio output is wired to the
matching `ref_video_audios.ref_video_audio_N`. No file path is placed in a prompt.

The live ComfyUI instance must expose `LoadVideo`, `GetVideoComponents`, and the
`ref_videos` autogrow socket reported by `MiniMaxH3ReferenceToVideo`; otherwise
the adapter refuses submission before it uploads or queues a generation. When
reference audio is explicitly enabled, its matching `ref_video_audios` socket is
also required; visual-only reference videos do not silently request or require it.

