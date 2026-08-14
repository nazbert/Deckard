from dataclasses import dataclass, field

from .ImageLayer import ImageLayer
from PIL import Image
from loguru import logger as log


@dataclass(eq=False)
class Media:
    """
    A stack of image layers plus the placement of the composed result.

    Args:
        size (float, optional): The size of the media. Defaults to 1.0.
        halign (float, optional): The horizontal alignment of the media. Defaults to 0.0.
        valign (float, optional): The vertical alignment of the media. Defaults to 0.0.
        layers (list[ImageLayer], optional): The list of image layers. Defaults to an empty list.
    """
    size: float = 1.0
    halign: float = 0.0
    valign: float = 0.0
    layers: list[ImageLayer] = field(default_factory=list)

    @classmethod
    def from_path(cls, path: str, size: float = 1.0, halign: float = 0.0, valign: float = 0.0):
        """
        Creates a new media by adding the media path directly as a new layer.

        Args:
            path (str): The path to the media.
            size (float, optional): The size of the media. Defaults to 1.0.
            halign (float, optional): The horizontal alignment of the media. Defaults to 0.0.
            valign (float, optional): The vertical alignment of the media. Defaults to 0.0.

        Returns:
            Media: A new Media object with the added image layer.
        """
        # from_image_path returns None for a path that is neither an image nor
        # an SVG. Do not build a [None] layer list: get_final_media reads
        # self.layers[0].image before its own layer guard runs, so an unusable
        # path dies inside the composite on an arbitrary worker thread. An
        # empty layer list makes get_final_media return None, which is the
        # documented empty-media result.
        #
        # The consumers still do not handle None. PluginSettingsWindow feeds
        # the result into image2pixbuf, and PluginSettings and Asset store it
        # as _rendered. This moves the failure onto a caller that a guard can
        # reach.
        layer = ImageLayer.from_image_path(path)
        layers = [layer] if layer is not None else []
        return cls(size=size, halign=halign, valign=valign, layers=layers)

    def add_layer(self, layer: ImageLayer | list[ImageLayer]):
        """
        Adds a layer to the media.

        Args:
            layer (ImageLayer | list[ImageLayer]): The layer or a list of layers to add.
        """
        if not layer:
            log.error(f"Error while adding layer! Layer is: {layer}")
            return

        if isinstance(layer, list):
            self.layers.extend(layer)
        elif isinstance(layer, ImageLayer):
            self.layers.append(layer)

    def append_layer(self, *args: ImageLayer | list[ImageLayer]):
        """
        Adds layers below the current existing ones.

        Args:
            *args (ImageLayer | list[ImageLayer]): The layers or a list of layers to add.
        """
        if not args:
            log.error(f"Error while adding layer below! Layer is: {args}")
            return

        for arg in args:
            if isinstance(arg, list):
                self.layers.extend(arg)
            elif isinstance(arg, ImageLayer):
                self.layers.append(arg)

    def prepend_layer(self, *args: ImageLayer | list[ImageLayer]):
        """
        Adds layers on top of the current existing ones.

        Args:
            *args (ImageLayer | list[ImageLayer]): The layers or a list of layers to add.
        """
        if not args:
            log.error(f"Error while adding layer on top! Layer is: {args}")
            return

        result = []

        for arg in args:
            if isinstance(arg, list):
                result.extend(arg)
            elif isinstance(arg, ImageLayer):
                result.append(arg)

        result.extend(self.layers)
        self.layers = result

    def get_final_media(self) -> Image.Image | None:
        """
        Transforms the layers list into a final image. The result of this image should be used sparingly because it takes some processing power and could be slow.

        Returns:
            Image: The final image that can be displayed by using set_media,
                or None when there are no layers to compose.
        """
        if not self.layers or len(self.layers) <= 0:
            return None

        pre_image = Image.new('RGBA', self.layers[0].image.size)

        for layer in self.layers:
            if not layer:
                log.error("Layer not defined")
                continue
            image, position = layer.transform(pre_image.size)
            pre_image.paste(image, position, image)

        post_image_layer = ImageLayer(image=pre_image, size=self.size, halign=self.halign, valign=self.valign)

        base_image = Image.new('RGBA', post_image_layer.image.size)

        image, position = post_image_layer.transform(base_image.size)

        base_image.paste(image, position, image)

        return base_image