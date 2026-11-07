"""The zlib payload codec shared by Warden and the module workers."""

from __future__ import annotations

from temporalio.api.common.v1 import Payload

from tests.modules.conftest import NAMES, load
from warden.codec import ENCODING, ZlibCodec, data_converter


async def test_round_trip_and_passthrough() -> None:
    codec = ZlibCodec()
    original = data_converter().payload_converter.to_payloads([["x.example.com"] * 1000])
    encoded = await codec.encode(original)
    assert encoded[0].metadata["encoding"] == ENCODING and len(encoded[0].data) < len(original[0].data) / 10
    assert await codec.decode(encoded) == original
    plain = Payload(metadata={"encoding": b"json/plain"}, data=b'"x"')
    assert await codec.decode([plain]) == [plain]  # payloads from before the codec pass through


async def test_every_module_carries_the_same_codec() -> None:
    warden = await ZlibCodec().encode([Payload(metadata={"encoding": b"json/plain"}, data=b'"abc"')])
    for name in NAMES:
        module_codec = load(name).helpers.ZlibCodec()
        assert await module_codec.decode(warden) == await ZlibCodec().decode(warden)
        assert await ZlibCodec().decode(await module_codec.encode(warden)) == warden
