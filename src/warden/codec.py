"""zlib compression for every Temporal payload between Warden and the fleet.

Module activities pass lists of hostnames between steps and return their output
files; compressed, a 2 MB payload holds about ten times more. Warden's client and
every module worker use this codec (each module carries its own copy in
helpers.py). Payloads without the codec's encoding (for example from before it
was introduced) are passed through unchanged.
"""

from __future__ import annotations

import dataclasses
import zlib
from collections.abc import Sequence

import temporalio.converter
from temporalio.api.common.v1 import Payload

ENCODING = b"binary/zlib"


class ZlibCodec(temporalio.converter.PayloadCodec):
    async def encode(self, payloads: Sequence[Payload]) -> list[Payload]:
        return [
            Payload(metadata={"encoding": ENCODING}, data=zlib.compress(p.SerializeToString()))
            for p in payloads
        ]

    async def decode(self, payloads: Sequence[Payload]) -> list[Payload]:
        out = []
        for p in payloads:
            if p.metadata.get("encoding") == ENCODING:
                out.append(Payload.FromString(zlib.decompress(p.data)))
            else:
                out.append(p)
        return out


def data_converter() -> temporalio.converter.DataConverter:
    return dataclasses.replace(temporalio.converter.default(), payload_codec=ZlibCodec())
