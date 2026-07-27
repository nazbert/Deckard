from dataclasses import dataclass


@dataclass(slots=True)
class MediaConfig:
    """
    The "media" section of a page/state dict, extracted once.

    Distinct from Media (a composed stack of image layers): MediaConfig is
    plain configuration -- what the page JSON says about a state's media --
    read out of ``state_dict.get("media", {})`` in one place instead of a
    chained ``.get().get()`` per field.

    Args:
        path (str, optional): The path to the media file. Defaults to None.
        loop (bool, optional): Whether video media loops. Defaults to True.
        fps (int, optional): The video frame rate. Defaults to 30.
        fill_mode (str, optional): How the media fills the input. Defaults to None.
        size (float, optional): The size of the media. Defaults to None.
        valign (float, optional): The vertical alignment of the media. Defaults to None.
        halign (float, optional): The horizontal alignment of the media. Defaults to None.
    """
    path: str = None
    loop: bool = True
    fps: int = 30
    fill_mode: str = None
    size: float = None
    valign: float = None
    halign: float = None

    @classmethod
    def from_dict(cls, d: dict) -> "MediaConfig":
        """
        Creates a MediaConfig from a raw media dict (kebab-case keys, as
        stored in the page JSON).

        Args:
            d (dict): The raw media section, e.g. ``state_dict.get("media", {})``.

        Returns:
            MediaConfig: A new MediaConfig with missing keys at their defaults.
        """
        return cls(
            path=d.get("path"),
            loop=d.get("loop", True),
            fps=d.get("fps", 30),
            fill_mode=d.get("fill-mode"),
            size=d.get("size"),
            valign=d.get("valign"),
            halign=d.get("halign"),
        )
