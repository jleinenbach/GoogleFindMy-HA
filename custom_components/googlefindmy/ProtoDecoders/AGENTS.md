# ProtoDecoders/AGENTS.md — Protobuf overlay expectations

**Scope:** Applies to all stub overlays under `custom_components/googlefindmy/ProtoDecoders/`.

## Generated message classes must inherit `google.protobuf.message.Message`

When updating or regenerating the protobuf stub overlays in this directory:

* Import `Message` from `google.protobuf.message` and alias it locally when needed (for example, `from google.protobuf import message as _message; Message = _message.Message`).
* Ensure every generated message class directly subclasses this concrete base (`class DeviceUpdate(Message): ...`). This maintains nominal subtyping so helpers typed against `google.protobuf.message.Message` continue to accept the generated stubs.
* Protocol helpers from `custom_components.googlefindmy.protobuf_typing` may still be used alongside the concrete inheritance. Prefer composition (e.g., aliasing `EnumTypeWrapperMeta`) rather than replacing the base class with a protocol.

Breaking this contract causes strict mypy runs to treat generated messages as incompatible with helper signatures expecting `Message`.

## Regeneration checklist (developer workflow)

Use the checked-in proto sources (`custom_components/googlefindmy/ProtoDecoders/*.proto`) and regenerate overlays from the repository root:

1. Ensure `protoc` ≥ 24 is installed locally and on the `PATH`.
2. Run `python -m custom_components.googlefindmy.ProtoDecoders.decoder`. The module's `__main__` hook orchestrates the required `protoc` invocations for both `.py` and `.pyi` outputs.
3. Verify the generated `.pyi` stubs keep `Message = _message.Message` and subclass `Message` directly before committing changes.

If the upstream proto schema changes, update the mirrored definitions under `custom_components/googlefindmy/ProtoDecoders/*.proto` first so regenerations remain reproducible from source control.
