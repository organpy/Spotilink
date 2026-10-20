from __future__ import annotations

from .base import Provider
from .soundcloud import SoundCloudProvider
from .youtube import YouTubeProvider

__all__ = ["Provider", "SoundCloudProvider", "YouTubeProvider"]
