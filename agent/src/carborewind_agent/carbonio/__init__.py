"""Integration with Carbonio CE (SOAP Admin/Mail and REST)."""

from .client import MUST_RESYNC, AccountInfo, CarbonioClient, FolderInfo, SyncDelta
from .soap import SoapClient, SoapFaultError

__all__ = [
    "MUST_RESYNC",
    "AccountInfo",
    "CarbonioClient",
    "FolderInfo",
    "SoapClient",
    "SoapFaultError",
    "SyncDelta",
]
