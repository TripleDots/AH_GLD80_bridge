from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ChannelState:
    index: int
    name: str = field(default_factory=str)
    fader: int = 0
    pan: int = 64
    mute: bool = False
    solo: bool = False
    select: bool = False
    record: bool = False
    colour: str = "white"

    def __post_init__(self) -> None:
        if not self.name:
            self.name = f"MIDI {self.index + 1:02d}"
