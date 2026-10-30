"""ClickBridge -- connect a Zwift Click (or Play/Ride controller) to MyWhoosh.

How it works
------------
ClickBridge is just a translator between two well-defined roles:

1. *BLE central side*  : it connects to your Zwift Click over Bluetooth and
   decodes button press/release events (both the plain "RideOn"/SF2 protocol
   used by the original Click + Zwift Ride, and the legacy ECDH-encrypted
   handshake found on older firmware).
2. *OpenBikeControl side*: it advertises the open OpenBikeControl protocol
   (mDNS + TCP, identical binary format as the BLE transport).  MyWhoosh
   5.7+ natively supports this protocol on every platform.

The result: your Click's buttons behave like a first-class controller inside
MyWhoosh -- virtual gear shifting and more, with no third-party bridge app.
"""

__version__ = "1.0.0"
APP_NAME = "ClickBridge"
