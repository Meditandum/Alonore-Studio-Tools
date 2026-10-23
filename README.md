# Alonore Studio Tools

An experimental studio-lighting add-on for Blender, developed by **Alonore Art**.

**Current build:** v0.11.6 (pre-release). Tested by the project creator in **Blender 5.3 Beta**. The add-on metadata declares Blender 4.2+, but compatibility with all those releases has **not** been verified.

## Features

- Create a studio rig with camera and adjustable lights.
- Unified Light Manager for core and additional Area / Point / Spot / Sun lights.
- LightPaint reflection placement for original studio lights, with saved target markers.
- Softbox shape, size, roll, and spread controls for original studio lights.
- Position, power, Kelvin/color, and softbox randomizers (scope differs by tool).
- Per-light Position / Power / Color randomizer locks.
- Four built-in lighting presets and six local custom preset slots.

## Installation

1. Download `alonore_studio_tools.py`.
2. In Blender, use **Edit → Preferences → Add-ons → Install from Disk** (or the equivalent extension installation action for your Blender build).
3. Select the Python file, then enable **Alonore Studio Tools**.
4. In the 3D Viewport, open the sidebar (`N`) and select the **Alonore** tab.
5. Select a mesh and click **Create Studio Setup**.

**Before updating:** Back up your `.blend` file and your previous add-on version. Avoid enabling two versions simultaneously.

## Known limitations

- The five original studio lights and additional lights still use different underlying settings systems. Some randomizers and LightPaint features currently apply only to original lights.
- Six custom preset slots save the original rig settings; they are **not** a portable community preset format for arbitrary lights.
- No built-in online catalog, preset sharing service, or account system yet.
- Blender version compatibility, clean-install behavior, undo/redo, and complete persistence testing need wider community testing.

## Feedback and contributions

Please include Blender version, add-on version, reproduction steps, expected/actual behavior, and the Python traceback if applicable. See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

Licensed under **GNU General Public License v3.0 or later (GPL-3.0-or-later)**. See [LICENSE](LICENSE). Contributions are welcome under the same license.

## Roadmap

See [ROADMAP.md](ROADMAP.md).
