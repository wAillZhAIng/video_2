# ComfyUI H3 workflow contract

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


Custom API workflows must include a `_video_factory` input mapping and match the installed ComfyUI `/object_info`. The application validates the actual nodes, input connections and sampling contract before submission.
