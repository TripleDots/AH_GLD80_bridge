# Other DAWs and NLEs — standard MCU workflow

Bridge v0.6.42 uses the basic MCU assignment model by default:

- PAN controls track Pan.
- Entering the GAIN layer sends MCU Send assignment (`0x29`) and Flip (`0x32`).
- The motor faders then control the currently selected Send.
- Turning GAIN sends Cursor Down for next Send and Cursor Up for previous Send.
- Moving PAN closes the Send-fader page and restores normal track faders.

The bridge sends Send assignment to the Universal and each configured Extender endpoint before the global Flip command. It does not convert GAIN into relative Send-level V-Pot movement in the default mapping.

Support depends on the host. Logic, Studio One, Cubase/Nuendo and other full MCU implementations may support the complete assignment area, but some applications and NLEs implement only volume, Pan, Mute, Solo and transport. A host may therefore ignore Send assignment, Flip or Send navigation even though its basic MCU controls work.

## Optional SoftKey 8 shortcut

Enable **Also use SoftKey 8 to toggle the selected Send on the motor faders** to use SoftKey 8 as an additional Flip control. GAIN remains previous/next Send. Press SoftKey 8 again to restore the normal fader page.

## Banking

Bank and Channel commands are sent only through the Universal endpoint; Extenders follow as one MCU surface. The bridge coalesces feedback, clears absent switch states and repaints the final fader page behind a generation guard. Standard MCU has no universal exact-page request, so the host must publish its new state after a bank change.

## Compatibility fallback

The Control Mapping page still offers **Legacy Send level rotary**. This keeps the old Send-assignment plus relative V-Pot experiment for hosts where it happened to work, but it can hit the GLD GAIN accumulator limitations and is not the recommended default.
