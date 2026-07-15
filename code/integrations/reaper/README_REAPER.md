# REAPER setup — MCU plus companion v1.23

Bridge v0.6.42 keeps normal faders, Bank/Channel, transport and track buttons on standard MCU. Companion v1.23 adds the Send-fader page that stock REAPER MCU does not implement, plus exact Pan, FX pages, names and colours.

## MCU surfaces

Create four control surfaces in REAPER:

| Surface | Offset | Size tweak |
|---|---:|---:|
| Mackie Control Universal | 0 | 8 |
| Mackie Control Extender | 8 | 8 |
| Mackie Control Extender | 16 | 8 |
| Mackie Control Extender | 24 | 8 |

Use a unique MIDI endpoint pair for each surface. Disable those endpoints in REAPER's ordinary MIDI Devices page.

## Install companion v1.23

1. Open REAPER's Action List.
2. Load `GLD80 Bridge - Sync REAPER track names and colours.lua` from this folder.
3. Stop every older copy of the action.
4. Run exactly one current copy.
5. In the bridge enable **REAPER companion (required for the Send fader page in REAPER)**.
6. Wait for **REAPER helper connected**.

## GAIN Send workflow

- Entering/turning GLD **GAIN** switches the motor faders to the selected Send.
- Clockwise GAIN chooses the next Send; counter-clockwise chooses the previous Send.
- Selection stops at Send 1 and does not wrap.
- Moving **PAN** restores normal track-volume faders and continues to control track Pan.
- The GAIN bar is used only as an input accumulator and does not display the Send level.
- The companion never creates missing Sends. A track without the selected Send stays at `−∞`.

Send selection is one shared surface slot. If Send 3 is selected, each visible motor fader controls Send 3 of its own track.

## Optional SoftKey 8

Enable **Also use SoftKey 8 to toggle the selected Send on the motor faders** to open or close the same page with SoftKey 8. GAIN still selects the Send; it no longer controls hidden track volume.

## Exact faders and banking

The companion publishes exact selected-Send values while the Send page is active and exact track-volume values on the normal page. During a full bank change, the bridge holds current motor positions until the companion acknowledges the destination page, then moves directly to the new values. Late all-down and old-page MCU packets are discarded behind a generation guard.

The companion follows the MCU bank offset; it does not perform the Bank/Channel action itself.

## Pan, FX, names and colours

The helper reads exact visible-track Pan values, track names, custom RGB colours and FX metadata. Soft 9 opens the selected track's FX list; MIX chooses an insert; PAN controls the eight shown parameters; Custom 1/2 navigate pages and inserts.

It does not operate transport, press track buttons or create Sends.
