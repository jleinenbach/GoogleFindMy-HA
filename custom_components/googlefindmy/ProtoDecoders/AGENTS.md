# ProtoDecoders/AGENTS.md — Protobuf overlay expectations

**Scope:** Applies to all stub overlays under `custom_components/googlefindmy/ProtoDecoders/`.

## Generated message classes must inherit `google.protobuf.message.Message`

When updating or regenerating the protobuf stub overlays in this directory:

* Import `Message` from `google.protobuf.message` and alias it locally when needed (for example, `from google.protobuf import message as _message; Message = _message.Message`).
* Ensure every generated message class directly subclasses this concrete base (`class DeviceUpdate(Message): ...`). This maintains nominal subtyping so helpers typed against `google.protobuf.message.Message` continue to accept the generated stubs.
* Protocol helpers from `custom_components.googlefindmy.protobuf_typing` may still be used alongside the concrete inheritance. Prefer composition (e.g., aliasing `EnumTypeWrapperMeta`) rather than replacing the base class with a protocol.

Breaking this contract causes strict mypy runs to treat generated messages as incompatible with helper signatures expecting `Message`.

## NEVER vendor types from the `google.*` namespace

Types that already exist in the official `protobuf` or `googleapis-common-protos` packages **must not** be re-defined here. Vendoring them causes a **duplicate-symbol crash** on Python >= 3.13 when another Home Assistant integration (e.g. Nest, Google Cloud TTS) loads the official library into the process-wide default descriptor pool.

Concrete rules:

* **`google.protobuf.Any`** — use `google.protobuf.any_pb2` from the `protobuf` package. A vendored `Any_pb2.py` was removed for this reason.
* **`google.rpc.Status`** — a vendored `RpcStatus_pb2.py` is kept as fallback because `googleapis-common-protos` is not a declared dependency. `nova_request.py` prefers the official `google.rpc.status_pb2` when available and falls back to the vendored copy. See the import cascade in `nova_request.py:55-74`.
* **Any new `.proto` with `package google.*`** — do not add one. Import the official module at runtime instead.

### Descriptor pool architecture

Every `_pb2.py` file in this project uses a **separate `DescriptorPool()`** instead of the process-wide default pool. This prevents symbol collisions with types that other integrations may register.

| Module | Pool variable | Shared with |
|--------|---------------|-------------|
| `RpcStatus_pb2.py` | `_rpc_pool` | — (seeds official `any_pb2` descriptor) |
| `Common_pb2.py` | `_common_pool` | `DeviceUpdate_pb2`, `LocationReportsUpload_pb2` |
| `DeviceUpdate_pb2.py` | `_findmy_pool` | shares `_common_pool` |
| `LocationReportsUpload_pb2.py` | `_findmy_pool` | shares `_common_pool` |
| Firebase modules | `_firebase_pool` | shared between `android_checkin_pb2`, `checkin_pb2`, `mcs_pb2` |

When adding a new proto module that depends on an existing one, **reuse the parent's pool** (e.g. `_findmy_pool = Common_pb2._common_pool`) so that cross-file type references resolve.

Regression tests: `tests/test_protobuf_namespace_conflict.py`.

## Regeneration checklist (developer workflow)

Use the checked-in proto sources (`custom_components/googlefindmy/ProtoDecoders/*.proto`) and regenerate overlays from the repository root:

1. Ensure `protoc` >= 24 is installed locally and on the `PATH`.
2. Run `python -m custom_components.googlefindmy.ProtoDecoders.decoder`. The module's `__main__` hook orchestrates the required `protoc` invocations for both `.py` and `.pyi` outputs.
3. **After regeneration**, manually replace the default pool (`_descriptor_pool.Default()`) with a separate pool variable in each new `_pb2.py` file. `protoc` does not generate this — it must be patched by hand.
4. Verify the generated `.pyi` stubs keep `Message = _message.Message` and subclass `Message` directly before committing changes.

If the upstream proto schema changes, update the mirrored definitions under `custom_components/googlefindmy/ProtoDecoders/*.proto` first so regenerations remain reproducible from source control.

## Proto3 scalar presence reminder

Proto3 **non-optional scalar fields do not support `HasField`**. Access them directly and rely on their default values (for example, `0` for integers) instead of calling presence checks that raise `ValueError`.
