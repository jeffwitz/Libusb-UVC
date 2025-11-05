# Quirks Directory

This directory stores JSON files that describe vendor- or GUID-specific control
metadata.  Each file is referenced by its Extension Unit (XU) GUID and enriches
the generic control discovery performed by `UVCControlsManager`.

## File Naming

- `guid_<lowercase-guid>.json` — One file per GUID.  The GUID should be written
  in lowercase with hyphens, for example `guid_0f3f95dc-2632-4c4e-92c9-a04782f43bc8.json`.

## JSON Structure (Summary)

While the schema is informal today, each file typically contains:

- `schema_version` — Integer for future upgrades.
- `guid` — GUID of the Extension Unit.
- `name` — Human-readable name of the unit.
- `controls` — Array mapping selectors to semantics. Each entry may include:
  - `selector` — Numeric selector value (if known).
  - `name` — Display name for the control.
  - `type` — Optional type hint (`bool`, `range`, etc.).
  - `notes` — Free-form documentation.
  - Additional fields to guide validation (expected GET_INFO bits, payload
    layouts, etc.).

Refer to the bundled Microsoft Camera Control XU file
(`guid_0f3f95dc-2632-4c4e-92c9-a04782f43bc8.json`) for a detailed example.

## Loading Order

`load_quirks()` currently reads only the packaged files, but the loader can be
extended in the future to incorporate system- or user-level overrides.

## Contributing New Quirks

When adding a new JSON file:

1. Match the naming convention (`guid_<guid>.json`).
2. Provide meaningful `name` and `notes`.
3. Populate `controls` with selector data or leave `selector` as `null` if it
   must be resolved dynamically.
4. Document any validation rules (`get_info_expect`, `payload`) that help detect
   firmware deviations.

Run `uvc_generate_quirk.py` from the `examples/` directory to bootstrap a new
file based on the descriptors advertised by a device.

