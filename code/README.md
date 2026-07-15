# GLD-80 DAW Bridge

## v0.6.42 — GAIN/PAN switch the motor-fader page immediately

- **GAIN no longer controls Send level with the rotary.** That absolute-to-relative conversion was not dependable on physical GLD hardware.
- **Pressing GAIN opens the standard MCU Send + Flip page as soon as the GLD publishes its layer refresh.** The motor faders move directly to the selected Send; the first GAIN turn remains a fallback on firmware that does not publish the refresh.
- **GAIN selects the Send.** Clockwise chooses the next Send; counter-clockwise chooses the previous Send. It stops at Send 1 and does not wrap.
- **Pressing PAN restores the normal track-volume faders** from the cached DAW page; moving PAN remains a fallback and continues to control track Pan.
- **Generic MCU hosts receive only standard MCU messages:** Send assignment (`0x29`), Flip (`0x32`) and Cursor Up/Down for Send selection.
- **REAPER companion v1.23 mirrors the same workflow** because stock REAPER MCU does not implement the standard Send page. It does not use GAIN as a Send-level control.
- **SoftKey 8 remains optional** as a second way to toggle the selected Send on the motor faders.

## What the bridge does

Cross-platform Python/PySide6 application connecting Allen & Heath GLD MIDI Strips to a DAW through **MCU**, **HUI** or transparent **Raw MIDI**. It supports 32 strips over four eight-channel DAW port pairs, direct GLD TCP MIDI, physical strip names/colours and a Vegas hardware test.

Project and issue tracker: https://github.com/TripleDots/AH_GLD80_bridge

## Protocol modes

### MCU — default

- GLD motor fader ↔ MCU 14-bit motor fader
- GLD PAN ↔ MCU track Pan
- GLD GAIN layer selection/report → MCU Send assignment + Flip
- Motor faders in GAIN mode → selected Send level
- GAIN turns in Send mode → previous/next Send
- PAN layer selection/report → leave Send mode and restore normal track faders
- GLD Mute ↔ MCU Mute
- GLD MIX ↔ MCU Select / V-Pot push in plug-in pages
- GLD PAFL ↔ MCU Solo
- MCU REC/RDY tally → optional strip-colour pulse
- MCU scribble strip → GLD strip name when enabled

The bridge does not generate fader-smoothing steps. DAW feedback is applied directly after echo-loop filtering.

The older **Legacy Send level rotary** mapping remains available in Control Mapping for experiments and existing custom profiles, but it is no longer the default.

### HUI

Core HUI support for Pro Tools: ping/reply, 14-bit faders, timed touch emulation, Pan, Mute, Solo, Select and channel-display names.

### Raw MIDI

Raw mode forwards every message unchanged between the GLD and the first DAW port pair. Do not loop its output back to its input.

## MCU port setup

Use four independent bidirectional endpoint pairs:

| Bridge bank | MCU device | DAW offset |
|---|---|---:|
| 1 | Mackie Control Universal | 0 |
| 2 | Mackie Control Extender | 8 |
| 3 | Mackie Control Extender | 16 |
| 4 | Mackie Control Extender | 24 |

Each MCU device controls eight tracks. In REAPER set **Size tweak = 8** for every surface. Disable the same endpoints under ordinary MIDI Devices so the DAW does not open them twice.

For A/B loopback cables, use one side for both bridge fields and the opposite side for both DAW fields. Mixing A and B inside the bridge can create a MIDI loop.

## Faders, banking and initial state

Normal faders use standard MCU. The bridge converts the GLD's physical `0..127` position to MCU `0..16383` pitch bend and converts DAW feedback back to `0..127`.

MCU has no portable “request every current parameter now” command. Initial state therefore depends on the DAW publishing its surface state when the device connects, a project opens or a page changes. In REAPER, companion v1.23 also publishes an exact visible-track fader page so full-bank changes can move directly to the destination without an intentional all-down sweep.

A Bank/Channel action is sent once through the Universal endpoint. Extenders follow it as one surface. The bridge coalesces the feedback burst and uses generation guards so delayed packets from the previous bank cannot repaint the new page.

## Send workflow

### Generic MCU DAWs and NLEs

The default workflow uses only common MCU controls:

1. A GAIN layer-selection refresh (or the first GAIN turn as fallback) sends Send assignment (`0x29`) to the Universal and Extender endpoints.
2. Flip (`0x32`) moves the selected Send to the motor faders.
3. Turning GAIN sends Cursor Down for next Send or Cursor Up for previous Send.
4. A PAN layer-selection refresh (or the first PAN turn as fallback) exits the Send page and restores normal track faders.

Host support varies. Some applications advertise MCU support but implement only volume, Pan, Mute, Solo and transport. In those hosts Send assignment, Flip or Send navigation may be ignored or interpreted differently.

### REAPER

Stock REAPER MCU keeps V-Pots on Pan and does not expose the standard Send assignment page. Run the bundled **companion v1.23** and enable the REAPER companion option in the bridge.

The companion:

- places the currently selected existing Send on the 32 visible motor faders;
- changes the selected Send slot when GAIN is turned;
- publishes exact track Pan, names, colours and optional FX pages;
- follows the MCU bank offset without performing the Bank/Channel action itself;
- never creates missing Sends.

A track without the selected Send stays at `−∞`. Send selection is global for the visible surface: for example, selecting Send 3 puts Send 3 of every visible track on its corresponding fader.

### Optional SoftKey 8 shortcut

Enable **Also use SoftKey 8 to toggle the selected Send on the motor faders** to use SoftKey 8 as an additional open/close control for the same page. GAIN still chooses the Send. Press SoftKey 8 again, or move PAN after entering through GAIN, to restore normal track faders.

## Plug-in control

With the optional REAPER companion enabled, Soft 9 opens the selected track's FX list. MIX selects an insert, PAN rotaries control the eight displayed parameters, Custom 1 changes parameter/insert pages and Custom 2 moves between inserts. Holding Soft 9 returns to the track page.

The helper strips format prefixes such as `VST:`, `VST3:` and `AU:` from insert names before the GLD's five visible characters are chosen. Outside REAPER, the bridge uses the host's standard MCU Plug-in assignment and V-Pot implementation.

## REAPER companion v1.23 installation

Stop every older companion copy, then load and run exactly one copy of:

`integrations/reaper/GLD80 Bridge - Sync REAPER track names and colours.lua`

Enable **REAPER companion (required for the Send fader page in REAPER)** in the bridge. Wait until the status says **REAPER helper connected** before using the GAIN Send page.

The companion does not operate transport, press track buttons or perform MCU Bank/Channel actions. Normal transport, faders, buttons and banking remain on the MCU control surfaces.

## REC-arm indication

When enabled, an MCU REC/RDY tally pulses the corresponding strip colour red, or white when the normal track colour is already red. The original colour is restored after the short pulse. This needs the GLD Editor-control connection on TCP `51321`.

## GLD connections

- Public MIDI/TCP: port `51325`
- Optional Editor-control name/colour/Pan redraw: port `51321`

GLD remote-control slots are limited. Close unused GLD Editor/remote clients if the bridge reports that all connections are in use.

## Physical controls available to the bridge

The GLD MIDI Strip protocol exposes the motor fader, Gain, Pan, Custom 1, Custom 2, Mute, MIX and PAFL for each of 32 strips. The bridge also supports ten GLD SoftKeys when they are configured to send the documented custom-note convention.

The GLD **Sel** key and the global Gain/Pan/Custom layer-selection keys do not transmit distinct dedicated MIDI Strip button messages. Some GLD firmware republishes several stored rotary values when GAIN or PAN is selected; v0.6.42 recognises that multi-strip refresh and switches the motor-fader page immediately. If a desk/firmware combination does not publish such a refresh, the first rotary turn or the optional SoftKey 8 toggle remains the reliable fallback.

## Installation

### Windows

Run `INSTALL_WINDOWS.cmd`, then `START_WINDOWS.cmd`.

### macOS

Run `INSTALL_MACOS.command`, then `START_MACOS.command`.

### Linux

Run `INSTALL_LINUX.sh`, then `START_LINUX.sh`.

Python requirements are listed in `requirements.txt`; supported Python is 3.10–3.12.

## Troubleshooting

**GAIN/PAN changes the fader page only after the first turn**

- Confirm the bridge is v0.6.42 or newer and that **REAPER helper connected** is shown.
- The GLD selector buttons have no dedicated public MIDI message. v0.6.42 detects the layer refresh emitted by supported firmware; if your desk does not emit it, enable SoftKey 8 as the explicit, immediate Send-page toggle.
- PAN and GAIN movement remain safe fallbacks and do not change a level before the page switch is complete.

**GAIN does not put Sends on the faders in REAPER**

- Stop all older companion actions and run bundled companion v1.23 exactly once.
- Enable the REAPER companion option.
- Wait for **REAPER helper connected**.
- Confirm the track already has the selected Send; the helper deliberately does not create routes.

**GAIN does not put Sends on the faders in another host**

- Confirm GAIN is mapped to **Send fader flip + previous/next Send**.
- Verify Universal and Extender output ports reach the matching DAW surfaces.
- Check whether the host implements MCU Send assignment `0x29`, Flip `0x32` and Cursor Up/Down in Send view.
- Stop the REAPER companion while testing another host.

**Turning GAIN selects too quickly or appears to skip**

- Use one deliberate turn per Send. The bridge applies a short debounce so a single physical gesture does not select several Sends.
- GAIN selection stops at Send 1; it does not wrap.

**Faders stutter or unrelated faders move**

- Make sure each MCU endpoint is opened by exactly one bridge bank and one DAW surface.
- Disable those endpoints under the DAW's ordinary MIDI input/output page.
- Check that no loopback cable routes the bridge output back into its own input.
- In REAPER, run only one current companion instance.

**Old project values appear only after touching controls**

- Reconnect or refresh the MCU surface in the DAW. There is no universal MCU state-query message the bridge can send.
- Verify the DAW output ports are connected to the bridge's **DAW → app** inputs.
