import io
import warnings
from pathlib import Path

from PIL import Image, UnidentifiedImageError

from .errors import OCRException


def read_path(path, config):
    candidate = Path(path).expanduser().resolve()
    roots = [Path(root).resolve() for root in config['allowed_path_roots']]
    if not any(candidate.is_relative_to(root) for root in roots):
        raise OCRException(422, 'path_not_allowed', 'Image path is outside configured allowed roots.')
    try:
        if not candidate.is_file():
            raise OCRException(422, 'invalid_path', 'Image path must point to a readable regular file.')
        with candidate.open('rb') as stream:
            data = stream.read(config['max_bytes'] + 1)
    except OSError as exc:
        raise OCRException(422, 'invalid_path', 'Unable to read image path.') from exc
    return data


def validate_image(data, config):
    if not data:
        raise OCRException(422, 'empty_image', 'Image is empty.')
    if len(data) > config['max_bytes']:
        raise OCRException(413, 'image_too_large', 'Image exceeds configured byte limit.')
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as image:
                mime = config['allowed_formats'].get(image.format)
                if not mime:
                    raise OCRException(415, 'unsupported_image', 'Image format is not supported.')
                if image.width * image.height > config['max_pixels']:
                    raise OCRException(413, 'too_many_pixels', 'Image exceeds configured pixel limit.')
                if not config['allow_animated'] and getattr(image, 'n_frames', 1) != 1:
                    raise OCRException(422, 'animated_image', 'Only a single image frame is supported.')
                image.verify()
            # Decode only to validate pixel data; never transform or re-encode it.
            with Image.open(io.BytesIO(data)) as image:
                image.load()
            return mime
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError, Image.DecompressionBombWarning, Image.DecompressionBombError) as exc:
        raise OCRException(422, 'invalid_image', 'Image is corrupt or cannot be validated.') from exc


def isThisImageAPrescription(image):
    """Future prescription classifier. Deliberately accepts every validated image."""
    return True
