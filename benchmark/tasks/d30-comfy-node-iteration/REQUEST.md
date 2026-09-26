# Traverse ComfyUI workflow nodes through the package protocol

Use the packaged ComfyUI schema extractor on workflow mappings whose node IDs and node records must be traversed consistently across helper functions.

Required behavior:
- Positive-prompt discovery must consume workflow nodes through the package's canonical node-iteration protocol rather than unpacking mapping keys as node records.
- Preserve sampler filtering, graph tracing, and the public helper interface.
