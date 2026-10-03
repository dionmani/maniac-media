import os

__version__ = "2.1.1"
# short git sha baked into the Docker image (empty for a source checkout)
__build__ = os.environ.get("MANIAC_REVISION", "")[:7]
