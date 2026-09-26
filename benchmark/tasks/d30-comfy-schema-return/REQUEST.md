# Return the complete reusable ComfyUI workflow schema

Use the packaged ComfyUI skill to inspect a workflow and return its reusable schema to a caller.

Required behavior:
- The returned schema must expose the accumulated configurable parameters together with output nodes, model dependencies, embedding dependencies, and summary metadata.
- Preserve the existing extraction rules and public return type.
