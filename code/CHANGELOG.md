# Changelog

## v0.6.42

- Added early GAIN/PAN layer-refresh detection. On GLD firmware that republishes the selected rotary layer, pressing **GAIN** now opens the selected-Send motor-fader page and pressing **PAN** restores normal track-volume faders without requiring the first encoder turn.
- Preserved the exact cached REAPER Send and track-volume pages across the mode transition and repainted the motors immediately. The next companion snapshot remains authoritative and repairs any missed write.
- Stopped retransmitting unchanged Pan feedback on every 10 ms companion poll. This prevents the desk's PAN-layer selection refresh from being continuously swallowed as a feedback echo.
- Added a late multi-strip echo classifier so a quick layer switch after a bank/mode refresh is recognised while immediate echoes of the bridge's own writes are still ignored.
- Stopped recentring all GAIN accumulators when leaving Send flip; this avoids hiding a quick subsequent GAIN-layer selection.
- The bundled REAPER companion remains v1.23 / snapshot v17; no script reinstall is required when upgrading from v0.6.41.
- Expanded automated regression coverage from 147 to 152 tests. All tests and Python compilation pass.

## v0.6.41

- Replaced the unreliable default GAIN-level emulation with a basic MCU Send + Flip workflow. Entering GAIN puts the selected Send on the motor faders; GAIN no longer changes Send or track volume directly.
- Added GAIN-based Send selection: clockwise sends Cursor Down / next Send, counter-clockwise sends Cursor Up / previous Send, with a short gesture debounce and no wrap below Send 1.
- PAN movement now leaves the GAIN-owned Send page and restores normal track faders before applying the Pan gesture.
- Generic hosts receive only standard MCU Send assignment, Flip and cursor messages. The old relative Send-level rotary remains available as an explicitly labelled legacy control mapping.
- Updated the REAPER companion to v1.23 / snapshot v17. It mirrors the same selected-Send motor-fader page because stock REAPER MCU omits Send assignment; it no longer uses GAIN for Send level or hidden track volume.
- Suppressed the complete GLD GAIN layer-activation burst so stored per-strip values cannot accidentally skip through Sends when the layer is selected.
- Migrated existing default GAIN mappings to **Send fader flip + previous/next Send**, updated the UI and documentation, and retained the optional SoftKey 8 shortcut.
- All 147 automated tests and Python compilation pass.

## v0.6.40

- Reverted the v0.6.39 absolute DAW-to-GLD GAIN feedback path after physical GLD testing showed that it can pull the bounded MIDI Strip accumulator upward and pin it at `127`.
- GAIN now acts only as a relative movement source. Exact REAPER Send and hidden track-volume values stay in the bridge cache and on the motor-fader flip page; they are never mirrored into GAIN.
- Added a per-strip 220 ms idle re-centre and an immediate endpoint re-centre. Echo guards consume the bridge's own centre writes so they cannot change the DAW value.
- Kept exact PAN feedback unchanged through the known GLD Editor-control Pan frame. No equivalent safe GAIN redraw frame is currently known.
- Updated the bundled REAPER companion to v1.22 / snapshot v16 and revised all setup documentation to describe the intentional GAIN-display limitation.
- Preserved the exact SoftKey 8 Send-fader page, no-dip REAPER banking, and standard MCU Send/Flip behavior for other hosts.
- All 146 automated tests and Python compilation pass.

## v0.6.39

- Added absolute bidirectional REAPER Gain/Send control with companion v1.21 / snapshot v15. Exact Send 1 values from REAPER are now written to the mapped GLD GAIN rotary, and physical GAIN values are written directly back as exact `0..127` Send targets.
- Removed delta detection and endpoint re-centring from the v1.21 REAPER path. This eliminates the bounded-accumulator lock, one-direction-only behavior and bounce that remained in the relative emulation.
- Extended the same absolute feedback model to true Send fader flip: motor faders own Send 1 while GAIN displays and controls exact normal track volume through a dedicated companion mailbox.
- Changed the Lua fader/Send conversion to round to the nearest 7-bit step, making bridge → REAPER → snapshot round-trips idempotent across the full `−∞..+10 dB` range.
- Preserved the existing generic MCU Send-assignment and relative V-Pot path for other DAWs/NLEs.
- Added regression tests for external DAW Send feedback, absolute direction reversal, endpoint behavior, failed-command rollback, flipped volume feedback and taper round-trip stability. Test count is now 146.

## v0.6.38

- Removed the absolute Send-to-Gain feedback loop in REAPER. The GLD Gain layer remains a pure movement accumulator, so counter-clockwise and clockwise turns can alternate without every snapshot fighting the next detent.
- Restored endpoint re-centring for the v0.6.38 relative Send path. Reaching rotary value `0` or `127` re-bases the bounded GLD accumulator to centre without changing the DAW Send.
- Implemented a real Send fader flip. While SoftKey 8 is active, the motor faders control Send 1 and the GAIN rotary controls the original track-volume fader.
- Added exact hidden track-volume caching and acknowledgement guards for the flipped REAPER rotary.
- Extended REAPER Bank protection beyond the initial coalescing timer so late all-down packets are held until the exact destination snapshot arrives.
- Updated the bundled companion to v1.20 / snapshot v14.
- Expanded automated regression coverage from 133 to 142 tests.

## v0.6.37

- Updated the bundled REAPER companion to v1.19 / snapshot v14. Every visible track now publishes the exact first-Send value in addition to Pan and track-fader repair data.
- Replaced REAPER Gain→Send's relative dB accumulator with the documented GLD fader/Send `0..127` taper (`0 = −∞`, `107 = 0 dB`, `127 = +10 dB`). GAIN and SoftKey 8 fader flip now share one absolute value domain, eliminating the lock between `−∞` and approximately `−60 dB`.
- Changed full REAPER Bank transitions to hold existing motor positions until the atomic destination snapshot arrives. Faders now move directly to the new page instead of first sweeping to `−∞`.
- Added a 900 ms exact-target guard after the destination snapshot so delayed REAPER MCU clear/old-page packets cannot pull one or more channels down after the correct page was already applied.
- Preserved the existing generic MCU Send assignment and Bank-refresh paths for other DAWs and NLEs.
- Expanded automated regression coverage from 128 to 133 tests.

## v0.6.36

- Updated the bundled REAPER companion to v1.18 / snapshot v13. Gain→Send now uses a persistent monotonic mailbox instead of deleting each command file after reading, eliminating the race that could discard the first detent when reversing direction.
- Added low-level Send recovery: the first clockwise detent from a REAPER Send below `−90 dB` re-enters at `−89.5 dB`, so tiny retained amplitudes around `−110…−150 dB` cannot make the rotary appear permanently stuck.
- Hardened full Bank changes again: missing fader tallies clear stale motors, absent Mute/Solo/Select/REC tallies clear old-bank LEDs, omitted REAPER surface slots are neutralised, and the settled fader page is repeated at 90/240/520 ms behind a generation token that rejects delayed writes from an older bank.
- Added the opt-in **Use SoftKey 8 to toggle Send 1 on the motor faders** option. REAPER uses companion-owned exact Send faders; generic hosts receive standard MCU Send assignment plus Flip.
- Forced Reset channels and Disconnect to repaint every bridge channel widget as well as the physical GLD, including focused name fields, before asynchronous port teardown starts.
- Migrated configuration to version 27 without changing an existing SoftKey 8 mapping unless the new option is explicitly enabled.
- Expanded automated regression coverage from 116 to 128 tests.

## v0.6.35

- Corrected GLD rotary ownership: PAN remains track Pan and GAIN is dedicated to Send control. Generic MCU hosts receive canonical Send assignment `0x29` plus relative V-Pot movement, with assignment reasserted at the start of each gesture and after bank changes.
- Added REAPER companion v1.17 / snapshot v12. Because stock REAPER MCU interprets V-Pots as Pan instead of implementing Send assignment, the companion applies lossless cumulative Gain movement to the first existing track Send in 0.5 dB steps without creating missing Sends. The bridge suppresses duplicate MCU V-Pots whenever this path is active.
- Hardened full-bank refreshes: the settle timer now restarts for every arriving packet, final values are coalesced, all fader output caches are invalidated, equal-valued motor positions are forced, and a 1.25-second hard deadline prevents continuous automation from leaving motors stuck in transition.
- Added **Reset channels**, which preserves the GLD/DAW connections while resetting names to `MIDI 01..32`, colours to white, faders to `−∞`, all rotary layers to centre, and Mute/MIX/PAFL plus local Select/Solo/REC states to off.
- Reworked Disconnect so callback generations are invalidated before teardown, port closing runs outside the Qt GUI thread, and every MIDI backend close has a timeout. Late callbacks and a blocked WinMM/rtmidi close can no longer freeze the interface.
- Removed blocking sleeps from surface reset, added a short post-reset DAW-feedback guard and updated REAPER/other-host setup documentation.
- Expanded automated regression coverage from 101 to 116 tests.

## v0.6.34

- Fixed Gain-layer Send control on MCU Extender banks. The bridge now sends the standard Send-assignment click to both the Universal and the matching Extender endpoint before that endpoint receives its first relative V-Pot movement, eliminating the cross-port race that could leave the rotary controlling Pan.
- Stabilised full Bank Left/Right changes by coalescing the host's short old-page/clear/new-page refresh burst and applying only the final fader, Mute, Solo, Select, REC and Pan tallies.
- Cleared page-scoped fader echo histories, button settle targets and rotary residue whenever the visible track offset changes, so state belonging to the previous page cannot suppress the new page's only feedback packet.
- Added regression coverage for Extender Send assignment ordering and full-bank clear-sweep recovery.

## v0.6.33

- Reworked Send rotation to the smallest standard-MCU path: one Send-assignment click followed by signed relative V-Pot movement on the matching Universal/Extender port. No Lua Send writer, dB curve, absolute Send cache or fader-owned Send code is involved.
- Stopped re-centring the GLD Gain accumulator after every Send packet. It is now rebased only at the hard `0`/`127` endpoints, preventing real-hardware gestures from being swallowed while retaining endless rotation.
- Reduced optional REAPER helper polling from 20 ms to 10 ms and accelerated paced GLD Editor updates to two short frames every 4 ms. Stale track-page packets are discarded immediately when an FX page is requested.
- Added companion v1.16: FX format prefixes such as `VST:`, `VST3:`, `AU:`, `CLAP:` and `JS:` are removed before the name reaches the GLD, and trailing vendor text is trimmed.
- Unnamed REAPER tracks now use stable project-index labels `Ch001`, `Ch002`, … instead of REAPER's generic `Track` display name.
- Corrected the MCU mapping UI labels for Bank Right (`0x2F`) and Channel Left (`0x30`).
- Kept exact Pan and displayed FX parameters as the only optional Lua-controlled values; faders, Sends, banking, transport and track buttons remain standard MCU.

## v0.6.32

- Restored optional REAPER exact Pan and FX insert/parameter pages while leaving faders, Sends, Bank/Channel, transport and track buttons on standard MCU.
- Added companion v1.15 and removed plug-in-format ambiguity from the control ownership model.
- Corrected standard MCU Bank/Channel note handling and kept navigation on the Universal port only.
- Converted Send movement to relative MCU direction and prevented DAW V-Pot ring feedback from fighting the GLD's bounded Gain accumulator.
- Stabilised the optional companion status across snapshot replacement gaps.

## v0.6.31

- Restored one direct **standard MCU** control path for faders, Pan, Sends, plug-in parameters, buttons and Bank/Channel. REAPER-specific exact-value command files no longer participate in control or feedback.
- Removed all remaining Send-on-faders runtime/state code. Sends are relative MCU V-Pots only and can never take ownership of normal track faders.
- Removed companion-owned fader snapshots, Pan/Send/plug-in value writers, retries and mode acknowledgements that could fight stock MCU feedback and cause stutter, snap-back or phantom movement.
- Made MCU motor-fader feedback authoritative again, with no persistent value deadband or generated smoothing steps.
- Restored DAW-to-GLD Send/plug-in position feedback from standard MCU V-Pot LED-ring CC `48..55`; a dark ring maps to the bottom/off position in those contexts.
- Kept Bank/Channel as one click on the Universal port. The three Extenders follow the host's normal MCU surface group.
- Added optional REAPER companion **v1.14**, limited to names, colours and label-page offset. It deletes old control-mailbox files on startup and never writes fader, Pan, Send or plug-in values.
- Disabled the optional companion during configuration migration and removed the obsolete exact-Pan and Send-fader settings.
- Added regression coverage for direct MCU faders, rotary Sends, dark-ring `-inf` feedback, standard plug-in V-Pots and metadata-only Lua behavior.

## v0.6.30

- Removed the experimental motor-fader Send-control path, its UI/config option and every runtime branch. Send mode is now rotary-only and can never steal normal track-fader input or feedback.
- Stopped echoing absolute Send/plug-in values back to the same GLD rotary on every detent. Contextual turns remain monotonic and companion feedback is deferred until the gesture has settled, eliminating direction reversal and stuck controls.
- Added companion v1.13 / snapshot v10 with an active-project identity. Opening an existing REAPER project/tab now forces a complete read and surface repaint of track faders, Pan, selected Sends and plug-in parameters without requiring a first user movement.
- Forced a complete value refresh after connection, project changes, acknowledged Bank/Channel moves and Send/plug-in page changes. Stale pending fader/rotary commands and motor targets are cleared when pages change.
- Kept track faders authoritative in every mode and added a persistent two-count motor deadband to suppress small conversion-driven phantom nudges.
- Expanded regression coverage to 121 tests.

## v0.6.29

- Returned Send control to the Gain rotaries by default and migrated v0.6.28 configurations back to that workflow. The optional motor-fader Send mode remains available.
- Removed the provisional `−∞` rotary write on Send entry. The bridge now waits for the acknowledged REAPER Send page and force-preloads every mapped rotary with the real existing Send level before normal movement.
- Added a dedicated Plug-in-parameter value domain, first-detent queue and acknowledgement/retry path. A stale snapshot can no longer erase a just-turned parameter, and the parameter page is preloaded before the first movement.
- Added companion v1.12 / snapshot v9 with monotonic per-strip Pan, Send-level and Plug-in-parameter command mailboxes. All exact-value sequences now start from a restart-safe wall-clock floor, so restarting only the bridge cannot make a still-running companion reject new movements.
- Suppressed one- and two-count motor corrections for 600 ms after a physical fader movement, eliminating small round-trip quantisation nudges while preserving larger automation moves.
- Kept exact companion fader feedback authoritative for a short grace period across snapshot-file replacement or reconnect gaps, blocking transient stock-MCU all-down sweeps.
- Fixed configuration persistence to write the current configuration version instead of an older version number.
- Expanded regression coverage to 116 tests.

## v0.6.28

- Added companion v1.11 / snapshot v8 exact visible-track fader values. A returned Bank/Channel page now restores every motor fader from REAPER's project state instead of depending on stock-MCU refresh timing.
- Made companion fader snapshots authoritative while v1.11 is connected. Transient all-down MCU clear sweeps during banking, stop/refresh or surface reinitialisation are ignored, preventing unexpected moves to `−∞`.
- Added a one-second exact-fader acknowledgement guard so the companion cannot briefly motor a just-moved physical fader back to its pre-change value while REAPER is committing the MCU write.
- Locked Plug-in parameter pages to the track and insert that were explicitly opened. Temporary REAPER focus/selection changes can no longer demote the surface to Plug-in Select after a few seconds.
- Added **Use motor faders for Send levels**, enabled by default. Turning Gain enters Send mode; faders then control the selected Send on the measured GLD law (`0 = −∞`, `98 = 0 dB`, `127 = +10 dB`). Returning to Pan/track mode restores exact track-volume positions.
- Kept generic DAW operation on canonical MCU: one Send assignment click on the Universal port and normal MCU fader messages. REAPER-specific exact Send writes remain companion-owned, avoiding duplicate MCU and companion commands.
- Removed the provisional `−∞` rotary repaint on Send entry. Existing Sends keep their real level and missing routes remain uncreated at `−∞`.
- Added a fail-safe: if the REAPER companion disappears during a companion-owned Send or Plug-in page, the bridge returns to normal track mode instead of letting a fader fall through to the wrong stock-MCU function.
- Extended configuration migration to version 20 and expanded regression coverage to 108 tests, including locked Plug-in pages, exact Send faders, ignored MCU clear sweeps and deterministic fader restoration.

## v0.6.27

- Completed a full MCU/companion routing audit. Generic DAWs receive one canonical MCU action; REAPER companion Plug-in/Send pages no longer receive a duplicate stock-MCU assignment in parallel.
- Added companion v1.10 and snapshot v7 with one active-instance token, monotonic snapshot sequence, shared Plug-in/Send command ordering and exact mode acknowledgements.
- Removed stale REAPER command mailboxes on companion startup. A delayed command from an earlier run can no longer reactivate Plug-in or Send mode.
- Re-synchronised only the current local mode after a companion restart and aligned its metadata offset before accepting names or colours.
- Kept Bank/Channel as one standard MCU Universal-port click. Offline banking no longer leaves a stale companion command file; a later companion connection aligns to the already-visible surface page.
- Forced a complete name/colour repaint after each acknowledged bank move, fixing stale colours left by an interrupted REC-arm pulse or by tracks sharing the same model colour.
- Standardised missing/initial Sends at `0 = −∞` without resetting existing REAPER Sends. First physical detents are queued until exact Send feedback arrives instead of being lost.
- Made router attachment idempotent and independent per signal, preventing a partial disconnect/rebind from registering duplicate GLD or DAW receivers.
- Once companion v1.10 owns the snapshot, legacy companion snapshots are ignored without downgrading the accepted owner, so an accidentally still-running v1.9 copy cannot resume Plug-in/Send control.
- Expanded regression coverage to 103 tests, including single-owner MCU/companion routing, one-time Raw MIDI forwarding, router reattachment, legacy-writer rejection, stale mailbox cleanup, offline bank handling, forced label repaint and queued first Send detents.

## v0.6.23

- Fixed Gain/Send and Pan-mapped Send rotaries remaining pinned at the right edge of the GLD's absolute `0..127` parameter range.
- Re-centred every Send-mapped physical rotary after each movement so it behaves as an endless relative encoder. The centre write is repeated deliberately after fractional-speed detents and after end-stop movement to rebase either GLD accumulator behaviour.
- Kept exact REAPER Send values separate from the physical rotary centre; DAW snapshots no longer write the absolute Send value back into the GLD Gain layer.
- Added regression coverage for continuing rightward Send movement after reaching value `127` and for centred exact-Send snapshot feedback.

## v0.6.22

- Replaced **Generic Pan speed** with one shared **Rotary speed** control for continuous Pan, Send and Plug-in parameter movement.
- Added fractional speeds from `0.10×` to `16.00×`; sub-1.0 settings retain fractional detents so slow movement stays smooth and predictable.
- Applied the same speed scaling to exact REAPER Pan, exact REAPER Send levels, standard MCU/HUI relative V-Pots and REAPER Plug-in parameters.
- Separated physical GLD rotary baselines from feedback values. A Gain/Send rotary can now continue from either the old physical accumulator or a newly written Send value without producing a large artificial jump.
- Added regression coverage for slowed Send, exact Pan, Plug-in parameters and feedback-rebase handling.

## v0.6.21

- Fixed REAPER Plug-in control flashing in and out when delayed snapshots or multiple companion instances alternated between Tracks, insert-list and parameter modes.
- Made bridge mode transitions authoritative until the exact requested companion mode is confirmed. Expired confirmation windows now keep rejecting stale snapshots instead of accepting them.
- Allowed the one legitimate companion-only transition from parameter view to the insert list when the selected REAPER track changes.
- Updated the bundled REAPER companion to v1.7 with single-active-instance election, preventing two newly started copies from consuming commands and publishing competing snapshots.
- Seeded Plug-in and Send command sequences from the current time so restarting the bridge cannot produce sequence numbers lower than those remembered by a still-running companion.
- Added regression coverage for duplicate-companion Tracks/insert-list snapshots, delayed confirmations and selected-track changes.

## v0.6.20

- Added per-user macOS and Linux install, start and uninstall scripts. Both installers create a private Python environment, application launcher and desktop/application entry without requiring administrator/root access. Uninstall keeps user settings unless `--purge` is supplied.
- Fixed REAPER Plug-in pages flashing between the insert list and parameter page. The bridge now holds a requested companion mode until the matching snapshot is confirmed, and companion v1.6 rejects stale command files and tolerates the temporary loss of track selection when an FX window takes focus.
- Fixed Gain/Send controls jumping strongly to the right. Gain is now treated as a relative physical movement against the exact DAW Send value, with pickup/rebase protection for large absolute-position discontinuities when changing rotary layers.
- Intentional Disconnect/quit no longer sends any Mute command to the GLD. Faders, Pan, other rotary accumulators, MIX, PAFL, names and colours are still neutralised, while Mute is deliberately left untouched to prevent an inverted tally after reconnecting.
- Updated the bundled REAPER companion to v1.6 and expanded the setup/troubleshooting documentation.

## v0.6.19

- Added per-protocol MCU/HUI control-mapping pages with save/load/restore-default profile support while preserving the established mapping as default.
- Renamed the startup option to **Start on bootup, minimized** and added Windows, macOS and Linux per-user startup backends.
- Added a scalable, tabbed mixer/settings layout and a detached raw MIDI/TCP log window.
- Added a best-effort neutral GLD surface reset before an intentional disconnect. v0.6.20 refines this reset so Mute is deliberately left untouched.

## v0.6.18

- Added standard MCU Send assignment on the GLD Gain rotary layer. Custom 1 selects Send slots while faders remain track volume.
- Updated the REAPER companion to v1.5 for exact existing-Send level synchronisation without creating routes.
- Added regression coverage and full GLD setup documentation for the Send workflow.

## v0.6.17

- Fixed REAPER companion Plug-in mode not opening. The bridge wrote the `plugin` action from Soft 9, but the Python command publisher rejected that action because it was missing from its allowlist.
- Added regression coverage for publishing the REAPER `plugin` action and for Soft 9 forwarding it while companion v1.4 is connected.
- Added the README recommendation to keep MIDI Strip 32 fixed at the far-right corner as a dedicated, easy-to-find Custom 1/2 navigation strip.
- Corrected the README release heading and documented the exact REAPER companion Plug-in workflow.

## v0.6.16

- Added a pure standard-MCU Plug-in workflow for hosts that support MCU Plug-in assignment, V-Pot pushes, V-Pot parameters and cursor navigation.
- Mapped Soft 9 to MCU Plug-in and Soft 10 to REC/RDY for the currently selected channels.
- Added a REAPER companion v1.4 compatibility layer for insert lists, FX selection, parameter pages and opening the selected FX window.
- Expanded the README with complete GLD MIDI Strip, SoftKey, Custom rotary, Scene Safe, network and Show-saving setup guidance.

## v0.6.15

- Added standard MCU Bank Left/Right and Channel Left/Right controls.
- Added configurable navigation from one GLD MIDI Strip: Custom 1 selects Bank Left/Right and Custom 2 selects Channel Left/Right. MIDI Strip 32 is the default.
- Recentres the selected GLD Custom parameter after each action so its absolute `0..127` value behaves as a repeatable navigation encoder; the first movement after connection arms it.
- Added Bank and Channel navigation buttons to the bridge UI.
- Updated the optional REAPER companion to v1.3 and snapshot format v3. It now follows the current MCU track offset for names, colours and exact Pan while MCU remains responsible for the actual bank change.
- Added configuration migration and regression coverage for all four standard MCU navigation commands and companion page metadata.

## v0.6.12

- Fixed all GLD scribble-strip names flashing while Pan is moved in REAPER.
  REAPER temporarily writes parameter text to the MCU display during a V-Pot
  gesture; while the companion script is connected, the companion snapshot is
  now the authoritative track-name source and transient MCU display text is
  ignored.
- Unnamed REAPER tracks now use compact `Trk01` … `Trk32` fallback names so
  every default name fits the GLD's five-character strip display and remains
  distinguishable.
- Updated the bundled REAPER companion script to v1.2.

## 0.6.9

- Added the reverse-engineered GLD Editor absolute Pan frame captured on TCP port `51321`.
- Verified endpoint frames for MIDI Strip 1 (`DD/3D`) and MIDI Strip 32 (`FC/5C`); both identifiers increment once per strip.
- DAW and bridge Pan changes now use the Editor frame so the physical scribble-strip Pan bar redraws in real time.
- Retained the public `B2 20..3F <value>` message as a fallback when the Editor-control socket is unavailable.
- Updated the UI and documentation to explain that TCP `51321` now provides Pan LCD redraw as well as names and colours.

## 0.6.8

- Fixed the remaining Mute synchronization race. After a physical GLD or bridge-UI click, the requested MCU/HUI tally is held for a short settling window so a stale previous DAW tally or reflected press/release pair cannot immediately undo the new state.
- Kept direct DAW Mute changes authoritative again as soon as the settling window expires. The same protection is applied to Mix/Select and PAFL/Solo.
- Removed the ineffective Pan adjacent-value refresh pulse. The bridge now sends one exact `B2 20..3F <VAR>` value; the GLD stores it correctly, but its strip LCD only redraws the remotely changed rotary after the rotary function is switched away and back.
- Added regression coverage for stale DAW Mute tallies, reflected MCU press/release messages, settling-window expiry and exact single-message Pan output.

## 0.6.5

- Fixed motor-fader startup and automation handling. An initial GLD fader report during the connection startup window is now treated as a baseline, so it cannot block the DAW's initial fader snapshot.
- Replaced the blanket GLD-fader ownership timer with short value-aware echo suppression. Genuine DAW fader moves can now reach the GLD immediately while reflected GLD movements are still rejected.
- Fixed exact REAPER Pan races: a stale companion snapshot can no longer overwrite a pending GLD Pan command before REAPER confirms it.
- Ignored coarse MCU/HUI Pan-ring feedback briefly after a physical GLD Pan movement, preventing the desk and DAW from pulling each other to different values.
- Corrected the Windows executable fixed version metadata to 0.6.5.

## 0.6.4

- Reworked GLD key handling to stop rapid Mute/Mix/PAFL presses from double-triggering. Momentary key presses now use a release-aware latch, stateful Local-ON messages are handled separately, and bridge feedback uses distinctive valid GLD velocities so returned tally messages are consumed instead of treated as another click.
- Removed the experimental Pan re-centering mode. The GLD Pan value is now kept as one bounded absolute 0–127 position, while generic MCU/HUI hosts still receive relative encoder ticks.
- Extended the bundled REAPER companion to provide exact bidirectional Pan for tracks 1–32. REAPER Pan changes now update the bridge and GLD, and GLD/UI Pan changes are applied directly to REAPER without relying on coarse MCU LED-ring positions.
- Added repeated, short-lived REAPER Pan command confirmation so rapid turns are not lost to temporary-file timing.
- Updated the REAPER companion snapshot format to version 2 while retaining read compatibility with version 1.
- Added an uninstaller prompt that can optionally remove `%USERPROFILE%\.gld80_mcu_bridge` and all saved settings.
- Confirmed and documented the Windows MIDI Services A/B setup: bridge B/B and REAPER A/A, or the exact reverse.

## 0.6.3

- Corrected Windows app-to-app endpoint guidance: with an A/B virtual cable, the bridge uses one side for both input and output, while REAPER uses the opposite side for both input and output. Crossing A/B inside either application creates a direct feedback loop.
- Added a stronger two-way fader ownership guard. GLD-originated fader movements temporarily ignore DAW echoes, and GLD motor-position reports caused by genuine DAW automation are no longer returned to the DAW.
- Added human-override detection when a motor fader moves away from its commanded target.
- Changed the physical GLD Pan control to an optional endless-encoder mode. The bridge re-centres the GLD MIDI Strip Pan accumulator after each movement and sends signed relative MCU/HUI ticks.
- Changed Pan sensitivity to a speed multiplier and reset the recommended/default value to `1`.
- Added connection logging when different bridge input/output endpoint names are selected, because that is only correct for explicitly separate one-way ports.

## 0.6.2

- Removed the DCA/SoftKey workaround from Vegas mode. Vegas now touches only MIDI Strip faders, Mute/Mix/PAFL indicators and optional strip colours.
- Fixed MCU/HUI Pan translation by establishing a GLD absolute-position baseline, rejecting implausible jumps and ignoring invalid V-Pot LED-ring positions that could force Pan to 100% right.
- Added a Pan feedback guard so DAW ring feedback cannot create a control loop back into the GLD.
- Added validation for complete, unique and consecutive MCU/HUI endpoint pairs.
- Added saved MIDI port selections and a per-bank connection log for tracks 1–8, 9–16, 17–24 and 25–32.
- Expanded the REAPER guide with the exact Universal/Extender setup, offsets 0/8/16/24 and Size tweak 8.
- Updated Windows MIDI guidance for Windows MIDI Services/MIDI 2.0 app-to-app endpoints and other endpoint pairs visible to both applications.
- Added the project GitHub link to the menu and About/limitations dialog.

## 0.6.1

- Changed the default colour for new MIDI Strips from Off to White.
- Migrated untouched all-Off 32-strip configurations to White while preserving mixed/manual colour choices.
- Added a bundled REAPER Lua companion script for the first 32 project-track names and exact RGB colours.
- Added automatic hue-based reduction from REAPER RGB to Red, Yellow, Green, Light blue, Blue, Purple or White. Dark/light variants remain in the same hue family; no custom colour defaults to White.
- Added a REAPER sync checkbox, live companion status and an in-app button that opens the dedicated setup guide.
- Added background snapshot polling with stale-file detection and batched label persistence.

## 0.6.0

- Added a selectable DAW protocol mode: MCU, HUI or transparent Raw MIDI.
- Kept MCU as the default and retained four 8-channel MCU port pairs for 32 tracks.
- Added four 8-channel HUI surfaces for Pro Tools with ping/reply, 14-bit faders, timed fader-touch emulation, Pan V-Pots, Mute, Solo, Select, LED feedback and HUI display-name parsing.
- Added Raw MIDI passthrough through the first DAW port pair. GLD-to-app traffic and app-to-GLD traffic are forwarded without translation while known strip messages still update the UI.
- Added protocol-specific routing labels, setup guidance and automatic disabling of unused Raw MIDI banks.
- Prevented changing the DAW protocol while connected.
- Migrated existing configurations to MCU mode by default.

## 0.5.5

- Removed the complete fader-smoothing subsystem, its UI controls, configuration keys and module.
- Forwarded GLD, DAW, UI and Vegas fader values directly with no generated intermediate steps.
- Migrated existing settings to remove obsolete smoothing options.

## 0.5.4

- Corrected the bridge fader readout using measured GLD-80 MIDI Strip control-position anchors.
- Removed all Dutch documentation and user-facing installer text.

## 0.5.3

- Corrected MCU button clicks and feedback handling for Mute, Mix and PAFL.
- Added direct UI-to-GLD pan feedback and common MCU V-Pot ring parsing.
- Added echo suppression and debounce for GLD key feedback.

## 0.5.2

- Added physical MIDI Strip 1–32 name/colour writes through the captured GLD Editor protocol on TCP 51321.
- Confirmed direct addresses for names `00–1F` and colours `20–3F`.
- Added immediate physical label updates, Sync all labels and Vegas colour animation/restoration.
